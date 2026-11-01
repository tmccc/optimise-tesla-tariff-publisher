"""Read only explicitly configured private token files; never discover secrets."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import stat


def read_secret_file(env_name: str) -> str:
    """Read the absolute token-file path named by an explicit environment key.

    The file must belong to this user, be a regular non-symlink file with mode
    0600, and contain at most 8192 UTF-8 bytes. Values, paths and underlying
    exception messages are never included in failures. This function does not
    inspect application configuration, refresh sessions, or search for tokens.
    """
    descriptor = None
    try:
        selected = os.environ.get(env_name)
        if not isinstance(selected, str) or not selected:
            raise ValueError('unavailable')
        path = Path(selected)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('unavailable')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= 8192):
            raise ValueError('unavailable')
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = None
            raw = handle.read(8193)
        if len(raw) > 8192:
            raise ValueError('unavailable')
        token = raw.decode('utf-8').strip()
        if not token or any(character.isspace() or ord(character) < 32 for character in token):
            raise ValueError('unavailable')
        return token
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ValueError('explicit_secret_file_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def secret_reader_source() -> str:
    """Pass code, never a token value, to the native HTTPS child process."""
    return 'import os,stat\nfrom pathlib import Path\n' + inspect.getsource(read_secret_file)
