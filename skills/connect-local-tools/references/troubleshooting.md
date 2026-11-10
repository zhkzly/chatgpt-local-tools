# Diagnose the failed layer

| Observation | Next action |
| --- | --- |
| Configuration rejected | Fix unsupported fields/types; use environment references for credentials. |
| Executable missing | Install the intended tool from its official source on the correct host. |
| MCP stdout is not JSON | Check its real MCP mode; ordinary CLI output is not MCP. |
| HTTP returns HTML/404 | Check the MCP URL rather than a web UI or REST root. |
| HTTP 401/403 | Check the intended account and explicit authentication; stop unauthorized operations. |
| Missing imported environment | Import removes original values; supply referenced values locally. |
| Local MCP passes, tunnel fails | Check key scopes, tunnel association, network/proxy/CA and official diagnostics. |
| Local ready, ChatGPT fails | Check cloud polling, workspace association and actual discovery/authentication. |
| `mcp_initialization_required` / `-32002` | Have the calling MCP client initialize and rediscover the current server. Check for an MCP child restart or overlapping runtimes on the same tunnel; neither is established by this error alone. A separate local preflight initializes a separate process, not the live cloud session. |
| Alias/config conflict | Inspect status/list and stop the old runtime before changing targets. |
| Same tunnel ID running | Reuse/stop it or use a separate ID for an independent server. |
| KDocs login stalls | Confirm personal versus enterprise account and use official login. |
| KDocs returns pending | Poll with the returned task ID and original file locator. |
| CLI returns error JSON with exit 0 | Honor its business error, not merely process exit status. |
| Unsupported adapter schema | Use the documented subset or build a dedicated SDK MCP server. |
| Output exceeds bound | Add upstream paging/narrow the operation; do not call partial output complete. |

Keep TLS verification and authentication enabled. Do not bypass denied browser or
network policies. Keep credentials and document contents out of public issues.
