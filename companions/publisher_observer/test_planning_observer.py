"""Offline observer proof checks; no credentials, network, or live writes."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import observer

def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
def iso(value):return observer.iso(value)
NOW=stamp('2026-11-02T03:05:00Z')
TARGET='a'*64
POLICY={'schema_version':1,'kind':'eon_earlybird_planning_v1',
    'rule_id':'eon-earlybird-tesla-planning-v1','timezone':'Europe/London',
    'start_date':'2026-10-30','end_date_exclusive':'2027-10-30',
    'overnight_reduction_p_per_kwh':3,'peak_export_bonus_p_per_kwh':3,
    'source_price_basis':'raw_eon_optimise_api','unquoted_scope':'quoted_only'}


def fixture(now=NOW,raw=(.20,.20),planned=(.17,.17)):
    begin=now//1800*1800;finish=begin+1800
    row={'valid_from':iso(begin),'valid_to':iso(finish),
         'import_gbp_per_kwh':str(raw[0]),'export_gbp_per_kwh':str(raw[1])}
    adjusted={**row,'import_gbp_per_kwh':str(planned[0]),'export_gbp_per_kwh':str(planned[1])}
    receipt={'verified':True,'verified_at':iso(now-5),'tariff_hash':TARGET,
        'policy_generation':'planning-test-v1','known_valid_from':iso(begin),'known_valid_until':iso(finish),
        'planning_policy':deepcopy(POLICY),'planning_policy_sha256':hashlib.sha256(json.dumps(POLICY,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'planning_adjustment_scope':'quoted_intervals_only','quoted_intervals':[row],'planning_quoted_intervals':[adjusted]}
    p={'assessed_at':iso(now-3),'source_valid':True,'owns_installed_tariff':True,'pause_latch':None,'action':'renew_lease'}
    owner={'verified_publisher_write':True,'tariff_semantic_hash':TARGET,'last_verified_tariff_hash':TARGET,
           'policy_generation':'planning-test-v1','last_verified_at':iso(now-6)}
    lease={'tariff_semantic_hash':TARGET,'intended_tariff_hash':TARGET,'guard_lease_trusted':True,
           'publisher_owns_installed_tariff':True,'recovery_only':False,'guard_lease_until':iso(now+240),'policy_generation':'planning-test-v1'}
    publication={'verified':True,'candidate_hash':TARGET,'resolved_at':iso(now-6)}
    return [receipt,p,owner,lease,publication,{'stale':False}]


class PlanningProofTests(unittest.TestCase):
    def test_current_verified_receipt_shows_raw_and_distinct_planning_prices(self):
        result=observer.planning_observation(NOW,*fixture())
        self.assertEqual(result['status'],'verified')
        self.assertAlmostEqual(result['raw_import_p'],20)
        self.assertAlmostEqual(result['raw_export_p'],20)
        self.assertAlmostEqual(result['current_import_p'],17)
        self.assertAlmostEqual(result['current_export_p'],17)
        self.assertEqual(result['scope'],'quoted_intervals_only')
    def test_missing_receipt_is_unavailable_and_configured_only_is_unverified(self):
        args=fixture();args[0]={}
        self.assertEqual(observer.planning_observation(NOW,*args)['status'],'unavailable')
        args=fixture();args[0]['verified']=False
        self.assertEqual(observer.planning_observation(NOW,*args)['status'],'configured_unverified')
    def test_ownership_publication_and_safety_failures_never_show_verified(self):
        changes=[(0,'verified_at',iso(NOW-241)),(0,'verified_at',iso(NOW+1)),
            (0,'planning_policy_sha256','b'*64),(0,'policy_generation','other'),
            (0,'known_valid_until',iso(NOW)),(0,'known_valid_from',iso(NOW+1)),
            (0,'planning_adjustment_scope','all_intervals'),
            (1,'assessed_at',iso(NOW-241)),(1,'source_valid',False),(1,'owns_installed_tariff',False),
            (1,'pause_latch',{'kind':'manual','reason':'manual_mode_change'}),(1,'action','pause'),
            (2,'tariff_semantic_hash','b'*64),(2,'last_verified_tariff_hash','b'*64),
            (2,'verified_publisher_write',False),(2,'last_verified_at',iso(NOW-241)),
            (2,'last_verified_at',iso(NOW+1)),(3,'tariff_semantic_hash','b'*64),
            (3,'intended_tariff_hash','b'*64),(3,'guard_lease_trusted',False),
            (3,'publisher_owns_installed_tariff',False),(3,'recovery_only',True),
            (3,'guard_lease_until',iso(NOW)),(3,'policy_generation','other'),
            (4,'verified',False),(4,'candidate_hash','b'*64),(4,'resolved_at',iso(NOW-4)),
            (5,'stale',True),(5,'stale',None)]
        for index,key,value in changes:
            with self.subTest(index=index,key=key,value=value):
                args=fixture();args[index][key]=value
                result=observer.planning_observation(NOW,*args)
                self.assertEqual(result['status'],'configured_unverified')
                self.assertIsNone(result['current_import_p'])
    def test_malformed_or_ambiguous_current_rows_are_not_verified(self):
        changes=[('planning_quoted_intervals',[]),('quoted_intervals',[]),
            ('planning_quoted_intervals',[fixture()[0]['planning_quoted_intervals'][0]]*2),
            ('quoted_intervals',True)]
        for key,value in changes:
            with self.subTest(key=key,value=value):
                args=fixture();args[0][key]=value
                self.assertEqual(observer.planning_observation(NOW,*args)['status'],'configured_unverified')
        for value in ('not-a-price','NaN',True,None,'-1'):
            with self.subTest(price=value):
                args=fixture();args[0]['planning_quoted_intervals'][0]['import_gbp_per_kwh']=value
                self.assertEqual(observer.planning_observation(NOW,*args)['status'],'configured_unverified')
    def test_wrong_or_crossed_adjustment_is_not_verified(self):
        for pair in ((.16,.16),(.17,.18),(.20,.20)):
            with self.subTest(pair=pair):self.assertEqual(observer.planning_observation(NOW,*fixture(planned=pair))['status'],'configured_unverified')
    def test_peak_bonus_uses_british_summer_clock(self):
        now=stamp('2027-07-01T15:05:00Z')
        result=observer.planning_observation(now,*fixture(now,raw=(.65,.42),planned=(.65,.45)))
        self.assertEqual(result['status'],'verified')
        self.assertAlmostEqual(result['current_export_p'],45)
    def test_after_offer_expiry_verified_current_prices_have_no_adjustment(self):
        now=stamp('2027-10-30T03:05:00Z')
        result=observer.planning_observation(now,*fixture(now,planned=(.20,.20)))
        self.assertEqual(result['status'],'verified')
        self.assertAlmostEqual(result['current_export_p'],20)
    def test_nonobject_proof_inputs_never_throw_or_verify(self):
        for index in range(6):
            args=fixture();args[index]=[]
            with self.subTest(index=index):self.assertNotEqual(observer.planning_observation(NOW,*args)['status'],'verified')


class SanitizationTests(unittest.TestCase):
    def test_pause_dict_is_readable_and_contains_only_known_codes(self):
        self.assertEqual(observer.display_pause({'kind':'manual','reason':'manual_mode_change','password':'hidden'}),'manual:manual_mode_change')
        self.assertEqual(observer.display_pause({'kind':'manual','reason':'<script>'}),'manual')
        self.assertIsNone(observer.display_pause(None))
    def test_status_json_read_is_bounded(self):
        with tempfile.TemporaryDirectory(prefix='planning-display-') as folder:
            path=Path(folder)/'oversized.json';path.write_bytes(b' '*200001+b'{}')
            self.assertEqual(observer.read_json(path),{})


if __name__=='__main__':unittest.main()
