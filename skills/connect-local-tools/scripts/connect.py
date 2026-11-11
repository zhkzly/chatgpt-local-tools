#!/usr/bin/env python3
"""Connect explicitly configured local MCP servers through OpenAI's tunnel client.

Python 3.10+, standard library. inspect/import-mcp never execute target commands.
check starts the configured MCP or contacts its HTTP endpoint for discovery only.
start delegates background supervision to the official client's native runtimes.
No cloud tunnel, API key, ChatGPT plugin, or system service is created here.
Explicit runtime_key_file configuration can save/reuse an existing runtime key.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

VERSION = "v0.0.20"
ARCHIVE_SHA256 = {
    "amd64": "2f06ce5a17425c74a827e7038692936e973ae388e7eec728c40efee1fb5f139d",
    "arm64": "2d89946c23111eb910f1bd4e7c5c4bfedfd73ac0dc9828aefc36294cfeae491f",
}
MAX_CONFIG = 2 * 1024 * 1024
MAX_MESSAGE = 2 * 1024 * 1024
MAX_DOWNLOAD = 256 * 1024 * 1024
ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TUNNEL_ID = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}\Z")
SECRET_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|AUTHORIZATION|CREDENTIALS?)(?:_|$)", re.I)
BASE_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "SHELL", "TMPDIR", "TMP", "TEMP",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY", "WAYLAND_DISPLAY", "SYSTEMROOT", "WINDIR", "PATHEXT",
}
RESERVED_RUNTIME_PREFIXES = (
    "LC_", "CONTROL_PLANE_", "MCP_", "HARPOON_", "HEALTH_", "ADMIN_UI_", "OAUTH_",
    "TUNNEL_CLIENT_", "CLOUDFLARED_", "LOG_", "PYTHON", "NODE_", "LD_", "DYLD_",
    "GIT_", "SSH_", "RUBY", "PERL", "BASH_",
)
RESERVED_RUNTIME_NAMES = {"ENV", "IFS", "CDPATH", "SHELLOPTS", "BASHOPTS"}
SERVER_KEYS = {
    "transport", "command", "args", "cwd", "env", "env_from", "url", "headers_from_env",
    "tunnel_id", "runtime_key_env", "runtime_key_file", "timeout_seconds",
    "stdio_send_initialized_notification",
}
RESERVED_HEADERS = {"host", "content-length", "content-type", "accept", "mcp-session-id", "mcp-protocol-version", "proxy-authorization"}
SCRIPT = Path(__file__).resolve()
SKILL_DIR = SCRIPT.parent.parent


class SetupError(Exception):
    """A diagnostic which intentionally excludes secrets and remote payloads."""


def say(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value, flush=True)


def read_json(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_CONFIG:
            raise SetupError("JSON file exceeds the 2 MiB limit.")
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise SetupError("Cannot read a valid JSON configuration; inspect the selected local file.") from error
    if not isinstance(value, dict):
        raise SetupError("Configuration must be a JSON object.")
    return value


def write_json(path: Path, value: dict, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not replace:
        raise SetupError("Output already exists; choose a new output file.")
    fd, tmp_name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if replace:
            if path.is_symlink():
                raise SetupError("Refusing to replace a symbolic-link state file.")
            os.replace(tmp, path)
        else:
            # Hard linking gives no-overwrite semantics even under concurrent imports.
            os.link(tmp, path)
    except FileExistsError as error:
        raise SetupError("Output already exists; choose a new output file.") from error
    finally:
        tmp.unlink(missing_ok=True)


def only_keys(value: dict, allowed: set[str], context: str) -> None:
    if set(value) - allowed:
        raise SetupError(f"Unknown field in {context}; check the documented configuration schema.")


def string_map(value, context: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise SetupError(f"{context} must map strings to strings.")
    if any("\x00" in k or "\x00" in v for k, v in value.items()):
        raise SetupError(f"{context} contains a NUL byte.")
    return value


def validate_url(url: str) -> bool:
    """Return whether this is literal loopback; never accept embedded credentials."""
    if not isinstance(url, str) or any(c in url for c in "\r\n\x00"):
        raise SetupError("MCP URL must be a plain string without control characters.")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as error:
        raise SetupError("Invalid MCP URL.") from error
    if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise SetupError("MCP URL needs a host and cannot contain credentials, query parameters, or fragments; use headers_from_env for authentication.")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise SetupError("Use HTTPS, or HTTP to a literal loopback address such as 127.0.0.1.")
    if port is not None and not (1 <= port <= 65535):
        raise SetupError("Invalid MCP port.")
    return loopback


def runtime_key_name(server: dict) -> str:
    name = server.get("runtime_key_env", "OPENAI_TUNNEL_KEY")
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        raise SetupError("runtime_key_env must be an environment variable name, never a key value.")
    if name.upper() in {key.upper() for key in BASE_ENV | RESERVED_RUNTIME_NAMES} or name.upper().startswith(RESERVED_RUNTIME_PREFIXES):
        raise SetupError("runtime_key_env cannot use a system, proxy, interpreter, or tunnel configuration variable. Choose a dedicated name such as OPENAI_TUNNEL_KEY.")
    return name


def review_import_arguments(original: dict) -> None:
    """Reject common inline credential forms; this is not an exhaustive secret scanner."""
    arguments = original.get("args", [])
    if not isinstance(arguments, list) or any(not isinstance(v, str) for v in arguments):
        raise SetupError("Imported args must be an array of strings.")
    texts = [v for v in (original.get("command"), original.get("url")) if isinstance(v, str)] + arguments
    credential_flag = re.compile(r"^--(?:api[-_]?key|access[-_]?token|auth[-_]?token|refresh[-_]?token|token|password|passwd|secret|authorization|cookie)(?:=|$)", re.I)
    credential_query = {"key", "api_key", "apikey", "token", "access_token", "refresh_token", "auth", "authorization", "password", "passwd", "secret", "cookie", "session", "session_id", "signature", "sig", "x_amz_signature", "x_goog_signature"}
    message = "Imported command contains a credential-bearing flag, header, or URL. Move credentials to environment references and review command/args before importing."
    for text in texts:
        if credential_flag.search(text) or re.search(r"\bBearer\s+\S", text, re.I):
            raise SetupError(message)
        if re.search(r"(?:^|=|\s)(?:Authorization|Proxy-Authorization|Cookie|X-API-Key)\s*:", text, re.I):
            raise SetupError(message)
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", text):
            try:
                parts = urllib.parse.urlsplit(match.group(0))
                params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                if parts.username is not None or parts.password is not None or any(key.lower().replace("-", "_") in credential_query for key, _ in params):
                    raise SetupError(message)
            except ValueError as error:
                raise SetupError("Imported command contains an invalid URL; review it locally before importing.") from error


def validate_config(config: dict) -> dict:
    only_keys(config, {"version", "servers"}, "configuration")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise SetupError("Configuration version must be 1.")
    servers = config.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise SetupError("servers must be a nonempty object.")
    for name, server in servers.items():
        if not isinstance(name, str) or not ALIAS.fullmatch(name):
            raise SetupError("Server names must be 1–48 lowercase letters, digits, hyphens, or underscores, starting with a letter or digit.")
        if not isinstance(server, dict):
            raise SetupError("Each server must be an object.")
        only_keys(server, SERVER_KEYS, "server")
        transport = server.get("transport")
        if transport not in ("stdio", "http"):
            raise SetupError("transport must be stdio or http; a REST API or ordinary CLI requires an MCP adapter first.")
        if "stdio_send_initialized_notification" in server:
            if transport != "stdio" or type(server["stdio_send_initialized_notification"]) is not bool:
                raise SetupError("stdio_send_initialized_notification must be a boolean on a stdio connection.")
        if transport == "stdio":
            command = server.get("command")
            args = server.get("args", [])
            if not isinstance(command, str) or not command or "\x00" in command:
                raise SetupError("stdio command must name one executable, not a shell expression.")
            if not isinstance(args, list) or any(not isinstance(a, str) or "\x00" in a for a in args):
                raise SetupError("stdio args must be an array of strings.")
            if "url" in server or "headers_from_env" in server:
                raise SetupError("stdio servers cannot contain HTTP connection fields.")
        else:
            validate_url(server.get("url"))
            if any(k in server for k in ("command", "args", "cwd", "env", "env_from")):
                raise SetupError("HTTP servers cannot contain stdio process fields.")
        if "cwd" in server and (not isinstance(server["cwd"], str) or not server["cwd"] or "\x00" in server["cwd"]):
            raise SetupError("cwd must be a nonempty path string.")
        env = string_map(server.get("env", {}), "env")
        refs = string_map(server.get("env_from", {}), "env_from")
        key_env = runtime_key_name(server)
        if "runtime_key_file" in server:
            path = server["runtime_key_file"]
            if not isinstance(path, str) or not path or "\x00" in path:
                raise SetupError("runtime_key_file must be a nonempty local path, never a key value.")
        for key, value in env.items():
            if not ENV_NAME.fullmatch(key):
                raise SetupError("Invalid environment variable name.")
            if SECRET_NAME.search(key) or key == key_env:
                raise SetupError("Credential-like environment names must use env_from, not literal env values.")
        for key, source in refs.items():
            if not ENV_NAME.fullmatch(key) or not ENV_NAME.fullmatch(source):
                raise SetupError("env_from must contain environment variable names only.")
            if key == key_env or source == key_env:
                raise SetupError("Do not forward the tunnel runtime key to the MCP process; use a separate tool credential.")
        if set(env) & set(refs):
            raise SetupError("A variable cannot appear in both env and env_from.")
        for header, source in string_map(server.get("headers_from_env", {}), "headers_from_env").items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header) or header.lower() in RESERVED_HEADERS:
                raise SetupError("Invalid or reserved HTTP header name.")
            if not ENV_NAME.fullmatch(source) or source == key_env:
                raise SetupError("HTTP credentials must reference a separate environment variable.")
        if "tunnel_id" in server and not isinstance(server["tunnel_id"], str):
            raise SetupError("tunnel_id must be a string.")
        timeout = server.get("timeout_seconds", 15)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
            raise SetupError("timeout_seconds must be between 1 and 120.")
    return config


def load_config(path: Path) -> dict:
    return validate_config(read_json(path))


def selected(config: dict, name: str) -> dict:
    try:
        return config["servers"][name]
    except KeyError as error:
        raise SetupError("Unknown server name; use inspect to list configured names.") from error


def expand(value: str, config_path: Path) -> str:
    return value.replace("${CONFIG_DIR}", str(config_path.parent)).replace("${SKILL_DIR}", str(SKILL_DIR))


def runtime_key_path(server: dict, config_path: Path) -> Path | None:
    value = server.get("runtime_key_file")
    if value is None:
        return None
    path = Path(expand(value, config_path)).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    # Do not resolve the final component: symlinks must remain detectable.
    return Path(os.path.abspath(path))


def validate_runtime_key_file(path: Path, *, allow_empty: bool = False) -> None:
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise SetupError("The credential parent must be an owned directory without group/other write access.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in (0o600, 0o400):
                raise SetupError("The runtime key must be an owned regular file with permissions 0600 or 0400.")
            if info.st_size > 8192 or (not allow_empty and info.st_size == 0):
                raise SetupError("The runtime key file is empty or exceeds 8192 bytes.")
    except OSError as error:
        raise SetupError("Cannot read the protected runtime key file; inspect its owner, permissions and symlink status locally.") from error


def write_runtime_key(path: Path, value: str, *, replace: bool = False) -> None:
    value = value.strip()
    if not value or len(value.encode("utf-8")) > 8192 or any(c in value for c in "\r\n\x00"):
        raise SetupError("Runtime key is empty, oversized or contains unsupported control characters.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise SetupError("Use an owned 0700 credential directory, for example ${CONFIG_DIR}/.credentials.")
    if path.is_symlink():
        raise SetupError("Refusing a symbolic-link runtime key file.")
    if replace and path.exists():
        validate_runtime_key_file(path, allow_empty=True)
    descriptor, name = tempfile.mkstemp(prefix=".runtime-key-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError as error:
        raise SetupError("A runtime key is already saved. Use save-key --replace for deliberate rotation.") from error
    finally:
        temporary.unlink(missing_ok=True)


def baseline_environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in BASE_ENV or k.startswith("LC_")}


def credential(source: str) -> str:
    value = os.environ.get(source)
    if not value:
        raise SetupError(f"Required environment variable is missing: {source}. Set it locally; do not paste credentials into chat.")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise SetupError(f"Environment variable {source} contains unsupported control characters.")
    return value


def process_environment(server: dict, config_path: Path) -> dict[str, str]:
    env = baseline_environment()
    env.pop(runtime_key_name(server), None)
    env.update({k: expand(v, config_path) for k, v in server.get("env", {}).items()})
    env.update({key: credential(source) for key, source in server.get("env_from", {}).items()})
    return env


def process_spec(server: dict, config_path: Path) -> tuple[list[str], Path, dict[str, str]]:
    env = process_environment(server, config_path)
    cwd = Path(expand(server.get("cwd", str(config_path.parent)), config_path)).expanduser()
    if not cwd.is_absolute():
        cwd = config_path.parent / cwd
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise SetupError("Configured working directory does not exist.")
    # Resolve PATH in the target working directory, then give both validation
    # and exec the same absolute executable. Relative PATH entries must not be
    # resolved once in the launcher cwd and again in the target cwd.
    path_entries = []
    for entry in env.get("PATH", os.defpath).split(os.pathsep):
        path = Path(entry or ".").expanduser()
        path_entries.append(str((path if path.is_absolute() else cwd / path).resolve()))
    env["PATH"] = os.pathsep.join(path_entries)
    cmd = expand(server["command"], config_path)
    if "/" in cmd or "\\" in cmd:
        path = Path(cmd).expanduser()
        if not path.is_absolute():
            path = cwd / path
        command = str(path.resolve())
        if not Path(command).is_file() or not os.access(command, os.X_OK):
            raise SetupError("Configured executable is missing or is not executable.")
    else:
        command = shutil.which(cmd, path=env["PATH"])
        if command is None:
            raise SetupError("Configured executable was not found on PATH; install the tool or set an absolute command path.")
    return [command, *(expand(a, config_path) for a in server.get("args", []))], cwd, env


def inspect_config(config: dict) -> dict:
    return {"version": 1, "servers": {name: {
        "transport": s["transport"],
        "tunnel_configured": bool(TUNNEL_ID.fullmatch(s.get("tunnel_id", ""))),
        "required_environment": sorted(set(s.get("env_from", {}).values()) | set(s.get("headers_from_env", {}).values())),
        "runtime_key_env": s.get("runtime_key_env", "OPENAI_TUNNEL_KEY"),
        "runtime_key_file": s.get("runtime_key_file"),
        "stdio_send_initialized_notification": s.get("stdio_send_initialized_notification", False),
    } for name, s in config["servers"].items()}}


def import_mcp(input_path: Path, output_path: Path) -> dict:
    raw = read_json(input_path)
    if set(raw) != {"mcpServers"} or not isinstance(raw["mcpServers"], dict) or not raw["mcpServers"]:
        raise SetupError("Import expects an object containing only a nonempty mcpServers object; extract that section first.")
    servers = {}
    for name, original in raw["mcpServers"].items():
        if not isinstance(original, dict):
            raise SetupError("Imported MCP entries must be objects.")
        only_keys(original, {"command", "args", "env", "cwd", "url", "type", "headers"}, "imported MCP entry")
        review_import_arguments(original)
        if original.get("type") not in (None, "stdio", "http", "streamable-http"):
            raise SetupError("Legacy SSE and unknown MCP transports are not imported; supply a Streamable HTTP endpoint.")
        s = {"transport": "http" if "url" in original else "stdio", "tunnel_id": "", "runtime_key_env": "OPENAI_TUNNEL_KEY"}
        for key in ("command", "args", "url"):
            if key in original:
                s[key] = original[key]
        if s["transport"] == "stdio":
            # Preserve command-relative paths when the imported file moves.
            cwd = Path(original.get("cwd", str(input_path.parent))).expanduser()
            if not cwd.is_absolute():
                cwd = input_path.parent / cwd
            s["cwd"] = str(cwd.resolve())
        refs = {}
        for key, value in string_map(original.get("env", {}), "imported env").items():
            reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if reference:
                refs[key] = reference.group(1)
            else:
                # Import values as references even when their names do not look secret.
                # The original process environment can hold arbitrary credentials.
                refs[key] = key
        if refs:
            s["env_from"] = refs
        headers = string_map(original.get("headers", {}), "imported headers")
        if headers:
            s["headers_from_env"] = {header: "MCP_" + re.sub(r"[^A-Za-z0-9_]", "_", name).upper() + "_" + re.sub(r"[^A-Za-z0-9_]", "_", header).upper() for header in headers}
        servers[name] = s
    config = validate_config({"version": 1, "servers": servers})
    write_json(output_path, config)
    return {"written": str(output_path), "inspection": inspect_config(config), "note": "Environment and header values were converted to references. Common inline credential flags and URLs are rejected, but this is not a complete secret scanner: review command/args locally. Export referenced variables and fill in each real tunnel_id before start. Import did not execute any command."}


class StdioProbe:
    def __init__(self, argv: list[str], cwd: Path, env: dict, timeout: float):
        self.timeout = timeout
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self.messages = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _put(self, value):
        while not self.closed.is_set():
            try:
                self.messages.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not line or len(line) > MAX_MESSAGE:
                    self._put(SetupError("MCP stdout closed or exceeded the message limit."))
                    return
                if line.strip():
                    try:
                        self._put(json.loads(line))
                    except ValueError:
                        self._put(SetupError("MCP stdout must contain JSON-RPC lines only; send application logs to stderr."))
                        return
        except (OSError, ValueError):
            self._put(SetupError("Could not read MCP stdout."))

    def _drain(self):
        try:
            while not self.closed.is_set() and self.process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def send(self, payload: dict):
        try:
            self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise SetupError("MCP process rejected a protocol message.") from error

    def exchange(self, payload: dict):
        self.send(payload)
        if "id" not in payload:
            return None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError("MCP discovery timed out.")
            try:
                item = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise SetupError("MCP discovery timed out.") from error
            if isinstance(item, SetupError):
                raise item
            if not isinstance(item, dict):
                raise SetupError("MCP returned a non-object message.")
            if item.get("id") == payload["id"] and "method" not in item:
                return item
            if "id" in item and "method" in item:
                self.send({"jsonrpc": "2.0", "id": item["id"], "error": {"code": -32601, "message": "Discovery probe supports no client requests"}})

    def close(self):
        self.closed.set()
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            with contextlib.suppress(OSError):
                pipe.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise SetupError("HTTP redirect refused. Configure the exact MCP endpoint; credentials are not forwarded to another URL.")


class HttpProbe:
    def __init__(self, server: dict):
        self.url = server["url"]
        self.timeout = server.get("timeout_seconds", 15)
        self.headers = {key: credential(source) for key, source in server.get("headers_from_env", {}).items()}
        self.session = None
        self.protocol = None
        handlers = [NoRedirect()]
        if validate_url(self.url):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def exchange(self, payload: dict):
        headers = dict(self.headers, **{"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    if len(session) > 2048 or any(c in session for c in "\r\n\x00"):
                        raise SetupError("Invalid MCP HTTP session header.")
                    self.session = session
                if "id" not in payload:
                    return None
                content_type = response.headers.get_content_type()
                if content_type == "application/json":
                    raw = response.read(MAX_MESSAGE + 1)
                    if len(raw) > MAX_MESSAGE:
                        raise SetupError("MCP HTTP response exceeds the message limit.")
                    result = json.loads(raw)
                elif content_type == "text/event-stream":
                    total = 0
                    data = []
                    deadline = time.monotonic() + self.timeout
                    while True:
                        if time.monotonic() > deadline:
                            raise SetupError("MCP event stream discovery timed out.")
                        line = response.readline(MAX_MESSAGE + 1)
                        total += len(line)
                        if total > MAX_MESSAGE:
                            raise SetupError("MCP event stream exceeds the message limit.")
                        if not line:
                            raise SetupError("MCP event stream closed before the response.")
                        line = line.rstrip(b"\r\n")
                        if not line and data:
                            item = json.loads(b"\n".join(data))
                            data = []
                            if isinstance(item, dict) and item.get("id") == payload["id"]:
                                result = item
                                break
                        elif line.startswith(b"data:"):
                            data.append(line[5:].lstrip(b" "))
                else:
                    raise SetupError("Expected MCP JSON or SSE. A normal REST API/web page is not an HTTP MCP endpoint.")
        except urllib.error.HTTPError as error:
            error.close()
            raise SetupError(f"MCP HTTP discovery returned status {error.code}; check the endpoint and local authentication.") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise SetupError("MCP HTTP discovery failed; check connectivity, TLS trust, and the endpoint. No response body was printed.") from error
        if not isinstance(result, dict) or result.get("id") != payload["id"]:
            raise SetupError("MCP HTTP response ID did not match the request.")
        return result

    def close(self):
        if not self.session:
            return
        headers = dict(self.headers, **{"Mcp-Session-Id": self.session})
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        with contextlib.suppress(SetupError, urllib.error.URLError, OSError):
            with self.opener.open(urllib.request.Request(self.url, headers=headers, method="DELETE"), timeout=min(3, self.timeout)):
                pass


def _probe_direct(server: dict, config_path: Path) -> dict:
    transport = HttpProbe(server) if server["transport"] == "http" else StdioProbe(*process_spec(server, config_path), server.get("timeout_seconds", 15))
    next_id = 0
    def request(method: str, params: dict):
        nonlocal next_id
        next_id += 1
        result = transport.exchange({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params})
        if "error" in result or "result" not in result:
            raise SetupError(f"MCP {method} was rejected; remote error text was not printed.")
        return result["result"]
    try:
        init = request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "connect-local-tools-check", "version": "0.1.0"}})
        if not isinstance(init, dict) or not isinstance(init.get("protocolVersion"), str):
            raise SetupError("MCP initialize did not return a protocol version.")
        if isinstance(transport, HttpProbe):
            transport.protocol = init["protocolVersion"]
        transport.exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names, cursor = [], None
        for _ in range(20):
            result = request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise SetupError("MCP tools/list returned an invalid response.")
            for tool in result["tools"]:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise SetupError("MCP returned an invalid tool definition.")
                names.append(tool["name"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str):
                raise SetupError("MCP returned an invalid pagination cursor.")
        else:
            raise SetupError("MCP tool discovery exceeded the pagination limit.")
        return {"local_mcp_discovery": "passed", "protocol_version": init["protocolVersion"], "tool_count": len(names), "tools": names,
                "business_tools_called": False, "chatgpt_end_to_end_verified": False}
    finally:
        transport.close()


def probe(server: dict, config_path: Path) -> dict:
    if server["transport"] != "http":
        return _probe_direct(server, config_path)
    # urllib socket timeouts measure inactivity, not wall time; a trickling
    # peer can defeat them while sending headers or a JSON/SSE body. Isolate the
    # entire HTTP discovery in a killable process to enforce an overall budget,
    # including DNS, TLS, response parsing, and best-effort session cleanup.
    env = baseline_environment()
    env.pop(runtime_key_name(server), None)
    for source in server.get("headers_from_env", {}).values():
        env[source] = credential(source)
    process = subprocess.Popen([sys.executable, str(SCRIPT), "_http"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               start_new_session=True)
    try:
        stdout, _ = process.communicate(json.dumps(server).encode(), timeout=server.get("timeout_seconds", 15))
    except subprocess.TimeoutExpired as error:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise SetupError("HTTP MCP discovery exceeded its total wall-clock timeout; the probe process was stopped.") from error
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    if len(stdout) > MAX_MESSAGE:
        raise SetupError("HTTP MCP discovery result exceeds the message limit.")
    if process.returncode:
        raise SetupError("HTTP MCP discovery failed. Check the exact MCP endpoint, authentication and TLS settings; remote payloads and helper diagnostics were not printed.")
    try:
        result = json.loads(stdout)
    except ValueError as error:
        raise SetupError("HTTP discovery helper returned an invalid response.") from error
    if not isinstance(result, dict) or result.get("local_mcp_discovery") != "passed":
        raise SetupError("HTTP discovery helper did not confirm successful tool discovery.")
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_client(data_dir: Path, *, allow_download: bool = True) -> Path:
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if platform.system() != "Linux" or architecture is None:
        raise SetupError("Automatic installation supports Linux amd64/arm64. On macOS, install the official client with Homebrew and pass --client /absolute/path/tunnel-client.")
    bin_dir = data_dir / "bin"
    binary = bin_dir / f"tunnel-client-{VERSION}"
    receipt = bin_dir / f"tunnel-client-{VERSION}.json"
    expected = ARCHIVE_SHA256[architecture]
    if binary.exists() or receipt.exists():
        if not binary.is_file() or binary.is_symlink() or not receipt.is_file() or receipt.is_symlink():
            raise SetupError("Incomplete or unexpected installed client; refusing to overwrite it.")
        data = read_json(receipt)
        if data != {"version": VERSION, "architecture": architecture, "archive_sha256": expected, "binary_sha256": file_sha256(binary)} or not os.access(binary, os.X_OK):
            raise SetupError("Installed client does not match its receipt; refusing to execute or overwrite it.")
        return binary
    if not allow_download:
        raise SetupError("No managed tunnel client is installed. Run install-client, or provide --client.")
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = f"tunnel-client-{VERSION}-linux-{architecture}.zip"
    url = f"https://github.com/openai/tunnel-client/releases/download/{VERSION}/{filename}"
    with tempfile.TemporaryDirectory(prefix=".download-", dir=bin_dir) as tmp:
        archive = Path(tmp) / "client.zip"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "connect-local-tools/0.1"}), timeout=45) as response, archive.open("wb") as output:
                if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                    raise SetupError("Client download was redirected away from HTTPS.")
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_DOWNLOAD:
                        raise SetupError("Client archive exceeds the download limit.")
                    output.write(block)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SetupError("Official client download failed; check GitHub connectivity and proxy settings.") from error
        if file_sha256(archive) != expected:
            raise SetupError("Official archive SHA-256 mismatch; nothing from this archive was executed.")
        staged = Path(tmp) / "tunnel-client"
        try:
            with zipfile.ZipFile(archive) as bundle:
                entry = bundle.getinfo("tunnel-client")
                if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or not 0 < entry.file_size <= MAX_DOWNLOAD:
                    raise SetupError("Unexpected executable entry in official archive.")
                with bundle.open(entry) as source, staged.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        except (zipfile.BadZipFile, KeyError, OSError) as error:
            raise SetupError("Could not extract the verified client executable.") from error
        staged.chmod(0o755)
        binary_digest = file_sha256(staged)
        # Never overwrite a concurrent installation.
        try:
            os.link(staged, binary)
        except FileExistsError as error:
            raise SetupError("Another installation appeared; retry to verify and reuse it.") from error
        write_json(receipt, {"version": VERSION, "architecture": architecture, "archive_sha256": expected, "binary_sha256": binary_digest})
    return binary


def data_directory(argument: str | None) -> Path:
    base = Path(argument).expanduser() if argument else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "chatgpt-local-tools"
    return base.resolve()


def choose_client(argument: str | None, data_dir: Path, *, download: bool = False) -> Path:
    if argument:
        path = Path(argument).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SetupError("--client must point to an executable official tunnel-client.")
        return path
    return install_client(data_dir, allow_download=download)


def native_environment(data_dir: Path, server: dict | None = None, runtime_key: str | None = None) -> dict:
    env = baseline_environment()
    env.update({"TUNNEL_CLIENT_STATE_DIR": str(data_dir / "state"), "TUNNEL_CLIENT_PROFILE_DIR": str(data_dir / "profiles"),
                "LOG_HTTP_RAW_UNSAFE": "false", "HEALTH_LISTEN_ADDR": "127.0.0.1:0"})
    if server:
        if "stdio_send_initialized_notification" in server:
            env["MCP_STDIO_SEND_INITIALIZED_NOTIFICATION"] = str(server["stdio_send_initialized_notification"]).lower()
        for source in set(server.get("env_from", {}).values()) | set(server.get("headers_from_env", {}).values()):
            env[source] = credential(source)
        if runtime_key is not None:
            env[runtime_key_name(server)] = runtime_key
        if server.get("headers_from_env"):
            headers = ",".join(f"{header}: env:{source}" for header, source in server["headers_from_env"].items())
            env["MCP_EXTRA_HEADERS"] = headers
            env["MCP_DISCOVERY_EXTRA_HEADERS"] = headers
    return env


def native_json(binary: Path, args: list[str], environment: dict) -> dict | list:
    try:
        result = subprocess.run([str(binary), "runtimes", *args, "--json"], env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("Official runtime command failed or timed out. Use the official client diagnostics; credentials and subprocess output were not printed.") from error
    if result.returncode != 0:
        raise SetupError(f"Official runtime command exited with code {result.returncode}. Check local runtime status and the official client version; subprocess output was not printed.")
    if len(result.stdout) > MAX_MESSAGE:
        raise SetupError("Official runtime response exceeded the output limit.")
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise SetupError("Official client returned non-JSON output; verify its native runtimes command compatibility.") from error
    if not isinstance(value, (dict, list)):
        raise SetupError("Official runtime response was not an object or array.")
    return value


def safe_status(value):
    """Print operational fields only, never commands, log tails, headers or config."""
    allowed = {"alias", "tunnel_id", "process_running", "healthy", "ready", "pid", "health_url", "profile_path", "log_path", "status", "state", "control_plane_poll_health", "name", "running", "stopped", "deleted"}
    if isinstance(value, list):
        return [safe_status(item) for item in value]
    if not isinstance(value, dict):
        return value if isinstance(value, (bool, int, float)) or value is None else "[omitted]"
    result = {}
    for key, item in value.items():
        if key in allowed and isinstance(item, (str, bool, int, float, type(None))):
            result[key] = item
        elif isinstance(item, (dict, list)):
            nested = safe_status(item)
            if nested:
                result[key] = nested
    return result


def runtime_entries(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("runtimes", "items", "aliases"):
            if isinstance(value.get(key), list):
                return runtime_entries(value[key])
        if isinstance(value.get("alias"), str):
            return [value]
    raise SetupError("Unknown official runtime list format; verify the installed client version before starting anything.")


@contextlib.contextmanager
def state_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = data_dir / "launcher.lock"
    if lock.is_symlink():
        raise SetupError("Refusing a symbolic-link launcher lock.")
    with lock.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("Another launcher is changing managed runtimes; retry after it finishes.") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def runtime_authentication(server: dict, config_path: Path, data_dir: Path) -> tuple[str, dict]:
    key_name = runtime_key_name(server)
    key_file = runtime_key_path(server, config_path)
    if key_file is not None and (key_file.exists() or key_file.is_symlink()):
        validate_runtime_key_file(key_file)
        return f"file:{key_file}", native_environment(data_dir, server)
    runtime_key = os.environ.get(key_name, "")
    if not runtime_key:
        if not sys.stdin.isatty():
            raise SetupError(f"Set {key_name} locally, save the configured runtime_key_file with save-key, or use an interactive terminal for hidden entry.")
        note = "saved privately to runtime_key_file" if key_file is not None else "not saved"
        runtime_key = getpass.getpass(f"OpenAI tunnel runtime key (hidden, {note}): ").strip()
    if not runtime_key or any(c in runtime_key for c in "\r\n\x00"):
        raise SetupError("Runtime key is empty or contains unsupported control characters.")
    if key_file is not None:
        write_runtime_key(key_file, runtime_key)
        return f"file:{key_file}", native_environment(data_dir, server)
    return f"env:{key_name}", native_environment(data_dir, server, runtime_key)


def legacy_doctor(binary: Path, legacy: dict, key_reference: str, environment: dict,
                  server: dict, config_path: Path) -> None:
    profile = Path(legacy["profile"]).expanduser()
    if not profile.is_file() or profile.is_symlink():
        raise SetupError("Legacy handover needs the reviewed existing native profile file.")
    if server["transport"] != "stdio":
        raise SetupError("Legacy Python/flock handover supports stdio targets only.")
    child = shlex.join([sys.executable, str(SCRIPT), "_stdio", "--config", str(config_path), "--server", legacy["server"]])
    command = [str(binary), "doctor", "--profile-file", str(profile), "--explain",
               "--control-plane.api-key", key_reference, "--mcp.command", child,
               "--log.http-raw-unsafe=false"]
    try:
        result = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("Official doctor could not finish; the legacy runner has not been stopped.") from error
    if result.returncode:
        raise SetupError(f"Official doctor exited with code {result.returncode}; the legacy runner has not been stopped. Fix its configuration before retrying.")


def legacy_lock_owner(path: Path) -> int | None:
    info = path.stat()
    expected = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or "->" in fields:
            continue
        try:
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == expected:
                pid = int(fields[4])
                return pid if pid > 0 else None
        except (ValueError, IndexError):
            continue
    return None


def legacy_process_identity(pid: int, script: Path, python_executable: str | None = None) -> tuple:
    proc = Path("/proc") / str(pid)
    if proc.stat().st_uid != os.geteuid():
        raise SetupError("The legacy lock belongs to another user; no process was signalled.")
    interpreter = Path(python_executable or sys.executable).expanduser()
    if not os.path.samefile(proc / "exe", interpreter):
        raise SetupError("The legacy executable does not match the reviewed Python interpreter. Use --legacy-python for that reviewed interpreter or stop the old runner manually; no signal was sent.")
    executable = (proc / "exe").stat()
    argv = [os.fsdecode(value) for value in (proc / "cmdline").read_bytes().split(b"\x00") if value]
    if not argv or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name):
        raise SetupError("The legacy lock owner is not the reviewed Python runner; no process was signalled.")
    arguments = argv[1:]
    while arguments and arguments[0] in ("-u", "-B", "-I", "-s", "-S", "-E"):
        arguments = arguments[1:]
    if not arguments or arguments[0].startswith("-"):
        raise SetupError("The legacy runner arguments are unrecognized; no process was signalled.")
    target = Path(arguments[0])
    if not target.is_absolute():
        target = Path(os.readlink(proc / "cwd")) / target
    if target.resolve() != script.resolve():
        raise SetupError("A different program owns the legacy lock; no process was signalled.")
    started = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return tuple(argv), started, executable.st_dev, executable.st_ino


def stop_legacy_runner(pid: int, script: Path, python_executable: str | None = None) -> None:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise SetupError("Automatic handover requires Linux pidfd support. Stop the original runner with Ctrl+C, then use start.")
    descriptor = None
    try:
        before = legacy_process_identity(pid, script, python_executable)
        descriptor = os.pidfd_open(pid)
        if legacy_process_identity(pid, script, python_executable) != before:
            raise SetupError("The legacy process changed identity; no signal was sent.")
        signal.pidfd_send_signal(descriptor, signal.SIGINT)
    except ProcessLookupError:
        # A runner that exited by itself can release its lock normally.
        return
    except OSError as error:
        raise SetupError(f"Cannot inspect/signal the legacy runner (errno={error.errno}); stop it with Ctrl+C and use start.") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def legacy_profile_client_pids(profile: Path) -> list[int]:
    """Conservative conflict detection, never authority to signal a process.

    A released Python flock does not prove its separately detached client exited.
    Inspect only this user's client-shaped commands for the exact reviewed
    profile. Run on the client host; a sandbox inventory is not a host inventory.
    """
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        raise SetupError("Legacy client inventory requires its Linux host; stop the old runtime there before using start.")
    expected = profile.expanduser().resolve()
    conflicts = []
    def option(argv: list[str], flag: str) -> str | None:
        for index, value in enumerate(argv):
            if value.startswith(flag + "="):
                return value.split("=", 1)[1]
            if value == flag and index + 1 < len(argv):
                return argv[index + 1]
        return None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.geteuid():
                continue
            if not (proc / "comm").read_text().strip().startswith("tunnel-client"):
                continue
            argv = [os.fsdecode(x) for x in (proc / "cmdline").read_bytes().split(b"\x00") if x]
            if len(argv) < 2 or argv[1] != "run":
                continue
            direct = option(argv, "--profile-file") or option(argv, "--config")
            if direct:
                candidate = Path(direct).expanduser()
            else:
                name, directory = option(argv, "--profile"), option(argv, "--profile-dir")
                if not name or not directory:
                    continue
                candidate = Path(directory).expanduser() / (name + ".yaml")
            if not candidate.is_absolute():
                candidate = Path(os.readlink(proc / "cwd")) / candidate
            if candidate.resolve() == expected:
                conflicts.append(int(proc.name))
        except (OSError, ValueError):
            continue
    return sorted(conflicts)


def ensure_no_legacy_clients(legacy: dict) -> None:
    if not legacy.get("profile"):
        return
    conflicts = legacy_profile_client_pids(Path(legacy["profile"]))
    if conflicts:
        raise SetupError("Legacy tunnel client PID(s) " + ", ".join(map(str, conflicts)) +
                         " still use the reviewed profile, even though its Python lock is free. Stop the reviewed old client gracefully on its host, then retry; the new runtime was not started.")


@contextlib.contextmanager
def legacy_guard(legacy: dict | None):
    if legacy is None:
        yield False
        return
    path = Path(legacy["lock_file"]).expanduser()
    if not path.exists():
        if path.is_symlink():
            raise SetupError("Refusing a symbolic-link legacy lock.")
        ensure_no_legacy_clients(legacy)
        yield False
        return
    if path.is_symlink() or not path.is_file():
        raise SetupError("Legacy handover needs the reviewed regular flock file.")
    with path.open("rb") as stream:
        transferred = False
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = legacy_lock_owner(path)
            if owner is None:
                raise SetupError("The legacy runner holds its lock but is not visible here. Run handover on its host or stop the original with Ctrl+C.")
            stop_legacy_runner(owner, Path(legacy["script"]).expanduser(), legacy.get("python_executable"))
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SetupError("The original runner did not release its lock; the new runtime was not started.")
                    time.sleep(0.2)
            transferred = True
        try:
            ensure_no_legacy_clients(legacy)
            yield transferred
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def start(binary: Path, data_dir: Path, name: str, server: dict, config_path: Path,
          discovery: dict | None = None, legacy: dict | None = None) -> dict:
    tunnel_id = server.get("tunnel_id", "")
    if not TUNNEL_ID.fullmatch(tunnel_id):
        raise SetupError("Fill in a real tunnel_id created in OpenAI Platform and associated with the intended ChatGPT workspace.")
    env = native_environment(data_dir)
    public_identity = {"config_path": str(config_path), "server": name, "config_sha256": hashlib.sha256(json.dumps(server, sort_keys=True).encode()).hexdigest()}
    record_path = data_dir / "receipts" / f"{name}.json"
    expected_profile = data_dir / "profiles" / f"{name}.yaml"
    with state_lock(data_dir):
        entries = runtime_entries(native_json(binary, ["list"], env))
        same = [entry for entry in entries if entry.get("alias") == name]
        if record_path.is_symlink() or expected_profile.is_symlink():
            raise SetupError("Refusing symbolic-link runtime metadata or profile files.")
        if expected_profile.exists() and not record_path.is_file():
            raise SetupError("An unrecognized profile file already occupies this alias; refusing to overwrite it.")
        if same and not record_path.is_file():
            raise SetupError("An unmanaged runtime already uses this alias in the selected state directory; choose a new name.")
        if record_path.exists():
            prior = read_json(record_path)
            if prior.get("identity") != public_identity:
                raise SetupError("This alias belongs to a different configuration revision. Stop the old runtime and choose a new server alias before starting the revised connection.")
            profile = prior.get("profile_path")
            if profile and Path(profile).is_file() and prior.get("profile_sha256") != file_sha256(Path(profile)):
                raise SetupError("Managed profile changed outside this launcher; refusing to overwrite it.")
        for entry in entries:
            if entry.get("tunnel_id") == tunnel_id and entry.get("alias") != name:
                # Resolve liveness through status: list can include stopped aliases.
                other = native_json(binary, ["status", entry["alias"]], env)
                if contains_true(other, "process_running"):
                    raise SetupError("Another managed runtime is already using this tunnel ID. Use one active runtime per tunnel, or create a different tunnel ID.")
        if same:
            current = native_json(binary, ["status", name], env)
            if contains_true(current, "process_running"):
                return {"runtime": safe_status(current), "already_running": True, "chatgpt_end_to_end_verified": False}
        result = discovery if discovery is not None else probe(server, config_path)
        key_reference, env = runtime_authentication(server, config_path, data_dir)
        if legacy is not None:
            legacy = dict(legacy, server=name)
            legacy_doctor(binary, legacy, key_reference, env, server, config_path)
        command = ["connect", "--alias", name, "--tunnel-id", tunnel_id, "--runtime-api-key", key_reference,
                   "--profile", name, "--profile-dir", str(data_dir / "profiles")]
        if server["transport"] == "stdio":
            child = shlex.join([sys.executable, str(SCRIPT), "_stdio", "--config", str(config_path), "--server", name])
            command += ["--mcp-command", child]
        else:
            command += ["--mcp-server-url", server["url"]]
        # Record ownership before invoking native supervision. If connect times out
        # after starting a process, status/stop and a later retry still recognize it.
        intent = read_json(record_path) if record_path.exists() else {"identity": public_identity}
        write_json(record_path, intent, replace=record_path.exists())
        with legacy_guard(legacy) as transferred:
            connected = native_json(binary, command, env)
            status = native_json(binary, ["status", name], env)
            profile = find_string(status, "profile_path") or find_string(connected, "profile_path")
            receipt = {"identity": public_identity}
            if profile and Path(profile).is_file():
                receipt.update(profile_path=profile, profile_sha256=file_sha256(Path(profile)))
            write_json(record_path, receipt, replace=record_path.exists())
        return {"local_discovery": result, "runtime": safe_status(status), "legacy_runner_migrated": transferred, "chatgpt_end_to_end_verified": False,
                "next_step": "Keep the local tool available. Add this tunnel in ChatGPT, review tools, and perform one intended read-only call. Use stop to stop the native background runtime."}


def contains_true(value, key: str) -> bool:
    if isinstance(value, dict):
        return value.get(key) is True or any(contains_true(v, key) for v in value.values())
    return isinstance(value, list) and any(contains_true(v, key) for v in value)


def find_string(value, key: str) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return value[key]
        for v in value.values():
            if found := find_string(v, key):
                return found
    if isinstance(value, list):
        for v in value:
            if found := find_string(v, key):
                return found
    return None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", help="Managed data directory (default: XDG_DATA_HOME/chatgpt-local-tools)")
    p.add_argument("--client", help="Explicit path to an already installed official tunnel-client")
    commands = p.add_subparsers(dest="action", required=True)
    for action in ("inspect", "check", "start", "_stdio", "save-key", "migrate-legacy"):
        sub = commands.add_parser(action, help="Internal MCP process entry point" if action == "_stdio" else None)
        sub.add_argument("--config", required=True)
        if action != "inspect":
            sub.add_argument("--server", required=True)
        if action == "save-key":
            sub.add_argument("--from-env", help="Read a key from this local environment variable instead of hidden input")
            sub.add_argument("--replace", action="store_true", help="Deliberately replace an existing owned key file")
        if action == "migrate-legacy":
            sub.add_argument("--legacy-script", required=True, help="Reviewed original Python launcher path")
            sub.add_argument("--legacy-lock", required=True, help="Original launcher's exclusive flock file")
            sub.add_argument("--legacy-profile", required=True, help="Original native profile for official doctor")
            sub.add_argument("--legacy-python", help="Reviewed original Python interpreter; default is this launcher's interpreter")
    imported = commands.add_parser("import-mcp")
    imported.add_argument("--input", required=True)
    imported.add_argument("--output", required=True)
    commands.add_parser("_http", help="Internal isolated HTTP discovery helper")
    commands.add_parser("install-client")
    commands.add_parser("list")
    for action in ("status", "stop"):
        sub = commands.add_parser(action)
        sub.add_argument("--server", required=True)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    data_dir = data_directory(args.state_dir)
    if args.action == "_http":
        raw = sys.stdin.buffer.read(MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG:
            raise SetupError("Internal HTTP probe configuration exceeds its limit.")
        try:
            server = json.loads(raw)
        except ValueError as error:
            raise SetupError("Invalid internal HTTP probe configuration.") from error
        validate_config({"version": 1, "servers": {"probe": server}})
        if server["transport"] != "http":
            raise SetupError("Internal HTTP probe requires an HTTP MCP target.")
        say(_probe_direct(server, Path.cwd() / "unused-config.json"))
        return 0
    if args.action == "import-mcp":
        say(import_mcp(Path(args.input).expanduser().resolve(), Path(args.output).expanduser().resolve()))
        return 0
    if args.action == "install-client":
        say({"client": str(choose_client(args.client, data_dir, download=True)), "version_pin": VERSION})
        return 0
    if args.action in ("list", "status", "stop"):
        if args.action != "list" and not ALIAS.fullmatch(args.server):
            raise SetupError("Invalid server alias.")
        binary = choose_client(args.client, data_dir)
        command = [args.action] + ([args.server] if args.action != "list" else [])
        say(safe_status(native_json(binary, command, native_environment(data_dir))))
        return 0
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    if args.action == "inspect":
        say(inspect_config(config))
        return 0
    server = selected(config, args.server)
    if args.action == "save-key":
        path = runtime_key_path(server, config_path)
        if path is None:
            raise SetupError("Configure runtime_key_file before using save-key; environment-only mode does not save secrets.")
        if args.from_env:
            if not ENV_NAME.fullmatch(args.from_env):
                raise SetupError("--from-env accepts an environment variable name, never a key value.")
            value = credential(args.from_env)
        else:
            if not sys.stdin.isatty():
                raise SetupError("Use a local terminal for hidden entry, or --from-env NAME for an existing local variable.")
            value = getpass.getpass("OpenAI tunnel runtime key (hidden, saved privately): ")
        write_runtime_key(path, value, replace=args.replace)
        say({"runtime_key_saved": True, "runtime_key_file": str(path), "runtime_started": False})
        return 0
    if args.action == "_stdio":
        if server["transport"] != "stdio":
            raise SetupError("Internal stdio entry point requires a stdio target.")
        command, cwd, env = process_spec(server, config_path)
        os.chdir(cwd)
        os.execvpe(command[0], command, env)
    if args.action == "check":
        say(probe(server, config_path))
        return 0
    if not TUNNEL_ID.fullmatch(server.get("tunnel_id", "")):
        raise SetupError("Fill in a real tunnel_id before start; check can be used without a tunnel ID.")
    binary = choose_client(args.client, data_dir, download=True)
    legacy = None
    if args.action == "migrate-legacy":
        legacy = {"script": args.legacy_script, "lock_file": args.legacy_lock, "profile": args.legacy_profile,
                  "python_executable": args.legacy_python}
    say(start(binary, data_dir, args.server, server, config_path, legacy=legacy))
    return 0


if __name__ == "__main__":
    try:
        if sys.version_info < (3, 10):
            raise SetupError("Python 3.10 or newer is required.")
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled. If start already created a native runtime, inspect status and use stop explicitly.", file=sys.stderr)
        raise SystemExit(130)
    except (SetupError, EOFError) as error:
        print(f"Not completed: {error}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as error:
        print(f"Local operation failed ({type(error).__name__}); details omitted to avoid exposing subprocess data.", file=sys.stderr)
        raise SystemExit(1)
