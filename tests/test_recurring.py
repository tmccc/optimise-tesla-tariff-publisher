"""Coordinator tests use private temporary journals and fake external transports."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from prepare import private_json
from runtime import private_document
from tests.test_recurring_state import NOW, OLD, NEW, KEEP, fixture, own_fallback, stamp


def load_coordinator():
    # The disk-journal implementation has its own focused tests. Inject only
    # that boundary here, including when the real module is already installed.
    journal=ModuleType('recurring_journal')
    journal.optional_private=lambda path:private_document(path) if Path(path).exists() else {}
    journal.record_mode_attempt=lambda *args,**kwargs:True
    journal.complete_mode_attempt=lambda result:None
    journal.reconcile_mode=lambda *args,**kwargs:{'pending':False}
    source=Path(__file__).resolve().parents[1]/'recurring.py'
    spec=importlib.util.spec_from_file_location('_recurring_coordinator_test',source)
    module=importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules',{'recurring_journal':journal}):spec.loader.exec_module(module)
    return module


class RecurringCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.module=load_coordinator()
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name);self.output=self.root/'output';self.output.mkdir()
        self.clock=NOW;self.posts=[];self.target=NEW;self.snapshots=[]
        self.state=fixture()
        self.state['active_mode_observation']={'hardware_preserve_settings':dict(KEEP)}
        for name,value in {'STATE_DIR':self.root,'OUTPUT':self.output,
                'POLICY':self.root/'recurring-policy.json','JOURNAL':self.output/'recurring-journal.json',
                'PENDING':self.output/'tariff-pending.json'}.items():
            setattr(self.module,name,value)
        self.module.now=lambda:self.clock
        self.module.watchdog_health=lambda:deepcopy(self.state['watchdog'])
        self.module.live_policy_enabled=lambda:True
        self.module.read_active_mode=lambda:{'mode':self.state['mode'],
            'mode_source':'local_gateway_TEDAPI','hardware_preserve_settings':dict(KEEP)}
        private_json(self.module.POLICY,self.state['policy'])
        private_json(self.module.JOURNAL,self.state['journal'])
        private_json(self.root/'publisher-policy.json',{'enabled':False})

    def observe(self):
        self.clock+=timedelta(seconds=1)
        result={key:deepcopy(self.state[key]) for key in ('energy_site_id','preserve_settings',
            'mode','mode_source','battery_enrolled','enrolled_last_success','energy_commands_granted')}
        result.update(current_tariff_hash=self.target,
            live_last_success=self.clock.isoformat(),observed_at=self.clock.isoformat())
        self.snapshots.append(result);return result

    def adapter_factory(self):return SimpleNamespace(read_snapshot=self.observe)

    def writer_factory(self, *, final_authorizer):
        def publish(candidate,**kwargs):
            persisted=private_document(self.module.PENDING)
            self.assertTrue(persisted['authorized']);self.assertIsNone(persisted['command_sent'])
            self.assertEqual(persisted['baseline_hash'],OLD);self.assertEqual(persisted['candidate_hash'],NEW)
            policy=private_document(self.root/'publisher-policy.json')
            self.assertEqual(policy['approved_candidate_semantic_hash'],NEW)
            self.assertEqual(policy['expected_installed_semantic_hash'],OLD)
            observations={key:deepcopy(self.state[key]) for key in ('energy_site_id','preserve_settings',
                'mode','battery_enrolled','enrolled_last_success','energy_commands_granted','live_last_success')}
            observations['candidate_tariff_hash']=NEW
            if not final_authorizer(observations):return {'command_sent':False}
            self.posts.append(kwargs)
            return {'command_sent':True,'write_http_status':200,'write_http_completed_at':self.clock.isoformat(),
                'write_accepted':None,'explicit_rejection':False,'reply_metadata':{'body_kind':'object','nodes':[]}}
        return SimpleNamespace(publish=publish)

    def process(self, *, writer_factory=None):
        return self.module.process(self.state,{}, {'quoted_interval_count':49},
            writer_factory=writer_factory or self.writer_factory,adapter_factory=self.adapter_factory)

    def test_http200_unknown_ack_is_committed_only_after_two_real_readbacks(self):
        result=self.process()
        self.assertEqual(result['action'],'refresh_tariff');self.assertTrue(result['publication_verified'])
        self.assertEqual(len(self.posts),1);self.assertEqual(len(self.snapshots),2)
        self.assertEqual(private_document(self.module.PENDING),{})
        owner=private_document(self.output/'publisher-ownership.json')
        lease=private_document(self.output/'publisher-lease.json')
        self.assertEqual(owner['tariff_semantic_hash'],NEW)
        self.assertFalse(owner['application_acknowledgment_known'])
        self.assertTrue(lease['guard_lease_trusted']);self.assertEqual(lease['tariff_semantic_hash'],NEW)

    def test_same_tariff_renews_without_post_and_preserves_original_write_time(self):
        self.state['candidate_tariff_hash']=OLD
        result=self.process()
        self.assertEqual(result['action'],'renew_lease');self.assertTrue(result['lease_renewed'])
        self.assertEqual(self.posts,[])
        self.assertEqual(private_document(self.output/'publisher-ownership.json')['written_at'],stamp(-3600))

    def test_stale_source_invalidates_lease_without_direct_control_request(self):
        self.state['source_last_success']=stamp(-1000)
        result=self.process()
        self.assertEqual(result['action'],'safe_fallback');self.assertEqual(result['control_requests'],0)
        lease=private_document(self.output/'publisher-lease.json')
        self.assertEqual(lease['guard_lease_until'],self.clock.isoformat());self.assertEqual(self.posts,[])

    def test_explicit_rejection_never_creates_ownership(self):
        def reject(**kwargs):
            return SimpleNamespace(publish=lambda *args,**kwargs:{'command_sent':True,
                'write_http_status':200,'write_accepted':False,'explicit_rejection':True})
        result=self.process(writer_factory=reject)
        self.assertFalse(result['publication_verified'])
        self.assertFalse((self.output/'publisher-ownership.json').exists())
        self.assertEqual(private_document(self.module.JOURNAL)['pause_latch']['kind'],'manual')

    def test_final_denial_clears_unsent_intent_without_claiming_ownership(self):
        self.module.live_policy_enabled=lambda:False
        result=self.process()
        self.assertEqual(result['control_requests'],0);self.assertEqual(self.posts,[])
        self.assertEqual(private_document(self.module.PENDING),{})
        self.assertFalse((self.output/'publisher-ownership.json').exists())

    def test_transport_crash_leaves_uncertain_intent_and_restart_never_reposts(self):
        def crash(**kwargs):
            def publish(*args,**kwargs):raise RuntimeError('synthetic lost transport result')
            return SimpleNamespace(publish=publish)
        with self.assertRaises(RuntimeError):self.process(writer_factory=crash)
        pending=private_document(self.module.PENDING)
        self.assertIsNone(pending['command_sent'])
        self.state['pending']=pending
        result=self.process()
        self.assertEqual(result['action'],'pause');self.assertEqual(self.posts,[])
        self.assertFalse((self.output/'publisher-ownership.json').exists())

    def test_eon_enrollment_is_latched_without_any_publication(self):
        self.state['battery_enrolled']=True
        result=self.process()
        self.assertEqual(result['action'],'handover');self.assertEqual(self.posts,[])
        self.assertEqual(private_document(self.module.JOURNAL)['pause_latch']['kind'],'eon')

    def test_mode_command_pending_blocks_any_tariff_rewrite(self):
        self.module.reconcile_mode=lambda *args,**kwargs:{'pending':True}
        result=self.process()
        self.assertEqual(result['reason_codes'],['mode_command_outcome_pending']);self.assertEqual(self.posts,[])

    def test_eon_enrollment_is_latched_even_with_unresolved_mode_command(self):
        self.state['battery_enrolled']=True
        self.module.reconcile_mode=lambda *args,**kwargs:{'pending':True}
        self.process()
        self.assertEqual(private_document(self.module.JOURNAL).get('pause_latch',{}).get('kind'),'eon')

    def test_hardware_settings_drift_during_final_gate_blocks_post(self):
        self.module.read_active_mode=lambda:{'mode':self.state['mode'],'mode_source':'local_gateway_TEDAPI',
            'hardware_preserve_settings':dict(KEEP,backup_reserve=50)}
        result=self.process()
        self.assertEqual(self.posts,[])
        self.assertEqual(result['control_requests'],0)

    def test_initial_self_powered_start_passes_actual_mode_to_tariff_writer(self):
        self.state['mode']='self_consumption';self.state['journal']={'bootstrap_consumed':False}
        private_json(self.module.JOURNAL,self.state['journal'])
        result=self.process()
        self.assertTrue(result['publication_verified'])
        self.assertEqual(self.posts[0]['operation_mode'],'self_consumption')

    def test_owned_savings_enable_uses_final_authorizer_and_outer_lock(self):
        own_fallback(self.state);self.state['candidate_tariff_hash']=OLD
        private_json(self.module.JOURNAL,self.state['journal'])
        private_json(self.output/'publisher-lease.json',self.state['lease'])
        calls=[];completed=[]
        self.module.complete_mode_attempt=lambda result:completed.append(result)
        def actor_factory(owner,*,mode_authorizer,mode_attempt_recorder):
            self.assertEqual(owner,self.state['ownership'])
            def set_savings(**kwargs):
                self.assertIs(kwargs['lock_held'],True)
                self.assertEqual(kwargs['preserve_settings'],KEEP)
                context=deepcopy(self.state)
                self.assertIs(mode_authorizer(context),True)
                self.assertIs(mode_attempt_recorder(context),True)
                calls.append(kwargs);return {'command_sent':True,'mode_http_status':200}
            return SimpleNamespace(set_savings=set_savings)
        self.module.FallbackAdapter=actor_factory
        result=self.process()
        self.assertEqual(result['action'],'enable_savings');self.assertEqual(result['control_requests'],1)
        self.assertTrue(result['mode_result_pending']);self.assertEqual(len(calls),1)
        self.assertEqual(completed,[{'command_sent':True,'mode_http_status':200}])

    def real_mode_intent(self):
        import recurring_journal
        patched=patch.object(recurring_journal,'OUTPUT',self.output)
        patched.start();self.addCleanup(patched.stop)
        self.module.reconcile_mode=recurring_journal.reconcile_mode
        self.module.optional_private=recurring_journal.optional_private
        intent={'authorized':True,'durable':True,'dispatch_started':True,'command_sent':True,
            'controller':'watchdog','target':'self_consumption','from_mode':'autonomous',
            'energy_site_id':123,'tariff_semantic_hash':OLD,'policy_generation':'fixture',
            'created_at':stamp(-20),'preserve_settings':dict(KEEP),'explicit_rejection':False}
        private_json(self.output/'mode-intent.json',intent)
        return intent

    def test_actual_journal_reconciles_gateway_fallback_before_deciding(self):
        self.real_mode_intent()
        self.state['mode']='self_consumption';self.state['observed_at']=stamp(-1)
        self.state['candidate_tariff_hash']=OLD;self.state['policy']['manage_savings']=False
        private_json(self.module.POLICY,self.state['policy'])
        result=self.process()
        self.assertEqual(result['reason_codes'],['savings_mode_management_disabled'])
        self.assertFalse((self.output/'mode-intent.json').exists())
        journal=private_document(self.module.JOURNAL)
        self.assertEqual(journal['last_controlled_mode'],'self_consumption')
        self.assertTrue(journal['last_mode_action']['verified']);self.assertEqual(self.posts,[])

    def test_actual_journal_keeps_unobserved_mode_intent_without_resending(self):
        self.real_mode_intent();self.state['observed_at']=stamp(-1)
        result=self.process()
        self.assertTrue(result['mode_result_pending']);self.assertEqual(self.posts,[])
        self.assertTrue((self.output/'mode-intent.json').exists())

    def test_actual_journal_rejection_latches_manual_review(self):
        intent=self.real_mode_intent();intent['explicit_rejection']=True
        private_json(self.output/'mode-intent.json',intent)
        result=self.process()
        self.assertEqual(result['reason_codes'],['persistent_manual_pause'])
        self.assertEqual(private_document(self.module.JOURNAL)['pause_latch']['kind'],'manual')
        self.assertFalse((self.output/'mode-intent.json').exists());self.assertEqual(self.posts,[])


if __name__=='__main__':unittest.main()
