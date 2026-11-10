"""Offline integration/security regression tests for the stdio tool adapter."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


DEFAULT_ADAPTER = Path(__file__).resolve().parents[1] / "skills/connect-local-tools/scripts/tool_adapter.py"
ADAPTER = Path(os.environ.get("TOOL_ADAPTER_PATH", str(DEFAULT_ADAPTER)))
spec = importlib.util.spec_from_file_location("tool_adapter", ADAPTER)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def schema() -> dict:
    return {"type": "object", "properties": {"text": {"type": "string", "maxLength": 1000}},
            "required": ["text"], "additionalProperties": False}


def cli_tool() -> dict:
    return {"name": "echo", "description": "Echo one literal argument", "kind": "cli", "read_only": True,
            "inputSchema": schema(), "command": [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", "{text}"],
            "timeout_seconds": 3, "max_output_bytes": 4096}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path)
        self.server.observations.append((route.path, self.headers.get("Authorization")))
        if route.path == "/trickle-headers":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(40):
                    self.connection.sendall(b"x")
                    time.sleep(0.04)
                self.connection.sendall(b"\r\nContent-Length: 2\r\n\r\nok")
            return
        if route.path == "/trickle-body":
            self.send_response(200)
            self.send_header("Content-Length", "40")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                for _ in range(40):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
            return
        if route.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/final")
            self.end_headers()
            return
        if route.path == "/slow":
            time.sleep(1)
        if route.path == "/error":
            self.send_response(401)
            body = b"SENSITIVE_RESPONSE_BODY"
        else:
            self.send_response(200)
            body = json.dumps({"query": urllib.parse.parse_qs(route.query), "auth": self.headers.get("Authorization")}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.observations.append(("POST", self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.observations = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.json"

    def write_config(self, tools: list[dict]) -> dict:
        self.config_path.write_text(json.dumps({"version": 1, "name": "test-adapter", "tools": tools}), encoding="utf-8")
        return adapter.load_config(self.config_path)

    def http_tool(self, route="/echo"):
        return {"name": "http", "description": "Use the local fixture", "kind": "http", "read_only": True,
                "inputSchema": schema(), "url": f"http://127.0.0.1:{self.server.server_port}{route}",
                "method": "GET", "query": {"q": "{text}"}, "timeout_seconds": 2, "max_output_bytes": 4096}

    def stdio(self, tools, messages, *args):
        self.write_config(tools)
        process = subprocess.run([sys.executable, str(ADAPTER), "--config", str(self.config_path), *args],
                                 input="".join(json.dumps(message) + "\n" for message in messages),
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return [json.loads(line) for line in process.stdout.splitlines()]

    @staticmethod
    def handshake():
        return [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "tests", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]

    def test_real_stdio_handshake_list_call_and_unknown_method(self):
        messages = self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "你好 world"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        ]
        replies = self.stdio([cli_tool()], messages)
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertTrue(replies[1]["result"]["tools"][0]["annotations"]["readOnlyHint"])
        self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"]), ["你好 world"])
        self.assertEqual(replies[3]["error"]["code"], -32601)

    def test_write_tools_are_hidden_and_cannot_be_called_without_flag(self):
        write = cli_tool()
        write.update(name="write", read_only=False)
        messages = self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "write", "arguments": {"text": "test"}}},
        ]
        replies = self.stdio([cli_tool(), write], messages)
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], ["echo"])
        self.assertEqual(replies[2]["error"]["code"], -32602)
        enabled = self.stdio([cli_tool(), write], messages, "--enable-writes")
        self.assertEqual(len(enabled[1]["result"]["tools"]), 2)
        self.assertFalse(enabled[2]["result"]["isError"])

    def test_schema_validation_prevents_undeclared_fields_and_wrong_types(self):
        config = self.write_config([cli_tool()])
        service = adapter.Adapter(config)
        service.initialized = service.ready = True
        for arguments in ({"text": 1}, {"text": "a", "extra": "b"}, {}, {"text": "x" * 1001}):
            response = service.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": arguments}})
            self.assertTrue(response["result"]["isError"])
        adapter.validate_value(3, {"type": "integer", "minimum": 1, "maximum": 5})
        with self.assertRaises(adapter.ToolError):
            adapter.validate_value(True, {"type": "integer"})

    def test_unsupported_schema_features_and_executable_templates_are_rejected(self):
        for mutate in (
            lambda tool: tool["inputSchema"]["properties"]["text"].update(pattern=".*"),
            lambda tool: tool["inputSchema"].update(additionalProperties=True),
            lambda tool: tool["inputSchema"].update(type=["object", "null"]),
            lambda tool: tool["command"].__setitem__(0, "{text}"),
            lambda tool: tool["command"].append("{text.__class__}"),
        ):
            tool = cli_tool()
            mutate(tool)
            with self.assertRaises(adapter.ConfigError):
                self.write_config([tool])

    def test_check_never_executes_a_configured_command(self):
        marker = Path(self.temp.name) / "should-not-exist"
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('bad')"]
        self.write_config([tool])
        result = subprocess.run([sys.executable, str(ADAPTER), "--config", str(self.config_path), "--check"], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Configuration valid", result.stdout)
        self.assertFalse(marker.exists())

    def test_cli_argv_is_literal_and_option_like_inputs_are_rejected(self):
        marker = Path(self.temp.name) / "injected"
        tool = self.write_config([cli_tool()])["tools"][0]
        text = f"hello; $(touch {marker}) && echo bypass"
        self.assertEqual(json.loads(adapter.run_cli(tool, {"text": text})), [text])
        self.assertFalse(marker.exists())
        with self.assertRaises(adapter.ToolError):
            adapter.run_cli(tool, {"text": "--dangerous-option"})

    def test_cli_environment_is_explicit_and_secret_output_is_redacted(self):
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", "import os,json; print(json.dumps([os.getenv('OPENAI_API_KEY'),os.getenv('BRIDGE_PRIVATE_TEST'),os.getenv('TOOL_KEY')]))"]
        tool["env_from"] = {"TOOL_KEY": "BRIDGE_PRIVATE_TEST"}
        config = self.write_config([tool])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-sensitive", "BRIDGE_PRIVATE_TEST": "adapter-sensitive"}):
            result = json.loads(adapter.run_cli(config["tools"][0], {"text": "unused"}))
        self.assertEqual(result, [None, None, "[redacted]"])

    def test_cli_redacts_raw_and_json_escaped_credentials(self):
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", (
            "import os,json; value=os.environ['TOOL_KEY']; "
            "print(json.dumps(dict(key=value), ensure_ascii=True)); "
            "print(json.dumps(dict(key=value), ensure_ascii=False)); print(value)"
        )]
        tool["env_from"] = {"TOOL_KEY": "BRIDGE_PRIVATE_TEST"}
        secret = 'quoted"key\\with\nnewlines\t世界🙂'
        configured = self.write_config([tool])["tools"][0]
        with patch.dict(os.environ, {"BRIDGE_PRIVATE_TEST": secret}):
            output = adapter.run_cli(configured, {"text": "unused"})
        lines = output.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0]), {"key": "[redacted]"})
        self.assertEqual(json.loads(lines[1]), {"key": "[redacted]"})
        self.assertEqual(lines[2], "[redacted]")
        for representation in (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]):
            self.assertNotIn(representation, output)

    @unittest.skipUnless(os.name == "posix", "Requires POSIX session/pipe semantics")
    def test_cli_timeout_is_bounded_when_detached_descendant_holds_pipes(self):
        pid_path = Path(self.temp.name) / "detached-child.pid"
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", (
            "import subprocess,sys; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'],start_new_session=True); "
            f"Path({str(pid_path)!r}).write_text(str(child.pid)); "
            "print('parent finished',flush=True)"
        )]
        tool["timeout_seconds"] = 0.1
        configured = self.write_config([tool])["tools"][0]
        start = time.monotonic()
        try:
            with self.assertRaisesRegex(adapter.ToolError, "timed out"):
                adapter.run_cli(configured, {"text": "unused"})
            elapsed = time.monotonic() - start
            self.assertTrue(pid_path.is_file(), "The escaped descendant fixture was not started")
            # The descendant intentionally keeps stdout/stderr open for 5 s.
            # A 0.1 s timeout must not wait on BufferedReader.close/read locks.
            self.assertLess(elapsed, 0.8)
            os.kill(int(pid_path.read_text()), 0)
        finally:
            if pid_path.is_file():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(pid_path.read_text()), signal.SIGKILL)

    def test_cli_timeout_output_bounds_and_stderr_redaction(self):
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", "import time; time.sleep(5)"]
        tool["timeout_seconds"] = 0.15
        configured = self.write_config([tool])["tools"][0]
        start = time.monotonic()
        with self.assertRaisesRegex(adapter.ToolError, "timed out"):
            adapter.run_cli(configured, {"text": "unused"})
        self.assertLess(time.monotonic() - start, 2)
        tool["command"] = [sys.executable, "-c", "print('x'*1000)"]
        tool["max_output_bytes"] = 32
        with self.assertRaisesRegex(adapter.ToolError, "limit"):
            adapter.run_cli(self.write_config([tool])["tools"][0], {"text": "unused"})
        tool["command"] = [sys.executable, "-c", "import sys; print('SENSITIVE_STDERR',file=sys.stderr); sys.exit(2)"]
        with self.assertRaises(adapter.ToolError) as caught:
            adapter.run_cli(self.write_config([tool])["tools"][0], {"text": "unused"})
        self.assertNotIn("SENSITIVE_STDERR", str(caught.exception))
        tool["command"] = [sys.executable, "-c", "import sys; sys.stderr.write('x'*70000)"]
        with self.assertRaisesRegex(adapter.ToolError, "limit"):
            adapter.run_cli(self.write_config([tool])["tools"][0], {"text": "unused"})

    def test_cli_cannot_report_success_until_exit_is_known(self):
        tool = cli_tool()
        tool["command"] = [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(2)"]
        tool["timeout_seconds"] = 0.1
        configured = self.write_config([tool])["tools"][0]
        with self.assertRaisesRegex(adapter.ToolError, "timed out"):
            adapter.run_cli(configured, {"text": "unused"})

    def test_http_query_authentication_and_loopback_proxy_bypass(self):
        tool = self.http_tool()
        tool["headers_from_env"] = {"Authorization": {"env": "ADAPTER_HTTP_SECRET", "prefix": "Bearer "}}
        configured = self.write_config([tool])["tools"][0]
        with patch.dict(os.environ, {"ADAPTER_HTTP_SECRET": "never-echo-this", "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1"}):
            result = json.loads(adapter.run_http(configured, {"text": "hello&admin=true"}))
        self.assertEqual(result["query"], {"q": ["hello&admin=true"]})
        self.assertEqual(result["auth"], "Bearer [redacted]")
        self.assertEqual(self.server.observations[-1][1], "Bearer never-echo-this")

    def test_http_redirect_is_not_followed_and_error_bodies_are_withheld(self):
        self.server.observations.clear()
        configured = self.write_config([self.http_tool("/redirect")])["tools"][0]
        with self.assertRaisesRegex(adapter.ToolError, "redirect refused"):
            adapter.run_http(configured, {"text": "a"})
        self.assertEqual([route for route, _ in self.server.observations], ["/redirect"])
        configured = self.write_config([self.http_tool("/error")])["tools"][0]
        with self.assertRaises(adapter.ToolError) as caught:
            adapter.run_http(configured, {"text": "a"})
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("SENSITIVE", str(caught.exception))

    def test_http_limits_and_fixed_endpoint_policy(self):
        tool = self.http_tool()
        tool["max_output_bytes"] = 8
        with self.assertRaisesRegex(adapter.ToolError, "output limit"):
            adapter.run_http(self.write_config([tool])["tools"][0], {"text": "long text"})
        for url in ("http://example.com/api", "http://localhost/api", "https://user:secret@example.com/api", "https://example.com/{text}", "https://example.com/api#fragment"):
            tool["url"] = url
            with self.assertRaises(adapter.ConfigError):
                self.write_config([tool])

    def test_http_deadline_covers_trickling_headers_and_body(self):
        for route in ("/trickle-headers", "/trickle-body"):
            with self.subTest(route=route):
                tool = self.http_tool(route)
                tool["timeout_seconds"] = 0.2
                configured = self.write_config([tool])["tools"][0]
                observed_before = len(self.server.observations)
                start = time.monotonic()
                with self.assertRaisesRegex(adapter.ToolError, "timed out"):
                    adapter.run_http(configured, {"text": "test"})
                self.assertLess(time.monotonic() - start, 0.8)
                self.assertIn(route, [path for path, _ in self.server.observations[observed_before:]])

    def test_http_post_json_preserves_parameter_types(self):
        tool = self.http_tool()
        tool["method"] = "POST"
        tool["inputSchema"]["properties"]["count"] = {"type": "integer", "minimum": 1, "maximum": 5}
        tool["inputSchema"]["required"].append("count")
        tool["json_body"] = {"text": "{text}", "count": "{count}", "nested": [True, "literal"]}
        result = adapter.run_http(self.write_config([tool])["tools"][0], {"text": "hello", "count": 3})
        self.assertEqual(json.loads(result), {"text": "hello", "count": 3, "nested": [True, "literal"]})

    def test_invalid_stdio_json_and_uninitialized_tool_calls(self):
        self.write_config([cli_tool()])
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        process = subprocess.run([sys.executable, str(ADAPTER), "--config", str(self.config_path)],
                                 input="{broken\n" + json.dumps(request) + "\n", text=True, capture_output=True, timeout=3)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32002)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
