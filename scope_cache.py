"""Short private cache for fixed Tesla energy permission names only.

No token value, vehicle data, tariff or protected settings enter this cache.
An expired/mismatched record is never used if a fresh permission lookup fails.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

SCOPE_CACHE_TTL_SECONDS = 300
_SCOPE_NAMES = frozenset({'energy_device_data', 'energy_cmds'})


class ScopeCacheError(ValueError):
    """Fixed errors only; no credential/provider content is retained."""


def _scope_clock(value=None):
    value=datetime.now(timezone.utc) if value is None else value
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScopeCacheError('scope_cache_clock_invalid')
    return value.astimezone(timezone.utc)


def _scope_file_info(descriptor):
    info=os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
        or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
        raise ScopeCacheError('scope_cache_private_file_required')
    return info


def scope_token_identity(token_file=None):
    """Stat the explicit private token file without reading any token bytes."""
    value=os.environ.get('TESSIE_TOKEN_FILE') if token_file is None else token_file
    if not value:raise ScopeCacheError('scope_cache_token_file_required')
    path=Path(value)
    if not path.is_absolute():raise ScopeCacheError('scope_cache_absolute_token_path_required')
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=_scope_file_info(descriptor)
        if not 1<=info.st_size<=8192:raise ScopeCacheError('scope_cache_token_file_invalid')
        return {key:getattr(info,'st_'+key) for key in
            ('dev','ino','size','mtime_ns','ctime_ns')}
    finally:os.close(descriptor)


def _scope_path(cache_path=None):
    value=os.environ.get('TESSIE_SCOPE_CACHE_FILE') if cache_path is None else cache_path
    if not value:return None
    path=Path(value)
    if not path.is_absolute():raise ScopeCacheError('scope_cache_absolute_path_required')
    return path


def _scope_directory(path):
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.stat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ScopeCacheError('scope_cache_private_directory_required')


@contextmanager
def _scope_guard(path):
    _scope_directory(path.parent)
    descriptor=os.open(path.with_name(path.name+'.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        _scope_file_info(descriptor)
        deadline=time.monotonic()+25
        while True:
            try:
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise ScopeCacheError('scope_cache_refresh_busy') from None
                time.sleep(.05)
        yield
    finally:os.close(descriptor)


def _scope_sanitized(reply):
    values=reply.get('scopes') if isinstance(reply,dict) else None
    if (not isinstance(values,list) or len(values)>128
        or any(not isinstance(value,str) or len(value)>128 for value in values)):
        raise ScopeCacheError('scope_lookup_invalid')
    return sorted(set(values)&_SCOPE_NAMES)


def _scope_load(path, identity, clock):
    try:descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:return None
    try:
        _scope_file_info(descriptor)
        with os.fdopen(descriptor,'rb',closefd=False) as handle:raw=handle.read(4097)
        if len(raw)>4096:return None
        try:
            value=json.loads(raw)
            if (not isinstance(value,dict) or set(value)!={'schema_version','observed_at','token_file_identity','scopes'}
                or value.get('schema_version')!=1 or value.get('token_file_identity')!=identity):return None
            stamp=_scope_clock(datetime.fromisoformat(value['observed_at']))
            if not 0<=(clock-stamp).total_seconds()<SCOPE_CACHE_TTL_SECONDS:return None
            scopes=_scope_sanitized(value)
            if scopes!=value['scopes']:return None
            return {'scopes':scopes}
        except (ValueError,TypeError,KeyError,OverflowError):return None
    finally:os.close(descriptor)


def _scope_store(path, identity, scopes, clock):
    # Reject unsafe pre-existing paths even when no readable cache was present.
    try:descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:pass
    else:
        try:_scope_file_info(descriptor)
        finally:os.close(descriptor)
    temporary=path.with_name('.'+path.name+'.'+secrets.token_hex(8))
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(descriptor,'w') as handle:
            json.dump({'schema_version':1,'observed_at':clock.isoformat(),
                'token_file_identity':identity,'scopes':scopes},handle,allow_nan=False)
            handle.write('\n');handle.flush();os.fsync(handle.fileno())
        if path.is_symlink():raise ScopeCacheError('scope_cache_symlink_rejected')
        os.replace(temporary,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:temporary.unlink(missing_ok=True)


def _scope_identity(token_file, expected):
    identity=scope_token_identity(token_file)
    if expected is not None and expected!=identity:
        raise ScopeCacheError('scope_token_changed')
    return identity


def cached_scope_reply(fetch, *, now=None, cache_path=None, token_file=None, expected_token_identity=None):
    """Return a private fresh permission record, or perform one fresh GET.

    ``fetch`` is a zero-argument existing GET-only transport. Its exceptions
    propagate without permitting a stale cache fallback. The cross-process lock
    serialises refreshes; subsequent readers reuse at most five-minute evidence.
    """
    path=_scope_path(cache_path)
    if path is None:return {'scopes':_scope_sanitized(fetch())}
    with _scope_guard(path):
        identity=_scope_identity(token_file,expected_token_identity)
        cached=_scope_load(path,identity,_scope_clock(now))
        if cached is not None:
            _scope_identity(token_file,identity);return cached
        scopes=_scope_sanitized(fetch())
        _scope_identity(token_file,identity)
        _scope_store(path,identity,scopes,_scope_clock(now))
        return {'scopes':scopes}


async def cached_scope_reply_async(fetch, *, now=None, cache_path=None, token_file=None, expected_token_identity=None):
    """Async native-child equivalent with identical cache/identity semantics."""
    path=_scope_path(cache_path)
    if path is None:return {'scopes':_scope_sanitized(await fetch())}
    with _scope_guard(path):
        identity=_scope_identity(token_file,expected_token_identity)
        cached=_scope_load(path,identity,_scope_clock(now))
        if cached is not None:
            _scope_identity(token_file,identity);return cached
        scopes=_scope_sanitized(await fetch())
        _scope_identity(token_file,identity)
        _scope_store(path,identity,scopes,_scope_clock(now))
        return {'scopes':scopes}


def scope_cache_source():
    """Embed this same implementation in the bounded native HTTPS children."""
    return Path(__file__).read_text()
