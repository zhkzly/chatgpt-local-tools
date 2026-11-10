# Official tunnel lifecycle

## Prerequisites

Follow the current [official guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
OpenAI controls availability, permissions, workspace associations and pricing.
Do not promise free service or universal eligibility based on Pro. This project
does not host/sell a tunnel and is not an OpenAI product.

Create/select a tunnel at <https://platform.openai.com/settings/organization/tunnels>
and associate it with the intended workspace. Runtime keys need Tunnels Read + Use;
management needs Read + Manage. The launcher binds an existing tunnel only.

## Lifecycle

```bash
python3 scripts/connect.py install-client
python3 scripts/connect.py start --config /absolute/connections.local.json --server NAME
python3 scripts/connect.py status --server NAME
python3 scripts/connect.py list
python3 scripts/connect.py stop --server NAME
```

`start` also installs a missing client. It delegates to official `runtimes connect`
and returns after starting a managed background process. Closing the launching
terminal does not stop the process. Use `stop`; keep the host awake and upstream
application running. Verify a real ChatGPT call after checking status.
The native client detaches its process from the launching terminal and records
process identity, health URL and log paths. This does not install a boot-time
service or an OS supervisor to restart an exited tunnel process.

The installer pins full `tunnel-client v0.0.20`, verifies SHA-256 and extracts only
the executable. Automatic install supports Linux amd64/arm64. Python 3.10+ is
required; no pip packages. Node is only needed for Node-based MCPs.

Use global `--client /absolute/tunnel-client` for an existing official executable.
Verify newer versions against actual help. For macOS, follow the official Homebrew
installation and supply that executable; this project's lifecycle tests run on
Linux. Native Windows is not supported by this launcher; a Linux/WSL host must be
able to reach the tool independently.

## State and credentials

Default root: `$XDG_DATA_HOME/chatgpt-local-tools`, or
`~/.local/share/chatgpt-local-tools`. Global `--state-dir` selects another root.
It holds the binary/checksum receipt, owned connection records, native profiles,
process state, health URLs and logs. The launcher restricts new state permissions
and refuses to overwrite inconsistent/unowned connection configuration.

Runtime key: configured `runtime_key_env`, default `OPENAI_TUNNEL_KEY`, or hidden
local input. Profiles store environment references, not key literals. Declare
tool credentials in `env_from` or `headers_from_env`; they are not inherited
implicitly. HTTP MCP header environments contain the complete header value.

The native client writes local runtime logs. Our scripts avoid secret-bearing
diagnostics, but an upstream program can print its own credentials. Inspect logs
before sharing. Shell startup, firewall, system proxy, application source and OS
startup services are not changed.

Run one active stdio client per tunnel ID. The launcher's checks cover its own
state directory, not unrelated clients on other hosts. Stop before changing or
moving the config/skill. Remove local data only after stopping aliases; cloud
tunnels, ChatGPT links and runtime keys require separate deletion/revocation.

References: [runtime flows](https://github.com/openai/tunnel-client/blob/v0.0.20/plugins/tunnel-mcp/skills/tunnel-mcp/references/runtime-flows.md),
[configuration](https://github.com/openai/tunnel-client/blob/v0.0.20/docs/configuration.md),
[pinned release](https://github.com/openai/tunnel-client/releases/tag/v0.0.20).
