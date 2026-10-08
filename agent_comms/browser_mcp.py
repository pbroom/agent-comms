"""Browser readiness tools. These record evidence; none operates a browser."""
from mcp.server.mcpserver import Context
from pydantic import StrictInt
from . import browser_readiness as br


def install(mcp, board, principal, session, run):
    @mcp.tool(description='Bind the exact browser target before routing or starting a browser request. This is immutable and grants no access.')
    def board_bind_browser_request(post_id: StrictInt, recipient: str, target_url: str,
                                   session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.bind_request(board,principal(ctx),session(ctx,session_id),post_id,recipient,target_url))

    @mcp.tool(description='Begin a short-lived probe attempt in this exact context before an authorized browser observation. Does not grant permission or run a browser. Denied origins cannot begin probes.')
    def board_browser_begin_probe(target_url: str, context: dict,
                                  session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.begin_probe(board,principal(ctx),session(ctx,session_id),target_url,context))

    @mcp.tool(description='Record a successful HTTP response, rendered target identity and harmless interaction/result from this exact executing browser context. Never infer success from tool inventory or reuse another process desktop browser. Host permissions still apply.')
    def board_browser_probe(target_url: str, context: dict, evidence: dict, attempt_id: str,
                            session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.report_probe(board,principal(ctx),session(ctx,session_id),target_url,context,evidence,attempt_id))

    @mcp.tool(description='Record browser failure immediately. policy_denied/host_permission persist across identities and contexts; never reroute or retry denied access. Other failures invalidate readiness. This reports evidence, not authority.')
    def board_browser_failure(target_url: str, context: dict, failure: str, evidence: str,
                              session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.report_failure(board,principal(ctx),session(ctx,session_id),target_url,context,failure,evidence))

    @mcp.tool(description='Reserve one of at most two supported reconnect attempts in the same disconnected context, only within existing authorization. Does not execute reconnect. No retries for policy denial or host permission failure. Requires full fresh probe afterward.')
    def board_browser_reconnect(target_url: str, context: dict,
                                session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.claim_reconnect(board,principal(ctx),session(ctx,session_id),target_url,context))

    @mcp.tool(description='Read own browser readiness and sticky permission gate. A board gate change does not grant browser permissions; supported host permission change and a fresh full probe are necessary.')
    def board_browser_status(target_url: str, session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.status(board,principal(ctx),session(ctx,session_id),target_url))
