"""Pure quoted-only planning transform; no credentials or live APIs."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import unittest

from date_compiler import build_date_candidate
from planning_pricing import normalize_policy
from tariff_schema import rate_at,semantic_hash
from writer import validate_candidate
from test_date_compiler import baseline,preview,instant

POLICY={'schema_version':1,'kind':'eon_earlybird_planning_v1',
    'rule_id':'eon-optimise-earlybird-planning-20261030-v1','timezone':'Europe/London',
    'start_date':'2026-10-30','end_date_exclusive':'2027-10-30',
    'overnight_reduction_p_per_kwh':3,'peak_export_bonus_p_per_kwh':3,
    'source_price_basis':'raw_eon_optimise_api','unquoted_scope':'quoted_only'}


def source(start='2026-11-02T00:00:00Z',count=48):
    value,now=preview(start,count,equal_prices=True)
    for row in value['periods']:
        stamp=instant(row['valid_from']).astimezone(__import__('zoneinfo').ZoneInfo('Europe/London'))
        buy,sell=(Decimal('.65'),Decimal('.42')) if 16<=stamp.hour<19 else (Decimal('.21'),Decimal('.21'))
        row.update(import_gbp_per_kwh=str(buy),export_gbp_per_kwh=str(sell),
                   import_p_per_kwh=str(buy*100),export_p_per_kwh=str(sell*100))
    return value,now


class PlanningPriceTests(unittest.TestCase):
    def build(self,value=None,now=None,policy=None):
        if value is None:value,now=source()
        return build_date_candidate(value,baseline(),now=now,
            unquoted_policy='frozen_current_quote',planning_policy=POLICY if policy is None else policy)

    def assertRate(self,result,when,buy,sell):
        self.assertEqual(rate_at(result['tariff'],instant(when)),Decimal(buy))
        self.assertEqual(rate_at(result['tariff'],instant(when),'export'),Decimal(sell))

    def test_exact_requested_clock_windows(self):
        result=self.build();self.assertTrue(result['candidate_valid'],result['errors'])
        for when,buy,sell in [('2026-11-02T00:00Z','.18','.18'),('2026-11-02T05:30Z','.18','.18'),
              ('2026-11-02T06:00Z','.21','.21'),('2026-11-02T15:30Z','.21','.21'),
              ('2026-11-02T16:00Z','.65','.45'),('2026-11-02T18:30Z','.65','.45'),('2026-11-02T19:00Z','.21','.21')]:
            with self.subTest(when=when):self.assertRate(result,when,buy,sell)

    def test_quoted_only_leaves_frozen_pair_raw_everywhere_else(self):
        result=self.build();self.assertTrue(result['candidate_valid'],result['errors'])
        for when in ('2026-11-03T02:00Z','2026-11-03T16:00Z','2027-02-01T17:00Z'):
            self.assertRate(result,when,'.21','.21')
        pair=result['manifest']['unquoted_assumed_pair']
        self.assertEqual(pair['import_gbp_per_kwh'],'0.21');self.assertEqual(pair['export_gbp_per_kwh'],'0.21')
        self.assertEqual(result['manifest']['planning_adjustment_scope'],'quoted_intervals_only')

    def test_raw_quotes_are_preserved_and_effective_rows_are_separate(self):
        value,now=source();before=deepcopy(value);result=self.build(value,now)
        self.assertEqual(value,before)
        raw=result['manifest']['quoted_intervals'];planned=result['manifest']['planning_quoted_intervals']
        self.assertEqual(raw[0]['import_gbp_per_kwh'],'0.21')
        self.assertEqual(planned[0]['import_gbp_per_kwh'],'0.18')
        self.assertFalse(result['manifest']['planning_prices_are_billing_prices'])
        self.assertEqual(result['tariff']['name'],'Next Optimise (HA planning rates)')

    def test_second_rebuild_is_idempotent_not_a_second_discount(self):
        value,now=source();first=self.build(value,now)
        second=build_date_candidate(value,first['tariff'],now=now,
            unquoted_policy='frozen_current_quote',planning_policy=POLICY)
        self.assertEqual(semantic_hash(first['tariff']),semantic_hash(second['tariff']))
        self.assertRate(second,'2026-11-02T01:00Z','.18','.18')

    def test_writer_rebuild_validates_complete_annual_projection(self):
        value,now=source();result=self.build(value,now)
        self.assertEqual(validate_candidate(result['tariff'],result['manifest'],now=now),[])

    def test_writer_rejects_policy_or_adjusted_row_tampering(self):
        value,now=source();result=self.build(value,now)
        for mode in ('hash','row','scope','billing'):
            manifest=deepcopy(result['manifest'])
            if mode=='hash':manifest['planning_policy_sha256']='0'*64
            elif mode=='row':manifest['planning_quoted_intervals'][0]['import_gbp_per_kwh']='0.17'
            elif mode=='scope':manifest['planning_adjustment_scope']='all'
            else:manifest['planning_prices_are_billing_prices']=True
            with self.subTest(mode=mode):self.assertIn('planning_metadata_mismatch',validate_candidate(result['tariff'],manifest,now=now))

    def test_writer_rejects_removing_policy_from_adjusted_tariff(self):
        value,now=source();result=self.build(value,now);manifest=deepcopy(result['manifest'])
        manifest.pop('planning_policy')
        self.assertIn('candidate_quote_semantics_mismatch',validate_candidate(result['tariff'],manifest,now=now))

    def test_summer_windows_follow_london_not_utc(self):
        value,now=source('2027-07-01T00:00Z',48);result=self.build(value,now)
        self.assertTrue(result['candidate_valid'],result['errors'])
        self.assertRate(result,'2027-07-01T04:30Z','.18','.18')
        self.assertRate(result,'2027-07-01T05:00Z','.21','.21')
        self.assertRate(result,'2027-07-01T15:00Z','.65','.45')
        self.assertRate(result,'2027-07-01T18:00Z','.21','.21')

    def test_spring_clock_jump_remains_valid(self):
        value,now=source('2027-03-28T00:00Z',48);result=self.build(value,now)
        self.assertTrue(result['candidate_valid'],result['errors'])
        self.assertRate(result,'2027-03-28T00:30Z','.18','.18')
        self.assertRate(result,'2027-03-28T04:30Z','.18','.18')
        self.assertRate(result,'2027-03-28T05:00Z','.21','.21')

    def test_fall_equal_fold_is_adjusted_consistently(self):
        value,now=source('2026-10-25T00:00Z',48);policy=dict(POLICY,start_date='2026-10-01',end_date_exclusive='2027-10-01')
        result=self.build(value,now,policy)
        self.assertTrue(result['candidate_valid'],result['errors'])
        self.assertRate(result,'2026-10-25T00:00Z','.18','.18')
        self.assertRate(result,'2026-10-25T01:00Z','.18','.18')

    def test_eligibility_starts_and_expires_at_local_date(self):
        for start,expected in [('2026-10-29T00:00Z','.21'),('2026-10-30T00:00Z','.18'),('2027-10-29T23:00Z','.21')]:
            value,now=source(start,4);result=self.build(value,now)
            with self.subTest(start=start):
                self.assertTrue(result['candidate_valid'],result['errors']);self.assertRate(result,start,expected,expected)

    def test_negative_transformed_price_refuses_instead_of_clamps(self):
        value,now=source()
        row=value['periods'][0];row.update(import_gbp_per_kwh='.02',export_gbp_per_kwh='.02',import_p_per_kwh='2',export_p_per_kwh='2')
        result=self.build(value,now)
        self.assertFalse(result['candidate_valid']);self.assertIsNone(result['tariff'])
        self.assertIn('planning_negative_price',[e['code'] for e in result['errors']])

    def test_real_peak_crossed_pair_is_still_blocked(self):
        value,now=source('2026-11-02T16:00Z',4)
        for row in value['periods']:row.update(import_gbp_per_kwh='.22',export_gbp_per_kwh='.21',import_p_per_kwh='22',export_p_per_kwh='21')
        result=self.build(value,now)
        self.assertFalse(result['candidate_valid'])
        self.assertIn('candidate_tesla_validation_failed',[e['code'] for e in result['errors']])

    def test_already_adjusted_source_is_rejected(self):
        value,now=source();value['price_basis']='customer_effective'
        result=self.build(value,now)
        self.assertIn('planning_source_already_adjusted',[e['code'] for e in result['errors']])

    def test_unknown_scope_amount_or_unbounded_policy_is_rejected(self):
        for change in ({'unquoted_scope':'all_planning_periods'},{'overnight_reduction_p_per_kwh':2},{'end_date_exclusive':'2030-10-30'}):
            with self.subTest(change=change),self.assertRaises(ValueError):normalize_policy(dict(POLICY,**change))

if __name__=='__main__':unittest.main()
