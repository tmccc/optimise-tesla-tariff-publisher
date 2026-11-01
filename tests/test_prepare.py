from copy import deepcopy
from datetime import datetime,timedelta,timezone
import unittest
import tempfile
from pathlib import Path
import json
from unittest.mock import patch
import prepare
from contextlib import redirect_stdout
from io import StringIO

from prepare import prepare_report,_enrollment,persist_outputs
from tariff_schema import canonical_hash

UTC=timezone.utc
NOW=datetime(2026,11,1,12,5,tzinfo=UTC)


def fixture():
    def side(price):
        return {'currency':'GBP','demand_charges':{'ALL':{'ALL':0}},
            'seasons':{'Annual':{'fromMonth':1,'fromDay':1,'toMonth':12,'toDay':31,
                'tou_periods':{'Flat':[{'fromDayOfWeek':0,'toDayOfWeek':6,
                    'fromHour':0,'fromMinute':0,'toHour':0,'toMinute':0}]}}},
            'energy_charges':{'Annual':{'Flat':price}}}
    tariff=side(0)
    tariff['sell_tariff']=side(0.1)
    digest=canonical_hash(tariff)
    backup={'tariff_rate_response':tariff,'sha256':digest}
    source={'captured_at':NOW.isoformat(),'last_success':NOW.isoformat(),
        'currency':'GBP','unit':'p/kWh','timezone':'Europe/London','channels':{}}
    for direction,price in [('import',30),('export',20)]:
        rows=[]
        for index in range(49):
            start=NOW.replace(minute=0)+timedelta(minutes=30*index)
            rows.append({'valid_from':start.isoformat(),
                'valid_to':(start+timedelta(minutes=30)).isoformat(),'value':price,
                'quality':'current' if index==0 else 'forecast','estimate':False,
                'interval_basis':'provisional_30m'})
        source['channels'][direction]=rows
    readback={'tariff_sha256':digest,'captured_at':NOW.isoformat(),
        'energy_commands_scope':True,
        'site_settings':{'default_real_mode':'autonomous','backup_reserve_percent':10}}
    status={'states':[{'key':'battery_enrolled','state':'off'},
        {'key':'status_last_update','state':NOW.isoformat()}],
        'diagnostics':[{'status':{'fresh':True,'last_update_success':True,
            'provider_error':{'failed_roots':['smartshiftBatteryStrategyConfig']}}}]}
    return source,backup,readback,status


