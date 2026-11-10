#!/usr/bin/env python3
"""Exercise a real official client against local fixtures, never hosted OpenAI.

Requires loopback sockets and an explicitly supplied official tunnel-client.
The native lifecycle probe redirects only the control-plane destination; it
does not mock command responses or replace the client process. The protocol
probe uses the official client's built-in local dev proxy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/connect-local-tools/scripts/connect.py"
spec = importlib.util.spec_from_file_location("connect", SCRIPT)
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)

# This shim supplies a loopback fixture destination that the product launcher
# intentionally does not expose as user configuration. All state and responses
# still come from the real official client through the real launcher functions.
LAUNCHER = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('connect', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
native = module.native_json
def local_native(binary, args, environment):
    return native(binary, ['--control-plane-base-url', os.environ['LOCAL_TOOLS_TEST_BASE_URL'], *args], environment)
module.native_json = local_native
raise SystemExit(module.main(sys.argv[2:]))
"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def find(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    for child in values:
        found = find(child, key)
        if found is not None:
            return found
    return None


def stop_group(process: subprocess.Popen) -> None:
    def live_members() -> bool:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == process.pid and fields[0] != 'Z':
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 3
        while live_members() and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.05)
        if not live_members():
            break
    process.wait(timeout=5)
    require(not live_members(), "Official proxy process group still has live members")


def managed_runtime(binary: Path, directory: Path, config: Path) -> dict:
    # Deliberately unreachable loopback control plane: process health and
    # detachment can pass, but this can never establish cloud readiness.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{unused.getsockname()[1]}"
    environment = connect.baseline_environment()
    environment.update(LOCAL_TOOLS_TEST_BASE_URL=base,
                       LOCAL_TOOLS_TEST_RUNTIME_KEY="local-fixture-key-not-a-real-account",
                       NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")

    def launch(action: str) -> dict:
        args = [sys.executable, "-c", LAUNCHER, str(SCRIPT), "--client", str(binary),
                "--state-dir", str(directory), action, "--server", "demo"]
        if action == "start":
            args += ["--config", str(config)]
        result = subprocess.run(args, env=environment, text=True, capture_output=True, timeout=90)
        require(result.returncode == 0,
                f"Real native {action} failed ({result.returncode}): {result.stderr[-2000:]}")
        return json.loads(result.stdout)

    try:
        started = launch("start")
        require(connect.contains_true(started, "process_running"), "Native start did not leave a running process")
        # launch() has waited for the launching Python process to exit. A later,
        # separate status process must still see the same detached runtime.
        status = launch("status")
        require(connect.contains_true(status, "process_running"), "Runtime died with its launcher")
        require(connect.contains_true(status, "healthy"), "Runtime health endpoint did not respond")
        pid = find(status, "pid")
        require(isinstance(pid, int) and pid > 0, "Native status did not expose a runtime PID")
        require(os.getsid(pid) == pid, "Runtime did not create its own Unix session")
        # Native /readyz may succeed before a control-plane poll has succeeded.
        # Preserve its actual value rather than treating it as cloud evidence.
        ready = connect.contains_true(status, "ready")
        launch("stop")
        stopped = launch("status")
        require(not connect.contains_true(stopped, "process_running"), "Native stop left its process running")
        return {"launcher_exited_runtime_survived": True, "separate_unix_session": True,
                "healthy": True, "ready_observed": ready, "stop_verified": True,
                "fixture": "unreachable-loopback-control-plane"}
    finally:
        # Also clean up a partially failed startup; the launcher writes ownership
        # before invoking native supervision so stop can recognize that alias.
        if (directory / "receipts" / "demo.json").is_file():
            launch("stop")


def tunneled_call(binary: Path, directory: Path, config: Path) -> dict:
    connection_file = directory / "proxy.json"
    command = shlex.join([sys.executable, str(SCRIPT), "_stdio", "--config", str(config), "--server", "demo"])
    environment = connect.baseline_environment()
    environment.update(NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
    with (directory / "proxy.log").open("wb") as log:
        process = subprocess.Popen([str(binary), "dev", "proxy", "--backend", "go",
                                    "--mcp-command", command, "--url-file", str(connection_file),
                                    "--duration", "60s", "--readiness-timeout", "15s"],
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        transport = None
        try:
            deadline = time.monotonic() + 25
            while not connection_file.is_file():
                require(process.poll() is None, "Official local dev proxy exited before readiness")
                require(time.monotonic() < deadline, "Official local dev proxy did not publish its URL")
                time.sleep(0.1)
            url = json.loads(connection_file.read_text())["mcp_url"]
            connect.validate_url(url)
            transport = connect.HttpProbe({"transport": "http", "url": url, "timeout_seconds": 15})

            def request(request_id: int, method: str, params: dict) -> dict:
                reply = transport.exchange({"jsonrpc": "2.0", "id": request_id,
                                            "method": method, "params": params})
                require("error" not in reply and isinstance(reply.get("result"), dict),
                        f"Official local tunnel rejected {method}")
                return reply["result"]

            initialized = request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                    "clientInfo": {"name": "official-local-smoke", "version": "1"}})
            transport.protocol = initialized["protocolVersion"]
            transport.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = request(2, "tools/list", {})
            require([tool["name"] for tool in tools["tools"]] == ["echo_text"], "Unexpected tunneled tools")
            text = "Real official tunnel: 中文 'quotes' ; $(printf literal)"
            called = request(3, "tools/call", {"name": "echo_text", "arguments": {"text": text}})
            require(called.get("isError", False) is False and called.get("content") == [{"type": "text", "text": text}],
                    "Real official tunnel did not return the exact CLI result")
            return {"official_local_tunnel_tool_call": "passed", "tool": "echo_text",
                    "strict_text_equality": True, "fixture": "official-dev-proxy"}
        finally:
            if transport is not None:
                transport.close()
            stop_group(process)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    args = parser.parse_args()
    binary = args.client.resolve()
    require(sys.platform.startswith('linux'), "This native lifecycle smoke targets Linux")
    require(binary.is_file() and os.access(binary, os.X_OK), "Supply an executable official client")
    with tempfile.TemporaryDirectory(prefix="official-client-smoke-") as temporary:
        directory = Path(temporary)
        config = directory / "demo.connection.json"
        value = connect.load_config(connect.SKILL_DIR / "assets/demo.connection.json")
        value["servers"]["demo"].update(tunnel_id="tunnel_22222222222222222222222222222222",
                                         runtime_key_env="LOCAL_TOOLS_TEST_RUNTIME_KEY")
        connect.write_json(config, value)
        managed = managed_runtime(binary, directory / "managed", config)
        tunneled = tunneled_call(binary, directory, config)
        print(json.dumps({"managed_runtime": managed, "local_tunnel": tunneled,
                          "chatgpt_end_to_end_verified": False}, indent=2))


if __name__ == "__main__":
    main()
