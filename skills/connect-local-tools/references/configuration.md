# Connection configuration

## Commands

Run from this skill folder, or use an absolute script path:

```bash
python3 scripts/connect.py inspect --config connections.local.json
python3 scripts/connect.py check --config connections.local.json --server wiki
python3 scripts/connect.py start --config connections.local.json --server wiki
python3 scripts/connect.py status --server wiki
python3 scripts/connect.py stop --server wiki
python3 scripts/connect.py list
```

Global options precede the subcommand: `--state-dir /private/path` and
`--client /absolute/tunnel-client`. `inspect` does not execute commands; `check`
does execute the configured server and performs MCP discovery. For a new connection, `start` performs discovery before connecting. It may first
prepare the official client to inspect existing runtime state. An unchanged running
alias is reused without starting a second discovery process.

## JSON format

```json
{
  "version": 1,
  "servers": {
    "wiki": {
      "transport": "stdio",
      "command": "node",
      "args": ["/usr/lib/LLM Wiki/mcp-server/dist/src/index.js"],
      "env": {"LLM_WIKI_API_BASE_URL": "http://127.0.0.1:19828"},
      "tunnel_id": "",
      "runtime_key_env": "OPENAI_TUNNEL_KEY"
    }
  }
}
```

Leave the tunnel ID empty for local checks. Before `start`, put the real ID from
OpenAI Platform in this private local config. The runtime key is prompted locally
if its environment variable is absent. Never use a key as the tunnel ID.

| Field | Meaning |
| --- | --- |
| `transport` | `stdio` or `http` (Streamable HTTP MCP). |
| `command`, `args` | Fixed executable and argv for stdio. No shell command string. |
| `cwd` | Optional working directory; defaults to the config file's directory. |
| `env` | Explicit non-secret environment literals for the tool. |
| `env_from` | Tool environment name to host environment name. |
| `url` | Fixed HTTP MCP endpoint, for HTTP transport. |
| `headers_from_env` | HTTP header name to host environment name containing the full value. |
| `runtime_key_env` | Host environment containing the tunnel key; default `OPENAI_TUNNEL_KEY`. |
| `tunnel_id` | Existing associated OpenAI tunnel; empty is allowed until start. |
| `timeout_seconds` | Bounded local discovery timeout. |

Only `${CONFIG_DIR}` and `${SKILL_DIR}` are expanded in supported stdio paths and
arguments. They refer to the config's directory and the installed skill directory.
Do not assume general shell/environment expansion. The config is executable policy:
review it before `check` or `start`. For HTTP, use HTTPS or literal-loopback HTTP;
legacy standalone SSE URLs are not supported. Authentication headers are applied to
both discovery and runtime calls.

Example credential binding:

```json
{"env_from": {"MY_TOOL_TOKEN": "MY_TOOL_TOKEN"}}
```

The launcher preserves a small common environment (PATH/HOME, proxy/CA, locale and
desktop keyring context) and only explicitly referenced credentials. An API/CLI
adapter introduces a second process boundary: add a secret to the connection's
`env_from` AND the adapter's `env_from`/`headers_from_env` if both need to receive it.
Do not pass the tunnel key to the upstream tool.

## Import an existing MCP config

```bash
python3 scripts/connect.py import-mcp --input /absolute/mcp.json --output connections.local.json
```

Supports the common `mcpServers` command/args/env/cwd or URL/header shape. All
environment values become environment references, even values that do not look
like credentials. Literal header values also become references. Inspect the
reported variable names and set them locally. Unsupported fields/transports are
rejected instead of silently discarded. Output is private and not overwritten.

## Changes while running

The launcher detects configuration changes for an owned alias. Stop it before
reconfiguring. In this initial release, use a new server alias for a changed
configuration; an unchanged owned alias can be restarted. Do not edit generated native profiles; the official client may
regenerate them. Keep one active instance per stdio tunnel ID. Existing MCP tools
are not filtered by this configuration.
