from copy import deepcopy
import json
from pathlib import Path
import unittest
from customer_pricing import Policy,load_policy

CONFIG=json.loads((Path(__file__).parent/'test-policy.json').read_text())

class IncentiveTests(unittest.TestCase):
    def setUp(self):self.policy=Policy(CONFIG)
    def quote(self,direction,start,end,value=20):return self.policy.adjust_price(value,direction,start,end)
    def test_import_boundaries(self):
        for left,right,expected in [('2026-11-01T23:30Z','2026-11-02T00:00Z',20),('2026-11-02T00:00Z','2026-11-02T00:30Z',17),('2026-11-02T05:30Z','2026-11-02T06:00Z',17),('2026-11-02T06:00Z','2026-11-02T06:30Z',20)]:
            with self.subTest(start=left):self.assertEqual(self.quote('import',left,right)['effective_p_per_kwh'],expected)
    def test_export_boundaries(self):
        for left,right,expected in [('2026-11-02T15:30Z','2026-11-02T16:00Z',20),('2026-11-02T16:00Z','2026-11-02T16:30Z',23),('2026-11-02T18:30Z','2026-11-02T19:00Z',23),('2026-11-02T19:00Z','2026-11-02T19:30Z',20)]:
            with self.subTest(start=left):self.assertEqual(self.quote('export',left,right)['effective_p_per_kwh'],expected)
    def test_summer_uses_british_local_time(self):
        self.assertEqual(self.quote('export','2027-07-01T15:00Z','2027-07-01T15:30Z')['effective_p_per_kwh'],23)
        self.assertEqual(self.quote('export','2027-07-01T18:00Z','2027-07-01T18:30Z')['effective_p_per_kwh'],20)
        self.assertEqual(self.quote('import','2027-06-30T23:00Z','2027-06-30T23:30Z')['effective_p_per_kwh'],17)
    def test_spring_clock_change_overnight_window(self):
        for hour in (0,1,2,3,4):self.assertEqual(self.quote('import',f'2027-03-28T0{hour}:00Z',f'2027-03-28T0{hour}:30Z')['effective_p_per_kwh'],17)
        self.assertEqual(self.quote('import','2027-03-28T05:00Z','2027-03-28T05:30Z')['effective_p_per_kwh'],20)
    def test_autumn_repeated_hour(self):
        cfg=deepcopy(CONFIG);cfg.update(start_date='2025-10-30',end_date_exclusive='2027-10-30')
        policy=Policy(cfg)
        for h in ('00','01'):self.assertEqual(policy.adjust_price(20,'import',f'2026-10-25T{h}:00Z',f'2026-10-25T{h}:30Z')['effective_p_per_kwh'],17)
    def test_eligibility_start_and_expiry_local_midnight(self):
        self.assertEqual(self.quote('import','2026-10-29T00:00Z','2026-10-29T00:30Z')['effective_p_per_kwh'],20)
        self.assertEqual(self.quote('import','2026-10-30T00:00Z','2026-10-30T00:30Z')['effective_p_per_kwh'],17)
        self.assertEqual(self.quote('import','2027-10-29T23:00Z','2027-10-29T23:30Z')['effective_p_per_kwh'],20)
    def test_missing_base_is_not_an_invented_price(self):
        result=self.quote('import','2026-11-02T00:00Z','2026-11-02T00:30Z',None)
        self.assertIsNone(result['effective_p_per_kwh']);self.assertEqual(result['reason'],'missing_base_price')
    def test_zero_negative_and_crossed_prices_preserved(self):
        self.assertEqual(self.quote('import','2026-11-02T00:00Z','2026-11-02T00:30Z',0)['effective_p_per_kwh'],-3)
        self.assertEqual(self.quote('export','2026-11-02T16:00Z','2026-11-02T16:30Z',-5)['effective_p_per_kwh'],-2)
    def test_mixed_boundary_or_long_period_is_rejected(self):
        for left,right in [('2026-11-02T05:59Z','2026-11-02T06:01Z'),('2026-11-02T00:00Z','2026-11-02T06:00Z')]:
            with self.assertRaises(ValueError):self.quote('import',left,right)
    def test_invalid_prices_and_naive_times_rejected(self):
        for value in (True,'20',float('nan'),float('inf')):
            with self.assertRaises(ValueError):self.quote('import','2026-11-02T00:00Z','2026-11-02T00:30Z',value)
        with self.assertRaises(ValueError):self.quote('import','2026-11-02T00:00','2026-11-02T00:30')
    def test_policy_must_explicitly_exclude_upstream_incentive(self):
        cfg=deepcopy(CONFIG);cfg['source_prices_include_incentive']=True
        with self.assertRaises(ValueError):Policy(cfg)
    def test_metadata_and_optional_configuration(self):
        self.assertIsNone(load_policy(None));self.assertEqual(len(self.policy.metadata()['policy_sha256']),64)
        self.assertEqual(CONFIG['rule_id'],self.policy.metadata()['rule_id'])

if __name__=='__main__':unittest.main()
