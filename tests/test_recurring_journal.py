"""Private durable mode intents prevent replay after process interruption."""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recurring_journal as records


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.output=self.root/'output'
        self.stack.enter_context(patch.object(records,'OUTPUT',self.output))
        self.now=datetime.now(timezone.utc)
        self.policy={'generation':'test','energy_site_id':123,'enabled':True,
            'fallback_approved':True,'manage_savings':True}
        self.keep={'backup_reserve':10,'grid_charging':True,'export_permission':'battery_ok'}
        self.context={'action':'fallback','target_mode':'self_consumption','from_mode':'autonomous',
            'energy_site_id':123,'current_tariff_hash':'a'*64,'preserve_settings':self.keep,
            'battery_enrolled':False,'enrolled_last_success':self.now.isoformat()}

    def record(self):
        return records.record_mode_attempt(self.context,controller='watchdog',policy=self.policy)

    def snapshot(self):
        stamp=datetime.now(timezone.utc).isoformat()
        return {'energy_site_id':123,'current_tariff_hash':'a'*64,'mode':'self_consumption',
            'mode_source':'local_gateway_TEDAPI','observed_at':stamp,'live_last_success':stamp,
            'battery_enrolled':False,'enrolled_last_success':stamp,'energy_commands_granted':True,
            'preserve_settings':self.keep}

    def test_intent_is_private_durable_and_cannot_be_overwritten(self):
        self.assertTrue(self.record())
        path=self.output/'mode-intent.json'
        before=path.read_bytes()
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertFalse(self.record())
        self.assertEqual(before,path.read_bytes())
        value=json.loads(before)
        self.assertIsNone(value['command_sent'])
        self.assertTrue(value['durable'])

    def test_uncertain_dispatch_needs_matching_actual_observation(self):
        self.assertTrue(self.record())
        result=records.complete_mode_attempt({'command_sent':None,'accepted':None,
            'private_provider_reply':'DO_NOT_SAVE'})
        self.assertTrue(result['pending'])
        self.assertNotIn('DO_NOT_SAVE',(self.output/'mode-intent.json').read_text())
        snapshot=self.snapshot();snapshot['mode']='autonomous'
        self.assertTrue(records.reconcile_mode(snapshot,policy=self.policy,
            now=datetime.now(timezone.utc))['pending'])
        result=records.reconcile_mode(self.snapshot(),policy=self.policy,now=datetime.now(timezone.utc))
        self.assertTrue(result['verified'])
        self.assertFalse(result['pending'])
        self.assertFalse((self.output/'mode-intent.json').exists())
        journal=records.optional_private(self.output/'recurring-journal.json')
        self.assertEqual(journal['last_controlled_mode'],'self_consumption')
        self.assertFalse(journal['last_mode_action']['application_acknowledgment_known'])

    def test_definite_abort_clears_intent_without_adopting_mode(self):
        self.assertTrue(self.record())
        self.assertFalse(records.complete_mode_attempt({'command_sent':False})['pending'])
        self.assertFalse((self.output/'recurring-journal.json').exists())

    def test_explicit_rejection_latches_pause_without_adopting_human_mode(self):
        self.assertTrue(self.record())
        records.complete_mode_attempt({'command_sent':True,'accepted':False})
        result=records.reconcile_mode(self.snapshot(),policy=self.policy,now=datetime.now(timezone.utc))
        self.assertTrue(result['rejected'])
        journal=records.optional_private(self.output/'recurring-journal.json')
        self.assertEqual(journal['pause_latch']['kind'],'manual')
        self.assertNotIn('last_controlled_mode',journal)
        self.assertFalse(self.record())

    def test_stale_enrollment_and_missing_approval_block_intent(self):
        self.context['enrolled_last_success']=(self.now-timedelta(minutes=1)).isoformat()
        self.assertFalse(self.record())
        self.context['enrolled_last_success']=self.now.isoformat()
        self.policy['fallback_approved']=False
        self.assertFalse(self.record())
        self.assertFalse((self.output/'mode-intent.json').exists())

    def test_existing_pause_is_preserved_on_verified_outcome(self):
        self.assertTrue(self.record())
        latch={'kind':'eon','reason':'optimise_battery_enrolled'}
        records.durable_private_json(self.output/'recurring-journal.json',{'pause_latch':latch})
        self.assertTrue(records.reconcile_mode(self.snapshot(),policy=self.policy,
            now=datetime.now(timezone.utc))['verified'])
        self.assertEqual(records.optional_private(self.output/'recurring-journal.json')['pause_latch'],latch)


if __name__=='__main__':unittest.main()
