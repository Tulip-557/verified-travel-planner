#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
quality_report.py — 行程质量评分：从「排得通」到「排得好」，四维可解释。

为什么需要它
────────────────────────────────────────────────────────────────────────
`evaluate` 只判**可行性**（闭馆冲突 / 换乘余量 / 预算闭合），score 100 的意思是
「没违规」，不是「方案优秀」——项目文档反复强调这一点。`references/landscape.md`
把它列为 P2：借鉴 travel-itinerary-planner 的四维评分，但立了两条本包的护栏：

· **分数必须可解释**——每个得分点挂到具体活动/换乘段与规则，不给裸总分
  （「看起来很科学的数字」正是本包反对的东西）；
· **不与 evaluate 重复**——可行性闸门管过的（闭馆/余量/预算闭合）这里不再判，
  这里只说 evaluate 不说的话：节奏赶不赶、成本排没排、体验配不配、衔接稳不稳。

四个维度
────────────────────────────────────────────────────────────────────────
D1 节奏与疲劳：逐日活动数 vs 上限（上限来源：style/mobility，缺省 balanced=6）、
   有效跨度 vs constraints.max_daily_minutes、步行量 vs max_walking_km_per_day、
   单点停留 <40 分钟（走马观花）、用餐间隔 >6 小时。
D2 成本效率：预算利用率（已排入成本 / 预算，按 budget_scope 折算人头）、
   逐日成本分布、**零成本可疑项**（MEAL/ATTRACTION 的 estimated_cost=0 ——
   不是错，是「这笔钱没进账」，提醒别把预算读成全价）。
D3 体验匹配：type 多样性（同类型连排 ≤2）、must_visit 覆盖率（需 --request）、
   特种兵打卡点数显性化（style=fast 时逐日不同景点数）。**没有 trip_request
   时该维只跑不依赖偏好的子项并明说**——机器不猜偏好。
D4 衔接稳健：换乘 buffer ≥ constraints.default_transfer_buffer_minutes 的比例、
   紧衔接（buffer<30 且段时长>60）、踩着闭馆/末入场结束的活动。

评分口径
────────────────────────────────────────────────────────────────────────
每个检查点 status ∈ {OK, WARN, RED}，计分 OK=1 / WARN=0.5 / RED=0；
维度分 = 得分点数/总点数；总分 = 各维度（SKIP 的维度不入分母）平均。
**WARN/RED 不阻断**（--strict 时任一 RED 退出码 2）——它是体检建议，不是闸门：
「WARN 清单」是调整行程的抓手，不是返工令。

用法
────────────────────────────────────────────────────────────────────────
    python quality_report.py itinerary_XX.json
    python quality_report.py itinerary_XX.json --request trip_request.json
    python quality_report.py itinerary_XX.json --strict        # 有 RED → 退出码 2
    python quality_report.py itinerary_XX.json --json

退出码
────────────────────────────────────────────────────────────────────────
0 = 报告完成（可以有 WARN/RED——建议清单就是用来交代它们的）
2 = --strict 且存在 RED（节奏/衔接被压到红线）
1 = 文件读不到 / 不是合法 JSON
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---- 终端符号自适应（与 freshness.py 同款）
_SYM_FALLBACK = str.maketrans({
    '✅': '[OK]', '✓': '[OK]', '√': '[OK]',
    '❌': '[X]', '✗': '[X]', '×': '[X]',
    '→': '->', '■': '[*]', '●': '[*]', '·': '.',
    '⚠': '(!)', '🔴': '(*)', '｜': '|', '─': '-', '—': '-',
    'ℹ': '[i]',
})

# 活动数上限（D1）。来源优先级：trip_request.mobility.level > trip_request.style > 缺省。
# 数值是 SKILL.md 节奏纪律的建议值，不是闸门——每条检查都把「用的哪个上限、
# 从哪来」写进 detail，读者可以不同意上限，但看得见依据。
CAP_ACTIVITY_BY_STYLE = {'fast': 8, 'balanced': 6, 'relaxed': 4}
CAP_ACTIVITY_BY_MOBILITY = {'LOW': 4, 'MODERATE': 6, 'HIGH': 8}
MEAL_GAP_WARN_HOURS = 6
ATTRACTION_MIN_MINUTES = 40


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
# 数据准备
# ============================================================

