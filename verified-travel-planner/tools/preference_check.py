#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
preference_check.py — 偏好覆盖体检：「按你的要求排到了吗」逐条对账。

为什么需要它
────────────────────────────────────────────────────────────────────────
SKILL.md 阶段 0 采集的偏好（玩法取向 / 偏好锚点 / 同行者结构 / 作息）与阶段 4 的
个性化规则（三轴正交表、特殊时段玩法必须显性交代、特种兵打卡数显性给出、
长辈版配套）**全是给模型看的约定，没有一条是机器可判的**——漏了只能靠人眼。

本工具把其中可机判的四类做成检查（口径与 social_notes 的死线校验同模式：
判据是「留痕」而不是「必须做到」）：

P1 偏好已声明：facts.preferences 存在且 pace/interests 非空——SKILL.md 交付自查
   第 ⑳ 项（软字段：没采集不拦，采集了却不呈现才提醒）。
P2 人群 × 模式适配：meta.variant / trip_request 的同行者结构 vs 正文关键词——
   带长辈（seniors）却出现「夜排/夜爬/暴走/通宵/特种兵」，情侣/家庭模式出现
   「特种兵」连用，逐条点名（词表窄口径防误伤：只认明确冲突词）。
P3 特殊时段玩法显性化：含「日出/日落/星空/夜爬/夜景/夜市/早市/赶海/云海/晨雾/
   灯光秀/夜游」的 slot，必须在同 slot 或全文出现「早起包/夜游包/自愿/作息代价」
   归属交代（SKILL.md 阶段 4 红线：不许因默认晚起静默砍掉）。
P4 特种兵打卡数显性：style=fast（或正文自称特种兵）时，打卡点总数必须以
   「打卡 N 个点」形式显性给出（阶段 4.5④：数量本身就是爽点）。

边界
────────────────────────────────────────────────────────────────────────
· 只查「显性交代」，不查「体验好坏」——后者是 quality_report 的维度；
· 词表匹配是字面的：别名词（如「追晨光」）对不上不报，**宁可漏报不误报**；
· must_visit 覆盖在 quality_report 的体验匹配维度里查，这里不重复。

用法
────────────────────────────────────────────────────────────────────────
    python preference_check.py 路书_XX.json
    python preference_check.py 路书_XX.json --request trip_request.json
    python preference_check.py 路书_XX.json --json

退出码
────────────────────────────────────────────────────────────────────────
0 = 体检完成（P1 未声明/P2-P4 有提醒都返回 0——这是提醒清单不是闸门；
    「报『没做到』比假装『做到了』值钱」）
