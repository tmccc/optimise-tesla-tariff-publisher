"""Source locations and explicit user-local runtime state; no IO on import."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get(
    'OPTIMISE_TESLA_STATE_DIR',
    str(Path.home() / '.local' / 'state' / 'optimise-tesla-tariff-publisher'),
)).expanduser()


def task_env():
    """Preserve caller-selected TMPDIR; never embed a development-machine path."""
    return dict(os.environ, PYTHONDONTWRITEBYTECODE='1')


def ensure_state_dir():
    if STATE_DIR.is_symlink():
        raise ValueError('state_directory_symlink_rejected')
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not STATE_DIR.is_dir() or STATE_DIR.stat().st_uid != os.getuid():
        raise ValueError('state_directory_invalid')
    STATE_DIR.chmod(0o700)
    return STATE_DIR
