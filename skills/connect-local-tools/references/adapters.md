# API and CLI adapters

## Commands and contract

```bash
python3 scripts/tool_adapter.py --config adapter.json --check
python3 scripts/tool_adapter.py --config adapter.json
```

`--check` validates without executing commands or HTTP calls. Ordinary mode is a
sequential, tools-only stdio MCP server. Actual CLI/API execution requires POSIX
(Linux/macOS) in this release; native Windows is not supported. Configure the tunnel launcher to run it;
do not run it in the background with ordinary shell redirection.

Root JSON fields: `version: 1`, `name`, `tools`. Each tool has `name`, `description`,
`kind` (`cli` or `http`), `read_only`, `inputSchema`, and optional
`timeout_seconds`/`max_output_bytes`. Unknown fields are rejected.

## CLI operations

Use a fixed argv array, for example:

```json
{
  "name": "kdocs_search",
  "description": "Search accessible personal KDocs files by a query.",
  "kind": "cli",
  "read_only": true,
  "command": ["kdocs-cli", "drive", "search-files", "--keyword", "{query}", "--type", "all", "--page-size", "10"],
  "inputSchema": {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "required": ["query"],
    "additionalProperties": false
  }
}
```

Optional `cwd` is relative to the adapter config directory unless absolute.
`env_from` maps child environment names to host environment names. Ordinary
system/proxy/CA/keyring variables are retained; other credentials are not inherited.

The executable cannot be a template. Remaining argv elements may contain simple
`{parameter}` references. There is no shell evaluation, property access or format
expression execution. Every referenced parameter must be provided. Dynamic values
starting with `-` are rejected, including negative numbers. This conservative
restriction reduces option injection but does not validate every CLI's semantics.
Static `python -c`/shell/program choices are trusted configuration, not a sandbox.

The adapter returns bounded stdout as text. Nonzero exit, timeout and oversized
output return errors without echoing stderr. A CLI may return business-error JSON
with exit 0: the calling agent must interpret its documented schema. Optional flags
are best represented as separate named tools in this initial version.

## HTTP operations

Use fixed `url` and `method` (GET/POST). URL/path templates are intentionally not
supported. Put parameters in the `query` map or POST `json_body` object. Example:

```json
{
  "url": "http://127.0.0.1:9000/search",
  "method": "GET",
  "query": {"q": "{query}"},
  "headers_from_env": {"Authorization": {"env": "NOTES_TOKEN", "prefix": "Bearer "}}
}
```

Headers are explicit environment references; the prefix is optional. HTTP is only
allowed for literal loopback IPs; other endpoints require HTTPS. Redirects are
rejected. Loopback requests avoid external proxies; other requests use the host's
normal proxy/CA configuration. TLS verification stays enabled.

An exact `"{parameter}"` value in `json_body` preserves the JSON type; a number stays
a number. GET bodies and arbitrary header overrides are rejected. Responses are
bounded UTF-8 text, not a binary transfer channel. Non-2xx responses produce errors
without leaking their body. Successful JSON may still encode a business error.

## Schemas, writes and limits

Use a single `type` per node; all object schemas must set
`additionalProperties: false`. Supported: basic JSON types, properties/required,
array items, primitive enums, string/array lengths, numeric bounds, titles and
descriptions. `$ref`, `pattern`, `anyOf`, defaults and other unsupported keywords
are rejected. For complex schemas or broad protocol features, use a dedicated
server with an official MCP SDK.

Default mode exposes only `read_only: true` tools and rejects direct calls to hidden
write tools. `--enable-writes` includes explicitly configured write tools; use only
when the user's task authorizes those operations. `read_only` remains the author's
declaration, not enforcement of downstream behavior. Existing MCPs passed directly
through the launcher do not receive this filtering.

The adapter supports initialization-based MCP versions 2024-11-05, 2025-03-26 and
2025-06-18. It does not implement resources, prompts, sampling, async MCP tasks or
in-flight cancellation. Calls are sequential. A parent-process deadline covers CLI execution and the
HTTP worker (including DNS, connection, headers and body). Cleanup is bounded, but
this is not an OS sandbox for malicious commands or deliberately detached descendants.
Known credential redaction covers literal/common JSON-escaped forms, not arbitrary
encodings or all sensitive data; do not rely on it as a data-loss prevention system.
Local fixture tests do not establish live vendor/ChatGPT compatibility.