def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def group_days(activities):
    days = defaultdict(list)
    for a in activities:
        st = _parse_dt(a.get('start'))
        if st:
            days[st.date().isoformat()].append(a)
    for d in days:
        days[d].sort(key=lambda a: str(a.get('start')))
    return dict(sorted(days.items()))


def activity_cap(request):
    """D1 的活动数上限 + 出处。"""
    if request:
        mob = (request.get('mobility') or {}).get('level')
        if mob in CAP_ACTIVITY_BY_MOBILITY:
            return CAP_ACTIVITY_BY_MOBILITY[mob], 'trip_request.mobility.level=%s' % mob
        style = request.get('style')
        if style in CAP_ACTIVITY_BY_STYLE:
            return CAP_ACTIVITY_BY_STYLE[style], 'trip_request.style=%s' % style
    return CAP_ACTIVITY_BY_STYLE['balanced'], '缺省 balanced=6（无 trip_request 或字段缺失）'


# ============================================================
# 四个维度
# ============================================================

def dim_pacing(days, cons, request):
    """D1 节奏与疲劳。"""
    items = []
    cap, cap_src = activity_cap(request)
    max_minutes = cons.get('max_daily_minutes') or 720
    max_walk = ((request or {}).get('mobility') or {}).get('max_walking_km_per_day') \
        or cons.get('max_walking_km_per_day') or 10
    for day, acts in days.items():
        real = [a for a in acts if a.get('type') != 'LODGING']
        items.append({
            'day': day, 'subject': '当日 %d 项活动' % len(real),
            'rule': '活动数 ≤ %d（%s）' % (cap, cap_src),
            'status': 'RED' if len(real) > cap else ('WARN' if len(real) == cap else 'OK'),
            'detail': '%d 项 / 上限 %d' % (len(real), cap),
        })
        starts = [_parse_dt(a['start']) for a in real if a.get('start')]
        ends = [_parse_dt(a['end']) for a in real if a.get('end')]
        span = 0.0
        if starts and ends:
            span = (max(ends) - min(starts)).total_seconds() / 60
            items.append({
                'day': day, 'subject': '有效跨度',
                'rule': '跨度 ≤ %d 分钟（constraints.max_daily_minutes）' % max_minutes,
                'status': 'RED' if span > max_minutes else (
                    'WARN' if span > max_minutes * 0.9 else 'OK'),
                'detail': '%.0f 分钟（%s~%s）' % (
                    span, min(starts).strftime('%H:%M'), max(ends).strftime('%H:%M')),
            })
        walk = sum(float(a.get('walking_km') or 0) for a in real)
        if walk:
            items.append({
                'day': day, 'subject': '步行量',
                'rule': '≤ %s km/日（mobility 或 constraints）' % max_walk,
                'status': 'RED' if walk > max_walk else ('WARN' if walk > max_walk * 0.8 else 'OK'),
                'detail': '%.1f km / 上限 %s km' % (walk, max_walk),
            })
        # 节奏画像：把当日三项占用（活动数/跨度/步行）折成一个可读等级——
        # 不引入新判据，只把上面三条的比值汇总成人话（可游玩性的直观锚点）。
        loads = []
        if len(real):
            loads.append(len(real) / float(cap))
        if starts and ends:
            loads.append(span / float(max_minutes))
        if walk:
            loads.append(walk / float(max_walk))
        if loads:
            top = max(loads)
            grade = ('超载' if top > 1 else '满' if top > 0.85
                     else '适中' if top > 0.6 else '轻松')
            items.append({
                'day': day, 'subject': '节奏画像',
                'rule': '当日三项占用（活动数/跨度/步行）最高比值 → 轻松<60% ≤ 适中 <85% ≤ 满 ≤100% < 超载',
                'status': 'OK',
                'detail': '%s（最高占用 %.0f%%：%d 项 %.0f%% ｜ 跨度 %.0f%% ｜ 步行 %.0f%%）'
                          % (grade, top * 100,
                             len(real), loads[0] * 100,
                             (span / float(max_minutes)) * 100 if starts and ends else 0,
                             (walk / float(max_walk)) * 100 if walk else 0),
            })
        for a in real:
            if a.get('type') != 'ATTRACTION':
                continue
            st, en = _parse_dt(a.get('start')), _parse_dt(a.get('end'))
            if st and en:
                dwell = (en - st).total_seconds() / 60
                if dwell < ATTRACTION_MIN_MINUTES:
                    items.append({
                        'day': day, 'subject': a.get('name', a.get('id')),
                        'rule': '景点停留 ≥ %d 分钟（<40 即走马观花）' % ATTRACTION_MIN_MINUTES,
                        'status': 'WARN', 'detail': '仅停留 %.0f 分钟' % dwell,
                    })
        meals = [a for a in real if a.get('type') == 'MEAL']
        if meals and len(real) >= 3:
            m_starts = [_parse_dt(a['start']) for a in meals if a.get('start')]
            all_starts = sorted(_parse_dt(a['start']) for a in real if a.get('start'))
            if m_starts and all_starts:
                meal_set = set(m_starts)
                gaps, prev = [], all_starts[0]
                for t in all_starts:
                    if t in meal_set:
                        gaps.append(((t - prev).total_seconds() / 3600, prev, t))
                        prev = t
                longest = max((g[0] for g in gaps), default=0)
                if longest > MEAL_GAP_WARN_HOURS:
                    items.append({
                        'day': day, 'subject': '用餐间隔',
                        'rule': '两餐间隔 ≤ %d 小时' % MEAL_GAP_WARN_HOURS,
                        'status': 'WARN',
                        'detail': '最长 %.1f 小时没排正餐（当天 %d 项活动只有 %d 餐）'
                                  % (longest, len(real), len(meals)),
                    })
    return items


