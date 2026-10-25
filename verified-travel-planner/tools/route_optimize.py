#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
route_optimize.py — 动线优化建议：同样的点，换个顺序能少走多少直线公里。

为什么需要它
────────────────────────────────────────────────────────────────────────
`evaluate` **校验**动线（时间窗 / 换乘余量），不**优化**动线——中山 D2 卡到
719 分钟是手工挤出来的。`references/landscape.md` P3 借鉴 travel-itinerary-planner
的地理聚类 + TSP 降折返，但立了本包的硬边界：

· **只出建议，绝不自动改事实源**——优化器动过的事实源，「每个数字有来源」就失效了
  （采纳与否是人的决策，机器只把省多少摆出来）；
· **直线口径，明说**——坐标球面直线（haversine），不是高德实测车程。所有数字
  标【估算·直线】；采纳前用高德路线（amap-snapshot / search-places）复核真实车程。

算法（确定性与可解释优先）
────────────────────────────────────────────────────────────────────────
最近邻贪心起步（起点固定：出发地/酒店不动）+ 2-opt 局部改进（不引入随机重启，
same input → same output）。规模 ≤ 12 点/日：旅行商是 NP-hard，但一日动线这点
规模上 2-opt 的解足够好，且每一步改进都能说清「哪两段边交换了」。

吃哪些文件
────────────────────────────────────────────────────────────────────────
· 动线文件：{"days":[{"date":"…","points":[{"name":"…","location":"lng,lat"}]}]}
· nearby_XX.json：stops[] 的 stop/location 视作一条动线（各停留点串起来的顺序）
· 裸 points[]：{"points":[…]} 直接当一条动线

用法
────────────────────────────────────────────────────────────────────────
    python route_optimize.py 动线_XX.json                  # 逐日给建议
    python route_optimize.py nearby_东莞.json --fix-end    # 末点固定（如必须回酒店）
    python route_optimize.py 动线_XX.json --json

