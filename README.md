# ChatGPT Local Tools

[中文说明](README.zh-CN.md) · [Agent Skill](skills/connect-local-tools/SKILL.md) · [Configuration](skills/connect-local-tools/references/configuration.md)

An Agent Skill and small Python toolkit for connecting local knowledge bases and
tools to ChatGPT. Reuse an existing MCP server, or adapt a few fixed HTTP API/CLI
operations into MCP, then use OpenAI's official Secure MCP Tunnel for reachability.

**This is an independent integration project.** It does not host a tunnel, create
OpenAI accounts, bypass subscription/workspace restrictions, or promise free OpenAI
service. It is a GitHub-distributed skill, not a published ChatGPT App Directory app.

## What is included

| Component | Responsibility |
| --- | --- |
| `skills/connect-local-tools/SKILL.md` | Teach a local agent how to identify the interface, choose a route, configure and verify it. |
| `scripts/connect.py` inside the skill | Import `mcpServers`, check stdio/HTTP MCP, install a checked official client, start/status/stop managed connections. |
| `scripts/tool_adapter.py` inside the skill | Expose explicitly configured HTTP operations or fixed CLI argv as named MCP tools. |
| Skill `assets/` | LLM Wiki, HTTP MCP, personal KDocs, WPS365, REST and harmless local demo templates. |
| `tests/` | Offline protocol, process, HTTP, lifecycle-contract and credential-boundary tests. |

The skill tells an agent **how** to work. API/CLI adapters expose **what** can be
called. MCP supplies tool discovery and invocation. The tunnel supplies the
network path. Installing a skill alone does not give ChatGPT access to a laptop.

## Requirements and support

- Python 3.10+; both scripts use the standard library only.
- Run on a host that can reach the local tool. Linux is the tested launcher platform.
- Automatic official-client download supports Linux amd64/arm64 and pins
  `tunnel-client v0.0.20` with SHA-256 verification.
- Other POSIX hosts may supply an already installed official client using
  `--client /absolute/path`; macOS users should use the official Homebrew route.
  Native Windows is not supported by the launcher.
- The intended tool/CLI must already be installed/authenticated. Node is needed
  only for a Node-based MCP. This project does not install arbitrary vendor tools.
