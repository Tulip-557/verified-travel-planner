#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evidence_report（证据健康度一页纸）的机器断言。

覆盖四类判据：
  ① 徽章统计：[A]/[B]/[C]/[C·单源]/[D] 各自计数、全 0 时显式告警；
  ② [D] 未核实清单：图例句（同时含 [A][B] 或自称「证据等级」）不混进清单、
     同句去重——东莞实测里图例句混过首条，稀释了清单可信度；
  ③ 闸门壳状态机：run_gate 对「过 / 校验未过 / 执行错」三态的翻译——
     校验未过(退出码 2)时 JSON 仍要取得到，报告要展示败在哪；
  ④ 端到端：合成事实源 → aggregate → HTML 单文件自包含（无外部引用），
     桩件自述（meta.render_fixture）时内容闸门 SKIP 而不是硬跑。

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
# 与 test_compact_draft.py 同一约定：工具模块走 sys.path（tools/ 无包名）。
sys.path.insert(0, str(_SKILL_ROOT / 'tools'))

from evidence_report import (  # noqa: E402
    _parse_json_stdout, badge_stats, d_items, is_render_fixture, run_gate,
)

REPORT = _SKILL_ROOT / 'tools' / 'evidence_report.py'

# 合成事实源：两枚 [A]、一枚 [C·单源]、两枚 [D]（其中一句是图例，应被过滤）
FACTS = {
    "meta": {"destination": "测试市", "date_range": "2026-10-30 ~ 2026-10-31"},
    "days": [
        {"title": "D1", "slots": [
            {"time": "09:00", "body": {"full":
                "车程 16 分钟（2026-10-25 查·高德）[A]。</br>"
                "含数字的句子已挂证据等级标：[A] 工具实证、[B] 双源、[C·单源] 单源、[D] 未核实。"}},
            {"time": "12:00", "body": {"full":
                "人均未核实 [D] ——出发前用点评自行确认。人均未核实 [D] ——出发前用点评自行确认。"}},
        ]},
    ],
    "sections": [
        {"id": "budget", "body": "门票 ¥60（2026-10-25 查·官网）[A]。"},
    ],
}


def _write_facts(tmp: Path, name='路书_测试.json', meta_extra=None):
    doc = json.loads(json.dumps(FACTS))          # 深拷贝，别让用例互相污染
    if meta_extra:
        doc['meta'].update(meta_extra)
    p = tmp / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return p


def _run(*extra):
    args = [sys.executable, str(REPORT)] + list(extra)
    proc = subprocess.run(args, capture_output=True, timeout=180)
    return proc.returncode, proc.stdout.decode('utf-8')


class TestBadgeStats(unittest.TestCase):
    """① 徽章统计口径。"""

    def test_c_counts_c_plain_and_c_danYuan(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_facts(Path(td))
            stats = badge_stats(p)
        # 计的是原始文本里的全部徽章（与 source_audit E1 同口径）：
        # [A] 车程 1 + 图例 1 + 门票 1；图例里的 [B]/[C·单源]/[D] 同样计入。
        self.assertEqual(stats['A'], 3)
        self.assertEqual(stats['B'], 1)
        self.assertEqual(stats['C'], 1)            # [C·单源] 也计入 C 档
        self.assertEqual(stats['D'], 3)
        self.assertEqual(stats['total'], 8)

    def test_zero_badges_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / '空.json'
            p.write_text(json.dumps({'meta': {}, 'days': []}), encoding='utf-8')
            stats = badge_stats(p)
        self.assertEqual(stats['total'], 0)


class TestDItems(unittest.TestCase):
    """② [D] 清单：图例不进清单、同句去重。"""

    def test_legend_excluded_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_facts(Path(td))
            items = d_items(p)
        # 图例句（含 [A]+[B]）必须被滤掉；重复句去重后只留一条 → 清单恰 1 条
        self.assertEqual(len(items), 1)
        self.assertTrue(all('证据等级' not in it for it in items))
        self.assertTrue(any('人均未核实' in it for it in items))


class TestRunGateStates(unittest.TestCase):
    """③ 三态翻译：OK / FAIL（退出码 2 但 JSON 在）/ ERROR（退出码 1）。"""

    def test_ok(self):
        g = run_gate(['-c', 'import json;print(json.dumps({"v": 1}))'])
        self.assertEqual(g['status'], 'OK')
        self.assertEqual(g['data'], {'v': 1})

    def test_fail_keeps_json(self):
        # 退出码 2 = 校验未过。报告要展示败在哪，data 不能丢。
        code = ('import json,sys;'
                'print(json.dumps({"rules": {"E1": {"status": "FAIL"}}}));'
                'sys.exit(2)')
        g = run_gate(['-c', code])
        self.assertEqual(g['status'], 'FAIL')
        self.assertEqual(g['code'], 2)
        self.assertIn('E1', g['data']['rules'])

    def test_error_on_exit1(self):
        g = run_gate(['-c', 'print("boom"); raise SystemExit(1)'])
        self.assertEqual(g['status'], 'ERROR')
        self.assertIsNone(g['data'])

    def test_parse_json_stdout_tolerates_prefix(self):
        data, err = _parse_json_stdout('警告行\n{"a": 1}\n')
        self.assertIsNone(err)
        self.assertEqual(data, {'a': 1})
        data, err = _parse_json_stdout('根本不是 JSON')
        self.assertIsNone(data)
        self.assertTrue(err)


class TestFixtureAndCLI(unittest.TestCase):
    """④ 桩件自述 + 端到端 CLI。"""

    def test_render_fixture_self_declared(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_facts(Path(td), meta_extra={'render_fixture': True})
            self.assertTrue(is_render_fixture(p))
            p2 = _write_facts(Path(td), name='路书_非桩件.json')
            self.assertFalse(is_render_fixture(p2))   # 不写 meta 就不是——判据是自述不是推断

    def test_cli_json_output_and_selfcontained_html(self):
        with tempfile.TemporaryDirectory() as td:
            _write_facts(Path(td))
            facts = Path(td) / '路书_测试.json'
            rc, out = _run(str(facts), '--json')
            self.assertEqual(rc, 0)                    # 报告不是闸门：有 FAIL 也是 0
            rep = json.loads(out)
            self.assertEqual(rep['badges']['A'], 3)
            for gate in rep['gates'].values():
                self.assertIn(gate['status'], ('OK', 'FAIL', 'SKIP', 'ERROR'))
            # echo/cross 默认不联网，必须显式 SKIP 且带原因（不静默）
            self.assertEqual(rep['gates']['echo_audit']['status'], 'SKIP')
            self.assertTrue(rep['gates']['echo_audit']['note'])

            rc2, out2 = _run(str(facts))
            self.assertEqual(rc2, 0)
            html_path = Path(td) / '可信度概览_测试.html'
            self.assertTrue(html_path.exists())
            html = html_path.read_text(encoding='utf-8')
            self.assertNotIn('http://', html)          # 单文件自包含，零外部引用
            self.assertNotIn('https://', html)
            self.assertIn('证据徽章分布', html)


if __name__ == '__main__':
    unittest.main()
