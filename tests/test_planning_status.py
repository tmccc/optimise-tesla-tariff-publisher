"""Verified planning metadata follows ownership; no publication is simulated live."""
from copy import deepcopy
import unittest

from runtime import private_document
from prepare import private_json
import test_recurring as coordinator
from test_recurring_state import OLD,NEW,stamp
from test_planning_pricing import POLICY,source
from date_compiler import build_date_candidate
from test_date_compiler import baseline


class PlanningStatusTests(unittest.TestCase):
    def setUp(self):
        self.delegate=coordinator.RecurringCoordinatorTests('runTest')
        self.delegate.setUp();self.addCleanup(self.delegate.doCleanups)
        self.module=self.delegate.module;self.state=self.delegate.state;self.output=self.delegate.output
        self.process=self.delegate.process
    def configure(self):
        self.state['policy']['planning_price_policy']=deepcopy(POLICY)
        private_json(self.module.POLICY,self.state['policy'])
        value,clock=source()
        manifest=build_date_candidate(value,baseline(),now=clock,unquoted_policy='frozen_current_quote',planning_policy=POLICY)['manifest']
        self.state['planning_manifest']={key:manifest.get(key) for key in (
            'planning_policy','planning_policy_sha256','planning_adjustment_scope',
            'planning_quoted_intervals','quoted_intervals','known_valid_from','known_valid_until')}

    def test_planning_status_only_after_verified_publication(self):
        self.configure();result=self.process()
        self.assertTrue(result['publication_verified'])
        status=private_document(self.output/'planning-status.json')
        self.assertTrue(status['verified']);self.assertEqual(status['tariff_hash'],NEW)
        self.assertEqual(status['verification_kind'],'publication')
        self.assertEqual(status['planning_adjustment_scope'],'quoted_intervals_only')
        self.assertFalse(status['planning_prices_are_billing_prices'])

    def test_renewal_has_new_verification_time_not_a_new_write_time(self):
        self.configure();self.state['candidate_tariff_hash']=OLD
        result=self.process();self.assertTrue(result['lease_renewed'])
        status=private_document(self.output/'planning-status.json')
        self.assertEqual(status['tariff_written_at'],stamp(-3600))
        self.assertEqual(status['verified_at'],self.delegate.clock.isoformat())
        self.assertEqual(status['verification_kind'],'owned_tariff_renewal')

    def test_invalid_manifest_or_unverified_candidate_cannot_create_status(self):
        for kind in ('policy','scope','hash','candidate'):
            self.configure()
            if kind=='policy':self.state['planning_manifest']['planning_policy']['rule_id']='other-policy'
            elif kind=='scope':self.state['planning_manifest']['planning_adjustment_scope']='all'
            elif kind=='hash':self.state['planning_manifest']['planning_policy_sha256']='0'*64
            else:self.state['candidate_prices_verified']=False
            records={'verified':True,'ownership':{'tariff_semantic_hash':self.state['candidate_tariff_hash']}}
            self.module._record_verified_planning_status(self.state,records)
            self.assertFalse((self.output/'planning-status.json').exists(),kind)

    def test_wrong_ownership_hash_is_not_reported_as_verified_plan(self):
        self.configure()
        records={'verified':True,'ownership':{'tariff_semantic_hash':OLD}}
        self.module._record_verified_planning_status(self.state,records)
        self.assertFalse((self.output/'planning-status.json').exists())

    def test_no_policy_retains_legacy_no_planning_status_behavior(self):
        result=self.process();self.assertTrue(result['publication_verified'])
        self.assertFalse((self.output/'planning-status.json').exists())


if __name__=='__main__':unittest.main()
