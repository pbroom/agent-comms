"""Verify instructions and Action schemas against the real server surface."""
import json
from pathlib import Path

from agent_comms.api import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_chatgpt_instructions_attached_to_mcp(env):
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    protocol = (ROOT / 'integrations/chatgpt/INSTRUCTIONS.md').read_text()
    app = create_app(env.board, mcp_instructions=protocol)

    async def initialize():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))
            headers = {"Authorization": f"Bearer {env.tokens['codex']}"}
            async with httpx.AsyncClient(transport=transport, headers=headers) as http:
                async with streamable_http_client("http://127.0.0.1:8787/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        return await session.initialize()

    result = asyncio.run(initialize())
    assert result.instructions == protocol


def test_action_schema_only_advertises_real_nonadmin_routes(env):
    action = json.loads((ROOT / 'integrations/chatgpt/openapi.json').read_text())
    actual = create_app(env.board).openapi()
    assert action['servers'][0]['url'] == 'https://board.example.invalid'
    assert len(action['paths']) == 9
    for path, methods in action['paths'].items():
        assert path in actual['paths']
        assert not any(word in path for word in ('admin','unseal','finalize','state'))
        for method, operation in methods.items():
            assert method in actual['paths'][path]
            assert operation['security'] == [{'boardBearer': []}]
            if method == 'post':
                assert operation['x-openai-isConsequential'] is True
    schemas = action['components']['schemas']
    assert 'final' not in schemas['PostIn']['properties']
    assert 'session_id' in schemas['PostIn']['required']
