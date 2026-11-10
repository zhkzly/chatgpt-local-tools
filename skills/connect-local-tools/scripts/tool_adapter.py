#!/usr/bin/env python3
"""Expose explicitly configured HTTP operations and CLI argv as stdio MCP tools.

Python 3.10+, standard library only. This is a tools-only, sequential MCP server,
not a shell, a generic HTTP proxy, or an operating-system sandbox. Configuration
is trusted executable policy: review the underlying command/API and its argument
semantics. ``read_only`` is the configuration author's declaration, not an
enforced property of the downstream program. Write tools require --enable-writes.

Secrets belong in environment variables named by env_from / headers_from_env.
Never put credentials in command arguments, URLs, or configuration literals.
Output redaction covers literal secrets and their usual JSON-escaped forms; it
cannot prevent a malicious program from returning arbitrary encodings of secrets.
Tool execution requires POSIX nonblocking pipes (Linux/macOS). HTTP runs in a
bounded worker process so DNS, headers and body cannot bypass its deadline.
A deliberately
detached descendant is not an OS-sandboxed child: waiting is bounded, but review
commands so they do not create independent background services.
HTTP and CLI results are text; this adapter does not transfer binary files.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import selectors
import signal
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
MAX_LINE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_WORKER_INPUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
FORMATTER = string.Formatter()
SAFE_ENVIRONMENT = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "TZ", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "PATHEXT",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
}
SCHEMA_KEYS = {
    "type", "title", "description", "properties", "required", "additionalProperties",
    "enum", "items", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems",
}
SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


class ConfigError(Exception):
    """A configuration problem whose message never contains credential values."""


class ToolError(Exception):
    """A bounded, safe error to expose to an MCP caller."""


def reject_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def parse_json(payload: str | bytes) -> Any:
    value = json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_object)

    def check_text(item: Any) -> None:
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, dict):
            for key, child in item.items():
                check_text(key)
                check_text(child)
        elif isinstance(item, list):
            for child in item:
                check_text(child)

    check_text(value)
    return value


def is_number(value: Any) -> bool:
    return type(value) is int or isinstance(value, float) and math.isfinite(value)


def check_keys(value: dict, allowed: set[str], label: str) -> None:
    if any(key not in allowed for key in value):
        raise ConfigError(f"{label} contains an unsupported field.")


def validate_schema(schema: Any, depth: int = 0) -> None:
    if not isinstance(schema, dict) or depth > 8:
        raise ConfigError("inputSchema must use bounded object/array schemas.")
    check_keys(schema, SCHEMA_KEYS, "inputSchema")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in SCHEMA_TYPES:
        raise ConfigError("Every inputSchema node needs a supported single type.")
    for key in ("title", "description"):
        if key in schema and (not isinstance(schema[key], str) or len(schema[key]) > 4000):
            raise ConfigError("Schema title/description must be a short string.")
    fields_for_type = {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
        "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
        "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
        "boolean": set(), "null": set(),
    }
    specific = SCHEMA_KEYS - {"type", "title", "description", "enum"}
    if any(key in specific and key not in fields_for_type[kind] for key in schema):
        raise ConfigError("A schema keyword is incompatible with its type.")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 100:
            raise ConfigError("Object schema properties must be a bounded mapping.")
        if schema.get("additionalProperties") is not False:
            raise ConfigError("Every object schema must set additionalProperties to false.")
        if any(not PARAMETER.fullmatch(key) for key in properties):
            raise ConfigError("Schema property names must be identifier-like strings.")
        required = schema.get("required", [])
        if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required) or any(key not in properties for key in required)):
            raise ConfigError("Schema required must name distinct declared properties.")
        for child in properties.values():
            validate_schema(child, depth + 1)
    if kind == "array":
        if "items" not in schema:
            raise ConfigError("Array schemas require one items schema.")
        validate_schema(schema["items"], depth + 1)
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        for key in (low, high):
            if key in schema and (type(schema[key]) is not int or not 0 <= schema[key] <= MAX_LINE_BYTES):
                raise ConfigError("Length and item bounds must be nonnegative bounded integers.")
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ConfigError("Schema minimum length exceeds maximum length.")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not is_number(schema[key]):
            raise ConfigError("Numeric schema bounds must be finite numbers.")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise ConfigError("Schema minimum exceeds maximum.")
    if "enum" in schema:
        choices = schema["enum"]
        if kind in ("object", "array") or not isinstance(choices, list) or not 1 <= len(choices) <= 100:
            raise ConfigError("enum must be a nonempty bounded list of primitive values.")
        without_enum = {key: value for key, value in schema.items() if key != "enum"}
        for choice in choices:
            try:
                validate_value(choice, without_enum)
            except ToolError as error:
                raise ConfigError("An enum value does not satisfy its schema.") from error


def validate_value(value: Any, schema: dict, path: str = "arguments") -> None:
    kind = schema["type"]
    matches = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: type(value) is int,
        "number": lambda: is_number(value),
        "boolean": lambda: type(value) is bool,
        "null": lambda: value is None,
    }
    if not matches[kind]():
        raise ToolError(f"{path} has the wrong type.")
    if "enum" in schema and not any(type(value) is type(choice) and value == choice for choice in schema["enum"]):
        raise ToolError(f"{path} is not an allowed enum value.")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in properties for key in value):
            raise ToolError(f"{path} contains an undeclared field.")
        if any(key not in value for key in schema.get("required", [])):
            raise ToolError(f"{path} is missing a required field.")
        for key, child in value.items():
            validate_value(child, properties[key], f"{path}.{key}")
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise ToolError(f"{path} has an invalid number of items.")
        for child in value:
            validate_value(child, schema["items"], f"{path}[]")
    if kind == "string":
        if "\x00" in value or len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", MAX_LINE_BYTES):
            raise ToolError(f"{path} has an invalid string length or contains NUL.")
    if kind in ("integer", "number"):
        for key, invalid in (
            ("minimum", lambda x: value < x), ("maximum", lambda x: value > x),
            ("exclusiveMinimum", lambda x: value <= x), ("exclusiveMaximum", lambda x: value >= x),
        ):
            if key in schema and invalid(schema[key]):
                raise ToolError(f"{path} is outside its numeric bounds.")


def template_fields(text: str) -> set[str]:
    if not isinstance(text, str) or "\x00" in text:
        raise ConfigError("Templates must be NUL-free strings.")
    result = set()
    try:
        for _, field, spec, conversion in FORMATTER.parse(text):
            if field is not None:
                if not PARAMETER.fullmatch(field) or spec or conversion:
                    raise ConfigError("Use only simple {parameter} placeholders.")
                result.add(field)
    except ValueError as error:
        raise ConfigError("A template has invalid braces.") from error
    return result


def check_template(value: Any, properties: dict, scalar: bool = False, depth: int = 0) -> None:
    if depth > 8:
        raise ConfigError("JSON body template is too deeply nested.")
    if isinstance(value, str):
        fields = template_fields(value)
        if any(field not in properties for field in fields):
            raise ConfigError("A template references an undeclared input property.")
        if scalar and any(properties[field]["type"] in ("object", "array", "null") for field in fields):
            raise ConfigError("CLI/query templates can reference only scalar non-null properties.")
    elif isinstance(value, dict):
        for key, child in value.items():
            if "{" in key or "}" in key:
                raise ConfigError("JSON body keys must be literal strings.")
            check_template(child, properties, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_template(child, properties, depth=depth + 1)
    elif value is not None and type(value) not in (int, float, bool):
        raise ConfigError("Unsupported JSON body template value.")


def scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)) or value is None:
        raise ToolError("A text template needs a scalar argument.")
    return str(value)


def render(value: Any, arguments: dict, preserve_type: bool = False) -> Any:
    if isinstance(value, str):
        fields = template_fields(value)
        if any(field not in arguments for field in fields):
            raise ToolError("A template argument is missing; provide every referenced parameter.")
        if preserve_type and len(fields) == 1:
            field = next(iter(fields))
            if value == "{" + field + "}":
                return arguments[field]
        return value.format_map({field: scalar_text(arguments[field]) for field in fields})
    if isinstance(value, list):
        return [render(child, arguments, True) for child in value]
    if isinstance(value, dict):
        return {key: render(child, arguments, True) for key, child in value.items()}
    return value


def validate_url(url: Any) -> bool:
    """Validate the operator's fixed endpoint; return whether it is loopback."""
    if not isinstance(url, str) or len(url) > 8192 or any(c in url for c in "\r\n\x00{}"):
        raise ConfigError("HTTP URL must be a fixed, bounded URL without templates.")
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError()
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError()
    except ValueError as error:
        raise ConfigError("Use HTTPS, or HTTP with a literal loopback IP; omit credentials and fragments.") from error
    return loopback


