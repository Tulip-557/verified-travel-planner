#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
evidence_report.py — 证据健康度一页纸：这份路书哪里最硬、哪里最虚，一眼看到。

为什么需要它
────────────────────────────────────────────────────────────────────────
可信度数据**有了但散着**：A/B/C/D 徽章分布在事实源里、死线违规在 source_audit、
须重查项在 freshness、UNVERIFIED 在 claim_audit、转引风险在 cross_check——
五个工具各自打印一份，用户要自己拼出「这份路书整体可信吗」。

`references/landscape.md` 的 P2 候选把它定名为「证据健康度一页纸」，并立了两条
护栏：**别做成分数**（分数会诱发刷分，清单不会）；**别做成第七道闸门**
（闸门管「能不能交付」，一页纸管「交付时交代清楚」——本工具纯汇总，不阻断）。

它做什么
────────────────────────────────────────────────────────────────────────
对一份事实源（自动发现同目录的 plan / itinerary / nearby / 线索卡 / 双版本 HTML）：

1. 子进程实跑四个**本地**闸门并吃它们的 --json 结构化输出：
   source_audit（标注纪律）/ compact_check（双版本）/ claim_audit（声明↔留痕）/
   freshness（时效）——跑的过程即校验的过程，不缓存旧结论；
2. 自算徽章分布（[A]/[B]/[C·单源]/[D]）与 [D] 未核实清单；
3. echo_audit / cross_check 需要**联网抓引用页**，默认跳过并明说（不静默）；
   `--live` 才实跑（与 ship.py 同款共享缓存：echo 抓一次，cross 复用）；
4. 汇成终端摘要 + 单文件 HTML《可信度概览》（零外部引用、断网可开、可打印）。

口径与边界（照抄各闸门的既有边界，一页纸不新增判据）
────────────────────────────────────────────────────────────────────────
· 各闸门状态只如实转述（PASS/WARN/FAIL/SKIP），本工具不改判、不折算成分数；
· 「全部 PASS」≠「数字一定对」——三层内容真实链的边界（页面改版 / JS 渲染页 /
  高明洗稿）在一页纸页脚原样保留，见 references/echo-cross.md；
· 自述 render_fixture 的事实源（如成都渲染基线桩件）跳过内容闸门并打印声明，
  判据与 ship.py 一致（读 meta 自述，不靠推断）。

用法
────────────────────────────────────────────────────────────────────────
    python evidence_report.py 路书_东莞.json                 # 终端摘要 + 同目录 HTML
    python evidence_report.py 路书_东莞.json -o 概览.html     # 指定 HTML 路径
    python evidence_report.py 路书_东莞.json --live           # 连 echo/cross 一起跑（需联网）
    python evidence_report.py 路书_东莞.json --json           # 只出机读 JSON

退出码
────────────────────────────────────────────────────────────────────────
0 = 报告生成（**存在 FAIL 也返回 0**——它是体检报告不是闸门，FAIL 该拦的
    在 ship.py 里已经拦了；这里只负责让用户看清）
