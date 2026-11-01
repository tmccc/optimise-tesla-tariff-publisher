import ast
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from runtime import FallbackAdapter,REMOTE,live_policy_enabled,private_document

UTC=timezone.utc
HASH='1'*64
KEEP={'backup_reserve':10,'grid_charging':True,'export_permission':'battery_ok'}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.policy=self.root/'policy.json';self.lease=self.root/'lease.json'
        self.calls=[]
        self.owner={'verified_publisher_write':True,'energy_site_id':123,'tariff_semantic_hash':HASH}
        self.snapshot={'energy_site_id':123,'current_tariff_hash':HASH,'mode':'autonomous',
            'energy_commands_granted':True,'preserve_settings':dict(KEEP),
            'live_last_success':datetime.now(UTC).isoformat()}
        self.enrollment={'battery_enrolled':False,'enrolled_last_success':datetime.now(UTC).isoformat()}
        self.write(self.policy,{'fallback_design_approved':True,'live_execution_enabled':False,
            'allowed_mode':'self_consumption','allow_tariff_writes':False})
        self.write(self.lease,{'guard_lease_until':(datetime.now(UTC)-timedelta(minutes=1)).isoformat(),
            'guard_lease_trusted':True,**self.owner})

    def write(self,path,value):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as handle:json.dump(value,handle)

    def arm_fake_policy(self):
        policy=private_document(self.policy);policy['live_execution_enabled']=True
        self.write(self.policy,policy)

    def transport(self,action,expected,authorized):
        self.calls.append((action,authorized))
        if action=='read':return deepcopy(self.snapshot)
        return {'command_sent':True,'preflight_verified':True}

    def adapter(self):
        return FallbackAdapter(self.owner,policy_path=self.policy,lease_path=self.lease,
            lock_path=self.root/'control.lock',transport=self.transport,
            enrollment_reader=lambda:dict(self.enrollment),
            enrollment_refresher=lambda:dict(self.enrollment),
            active_mode_reader=lambda:{'mode':self.snapshot['mode'],'mode_source':'fake_local_gateway'})

    def invoke(self,adapter=None,keep=None):
        return (adapter or self.adapter()).set_self_powered(expected_tariff_hash=HASH,
            preserve_settings=KEEP if keep is None else keep)

    def test_disabled_policy_refuses_before_auth_or_network(self):
        self.assertFalse(self.invoke()['command_sent'])
        self.assertEqual(self.calls,[])

    def test_private_policy_permissions_and_symlink_are_required(self):
        self.arm_fake_policy();self.policy.chmod(0o644)
        self.assertFalse(live_policy_enabled(self.policy))
        self.assertEqual(self.calls,[])
        link=self.root/'link';link.symlink_to(self.policy)
        self.assertFalse(live_policy_enabled(link))

    def test_missing_durable_lease_refuses_before_network(self):
        self.arm_fake_policy();self.lease.unlink()
        self.assertEqual(self.invoke()['reason'],'trusted_expired_lease_required')
        self.assertEqual(self.calls,[])

    def test_renewed_lease_prevents_fallback(self):
        self.arm_fake_policy();lease=private_document(self.lease)
        lease['guard_lease_until']=(datetime.now(UTC)+timedelta(minutes=1)).isoformat()
        self.write(self.lease,lease)
        self.assertFalse(self.invoke()['command_sent']);self.assertEqual(self.calls,[])

    def test_verified_expired_owned_state_reaches_fake_executor_only(self):
        self.arm_fake_policy()
        self.assertTrue(self.invoke()['command_sent'])
        self.assertEqual(self.calls,[('read',False),('self_powered',True)])

    def test_wrong_site_or_hash_cannot_be_treated_as_owned(self):
        self.arm_fake_policy();self.snapshot['energy_site_id']=456
        self.assertFalse(self.invoke()['command_sent'])
        self.assertEqual(self.calls,[('read',False)])

    def test_saved_cloud_default_is_not_used_as_active_mode(self):
        self.arm_fake_policy();adapter=self.adapter()
        adapter.active_mode_reader=lambda:{'mode':'self_consumption','mode_source':'fake_local_gateway'}
        snapshot=adapter.read_snapshot()
        self.assertEqual(snapshot['mode'],'self_consumption')
        self.assertEqual(snapshot['cloud_default_mode'],'autonomous')
        self.calls.clear()
        self.assertFalse(self.invoke(adapter=adapter)['command_sent'])
        self.assertEqual(self.calls,[('read',False)])

    def test_stale_tesla_readback_blocks_direct_setter(self):
        self.arm_fake_policy()
        self.snapshot['live_last_success']=(datetime.now(UTC)-timedelta(minutes=6)).isoformat()
        self.assertFalse(self.invoke()['command_sent'])
        self.assertEqual(self.calls,[('read',False)])

    def test_enrolled_or_unknown_eon_never_reaches_mutation(self):
        self.arm_fake_policy()
        for value in (True,None):
            self.enrollment['battery_enrolled']=value;self.calls.clear()
            self.assertFalse(self.invoke()['command_sent'])
            self.assertEqual(self.calls,[('read',False)])

    def test_protected_settings_change_blocks(self):
        self.arm_fake_policy();self.snapshot['preserve_settings']['backup_reserve']=20
        self.assertFalse(self.invoke()['command_sent'])
        self.assertEqual(self.calls,[('read',False)])

    def test_unknown_protected_settings_refused_before_network(self):
        self.arm_fake_policy();keep=dict(KEEP,grid_charging=None)
        self.assertFalse(self.invoke(keep=keep)['command_sent']);self.assertEqual(self.calls,[])

    def test_omitted_grid_setting_is_preserved_without_inventing_a_boolean(self):
        self.arm_fake_policy();keep=dict(KEEP,grid_charging='not_reported')
        self.snapshot['preserve_settings']=dict(keep)
        self.assertTrue(self.invoke(keep=keep)['command_sent'])
        self.assertEqual(self.calls,[('read',False),('self_powered',True)])

    def test_current_lease_or_policy_change_blocks_final_handshake(self):
        self.arm_fake_policy();adapter=self.adapter()
        adapter._pending_lease=private_document(self.lease)
        self.assertTrue(adapter._confirm_remote_preflight(self.snapshot))
        lease=private_document(self.lease);lease['guard_lease_until']=(datetime.now(UTC)+timedelta(minutes=1)).isoformat()
        self.write(self.lease,lease)
        self.assertFalse(adapter._confirm_remote_preflight(self.snapshot))

    def test_fresh_activation_or_old_refresh_blocks_handshake(self):
        self.arm_fake_policy();adapter=self.adapter()
        adapter._pending_lease=private_document(self.lease)
        self.enrollment['battery_enrolled']=True
        self.assertFalse(adapter._confirm_remote_preflight(self.snapshot))

    def test_slow_refresh_cannot_authorize_an_aged_tesla_preflight(self):
        self.arm_fake_policy();adapter=self.adapter()
        adapter._pending_lease=private_document(self.lease)
        ready=(datetime.now(UTC)-timedelta(seconds=16)).isoformat()
        self.assertFalse(adapter._confirm_remote_preflight(self.snapshot,remote_ready_at=ready))
        self.enrollment['battery_enrolled']=False
        self.enrollment['enrolled_last_success']=(datetime.now(UTC)-timedelta(seconds=31)).isoformat()
        self.assertFalse(adapter._confirm_remote_preflight(self.snapshot))

    def test_embedded_transport_code_is_valid_python(self):
        ast.parse(REMOTE)

    def test_default_read_transport_is_native_and_keeps_token_out_of_command(self):
        token=self.root/'explicit-token'
        token.write_text('SYNTHETIC_PROCESS_BOUNDARY_VALUE')
        token.chmod(0o600)
        with patch.dict(os.environ,{'TESSIE_TOKEN_FILE':str(token)}),\
            patch('runtime.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stdout='{"command_sent":false}')) as running:
            result=self.adapter()._child('read',{},False)
        self.assertFalse(result['command_sent'])
        self.assertEqual(running.call_args.args[0],[sys.executable,'-'])
        source=running.call_args.kwargs['input']
        self.assertNotIn('SYNTHETIC_PROCESS_BOUNDARY_VALUE',source)
        self.assertIn("read_secret_file('TESSIE_TOKEN_FILE')",source)
        self.assertEqual(running.call_args.kwargs['env']['TESSIE_TOKEN_FILE'],str(token))


if __name__=='__main__':unittest.main()
