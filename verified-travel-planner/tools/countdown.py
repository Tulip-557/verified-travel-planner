#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
countdown.py — 临行倒排清单：把「出发前记得……」从散文变成 T-N 清单。

为什么需要它
────────────────────────────────────────────────────────────────────────
SKILL.md 阶段 4 的「倒计时行动清单」（T-5 预约 / T-3 普通预约+抢票 / T-1 天气终判 /
退改截止提醒）目前是**手写的正文区块**——写没写、写全没全，没有机器口径。
阶段 6 又要求临行前 3 天跑 freshness + weather + bulletin 三件事。

本工具从**结构化来源**生成倒排清单（不解析散文猜测意图）：

· 规则锚点（出处=SKILL.md 阶段 4/6，标注 rule）：
  T-0 出发日（meta.date_range 起始）；T-1 天气终判+行前公告复扫（高德预报窗口
  只有 4 天，早查无意义）；T-3 普通预约+抢票确认；T-5 高热度预约倒排（放票日）。
· 数据锚点（出处=闸门输出/事实源字段，标注 data）：
  - freshness 的「外部事实须重查」清单（票价/营业时间超时效项）→ 挂到 T-1；
  - 门票 [D] 项（未核实门票价/预约要求）→ 挂到 T-5「逐项官方渠道确认」；
  - 正文含「退改/退票/扣手续费」的句子 → 原样列出，挂到其自身日期或 T-1；
  - meta.bulletin 存在时提示复扫。
· 每条带来源与时戳——「哪个动作、为什么、依据在哪」三件套齐才算一条清单项。

用法
────────────────────────────────────────────────────────────────────────
    python countdown.py 路书_XX.json                  # 基准日取出发日
    python countdown.py 路书_XX.json --on 2026-11-07  # 显式基准日
    python countdown.py 路书_XX.json --json

退出码
────────────────────────────────────────────────────────────────────────
0 = 清单生成（空清单也是 0——它如实说明「没有可倒排的锚点」，不是失败）
1 = 文件读不到 / 不是合法 JSON / 出发日解析不出
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]

# ---- 终端符号自适应（与 freshness.py 同款）
_SYM_FALLBACK = str.maketrans({
    '✅': '[OK]', '→': '->', '⚠': '(!)', '｜': '|', '─': '-', '—': '-', '·': '.',
    'ℹ': '[i]',
})

_DATE_RE = re.compile(r'(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})')
_SPLIT_RE = re.compile(r'<br\s*/?>|</p>|</li>|\n|。|；|;')
_TAG_RE = re.compile(r'<[^>]+>')
_REFUND_RE = re.compile(r'退改|退票|改签|扣.{0,4}手续费|免费取消')
_TICKET_D_RE = re.compile(r'门票[^。；]{0,40}\[D\]|\[D\][^。；]{0,20}门票')


def emit(line=''):
    try:
        print(line)
    except UnicodeEncodeError:
        try:
            print(line.translate(_SYM_FALLBACK))
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or 'ascii'
            print(line.encode(enc, 'replace').decode(enc))


def departure_date(facts):
    m = _DATE_RE.search(str((facts.get('meta') or {}).get('date_range') or ''))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def freshness_must_recheck(facts_path: Path):
    """子进程跑 freshness --json，取「外部事实须重查」条目。跑不动就如实说。"""
    try:
        proc = subprocess.run(
            [sys.executable, 'tools/freshness.py', str(facts_path), '--json'],
            capture_output=True, cwd=str(_SKILL_ROOT), timeout=120)
    except subprocess.TimeoutExpired:
        return None, 'freshness 超时'
    out = proc.stdout.decode('utf-8', 'replace').strip()
    try:
        data = json.loads(out)
    except ValueError:
        i, j = out.find('{'), out.rfind('}')
        if i < 0:
            return None, 'freshness 无 JSON 输出'
        try:
            data = json.loads(out[i:j + 1])
        except ValueError:
            return None, 'freshness JSON 解析失败'
    rows = [r for r in (data.get('dated') or [])
            if r.get('verdict') == 'MUST_RECHECK' and r.get('group') == 'EXTERNAL']
    return rows, None


