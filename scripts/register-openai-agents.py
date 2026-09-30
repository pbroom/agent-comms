#!/usr/bin/env python3
"""Register distinct identities without printing tokens or rotating existing credentials."""
import os
from pathlib import Path
from agent_comms.config import Settings, create_agent, read_agents, hash_token

settings = Settings.load()
secret_dir = Path.home() / '.config' / 'agent-comms'
secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
for name, runtime in [('codex', 'codex-cli'), ('chatgpt', 'chatgpt')]:
    specs = read_agents(settings.agents_path)
    target = secret_dir / f'{name}.token'
    if name in specs:
        token = os.environ.get(f'AGENT_COMMS_{name.upper()}_TOKEN')
        if token is None and target.is_file():
            token = target.read_text().strip()
        if not token or hash_token(token) != specs[name].token_sha256:
            raise SystemExit(f'{name}: already registered; supply its existing token. No rotation performed.')
        print(f'{name}: existing identity verified')
        continue
    if target.exists() or target.is_symlink():
        raise SystemExit(f'Refusing to overwrite {target}')
    token = create_agent(settings.agents_path, name, runtime)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(token + '\n')
    print(f'{name}: registered; private token saved to {target}')
