#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
revision_audit.py — 改版审计：路书从 v1 改到 v2，哪些声明变了、哪些证据等级被降了。

为什么需要它
────────────────────────────────────────────────────────────────────────
事实源 JSON 是唯一事实源（铁律 3），改版是常态：临行重查把 ¥60 改成 ¥80、
把查不到的门票从 [A] 降成 [D]。但**变化是悄悄发生的**——v1→v2 目前只能人工比对
（中山 v1→v2 就是人工比的）。最危险的正是「降级无声」：[A] 静默变成 [C]，
读者以为的「实测过」其实已经缩水成「单源线索」。

`references/landscape.md` 把它列为 P2（改版审计），《二次开发实施方案》列为 P1
（决策变更审计）。机制天然现成：两份 JSON 的声明级 diff。

它判什么
────────────────────────────────────────────────────────────────────────
把两版事实源的全部字符串切成「声明单元」（含徽章或含数值的句子），用
difflib 序列对齐配对，然后逐对比较：

· **数值变化**：同单元里按单位分桶（¥/元/公里/分钟/小时/时刻/…），旧值集 ≠ 新值集
  即报「¥60 → ¥80」。这是 claim_audit（声明↔留痕）的时间轴版：那边比「路书 vs 采集」，
  这边比「路书 vs 路书」。
· **等级变化**：徽章字母集合按 A>B>C>D 排秩，新秩更低 = **降级**（[A]→[C]、
  [B]→[D]、[A] 消失而 [C] 出现都算），反向是升级。降级必须**显式发生**。
· **增删声明**：只出现在 v1 的（被删）、只出现在 v2 的（新增）。

降级的「显式」怎么落地（--strict 的判据）
────────────────────────────────────────────────────────────────────────
v2 事实源在 `meta.revision_log` 里登记本批改版的决策记录：

    "revision_log": [
      {"about": "虎门海战馆门票", "from": "A", "to": "D",
       "reason": "官方渠道 10-20 停更，降为未核实并给自查出路"}
    ]

`about` 是声明文本的子串。命中的降级视为「已声明」；没命中的就是**悄悄降级**，
`--strict` 下退出码 2。理由要写：三个月后重开这份路书，看的就是这条 why。

边界（别读成保证）
────────────────────────────────────────────────────────────────────────
· 句子被整句改写时配不上主对齐，但**改写不是隐身术**：被删的 [A]/[B] 与新增声明
  做二次相似配对（≥0.3），相似且等级更低 = 疑似改写降级，同样计入降级清单；
  高明改写（相似度 <0.3）会漏报——那只能靠 claim_audit / echo_audit 接力；
· 数值比较认「值+单位」字面，不做米↔公里的单位换算（claim_audit v2 的 C2 才做容差）；
· 只比**声明层**。渲染产物是派生物，改版后照常重渲染 + 跑 ship。

用法
────────────────────────────────────────────────────────────────────────
    python revision_audit.py 路书_XX_v1.json 路书_XX.json             # 报告
    python revision_audit.py v1.json v2.json --strict                 # 悄悄降级 → 退出码 2
    python revision_audit.py v1.json v2.json --json                   # 机读