1 = 事实源读不到 / 不是合法 JSON（没有可报告的对象）
"""
import argparse
import html as _html
import json
import re
import subprocess
import sys
import tempfile
import shutil
import atexit
from datetime import datetime
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]

# 与 ship.py 同款的事实源命名拆解：路书_东莞.json / 路书_成都_事实源.json
_FACTS_RE = re.compile(r'^路书_(?P<city>.+?)(?:_事实源)?\.json$')

# 徽章：[A] [B] [C] [C·单源] [D]——比 ship.py 的 \[[ABCD]\] 宽一档，
# 因为正文里 C 档常写全称 [C·单源]（E1 徽章在册认两种写法，统计口径跟随）。
_BADGE_RES = {
    'A': re.compile(r'\[A\]'),
    'B': re.compile(r'\[B\]'),
    'C': re.compile(r'\[C(?:·单源)?\]'),
    'D': re.compile(r'\[D\]'),
}

_SPLIT_RE = re.compile(r'<br\s*/?>|</p>|</li>|\n|。|；|;')
_TAG_RE = re.compile(r'<[^>]+>')

# ---- 终端符号自适应（中文 Windows 黑框认不出 ✅ 时退 ASCII），与 freshness 同款
_SYM_FALLBACK = str.maketrans({
    '✅': '[OK]', '✓': '[OK]', '√': '[OK]',
    '❌': '[X]', '✗': '[X]', '×': '[X]',
    '→': '->', '■': '[*]', '●': '[*]', '·': '.',
    '⚠': '(!)', '🔴': '(*)', '｜': '|', '─': '-', '—': '-',
    'ℹ': '[i]',
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
# 示例发现（单个事实源 → 全套配套产物；口径与 ship.py discover_examples 一致）
# ============================================================

def discover(facts_path: Path) -> dict:
    m = _FACTS_RE.match(facts_path.name)
    city = m.group('city') if m else facts_path.stem.replace('路书_', '')
    d = facts_path.parent
    htmls = sorted(d.glob('路书_%s*.html' % city))
    clues = sorted(d.glob('社媒线索卡_%s*.json' % city))
    return {
        'city': city,
        'facts': facts_path,
        'plan': d / ('final_plan_%s.json' % city),
        'itinerary': d / ('itinerary_%s.json' % city),
        'nearby': sorted(d.glob('nearby_%s*.json' % city)),
        'clues': clues[0] if clues else None,
        'full_html': next((h for h in htmls if '精简版' not in h.name), None),
        'compact_html': next((h for h in htmls if '精简版' in h.name), None),
    }


def is_render_fixture(facts_path: Path) -> bool:
    """桩件判据照抄 ship.py：读 meta.render_fixture 自述，不推断。"""
    try:
        meta = (json.loads(facts_path.read_text(encoding='utf-8')).get('meta') or {})
    except ValueError:
        return False
    return meta.get('render_fixture') is True


# ============================================================
# 子进程实跑闸门
# ============================================================

def _parse_json_stdout(text: str):
    """闸门 --json 模式 stdout 只应有一份 JSON；宽容起见截取首尾大括号。"""
    text = text.strip()
    try:
        return json.loads(text), None
    except ValueError:
        pass
    i, j = text.find('{'), text.rfind('}')
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1]), None
        except ValueError as e:
            return None, 'JSON 解析失败：%s' % e
    return None, 'stdout 里没有 JSON'


def run_gate(args, timeout=180) -> dict:
    """跑一个闸门，返回 {status: OK/FAIL/ERROR/SKIP, code, data, note}。

    退出码语义沿用各闸门：0 过 / 2 校验未过（--json 时 JSON 已在 stdout）/
    1 执行出错。FAIL 时 data 仍取得到——报告要展示「败在哪」，不能只见红不知因。
    """
    try:
        proc = subprocess.run([sys.executable] + args,
                              capture_output=True, cwd=str(_SKILL_ROOT),
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'status': 'ERROR', 'code': None, 'data': None,
                'note': '超时（%ds）' % timeout}
    out = proc.stdout.decode('utf-8', 'replace')
    err = proc.stderr.decode('utf-8', 'replace')
    if proc.returncode == 1:
        return {'status': 'ERROR', 'code': 1, 'data': None,
                'note': (out + err).strip().splitlines()[:1][-1] if (out + err).strip() else 'exit=1'}
    data, perr = _parse_json_stdout(out)
    if data is None:
        return {'status': 'ERROR', 'code': proc.returncode, 'data': None, 'note': perr}
    return {'status': 'OK' if proc.returncode == 0 else 'FAIL',
            'code': proc.returncode, 'data': data, 'note': ''}


def gate_verdict(gate: dict) -> str:
    """把 run_gate 的壳状态翻译成报告格：OK/FAIL/ERROR/SKIP。"""
    return gate['status']


# ============================================================
# 自算：徽章分布 + [D] 未核实清单
# ============================================================

def badge_stats(facts_path: Path) -> dict:
    text = facts_path.read_text(encoding='utf-8')
    counts = {k: len(rx.findall(text)) for k, rx in _BADGE_RES.items()}
    counts['total'] = sum(counts.values())
    return counts


def d_items(facts_path: Path, cap=60) -> list:
    """[D] 未核实清单：正文里标了 [D] 的句子（剥标签、切句、去重）。

    这是交付说明里「未核实项清单」的数据面——SKILL.md 阶段 5.5 要求交付时逐条
    列出并带用户自查出路，一页纸把它从 HTML 里捞出来直接成表。
    """
    try:
        data = json.loads(facts_path.read_text(encoding='utf-8'))
    except ValueError:
        return []
    frags = []

    def walk(node):
        # 双档只取 full（口径同 freshness：compact 是同一句的换写法，扫两遍=重复）
        if isinstance(node, str):
            frags.append(node)
        elif isinstance(node, dict):
            if 'full' in node and 'compact' in node:
                walk(node['full'])
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    seen, items = set(), []
    for frag in frags:
        plain = _TAG_RE.sub(' ', frag)
        for unit in _SPLIT_RE.split(plain):
            unit = unit.strip()
            if '[D]' not in unit or len(unit) < 6:
                continue
            # 图例句（同时出现多档徽章、或自称「证据等级标」）不是未核实项——
            # 东莞实测首条 [D] 就是徽章说明文字，混进清单会稀释可信度。
            if ('[A]' in unit and '[B]' in unit) or '证据等级' in unit or '徽章' in unit:
                continue
            key = unit[:60]
            if key not in seen:
                seen.add(key)
                items.append(unit[:160])
            if len(items) >= cap:
                return items
    return items


# ============================================================
# 聚合
# ============================================================

def aggregate(ex: dict, live: bool) -> dict:
    facts = ex['facts']
    fixture = is_render_fixture(facts)
    gates = {}

    if fixture:
        gates['source_audit'] = {'status': 'SKIP',
                                 'note': '自述为渲染基线桩件，内容闸门不适用（判据同 ship.py）'}
        gates['compact_check'] = {'status': 'SKIP', 'note': '同上'}
    else:
        sa = ['tools/source_audit.py', '--facts', str(facts), '--json']
        if ex['plan'].exists():
            sa += ['--plan', str(ex['plan'])]
        gates['source_audit'] = run_gate(sa)

        cc = ['tools/compact_check.py', '--facts', str(facts), '--json']
        if ex['full_html']:
            cc += ['--full-html', str(ex['full_html'])]
        if ex['compact_html']:
            cc += ['--compact-html', str(ex['compact_html'])]
        gates['compact_check'] = run_gate(cc)

    # claim_audit 至少要一份留痕；没有就明说，不静默（口径同 ship.py）
    ca_args = ['tools/claim_audit.py', '--facts', str(facts), '--json']
    has_ev = False
    if ex['itinerary'].exists():
        ca_args += ['--itinerary', str(ex['itinerary'])]
        has_ev = True
    for n in ex['nearby']:
        if n.exists():
            ca_args += ['--nearby', str(n)]
            has_ev = True
    if has_ev:
        gates['claim_audit'] = run_gate(ca_args)
    else:
        gates['claim_audit'] = {'status': 'SKIP', 'note': '无留痕文件（itinerary/nearby 均缺）'}

    gates['freshness'] = run_gate(['tools/freshness.py', str(facts), '--json'])

    # 第 2/3 层要联网抓引用页：默认跳过并明说。--live 时与 ship.py 共享一份
    # 抓取缓存（echo 抓一次、cross 复用），既省时又不重复打对方站点。
    if live and ex['clues']:
        cache = Path(tempfile.mkdtemp(prefix='evreport-echo-'))
        atexit.register(shutil.rmtree, cache, True)
        gates['echo_audit'] = run_gate(
            ['tools/echo_audit.py', '--clues', str(ex['clues']),
             '--facts', str(facts), '--cache', str(cache), '--json'], timeout=600)
        gates['cross_check'] = run_gate(
            ['tools/cross_check.py', '--clues', str(ex['clues']),
             '--facts', str(facts), '--cache', str(cache), '--json'], timeout=300)
    elif ex['clues'] is None:
        gates['echo_audit'] = {'status': 'SKIP', 'note': '无线索卡（社媒线索卡_<城市>.json 缺）'}
        gates['cross_check'] = {'status': 'SKIP', 'note': '同上'}
    else:
        gates['echo_audit'] = {'status': 'SKIP', 'note': '默认不联网抓页；加 --live 实跑'}
        gates['cross_check'] = {'status': 'SKIP', 'note': '同上'}

    return {
        'city': ex['city'],
        'facts': str(facts),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'gates': gates,
        'badges': badge_stats(facts),
        'd_items': d_items(facts),
    }


# ============================================================
# 终端渲染
# ============================================================

def _freshness_summary(rep: dict) -> dict:
    g = rep['gates']['freshness']
    if g['status'] in ('OK', 'FAIL') and isinstance(g.get('data'), dict):
        return g['data'].get('summary') or {}
    return {}


def _claim_summary(rep: dict) -> dict:
    g = rep['gates']['claim_audit']
    if g['status'] in ('OK', 'FAIL') and isinstance(g.get('data'), dict):
        d = g['data']
        return {'pass': len(d.get('c1_coverage', {}).get('pass', [])),
                'fail': len(d.get('c1_coverage', {}).get('fail', [])),
                'unverified': len(d.get('c2_unverified', []))}
    return {}


def render_terminal(rep: dict) -> None:
    b = rep['badges']
    emit('【证据健康度一页纸】%s ｜ 生成于 %s' % (rep['city'], rep['generated_at']))
    emit('─' * 74)
    emit('徽章分布：[A] %d ｜ [B] %d ｜ [C] %d ｜ [D] %d ｜ 合计 %d'
         % (b['A'], b['B'], b['C'], b['D'], b['total']))
    if b['total'] == 0:
        emit('  ⚠ 全文 0 枚徽章——要么是桩件，要么整篇未分级（后者违反交付自查第 ② 项）')
    emit('')
    emit('闸门状态（如实转述各闸门结论，本工具不改判）：')
    order = ['source_audit', 'compact_check', 'claim_audit', 'freshness',
             'echo_audit', 'cross_check']
    names = {'source_audit': '标注纪律', 'compact_check': '双版本', 'claim_audit': '声明↔留痕',
             'freshness': '时效', 'echo_audit': '来源回声', 'cross_check': '独立源'}
    for key in order:
        g = rep['gates'][key]
        mark = {'OK': '✅', 'FAIL': '❌', 'SKIP': '--', 'ERROR': '⚠'}[g['status']]
        note = g.get('note') or ''
        extra = ''
        if key == 'freshness':
            s = _freshness_summary(rep)
            if s:
                extra = '须重查 %s 条 ｜ 待重算 %s 条 ｜ 无时戳 %s 条' % (
                    s.get('external_must_recheck', '?'),
                    s.get('route_recheck_on_departure', '?'),
                    s.get('undated', '?'))
        if key == 'claim_audit':
            s = _claim_summary(rep)
            if s:
                extra = '实采可寻 %d/%d ｜ UNVERIFIED %d' % (
                    s['pass'], s['pass'] + s['fail'], s['unverified'])
        emit('  %s %-12s %-8s %s %s' % (mark, key, g['status'], extra, note))
    emit('')
    fr = _freshness_summary(rep).get('external_must_recheck') or 0
    if rep['d_items']:
        emit('[D] 未核实清单 %d 条（交付时应逐条带用户自查出路）：' % len(rep['d_items']))
        for it in rep['d_items'][:8]:
            emit('  · %s' % it[:64])
        if len(rep['d_items']) > 8:
            emit('  …另有 %d 条（见 HTML 报告）' % (len(rep['d_items']) - 8))
    emit('')
    emit('汇总：须重查 %s ｜ [D] %d ｜ UNVERIFIED %s —— 报告是交代，不是评分；'
         % (fr, b['D'], _claim_summary(rep).get('unverified', '?')))
    emit('「全部 ✅」≠「数字一定对」，边界见 references/echo-cross.md。')


# ============================================================
# HTML 渲染（自带极简模板——不碰 assets/基准骨架，版式指纹基线不受影响）
# ============================================================

_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --ink:#1c2733; --sub:#5b6b7a; --line:#dde4ea; --bg:#f6f8fa;
  --a:#1e8e5a; --b:#2a7fa8; --c:#c07f1a; --d:#c0392b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px 16px 40px; font:15px/1.7 "PingFang SC",
  "Microsoft YaHei", system-ui, sans-serif; color:var(--ink); background:var(--bg); }}
main {{ max-width:860px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--sub); font-size:13px; margin-bottom:20px; }}
h2 {{ font-size:16px; margin:26px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; }}
.gate {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 12px; }}
.gate .name {{ font-weight:600; }}
.gate .st {{ float:right; font-size:13px; font-weight:600; }}
.st-OK {{ color:var(--a); }} .st-FAIL {{ color:var(--d); }}
.st-SKIP {{ color:var(--sub); }} .st-ERROR {{ color:var(--c); }}
.gate .note {{ color:var(--sub); font-size:13px; }}
.bar {{ display:flex; height:26px; border-radius:6px; overflow:hidden; margin:8px 0 4px; }}
.bar span {{ color:#fff; font-size:12px; line-height:26px; text-align:center; min-width:2px; }}
.legend {{ color:var(--sub); font-size:13px; }}
table {{ width:100%; border-collapse:collapse; background:#fff; font-size:14px; }}
th, td {{ border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }}
th {{ background:#eef2f5; font-weight:600; }}
ul.plain {{ list-style:none; padding:0; margin:0; }}
ul.plain li {{ background:#fff; border:1px solid var(--line); border-radius:6px;
  padding:6px 10px; margin-bottom:6px; }}
footer {{ margin-top:28px; color:var(--sub); font-size:12.5px;
  border-top:1px solid var(--line); padding-top:10px; }}
@media print {{ body {{ background:#fff; padding:0; }} .gate, table, ul.plain li {{ break-inside:avoid; }} }}
</style>
</head>
<body><main>
<h1>{title}</h1>
<div class="sub">事实源 {facts} ｜ 生成于 {generated} ｜ 证据健康度一页纸（纯汇总报告，不是闸门）</div>

<h2>证据徽章分布</h2>
<div class="bar">{bar}</div>
<div class="legend">{legend}</div>

<h2>闸门状态（如实转述）</h2>
<div class="grid">{gates}</div>

<h2>须出发前重查（时效体检）</h2>
{recheck}

<h2>[D] 未核实清单</h2>
{dlist}

<h2>留痕未覆盖声明（UNVERIFIED）</h2>
{unverified}

<footer>
本页由 tools/evidence_report.py 生成，只汇总各闸门的结构化结论，不改判、不折算分数。<br>
「全部通过」≠「数字一定对」：内容真实三层链止于「页面怎么说」，页面在核实日之后
合法改版、JS 渲染页抓不到正文、重合低于阈值的洗稿均兜不住——引用页自身的对错
请在临行前用官方渠道复核，本清单就是为这一步准备的。边界详见
verified-travel-planner/references/echo-cross.md。
</footer>
</main></body></html>
"""


