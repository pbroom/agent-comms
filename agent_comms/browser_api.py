"""Browser readiness endpoints use the existing identity and CSRF boundary."""
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, StrictInt
from . import browser_readiness as br


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: StrictInt | None = None
    target_url: str


class Bind(Input):
    recipient: str


class Probe(Input):
    context: dict
    evidence: dict


class Failure(Input):
    context: dict
    failure: str
    evidence: str


class Reconnect(Input):
    context: dict


class PermissionChange(Input):
    project: str
    evidence: str
    expected_epoch: StrictInt


def install(app, board, principal, sid):
    P = Depends(principal)

    @app.post('/api/requests/{post_id}/browser')
    def bind(post_id: int, body: Bind, request: Request, p=P):
        return br.bind_request(board,p,sid(p,request,body.session_id),post_id,body.recipient,body.target_url)

    @app.post('/api/browser/probe')
    def probe(body: Probe, request: Request, p=P):
        return br.report_probe(board,p,sid(p,request,body.session_id),body.target_url,body.context,body.evidence)

    @app.post('/api/browser/failure')
    def failure(body: Failure, request: Request, p=P):
        return br.report_failure(board,p,sid(p,request,body.session_id),body.target_url,body.context,body.failure,body.evidence)

    @app.post('/api/browser/reconnect')
    def reconnect(body: Reconnect, request: Request, p=P):
        return br.claim_reconnect(board,p,sid(p,request,body.session_id),body.target_url,body.context)

    @app.post('/api/browser/permission-change')
    def permission_change(body: PermissionChange, request: Request, p=P):
        return br.record_permission_change(board,p,sid(p,request,body.session_id),body.project,body.target_url,body.evidence,body.expected_epoch)

    @app.get('/api/browser/status')
    def status(request: Request, target_url: str, session_id: int | None = None, p=P):
        return br.status(board,p,sid(p,request,session_id),target_url)
