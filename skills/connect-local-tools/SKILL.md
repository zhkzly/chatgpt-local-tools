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

Reuse the shared scripts for every connection. An existing MCP needs connection
JSON; an ordinary supported API/CLI needs adapter JSON plus connection JSON.
Do not write a new per-tool manager just to remember keys, start Node, run in the
background or manage status/stop. Write a dedicated MCP adapter only when the
existing interfaces/configuration subset cannot express the requested operation.

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

Before replacing a stdio deployment, inventory the actual client processes and
their native profiles on that host, not only the Python wrapper's lock or the
launcher's own state directory. A detached tunnel client may outlive its Python
parent after the parent releases its lock. Keep one active client per stdio
Tunnel ID and use one entry point/state directory for ordinary retries.

Have the user enter the runtime key in a hidden local prompt or protected
environment. Follow the host's existing permissions and complete authorized work
without inventing new approval gates.

Environment-only authentication remains the default. When the user requests
saved credentials, configure `runtime_key_file`, for example
`${CONFIG_DIR}/.credentials/wiki.key`. The first start can save hidden input or
the configured runtime-key environment value; later starts reuse the file.
Explain that this is a local plaintext file protected by ownership/0600
permissions, not an encrypted keychain. Use the saved-key template in `assets/`;
never put key values in JSON, command arguments or the public repository.

```bash
python3 scripts/connect.py start --config /absolute/connections.local.json --server NAME
python3 scripts/connect.py status --server NAME
python3 scripts/connect.py stop --server NAME
```

`start` uses the official managed background runtime. Closing the launching terminal
does not stop it; use `stop`. Retain the config/skill and keep the host/upstream app
available. State, profiles and logs live in the project's private data directory.
Managed background operation does not install OS startup or restart services.

Closing/reopening an upstream desktop app normally affects its local API, not
the tunnel's lifecycle. Wait for that API to return before retrying tools; do not
start a second tunnel for the same ID. Repeated unchanged `start` reuses its
managed runtime. Use `stop` then `start` for an intentional shared-launcher restart.

For a legacy caller that sends `initialize` but omits the initialized notification,
the stdio-only boolean `stdio_send_initialized_notification` enables the official
compatibility shim. It forwards the required notification after a successful
initialize and suppresses a later duplicate. It does not generate initialize or
replace duplicate-client cleanup. LLM Wiki templates opt in; other connections
keep the official default unless configured. See [configuration.md](references/configuration.md).

For deliberate key replacement, use `scripts/connect.py save-key --config CONFIG
--server NAME --replace`, then stop/start to apply it. For an existing foreground
Python/flock tunnel launcher, read [migration.md](references/migration.md) and use
the shared `migrate-legacy` command. Other supervisors need their own documented
stop procedure; automatic handover is limited to the reviewed Python/flock case.

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