- OpenAI must grant your account/workspace the relevant custom MCP/tunnel access.
  Check current [official requirements](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## Quick start: no account or cloud connection needed

```bash
git clone https://github.com/zhkzly/chatgpt-local-tools.git
cd chatgpt-local-tools
python3 skills/connect-local-tools/scripts/tool_adapter.py \
  --config skills/connect-local-tools/assets/demo.adapter.json --check
python3 skills/connect-local-tools/scripts/connect.py check \
  --config skills/connect-local-tools/assets/demo.connection.json --server demo
python3 skills/connect-local-tools/scripts/smoke_demo.py
```

The first two commands validate configuration and perform local MCP discovery.
The final command actually calls the harmless `echo_text` tool through the
launcher's stdio wrapper and checks its exact result. None starts a tunnel;
this demonstration does not verify a ChatGPT connection.

## Install the Agent Skill

Copy the **whole folder**, not just `SKILL.md`. For Codex's user skill directory:

```bash
mkdir -p ~/.agents/skills
cp -R skills/connect-local-tools ~/.agents/skills/
```

Review/back up an existing installation before replacing it. For a repository-local
skill, use `.agents/skills/`; for Claude Code, use its `.claude/skills/` directory.
Other agents that support the Agent Skills folder format can use the same content,
but each must have local execution and the relevant account access.

Example request:

> Use $connect-local-tools to connect my local LLM Wiki to ChatGPT. Inspect the
> existing MCP config, show the exposed tools, check the local server, and verify
> one read-only ChatGPT call after the tunnel is ready.

Do not paste credentials into the agent conversation. Installation of the skill
does not automatically install or authorize a ChatGPT connection.

## Connect LLM Wiki

Keep LLM Wiki running with its local API and MCP enabled. Copy the template to a
private config, fill the real tunnel ID, and retain the same local config path:

```bash
cp -n skills/connect-local-tools/assets/llm-wiki.connection.json connections.local.json
python3 skills/connect-local-tools/scripts/connect.py check \
  --config connections.local.json --server llm-wiki
```

Create/select an eligible tunnel in [OpenAI Platform](https://platform.openai.com/settings/organization/tunnels)
and associate it with your ChatGPT workspace. Edit only your private copy's
`tunnel_id`. The runtime key needs Tunnels Read + Use. Then:

```bash
python3 skills/connect-local-tools/scripts/connect.py start \
  --config connections.local.json --server llm-wiki
python3 skills/connect-local-tools/scripts/connect.py status --server llm-wiki
```

If `OPENAI_TUNNEL_KEY` is absent, `start` prompts for it with hidden terminal input.
It installs a missing official client, then starts the official **background**
runtime. Closing this terminal is not a stop command.

Add a custom MCP connection in ChatGPT using that tunnel, review the discovered
tools and make one authorized read call. Local `ready` is not end-to-end proof.
The original LLM Wiki MCP includes write/action tools; this launcher passes the
original tool set through and is not a read-only filter.

Stop the connection with:

```bash
python3 skills/connect-local-tools/scripts/connect.py stop --server llm-wiki
```

## Use an existing MCP config, REST API, or CLI

Import the common desktop-client JSON shape without executing it:

```bash
python3 skills/connect-local-tools/scripts/connect.py import-mcp \
  --input /absolute/mcp.json --output connections.local.json
python3 skills/connect-local-tools/scripts/connect.py inspect --config connections.local.json
```

Imported environment/header values become references; supply those values locally
and review argv before execution. See [configuration](skills/connect-local-tools/references/configuration.md).

For a REST API or ordinary CLI, start from `assets/api-readonly.adapter.json` or
`assets/kdocs-readonly.adapter.json`. Define the fixed endpoint/method or executable
and argv, bounded inputs, timeouts and output limits. Then point a stdio connection
at `tool_adapter.py --config /absolute/adapter.json`. See
[adapter details](skills/connect-local-tools/references/adapters.md).

Prefer an existing MCP endpoint or native `mcp serve` mode over adding an adapter.
Ordinary REST and old standalone SSE are not Streamable HTTP MCP endpoints.

## Personal KDocs / 金山文档

Personal KDocs has an [official CLI and Skill](https://github.com/kdocs-app/kdocs-skill).
The bundled example uses that CLI's local login/keychain and exposes search,
personal-root listing, star listing, document read and pending-read polling.
It never exposes login or token export as tools.

After following the official installation instructions and inspecting its help:

```bash
kdocs-cli auth login
kdocs-cli auth status
python3 skills/connect-local-tools/scripts/connect.py check \
  --config skills/connect-local-tools/assets/kdocs.connection.json --server kdocs
```

The example limits listings to an initial page and preserves pending/partial-read
results. It has not been tested against a real user's KDocs account. WPS365 is a
separate enterprise product and already offers `wps365-cli mcp serve`. See the
[provider notes and sources](skills/connect-local-tools/references/kdocs.md).

## Scope, data and state

- Default local state: `${XDG_DATA_HOME:-~/.local/share}/chatgpt-local-tools`.
  It contains the official binary/checksum receipt, profiles, alias/process state,
  ownership records and local logs. Use global `--state-dir` before the subcommand
  for another root. No OS startup service, firewall change or shell-config edit.
- Credentials use explicit environment references. Native runtime logs can still
  contain anything a third-party program prints; inspect before sharing.
- Tool results travel to ChatGPT. This project does not upload an entire knowledge
  base, sync cloud drives or transfer downloaded binaries as ChatGPT attachments.
- The adapter defaults to declared read-only tools and rejects hidden write calls.
  `--enable-writes` explicitly enables configured write tools. Declarations are
  not an OS sandbox and cannot prove a downstream command has no side effects.
- Adapter schemas deliberately support a subset; unsupported keywords fail.
  Calls are sequential, text-only and timeout-bounded; no async MCP task extension.
- Keep one active stdio client per tunnel ID. Checks cover this state directory,
  not separately managed runtimes. Stop before changing a config; this initial
  version requires a new alias for a changed configuration.
- Stop first before deleting local data. Cloud tunnel, ChatGPT link and API-key
  revocation are separate operations. Moving/deleting the skill or config can
  break a running stdio connection.

## Validation and contributing

```bash
python3 -m unittest discover -s tests -v
```

Tests use local fixtures and a mock official CLI. They verify implementation
boundaries, not live OpenAI access or billing. See [validation](docs/validation.md)
for what was and was not exercised, and [CONTRIBUTING.md](CONTRIBUTING.md).

## License and upstream projects

MIT for original code and documentation in this repository. The separately
downloaded official tunnel client and vendor tools retain their own licenses.
See [THIRD_PARTY.md](THIRD_PARTY.md). No vendor Skill or binary is redistributed.
