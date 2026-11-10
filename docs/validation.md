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

## Source-verified only

The official `tunnel-client v0.0.20` flags, native state JSON, header environment
format and release hashes were checked against its tagged source/documentation.
Native lifecycle tests use a local mock of that contract. KDocs examples were
checked against current official Skill/CLI documentation; vendor commands must
still be checked against the user's installed version and account.

## Not exercised in this release session

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
