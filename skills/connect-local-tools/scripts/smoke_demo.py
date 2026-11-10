#!/usr/bin/env python3
"""Call the bundled local echo MCP tool; no account or tunnel is required."""

from __future__ import annotations

import sys

import connect


def smoke() -> dict:
    config = connect.SKILL_DIR / "assets" / "demo.connection.json"
    argv = [sys.executable, str(connect.SCRIPT), "_stdio", "--config", str(config), "--server", "demo"]
    transport = connect.StdioProbe(argv, connect.SKILL_DIR, connect.baseline_environment(), 10)

    def request(request_id: int, method: str, params: dict) -> dict:
        reply = transport.exchange({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if "error" in reply or not isinstance(reply.get("result"), dict):
            raise connect.SetupError(f"Local demo {method} did not succeed.")
        return reply["result"]

    sample = "Local tools: 中文 'quotes' ; $(printf literal)"
    try:
        request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "local-demo-smoke", "version": "1"}})
        transport.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = request(2, "tools/list", {})
        if [tool.get("name") for tool in listed.get("tools", [])] != ["echo_text"]:
            raise connect.SetupError("Local demo did not expose the expected echo_text tool.")
        called = request(3, "tools/call", {"name": "echo_text", "arguments": {"text": sample}})
        if called.get("isError", False) is not False or called.get("content") != [{"type": "text", "text": sample}]:
            raise connect.SetupError("Local demo did not return the input text exactly.")
        return {"local_tool_call": "passed", "tool": "echo_text", "strict_text_equality": True,
                "chatgpt_end_to_end_verified": False}
    finally:
        transport.close()


if __name__ == "__main__":
    try:
        connect.say(smoke())
    except connect.SetupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
