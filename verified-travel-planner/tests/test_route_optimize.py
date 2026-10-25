#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""route_optimize（动线优化建议）的机器断言。

覆盖四类判据：
  ① 折返检测：Z 字动线能被重排出更短顺序（起点固定，节省 >30%）；
  ② 诚实负向：已近优的顺序必须报 ALREADY_GOOD「不动它」，不为了显得有用而瞎建议；
  ③ --fix-end：末点钉死在末位（如必须回酒店），只优化中间顺序；
  ④ schema 兼容与拒绝：nearby 的 stops[] 可解析；<3 个坐标点拒绝（没有顺序可优化）。

以及边界断言：产出文本必须含「非高德实测车程」的直线口径声明——
这是本工具与「每个数字有来源」铁律的相处方式：估算就标估算。

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

from route_optimize import extract_routes, haversine_km, optimize  # noqa: E402

ROUTE_TOOL = _SKILL_ROOT / 'tools' / 'route_optimize.py'

# 南北向四点：A(北) C(南) B(中) D(极北)——Z 字折返
ZIGZAG = {'days': [{'date': '2026-10-30', 'points': [
    {'name': 'A 北', 'location': '113.30,22.90'},
    {'name': 'C 南', 'location': '113.30,22.50'},
    {'name': 'B 中', 'location': '113.30,22.70'},
    {'name': 'D 极北', 'location': '113.30,23.00'},
]}]}

# 顺序即最优的四点（北→南一字排开）
LINEAR = {'days': [{'date': '2026-10-30', 'points': [
    {'name': 'D 极北', 'location': '113.30,23.00'},
    {'name': 'A 北', 'location': '113.30,22.90'},
    {'name': 'B 中', 'location': '113.30,22.70'},
    {'name': 'C 南', 'location': '113.30,22.50'},
]}]}


def _write(tmp: Path, doc, name='动线.json'):
    p = tmp / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return p


def _run(*extra):
    proc = subprocess.run([sys.executable, str(ROUTE_TOOL)] + list(extra),
                          capture_output=True, timeout=60)
    return proc.returncode, proc.stdout.decode('utf-8')


class TestOptimize(unittest.TestCase):
    """①②③ 核心算法行为。"""

    def setUp(self):
        self.zigzag = [(p['name'], tuple(float(x) for x in p['location'].split(',')[::-1]))
                       for p in ZIGZAG['days'][0]['points']]

    def test_zigzag_reordered_with_big_saving(self):
        cur, sug, m = optimize(self.zigzag)
        from route_optimize import path_len
        save = path_len(cur, m) - path_len(sug, m)
        self.assertGreater(save, 30.0)                    # 100.1 → 66.7
        self.assertEqual(sug[0], 0)                       # 起点固定不动
        self.assertNotEqual(cur, sug)

    def test_already_good_not_touched(self):
        pts = [(p['name'], tuple(float(x) for x in p['location'].split(',')[::-1]))
               for p in LINEAR['days'][0]['points']]
        cur, sug, m = optimize(pts)
        from route_optimize import path_len
        save = path_len(cur, m) - path_len(sug, m)
        self.assertLess(save, 0.5)                        # 近优不折腾

    def test_fix_end_keeps_last(self):
        cur, sug, m = optimize(self.zigzag, fix_end=True)
        self.assertEqual(sug[-1], len(self.zigzag) - 1)   # 末点钉死
        self.assertEqual(sug[0], 0)

    def test_haversine_sanity(self):
        # 北京→上海约 1067 km，误差容忍 ±3%（球面直线的常识刻度）
        d = haversine_km((39.9042, 116.4074), (31.2304, 121.4737))
        self.assertAlmostEqual(d, 1067, delta=32)


class TestSchemaAndCLI(unittest.TestCase):
    """④ schema 与 CLI 契约。"""

    def test_nearby_stops_parsed(self):
        doc = {'city': '东莞', 'stops': [
            {'stop': '甲', 'location': '113.66,22.82'},
            {'stop': '乙', 'location': '113.75,22.95'},
            {'stop': '丙', 'location': '113.60,22.70'},
        ]}
        routes = extract_routes(doc)
        self.assertEqual(len(routes), 1)
        self.assertEqual(len(routes[0][1]), 3)
        self.assertEqual(routes[0][1][0][0], '甲')

    def test_too_few_points_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td), {'points': [
                {'name': '甲', 'location': '113.3,22.9'},
                {'name': '乙', 'location': '113.3,22.8'}]})
            rc, out = _run(str(p))
            self.assertEqual(rc, 1)

    def test_cli_reports_straight_line_caveat_and_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td), ZIGZAG)
            rc, out = _run(str(p))
            self.assertEqual(rc, 0)
            self.assertIn('非高德实测车程', out)           # 直线口径声明必须在场
            self.assertIn('节省', out)
            rc2, out2 = _run(str(p), '--json')
            rep = json.loads(out2)
            self.assertEqual(rep['metric'], 'haversine_straight_km')
            self.assertEqual(rep['routes'][0]['verdict'], 'SUGGEST')
            self.assertEqual(rep['routes'][0]['suggested_order'][0], 'A 北')


if __name__ == '__main__':
    unittest.main()