退出码
────────────────────────────────────────────────────────────────────────
0 = 建议生成（**没有「更优」也是 0**——当前顺序已近优同样是有价值的结论）
1 = 文件读不到 / 不是合法 JSON / 找不到可用的坐标点
"""
import argparse
import json
import math
import sys
from pathlib import Path

# ---- 终端符号自适应（与 freshness.py 同款）
_SYM_FALLBACK = str.maketrans({
    '✅': '[OK]', '→': '->', '⚠': '(!)', '｜': '|', '─': '-', '—': '-', '·': '.',
})


def emit(line=''):
    try:
        print(line)
    except UnicodeEncodeError:
        try:
            print(line.translate(_SYM_FALLBACK))
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or 'ascii'
            print(line.encode(enc, 'replace').decode(enc))


# ============================================================
# 输入解析：三类 schema → [{label, points:[(name, lat, lng)]}]
# ============================================================

def _parse_location(v):
    """location 兼容 'lng,lat' 字符串与 {'lng','lat'} 对象 → (lat, lng) 或 None。"""
    if isinstance(v, str):
        parts = [p for p in v.replace(',', ' ').split() if p]
        if len(parts) >= 2:
            try:
                return float(parts[1]), float(parts[0])       # amap 习惯：经度在前
            except ValueError:
                return None
    if isinstance(v, dict):
        try:
            return float(v['lat']), float(v['lng'])
        except (KeyError, ValueError, TypeError):
            return None
    return None


def extract_routes(doc) -> list:
    """返回 [(label, [(name,(lat,lng)), …]), …]。"""
    routes = []

    def pts_from(seq, name_key, loc_key):
        pts = []
        for p in seq if isinstance(seq, list) else []:
            if not isinstance(p, dict):
                continue
            loc = _parse_location(p.get(loc_key) or p.get('location'))
            if loc:
                pts.append((str(p.get(name_key) or p.get('name') or '?'), loc))
        return pts

    if isinstance(doc.get('days'), list):
        for i, d in enumerate(doc['days'], 1):
            pts = pts_from(d.get('points'), 'name', 'location')
            if len(pts) >= 3:
                routes.append(('D%d %s' % (i, d.get('date', '')), pts))
    if isinstance(doc.get('stops'), list):
        pts = pts_from(doc['stops'], 'stop', 'location')
        if len(pts) >= 3:
            routes.append(('stops（%s，%d 个停留点）'
                           % (doc.get('city', '?'), len(pts)), pts))
    if not routes:
        pts = pts_from(doc.get('points'), 'name', 'location')
        if len(pts) >= 3:
            routes.append(('points（%d 个点）' % len(pts), pts))
    return routes


# ============================================================
# 距离与优化
# ============================================================

def haversine_km(a, b) -> float:
    lat1, lng1 = a
    lat2, lng2 = b
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def dist_matrix(pts):
    n = len(pts)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_km(pts[i][1], pts[j][1])
            m[i][j] = m[j][i] = d
    return m


def path_len(order, m) -> float:
    return sum(m[order[i]][order[i + 1]] for i in range(len(order) - 1))


def nearest_neighbor(n, m, start=0):
    order, left = [start], set(range(n)) - {start}
    while left:
        cur = order[-1]
        nxt = min(left, key=lambda j: m[cur][j])
        order.append(nxt)
        left.remove(nxt)
    return order


def two_opt(order, m, fixed_end=None):
    """确定性 2-opt：交换两条边消除交叉；fixed_end 索引钉在末位不参与。"""
    order = list(order)
    if fixed_end is not None:
        order.remove(fixed_end)
        order.append(fixed_end)
    improved = True
    guard = 0
    while improved and guard < 1000:
        improved = False
        guard += 1
        n = len(order)
        end = n - 1 if fixed_end is not None else n
        for i in range(0, end - 2):
            for j in range(i + 2, end if fixed_end is None else end):
                # 交换 (i,i+1) 与 (j,j+1) 两条边
                if j + 1 >= n:
                    continue
                a, b, c, d = order[i], order[i + 1], order[j], order[j + 1]
                if m[a][b] + m[c][d] > m[a][c] + m[b][d] + 1e-12:
                    order[i + 1:j + 1] = reversed(order[i + 1:j + 1])
                    improved = True
    if fixed_end is not None:
        order.remove(fixed_end)
        order.append(fixed_end)
    return order


def optimize(points, fix_end=False):
    """返回 (当前序, 建议序, 距离阵)。当前序 = 输入顺序（0..n-1）。"""
    m = dist_matrix(points)
    cur = list(range(len(points)))
    if fix_end and len(cur) > 2:
        # 起点固定 + 终点固定：对中间点做最近邻（把末点临时剔掉，最后补回）
        mid = list(range(1, len(cur) - 1))
        end_i = len(cur) - 1
        order = [0]
        left = set(mid)
        while left:
            c = order[-1]
            nxt = min(left, key=lambda j: m[c][j])
            order.append(nxt)
            left.remove(nxt)
        order.append(end_i)
        sug = two_opt(order, m, fixed_end=end_i)
    else:
        sug = two_opt(nearest_neighbor(len(cur), m), m)
    return cur, sug, m


# ============================================================
# 渲染
# ============================================================

def render_route(label, pts, cur, sug, m, quiet=False) -> dict:
    names = [p[0] for p in pts]
    cur_km, sug_km = path_len(cur, m), path_len(sug, m)
    save = cur_km - sug_km
    moved = [names[i] for i in sug]
    result = {
        'label': label,
        'current_order': [names[i] for i in cur],
        'suggested_order': moved,
        'current_km': round(cur_km, 2),
        'suggested_km': round(sug_km, 2),
        'saving_km': round(save, 2),
        'legs': [
            {'from': names[sug[i]], 'to': names[sug[i + 1]],
             'straight_km': round(m[sug[i]][sug[i + 1]], 2)}
            for i in range(len(sug) - 1)
        ],
    }
    if quiet:
        result['verdict'] = 'ALREADY_GOOD' if save < 0.05 else 'SUGGEST'
        return result
    emit('')
    emit('· %s（%d 个点）' % (label, len(pts)))
    if save < 0.05:
        emit('  当前顺序已近优（直线 %.1f km【估算】，优化空间 %.2f km）——不动它。'
             % (cur_km, max(save, 0)))
        result['verdict'] = 'ALREADY_GOOD'
        return result
    result['verdict'] = 'SUGGEST'
    emit('  当前：%s' % ' → '.join(names[i] for i in cur))
    emit('        直线合计 %.1f km【估算·直线】' % cur_km)
    emit('  建议：%s' % ' → '.join(moved))
    emit('        直线合计 %.1f km【估算·直线】' % sug_km)
    emit('  预计节省 %.1f km 直线（%.0f%%）——**直线口径，非高德实测车程**；'
         '采纳前用 amap-snapshot 复核真实车程与时刻。' % (save, 100 * save / cur_km if cur_km else 0))
    return result


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='动线优化建议：最近邻 + 2-opt，直线口径【估算】；只出建议，'
                    '绝不写回事实源（采纳是人的决策）')
    ap.add_argument('input', help='动线 JSON（days[].points[] / stops[] / points[]，'
                                  '点带 name + location(经度,纬度)）')
    ap.add_argument('--fix-end', action='store_true',
                    help='末点固定（如必须回到酒店/车站），只优化中间顺序')
    ap.add_argument('--json', action='store_true', help='输出机读 JSON')
    a = ap.parse_args(argv)

    path = Path(a.input)
    if not path.is_file():
        emit('❌ 读不到文件：%s' % path)
        return 1
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as e:
        emit('❌ 不是合法 JSON：%s' % e)
        return 1

    routes = extract_routes(doc)
    if not routes:
        emit('❌ 没找到 ≥3 个带坐标的点——支持 days[].points[] / stops[] / points[]，'
             '每个点要 name + location（经度,纬度）。少于 3 个点没有「顺序」可优化。')
        return 1

    emit('【动线优化建议】%s ｜ 口径：坐标直线（haversine），非实测车程' % path.name
         if not a.json else '')
    results = []
    for label, pts in routes:
        cur, sug, m = optimize(pts, fix_end=a.fix_end)
        results.append(render_route(label, pts, cur, sug, m, quiet=a.json))

    if a.json:
        # --json 只出 JSON（仓里惯例：机读模式不夹人类可读文本）
        print(json.dumps({'input': str(path), 'metric': 'haversine_straight_km',
                          'routes': results}, ensure_ascii=False, indent=2))
        return 0

    emit('')
    emit('─' * 74)
    emit('边界：直线 ≠ 车程（绕行系数常在 1.2-1.4）；时间窗 / 营业时间 / 预约时段'
         '不在此判——那是 evaluate 的职责。本工具不写任何文件：建议是否采纳、'
         '事实源怎么改，由人决定并按铁律带来源重查。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
