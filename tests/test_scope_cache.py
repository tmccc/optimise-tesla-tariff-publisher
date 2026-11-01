"""Permission caching uses synthetic tokens and private temporary files only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scope_cache import (ScopeCacheError,cached_scope_reply,cached_scope_reply_async,
                        scope_cache_source,scope_token_identity)

NOW=datetime(2026,11,1,18,tzinfo=timezone.utc)


class ScopeCacheTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.token=self.root/'synthetic-token'
        self.token.write_text('PRIVATE_SYNTHETIC_TOKEN');self.token.chmod(0o600)
        self.cache=self.root/'private-cache'/'scopes.json';self.fetches=0

    def fetch(self):
        self.fetches+=1
        return {'scopes':['energy_cmds','energy_device_data','vehicle_cmds'],
            'message':'PRIVATE_PROVIDER_MESSAGE','access_token':'PRIVATE_RETURNED_TOKEN'}

    def call(self,fetch=None,**kwargs):
        return cached_scope_reply(fetch or self.fetch,cache_path=self.cache,
            token_file=self.token,now=kwargs.pop('now',NOW),**kwargs)

    def test_opt_in_is_required_and_unset_environment_never_writes_cache(self):
        with patch.dict(os.environ,{'TESSIE_SCOPE_CACHE_FILE':''}):
            for _ in range(2):
                self.assertEqual(cached_scope_reply(self.fetch),{'scopes':['energy_cmds','energy_device_data']})
        self.assertEqual(self.fetches,2);self.assertFalse(self.cache.parent.exists())

    def test_first_lookup_is_shared_until_exact_five_minute_expiry(self):
        first=self.call();self.assertEqual(first,{'scopes':['energy_cmds','energy_device_data']})
        self.assertEqual(self.call(now=NOW+timedelta(seconds=299.999)),first)
        self.assertEqual(self.fetches,1)
        self.call(now=NOW+timedelta(seconds=300));self.assertEqual(self.fetches,2)
        self.assertEqual(self.cache.stat().st_mode&0o777,0o600)
        self.assertEqual(self.cache.with_name(self.cache.name+'.lock').stat().st_mode&0o777,0o600)
        value=json.loads(self.cache.read_text())
        self.assertEqual(set(value),{'schema_version','observed_at','token_file_identity','scopes'})
        for private in ('PRIVATE_SYNTHETIC_TOKEN','PRIVATE_PROVIDER_MESSAGE','PRIVATE_RETURNED_TOKEN','vehicle_cmds'):
            self.assertNotIn(private,self.cache.read_text())

    def test_token_replacement_invalidates_cached_permission_grants(self):
        self.call();old=scope_token_identity(self.token)
        replacement=self.root/'new-synthetic-token'
        replacement.write_text('DIFFERENT_PRIVATE_TOKEN');replacement.chmod(0o600)
        replacement.replace(self.token)
        self.assertNotEqual(scope_token_identity(self.token),old)
        self.call();self.assertEqual(self.fetches,2)

    def test_in_place_token_edit_also_invalidates_identity(self):
        self.call();old=scope_token_identity(self.token)
        self.token.write_text('CHANGED_TOKEN_OF_DIFFERENT_LENGTH')
        self.assertNotEqual(scope_token_identity(self.token),old)
        self.call();self.assertEqual(self.fetches,2)

    def test_expected_token_identity_is_required_to_remain_consistent(self):
        old=scope_token_identity(self.token);self.token.write_text('CHANGED')
        with self.assertRaises(ScopeCacheError):self.call(expected_token_identity=old)
        self.assertEqual(self.fetches,0)

    def test_token_change_while_fetching_never_returns_or_stores_grant(self):
        def changed():
            self.token.write_text('CHANGED_DURING_FETCH')
            return {'scopes':['energy_cmds']}
        with self.assertRaises(ScopeCacheError):self.call(changed)
        self.assertFalse(self.cache.exists())

    def test_expired_cache_is_never_used_on_lookup_failure(self):
        self.call()
        def fail():raise ValueError('fixed_synthetic_lookup_failure')
        with self.assertRaises(ValueError):self.call(fail,now=NOW+timedelta(seconds=300))

    def test_revocation_and_empty_permission_list_replace_old_grants(self):
        self.call()
        result=self.call(lambda:{'scopes':[]},now=NOW+timedelta(seconds=300))
        self.assertEqual(result,{'scopes':[]})
        self.assertEqual(self.call(now=NOW+timedelta(seconds=301)),{'scopes':[]})

    def test_invalid_fresh_reply_never_creates_cache(self):
        for invalid in ({},None,{'scopes':'energy_cmds'},{'scopes':[True]}):
            with self.subTest(invalid=invalid),self.assertRaises(ScopeCacheError):
                self.call(lambda:invalid)
        self.assertFalse(self.cache.exists())

    def test_future_or_invalid_owned_cache_requires_new_lookup(self):
        self.call();value=json.loads(self.cache.read_text())
        for stamp in ((NOW+timedelta(seconds=1)).isoformat(),'not_a_timestamp'):
            value['observed_at']=stamp;self.cache.write_text(json.dumps(value))
            previous=self.fetches;self.call();self.assertEqual(self.fetches,previous+1)

    def test_cache_and_lock_require_private_regular_non_symlink_files(self):
        self.call();self.cache.chmod(0o644)
        with self.assertRaises(ScopeCacheError):self.call()
        self.cache.chmod(0o600);self.cache.unlink()
        target=self.root/'target';target.write_text('{}');target.chmod(0o600)
        self.cache.symlink_to(target)
        with self.assertRaises((ScopeCacheError,OSError)):self.call()
        self.cache.unlink();lock=self.cache.with_name(self.cache.name+'.lock');lock.chmod(0o644)
        with self.assertRaises(ScopeCacheError):self.call()

    def test_token_requires_owned_private_non_symlink_file(self):
        self.token.chmod(0o640)
        with self.assertRaises(ScopeCacheError):self.call()
        self.token.chmod(0o600);link=self.root/'token-link';link.symlink_to(self.token)
        with self.assertRaises((ScopeCacheError,OSError)):scope_token_identity(link)
        self.assertEqual(self.fetches,0)

    def test_shared_lock_allows_only_one_refresh_for_concurrent_readers(self):
        barrier=threading.Barrier(2)
        def fetch():
            self.fetches+=1;time.sleep(.08);return {'scopes':['energy_cmds']}
        def worker():barrier.wait();return self.call(fetch)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(worker) for _ in range(2)]
            results=[future.result(timeout=5) for future in futures]
        self.assertEqual(results,[{'scopes':['energy_cmds']}]*2);self.assertEqual(self.fetches,1)

    def test_async_and_sync_helpers_share_the_same_cache(self):
        self.call()
        async def unused():raise AssertionError('cached permission looked up again')
        result=asyncio.run(cached_scope_reply_async(unused,cache_path=self.cache,
            token_file=self.token,now=NOW+timedelta(seconds=1)))
        self.assertEqual(result,{'scopes':['energy_cmds','energy_device_data']})

    def test_embedded_source_has_identical_cache_contract(self):
        namespace={};exec(scope_cache_source(),namespace)
        result=namespace['cached_scope_reply'](self.fetch,cache_path=self.cache,
            token_file=self.token,now=NOW)
        self.assertEqual(result,{'scopes':['energy_cmds','energy_device_data']})
        self.assertEqual(self.call(),result);self.assertEqual(self.fetches,1)

    def test_relative_cache_path_is_rejected(self):
        with self.assertRaises(ScopeCacheError):cached_scope_reply(self.fetch,cache_path='relative.json')
        self.assertEqual(self.fetches,0)