def load_config(path: Path, enable_writes: bool = False) -> dict:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_LINE_BYTES:
            raise ConfigError("Configuration exceeds 1 MiB.")
        config = parse_json(raw)
    except (OSError, ValueError, RecursionError) as error:
        raise ConfigError("Cannot read valid configuration JSON.") from error
    return validate_config(config, path, enable_writes)


def validate_config(config: Any, path: Path, enable_writes: bool = False) -> dict:
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object.")
    check_keys(config, {"version", "name", "tools"}, "Configuration")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ConfigError("Configuration version must be 1.")
    if not isinstance(config.get("name"), str) or not NAME.fullmatch(config["name"]):
        raise ConfigError("Configuration name must be a short identifier.")
    if not isinstance(config.get("tools"), list) or not 1 <= len(config["tools"]) <= 64:
        raise ConfigError("Configure between 1 and 64 explicit tools.")
    names = set()
    for tool in config["tools"]:
        if not isinstance(tool, dict):
            raise ConfigError("Each tool must be an object.")
        kind = tool.get("kind")
        common = {"name", "description", "inputSchema", "read_only", "kind", "timeout_seconds", "max_output_bytes"}
        extra = {"cli": {"command", "cwd", "env_from"},
                 "http": {"url", "method", "query", "json_body", "headers_from_env"}}
        if not isinstance(kind, str) or kind not in extra:
            raise ConfigError("Tool kind must be cli or http.")
        check_keys(tool, common | extra[kind], "Tool")
        name = tool.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise ConfigError("Tool names must be distinct short identifiers.")
        names.add(name)
        if not isinstance(tool.get("description"), str) or not 1 <= len(tool["description"]) <= 4000:
            raise ConfigError("Each tool needs a bounded description.")
        if type(tool.get("read_only")) is not bool:
            raise ConfigError("Each tool must explicitly declare read_only true or false.")
        validate_schema(tool.get("inputSchema"))
        if tool["inputSchema"]["type"] != "object":
            raise ConfigError("The top-level inputSchema must be an object.")
        props = tool["inputSchema"].get("properties", {})
        timeout = tool.get("timeout_seconds", 20)
        limit = tool.get("max_output_bytes", 65536)
        if not is_number(timeout) or not 0 < timeout <= 120:
            raise ConfigError("timeout_seconds must be greater than 0 and at most 120.")
        if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_BYTES:
            raise ConfigError("max_output_bytes must be between 1 and 1048576.")
        tool["timeout_seconds"] = timeout
        tool["max_output_bytes"] = limit
        if kind == "cli":
            command = tool.get("command")
            if (not isinstance(command, list) or not 1 <= len(command) <= 100
                    or any(not isinstance(part, str) or "\x00" in part or len(part) > 8192 for part in command)
                    or not command[0]):
                raise ConfigError("CLI command must be a bounded, nonempty argv string array.")
            if template_fields(command[0]):
                raise ConfigError("The CLI executable must be fixed.")
            if "{" in command[0] or "}" in command[0]:
                raise ConfigError("The CLI executable cannot contain template braces.")
            for part in command[1:]:
                check_template(part, props, scalar=True)
            cwd = tool.get("cwd")
            if cwd is not None:
                if not isinstance(cwd, str) or not cwd or any(char in cwd for char in "\x00{}"):
                    raise ConfigError("CLI cwd must be a fixed directory path.")
                target = Path(cwd).expanduser()
                tool["cwd"] = str(target if target.is_absolute() else path.resolve().parent / target)
            env_from = tool.get("env_from", {})
            if (not isinstance(env_from, dict) or any(not ENV_NAME.fullmatch(key)
                    or not isinstance(value, str) or not ENV_NAME.fullmatch(value) for key, value in env_from.items())):
                raise ConfigError("env_from maps environment variable names to host variable names.")
        else:
            validate_url(tool.get("url"))
            if tool.get("method") not in ("GET", "POST"):
                raise ConfigError("HTTP method must explicitly be GET or POST.")
            query = tool.get("query", {})
            if (not isinstance(query, dict) or any(not isinstance(key, str) or not key or "{" in key or "}" in key
                    or not isinstance(value, str) for key, value in query.items())):
                raise ConfigError("HTTP query must map fixed names to string templates.")
            for value in query.values():
                check_template(value, props, scalar=True)
            if "json_body" in tool:
                if tool["method"] != "POST" or not isinstance(tool["json_body"], dict):
                    raise ConfigError("json_body must be an object and is supported only with POST.")
                check_template(tool["json_body"], props)
            headers = tool.get("headers_from_env", {})
            if not isinstance(headers, dict):
                raise ConfigError("headers_from_env must be a mapping.")
            for header, reference in headers.items():
                if (not HEADER_NAME.fullmatch(header) or header.lower() in
                        {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}):
                    raise ConfigError("Unsupported HTTP header name.")
                if not isinstance(reference, dict):
                    raise ConfigError("Each header reference must be an object with env and optional prefix.")
                check_keys(reference, {"env", "prefix"}, "Header reference")
                env = reference.get("env")
                prefix = reference.get("prefix", "")
                if (not isinstance(env, str) or not ENV_NAME.fullmatch(env) or not isinstance(prefix, str)
                        or len(prefix) > 64 or any(c in prefix for c in "\r\n\x00")):
                    raise ConfigError("Invalid HTTP header environment reference.")
    config["tools"] = [tool for tool in config["tools"] if tool["read_only"] or enable_writes]
    return config