def dim_cost(itinerary, request):
    """D2 成本效率。

    预算口径：itinerary.budget_cny 一律视为**总预算**（与 evaluate 的预算闭合同源）。
    trip_request 若自报 PER_PERSON，折算后与总额并列报出、不裁决——两边对不上
    是采集问题，机器只负责让它看得见（铁律「矛盾不裁决」）。
    """
    items = []
    budget = itinerary.get('budget_cny')
    travelers = (request or {}).get('travelers') or 1
    scope = (request or {}).get('budget_scope') or 'TOTAL'
    if not budget:
        return [{'day': '-', 'subject': '预算', 'rule': 'budget_cny 存在才可算利用率',
                 'status': 'WARN', 'detail': 'itinerary 无 budget_cny，跳过利用率；逐日成本照报'}]
    act_cost = sum(int(a.get('estimated_cost') or 0)
                   for a in itinerary.get('activities', []))
    seg_cost = sum(int(s.get('estimated_cost') or 0)
                   for s in itinerary.get('segments', []))
    total_cost = act_cost + seg_cost
    use = total_cost / float(budget) if budget else 0
    items.append({
        'day': '-', 'subject': '预算利用率',
        'rule': '已排入成本 / itinerary.budget_cny（总额口径）',
        'status': 'OK',
        'detail': '已排 ¥%d / 总预算 ¥%d（%.0f%%）——余量 ¥%d；低于 100%% 不代表省钱，'
                  '先看下面有没有「该计入而未计入」' % (total_cost, budget, use * 100,
                                                       max(budget - total_cost, 0)),
    })
    if use > 1.0:
        items[-1]['status'] = 'RED'
        items[-1]['detail'] = ('已排 ¥%d 超预算 ¥%d（%.0f%%）——evaluate 的预算闭合若也红，'
                               '这是同一问题的质量面' % (total_cost, budget, use * 100))
    if request and request.get('budget_cny') and scope == 'PER_PERSON':
        req_total = request['budget_cny'] * travelers
        if abs(req_total - budget) > max(budget, req_total) * 0.05:
            items.append({
                'day': '-', 'subject': '预算口径不一致',
                'rule': 'request 人均×人头 vs itinerary 总额，差 >5% 即并列报出（不裁决）',
                'status': 'WARN',
                'detail': 'request 自报人均 ¥%d × %d 人 = ¥%d；itinerary 总额 ¥%d——'
                          '两边对不上，先对齐口径再读利用率'
                          % (request['budget_cny'], travelers, req_total, budget),
            })
    zero_sus = [a for a in itinerary.get('activities', [])
                if a.get('type') in ('MEAL', 'ATTRACTION')
                and not a.get('estimated_cost')]
    if zero_sus:
        items.append({
            'day': '-', 'subject': '零成本项 %d 个' % len(zero_sus),
            'rule': 'MEAL/ATTRACTION 应有 estimated_cost（0 = 这笔钱没进预算账）',
            'status': 'WARN',
            'detail': '；'.join(a.get('name', a.get('id', '?')) for a in zero_sus[:6])
                      + ('…' if len(zero_sus) > 6 else ''),
        })
    days = group_days(itinerary.get('activities', []))
    per_day = {}
    for day, acts in days.items():
        # 段成本无日期轴，只进总量，不摊到天（怎么摊都是编数）
        per_day[day] = sum(int(a.get('estimated_cost') or 0) for a in acts)
    if per_day:
        detail = ' ｜ '.join('%s ¥%d' % (d[5:], v) for d, v in per_day.items())
        if seg_cost:
            detail += '（不含交通段 ¥%d）' % seg_cost
        items.append({
            'day': '-', 'subject': '逐日活动成本分布',
            'rule': '分布供人工判断（哪天明显偏轻/偏重），不设机器阈值',
            'status': 'OK', 'detail': detail,
        })
    return items


