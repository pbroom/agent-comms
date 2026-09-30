#!/usr/bin/env python3
"""Serve the normal loopback board with the ChatGPT protocol in MCP initialization."""
import os
from pathlib import Path
import uvicorn
from agent_comms.api import create_app
from agent_comms.config import Settings

# Keep the canonical board independent of the code checkout or current directory.
os.environ.setdefault('AGENT_COMMS_HOME', str(Path.home() / 'agent-comms'))
protocol = Path(__file__).with_name('INSTRUCTIONS.md').read_text()
# This listener remains local. The tunnel gateway exposes only its /mcp route.
uvicorn.run(create_app(settings=Settings.load(), mcp_instructions=protocol),
            host='127.0.0.1', port=8787, log_level='warning')
