---
name: connect-local-tools
description: Connect local tools and knowledge bases to ChatGPT through MCP. Use when a user wants to expose an existing stdio or HTTP MCP server, adapt an HTTP API or authenticated CLI into named MCP tools, import mcpServers configuration, or diagnose a private connection. Includes LLM Wiki and personal KDocs CLI examples. Requires an execution environment that can reach the tool; a skill alone does not grant computer, account, or network access.
---

# Connect Local Tools

Run on the computer that can reach the user's tool. Reuse the official OpenAI
tunnel client for transport/lifecycle. Adapt APIs or CLIs only when no suitable
MCP server already exists.

## 1. Identify the interface and execution host

Read supplied configuration, official documentation and installed CLI help.
Identify product, account type, executable/URL, authentication and intended
operations. Distinguish cloud localhost from the user's computer.

| Interface | Route |
| --- | --- |
| Supported public MCP | Connect directly when possible. |
| Local stdio MCP, including `mcp serve` | Preserve argv, cwd and explicit environment references. |
| Streamable HTTP MCP | Use the MCP endpoint, not the web UI or REST root. |
| Ordinary API | Wrap fixed operations with `scripts/tool_adapter.py`. |
| Ordinary CLI | Prefer native MCP mode, otherwise wrap fixed executable/subcommands. |
| GUI only | Report the missing programmatic interface; do not invent one. |

Read [interface-selection.md](references/interface-selection.md) and the relevant
provider notes, including [kdocs.md](references/kdocs.md).

## 2. Prepare a local configuration

Read [configuration.md](references/configuration.md). Use templates in `assets/`.
Use Python 3.10+. Resolve script paths relative to this skill. Keep the complete
skill and local config available while a runtime uses them; generated commands
refer to absolute paths. Stop the runtime before moving these files.

```bash
python3 scripts/connect.py import-mcp --input /absolute/mcp.json --output /absolute/connections.local.json
python3 scripts/connect.py inspect --config /absolute/connections.local.json
```

Import does not execute commands. Inspect them before running. Import removes
original environment values and literal authentication headers in favor of
references; restore required values locally, never in chat or public code.

For APIs/CLIs, read [adapters.md](references/adapters.md). Run the adapter's
`--check` first; it validates configuration without executing business operations.
Expose a narrow, named tool set. Do not expose arbitrary shell/executable/URL
selection, authentication commands or a directory broader than requested.
`read_only` is an author declaration, not an OS sandbox or proof of no side effects.

## 3. Verify local MCP discovery

```bash
python3 scripts/connect.py check --config /absolute/connections.local.json --server NAME
```

This executes the MCP server, initializes it and lists tools, but calls no business
tools. Check the discovered operations against scope. Existing MCP tools are passed
through unfiltered. Server startup can itself have side effects.

For a credential-free local tool-call demonstration, run:

```bash
python3 scripts/smoke_demo.py
```

This uses the bundled [demo connection](assets/demo.connection.json) and
[CLI adapter](assets/demo.adapter.json), performs initialization/discovery, then
calls `echo_text` through the launcher's stdio wrapper and checks the exact result.
It verifies a local tool call; it does not establish a tunnel or a ChatGPT connection.

Check authentication through the tool's documented login/status flow. For personal
KDocs use `kdocs-cli auth login` and `kdocs-cli auth status` locally. Do not extract
browser cookies or expose commands that print credentials.

## 4. Establish the official tunnel

Read [tunnel.md](references/tunnel.md). Inspect/reuse suitable authorized resources.
Use a separate alias and tunnel ID per independent server. Verify current account
access, workspace association and pricing; do not infer them from a plan name.
The launcher binds an existing tunnel and does not request an admin key.

Have the user enter the runtime key in a hidden local prompt or protected
environment. Follow the host's existing permissions and complete authorized work
without inventing new approval gates.

```bash
python3 scripts/connect.py start --config /absolute/connections.local.json --server NAME
python3 scripts/connect.py status --server NAME
python3 scripts/connect.py stop --server NAME
```

`start` uses the official managed background runtime. Closing the launching terminal
does not stop it; use `stop`. Retain the config/skill and keep the host/upstream app
available. State, profiles and logs live in the project's private data directory.
Managed background operation does not install OS startup or restart services.

Add the ChatGPT custom MCP connection when authorized and browser access permits.
Otherwise deliver the exact remaining UI step. Never bypass a browser/account
policy block. Publishing this skill on GitHub does not publish an App Directory app.

## 5. Verify and report the actual result

Keep these levels distinct:

1. Configuration validated.
2. Local MCP initialized and tools discovered.
3. Native runtime running and locally healthy/ready.
4. Cloud polling working, when observed.
5. ChatGPT discovered the tools and completed one authorized narrow read operation.

Only level 5 verifies the requested end-to-end integration. Fixture tests and local
readiness are not substitutes. Preserve API/CLI business errors, pagination,
asynchronous pending results and partial-read warnings. A zero process exit or
MCP `isError=false` alone is not proof of business success.

Report installations, files written, tools exposed, data sent on calls, stop/removal
instructions and the exact verification level. Use
[troubleshooting.md](references/troubleshooting.md) for concrete failures.
