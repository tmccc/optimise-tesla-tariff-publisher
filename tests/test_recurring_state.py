"""Recurring decisions are tested without files, services or network access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from recurring_state import decide_cycle, proposed_lease_until

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 14, 30, tzinfo=UTC)
OLD, NEW, OTHER = '1' * 64, '2' * 64, '3' * 64
KEEP = {'backup_reserve': 10, 'grid_charging': 'not_reported', 'export_permission': 'battery_ok'}


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def fixture():
    return {
        'source_last_success': stamp(-10), 'source_captured_at': stamp(-5),
        'known_valid_from': stamp(-1800), 'known_valid_until': stamp(86400),
        'candidate_valid': True, 'candidate_prices_verified': True,
        'candidate_tariff_hash': NEW, 'live_last_success': stamp(-2),
        'battery_enrolled': False, 'enrolled_last_success': stamp(-3),
        'energy_commands_granted': True, 'mode': 'autonomous',
        'mode_source': 'local_gateway_TEDAPI', 'energy_site_id': 123,
        'current_tariff_hash': OLD, 'preserve_settings': dict(KEEP),
        'publisher_pid': 2001,
        'ownership': {'verified_publisher_write': True, 'energy_site_id': 123,
            'tariff_semantic_hash': OLD, 'written_at': stamp(-3600), 'last_verified_at': stamp(-30)},
        'lease': {'verified_publisher_write': True, 'publisher_owns_installed_tariff': True,
            'guard_lease_trusted': True, 'energy_site_id': 123, 'tariff_semantic_hash': OLD,
            'issued_at': stamp(-30), 'guard_lease_until': stamp(180)},
        'watchdog': {'continuous': True, 'armed': True, 'pid_alive': True,
            'pid': 2002, 'last_cycle_at': stamp(-5)},
        'policy': {'enabled': True, 'energy_site_id': 123, 'manage_savings': True,
            'resume_after_own_fallback': True, 'bootstrap_from_self_powered': True,
            'fallback_approved': True, 'frozen_assumption_approved': True, 'generation': 'fixture'},
        'journal': {'bootstrap_consumed': True, 'last_controlled_mode': 'autonomous'},
    }


def own_fallback(state):
    state['mode'] = 'self_consumption'
    state['journal']['last_mode_action'] = {'controller': 'watchdog',
        'target': 'self_consumption', 'verified': True, 'verified_at': stamp(-20),
        'energy_site_id': 123, 'tariff_semantic_hash': OLD, 'policy_generation': 'fixture'}


def pending(state, *, installed_target=True, reads=2):
    state['pending'] = {'authorized': True, 'kind': 'tariff', 'energy_site_id': 123,
        'baseline_hash': OLD, 'candidate_hash': NEW, 'created_at': stamp(-60),
        'http_status': 200, 'command_sent': True, 'write_accepted': None,
        'preserve_settings': dict(KEEP), 'matching_readbacks': [
            {'observed_at': stamp(-40 + 10 * index), 'tariff_hash': NEW,
                'energy_site_id': 123, 'preserve_settings': dict(KEEP)} for index in range(reads)]}
    if installed_target:
        state['current_tariff_hash'] = NEW


class RecurringStateTests(unittest.TestCase):
    def decide(self, state):
        return decide_cycle(state, now=NOW)

    def test_changed_valid_quotes_refresh_without_mode_change(self):
        result = self.decide(fixture())
        self.assertEqual(result['action'], 'refresh_tariff')
        self.assertTrue(result['allow_write'])
        self.assertFalse(result['allow_mode_change'])
        self.assertFalse(result['effects_executed'])
        self.assertEqual(result['control_requests'], 0)

    def test_same_semantics_renews_without_another_post(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        result = self.decide(state)
        self.assertEqual(result['action'], 'renew_lease')
        self.assertFalse(result['allow_write'])
        self.assertTrue(result['allow_lease_renewal'])

    def test_expired_lease_can_renew_when_current_quotes_and_owner_reverified(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        state['lease']['guard_lease_until'] = stamp(-60)
        self.assertEqual(self.decide(state)['action'], 'renew_lease')

    def test_self_powered_start_updates_prices_then_lease_then_enables_savings(self):
        state = fixture(); state['mode'] = 'self_consumption'
        state['journal'] = {'bootstrap_consumed': False}
        self.assertEqual(self.decide(state)['action'], 'refresh_tariff')
        state['current_tariff_hash'] = NEW; state['ownership']['tariff_semantic_hash'] = NEW
        state['lease'] = None
        self.assertEqual(self.decide(state)['action'], 'renew_lease')
        state['lease'] = {'verified_publisher_write': True, 'publisher_owns_installed_tariff': True,
            'guard_lease_trusted': True, 'energy_site_id': 123, 'tariff_semantic_hash': NEW,
            'issued_at': stamp(-1), 'guard_lease_until': stamp(180)}
        result = self.decide(state)
        self.assertEqual(result['action'], 'enable_savings')
        self.assertTrue(result['allow_mode_change'])
        self.assertFalse(result['allow_write'])

    def test_self_powered_lease_too_short_is_renewed_before_mode_enable(self):
        state = fixture(); own_fallback(state); state['candidate_tariff_hash'] = OLD
        state['lease']['guard_lease_until'] = stamp(59)
        self.assertEqual(self.decide(state)['action'], 'renew_lease')

    def test_future_invalid_lease_is_replaced_not_used_for_savings(self):
        state = fixture(); own_fallback(state); state['candidate_tariff_hash'] = OLD
        state['lease']['guard_lease_until'] = stamp(3600)
        self.assertEqual(self.decide(state)['action'], 'renew_lease')

    def test_own_verified_fallback_resumes_with_explicit_policy(self):
        state = fixture(); own_fallback(state); state['candidate_tariff_hash'] = OLD
        self.assertEqual(self.decide(state)['action'], 'enable_savings')

    def test_recovery_disabled_leaves_self_powered_without_mode_write(self):
        state = fixture(); own_fallback(state); state['policy']['resume_after_own_fallback'] = False
        self.assertEqual(self.decide(state)['action'], 'pause')
        self.assertFalse(self.decide(state)['allow_mode_change'])

    def test_unverified_or_wrong_generation_fallback_is_not_owned(self):
        for field, value in [('verified', False), ('policy_generation', 'another'),
            ('energy_site_id', 456), ('verified_at', stamp(1)), ('tariff_semantic_hash', None)]:
            with self.subTest(field=field):
                state = fixture(); own_fallback(state)
                state['journal']['last_mode_action'][field] = value
                result = self.decide(state)
                self.assertEqual(result['action'], 'pause')
                self.assertEqual(result['pause_latch']['kind'], 'manual')

    def test_manual_self_powered_choice_is_latched(self):
        state = fixture(); state['mode'] = 'self_consumption'
        result = self.decide(state)
        self.assertEqual(result['action'], 'pause')
        self.assertEqual(result['reason_codes'], ['manual_mode_change'])
        self.assertEqual(result['pause_latch']['kind'], 'manual')

    def test_unconsumed_bootstrap_does_not_override_observed_manual_change(self):
        state = fixture(); state['mode'] = 'self_consumption'
        state['journal']['bootstrap_consumed'] = False
        self.assertEqual(self.decide(state)['reason_codes'], ['manual_mode_change'])

    def test_old_eon_handover_never_clears_itself(self):
        state = fixture(); state['journal']['pause_latch'] = {
            'kind': 'eon', 'reason': 'optimise_battery_enrolled', 'created_at': stamp(-3600)}
        result = self.decide(state)
        self.assertEqual(result['action'], 'handover')
        self.assertEqual(result['pause_latch']['kind'], 'eon')
        self.assertFalse(result['allow_write'])

    def test_manual_pause_never_clears_and_arbitrary_notes_are_not_echoed(self):
        state = fixture(); state['journal']['pause_latch'] = {
            'kind': 'manual', 'reason': 'operator_requested_pause', 'created_at': stamp(-3600),
            'private_note': 'DO_NOT_ECHO'}
        result = self.decide(state)
        self.assertEqual(result['action'], 'pause')
        self.assertNotIn('DO_NOT_ECHO', json.dumps(result))

    def test_unknown_existing_latch_is_fail_closed(self):
        state = fixture(); state['journal']['pause_latch'] = 'unexpected'
        self.assertEqual(self.decide(state)['pause_latch']['kind'], 'manual')

    def test_eon_enrolment_requests_durable_handover(self):
        state = fixture(); state['battery_enrolled'] = True
        result = self.decide(state)
        self.assertEqual(result['action'], 'handover')
        self.assertEqual(result['pause_latch']['kind'], 'eon')
        self.assertFalse(result['allow_mode_change'])

    def test_unknown_or_stale_enrolment_never_forces_a_mode(self):
        for change in ({'battery_enrolled': None}, {'enrolled_last_success': stamp(-2700)},
                       {'enrolled_last_success': stamp(1)}):
            state = fixture(); state.update(change); state['source_read_failed'] = True
            result = self.decide(state)
            self.assertEqual(result['action'], 'pause')
            self.assertFalse(result['allow_mode_change'])
            self.assertIsNone(result['pause_latch'])

    def test_cloud_saved_mode_cannot_be_active_authority(self):
        state = fixture(); state['mode_source'] = 'cloud_default_real_mode'
        self.assertEqual(self.decide(state)['reason_codes'], ['active_live_observation_unavailable'])

    def test_stale_or_future_live_snapshot_cannot_control(self):
        for value in (stamp(-300), stamp(1), None):
            state = fixture(); state['live_last_success'] = value
            self.assertFalse(self.decide(state)['allow_write'])
            self.assertFalse(self.decide(state)['allow_mode_change'])

    def test_backup_or_special_mode_is_respected(self):
        state = fixture(); state['mode'] = 'backup'
        result = self.decide(state)
        self.assertEqual(result['pause_latch']['kind'], 'manual')
        self.assertFalse(result['allow_mode_change'])

    def test_wrong_site_and_external_tariff_are_manual_takeover(self):
        state = fixture(); state['energy_site_id'] = 456
        self.assertEqual(self.decide(state)['reason_codes'], ['energy_site_identity_mismatch'])
        state = fixture(); state['current_tariff_hash'] = OTHER
        self.assertEqual(self.decide(state)['reason_codes'], ['external_tariff_change'])

    def test_current_protected_settings_are_not_pinned_to_historic_owner_values(self):
        state = fixture(); state['ownership']['preserve_settings'] = dict(KEEP)
        state['preserve_settings'] = dict(KEEP, backup_reserve=50)
        self.assertEqual(self.decide(state)['action'], 'refresh_tariff')

    def test_unobserved_protected_settings_or_permission_blocks(self):
        for change in ({'preserve_settings': None}, {'energy_commands_granted': False}):
            state = fixture(); state.update(change)
            self.assertFalse(self.decide(state)['allow_write'])
            self.assertFalse(self.decide(state)['allow_mode_change'])

    def test_malformed_protected_numbers_fail_closed_without_overflow(self):
        for value in (True, float('inf'), float('nan'), 10**1000, -1, 101):
            state = fixture(); state['preserve_settings']['backup_reserve'] = value
            self.assertEqual(self.decide(state)['action'], 'pause')
            self.assertFalse(self.decide(state)['allow_mode_change'])

    def test_stale_price_source_falls_back_only_on_verified_owned_auto(self):
        state = fixture(); state['source_last_success'] = stamp(-900)
        result = self.decide(state)
        self.assertEqual(result['action'], 'safe_fallback')
        self.assertTrue(result['allow_mode_change'])
        state['ownership'] = {}
        self.assertFalse(self.decide(state)['allow_mode_change'])

    def test_read_failure_or_invalid_candidate_expires_owned_control(self):
        for change in ({'source_read_failed': True}, {'candidate_valid': False},
                       {'candidate_prices_verified': False}, {'known_valid_until': stamp(0)}):
            state = fixture(); state.update(change)
            result = self.decide(state)
            self.assertEqual(result['action'], 'safe_fallback')
            self.assertFalse(result['allow_write'])

    def test_watchdog_loss_causes_owned_fallback_even_with_good_prices(self):
        for field, value in [('pid_alive', False), ('armed', False),
            ('continuous', False), ('last_cycle_at', stamp(-90)), ('pid', 2001)]:
            with self.subTest(field=field):
                state = fixture(); state['watchdog'][field] = value
                self.assertEqual(self.decide(state)['action'], 'safe_fallback')

    def test_self_powered_waits_for_source_and_watchdog_without_new_mode(self):
        state = fixture(); own_fallback(state); state['source_read_failed'] = True
        result = self.decide(state)
        self.assertEqual(result['action'], 'pause')
        self.assertIsNone(result['pause_latch'])

    def test_disabled_policy_cannot_send_anything(self):
        state = fixture(); state['policy']['enabled'] = False
        self.assertEqual(self.decide(state)['reason_codes'], ['recurring_policy_disabled'])

    def test_design_approvals_must_be_actual_booleans(self):
        state = fixture(); state['policy']['frozen_assumption_approved'] = 'true'
        self.assertEqual(self.decide(state)['reason_codes'], ['design_approval_missing'])

    def test_unowned_self_powered_initial_baseline_requires_explicit_hash_pin(self):
        state = fixture(); state['mode'] = 'self_consumption'
        state['ownership'] = {}; state['journal'] = {'bootstrap_consumed': False}
        self.assertEqual(self.decide(state)['action'], 'pause')
        state['policy']['bootstrap_baseline_hash'] = OLD
        self.assertEqual(self.decide(state)['action'], 'refresh_tariff')

    def test_equal_unowned_candidate_cannot_be_adopted_without_a_verified_write(self):
        state = fixture(); state['mode'] = 'self_consumption'; state['candidate_tariff_hash'] = OLD
        state['ownership'] = {}; state['journal'] = {'bootstrap_consumed': False}
        state['policy']['bootstrap_baseline_hash'] = OLD
        result = self.decide(state)
        self.assertEqual(result['action'], 'pause')
        self.assertFalse(result['allow_lease_renewal'])

    def test_pending_target_two_reads_reconciles_without_a_new_post(self):
        state = fixture(); pending(state)
        result = self.decide(state)
        self.assertEqual(result['action'], 'reconcile_pending')
        self.assertTrue(result['pending_requires_resolution'])
        self.assertFalse(result['allow_write'])
        self.assertFalse(result['allow_mode_change'])

    def test_pending_reboot_recovery_can_identify_owner_with_expired_original_source(self):
        state = fixture(); pending(state)
        state['source_last_success'] = stamp(-86400)
        state['source_captured_at'] = stamp(-86400)
        result = self.decide(state)
        self.assertEqual(result['action'], 'reconcile_pending')
        self.assertTrue(result['expired_recovery_only'])
        self.assertFalse(result['allow_lease_renewal'])

    def test_pending_target_needs_two_fresh_ordered_independent_reads(self):
        for change in ('one', 'stale', 'same_time', 'settings', 'no_http200'):
            with self.subTest(change=change):
                state = fixture(); pending(state)
                record = state['pending']
                if change == 'one':record['matching_readbacks'] = record['matching_readbacks'][:1]
                elif change == 'stale':record['matching_readbacks'][0]['observed_at'] = stamp(-301)
                elif change == 'same_time':record['matching_readbacks'][1]['observed_at'] = record['matching_readbacks'][0]['observed_at']
                elif change == 'settings':record['matching_readbacks'][1]['preserve_settings']['backup_reserve'] = 50
                elif change == 'no_http200':record['http_status'] = None
                result = self.decide(state)
                self.assertEqual(result['action'], 'pause')
                self.assertFalse(result['allow_write'])

    def test_pending_old_baseline_never_triggers_an_automatic_resend(self):
        state = fixture(); pending(state, installed_target=False)
        result = self.decide(state)
        self.assertEqual(result['action'], 'pause')
        self.assertIn('no_resend', result['reason_codes'][0])

    def test_unresolved_pending_old_owned_auto_can_fall_back_on_expiry(self):
        state = fixture(); pending(state, installed_target=False)
        state['lease']['guard_lease_until'] = stamp(-1)
        self.assertEqual(self.decide(state)['action'], 'safe_fallback')

    def test_pending_unrelated_hash_or_explicit_rejection_latches_for_review(self):
        state = fixture(); pending(state); state['current_tariff_hash'] = OTHER
        self.assertEqual(self.decide(state)['pause_latch']['kind'], 'manual')
        state = fixture(); pending(state); state['pending']['write_accepted'] = False
        self.assertEqual(self.decide(state)['pause_latch']['kind'], 'manual')

    def test_five_minute_lease_clips_all_source_and_live_windows(self):
        state = fixture()
        self.assertEqual(proposed_lease_until(state, now=NOW), stamp(298))
        state['live_last_success'] = stamp(0); state['source_last_success'] = stamp(-800)
        state['source_captured_at'] = stamp(-799)
        self.assertEqual(proposed_lease_until(state, now=NOW), stamp(100))

    def test_insufficient_lease_headroom_stops_auto_but_does_not_enable_self_powered(self):
        state = fixture(); state['source_last_success'] = stamp(-880)
        self.assertIsNone(proposed_lease_until(state, now=NOW))
        self.assertEqual(self.decide(state)['action'], 'safe_fallback')
        own_fallback(state)
        self.assertEqual(self.decide(state)['action'], 'pause')

    def test_unknown_mode_and_invalid_clock_fail_closed(self):
        state = fixture(); state['mode'] = None
        self.assertEqual(self.decide(state)['action'], 'pause')
        with self.assertRaisesRegex(ValueError, 'aware'):
            decide_cycle(state, now=NOW.replace(tzinfo=None))

    def test_input_is_not_mutated(self):
        state = fixture(); pending(state); before = deepcopy(state)
        self.decide(state)
        self.assertEqual(state, before)


if __name__ == '__main__':unittest.main()
