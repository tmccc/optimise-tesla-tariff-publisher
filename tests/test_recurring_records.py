"""Recurring leases and mode journals need observed proof, not optimistic ACKs."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest

from recurring_records import commit_lease, record_mode_observation
from recurring_state import decide_cycle
from tests.test_recurring_state import NOW, OLD, NEW, KEEP, fixture, own_fallback, pending, stamp


class RecurringRecordTests(unittest.TestCase):
    def test_same_tariff_renewal_has_no_new_post_or_new_written_timestamp(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        original = deepcopy(state)
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertEqual(result['ownership']['written_at'], stamp(-3600))
        self.assertEqual(result['ownership']['publication_kind'], 'verified_owned_lease_renewal')
        self.assertTrue(result['ownership']['verification_evidence']['renewal_without_post'])
        self.assertEqual(result['control_requests'], 0)
        self.assertEqual(result['lease']['guard_lease_until'], stamp(298))
        self.assertFalse(result['application_acknowledgment_known'])
        self.assertEqual(state, original)

    def test_self_powered_receives_lease_before_savings_is_enabled(self):
        state = fixture(); own_fallback(state); state['candidate_tariff_hash'] = OLD
        state['lease'] = None
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        state['lease'] = result['lease']; state['ownership'] = result['ownership']
        self.assertEqual(decide_cycle(state, now=NOW)['action'], 'enable_savings')
        self.assertTrue(commit_lease(state, now=NOW)['verified'])

    def test_renewal_source_metadata_and_policy_generation_are_recorded(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        result = commit_lease(state, now=NOW)
        self.assertEqual(result['lease']['policy_generation'], 'fixture')
        self.assertEqual(result['ownership']['source']['source_last_success'], stamp(-10))
        self.assertEqual(result['lease']['known_valid_until'], stamp(86400))

    def test_current_reserve_is_preserved_without_freezing_original_reserve(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        state['ownership']['preserve_settings'] = dict(KEEP)
        state['preserve_settings']['backup_reserve'] = 50
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertEqual(result['lease']['preserve_settings']['backup_reserve'], 50)

    def test_changed_candidate_cannot_be_renewed_without_a_write(self):
        result = commit_lease(fixture(), now=NOW)
        self.assertFalse(result['verified'])
        self.assertIsNone(result['lease'])

    def test_unknown_owner_cannot_be_adopted_by_no_post_renewal(self):
        state = fixture(); state['ownership'] = None; state['candidate_tariff_hash'] = OLD
        self.assertFalse(commit_lease(state, now=NOW)['verified'])

    def test_disabled_policy_or_latch_cannot_issue_a_lease(self):
        for mutation in ('disabled', 'manual', 'eon'):
            state = fixture(); state['candidate_tariff_hash'] = OLD
            if mutation == 'disabled':state['policy']['enabled'] = False
            else:state['journal']['pause_latch'] = {'kind': mutation}
            self.assertFalse(commit_lease(state, now=NOW)['verified'])

    def test_post_publication_requires_pending_proof(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        for kind in ('publication', 'reconciliation', 'arbitrary'):
            self.assertFalse(commit_lease(state, now=NOW, publication_kind=kind)['verified'])

    def test_http200_unknown_acknowledgment_remains_unknown_with_two_readbacks(self):
        state = fixture(); pending(state)
        result = commit_lease(state, now=NOW, publication_kind='publication')
        self.assertTrue(result['verified'])
        self.assertFalse(result['application_acknowledgment_known'])
        self.assertEqual(result['ownership']['publication_kind'], 'verified_pending_transition')
        self.assertEqual(result['ownership']['written_at'], stamp(-40))
        self.assertTrue(result['ownership']['written_at_is_upper_bound'])
        self.assertEqual(result['lease']['guard_lease_until'], stamp(240))
        self.assertFalse(result['recovery_only'])

    def test_acknowledged_write_timestamp_must_precede_observed_transition(self):
        state = fixture(); pending(state)
        state['pending']['write_accepted'] = True
        state['pending']['http_completed_at'] = stamp(-50)
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['application_acknowledgment_known'])
        self.assertEqual(result['ownership']['written_at'], stamp(-50))
        self.assertFalse(result['ownership']['written_at_is_upper_bound'])
        state['pending']['http_completed_at'] = stamp(-20)
        result = commit_lease(state, now=NOW)
        self.assertEqual(result['ownership']['written_at'], stamp(-40))
        self.assertTrue(result['ownership']['written_at_is_upper_bound'])

    def test_pending_argument_overrides_state_without_mutating_it(self):
        state = fixture(); pending(state); intent = state.pop('pending')
        original = deepcopy(state)
        self.assertTrue(commit_lease(state, now=NOW, pending=intent)['verified'])
        self.assertEqual(state, original)

    def test_pending_target_needs_real_two_ordered_fresh_reads_and_http200(self):
        for mutation in ('one_read', 'not200', 'settings', 'different_hash', 'rejected'):
            state = fixture(); pending(state); intent = state['pending']
            if mutation == 'one_read':intent['matching_readbacks'].pop()
            elif mutation == 'not200':intent['http_status'] = None
            elif mutation == 'settings':intent['matching_readbacks'][1]['preserve_settings']['backup_reserve'] = 20
            elif mutation == 'different_hash':state['current_tariff_hash'] = '3' * 64
            else:intent['write_accepted'] = False
            self.assertFalse(commit_lease(state, now=NOW)['verified'])

    def test_old_pending_is_identified_but_cannot_receive_a_new_five_minutes(self):
        state = fixture(); pending(state)
        state['pending']['created_at'] = stamp(-86400)
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertTrue(result['recovery_only'])
        self.assertEqual(result['lease']['guard_lease_until'], stamp(-86100))

    def test_stale_price_source_allows_ownership_recovery_only(self):
        state = fixture(); pending(state)
        state['source_last_success'] = stamp(-86400)
        state['source_captured_at'] = stamp(-86400)
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertTrue(result['recovery_only'])
        self.assertEqual(result['lease']['guard_lease_until'], stamp(0))
        state.pop('pending'); state['ownership'] = result['ownership']; state['lease'] = result['lease']
        self.assertEqual(decide_cycle(state, now=NOW)['action'], 'safe_fallback')

    def test_invalid_source_dates_do_not_invent_fresh_metadata(self):
        state = fixture(); pending(state); state['known_valid_until'] = None
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertIsNone(result['lease']['known_valid_until'])
        self.assertTrue(result['recovery_only'])

    def test_pending_target_no_longer_current_candidate_receives_expired_lease(self):
        state = fixture(); pending(state); state['candidate_tariff_hash'] = '3' * 64
        result = commit_lease(state, now=NOW)
        self.assertTrue(result['verified'])
        self.assertTrue(result['recovery_only'])
        self.assertEqual(result['lease']['tariff_semantic_hash'], NEW)
        self.assertEqual(result['lease']['guard_lease_until'], stamp(0))

    def test_recovered_write_needs_healthy_watchdog_and_approvals_for_live_lease(self):
        for mutation in ('watchdog', 'fallback', 'assumption'):
            state = fixture(); pending(state)
            if mutation == 'watchdog':state['watchdog']['pid_alive'] = False
            elif mutation == 'fallback':state['policy']['fallback_approved'] = False
            else:state['policy']['frozen_assumption_approved'] = False
            result = commit_lease(state, now=NOW)
            self.assertTrue(result['verified'])
            self.assertTrue(result['recovery_only'])
            self.assertEqual(result['lease']['guard_lease_until'], stamp(0))

    def test_pending_proof_without_current_publisher_identity_cannot_issue_lease(self):
        state = fixture(); pending(state); state.pop('publisher_pid')
        self.assertFalse(commit_lease(state, now=NOW)['verified'])

    def test_not_freshly_unenrolled_cannot_even_reconcile_a_live_control_lease(self):
        state = fixture(); pending(state); state['battery_enrolled'] = None
        self.assertFalse(commit_lease(state, now=NOW)['verified'])

    def test_renewal_expiry_is_clipped_to_shortest_quote_or_source_window(self):
        state = fixture(); state['candidate_tariff_hash'] = OLD
        state['known_valid_until'] = stamp(70)
        self.assertEqual(commit_lease(state, now=NOW)['lease']['guard_lease_until'], stamp(70))
        state['source_last_success'] = stamp(-850)
        self.assertEqual(commit_lease(state, now=NOW)['lease']['guard_lease_until'], stamp(50))

    def test_records_are_json_serializable(self):
        state = fixture(); pending(state)
        state['pending']['matching_readbacks'][0]['observed_at'] = NOW - timedelta(seconds=40)
        json.dumps(commit_lease(state, now=NOW))


def mode_fixture(target='self_consumption', controller='watchdog'):
    snapshot = fixture()
    snapshot['mode'] = target; snapshot['observed_at'] = stamp(-2)
    intent = {'authorized': True, 'durable': True, 'dispatch_started': True,
        'command_sent': True, 'controller': controller, 'target': target,
        'from_mode': 'autonomous' if target == 'self_consumption' else 'self_consumption',
        'energy_site_id': 123, 'tariff_semantic_hash': OLD, 'policy_generation': 'fixture',
        'created_at': stamp(-20), 'preserve_settings': dict(KEEP)}
    return snapshot['journal'], intent, snapshot, snapshot['policy']


class ModeObservationTests(unittest.TestCase):
    def record(self, journal, intent, snapshot, policy):
        return record_mode_observation(journal, intent, snapshot, NOW, policy)

    def test_verified_gateway_fallback_is_recorded_and_journal_preserved(self):
        journal, intent, snapshot, policy = mode_fixture()
        original = deepcopy((journal, intent, snapshot, policy))
        journal['pause_latch'] = {'kind': 'manual', 'reason': 'operator_requested_pause'}
        result = self.record(journal, intent, snapshot, policy)
        self.assertTrue(result['verified'])
        updated = result['journal']
        self.assertEqual(updated['last_controlled_mode'], 'self_consumption')
        self.assertEqual(updated['last_mode_action']['controller'], 'watchdog')
        self.assertEqual(updated['pause_latch'], journal['pause_latch'])
        self.assertEqual(result['control_requests'], 0)
        journal.pop('pause_latch')
        self.assertEqual((journal, intent, snapshot, policy), original)

    def test_savings_confirmation_consumes_initial_bootstrap(self):
        fields = mode_fixture('autonomous', 'publisher')
        fields[0]['bootstrap_consumed'] = False
        result = self.record(*fields)
        self.assertTrue(result['verified'])
        self.assertTrue(result['journal']['bootstrap_consumed'])

    def test_manual_observed_change_without_durable_dispatch_is_not_owned(self):
        for field in ('authorized', 'durable', 'dispatch_started'):
            fields = mode_fixture(); fields[1][field] = False
            self.assertFalse(self.record(*fields)['verified'])

    def test_network_uncertain_dispatch_can_be_verified_without_fabricating_ack(self):
        fields = mode_fixture(); fields[1]['command_sent'] = None
        result = self.record(*fields)
        self.assertTrue(result['verified'])
        self.assertIsNone(result['journal']['last_mode_action']['command_sent'])
        self.assertFalse(result['journal']['last_mode_action']['application_acknowledgment_known'])
        fields[1]['command_sent'] = False
        self.assertFalse(self.record(*fields)['verified'])

    def test_explicitly_rejected_command_cannot_own_later_manual_matching_mode(self):
        fields = mode_fixture(); fields[1]['write_accepted'] = False
        self.assertFalse(self.record(*fields)['verified'])

    def test_only_publisher_may_enable_savings(self):
        fields = mode_fixture('autonomous', 'watchdog')
        self.assertFalse(self.record(*fields)['verified'])
        fields = mode_fixture('autonomous', 'publisher'); fields[3]['manage_savings'] = False
        self.assertFalse(self.record(*fields)['verified'])

    def test_disabled_policy_does_not_erase_previously_sent_fallback_evidence(self):
        fields = mode_fixture(); fields[3]['enabled'] = False
        self.assertTrue(self.record(*fields)['verified'])

    def test_wrong_site_hash_policy_or_settings_never_adopts_mode(self):
        for field, value in [('energy_site_id', 456), ('current_tariff_hash', NEW),
            ('mode', 'autonomous'), ('mode_source', 'cloud_default_real_mode'),
            ('battery_enrolled', True), ('energy_commands_granted', False),
            ('live_last_success', stamp(-300)), ('observed_at', stamp(-21)),
            ('enrolled_last_success', stamp(-2700))]:
            fields = mode_fixture(); fields[2][field] = value
            self.assertFalse(self.record(*fields)['verified'], field)
        fields = mode_fixture(); fields[1]['policy_generation'] = 'another'
        self.assertFalse(self.record(*fields)['verified'])
        fields = mode_fixture(); fields[2]['preserve_settings']['backup_reserve'] = 50
        self.assertFalse(self.record(*fields)['verified'])

    def test_observed_matching_mode_cannot_adopt_an_intent_for_same_starting_mode(self):
        fields = mode_fixture(); fields[1]['from_mode'] = fields[1]['target']
        self.assertFalse(self.record(*fields)['verified'])

    def test_mode_journal_can_authorise_later_explicit_own_fallback_recovery(self):
        fields = mode_fixture(); result = self.record(*fields)
        state = fields[2]; state['candidate_tariff_hash'] = OLD
        state['journal'] = result['journal']
        self.assertEqual(decide_cycle(state, now=NOW)['action'], 'enable_savings')

    def test_invalid_clocks_are_rejected_without_side_effects(self):
        with self.assertRaises(ValueError):commit_lease(fixture(), now=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            fields = mode_fixture(); record_mode_observation(*fields[:3], NOW.replace(tzinfo=None), fields[3])


if __name__ == '__main__':unittest.main()
