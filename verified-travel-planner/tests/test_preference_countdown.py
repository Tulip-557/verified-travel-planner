#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preference_check（偏好覆盖体检）与 countdown（临行倒排清单）的机器断言。

preference_check 四类判据：
  ① P1 偏好已声明：有 preferences → OK；没有 → WARN（采集了却不呈现才提醒）；
  ② P2 人群适配：variant=seniors 动线出现「夜排/暴走」等冲突词 → 逐条点名；
     couple 干净动线 → OK；
  ③ P3 特殊时段玩法：含「日落/星空」的 slot 必须带「早起包/夜游包/自愿/作息代价」
     交代，全文都没有 → WARN（SKILL.md 阶段 4 红线：不许静默砍掉）；
  ④ P4 特种兵打卡数：style=fast 或正文自称特种兵时，必须显性「打卡 N 个点」。

countdown 三类判据：
  ⑤ 规则锚点齐全：T-5/T-3/T-1/T-0 四档都在，且每条带 source 出处；
  ⑥ 数据锚点：门票 [D] 项挂到 T-5、退改条款句挂到 T-1；_使用说明 等元字段不混入；
  ⑦ 出发日解析：缺 date_range 且无 --on → 退出码 1（不猜基准日）。

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
PREF = _SKILL_ROOT / 'tools' / 'preference_check.py'
CD = _SKILL_ROOT / 'tools' / 'countdown.py'


def _facts(**meta_extra):
    doc = {
        "_使用说明": "测试事实源。门票未核实 [D]——官方渠道自查（这句在 _ 字段里，不该进清单）。",
        "meta": {"destination": "测试", "date_range": "2026-11-07 ~ 2026-11-08",
                 "variant": "couple"},
        "preferences": {"pace": "balanced", "interests": ["城市地标"]},
        "days": [{"title": "D1", "slots": [
            {"time": "10:00", "body": {"full": "白天逛景点。"}},
            {"time": "17:30", "body": {"full": "崖口看日落。"}},
        ]}],
    }
    doc['meta'].update(meta_extra)
    return doc


def _run(tool, *extra):
    proc = subprocess.run([sys.executable, str(tool)] + list(extra),
                          capture_output=True, timeout=120)
    return proc.returncode, proc.stdout.decode('utf-8')


def _write(tmp, doc, name='路书_测试.json'):
    p = Path(tmp) / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return p


class TestPreferenceCheck(unittest.TestCase):
    """①②③④ 偏好体检。"""

    def test_clean_facts_all_ok(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, _facts())
            rc, out = _run(PREF, str(p), '--json')
            rep = json.loads(out)
            self.assertEqual(rc, 0)
            # 干净版：P1 OK；日落 slot 无交代 → P3 应 WARN（这正是要抓的）
            p1 = [i for i in rep['items'] if i['rule'].startswith('P1')]
            self.assertEqual(p1[0]['status'], 'OK')
            p3 = [i for i in rep['items'] if i['rule'].startswith('P3')]
            self.assertTrue(any(i['status'] == 'WARN' and '日落' in i['detail'] for i in p3))

    def test_special_time_with_pack_ok(self):
        doc = _facts()
        doc['days'][0]['slots'][1]['body']['full'] = '崖口看日落（夜游包·自愿，代价 19:30 后才能吃饭）。'
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            _, out = _run(PREF, str(p), '--json')
            rep = json.loads(out)
            p3 = [i for i in rep['items'] if i['rule'].startswith('P3')]
            self.assertTrue(any(i['status'] == 'OK' and '日落' in i['detail'] for i in p3))

    def test_seniors_conflict_words(self):
        doc = _facts(variant='seniors')
        doc['days'][0]['slots'].append(
            {"time": "23:00", "body": {"full": "夜排等日出，特种兵暴走模式。"}})
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            _, out = _run(PREF, str(p), '--json')
            rep = json.loads(out)
            p2 = [i for i in rep['items'] if i['rule'].startswith('P2')]
            self.assertTrue(any(i['status'] == 'WARN' for i in p2))

    def test_no_preferences_warns(self):
        doc = _facts()
        doc.pop('preferences')
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            _, out = _run(PREF, str(p), '--json')
            rep = json.loads(out)
            p1 = [i for i in rep['items'] if i['rule'].startswith('P1')]
            self.assertEqual(p1[0]['status'], 'WARN')

    def test_fast_style_needs_explicit_count(self):
        doc = _facts()
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            req = Path(td) / 'req.json'
            req.write_text(json.dumps({'style': 'fast'}), encoding='utf-8')
            _, out = _run(PREF, str(p), '--request', str(req), '--json')
            rep = json.loads(out)
            p4 = [i for i in rep['items'] if i['rule'].startswith('P4')]
            self.assertEqual(p4[0]['status'], 'WARN')
            # 给了显性数字后转 OK
            doc['days'][0]['slots'].append(
                {"time": "20:00", "body": {"full": "本趟打卡 12 个点，收官行计数。"}})
            p2 = _write(td, doc, '路书_测试2.json')
            _, out2 = _run(PREF, str(p2), '--request', str(req), '--json')
            rep2 = json.loads(out2)
            p4b = [i for i in rep2['items'] if i['rule'].startswith('P4')]
            self.assertEqual(p4b[0]['status'], 'OK')


class TestCountdown(unittest.TestCase):
    """⑤⑥⑦ 倒排清单。"""

    def test_rule_anchors_and_sources(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, _facts())
            rc, out = _run(CD, str(p), '--json')
            rep = json.loads(out)
            self.assertEqual(rc, 0)
            ts = {i['t_minus'] for i in rep['items']}
            self.assertEqual(ts, {5, 3, 1, 0})
            self.assertTrue(all(i.get('source') for i in rep['items']))
            self.assertEqual(rep['departure'], '2026-11-07')

    def test_meta_fields_not_leaked(self):
        # _使用说明 里的 [D] 句子不该变成 T-5 动作
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, _facts())
            _, out = _run(CD, str(p), '--json')
            rep = json.loads(out)
            t5 = ' '.join(i['action'] for i in rep['items'] if i['t_minus'] == 5)
            self.assertNotIn('测试事实源', t5)

    def test_missing_departure_exit1(self):
        doc = _facts()
        doc['meta'].pop('date_range')
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            rc, _ = _run(CD, str(p))
            self.assertEqual(rc, 1)

    def test_refund_clause_listed(self):
        doc = _facts()
        doc['days'][0]['slots'].append(
            {"time": "21:00", "body": {"full": "酒店选了可免费取消的房型。"}})
        with tempfile.TemporaryDirectory() as td:
            p = _write(td, doc)
            _, out = _run(CD, str(p), '--json')
            rep = json.loads(out)
            t1 = ' '.join(i['action'] for i in rep['items'] if i['t_minus'] == 1)
            self.assertIn('免费取消', t1)


if __name__ == '__main__':
    unittest.main()