def build(facts, facts_path: Path, base: date):
    items = []

    def add(days_before, action, source, origin):
        items.append({'t_minus': days_before,
                      'date': (base - timedelta(days=days_before)).isoformat(),
                      'action': action, 'source': source, 'origin': origin})

    # —— 规则锚点（SKILL.md 阶段 4/6）——
    add(0, '出发：带上精简版路书（执行件）；未核实项清单过一遍', 'rule',
        'SKILL.md 阶段 5.5 交两版')
    add(1, '天气终判 + 行前公告复扫：weather 命令重查（预报窗口 4 天，此时才查得到真预报）；'
           'bulletin --plan 复扫闭馆/调价', 'rule', 'SKILL.md 阶段 6 临行前 3 天两件事')
    add(3, '普通预约 + 抢票确认：逐项核对预约状态表（约了什么/截止日/取消规则）', 'rule',
        'SKILL.md 阶段 4 倒计时清单')
    add(5, '高热度预约倒排：按「提前 5 天」核对需预约景点，放票日设提醒', 'rule',
        'SKILL.md 阶段 4 门票/预约')

    # —— 数据锚点：时效须重查（freshness）——
    rows, err = freshness_must_recheck(facts_path)
    if rows:
        for r in rows[:10]:
            add(1, '重查（%s）：%s' % (r.get('name', '?'), str(r.get('text', ''))[:48]),
                'data: freshness', '时戳 %s' % r.get('stamp_date', '?'))
        if len(rows) > 10:
            add(1, '另有 %d 条须重查（freshness --json 看全量）' % (len(rows) - 10),
                'data: freshness', '')
    elif err:
        add(1, '时效体检未能执行（%s）——出发前手动跑一次 freshness' % err, 'data: freshness', '')

    # —— 数据锚点：门票 [D] 项 → T-5 逐项官方渠道确认 ——
    d_tickets = []

    def walk(node):
        if isinstance(node, str):
            plain = _TAG_RE.sub(' ', node)
            for unit in _SPLIT_RE.split(plain):
                unit = unit.strip()
                if len(unit) >= 6 and _TICKET_D_RE.search(unit):
                    d_tickets.append(unit[:120])
        elif isinstance(node, dict):
            if 'full' in node and 'compact' in node:
                walk(node['full'])
            else:
                for k, v in node.items():
                    # _使用说明 等元字段是写给作者的话，不是行程声明
                    if not str(k).startswith('_'):
                        walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(facts)
    for tk in d_tickets[:8]:
        add(5, '门票未核实项，官方渠道确认：%s' % tk, 'data: 事实源 [D]', '')
    if len(d_tickets) > 8:
        add(5, '另有 %d 处门票 [D] 项' % (len(d_tickets) - 8), 'data: 事实源 [D]', '')

    # —— 数据锚点：退改/免费取消句 → 原样列出 ——
    refunds = []
    seen = set()

    def walk_refund(node):
        if isinstance(node, str):
            plain = _TAG_RE.sub(' ', node)
            for unit in _SPLIT_RE.split(plain):
                unit = unit.strip()
                if len(unit) >= 6 and _REFUND_RE.search(unit) and unit[:50] not in seen:
                    seen.add(unit[:50])
                    refunds.append(unit[:120])
        elif isinstance(node, dict):
            if 'full' in node and 'compact' in node:
                walk_refund(node['full'])
            else:
                for k, v in node.items():
                    if not str(k).startswith('_'):
                        walk_refund(v)
        elif isinstance(node, list):
            for v in node:
                walk_refund(v)

    walk_refund(facts)
    for rf in refunds[:6]:
        add(1, '退改/取消条款自查：%s' % rf, 'data: 事实源正文', '')

    # —— meta.bulletin 提示 ——
    if (facts.get('meta') or {}).get('bulletin'):
        add(1, '公告条有内容——按 bulletin --check 复扫一遍再出发', 'data: meta.bulletin', '')

    items.sort(key=lambda x: -x['t_minus'])
    return items


def render_text(path: Path, base: date, items):
    emit('【临行倒排清单】%s ｜ 出发日 %s' % (path.name, base.isoformat()))
    emit('─' * 74)
    last_t = None
    for it in items:
        if it['t_minus'] != last_t:
            last_t = it['t_minus']
            emit('')
            emit('T-%d（%s）' % (it['t_minus'], it['date']))
        emit('  · %s' % it['action'][:74])
        emit('    出处：%s%s' % (it['source'], ('｜' + it['origin']) if it['origin'] else ''))
    emit('')
    emit('─' * 74)
    emit('清单只含结构化锚点（规则 + 闸门输出 + 事实源字段），不解析散文猜意图；')
    emit('「哪天该做什么」以官方渠道实时信息为准——本清单是提醒，不是承诺。')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='临行倒排清单：规则锚点（T-5/T-3/T-1/T-0）+ 数据锚点'
                    '（freshness 须重查/门票 [D]/退改条款）→ T-N 清单，每条带出处')
    ap.add_argument('facts', help='事实源 JSON（路书_XX.json）')
    ap.add_argument('--on', help='基准日 YYYY-MM-DD，缺省取 meta.date_range 起始（出发日）')
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

    if a.on:
        try:
            base = datetime.strptime(a.on.strip(), '%Y-%m-%d').date()
        except ValueError:
            emit('❌ --on 需要 YYYY-MM-DD，收到：%s' % a.on)
            return 1
    else:
        base = departure_date(facts)
        if base is None:
            emit('❌ meta.date_range 解析不出出发日——请显式给 --on YYYY-MM-DD')
            return 1

    items = build(facts, path, base)

    if a.json:
        print(json.dumps({'facts': str(path), 'departure': base.isoformat(),
                          'items': items}, ensure_ascii=False, indent=2))
        return 0
    render_text(path, base, items)
    return 0


if __name__ == '__main__':
    sys.exit(main())