def _h(s: str) -> str:
    return _html.escape(str(s), quote=False)


def render_html(rep: dict) -> str:
    b = rep['badges']
    total = max(b['total'], 1)
    colors = {'A': 'var(--a)', 'B': 'var(--b)', 'C': 'var(--c)', 'D': 'var(--d)'}
    bar = ''.join(
        '<span style="width:%.1f%%;background:%s">%s</span>'
        % (100.0 * b[k] / total, colors[k], (b[k] if b[k] else ''))
        for k in 'ABCD' if b[k])
    if not bar:
        bar = '<span style="width:100%;background:var(--d);text-align:center">全文 0 枚徽章</span>'
    legend = ' ｜ '.join(
        '[%s] %d 枚（%.0f%%）' % (k, b[k], 100.0 * b[k] / total) for k in 'ABCD')

    names = {'source_audit': '标注纪律 source_audit',
             'compact_check': '双版本 compact_check',
             'claim_audit': '声明↔留痕 claim_audit',
             'freshness': '时效 freshness',
             'echo_audit': '来源回声 echo_audit',
             'cross_check': '独立源 cross_check'}
    gates_html = []
    for key, g in rep['gates'].items():
        note = _h(g.get('note') or '')
        gates_html.append(
            '<div class="gate"><span class="st st-%s">%s</span>'
            '<div class="name">%s</div><div class="note">%s</div></div>'
            % (g['status'], g['status'], _h(names.get(key, key)), note))

    fr = _freshness_summary(rep)
    rows = []
    if fr:
        rows.append('<tr><th>外部事实须重查</th><td>%s 条（票价/营业时间超时效）</td></tr>'
                    % fr.get('external_must_recheck', '?'))
        rows.append('<tr><th>路线待重算</th><td>%s 条（出发日例行刷新，不算错）</td></tr>'
                    % fr.get('route_recheck_on_departure', '?'))
        rows.append('<tr><th>仍在时效内</th><td>%s 条</td></tr>' % fr.get('fresh', '?'))
        rows.append('<tr><th>有数值但无时戳</th><td>%s 条（连「新不新」都无从判断）</td></tr>'
                    % fr.get('undated', '?'))
    else:
        rows.append('<tr><td colspan="2">时效闸门未产出结论（被跳过或执行出错）</td></tr>')
    recheck = '<table>%s</table>' % ''.join(rows)

    if rep['d_items']:
        dlist = '<ul class="plain">%s</ul>' % ''.join(
            '<li>%s</li>' % _h(it) for it in rep['d_items'])
        dlist += ('<div class="legend">共 %d 条。交付时应逐条给出用户可自查的出路'
                  '（打哪个电话 / 看哪个公众号 / 去哪比价）。</div>' % len(rep['d_items']))
    else:
        dlist = '<div class="legend">正文没有 [D] 标记——注意「没有标」不等于「都已核实」，'
        dlist += '以源核验 E1 的徽章覆盖结论为准。</div>'

    cs = _claim_summary(rep)
    g = rep['gates']['claim_audit']
    if g['status'] in ('OK', 'FAIL') and isinstance(g.get('data'), dict):
        unv = g['data'].get('c2_unverified') or []
        items = ''.join('<li>%s</li>' % _h(u) for u in unv[:40])
        unverified = ('<div class="legend">实采可寻 %d/%d ｜ UNVERIFIED %d 条——'
                      'UNVERIFIED 是诚实缺口（快照未随附），不是 FAIL；随附快照即可回查。</div>'
                      '<ul class="plain">%s</ul>'
                      % (cs.get('pass', 0), cs.get('pass', 0) + cs.get('fail', 0),
                         len(unv), items))
    else:
        unverified = '<div class="legend">claim_audit 未产出结论：%s</div>' % _h(
            g.get('note') or g['status'])

    return _HTML_TMPL.format(
        title='可信度概览 · %s' % rep['city'],
        facts=_h(rep['facts']),
        generated=_h(rep['generated_at']),
        bar=bar, legend=_h(legend), gates=''.join(gates_html),
        recheck=recheck, dlist=dlist, unverified=unverified)


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='证据健康度一页纸：聚合各闸门结论 + 徽章分布 → 终端摘要与单文件 HTML 概览'
                    '（纯汇总报告，不是闸门：存在 FAIL 也返回 0）')
    ap.add_argument('facts', help='事实源 JSON（路书_XX.json / 路书_XX_事实源.json）')
    ap.add_argument('-o', '--output', help='HTML 输出路径；缺省写到事实源同目录 可信度概览_<城市>.html')
    ap.add_argument('--live', action='store_true',
                    help='连 echo_audit / cross_check 一起实跑（需联网抓引用页，较慢）')
    ap.add_argument('--json', action='store_true', help='只输出机读 JSON，不写 HTML')
    a = ap.parse_args(argv)

    facts_path = Path(a.facts)
    if not facts_path.is_file():
        emit('❌ 读不到事实源：%s' % facts_path)
        return 1
    try:
        json.loads(facts_path.read_text(encoding='utf-8'))
    except ValueError as e:
        emit('❌ 事实源不是合法 JSON：%s' % e)
        return 1

    ex = discover(facts_path)
    rep = aggregate(ex, a.live)

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return 0

    render_terminal(rep)

    out = Path(a.output) if a.output else facts_path.parent / ('可信度概览_%s.html' % ex['city'])
    out.write_text(render_html(rep), encoding='utf-8')
    emit('')
    emit('✅ HTML 概览已写入：%s（单文件自包含，断网可开）' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