退出码
────────────────────────────────────────────────────────────────────────
0 = 审计完成（可以有变化项——变化是改版的常态，报告就是用来交代它的）
2 = --strict 且存在**未声明**的降级（已声明的降级不罚：留痕即合规）
1 = 文件读不到 / 不是合法 JSON
"""
import argparse
import difflib
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

# 徽章：等级取方括号内首字母，允许注释后缀（[C·单源]、[D·估算——…]）
_BADGE_RE = re.compile(r'\[([ABCD])[^]\n]{0,60}\]')

# 数值令牌：值 + 单位（口径与 freshness._VALUE_RE 同族，另加「折/%/张/人」）
_VALUE_RES = [
    re.compile(r'([¥￥]\s*\d+(?:\.\d+)?)'),                     # ¥60
    re.compile(r'(\d+(?:\.\d+)?\s*(?:元|公里|分钟|小时|min|km|人次|张|折|%))'),  # 16 分钟
    re.compile(r'(\d{1,2}\s*[:：]\s*\d{2})'),                   # 14:30
    re.compile(r'(\d+(?:\.\d+)?\s*点)'),                        # 8 点
]
_SPLIT_RE = re.compile(r'<br\s*/?>|</p>|</li>|\n|。|；|;')
_TAG_RE = re.compile(r'<[^>]+>')

_RANK = {'A': 0, 'B': 1, 'C': 2, 'D': 3}


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
# 声明提取
# ============================================================

def collect_claims(doc) -> list:
    """把事实源切成 (路径, 句子) 声明单元，只留含徽章或含数值的。

    双档只取 full（口径同 freshness：compact 是同一句的换写法，两档都收 = 一条声明
    报两遍；只写 compact 的 dict 照收，不许漏）。
    """
    out = []

    def walk(node, path):
        if isinstance(node, str):
            plain = _TAG_RE.sub(' ', node)
            for unit in _SPLIT_RE.split(plain):
                unit = unit.strip()
                if len(unit) >= 6 and (_BADGE_RE.search(unit) or any(
                        rx.search(unit) for rx in _VALUE_RES)):
                    out.append((path, unit))
        elif isinstance(node, dict):
            if 'full' in node and 'compact' in node:
                walk(node['full'], path)
            else:
                for k, v in node.items():
                    walk(v, '%s.%s' % (path, k) if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, '%s[%d]' % (path, i))

    walk(doc, '')
    return out


def grades_of(unit: str) -> list:
    """句子里出现的等级字母（按出现序，可能多枚）。"""
    return [m.group(1) for m in _BADGE_RE.finditer(unit)]


def values_of(unit: str) -> dict:
    """按单位分桶的数值集合：{'分钟': ['16', '12'], '¥': ['60']}。"""
    buckets = {}

    def bucket(token: str) -> str:
        t = token.replace('：', ':').replace('￥', '¥')
        if t.startswith('¥'):
            return '¥'
        if re.match(r'^\d{1,2}:\d{2}$', t):
            return '时刻'
        m = re.search(r'(元|公里|分钟|小时|min|km|人次|张|折|%|点)$', t)
        if not m:
            return '个'
        b = m.group(1)
        return {'min': '分钟', 'km': '公里'}.get(b, b)

    for rx in _VALUE_RES:
        for m in rx.finditer(unit):
            tok = re.sub(r'\s+', '', m.group(1))
            buckets.setdefault(bucket(tok), []).append(
                tok.replace('¥', '').replace('￥', ''))
    return buckets


# ============================================================
# 对齐与比较
# ============================================================

def compare_pair(old_unit: str, new_unit: str) -> dict:
    """一对配上的声明：等级变化 + 数值变化。"""
    og, ng = grades_of(old_unit), grades_of(new_unit)
    grade_changes = []
    old_left, new_left = list(og), list(ng)
    # 同字母对消（[A] 还在就不算它变）；剩下的按秩配成 升/降 级对
    for g in set(old_left):
        while g in old_left and g in new_left:
            old_left.remove(g)
            new_left.remove(g)
    for g1 in sorted(old_left, key=lambda x: _RANK[x]):
        if new_left:
            g2 = new_left.pop(0)
            kind = 'DOWNGRADE' if _RANK[g2] > _RANK[g1] else 'UPGRADE'
            grade_changes.append({'from': g1, 'to': g2, 'kind': kind})
        else:
            grade_changes.append({'from': g1, 'to': None, 'kind': 'REMOVED_GRADE'})
    for g2 in new_left:
        grade_changes.append({'from': None, 'to': g2, 'kind': 'ADDED_GRADE'})

    value_changes = []
    ov, nv = values_of(old_unit), values_of(new_unit)
    for unit_key in sorted(set(ov) | set(nv)):
        a, b = sorted(ov.get(unit_key, [])), sorted(nv.get(unit_key, []))
        if a != b:
            value_changes.append({'unit': unit_key, 'old': a, 'new': b})

    return {'grade_changes': grade_changes, 'value_changes': value_changes}


def audit(doc_v1: dict, doc_v2: dict) -> dict:
    claims1 = collect_claims(doc_v1)
    claims2 = collect_claims(doc_v2)
    texts1 = [c[1] for c in claims1]
    texts2 = [c[1] for c in claims2]

    changes, removed, added = [], [], []
    sm = difflib.SequenceMatcher(a=texts1, b=texts2, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if tag == 'delete':
            for k in range(i1, i2):
                removed.append({'path': claims1[k][0], 'text': texts1[k]})
        elif tag == 'insert':
            for k in range(j1, j2):
                added.append({'path': claims2[k][0], 'text': texts2[k]})
        else:  # replace：块内按相似度配对（≥0.5 才算「同一句改了」），
               # 配不上的落回增/删——把无关句子硬配成「变化」比拆开报更误导。
            olds = texts1[i1:i2]
            news = texts2[j1:j2]
            used = set()
            for oi, old_t in enumerate(olds):
                best, bj = 0.0, None
                for jx, new_t in enumerate(news):
                    if jx in used:
                        continue
                    r = difflib.SequenceMatcher(None, old_t, new_t).ratio()
                    if r > best:
                        best, bj = r, jx
                if best >= 0.5 and bj is not None:
                    used.add(bj)
                    cmp_ = compare_pair(old_t, news[bj])
                    if cmp_['grade_changes'] or cmp_['value_changes']:
                        changes.append({
                            'path_old': claims1[i1 + oi][0],
                            'path_new': claims2[j1 + bj][0],
                            'old': old_t, 'new': news[bj],
                            **cmp_,
                        })
                else:
                    removed.append({'path': claims1[i1 + oi][0], 'text': old_t})
            for jx, new_t in enumerate(news):
                if jx not in used:
                    added.append({'path': claims2[j1 + jx][0], 'text': new_t})

    # 降级项 × meta.revision_log 声明对账
    log = (doc_v2.get('meta') or {}).get('revision_log') or []
    downgrades = []
    for ch in changes:
        for gc in ch['grade_changes']:
            if gc['kind'] == 'DOWNGRADE':
                hay = '%s %s' % (ch['old'], ch['new'])
                hit = next((e for e in log
                            if isinstance(e, dict)
                            and str(e.get('about', ''))[:40]
                            and str(e.get('about'))[:40] in hay), None)
                downgrades.append({
                    'from': gc['from'], 'to': gc['to'],
                    'old': ch['old'][:120], 'new': ch['new'][:120],
                    'declared': hit is not None,
                    'reason': (hit or {}).get('reason', ''),
                    'via': 'paired',
                })

    # 改写式降级（启发式）：被删的 [A]/[B] 声明 × 新增声明做二次配对——
    # 相似度 ≥0.3 且等级更低 = 疑似「删一句 [A] 重写成 [C]」。不做这一层，
    # strict 会被整句改写绕过（改写配不上 0.5 的主配对阈值）。
    # 阈值刻意低于主配对：宁可多问一句，不让降级无声。仍是启发式，高明改写可能漏报。
    added_left = list(added)
    for r in list(removed):
        r_grades = grades_of(r['text'])
        if not any(g in 'AB' for g in r_grades):
            continue
        best, ba = 0.0, None
        for cand in added_left:
            ratio = difflib.SequenceMatcher(None, r['text'], cand['text']).ratio()
            if ratio > best:
                best, ba = ratio, cand
        if ba is None or best < 0.3:
            continue
        a_grades = grades_of(ba['text'])
        if not a_grades or min(a_grades, key=lambda x: _RANK[x]) == 'D' or (
                _RANK[min(a_grades, key=lambda x: _RANK[x])]
                > _RANK[min(r_grades, key=lambda x: _RANK[x])]):
            hay = '%s %s' % (r['text'], ba['text'])
            hit = next((e for e in log
                        if isinstance(e, dict)
                        and str(e.get('about', ''))[:40]
                        and str(e.get('about'))[:40] in hay), None)
            downgrades.append({
                'from': min(r_grades, key=lambda x: _RANK[x]),
                'to': min(a_grades, key=lambda x: _RANK[x]) if a_grades else None,
                'old': r['text'][:120], 'new': ba['text'][:120],
                'declared': hit is not None,
                'reason': (hit or {}).get('reason', ''),
                'via': 'rewrite',
            })
            removed.remove(r)
            added_left.remove(ba)
    added[:] = added_left

    return {
        'claims_v1': len(claims1),
        'claims_v2': len(claims2),
        'changes': changes,
        'removed': removed,
        'added': added,
        'downgrades': downgrades,
        'undeclared_downgrades': [d for d in downgrades if not d['declared']],
        'revision_log_entries': len(log),
    }


# ============================================================
# 渲染
# ============================================================

def render_text(p1: Path, p2: Path, rep: dict, strict: bool) -> int:
    emit('【改版审计】%s  →  %s' % (p1.name, p2.name))
    emit('声明单元：v1 %d 个 ｜ v2 %d 个 ｜ 变化 %d ｜ 删除 %d ｜ 新增 %d'
         % (rep['claims_v1'], rep['claims_v2'],
            len(rep['changes']), len(rep['removed']), len(rep['added'])))
    emit('─' * 74)

    for ch in rep['changes']:
        bits = []
        for gc in ch['grade_changes']:
            if gc['kind'] == 'DOWNGRADE':
                bits.append('🔴 [%s]→[%s] 降级' % (gc['from'], gc['to']))
            elif gc['kind'] == 'UPGRADE':
                bits.append('[%s]→[%s] 升级' % (gc['from'], gc['to']))
            elif gc['kind'] == 'REMOVED_GRADE':
                bits.append('🔴 [%s] 标记消失' % gc['from'])
            else:
                bits.append('新增 [%s] 标记' % gc['to'])
        for vc in ch['value_changes']:
            bits.append('%s %s → %s' % (vc['unit'],
                                        '/'.join(vc['old']) or '∅',
                                        '/'.join(vc['new']) or '∅'))
        emit('')
        emit('· %s' % ' ｜ '.join(bits))
        emit('  旧：%s' % ch['old'][:72])
        emit('  新：%s' % ch['new'][:72])

    if rep['removed']:
        emit('')
        emit('被删除的声明 %d 条（[A]/[B] 被删要能说出为什么）：' % len(rep['removed']))
        for r in rep['removed'][:10]:
            g = '/'.join(grades_of(r['text'])) or '无标'
            emit('  · [%s] %s' % (g, r['text'][:64]))
        if len(rep['removed']) > 10:
            emit('  …另有 %d 条（--json 看全量）' % (len(rep['removed']) - 10))
    if rep['added']:
        emit('')
        emit('新增声明 %d 条（新数字要按铁律 1 带来源与时戳）：' % len(rep['added']))
        for a in rep['added'][:10]:
            g = '/'.join(grades_of(a['text'])) or '无标'
            emit('  · [%s] %s' % (g, a['text'][:64]))
        if len(rep['added']) > 10:
            emit('  …另有 %d 条（--json 看全量）' % (len(rep['added']) - 10))

    emit('')
    emit('─' * 74)
    if rep['downgrades']:
        emit('降级 %d 处（已声明 %d ｜ 未声明 %d）'
             % (len(rep['downgrades']),
                len(rep['downgrades']) - len(rep['undeclared_downgrades']),
                len(rep['undeclared_downgrades'])))
        for d in rep['undeclared_downgrades'][:6]:
            emit('  🔴 未声明：[%s]→[%s]%s %s'
                 % (d['from'], d['to'],
                    '（疑似改写降级）' if d.get('via') == 'rewrite' else '',
                    d['new'][:56]))
        if strict and rep['undeclared_downgrades']:
            emit('')
            emit('结论：FAIL —— 悄悄降级 %d 处；在 v2 的 meta.revision_log 登记'
                 ' {about, from, to, reason} 后重跑' % len(rep['undeclared_downgrades']))
            return 2
        emit('说明：降级不禁止，禁止的是无声——revision_log 写清 why 即视为已声明。')
    else:
        emit('降级 0 处。')
    emit('边界：整句改写会落入「增/删」而非「变化」；删除一条 [A] 重写成 [C] 同样会在这份报告现形。')
    return 0


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='改版审计：两版事实源的声明级 diff——数值变化、证据等级升降'
                    '（降级须在 v2 的 meta.revision_log 声明，--strict 时未声明即退出码 2）')
    ap.add_argument('old', help='旧版事实源 JSON（v1）')
    ap.add_argument('new', help='新版事实源 JSON（v2）')
    ap.add_argument('--strict', action='store_true',
                    help='存在未声明的证据等级降级时退出码 2')
    ap.add_argument('--json', action='store_true', help='输出机读 JSON')
    a = ap.parse_args(argv)

    docs = []
    for arg in (a.old, a.new):
        p = Path(arg)
        if not p.is_file():
            emit('❌ 读不到文件：%s' % p)
            return 1
        try:
            docs.append(json.loads(p.read_text(encoding='utf-8')))
        except ValueError as e:
            emit('❌ %s 不是合法 JSON：%s' % (p.name, e))
            return 1

    rep = audit(docs[0], docs[1])

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 2 if (a.strict and rep['undeclared_downgrades']) else 0

    return render_text(Path(a.old), Path(a.new), rep, a.strict)


if __name__ == '__main__':
    sys.exit(main())
