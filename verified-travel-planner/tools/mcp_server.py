#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcp_server.py — 零依赖 MCP 服务：把引擎与闸门暴露给任何 MCP 客户端。

为什么需要它
────────────────────────────────────────────────────────────────────────
本包只有 CLI 一种形态，别的 agent 想用九道闸门得「读 SKILL.md 说明书 → 自己拼命令行」。
`references/landscape.md` 把 MCP 化列为 P3（china-travel-kit 的 CLI/HTTP/MCP 三接口
证明这条路走得通）：对「让别人用上」有帮助。MCP（Model Context Protocol）是
Anthropic 开放协议， JSON-RPC 2.0 over stdio，任何支持 MCP 的客户端
（Claude Desktop / Cursor / ZCode 等）可直接调用。

零第三方依赖的实现口径
────────────────────────────────────────────────────────────────────────
不装 `mcp` SDK——协议子集（initialize / notifications/* / ping / tools/list /
tools/call）用标准库 json + sys.stdin 逐行读实现。**换行分帧**是 MCP stdio 传输的
标准形态（每条消息一行 JSON）。日志一律走 stderr，stdout 只留给 JSON-RPC——
混了一条日志进 stdout，客户端就解析失败。

安全与边界
────────────────────────────────────────────────────────────────────────
· 工具执行 = 以子进程跑本包既有 CLI（不 import、不旁路闸门逻辑），
  退出码语义原样透传：0 过 / 2 校验未过 → isError=true 且**完整输出随回**；
· 文件路径参数原样传给 CLI（服务跑在使用者本机，读的是使用者自己的文件）；
  输出文本超长截断（保头保尾，中间标注截断长度）；
· 只读边界不变：本服务不新增任何写能力，能写文件的只有渲染器（写它自己的 -o）。

用法
────────────────────────────────────────────────────────────────────────
    python tools/mcp_server.py            # stdio 模式（客户端拉起，不是人手跑的）
    python tools/mcp_server.py --list     # 人看的：暴露了哪些工具

客户端配置示例（Claude Desktop / 任何 MCP 客户端）：
    {"mcpServers": {"verified-travel-planner": {
        "command": "python",
        "args": ["<本包路径>/tools/mcp_server.py"]}}}

退出码
────────────────────────────────────────────────────────────────────────
0 = 正常收尾（stdin EOF）；1 = 致命启动错误；JSON-RPC 层错误通过协议报，不用退出码。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]

PROTOCOL_VERSION = '2024-11-05'
SERVER_INFO = {'name': 'verified-travel-planner', 'version': '1.1.0',
               'title': '可核验旅行规划器'}
MAX_TEXT = 20000         # 单条工具输出上限（保头 15000 + 保尾 5000；闸门 JSON 通常 <10k）


def _log(msg: str) -> None:
    """诊断走 stderr——stdout 是 JSON-RPC 专线，一条日志都不能混。"""
    print('[mcp] %s' % msg, file=sys.stderr, flush=True)


# ============================================================
# 工具登记表：name → (描述, JSON Schema, 命令模板)
# ============================================================

def _s(props: dict, required: list = None) -> dict:
    return {'type': 'object', 'properties': props,
            'required': required or [], 'additionalProperties': False}


_P = lambda desc: {'type': 'string', 'description': desc}   # noqa: E731
_B = lambda desc: {'type': 'boolean', 'description': desc}  # noqa: E731

TOOLS = {
    'doctor': (
        '环境体检：解释器/时区库/引擎/高德 key/随包资产 8 项检查，核心全 READY 才算可用',
        _s({}),
        ['tools/doctor.py']),
    'validate_request': (
        '需求采集校验：trip_request.json → READY / NEEDS_CLARIFICATION / INVALID',
        _s({'input': _P('trip_request.json 路径')}, ['input']),
        ['tools/travel_planner.py', 'validate-request', '{input}']),
    'evaluate': (
        '确定性可行性闸门：闭馆冲突/换乘余量/预算闭合；INFEASIBLE 退出码 2（排不通不给）',
        _s({'input': _P('itinerary_XX.json（行程层，带 activities）'),
            'now': _P('基准时刻，如 2026-10-25T13:00+08:00（可选）')}, ['input']),
        ['tools/travel_planner.py', 'evaluate', '--input', '{input}', '{now:--now {} }']),
    'validate_plan': (
        '方案完整性闸门：景点特色/理由/来源 + 内嵌机票/酒店报价校验',
        _s({'input': _P('final_plan_XX.json（方案层）')}, ['input']),
        ['tools/travel_planner.py', 'validate-plan', '--input', '{input}']),
    'source_audit': (
        '证据纪律闸门（8 条规则 E1-E8）：徽章在册/[A]时戳/双源/死线/[D]出路/来源闭环',
        _s({'facts': _P('事实源 JSON'), 'plan': _P('final_plan JSON（可选）')}, ['facts']),
        ['tools/source_audit.py', '--facts', '{facts}', '{plan:--plan {} }', '--json']),
    'freshness': (
        '时效体检：动态数字到出发日还新不新（票价/营业时间/车次按阈值判须重查）',
        _s({'input': _P('事实源 JSON 或渲染 HTML'),
            'on': _P('基准日 YYYY-MM-DD（可选，缺省取出发日）')}, ['input']),
        ['tools/freshness.py', '{input}', '{on:--on {} }', '--json']),
    'render': (
        '渲染器：事实源 JSON → 单文件 HTML（--compact 出精简执行版，--verify 连跑版式校验）',
        _s({'facts': _P('事实源 JSON'), 'output': _P('输出 HTML 路径'),
            'compact': _B('出精简版'), 'verify': _B('渲染后跑版式一致性校验')}, ['facts', 'output']),
        ['tools/render_html.py', '{facts}', '-o', '{output}',
         '{compact:--compact }', '{verify:--verify }']),
    'quality_report': (
        '行程质量评分：节奏疲劳/成本效率/体验匹配/衔接稳健——四维逐点可解释',
        _s({'input': _P('itinerary_XX.json'), 'request': _P('trip_request.json（可选）')},
           ['input']),
        ['tools/quality_report.py', '{input}', '{request:--request {} }', '--json']),
    'route_optimize': (
        '动线优化建议：最近邻+2-opt；直线【估算】口径；只出建议不写回',
        _s({'input': _P('动线 JSON（days[].points[] / stops[] / points[]）'),
            'fix_end': _B('末点固定（如必须回酒店）')}, ['input']),
        ['tools/route_optimize.py', '{input}', '{fix_end:--fix-end }', '--json']),
    'evidence_report': (
        '证据健康度一页纸：聚合各闸门结论+徽章分布+[D] 清单（纯汇总不阻断）',
        _s({'facts': _P('事实源 JSON'), 'live': _B('连 echo/cross 实跑（需联网）')}, ['facts']),
        ['tools/evidence_report.py', '{facts}', '{live:--live }', '--json']),
    'revision_audit': (
        '改版审计：两版事实源声明级 diff；降级须 meta.revision_log 声明',
        _s({'old': _P('旧版事实源'), 'new': _P('新版事实源'),
            'strict': _B('悄悄降级即失败')}, ['old', 'new']),
        ['tools/revision_audit.py', '{old}', '{new}', '{strict:--strict }', '--json']),
    'claim_audit': (
        '声明↔留痕比对（内容真实第 1 层）：实采段时长/段车费必须如实进路书',
        _s({'facts': _P('事实源 JSON'), 'itinerary': _P('行程留痕（可选）'),
            'snapshot': _P('amap 快照（可选）')}, ['facts']),
        ['tools/claim_audit.py', '--facts', '{facts}',
         '{itinerary:--itinerary {} }', '{snapshot:--snapshot {} }', '--json']),
}


# ============================================================
# 参数模板展开：'{key}' / '{key:--flag {} }'（有值才出现）
# ============================================================

def build_args(template: list, arguments: dict) -> list:
    """命令模板展开。

    模板记号两种：
      '{key}'              —— 必填位：有值就原样代入（无值跳过，交给 CLI 报缺参）
      '{key:--flag {} }'   —— 可选位：值存在才出现；True 只出旗标，字符串出 旗标+值
    值含空格（Windows 路径）没问题——subprocess 走参数列表，不经 shell。
    """
    out = []
    for tok in template:
        if not (tok.startswith('{') and tok.endswith('}')):
            out.append(tok)
            continue
        body = tok[1:-1]
        if ':' not in body:
            val = arguments.get(body)
            if val not in (None, False, ''):
                out.append(str(val))
            continue
        key, fmt = body.split(':', 1)
        val = arguments.get(key)
        if val in (None, False, ''):
            continue
        fmt = fmt.strip()
        if val is True:
            flag = fmt.replace('{}', '').strip()
            if flag:
                out.append(flag)
        elif '{}' in fmt:
            pre = fmt.split('{}', 1)[0].strip()
            if pre:
                out.append(pre)
            out.append(str(val))
        else:
            out.append(fmt)
    return out


def run_tool(name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        return {'content': [{'type': 'text', 'text': '未知工具：%s' % name}],
                'isError': True}
    desc, _schema, template = TOOLS[name]
    args = build_args(template, arguments or {})
    try:
        proc = subprocess.run([sys.executable] + args, capture_output=True,
                              cwd=str(_SKILL_ROOT), timeout=300)
    except subprocess.TimeoutExpired:
        return {'content': [{'type': 'text', 'text': '执行超时（300s）：%s' % ' '.join(args)}],
                'isError': True}
    out = (proc.stdout.decode('utf-8', 'replace')
           + (('\n[stderr]\n' + proc.stderr.decode('utf-8', 'replace'))
              if proc.stderr.strip() else '')).strip()
    if len(out) > MAX_TEXT:
        out = (out[:15000] + '\n…[截断：原 %d 字符，保留头尾]\n…' % len(out)
               + out[-5000:])
    return {'content': [{'type': 'text',
                         'text': out or '（无输出，退出码 %d）' % proc.returncode}],
            'isError': proc.returncode not in (0, 2),
            'exitCode': proc.returncode}


# ============================================================
# JSON-RIO 分发（stdout 专线）
# ============================================================

def handle(msg: dict):
    method = msg.get('method', '')
    mid = msg.get('id')
    is_notification = mid is None

    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': mid,
                'result': {'protocolVersion': PROTOCOL_VERSION,
                           'capabilities': {'tools': {}},
                           'serverInfo': SERVER_INFO}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': mid,
                'result': {'tools': [
                    {'name': n, 'description': d, 'inputSchema': s}
                    for n, (d, s, _t) in TOOLS.items()]}}
    if method == 'tools/call':
        params = msg.get('params') or {}
        return {'jsonrpc': '2.0', 'id': mid,
                'result': run_tool(str(params.get('name', '')),
                                   params.get('arguments') or {})}
    # 通知（notifications/initialized 等）不回应——回了客户端会当错误
    if is_notification:
        return None
    return {'jsonrpc': '2.0', 'id': mid,
            'error': {'code': -32601, 'message': '方法不存在：%s' % method}}


def serve() -> int:
    _log('stdio MCP server 启动：%s（%d 个工具）' % (SERVER_INFO['name'], len(TOOLS)))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _log('忽略无法解析的行：%s' % line[:80])
            continue
        try:
            resp = handle(msg)
        except Exception as e:                     # noqa: BLE001 — 协议层兜底，进程不能死
            _log('处理异常：%r' % e)
            resp = {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'error': {'code': -32603, 'message': '内部错误：%s' % e}}
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)
    _log('stdin EOF，正常退出')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='零依赖 stdio MCP 服务：把引擎与闸门暴露给任何 MCP 客户端'
                    '（JSON-RPC 2.0，逐行分帧；日志走 stderr）')
    ap.add_argument('--list', action='store_true', help='人看的：列出暴露的工具')
    a = ap.parse_args(argv)
    if a.list:
        for n, (d, s, _t) in TOOLS.items():
            print('%-18s %s' % (n, d))
            print('%18s 参数：%s' % ('', json.dumps(
                {k: v.get('description', '') for k, v in s['properties'].items()},
                ensure_ascii=False)))
        return 0
    return serve()


if __name__ == '__main__':
    sys.exit(main())