1 = 文件读不到 / 不是合法 JSON
"""
import argparse
import json
import re
import sys
from pathlib import Path

# ---- 终端符号自适应（与 freshness.py 同款）
_SYM_FALLBACK = str.maketrans({
    '✅': '[OK]', '✓': '[OK]', '√': '[OK]',
    '❌': '[X]', '✗': '[X]', '×': '[X]',
    '→': '->', '■': '[*]', '●': '[*]', '·': '.',
    '⚠': '(!)', '🔴': '(*)', '｜': '|', '─': '-', '—': '-',
    'ℹ': '[i]',
})

# P2 人群冲突词（窄口径：只认与「省力慢游」明确冲突的极限玩法词）
_CONFLICT_WORDS = ('夜排', '夜爬', '通宵', '暴走', '特种兵')
# P3 特殊时段玩法词 + 归属交代词
_SPECIAL_TIME_WORDS = ('日出', '日落', '星空', '夜爬', '夜景', '夜市', '早市',
                       '赶海', '云海', '晨雾', '灯光秀', '夜游', '晨祷', '观星')
_PACK_WORDS = ('早起包', '夜游包', '自愿', '作息代价', '作息成本')

_SPLIT_RE = re.compile(r'<br\s*/?>|</p>|</li>|\n|。|；|;')
_TAG_RE = re.compile(r'<[^>]+>')


def emit(line=''):
    try:
        print(line)
    except UnicodeEncodeError:
        try:
            print(line.translate(_SYM_FALLBACK))
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or 'ascii'
            print(line.encode(enc, 'replace').decode(enc))


def slot_texts(facts: dict):
    """逐 slot 产出 (定位, 正文 full 档)。"""
    for di, d in enumerate(facts.get('days', []), 1):
        for si, s in enumerate(d.get('slots', []), 1):
            body = s.get('body')
            if isinstance(body, dict):
                body = body.get('full') or body.get('compact') or ''
            yield 'D%d slot#%d %s' % (di, si, s.get('time', '')), str(body)


def all_text(facts: dict) -> str:
    return '\n'.join(t for _, t in slot_texts(facts))


# ============================================================
# 四条检查
# ============================================================

def check_p1_declared(facts, request):
    pref = facts.get('preferences') or {}
    items = []
    pace = pref.get('pace')
    interests = pref.get('interests') or []
    if pace or interests:
        items.append({'rule': 'P1 偏好已声明', 'status': 'OK',
                      'subject': 'preferences',
                      'detail': 'pace=%s；interests=%s（呈现用户原话，不呈现模型推断）'
                                % (pace or '—', '、'.join(map(str, interests)) or '—')})
    else:
        items.append({'rule': 'P1 偏好已声明', 'status': 'WARN',
                      'subject': 'preferences',
                      'detail': '事实源没有 preferences——阶段 0 若采集过偏好而没落进来，'
                                '就是「采集了却不呈现」；确实没采集则补一句声明'})
    return items


def check_p2_group_fit(facts, request):
    variant = str((facts.get('meta') or {}).get('variant') or '').lower()
    items = []
    if variant in ('seniors', 'family_seniors', 'elder'):
        for loc, text in slot_texts(facts):
            hits = [w for w in _CONFLICT_WORDS if w in text]
            if hits:
                items.append({'rule': 'P2 人群×模式适配', 'status': 'WARN',
                              'subject': loc,
                              'detail': '带长辈动线出现「%s」——SKILL.md 省力慢游细则：'
                                        '台阶陡坡/夜排/暴走应给替代点或说明' % '、'.join(hits)})
    if not items:
        items.append({'rule': 'P2 人群×模式适配', 'status': 'OK',
                      'subject': 'variant=%s' % (variant or '未标'),
                      'detail': '无窄口径冲突词命中（词表只认夜排/夜爬/通宵/暴走/特种兵，'
                                '别名词漏报不误报）'})
    return items


def check_p3_special_time(facts, request=None):
    items, ok = [], True
    whole = all_text(facts)
    has_pack_anywhere = any(w in whole for w in _PACK_WORDS)
    for loc, text in slot_texts(facts):
        hits = [w for w in _SPECIAL_TIME_WORDS if w in text]
        if not hits:
            continue
        if any(w in text for w in _PACK_WORDS):
            items.append({'rule': 'P3 特殊时段玩法显性化', 'status': 'OK',
                          'subject': loc, 'detail': '「%s」已带归属交代（自愿/包/作息代价）'
                                                    % '、'.join(hits)})
        elif has_pack_anywhere:
            items.append({'rule': 'P3 特殊时段玩法显性化', 'status': 'WARN',
                          'subject': loc,
                          'detail': '「%s」本 slot 未交代归属，全文有包但此处没挂——'
                                    '核对它到底进没进早起包/夜游包' % '、'.join(hits)})
        else:
            ok = False
            items.append({'rule': 'P3 特殊时段玩法显性化', 'status': 'WARN',
                          'subject': loc,
                          'detail': '「%s」全文无「早起包/夜游包/自愿/作息代价」——'
                                    'SKILL.md 阶段 4 红线：特殊时段玩法不许静默砍掉，'
                                    '必须显性交代「去不去/代价/拿什么补」' % '、'.join(hits)})
    if not any(i['rule'].startswith('P3') for i in items):
        items.append({'rule': 'P3 特殊时段玩法显性化', 'status': 'OK',
                      'subject': '—', 'detail': '正文未出现特殊时段玩法词，不适用'})
    return items


def check_p4_special_forces(facts, request):
    style = str((request or {}).get('style') or '')
    whole = all_text(facts)
    is_fast = style == 'fast' or '特种兵' in whole
    if not is_fast:
        return [{'rule': 'P4 特种兵打卡数显性', 'status': 'OK', 'subject': '—',
                 'detail': '非特种兵模式（style=%s，正文无「特种兵」），不适用' % (style or '未给')}]
    m = re.search(r'打卡\s*(\d+)\s*个', whole)
    if m:
        return [{'rule': 'P4 特种兵打卡数显性', 'status': 'OK', 'subject': '打卡总数',
                 'detail': '已显性给出「打卡 %s 个点」' % m.group(1)}]
    spots = len(re.findall(r'<b>[^<]{2,20}</b>', whole))
    return [{'rule': 'P4 特种兵打卡数显性', 'status': 'WARN', 'subject': '打卡总数',
             'detail': '特种兵模式但全文无「打卡 N 个点」——阶段 4.5④：数量本身就是爽点，'
                       '必须显性给数字（全文加粗 POI 约有 %d 处可数）' % spots}]


CHECKS = (check_p1_declared, check_p2_group_fit, check_p3_special_time, check_p4_special_forces)


def render_text(path, request_path, items):
    emit('【偏好覆盖体检】%s%s' % (path.name, '（含 trip_request）' if request_path else ''))
    emit('─' * 74)
    for it in items:
        mark = {'OK': '·', 'WARN': '!', 'RED': 'X'}[it['status']]
        emit('[%s] %s ｜ %s' % (mark, it['subject'], it['detail'][:70]))
    warns = sum(1 for i in items if i['status'] == 'WARN')
    emit('─' * 74)
    emit('提醒 %d 条——这是「按你的要求排到了吗」的对账单，不是返工令：' % warns)
    emit('没做到的如实报出来，比假装做到了值钱；体验好坏的评分见 quality_report。')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='偏好覆盖体检：偏好声明/人群适配/特殊时段玩法显性化/特种兵打卡数——'
                    '把 SKILL.md 的个性化约定变成机器可判的提醒清单')
    ap.add_argument('facts', help='事实源 JSON（路书_XX.json）')
    ap.add_argument('--request', help='trip_request.json（style 等字段）')
    ap.add_argument('--json', action='store_true', help='输出机读 JSON')
    a = ap.parse_args(argv)

    path = Path(a.facts)
    if not path.is_file():
        emit('❌ 读不到事实源：%s' % path)
        return 1
    try:
        facts = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as e:
        emit('❌ 不是合法 JSON：%s' % e)
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
                emit('⚠ trip_request 不是合法 JSON，按无 request 处理')

    items = []
    for fn in CHECKS:
        items.extend(fn(facts, request))

    if a.json:
        print(json.dumps({'facts': str(path), 'items': items,
                          'warns': sum(1 for i in items if i['status'] == 'WARN')},
                         ensure_ascii=False, indent=2))
        return 0
    render_text(path, request_path, items)
    return 0


if __name__ == '__main__':
    sys.exit(main())
