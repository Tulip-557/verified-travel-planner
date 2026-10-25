#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quality_report（行程质量评分）的机器断言。

覆盖四类判据：
  ① 节奏红线：活动数超上限 / 步行量超上限 → RED（--strict 退出码 2），
     单点停留 <40 分钟 → WARN；上限出处必须写进规则文本（mobility > style > 缺省）；
  ② 成本口径：itinerary.budget_cny 一律按总额；request 人均×人头与总额差 >5%
     时**并列报出不裁决**；超预算 → RED；MEAL/ATTRACTION 零成本 → WARN；
  ③ 体验匹配：must_visit 双向子串匹配（「可园」↔「可园博物馆」），
     别名对不上（「松山湖景区」≠「松湖烟雨」）如实报未匹配并列景点清单；
  ④ 衔接稳健：buffer 达标率、长段紧衔接、踩闭馆边缘各出检查点；
     空 activities 拒绝评分（EMPTY_ITINERARY 教训：空内容的分数是假分数）。

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
sys.path.insert(0, str(_SKILL_ROOT / 'tools'))

from quality_report import (  # noqa: E402
    dim_cost, dim_experience, dim_pacing, dim_transfer, group_days,
)

QUALITY = _SKILL_ROOT / 'tools' / 'quality_report.py'


def _act(aid, name, typ, start, end, **kw):
    d = {'id': aid, 'name': name, 'type': typ, 'start': start, 'end': end}
    d.update(kw)
    return d


def _itinerary():
    cons = {'max_daily_minutes': 720, 'max_walking_km_per_day': 8,
            'default_transfer_buffer_minutes': 20}
    acts = [
        _act('a1', '甲景点', 'ATTRACTION', '2026-10-30T09:00:00+08:00',
             '2026-10-30T12:00:00+08:00', walking_km=5, estimated_cost=60),
        _act('a2', '乙午餐', 'MEAL', '2026-10-30T12:30:00+08:00',
             '2026-10-30T13:30:00+08:00', walking_km=1, estimated_cost=80),
        _act('a3', '丙景点', 'ATTRACTION', '2026-10-30T14:00:00+08:00',
             '2026-10-30T17:30:00+08:00', walking_km=3, estimated_cost=0,
             closing_time='17:30', last_entry_time='16:30'),
    ]
    segs = [
        {'from_id': 'a1', 'to_id': 'a2', 'duration_minutes': 15, 'buffer_minutes': 25,
         'estimated_cost': 10},
        {'from_id': 'a2', 'to_id': 'a3', 'duration_minutes': 90, 'buffer_minutes': 15,
         'estimated_cost': 30},
    ]
    return {'budget_cny': 500, 'constraints': cons, 'activities': acts,
            'segments': segs, 'timezone': 'Asia/Shanghai'}


def _run(*extra):
    proc = subprocess.run([sys.executable, str(QUALITY)] + list(extra),
                          capture_output=True, timeout=60)
    return proc.returncode, proc.stdout.decode('utf-8')


class TestPacing(unittest.TestCase):
    """① 节奏红线与上限出处。"""

    def test_over_cap_and_walk_limit(self):
        it = _itinerary()
        req = {'mobility': {'level': 'LOW'}, 'travelers': 2}
        # LOW 上限 4：塞到 5 项 → RED；步行 5+1+3+3+5=17 → RED（上限 8）
        it['activities'].append(_act(
            'a4', '丁景点', 'ATTRACTION', '2026-10-30T18:00:00+08:00',
            '2026-10-30T19:00:00+08:00', walking_km=3))
        it['activities'].append(_act(
            'a5', '戊景点', 'ATTRACTION', '2026-10-30T19:10:00+08:00',
            '2026-10-30T20:00:00+08:00', walking_km=5))
        items = dim_pacing(group_days(it['activities']),
                           it['constraints'], req)
        reds = [i for i in items if i['status'] == 'RED']
        self.assertTrue(any('项活动' in i['subject'] for i in reds))
        self.assertTrue(any('步行量' in i['subject'] for i in reds))
        # 上限出处必须可读（可解释性：读者可以不同意上限，但看得见依据）
        cap_item = next(i for i in items if '项活动' in i['subject'])
        self.assertIn('mobility.level=LOW', cap_item['rule'])

    def test_short_dwell_warn(self):
        it = _itinerary()
        it['activities'][0]['end'] = '2026-10-30T09:30:00+08:00'   # 30 分钟
        items = dim_pacing(group_days(it['activities']), it['constraints'], None)
        self.assertTrue(any(i['status'] == 'WARN' and '走马观花' in i['rule']
                            for i in items))


