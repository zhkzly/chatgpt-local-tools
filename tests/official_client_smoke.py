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
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


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
                       NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")

    def launch(action: str, extra: list[str] | None = None) -> dict:
        args = [sys.executable, "-c", LAUNCHER, str(SCRIPT), "--client", str(binary),
                "--state-dir", str(directory), action, "--server", "demo"]
        if action in ("start", "migrate-legacy"):
            args += ["--config", str(config)]
        args += extra or []
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
        # Exercise a second invocation with no key environment or terminal input.
        # The reviewed legacy fixture uses the same flock/SIGINT contract as the
        # previous Python launcher; it does not run a second tunnel instance.
        old_script = directory / "reviewed-legacy.py"
        old_lock = directory / "legacy.lock"
        old_script.write_text("import fcntl,sys,time\nf=open(sys.argv[1],'w')\nfcntl.flock(f,fcntl.LOCK_EX)\nprint('locked',flush=True)\ntime.sleep(60)\n")
        old = subprocess.Popen([sys.executable, str(old_script), str(old_lock)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            readable, _, _ = select.select([old.stdout], [], [], 3)
            require(bool(readable) and old.stdout.readline() == b'locked\n', "Legacy fixture did not lock")
            migrated = launch("migrate-legacy", ["--legacy-script", str(old_script),
                              "--legacy-lock", str(old_lock), "--legacy-profile", str(directory / 'profiles/demo.yaml')])
            require(migrated.get('legacy_runner_migrated') is True, "Reviewed legacy fixture was not handed over")
            old.wait(timeout=3)
            require(connect.contains_true(migrated, "process_running"), "Restart with saved key did not run")
            require(connect.contains_true(migrated, "healthy"), "Restart with saved key was not healthy")
            profile = (directory / 'profiles/demo.yaml').read_text()
            require('file:' in profile and 'local-fixture-key-not-a-real-account' not in profile,
                    "Native profile must hold a file reference, never the fixture key")
            launch("stop")
            require(not connect.contains_true(launch("status"), "process_running"), "Restarted runtime did not stop")
        finally:
            stop_group(old)
            old.stdout.close()
        return {"launcher_exited_runtime_survived": True, "separate_unix_session": True,
                "healthy": True, "ready_observed": ready, "stop_verified": True,
                "saved_key_restart_without_prompt": True, "reviewed_legacy_handover": True,
                "fixture": "unreachable-loopback-control-plane"}
    finally:
        # Also clean up a partially failed startup; the launcher writes ownership
        # before invoking native supervision so stop can recognize that alias.
        if (directory / "receipts" / "demo.json").is_file():
            launch("stop")


def tunneled_call(binary: Path, directory: Path, config: Path, *, send_initialized: bool = True,
                  expect_initialization_error: bool = False) -> dict:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection_file = directory / "proxy.json"
    command = shlex.join([sys.executable, str(SCRIPT), "_stdio", "--config", str(config), "--server", "demo"])
    server = connect.selected(connect.load_config(config), 'demo')
    environment = connect.native_environment(directory / 'native', server)
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
            if send_initialized:
                transport.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
            if expect_initialization_error:
                headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                           "MCP-Protocol-Version": transport.protocol}
                if transport.session:
                    headers['Mcp-Session-Id'] = transport.session
                request_body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
                outgoing = urllib.request.Request(url, data=json.dumps(request_body).encode(), headers=headers, method='POST')
                try:
                    with transport.opener.open(outgoing, timeout=15) as response:
                        status, raw = response.status, response.read(connect.MAX_MESSAGE + 1)
                except urllib.error.HTTPError as error:
                    status, raw = error.code, error.read(connect.MAX_MESSAGE + 1)
                    error.close()
                require(len(raw) <= connect.MAX_MESSAGE, 'Initialization error exceeded its limit')
                if raw.lstrip().startswith(b'{'):
                    rejected = json.loads(raw)
                else:
                    rejected = json.loads(next(line[5:].strip() for line in raw.splitlines() if line.startswith(b'data:')))
                error = rejected.get('error', {})
                require(rejected.get('id') == 2 and error.get('code') == -32002 and
                        error.get('data', {}).get('origin') == 'tunnel-client',
                        'Omitted notification did not reproduce the native initialization gate')
                return {'omitted_notification_rejected': True, 'http_status': status,
                        'error_type': error['data'].get('error_type')}
            tools = request(2, "tools/list", {})
            require([tool["name"] for tool in tools["tools"]] == ["echo_text"], "Unexpected tunneled tools")
            text = "Real official tunnel: 中文 'quotes' ; $(printf literal)"
            called = request(3, "tools/call", {"name": "echo_text", "arguments": {"text": text}})
            require(called.get("isError", False) is False and called.get("content") == [{"type": "text", "text": text}],
                    "Real official tunnel did not return the exact CLI result")
            return {"official_local_tunnel_tool_call": "passed", "tool": "echo_text",
                    "strict_text_equality": True, "fixture": "official-dev-proxy",
                    "caller_sent_initialized": send_initialized,
                    "notification_shim": server.get('stdio_send_initialized_notification', False)}
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
        value["servers"]["demo"].pop('runtime_key_env', None)
        key_file = directory / '.credentials/runtime.key'
        connect.write_runtime_key(key_file, 'local-fixture-key-not-a-real-account')
        value["servers"]["demo"].update(tunnel_id="tunnel_22222222222222222222222222222222",
                                         runtime_key_file=str(key_file))
        connect.write_json(config, value)
        managed = managed_runtime(binary, directory / "managed", config)
        tunneled = tunneled_call(binary, directory / 'normal', config)
        omitted = tunneled_call(binary, directory / 'without-notification', config,
                                send_initialized=False, expect_initialization_error=True)
        value['servers']['demo']['stdio_send_initialized_notification'] = True
        connect.write_json(config, value, replace=True)
        shimmed = tunneled_call(binary, directory / 'with-notification-shim', config, send_initialized=False)
        print(json.dumps({"managed_runtime": managed, "local_tunnel": tunneled,
                          "missing_initialized_notification": omitted,
                          "initialized_notification_shim": shimmed,
                          "chatgpt_end_to_end_verified": False}, indent=2))


if __name__ == "__main__":
    main()