def dim_experience(itinerary, request):
    """D3 体验匹配：类型多样性 + must_visit 覆盖 + 打卡数显性化。"""
    items = []
    days = group_days(itinerary.get('activities', []))
    for day, acts in days.items():
        types = [a.get('type') for a in acts if a.get('type') != 'LODGING']
        run, longest, longest_type = 1, 1, types[0] if types else ''
        for i in range(1, len(types)):
            if types[i] == types[i - 1]:
                run += 1
            else:
                run = 1
            if run > longest:
                longest, longest_type = run, types[i]
        if longest > 2:
            items.append({
                'day': day, 'subject': '同类型连排',
                'rule': '同类活动连排 ≤ 2（连看 3 个同类易疲劳）',
                'status': 'WARN',
                'detail': '最长连排 %d 个「%s」' % (longest, longest_type),
            })
    must = (request or {}).get('must_visit') or []
    if not request:
        items.append({
            'day': '-', 'subject': 'must_visit 覆盖',
            'rule': '需 --request trip_request.json',
            'status': 'WARN',
            'detail': '未提供 trip_request：偏好匹配与 must_visit 覆盖不可判——机器不猜偏好',
        })
    elif must:
        names = [str(a.get('name', '')) for a in itinerary.get('activities', [])]
        joined = ' '.join(names)
        # 双向子串匹配（must「可园」↔ 活动「可园博物馆」；别名如「松山湖景区」↔
        # 「松湖烟雨」字面对不上——那种如实报未匹配，并列景点清单供人工核对）
        hits = [m for m in must
                if str(m.get('name', '')) and (
                    str(m['name']) in joined
                    or any(n and n in str(m['name']) for n in names))]
        miss = [m for m in must if m not in hits]
        if miss:
            spots = '、'.join(n for n in names if n) or '（无）'
            detail = ('未匹配到：%s——**字面匹配不认别名**（如「松山湖景区」≠「松湖烟雨」），'
                      '先人工核对是否已排。当前景点：%s' % (
                          '、'.join(str(m['name']) for m in miss), spots))
        else:
            detail = '已排：%s' % '、'.join(str(m['name']) for m in hits)
        items.append({
            'day': '-', 'subject': 'must_visit 覆盖 %d/%d' % (len(hits), len(must)),
            'rule': '点名要去的点都应排进或显式说明为何没排',
            'status': 'OK' if not miss else 'WARN',
            'detail': detail,
        })
    style = (request or {}).get('style')
    if style == 'fast':
        for day, acts in days.items():
            spots = [a for a in acts if a.get('type') == 'ATTRACTION']
            items.append({
                'day': day, 'subject': '特种兵打卡数',
                'rule': 'style=fast：逐日不同景点数应显性给出（SKILL.md 阶段 4.5④）',
                'status': 'OK', 'detail': '当日 %d 个景点' % len(spots),
            })
    return items


