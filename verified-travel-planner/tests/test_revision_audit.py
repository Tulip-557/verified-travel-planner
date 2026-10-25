#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""revision_audit（改版审计）的机器断言。

覆盖四类判据：
  ① 数值变化：同声明里 ¥60→¥80 按单位分桶现形（改版是 claim_audit 的时间轴版）；
  ② 等级降级显式化：[A]→[C] 必须被点名；悄悄降级在 --strict 下退出码 2，
     在 v2 的 meta.revision_log 用 about 子串声明后即放行（留痕即合规）；
  ③ 增删声明：v1 独有 = 删除（[A] 被删要能说出为什么）、v2 独有 = 新增；
  ④ 双档只取 full：同一句 full/compact 各写一遍不产生假变化。

运行（零第三方依赖，标准库 unittest）：
    python -m unittest discover -s verified-travel-planner/tests -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
REVISION = _SKILL_ROOT / 'tools' / 'revision_audit.py'

V1 = {
    "meta": {"destination": "测试市", "date_range": "2026-10-30 ~ 2026-10-31"},
    "days": [
        {"title": "D1", "slots": [
            {"time": "09:00", "body": {"full":
                "门票 ¥60（2026-10-25 查·官网）[A]。车程 16 分钟（2026-10-25 查·高德）[A]。"}},
            {"time": "12:00", "body": {"full":
                "海边日落观景点，顺路可去 [C·单源]。"}},
        ]},
    ],
    "sections": [
        {"id": "budget", "body": "人均预算 ¥300。"},
    ],
}


def _mutate(**kw):
    """从 V1 深拷贝后按命名开关改出 v2。"""
    doc = json.loads(json.dumps(V1))
    s0 = doc['days'][0]['slots'][0]['body']['full']
    if kw.get('price'):
        s0 = s0.replace('¥60', '¥80')
    if kw.get('downgrade'):
        s0 = s0.replace('门票 ¥60', '门票 ¥60').replace(
            '（2026-10-25 查·官网）[A]', '（游客转述）[C·单源]')
    if kw.get('upgrade'):
        s0 = s0.replace('顺路', '顺路')  # 占位，保持结构
    doc['days'][0]['slots'][0]['body']['full'] = s0
    if kw.get('drop_slot'):
        doc['days'][0]['slots'] = doc['days'][0]['slots'][:1]
    if kw.get('add_claim'):
        doc['days'][0]['slots'].append(
            {"time": "18:00", "body": {"full": "夜市人均 ¥50（2026-10-26 查·点评）[B]。"}})
    if kw.get('log'):
        doc['meta']['revision_log'] = kw['log']
    return doc


def _pair(tmp: Path, v1=None, v2=None):
    p1 = tmp / 'v1.json'
    p2 = tmp / 'v2.json'
    p1.write_text(json.dumps(v1 or V1, ensure_ascii=False), encoding='utf-8')
    p2.write_text(json.dumps(v2 or V1, ensure_ascii=False), encoding='utf-8')
    return p1, p2


def _run(*extra):
    proc = subprocess.run([sys.executable, str(REVISION)] + list(extra),
                          capture_output=True, timeout=60)
    return proc.returncode, proc.stdout.decode('utf-8')


class TestValueAndGradeChanges(unittest.TestCase):
    """①② 数值变化与等级降级。"""

    def test_price_change_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td), v2=_mutate(price=True))
            rc, out = _run(str(p1), str(p2), '--json')
            self.assertEqual(rc, 0)
            rep = json.loads(out)
            self.assertEqual(len(rep['changes']), 1)
            vc = rep['changes'][0]['value_changes']
            self.assertIn({'unit': '¥', 'old': ['60'], 'new': ['80']}, vc)

    def test_downgrade_undeclared_strict_fails(self):
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td), v2=_mutate(price=True, downgrade=True))
            rc, out = _run(str(p1), str(p2))
            self.assertEqual(rc, 0)                      # 默认只报告
            self.assertIn('降级 1 处', out)
            rc2, _ = _run(str(p1), str(p2), '--strict')
            self.assertEqual(rc2, 2)                     # 悄悄降级必须拦

    def test_downgrade_declared_passes_strict(self):
        log = [{"about": "门票", "from": "A", "to": "C", "reason": "官网停更，降级并给出路"}]
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td), v2=_mutate(price=True, downgrade=True, log=log))
            rc, out = _run(str(p1), str(p2), '--strict')
            self.assertEqual(rc, 0)                      # 留痕即合规
            self.assertIn('已声明 1', out)


class TestAddRemove(unittest.TestCase):
    """③ 增删声明。"""

    def test_removed_and_added(self):
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td), v2=_mutate(drop_slot=True, add_claim=True))
            rc, out = _run(str(p1), str(p2), '--json')
            rep = json.loads(out)
            self.assertEqual(rc, 0)
            # 日落句（[C·单源]）被删、夜市句（[B]）新增
            self.assertTrue(any('日落' in r['text'] for r in rep['removed']))
            self.assertTrue(any('夜市' in a['text'] for a in rep['added']))


class TestEdges(unittest.TestCase):
    """④ 双档与退出码。"""

    def test_identical_files_no_changes(self):
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td))
            rc, out = _run(str(p1), str(p2), '--strict')
            self.assertEqual(rc, 0)
            self.assertIn('变化 0', out)

    def test_dual_slot_counted_once(self):
        # full/compact 各写一遍同一句：声明单元数应等于单写 full（不产生假变化）
        v1 = json.loads(json.dumps(V1))
        v1['days'][0]['slots'][0]['body']['compact'] = \
            v1['days'][0]['slots'][0]['body']['full']
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td), v1=v1)
            rc, out = _run(str(p1), str(p2))
            self.assertEqual(rc, 0)
            self.assertIn('变化 0', out)

    def test_missing_file_exit1(self):
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = _pair(Path(td))
            rc, _ = _run(str(p1), str(Path(td) / '不存在.json'))
            self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
