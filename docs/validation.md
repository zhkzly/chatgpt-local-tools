# Validation record

Initial release validation: **2026-11-10**, Linux, Python 3.12.

## Executed

`python3 -m unittest discover -s tests -v` completed with **38 passing tests**:
21 launcher tests and 17 adapter tests. The repository snapshot, not a separate
implementation copy, was tested. Both scripts use only the Python standard library.

Coverage includes real local stdio subprocesses, MCP initialize/list/call flows,
HTTP JSON/SSE/session/authentication behavior, redirect refusal, command argument
boundaries, explicit credentials, import handling, checksums, profile ownership,
native lifecycle contracts, duplicate tunnel protection and idempotent start.

An independent review produced additional regressions for:

- Hard deadlines across slowly arriving HTTP headers/body/SSE.
- CLI timeout cleanup when a detached descendant retains pipe descriptors.
- Known credentials represented as raw or JSON-escaped text.
- Runtime-key names colliding with system/proxy environment variables.
- Relative PATH lookup using a different cwd from actual execution.
- Common inline secrets in imported argv/URLs and overly broad import claims.

The fixes and corresponding regressions are included. This is bounded defensive
engineering, not a formal security audit or a sandbox for untrusted executables.

The Skill Creator validator accepted the canonical skill. All 10 bundled JSON
examples passed their corresponding configuration validators. The repository's
demo passed adapter validation and real local MCP discovery.

A separate agent followed the Skill using a private copy of the demo config. It
performed configuration validation, inspection, MCP discovery and an actual local
`echo_text` tool call through the launcher stdio wrapper. The returned text matched
the input. No credentials were supplied and no tunnel was started.

## Source-verified only during the initial release

The official `tunnel-client v0.0.20` flags, native state JSON, header environment
format and release hashes were checked against its tagged source/documentation.
Native lifecycle tests use a local mock of that contract. KDocs examples were
checked against current official Skill/CLI documentation; vendor commands must
still be checked against the user's installed version and account.

## Not exercised during the initial release

- Downloading/running the real official tunnel binary or connecting to its service.
- Creating a user's tunnel, authenticating a real runtime key or installing a
  ChatGPT connection.
- A real ChatGPT discovery/tool call through a live tunnel.
- Real KDocs/WPS365 authentication or private document reads/writes.
- macOS/ARM hardware or native Windows execution.
- OpenAI account eligibility, billing or a claim that tunnel service is free.

Local success establishes the implementation's local protocol boundary. Users
still need an authorized host/account and one real ChatGPT read operation to verify
the full integration. Public GitHub availability does not change those requirements.

## Follow-up functional verification, 2026-11-10

[CI run 42810741784](https://github.com/zhkzly/chatgpt-local-tools/actions/runs/42810741784)
tested commit `9780c4b3e9526c57c4895c11eee4f081da86b4ce` on Ubuntu 24.04:

- The complete, unmodified 38-test suite passed, including the local HTTP cases.
- All 10 bundled JSON examples passed their actual configuration validators.
- The new `scripts/smoke_demo.py` completed initialize/list/call through the
  launcher's stdio wrapper and compared the CLI result exactly, without stripping
  whitespace. The example now preserves its input rather than adding a newline.
- The launcher downloaded the real pinned official client and checked its archive
  hash. No mock executable replaced it in the additional native smoke.
- The actual launcher start/status/stop functions exercised that official client
  using independent temporary state. The test redirected only the control-plane
  destination to an unreachable loopback fixture and used a dummy key/tunnel ID.
  After the launching Python process exited, a separate status process still
  observed the native runtime healthy, in its own Unix session. Stop succeeded.
- The official `dev proxy` then provided an in-memory local control plane and
  real runtime. An HTTP MCP client initialized, discovered `echo_text` and called
  it through the local tunnel, stdio wrapper, adapter and CLI. The exact returned
  text matched the input. The test cleaned its proxy process group.

The disconnected native fixture reported `ready=true`. This is an observed local
readiness value, not evidence of a successful hosted control-plane poll. The smoke
records it without treating it as cloud verification.

An independent Agent also followed the Skill from an ordinary `wc` CLI rather than
an existing MCP configuration. It created a fixed-file, read-only adapter and
connection, discovered the tool, made a real MCP call and compared its UTF-8 output
byte-for-byte against direct `wc`. An undeclared file argument was rejected. The
Agent separately ran the new credential-free demo command successfully.

A local check against an installed LLM Wiki MCP completed discovery of 12 tools.
An attempted status call through a pre-existing connected LLM Wiki plugin returned
`mcp_initialization_required` (`-32002`); no Wiki business call succeeded in this
follow-up. A local check initializes a separate MCP process and cannot repair the
cloud caller's current session. Browser control was unavailable for rediscovery.

These follow-up results establish actual local Skill use, real native background
operation and a real official local tunnel tool call. Hosted OpenAI authentication,
successful calls from ChatGPT, and real KDocs/WPS365 accounts remain unverified.

To repeat the local stages:

```bash
python3 -m unittest discover -s tests -v
python3 skills/connect-local-tools/scripts/smoke_demo.py
local_tools_test_dir="$(mktemp -d)"
python3 skills/connect-local-tools/scripts/connect.py --state-dir "$local_tools_test_dir" install-client
python3 tests/official_client_smoke.py --client "$local_tools_test_dir/bin/tunnel-client-v0.0.20"
```

The installer and native smoke download/run the official client and require ordinary
loopback socket access. The native smoke uses temporary fixture state, not the
user's existing runtime or accounts.