class TestCost(unittest.TestCase):
    """② 成本口径。"""

    def test_over_budget_red_and_scope_mismatch(self):
        it = _itinerary()
        it['budget_cny'] = 100            # 60+80+0 + 10+30 = 180 → 180%
        req = {'budget_cny': 90, 'budget_scope': 'PER_PERSON', 'travelers': 3}
        items = dim_cost(it, req)
        util = next(i for i in items if '利用率' in i['subject'])
        self.assertEqual(util['status'], 'RED')
        mismatch = next(i for i in items if '口径不一致' in i['subject'])
        self.assertIn('¥270', mismatch['detail'])       # 90×3 并列报出
        self.assertIn('¥100', mismatch['detail'])

    def test_zero_cost_meal_warn(self):
        items = dim_cost(_itinerary(), None)
        zero = next(i for i in items if '零成本' in i['subject'])
        self.assertEqual(zero['status'], 'WARN')
        self.assertIn('丙景点', zero['detail'])


class TestExperience(unittest.TestCase):
    """③ must_visit 双向匹配。"""

    def test_must_visit_two_way_and_alias_miss(self):
        it = _itinerary()
        req = {'must_visit': [{'name': '甲'}, {'name': '不存在的别名'}]}
        items = dim_experience(it, req)
        cov = next(i for i in items if 'must_visit' in i['subject'])
        self.assertEqual(cov['status'], 'WARN')         # 1/2
        self.assertIn('字面匹配不认别名', cov['detail'])
        self.assertIn('甲景点', cov['detail'])          # 景点清单供人工核对

    def test_same_type_run_warn(self):
        it = _itinerary()
        # 丙戊己三个 ATTRACTION 连排 → WARN（甲乙打断在前面，run=3）
        it['activities'].append(_act(
            'a5', '戊景点', 'ATTRACTION', '2026-10-30T19:30:00+08:00',
            '2026-10-30T20:30:00+08:00', walking_km=0))
        it['activities'].append(_act(
            'a6', '己景点', 'ATTRACTION', '2026-10-30T20:40:00+08:00',
            '2026-10-30T21:30:00+08:00', walking_km=0))
        items = dim_experience(it, None)
        self.assertTrue(any('同类型连排' in i['subject'] and i['status'] == 'WARN'
                            for i in items))


class TestTransferAndCLI(unittest.TestCase):
    """④ 衔接检查点 + CLI 契约。"""

    def test_transfer_points(self):
        items = dim_transfer(_itinerary(), _itinerary()['constraints'])
        ratio = next(i for i in items if '达标率' in i['subject'])
        self.assertIn('1/2', ratio['detail'])
        tight = next(i for i in items if 'a2 → a3' in i['subject'])
        self.assertEqual(tight['status'], 'WARN')       # 90 分钟段 buffer 15 < 30
        edge = next(i for i in items if '闭馆边缘' in i['rule'])
        self.assertEqual(edge['status'], 'WARN')        # 17:30 结束 = 17:30 闭馆

    def test_empty_itinerary_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'it.json'
            p.write_text(json.dumps({'activities': []}), encoding='utf-8')
            rc, out = _run(str(p))
            self.assertEqual(rc, 1)
            self.assertIn('EMPTY', out) or self.assertIn('没有 activities', out)

    def test_strict_red_exit2(self):
        with tempfile.TemporaryDirectory() as td:
            it = _itinerary()
            it['activities'].append(_act(
                'a4', '丁', 'ATTRACTION', '2026-10-30T21:00:00+08:00',
                '2026-10-30T22:00:00+08:00', walking_km=9))
            p = Path(td) / 'it.json'
            p.write_text(json.dumps(it, ensure_ascii=False), encoding='utf-8')
            rc, _ = _run(str(p), '--strict')
            self.assertEqual(rc, 2)
            rc2, out2 = _run(str(p), '--json')
            self.assertEqual(rc2, 0)                    # 不加 strict 不拦
            rep = json.loads(out2)
            self.assertIn('total', rep)
            self.assertIn('dimensions', rep)


if __name__ == '__main__':
    unittest.main()