def dim_transfer(itinerary, cons):
    """D4 衔接稳健：buffer 达标率、紧衔接、闭馆边缘。"""
    items = []
    default_buf = cons.get('default_transfer_buffer_minutes') or 20
    acts = {a.get('id'): a for a in itinerary.get('activities', [])}
    segs = itinerary.get('segments', [])
    tight = 0
    for s in segs:
        buf = int(s.get('buffer_minutes') or 0)
        dur = int(s.get('duration_minutes') or 0)
        if buf < default_buf:
            tight += 1
            if buf < 30 and dur > 60:
                items.append({
                    'day': '-', 'subject': '%s → %s' % (s.get('from_id'), s.get('to_id')),
                    'rule': '长段（>60 分钟）换乘 buffer 宜 ≥30 分钟',
                    'status': 'WARN',
                    'detail': '段 %d 分钟，buffer 仅 %d 分钟（缺省 %d）'
                              % (dur, buf, default_buf),
                })
    if segs:
        ok_ratio = 1 - tight / float(len(segs))
        items.insert(0, {
            'day': '-', 'subject': 'buffer 达标率',
            'rule': '段 buffer ≥ constraints.default_transfer_buffer_minutes（%d）' % default_buf,
            'status': 'OK' if ok_ratio >= 0.8 else ('WARN' if ok_ratio >= 0.5 else 'RED'),
            'detail': '%d/%d 段达标（%.0f%%）——低于缺省不一定错（可能有实测依据），'
                      '但要看得到' % (len(segs) - tight, len(segs), ok_ratio * 100),
        })
    for a in itinerary.get('activities', []):
        closing, last_entry = a.get('closing_time'), a.get('last_entry_time')
        en = _parse_dt(a.get('end'))
        if en and closing:
            ch, cm = str(closing).split(':')[:2]
            close_min = int(ch) * 60 + int(cm)
            end_min = en.hour * 60 + en.minute
            if 0 <= close_min - end_min <= 30:
                items.append({
                    'day': en.date().isoformat(), 'subject': a.get('name', a.get('id')),
                    'rule': '结束时刻距闭馆 ≤30 分钟属「踩闭馆边缘」',
                    'status': 'WARN',
                    'detail': '%s 结束，闭馆 %s——留神当日延时/人流（末入场 %s）'
                              % (en.strftime('%H:%M'), closing, last_entry or '未标'),
                })
    return items


# ============================================================
# 评分与渲染
# ============================================================

_STATUS_SCORE = {'OK': 1.0, 'WARN': 0.5, 'RED': 0.0}


def score_dimension(items):
    """SKIP 返回 None。"""
    if not items:
        return None
    return sum(_STATUS_SCORE[i['status']] for i in items) / len(items)


DIM_TITLES = {
    'pacing': 'D1 节奏与疲劳',
    'cost': 'D2 成本效率',
    'experience': 'D3 体验匹配',
    'transfer': 'D4 衔接稳健',
}


