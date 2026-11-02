"""Offline personal-offer accounting and immutable report revision tests."""
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from customer_pricing import Policy
from observation_store import POWER_FIELDS,iso
from test_observer import OfflineCase,T,observation

RULE={
    'schema_version':1,'rule_id':'test-earlybird-v1','source_prices_include_incentive':False,
    'timezone':'Europe/London','start_date':'2019-01-01','end_date_exclusive':'2021-01-01',
    'import_discount_p':3,'import_window':{'start':'00:00','end':'06:00'},
    'export_bonus_p':3,'export_window':{'start':'16:00','end':'19:00'},
}

def segment(start,imported=0.5,exported=0.0,price=20,export_price=41):
    row={'start':start,'end':start+1800,'quality':'monitored',**{p+'_kwh':0.0 for p in POWER_FIELDS},
         'optimise_import_p':price,'optimise_export_p':export_price,'agile_import_p':25,'agile_export_p':15}
    row.update(grid_import_kwh=imported,grid_export_kwh=exported,home_kwh=imported)
    return row

class CustomerAccountingTests(OfflineCase):
    def configured(self,**kwargs):
        result=self.make_observer(**kwargs)
        result.customer_pricing=Policy(deepcopy(RULE))
        return result

    def seed(self,instance,segments):
        for row in segments:
            raw={k:v for k,v in row.items() if not k.endswith('_p')}
            instance.store.db.execute('INSERT INTO segments VALUES(?,?,?)',(row['start'],row['end'],json.dumps(raw)))
            for kind in ('optimise_import','optimise_export','agile_import','agile_export'):
                instance.store.add_quote(kind,row['start'],row['end'],row[kind+'_p'],row['start'])
        for now in (T,T+1800,T+3600):
            obs=observation(now);obs.update(eon_enrolled=False,eon_fresh=True,publisher_pause=None)
            instance.store.add_observation(obs)
        instance.store.set_metadata('power_query_cursor',instance.until);instance.store.commit()

    def test_import_discount_and_crossed_prices_reprice_in_memory_only(self):
        instance=self.configured();original=[segment(T,imported=12.5,export_price=20)]
        snapshot=deepcopy(original)
        effective=instance.customer_segments(original)
        self.assertEqual(original,snapshot)
        self.assertEqual(effective[0]['optimise_import_p'],17)
        self.assertEqual(effective[0]['optimise_export_p'],20)
        raw={'overall':__import__('accounting').summarize_window(original,T,T+1800),
             'overnight':__import__('accounting').summarize_window(original,T,T+1800),
             'peak':__import__('accounting').summarize_window(original,T+1800,T+3600)}
        _,summary=instance.customer_summaries(T+3600,original,raw)
        self.assertAlmostEqual(summary['overall']['import_discount_gbp'],12.5*.03)
        self.assertAlmostEqual(summary['overall']['total_incentive_gbp'],12.5*.03)

    def test_peak_export_bonus_preserves_other_channels_and_quality(self):
        instance=self.configured();original=[segment(T+16*3600,exported=10)]
        effective=instance.customer_segments(original)[0]
        self.assertEqual(effective['optimise_export_p'],44)
        self.assertEqual(effective['optimise_import_p'],20)
        self.assertEqual(effective['agile_export_p'],15)
        self.assertEqual(effective['quality'],'monitored')
        self.assertEqual(effective['grid_export_kwh'],10)

    def test_repricing_an_effective_trace_uses_preserved_raw_quotes_once(self):
        instance=self.configured()
        for original in ([segment(T)],[segment(T+16*3600,exported=1)]):
            first=instance.customer_segments(original)
            second=instance.customer_segments(first)
            self.assertEqual(first,second)
            self.assertEqual(second[0]['raw_optimise_import_p'],20)
            self.assertEqual(second[0]['raw_optimise_export_p'],41)

    def test_missing_raw_quote_remains_unpriced(self):
        instance=self.configured();original=[segment(T,price=None)]
        effective=instance.customer_segments(original)
        self.assertIsNone(effective[0]['optimise_import_p'])
        summary=__import__('accounting').summarize_window(effective,T,T+1800)
        self.assertTrue(summary['net_cost_is_partial'])
        self.assertEqual(summary['unpriced_import_kwh'],.5)

    def test_adjustment_is_not_applied_twice_when_rendering_again(self):
        instance=self.configured();self.seed(instance,[segment(T),segment(T+1800)])
        _,first=instance.report(T+2000,observation(T+2000))
        _,second=instance.report(T+2100,observation(T+2100))
        self.assertAlmostEqual(first['summary']['overnight']['import_cost_gbp'],.085)
        self.assertEqual(first['summary']['overnight'],second['summary']['overnight'])
        self.assertEqual(instance.store.db.execute("SELECT price FROM quotes WHERE kind='optimise_import' LIMIT 1").fetchone()[0],20)

    def test_original_frozen_result_is_preserved_and_revision_survives_restart(self):
        instance=self.make_observer();self.seed(instance,[segment(T),segment(T+1800)])
        _,original=instance.report(T+2000,observation(T+2000))
        key=original['window_results']['overnight']['key']
        directory=Path(self.temporary.name)
        original_file=(directory/'overnight-result.json').read_bytes()
        original_metadata=instance.store.metadata('window_result_'+key)
        instance.customer_pricing=Policy(deepcopy(RULE))
        _,revised=instance.report(T+2100,observation(T+2100))
        entry=revised['window_results']['overnight']
        self.assertEqual(entry['key'],key)
        self.assertEqual(entry['ready_at'],original['window_results']['overnight']['ready_at'])
        self.assertEqual((directory/'overnight-result.json').read_bytes(),original_file)
        self.assertEqual(instance.store.metadata('window_result_'+key),original_metadata)
        self.assertAlmostEqual(entry['raw_summary']['net_cost_gbp'],.1)
        self.assertAlmostEqual(entry['summary']['net_cost_gbp'],.085)
        versioned=directory/'overnight-result-test-earlybird-v1.json'
        self.assertTrue(versioned.is_file())
        frozen=versioned.read_bytes()
        restarted=self.configured()
        _,again=restarted.report(T+2200,observation(T+2200))
        self.assertEqual(entry,again['window_results']['overnight'])
        self.assertEqual(versioned.read_bytes(),frozen)
        self.http.assert_not_called()

    def test_new_peak_ready_entry_exposes_effective_summary_to_alert_consumer(self):
        windows={'overall':[iso(T+16*3600),iso(T+20*3600)],'overnight':[iso(T),iso(T+6*3600)],'peak':[iso(T+16*3600),iso(T+19*3600)]}
        instance=self.configured(record_from=iso(T),capture_until=iso(T+20*3600),windows=windows)
        self.seed(instance,[segment(T+i*1800,imported=0,exported=1) for i in range(32,38)])
        _,report=instance.report(T+19*3600+120,observation(T+19*3600+120))
        entry=report['window_results']['peak']
        self.assertEqual(entry['status'],'ready')
        self.assertAlmostEqual(entry['raw_summary']['export_revenue_gbp'],6*.41)
        self.assertAlmostEqual(entry['summary']['export_revenue_gbp'],6*.44)
        self.assertAlmostEqual(entry['summary']['export_bonus_gbp'],.18)
        original=json.loads((Path(self.temporary.name)/'peak-result.json').read_text())
        self.assertAlmostEqual(original['summary']['export_revenue_gbp'],6*.41)

    def test_future_window_has_no_fake_zero_credit(self):
        instance=self.configured();_,report=instance.report(T-1,observation(T-1))
        self.assertIsNone(report['summary']['overnight']['total_incentive_gbp'])
        self.assertIsNone(report['summary']['overnight']['net_cost_gbp'])

    def test_final_model_and_actual_cost_use_same_effective_prices(self):
        instance=self.configured();self.seed(instance,[segment(T),segment(T+1800)])
        state,report=instance.report(T+3600,observation(T+3600))
        self.assertEqual(state,'complete')
        final=json.loads((Path(self.temporary.name)/'final-report.json').read_text())
        self.assertAlmostEqual(final['recording']['raw_summary']['overall']['net_cost_gbp'],.2)
        self.assertAlmostEqual(final['recording']['summary']['overall']['net_cost_gbp'],.17)
        model=final['controller_saving_estimate']
        self.assertEqual(model['status'],'illustrative_model_only')
        self.assertEqual(model['pricing']['basis'],'customer_adjusted_optimise')
        for scenario in model['scenarios']:
            self.assertAlmostEqual(scenario['result']['summary']['net_cost_gbp'],.17)
            self.assertAlmostEqual(scenario['result']['unadjusted_cash_difference_gbp'],-.17)
        rows=list(csv.DictReader(io.StringIO((Path(self.temporary.name)/'priced-segments.csv').read_text())))
        self.assertEqual(float(rows[0]['raw_optimise_import_p']),20)
        self.assertEqual(float(rows[0]['optimise_import_p']),17)

    def test_existing_model_guards_are_not_weakened(self):
        instance=self.configured();self.seed(instance,[segment(T),segment(T+1800)])
        for (ts,value) in instance.store.db.execute('SELECT ts,data FROM observations').fetchall():
            row=json.loads(value);row['gateway']['backup_reserve_percent']=50
            instance.store.db.execute('UPDATE observations SET data=? WHERE ts=?',(json.dumps(row),ts))
        _,report=instance.report(T+3600,observation(T+3600))
        final=json.loads((Path(self.temporary.name)/'final-report.json').read_text())
        self.assertEqual(final['controller_saving_estimate']['status'],'not_estimated')
        self.assertIn('Reserve',final['controller_saving_estimate']['reason'])

if __name__=='__main__':unittest.main()
