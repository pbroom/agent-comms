"""Browser readiness tools. These record evidence; none operates a browser."""
from mcp.server.mcpserver import Context
from pydantic import StrictInt
from . import browser_readiness as br


def install(mcp, board, principal, session, run, warning=""):
    @mcp.tool(description='Bind the exact browser target before routing or starting a browser request. This is immutable and grants no access.' + warning)
    def board_bind_browser_request(post_id: StrictInt, recipient: str, target_url: str,
                                   session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.bind_request(board,principal(ctx),session(ctx,session_id),post_id,recipient,target_url))

    @mcp.tool(description='Begin a short-lived probe attempt in this exact context before an authorized browser observation. Does not grant permission or run a browser. Denied origins cannot begin probes.' + warning)
    def board_browser_begin_probe(target_url: str, context: dict,
                                  session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.begin_probe(board,principal(ctx),session(ctx,session_id),target_url,context))

    @mcp.tool(description='Record a successful HTTP response, rendered target identity and harmless interaction/result from this exact executing browser context. Never infer success from tool inventory or reuse another process desktop browser. Host permissions still apply.' + warning)
    def board_browser_probe(target_url: str, context: dict, evidence: dict, attempt_id: str,
                            session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.report_probe(board,principal(ctx),session(ctx,session_id),target_url,context,evidence,attempt_id))

    @mcp.tool(description='Record browser failure immediately. policy_denied/host_permission mean only that the browser or its host refused the bound origin itself (a site permission declined, a host policy blocking that origin); they write a sticky gate that blocks every launch to that origin in this project, across identities and contexts, until the human records a permission change. Never reroute or retry denied access. A browser tool refusing a local file path ("outside allowed roots") is not a browser failure: save with a bare file name and copy the file from the path the tool reports; such evidence is refused for those two kinds. Other failures invalidate readiness. This reports evidence, not authority.' + warning)
    def board_browser_failure(target_url: str, context: dict, failure: str, evidence: str,
                              session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.report_failure(board,principal(ctx),session(ctx,session_id),target_url,context,failure,evidence))

    @mcp.tool(description='Reserve one of at most two supported reconnect attempts in the same disconnected context, only within existing authorization. Does not execute reconnect. No retries for policy denial or host permission failure. Requires full fresh probe afterward.' + warning)
    def board_browser_reconnect(target_url: str, context: dict,
                                session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.claim_reconnect(board,principal(ctx),session(ctx,session_id),target_url,context))

    @mcp.tool(description='Read own browser readiness and sticky permission gate. A board gate change does not grant browser permissions; supported host permission change and a fresh full probe are necessary.' + warning)
    def board_browser_status(target_url: str, session_id: int | None = None, ctx: Context = None) -> dict:
        return run(lambda: br.status(board,principal(ctx),session(ctx,session_id),target_url))