def environment_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise ToolError(f"Required environment variable {name} is not set.")
    return value


def redact(text: str, secrets: list[str]) -> str:
    # CLI/API JSON commonly escapes quotes, backslashes, newlines and Unicode.
    # Match encoded forms before raw forms to avoid partial replacement. This
    # is deliberately not advertised as arbitrary-encoding DLP or a sandbox.
    representations = set()
    for secret in secrets:
        if secret:
            representations.add(secret)
            for ensure_ascii in (True, False):
                representations.add(json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1])
    if not representations:
        return text
    pattern = "|".join(re.escape(value) for value in sorted(representations, key=len, reverse=True))
    return re.sub(pattern, "[redacted]", text)


def stop_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def run_process(command: list[str], environment: dict[str, str], *, timeout: float,
                output_limit: int, label: str, cwd: str | None = None,
                input_bytes: bytes | None = None) -> bytes:
    """Run and capture one POSIX process without reader-thread close races."""
    if os.name != "posix":
        raise ToolError("Tool execution currently requires a POSIX host (Linux or macOS).")
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, cwd=cwd, env=environment,
                                   stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False, start_new_session=True, bufsize=0)
    except (OSError, ValueError) as error:
        raise ToolError(f"Cannot start {label}; check its installation, cwd and permissions.") from error
    output = bytearray()
    try:
        # No buffered reader threads: a descendant can inherit a pipe and leave
        # it open after the direct child exits, even after creating a new session.
        # Nonblocking os.read + select bounds waiting without a close/read lock.
        with selectors.DefaultSelector() as selector:
            for stream, limit, keep in ((process.stdout, output_limit, True),
                                        (process.stderr, MAX_STDERR_BYTES, False)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ,
                                  {"kind": "read", "limit": limit, "keep": keep, "count": 0})
            if input_bytes is not None:
                if input_bytes:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, {"kind": "write", "offset": 0})
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError(f"{label} operation timed out.")
                for key, _ in selector.select(remaining):
                    state = key.data
                    if state["kind"] == "write":
                        try:
                            written = os.write(key.fd, input_bytes[state["offset"]:state["offset"] + 8192])
                        except BlockingIOError:
                            continue
                        except OSError as error:
                            raise ToolError(f"{label} closed its input before receiving the request.") from error
                        state["offset"] += written
                        if state["offset"] == len(input_bytes):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, min(8192, state["limit"] - state["count"] + 1))
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        raise ToolError(f"Cannot read {label} output.") from error
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    state["count"] += len(chunk)
                    if state["count"] > state["limit"]:
                        raise ToolError(f"{label} output exceeded a configured stdout/stderr limit.")
                    if state["keep"]:
                        output.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ToolError(f"{label} operation timed out.") from error
        if returncode != 0:
            raise ToolError(f"{label} operation failed with exit code {returncode}; inspect it locally.")
        return bytes(output)
    finally:
        stop_process(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


def run_cli(tool: dict, arguments: dict) -> str:
    used_fields = set().union(*(template_fields(part) for part in tool["command"][1:]))
    for field in used_fields:
        if field in arguments and scalar_text(arguments[field]).startswith("-"):
            raise ToolError("CLI argument values beginning with '-' are not allowed.")
    command = [render(part, arguments) for part in tool["command"]]
    environment = {name: value for name, value in os.environ.items() if name in SAFE_ENVIRONMENT}
    secrets = []
    for target, source in tool.get("env_from", {}).items():
        value = environment_value(source)
        environment[target] = value
        secrets.append(value)
    output = run_process(command, environment, timeout=tool["timeout_seconds"],
                         output_limit=tool["max_output_bytes"], label="CLI", cwd=tool.get("cwd"))
    return redact(output.decode("utf-8", errors="replace"), secrets)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ToolError("HTTP redirect refused; configure the final endpoint explicitly.")


def _run_http_request(tool: dict, arguments: dict) -> str:
    parsed = urllib.parse.urlsplit(tool["url"])
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, render(value, arguments)) for key, value in tool.get("query", {}).items())
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))
    if len(url) > 16384:
        raise ToolError("Generated HTTP URL exceeds the size limit.")
    headers = {"Accept": "application/json, text/plain;q=0.9", "Accept-Encoding": "identity"}
    secrets = []
    for header, reference in tool.get("headers_from_env", {}).items():
        value = environment_value(reference["env"])
        if any(char in value for char in "\r\n\x00"):
            raise ToolError("An HTTP credential contains an invalid header character.")
        headers[header] = reference.get("prefix", "") + value
        secrets.append(value)
    payload = None
    if "json_body" in tool:
        payload = json.dumps(render(tool["json_body"], arguments, True), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ToolError("HTTP JSON request exceeds 1 MiB.")
        headers["Content-Type"] = "application/json"
    handlers = [NoRedirect()]
    if validate_url(tool["url"]):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + tool["timeout_seconds"]
    try:
        request = urllib.request.Request(url, data=payload, headers=headers, method=tool["method"])
        with opener.open(request, timeout=tool["timeout_seconds"]) as response:
            output = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ToolError("HTTP operation timed out.")
                read = getattr(response, "read1", response.read)
                block = read(min(16384, tool["max_output_bytes"] + 1 - len(output)))
                if not block:
                    break
                output.extend(block)
                if len(output) > tool["max_output_bytes"]:
                    raise ToolError("HTTP response exceeded the configured output limit.")
        return redact(output.decode("utf-8", errors="replace"), secrets)
    except urllib.error.HTTPError as error:
        error.close()
        raise ToolError(f"HTTP operation failed with status {error.code}; response body withheld.") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ToolError("HTTP operation failed; check the configured endpoint, TLS, credentials and timeout locally.") from error


def run_http(tool: dict, arguments: dict) -> str:
    # Execute network I/O in a killable worker. Socket timeouts alone do not
    # bound DNS resolution or a peer trickling headers/body below that timeout.
    payload = json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_WORKER_INPUT_BYTES:
        raise ToolError("HTTP worker request exceeds its size limit.")
    environment = {name: value for name, value in os.environ.items() if name in SAFE_ENVIRONMENT}
    for reference in tool.get("headers_from_env", {}).values():
        environment[reference["env"]] = environment_value(reference["env"])
    command = [sys.executable, str(Path(__file__).resolve()), "--_http-worker"]
    # JSON can expand control characters to six bytes; never make the worker's
    # transport envelope an excuse to remove the downstream response limit.
    output = run_process(command, environment, timeout=tool["timeout_seconds"],
                         output_limit=tool["max_output_bytes"] * 6 + 2048,
                         label="HTTP", input_bytes=payload)
    try:
        result = parse_json(output)
    except (ValueError, RecursionError) as error:
        raise ToolError("HTTP worker returned an invalid response.") from error
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ToolError("HTTP worker returned an invalid response.")
    if result["ok"] and isinstance(result.get("text"), str):
        return result["text"]
    if not result["ok"] and isinstance(result.get("error"), str):
        raise ToolError(result["error"])
    raise ToolError("HTTP worker returned an incomplete response.")


def http_worker() -> int:
    """Private one-shot worker; stdin carries data, environment carries secrets."""
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ToolError("HTTP worker request exceeds its size limit.")
        message = parse_json(raw)
        if not isinstance(message, dict) or not isinstance(message.get("tool"), dict):
            raise ToolError("HTTP worker received invalid input.")
        tool = validate_config({"version": 1, "name": "http-worker", "tools": [message["tool"]]},
                               Path.cwd() / "worker.json", enable_writes=True)["tools"][0]
        if tool["kind"] != "http":
            raise ToolError("HTTP worker accepts only HTTP tools.")
        arguments = message.get("arguments")
        validate_value(arguments, tool["inputSchema"])
        result = {"ok": True, "text": _run_http_request(tool, arguments)}
    except (ToolError, ConfigError) as error:
        result = {"ok": False, "error": str(error)}
    except Exception:
        result = {"ok": False, "error": "HTTP worker failed; inspect the configured endpoint locally."}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))
    sys.stdout.flush()
    return 0


