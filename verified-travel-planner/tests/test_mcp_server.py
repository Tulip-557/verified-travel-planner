#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_server（零依赖 MCP 服务）的机器断言。

覆盖四类判据：
  ① 协议：initialize 握手返回 serverInfo；通知（无 id）不回应；未知方法报 -32601；
     ping 有响应——stdout 是 JSON-RPC 专线，多一条杂音客户端就解析失败；
  ② 工具目录：tools/list ≥12 个工具且各带 inputSchema（MCP 客户端靠它渲染调用面板）；
  ③ 执行语义：闸门退出码原样透传——0 过、2 校验未过（isError=false，输出可读）、
     1 执行错（isError=true）；真实示例 freshness 能跑出结构化 summary；
  ④ 参数模板：build_args 对必填位 / 可选旗标（True/False/字符串）的展开。

运行（零第三方依赖，标准库 unittest）：
    python -m unittest discover -s verified-travel-planner/tests -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SKILL_ROOT / 'tools'))

from mcp_server import build_args  # noqa: E402

SERVER = _SKILL_ROOT / 'tools' / 'mcp_server.py'
EXAMPLES = _SKILL_ROOT.parent / '产出示例'


class _Client:
    """极简 MCP stdio 客户端（测试用）。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(_SKILL_ROOT))

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + '\n').encode())
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline().decode()
        return json.loads(line) if line.strip() else None

    def call(self, tid, name, arguments):
        self.send({'jsonrpc': '2.0', 'id': tid, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': arguments}})
        return self.recv()['result']

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=15)
        return self.proc.returncode


class TestProtocol(unittest.TestCase):
    """① 协议层。"""

    @classmethod
    def setUpClass(cls):
        cls.c = _Client()

    @classmethod
    def tearDownClass(cls):
        rc = cls.c.close()
        assert rc == 0

    def test_initialize_and_ping(self):
        self.c.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        r = self.c.recv()
        self.assertEqual(r['result']['serverInfo']['name'], 'verified-travel-planner')
        self.assertIn('protocolVersion', r['result'])
        self.c.send({'jsonrpc': '2.0', 'id': 9, 'method': 'ping'})
        self.assertEqual(self.c.recv()['result'], {})

    def test_notification_gets_no_response(self):
        self.c.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.c.send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
        # 只应收到 id=2 的响应——通知若被回应，这里收到的 id 就不是 2
        self.assertEqual(self.c.recv()['id'], 2)

    def test_unknown_method_error(self):
        self.c.send({'jsonrpc': '2.0', 'id': 3, 'method': 'no_such_method'})
        self.assertEqual(self.c.recv()['error']['code'], -32601)

    def test_unknown_tool_is_error(self):
        r = self.c.call(4, '不存在工具', {})
        self.assertTrue(r['isError'])


class TestTools(unittest.TestCase):
    """②③ 工具目录与执行语义（用仓库自带示例，不触网）。"""

    @classmethod
    def setUpClass(cls):
        cls.c = _Client()
        cls.c.send({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})
        cls.c.recv()

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_tools_list_schema(self):
        self.c.send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        tools = self.c.recv()['result']['tools']
        self.assertGreaterEqual(len(tools), 12)
        for t in tools:
            self.assertIn('inputSchema', t)
            self.assertEqual(t['inputSchema']['type'], 'object')
        names = {t['name'] for t in tools}
        self.assertIn('evaluate', names)
        self.assertIn('evidence_report', names)

    def test_freshness_on_real_example(self):
        r = self.c.call(2, 'freshness', {
            'input': str(EXAMPLES / '东莞' / '路书_东莞.json')})
        self.assertFalse(r['isError'])
        data = json.loads(r['content'][0]['text'])
        self.assertIn('summary', data)
        self.assertIn('external_must_recheck', data['summary'])

    def test_exit_code_passthrough(self):
        r = self.c.call(3, 'evaluate', {
            'input': str(EXAMPLES / '东莞' / 'itinerary_东莞.json')})
        self.assertFalse(r['isError'])
        self.assertEqual(r['exitCode'], 0)
        r2 = self.c.call(4, 'evaluate', {'input': '不存在.json'})
        self.assertTrue(r2['isError'])
        self.assertEqual(r2['exitCode'], 1)


class TestBuildArgs(unittest.TestCase):
    """④ 参数模板展开。"""

    def test_required_and_optional(self):
        tmpl = ['tools/x.py', '--facts', '{facts}', '{plan:--plan {} }',
                '{compact:--compact }']
        self.assertEqual(
            build_args(tmpl, {'facts': 'a.json', 'plan': 'p.json', 'compact': True}),
            ['tools/x.py', '--facts', 'a.json', '--plan', 'p.json', '--compact'])
        self.assertEqual(
            build_args(tmpl, {'facts': 'a.json'}),
            ['tools/x.py', '--facts', 'a.json'])
        # False / 空串都视为「没给」
        self.assertEqual(
            build_args(tmpl, {'facts': 'a.json', 'compact': False, 'plan': ''}),
            ['tools/x.py', '--facts', 'a.json'])

    def test_path_with_spaces_single_token(self):
        # Windows 路径带空格：值作为单个参数传递，不经 shell 拆分
        out = build_args(['{facts}'], {'facts': r'D:\my dir\f.json'})
        self.assertEqual(out, [r'D:\my dir\f.json'])


if __name__ == '__main__':
    unittest.main()
