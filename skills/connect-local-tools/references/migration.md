# Reuse the shared launcher for an existing connection

Do not create another Python manager for each tool. Keep a connection JSON per
MCP and optional adapter JSON for ordinary APIs/CLIs. Use the same `connect.py`
commands for configuration checks, saved credentials and runtime lifecycle.

## Existing Tunnel ID and credentials

Inspect the user's existing local profile/records and reuse a suitable authorized
Tunnel ID and checked official client. Do not create another cloud tunnel merely
because the local launcher changes. Keep private IDs/configs/keys out of public
source. A former `env:NAME` reference does not mean the key was saved: it may only
exist in the old process. Do not promise to recover a key that was never persisted
or extract it from process environments. Use an existing authorized local variable
or one hidden local entry.

When the user requests persistence, configure `runtime_key_file`. The shared
`start`/`save-key` commands handle it; see [configuration.md](configuration.md).
An already running managed alias should be reused. Do not modify its connection
JSON or installed Skill in place without planning a stopped migration.

## Reviewed foreground Python/flock launcher

This optional Linux handover applies to the earlier `connect_llm_wiki.py` pattern
and other reviewed Python launchers using an exclusive `flock` file. The original
Python launcher is different from its Node MCP child. Identify the exact original
script path, its lock file and its native YAML profile from local evidence.

Validate the new connection and perform the intended local read-only business
check before handover. For LLM Wiki, verify its desktop API and MCP switches and
`llm_wiki_status`; tool discovery alone does not validate the desktop API.

```bash
python3 scripts/connect.py --client /absolute/tunnel-client migrate-legacy \
  --config /absolute/connections.local.json --server wiki \
  --legacy-script /absolute/original_launcher.py \
  --legacy-lock /absolute/original.lock \
  --legacy-profile /absolute/original-profile.yaml
```

The shared command checks the new local MCP and obtains the configured credential
before stopping anything. It runs official `doctor` against the reviewed profile,
overriding the key reference and MCP command with the clean new stdio wrapper.
`doctor` validates configuration/local conditions; it does not authenticate a
hosted control-plane poll or prove a successful ChatGPT call.

For a held lock, handover checks its actual kernel owner, same-user ownership,
Python executable, exact script path and process start identity, then sends
SIGINT through Linux pidfd. It waits up to 20 seconds for the old runner to release
the lock and holds that lock during native startup. Missing credentials, failed
local discovery/doctor or an unrecognized owner leave the old process untouched.
It does not force-kill unknown processes. With the old process stopped, ordinary
native startup can still fail; inspect `status` and logs rather than claiming
automatic rollback or end-to-end success.

If the original process is hidden by a sandbox, run handover on that host. For
another supervisor, unsupported Python invocation or missing pidfd support, use
its documented stop procedure and then the normal `start` command. Keep only one
active stdio runtime per Tunnel ID. Once migrated, future restarts use `start`
without the legacy arguments.

## Startup versus operating-system services

The official native runtime continues after its launching terminal closes. Node
MCP children are started automatically from connection `command`/`args`; do not
start another independent copy manually. Neither `start` nor handover installs a
systemd service, boot-time startup or an OS crash-restart supervisor. Configure
those separately only when requested. The host and upstream desktop tool must
remain available. Complete one real ChatGPT read call before reporting the full
connection verified.