class PrepareTests(unittest.TestCase):
    def test_public_defaults_do_not_inherit_lab_approvals(self):
        report,_=prepare_report(*fixture(),now=NOW)
        self.assertFalse(report['fallback_approved'])
        self.assertIsNone(report['unquoted_policy_approved'])
        self.assertIn('expiry_fallback_unapproved',report['guard']['reason_codes'])
        approval=report['candidates']['frozen_current_quote']['manifest']['approved_design_assumptions']
        self.assertFalse(approval['frozen_current_quote'])
        self.assertFalse(approval['self_powered_failure_policy'])
        self.assertFalse(approval['live_execution'])

    def test_offline_cli_does_not_read_credentials_or_make_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source,backup,_,_=fixture()
            (root/'source.json').write_text(json.dumps(source))
            (root/'baseline.json').write_text(json.dumps(backup['tariff_rate_response']))
            with patch.object(prepare,'STATE_DIR',root),patch.object(prepare,'ensure_state_dir',return_value=root),\
                patch.object(prepare,'read_live_source') as live,\
                patch.object(prepare,'read_battery_status') as enrollment,\
                patch.object(prepare,'read_live_readback') as readback:
                with redirect_stdout(StringIO()):
                    result=prepare.main(['--input',str(root/'source.json'),'--baseline',str(root/'baseline.json'),
                        '--as-of',NOW.isoformat(),'--output-dir',str(root/'output')])
            self.assertEqual(result,0)
            for callback in (live,enrollment,readback):callback.assert_not_called()
            report=json.loads((root/'output'/'readiness.json').read_text())
            self.assertFalse(report['ready_for_upload']);self.assertFalse(report['fallback_approved'])
            self.assertEqual(report['source_mode'],'offline_replay')
            self.assertIsNone(report['optimise_battery_enrolled'])
            self.assertIn('live_last_success_unknown',report['guard']['reason_codes'])

    def test_actual_baseline_problem_and_guard_block_are_preserved(self):
        report,artifacts=prepare_report(*fixture(),now=NOW,design_approvals={
            'fallback_design_approved':True,'fallback_policy':'self_powered',
            'unquoted_planning_assumption_approved':True,'unquoted_policy':'frozen_current_quote'})
        self.assertTrue(report['backup']['integrity_verified'])
        self.assertEqual(report['backup']['structural_errors'],[])
        self.assertEqual(report['backup']['upload_compatibility_errors'],['export_exceeds_import'])
        self.assertFalse(report['backup']['restoration_verified'])
        self.assertFalse(report['ready_for_upload'])
        self.assertFalse(report['guard']['allow_publish'])
        self.assertEqual(report['candidate_comparison']['price_values_compared'],98)
        self.assertTrue(report['candidate_comparison']['all_exact'])
        self.assertFalse(report['candidates']['preserve_baseline']['candidate_valid'])
        self.assertTrue(report['candidates']['disabled_zero_placeholder']['candidate_valid'])
        self.assertEqual(report['tesla_control_requests'],0)
        self.assertIn('independent_watchdog_unverified',report['guard']['reason_codes'])
        self.assertNotIn('expiry_fallback_unapproved',report['guard']['reason_codes'])
        self.assertTrue(report['fallback_approved'])
        self.assertEqual(report['unquoted_policy_approved'],'frozen_current_quote')
        self.assertIn('baseline-format-only-NOT-VERIFIED-ROLLBACK.json',artifacts)

    def test_backup_tampering_blocks_before_candidates(self):
        values=list(fixture());values[1]['tariff_rate_response']['currency']='EUR'
        report,artifacts=prepare_report(*values,now=NOW)
        self.assertFalse(report['backup']['integrity_verified'])
        self.assertEqual(report['blockers'],['backup_structure_or_integrity_failed'])
        self.assertEqual(artifacts,{})

    def test_activated_battery_never_proposes_control_override(self):
        values=list(fixture());values[3]['states'][0]['state']='on'
        report,_=prepare_report(*values,now=NOW)
        self.assertEqual(report['guard']['proposed_action'],
            'handover_to_optimise_without_control_writes')
        self.assertFalse(report['guard']['mode_change_permitted_by_policy'])

    def test_failed_experience_does_not_reuse_an_off_state(self):
        status=fixture()[3]
        status['diagnostics'][0]['status']['provider_error']['failed_roots'].append('smartshiftExperience')
        self.assertEqual(_enrollment(status),(None,None))

    def test_multiple_site_status_requires_selection(self):
        status=fixture()[3];status['states'].append({'key':'battery_enrolled','state':'off'})
        self.assertEqual(_enrollment(status),(None,None))

    def test_stale_prices_produce_no_candidate_or_upload_eligibility(self):
        values=list(fixture());values[0]['last_success']=(NOW-timedelta(minutes=16)).isoformat()
        report,artifacts=prepare_report(*values,now=NOW)
        self.assertFalse(report['prices']['valid'])
        self.assertEqual(report['candidate_comparison']['price_values_compared'],0)
        self.assertFalse(report['guard']['allow_publish'])
        self.assertNotIn('disabled_zero_placeholder-proposal.json',artifacts)

    def test_metadata_validation_failure_remains_blocked(self):
        values=list(fixture());values[0]['source_validation_errors']=['untrusted_unit']
        report,_=prepare_report(*values,now=NOW)
        self.assertFalse(report['prices']['valid'])
        self.assertIn('source_metadata_validation_failed',report['prices']['errors'])

    def test_scenario_results_are_explicitly_nonexecuting(self):
        report,_=prepare_report(*fixture(),now=NOW)
        for decision in report['guard_simulations'].values():
            self.assertTrue(decision['simulation_only'])
            self.assertFalse(decision['action_executed'])
            self.assertFalse(decision['allow_publish'])
        self.assertEqual(report['guard_simulations']['optimise_activates']['proposed_action'],
            'handover_to_optimise_without_control_writes')

    def test_blocked_refresh_removes_owned_stale_candidates_only(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'output'
            persist_outputs(output,{'ready_for_upload':False},
                {'frozen_current_quote-proposal.json':{'prior':True}})
            (output/'human-notes.txt').write_text('preserve')
            persist_outputs(output,{'blocked':True},{})
            self.assertFalse((output/'frozen_current_quote-proposal.json').exists())
            self.assertEqual((output/'human-notes.txt').read_text(),'preserve')

    def test_stale_candidate_symlink_cannot_delete_external_target(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'output';persist_outputs(output,{}, {})
            target=Path(folder)/'keep';target.write_text('preserve')
            (output/'frozen_current_quote-proposal.json').symlink_to(target)
            with self.assertRaisesRegex(ValueError,'symlink'):
                persist_outputs(output,{}, {})
            self.assertEqual(target.read_text(),'preserve')

    def test_nonempty_unowned_output_directory_is_never_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'output';output.mkdir(mode=0o755)
            (output/'source.json').write_text('unrelated')
            before=output.stat().st_mode
            with self.assertRaisesRegex(ValueError,'unowned_output_directory'):
                persist_outputs(output,{}, {})
            self.assertEqual(output.stat().st_mode,before)
            self.assertEqual((output/'source.json').read_text(),'unrelated')
            self.assertFalse((output/prepare.OUTPUT_MARKER).exists())
            self.assertFalse((output/'readiness.json').exists())

    def test_invalid_marker_is_rejected_before_chmod_or_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'output';output.mkdir(mode=0o755)
            (output/prepare.OUTPUT_MARKER).write_text('{"tool":"some-other-tool"}')
            (output/'source.json').write_text('unrelated')
            before=output.stat().st_mode
            with self.assertRaisesRegex(ValueError,'output_marker_invalid'):
                persist_outputs(output,{}, {})
            self.assertEqual(output.stat().st_mode,before)
            self.assertEqual((output/'source.json').read_text(),'unrelated')

    def test_symlink_marker_cannot_claim_a_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);output=root/'output';output.mkdir(mode=0o755)
            marker=root/'marker.json';marker.write_text(json.dumps(prepare.OUTPUT_IDENTITY))
            (output/prepare.OUTPUT_MARKER).symlink_to(marker)
            before=output.stat().st_mode
            with self.assertRaisesRegex(ValueError,'output_marker_symlink'):
                persist_outputs(output,{}, {})
            self.assertEqual(output.stat().st_mode,before)
            self.assertEqual(marker.read_text(),json.dumps(prepare.OUTPUT_IDENTITY))

    def test_owned_output_can_be_reused_and_marker_remains_private(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'output'
            persist_outputs(output,{'run':1},{'source.json':{'version':1}})
            persist_outputs(output,{'run':2},{'source.json':{'version':2}})
            self.assertEqual(json.loads((output/'source.json').read_text()),{'version':2})
            self.assertEqual(json.loads((output/prepare.OUTPUT_MARKER).read_text()),prepare.OUTPUT_IDENTITY)
            self.assertEqual((output/prepare.OUTPUT_MARKER).stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