def render_text(path: Path, dims, request_path) -> int:
    emit('【行程质量评分】%s%s' % (
        path.name, '（含 trip_request 偏好）' if request_path else '（无 trip_request——偏好维降级）'))
    emit('─' * 74)
    emit('口径：OK=1 / WARN=0.5 / RED=0，维度分=得分点/总点数；SKIP 维不入分母。')
    emit('它判「排得好不好」，不判「排不排得通」——那是 evaluate 的职责，两边不重复。')
    emit('─' * 74)
    total, n = 0.0, 0
    for key in ('pacing', 'cost', 'experience', 'transfer'):
        items = dims[key]
        sc = score_dimension(items)
        if sc is None:
            emit('')
            emit('%s：SKIP（无数据）' % DIM_TITLES[key])
            continue
        total += sc
        n += 1
        reds = [i for i in items if i['status'] == 'RED']
        warns = [i for i in items if i['status'] == 'WARN']
        emit('')
        emit('%s：%.0f 分（检查点 %d ｜ WARN %d ｜ RED %d）'
             % (DIM_TITLES[key], sc * 100, len(items), len(warns), len(reds)))
        for i in items:
            mark = {'OK': '·', 'WARN': '!', 'RED': 'X'}[i['status']]
            emit('  [%s] %s ｜ %s' % (mark, i['subject'], i['detail'][:64]))
            emit('        规则：%s' % i['rule'][:66])
    if n:
        emit('')
        emit('─' * 74)
        emit('总分 %.0f / 100（%d 个维度平均）——读分数必须连同上面每条检查，'
             '裸总分没有意义。' % (100 * total / n, n))
        emit('WARN 是调整抓手不是返工令；RED 在 --strict 下才会拦（退出码 2）。')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='行程质量评分：节奏疲劳/成本效率/体验匹配/衔接稳健——四维、逐点可解释，'
                    '不与 evaluate 的可行性判定重复')
    ap.add_argument('itinerary', help='行程层 itinerary_XX.json（带 activities）')
    ap.add_argument('--request', help='trip_request.json（有则启用偏好匹配与 must_visit 覆盖）')
    ap.add_argument('--strict', action='store_true', help='存在 RED 检查点时退出码 2')
    ap.add_argument('--json', action='store_true', help='输出机读 JSON')
    a = ap.parse_args(argv)

    path = Path(a.itinerary)
    if not path.is_file():
        emit('❌ 读不到行程文件：%s' % path)
        return 1
    try:
        itinerary = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as e:
        emit('❌ 不是合法 JSON：%s' % e)
        return 1
    if not itinerary.get('activities'):
        emit('❌ 没有 activities——要喂行程层 itinerary_XX.json，不是方案层 final_plan。'
             '（这是 evaluate 的 EMPTY_ITINERARY 教训同款：空内容的分数是假分数）')
        return 1

    request = None
    request_path = None
    if a.request:
        rp = Path(a.request)
        if rp.is_file():
            try:
                request = json.loads(rp.read_text(encoding='utf-8'))
                request_path = rp.name
            except ValueError:
                emit('⚠ trip_request 不是合法 JSON，按无偏好处理')
        else:
            emit('⚠ 读不到 trip_request：%s，按无偏好处理' % rp)

    cons = itinerary.get('constraints') or {}
    days = group_days(itinerary.get('activities', []))
    dims = {
        'pacing': dim_pacing(days, cons, request),
        'cost': dim_cost(itinerary, request),
        'experience': dim_experience(itinerary, request),
        'transfer': dim_transfer(itinerary, cons),
    }
    any_red = any(i['status'] == 'RED' for items in dims.values() for i in items)

    if a.json:
        print(json.dumps({
            'itinerary': str(path),
            'request': str(request_path) if request_path else None,
            'dimensions': {
                k: {'score': score_dimension(v), 'items': v}
                for k, v in dims.items()},
            'total': (sum(score_dimension(v) for v in dims.values()
                          if score_dimension(v) is not None)
                      / max(sum(1 for v in dims.values()
                                if score_dimension(v) is not None), 1)),
        }, ensure_ascii=False, indent=2))
        return 2 if (a.strict and any_red) else 0

    render_text(path, dims, request_path)
    return 2 if (a.strict and any_red) else 0


if __name__ == '__main__':
    sys.exit(main())
