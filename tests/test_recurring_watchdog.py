"""Watchdog startup, concurrency and mode dispatch require independent evidence."""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import recurring_journal
import run_guard


class RecurringWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.output=self.root/'output';self.output.mkdir(mode=0o700)
        for module,name,value in ((run_guard,'STATE_DIR',self.root),(run_guard,'OUTPUT',self.output),
            (recurring_journal,'OUTPUT',self.output)):
            self.stack.enter_context(patch.object(module,name,value))
        self.stack.enter_context(patch.object(run_guard,'live_policy_enabled',return_value=True))
        self.adapter=Mock()
        self.stack.enter_context(patch.object(run_guard,'FallbackAdapter',return_value=self.adapter))
        self.now=datetime.now(timezone.utc)
        self.adapter.enrollment_reader.return_value={'battery_enrolled':False,
            'enrolled_last_success':self.now.isoformat()}
        self.policy={'generation':'test','energy_site_id':123,'enabled':True,
            'fallback_approved':True,'manage_savings':True}
        self.keep={'backup_reserve':10,'grid_charging':True,'export_permission':'battery_ok'}
        self.write(self.root/'recurring-policy.json',self.policy)
        self.write(self.root/'execution-policy.json',{'fallback_design_approved':True})

    def write(self,path,value):
        recurring_journal.durable_private_json(path,value)

    def read(self,name):
        return recurring_journal.optional_private(self.output/name)

    def prepare_lease(self,seconds=-60):
        stamp=lambda d:(self.now+timedelta(seconds=d)).isoformat()
        self.write(self.output/'publisher-ownership.json',{'verified_publisher_write':True,
            'energy_site_id':123,'tariff_semantic_hash':'a'*64})
        self.write(self.output/'publisher-lease.json',{'guard_lease_trusted':True,
            'guard_lease_until':stamp(seconds),'last_verified_tariff_hash':'a'*64,
            'intended_tariff_hash':'a'*64,'source_last_success':stamp(-20),
            'source_captured_at':stamp(-10),'known_valid_from':stamp(-100),
            'known_valid_until':stamp(300),'tariff_semantic_hash':'a'*64,'energy_site_id':123})
        self.write(self.output/'recurring-journal.json',{'last_controlled_mode':'autonomous'})

    def snapshot(self,mode='autonomous'):
        stamp=datetime.now(timezone.utc).isoformat()
        return {'energy_site_id':123,'current_tariff_hash':'a'*64,'mode':mode,
            'mode_source':'local_gateway_TEDAPI','observed_at':stamp,'live_last_success':stamp,
            'battery_enrolled':False,'enrolled_last_success':stamp,'energy_commands_granted':True,
            'publisher_owns_installed_tariff':True,'preserve_settings':self.keep}

    def test_boot_arms_heartbeat_without_a_source_or_publisher_lease(self):
        audit=run_guard.observe_once(continuous=True)
        self.assertEqual(audit['status'],'waiting_for_publisher_state')
        health=self.read('watchdog-health.json')
        self.assertEqual(health['pid'],os.getpid())
        self.assertTrue(health['continuous'])
        self.assertTrue(health['live_execution_enabled'])
        self.assertFalse(health['publisher_lease_renewed'])
        self.adapter.read_snapshot.assert_not_called()

    def test_publisher_lock_contention_refreshes_heartbeat_without_observation(self):
        fd=os.open(self.root/'.control.lock',os.O_CREAT|os.O_WRONLY,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            audit=run_guard.observe_once(continuous=True)
        finally:os.close(fd)
        self.assertEqual(audit['status'],'controller_busy')
        self.assertTrue(self.read('watchdog-health.json')['live_execution_enabled'])
        self.adapter.enrollment_reader.assert_not_called()

    def test_current_lease_polls_enrollment_without_tesla_reads(self):
        self.prepare_lease(120)
        self.assertEqual(run_guard.observe_once(continuous=True)['status'],'publisher_lease_current')
        self.adapter.enrollment_reader.assert_called_once()
        self.adapter.read_snapshot.assert_not_called()
        self.adapter.set_self_powered.assert_not_called()

    def test_eon_activation_latches_handover_and_future_false_cannot_resume(self):
        self.prepare_lease(120)
        self.adapter.enrollment_reader.return_value={'battery_enrolled':True}
        self.assertEqual(run_guard.observe_once(continuous=True)['status'],'handover_to_optimise')
        self.adapter.enrollment_reader.return_value={'battery_enrolled':False,
            'enrolled_last_success':self.now.isoformat()}
        self.assertEqual(run_guard.observe_once(continuous=True)['status'],'persistent_pause')
        self.assertEqual(self.read('recurring-journal.json')['pause_latch']['kind'],'eon')
        self.adapter.set_self_powered.assert_not_called()

    def test_expired_lease_mode_post_owns_same_lock_and_has_durable_intent(self):
        self.prepare_lease()
        snapshots=[]
        def observe():
            value=self.snapshot('self_consumption' if len(snapshots)>=2 else 'autonomous')
            snapshots.append(value)
            return value
        self.adapter.read_snapshot.side_effect=observe
        def dispatch(**kwargs):
            self.assertTrue(kwargs['lock_held'])
            fd=os.open(self.root/'.control.lock',os.O_WRONLY)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
            context=dict(self.snapshot(),action='fallback',target_mode='self_consumption',
                from_mode='autonomous')
            self.assertTrue(recurring_journal.record_mode_attempt(context,
                controller='watchdog',policy=self.policy))
            self.assertTrue(self.read('mode-intent.json')['dispatch_started'])
            return {'command_sent':True,'preflight_verified':True,'accepted':None}
        self.adapter.set_self_powered.side_effect=dispatch
        audit=run_guard.observe_once(continuous=True)
        self.assertEqual(audit['control_requests'],1)
        self.assertTrue(audit['fallback_verified'])
        self.assertFalse((self.output/'mode-intent.json').exists())
        self.assertEqual(self.read('recurring-journal.json')['last_controlled_mode'],'self_consumption')
        self.adapter.set_self_powered.assert_called_once()

    def test_existing_uncertain_intent_never_dispatches_another_post(self):
        self.prepare_lease()
        context=dict(self.snapshot(),action='fallback',target_mode='self_consumption',from_mode='autonomous')
        self.assertTrue(recurring_journal.record_mode_attempt(context,controller='watchdog',policy=self.policy))
        self.adapter.read_snapshot.side_effect=lambda:self.snapshot()
        audit=run_guard.observe_once(continuous=True)
        self.assertEqual(audit['status'],'mode_outcome_pending')
        self.adapter.set_self_powered.assert_not_called()
        self.assertTrue(self.read('mode-intent.json'))

    def test_human_mode_change_latches_without_dispatch(self):
        self.prepare_lease()
        self.adapter.read_snapshot.side_effect=lambda:self.snapshot('self_consumption')
        self.assertEqual(run_guard.observe_once(continuous=True)['status'],'manual_change_pause')
        self.adapter.set_self_powered.assert_not_called()
        self.assertEqual(self.read('recurring-journal.json')['pause_latch']['kind'],'manual')


if __name__=='__main__':unittest.main()
