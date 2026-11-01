from contextlib import redirect_stdout
from datetime import datetime,timedelta,timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import run_guard


def write(path,value):
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as handle:json.dump(value,handle)


class RunnerTests(unittest.TestCase):
    def test_offline_never_dispatches_even_if_future_policy_is_armed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'output';output.mkdir(mode=0o700)
            now=datetime.now(timezone.utc);stamp=now.isoformat();hash_value='1'*64
            state={'source_last_success':(now-timedelta(minutes=20)).isoformat(),
                'source_captured_at':(now-timedelta(minutes=20)).isoformat(),
                'known_valid_from':(now-timedelta(hours=1)).isoformat(),
                'known_valid_until':(now+timedelta(hours=1)).isoformat(),
                'live_last_success':stamp,'battery_enrolled':False,
                'enrolled_last_success':stamp,'energy_commands_granted':True,
                'mode':'autonomous','current_tariff_hash':hash_value,
                'last_verified_tariff_hash':hash_value,'intended_tariff_hash':hash_value,
                'guard_lease_until':(now-timedelta(minutes=1)).isoformat(),
                'guard_lease_trusted':True,'publisher_owns_installed_tariff':True,
                'fallback_policy':'self_powered','fallback_approved':True,
                'watchdog_verified':True,'preserve_settings':{'backup_reserve':10,
                    'grid_charging':True,'export_permission':'battery_ok'}}
            write(output/'guard-input.json',state)
            write(output/'publisher-lease.json',state)
            write(output/'publisher-ownership.json',{'verified_publisher_write':True})
            write(root/'execution-policy.json',{'fallback_design_approved':True})
            executor=Mock()
            with patch.object(run_guard,'ROOT',root),patch.object(run_guard,'STATE_DIR',root),patch.object(run_guard,'OUTPUT',output),\
                patch.object(run_guard,'FallbackAdapter',return_value=executor),\
                patch.object(run_guard,'live_policy_enabled',return_value=True):
                audit=run_guard.observe_once(read_live=False)
            self.assertTrue(audit['offline_snapshot_used'])
            self.assertFalse(audit['live_execution_enabled'])
            self.assertEqual(audit['control_requests'],0)
            executor.read_snapshot.assert_not_called()
            executor.set_self_powered.assert_not_called()

    def test_failed_audit_does_not_claim_zero_after_an_unknown_dispatch(self):
        capture=StringIO()
        with patch.object(run_guard,'observe_once',side_effect=OSError('PRIVATE_ERROR')),redirect_stdout(capture):
            result=run_guard._run(SimpleNamespace(watch=False,offline=False))
        self.assertEqual(result,1)
        output=capture.getvalue()
        self.assertNotIn('PRIVATE_ERROR',output)
        self.assertIsNone(json.loads(output)['control_requests'])

    def test_watch_recovers_after_subprocess_timeout_without_network_or_sleep(self):
        capture=StringIO()
        timeout=subprocess.TimeoutExpired('PRIVATE_COMMAND_TOKEN',110,
            output='PRIVATE_OUTPUT',stderr='PRIVATE_ERROR')
        recovered={'status':'observe_only','live_execution_enabled':False,
            'actual_mode_observed':'autonomous','publisher_ownership_verified':False,
            'command_sent':False,'control_requests':0,'reason_codes':[],
            'guard_reason_codes':['publisher_ownership_unverified']}
        with patch.object(run_guard,'observe_once',side_effect=[timeout,recovered]) as observe,\
            patch.object(run_guard.time,'sleep',side_effect=[None,KeyboardInterrupt]) as sleep,\
            redirect_stdout(capture):
            result=run_guard._run(SimpleNamespace(watch=True,offline=False))
        self.assertEqual(result,0)
        self.assertEqual(observe.call_count,2)
        self.assertEqual(sleep.call_count,2)
        sleep.assert_called_with(60)
        output=capture.getvalue()
        self.assertNotIn('PRIVATE_',output)
        # Both the failed cycle and the successful subsequent observation are
        # reported. A timeout never claims zero control requests for that cycle.
        decoder=json.JSONDecoder()
        first,position=decoder.raw_decode(output)
        second,_position=decoder.raw_decode(output[position:].lstrip())
        self.assertEqual(first['status'],'observation_failed')
        self.assertIsNone(first['control_requests'])
        self.assertEqual(second['status'],'observe_only')
        self.assertEqual(second['control_requests'],0)

    def test_single_observation_reports_subprocess_failure_privately(self):
        capture=StringIO()
        failure=subprocess.CalledProcessError(1,'PRIVATE_COMMAND',stderr='PRIVATE_ERROR')
        with patch.object(run_guard,'observe_once',side_effect=failure),redirect_stdout(capture):
            result=run_guard._run(SimpleNamespace(watch=False,offline=False))
        self.assertEqual(result,1)
        self.assertNotIn('PRIVATE_',capture.getvalue())
        self.assertIsNone(json.loads(capture.getvalue())['control_requests'])


if __name__=='__main__':unittest.main()
