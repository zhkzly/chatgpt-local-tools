# Choose the interface

An API exposes operations as network requests. A CLI exposes local commands. MCP
adds tool discovery, descriptions, parameter schemas and invocation/result semantics
for agents. A Skill teaches an agent how to choose/configure/verify these interfaces.
The tunnel makes a private MCP reachable from a supported OpenAI product.

Prefer an existing public MCP, then a private MCP through a tunnel, then a small
API/CLI adapter. A CLI may already supply `mcp serve`; check actual help first.
A vendor Skill that calls its CLI from a local shell is not automatically a remote
MCP server. An ordinary REST URL is not a Streamable HTTP MCP endpoint.

The adapter uses the main stdio MCP channel. OpenAI also provides Harpoon for
allowlisted HTTP operations; verify its channel/discovery behavior in the intended
product before selecting it. See the
[official guide](https://github.com/openai/tunnel-client/blob/v0.0.20/docs/harpoon-target-templates.md).

The host must stay available. A cloud-only skill cannot operate a laptop's localhost
without an authorized execution/connection path. Configured programs run with the
host user's permissions; input schemas and read-only hints do not sandbox them.
Tool outputs travel to ChatGPT, while this project performs no full-library upload.

Sources: [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture),
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
[official tunnel Skill](https://github.com/openai/tunnel-client/tree/v0.0.20/plugins/tunnel-mcp/skills/tunnel-mcp).