class Adapter:
    def __init__(self, config: dict):
        self.name = config["name"]
        self.tools = {tool["name"]: tool for tool in config["tools"]}
        self.initialized = False
        self.ready = False

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict | None:
        if not isinstance(request, dict):
            return self.error(None, -32600, "JSON-RPC batches and non-object requests are unsupported.")
        request_id = request.get("id")
        if request_id is not None and (type(request_id) not in (str, int) or isinstance(request_id, str) and len(request_id) > 256):
            return self.error(None, -32600, "Invalid JSON-RPC request id.")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self.error(request_id, -32600, "Invalid JSON-RPC request.")
        method = request["method"]
        params = request.get("params", {})
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "params must be an object.")
        if method == "initialize":
            protocol = params.get("protocolVersion")
            if not isinstance(protocol, str):
                return self.error(request_id, -32602, "initialize requires protocolVersion.")
            self.initialized = True
            self.ready = False
            result = {"protocolVersion": protocol if protocol in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": self.name, "version": VERSION}}
        elif method == "ping":
            result = {}
        elif method in ("tools/list", "tools/call"):
            if not self.ready:
                return self.error(request_id, -32002, "Initialize the MCP session before requesting tools.")
            if method == "tools/list":
                if params.get("cursor") is not None:
                    return self.error(request_id, -32602, "This server does not use pagination cursors.")
                result = {"tools": [{"name": tool["name"], "description": tool["description"],
                                     "inputSchema": tool["inputSchema"],
                                     "annotations": {"readOnlyHint": tool["read_only"]}}
                                    for tool in self.tools.values()]}
            else:
                name = params.get("name")
                if not isinstance(name, str) or name not in self.tools:
                    return self.error(request_id, -32602, "Unknown or disabled tool.")
                arguments = params.get("arguments", {})
                tool = self.tools[name]
                try:
                    validate_value(arguments, tool["inputSchema"])
                    text = run_cli(tool, arguments) if tool["kind"] == "cli" else run_http(tool, arguments)
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except ToolError as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
                except Exception:
                    result = {"content": [{"type": "text", "text": "Local adapter failed; inspect the configuration locally."}], "isError": True}
        else:
            return self.error(request_id, -32601, "Method not supported by this tools-only server.")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(config: dict) -> int:
    adapter = Adapter(config)
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        too_large = len(line) > MAX_LINE_BYTES
        try:
            if too_large:
                raise ValueError()
            response = adapter.handle(parse_json(line))
        except (ValueError, RecursionError):
            response = adapter.error(None, -32700, "Invalid or oversized JSON input.")
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        if too_large:
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Reviewed JSON configuration file")
    parser.add_argument("--check", action="store_true", help="Validate config without executing tools or opening connections")
    parser.add_argument("--enable-writes", action="store_true", help="Also expose explicitly configured read_only=false tools")
    parser.add_argument("--_http-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._http_worker:
        return http_worker()
    if args.config is None:
        parser.error("--config is required")
    config = load_config(args.config.expanduser(), args.enable_writes)
    if args.check:
        print(f"Configuration valid: {len(config['tools'])} tools exposed; writes {'enabled' if args.enable_writes else 'disabled'}.")
        return 0
    return serve(config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2)
    except (KeyboardInterrupt, BrokenPipeError):
        raise SystemExit(0)
