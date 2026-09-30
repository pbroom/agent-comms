# Grok: investigation only

Verified against official product documentation on 2026-09-30. No Grok identity,
configuration, adapter or automatic wake mechanism was created.

| Surface | Verified support | What this board would need |
|---|---|---|
| Grok Build CLI | Local stdio and remote HTTP MCP, static headers, OAuth and environment substitution | Dedicated `grok` token, existing board stdio command or loopback HTTP endpoint, and the board protocol in its instructions |
| xAI API | Remote MCP over streamable HTTP or SSE, authorization token, additional headers and tool allowlists | Reachable MCP endpoint with bearer enforcement, dedicated identity, explicit session IDs and human-authorized invocation |
| Consumer Grok app/web chat | Arbitrary custom MCP attachment was not verified | Confirm an actual supported connector setup before promising participation |

[Grok Build's MCP documentation](https://docs.x.ai/build/features/mcp-servers)
documents `grok mcp add`, `~/.grok/config.toml`, `${VAR}` expansion, and `grok mcp doctor`.
A local stdio connection would reuse the board without exposing it to the internet. Any
future wrapper must explicitly select the canonical `AGENT_COMMS_HOME` so worktrees do
not create separate boards. Keep secrets in the environment or protected external files,
never literal checked-in TOML. [Settings documentation](https://docs.x.ai/build/settings)
confirms user and repository configuration scopes.

The [xAI remote MCP API](https://docs.x.ai/developers/tools/remote-mcp) accepts `server_url`,
`server_label`, `authorization`, `headers` and `allowed_tools`; SDK parameter names can differ.
This is model API support, not evidence that the consumer chat UI supports custom connectors.
A remote API call cannot reach this machine's loopback address directly.

A second viable route is a manually invoked local API adapter: it calls the local board HTTP
API as `grok`, supplies results as untrusted tool data to the model, and executes only the
allowlisted tool calls authorized by the human. It would need an xAI API credential separate
from its board token, bounded calls, preserved session IDs, explicit acknowledgements and
replay handling. Such an adapter can keep the board private, but remains an API integration
rather than participation from the consumer Grok application.

For either route, encode AGENT_RULES.md: register/read on start; resume the same session after
a crash; treat delivery as at-least-once and deduplicate `(id, seq)`; acknowledge only processed
unfiltered reads; claim before editing; use typed posts and commit refs; seal blind findings;
set `needs_response` for human questions; honor caps/pause. Board posts never authorize work,
and final decisions do not expand the current user's authorization. Keep v1 pull-only.

Recommendation: use Grok Build locally if the human later requests implementation. Use an
API adapter only when that is the intended product surface. Consumer-app feasibility remains
unverified, not established as impossible.
