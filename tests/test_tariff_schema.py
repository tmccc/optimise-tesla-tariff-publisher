import copy
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from tariff_schema import (TariffValidationError, canonical_hash, rate_at,
                           semantic_hash, select_installed_tariff, to_upload_tariff, validate_tariff)


def period(start=0, end=0, first=0, last=6):
    return {'fromDayOfWeek': first, 'toDayOfWeek': last,
            'fromHour': start // 60, 'fromMinute': start % 60,
            'toHour': end // 60, 'toMinute': end % 60}


def channel(value='0.3'):
    return {'currency': 'GBP', 'code': 'TEST',
            'demand_charges': {'ALL': {'ALL': 0}, 'YEAR': {}},
            'energy_charges': {'YEAR': {'RATE': value}},
            'seasons': {'YEAR': {'fromMonth': 1, 'fromDay': 1,
                                   'toMonth': 12, 'toDay': 31,
                                   'tou_periods': {'RATE': [period()]}}}}


def tariff():
    result = channel()
    result['sell_tariff'] = channel('0.1')
    return result


def moment(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class SchemaTests(unittest.TestCase):
    def test_full_day_zero_midnight_is_valid(self):
        value = tariff()
        self.assertEqual(validate_tariff(value), [])
        self.assertEqual(rate_at(value, moment('2026-11-01T23:59:59Z')), Decimal('0.3'))
        self.assertEqual(rate_at(value, moment('2026-11-02T00:00:00Z'), 'export'), Decimal('0.1'))

    def test_half_hour_boundary_and_last_midnight(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods'] = {
            'FIRST': [period(0, 30)], 'REST': [period(30, 0)]}
        value['energy_charges']['YEAR'] = {'FIRST': '0.2', 'REST': '0.4'}
        self.assertEqual(validate_tariff(value), [])
        self.assertEqual(rate_at(value, moment('2026-11-01T00:29:59Z')), Decimal('.2'))
        self.assertEqual(rate_at(value, moment('2026-11-01T00:30:00Z')), Decimal('.4'))

    def test_year_wrapping_seasons_are_inclusive(self):
        value = tariff()
        value['seasons'] = {
            'WINTER': {'fromMonth': 12, 'fromDay': 1, 'toMonth': 2, 'toDay': 29,
                       'tou_periods': {'R': [period()]}},
            'REST': {'fromMonth': 3, 'fromDay': 1, 'toMonth': 11, 'toDay': 30,
                     'tou_periods': {'R': [period()]}}}
        value['energy_charges'] = {'WINTER': {'R': '.4'}, 'REST': {'R': '.3'}}
        value['demand_charges'] = {'ALL': {'ALL': 0}}
        self.assertEqual(validate_tariff(value), [])
        self.assertEqual(rate_at(value, moment('2028-02-29T12:00:00Z')), Decimal('.4'))
        self.assertEqual(rate_at(value, moment('2027-02-28T12:00:00Z')), Decimal('.4'))
        self.assertEqual(rate_at(value, moment('2026-03-01T12:00:00Z')), Decimal('.3'))

    def test_missing_leap_day_is_a_gap(self):
        value = tariff()
        a = copy.deepcopy(value['seasons']['YEAR'])
        a.update(toMonth=2, toDay=28)
        b = copy.deepcopy(value['seasons']['YEAR'])
        b.update(fromMonth=3, fromDay=1)
        value['seasons'] = {'A': a, 'B': b}
        value['energy_charges'] = {'A': {'RATE': '.3'}, 'B': {'RATE': '.3'}}
        self.assertIn('import.season_gap', validate_tariff(value))

    def test_season_overlap_is_rejected(self):
        value = tariff()
        value['seasons']['DUPLICATE'] = copy.deepcopy(value['seasons']['YEAR'])
        value['energy_charges']['DUPLICATE'] = {'RATE': '.3'}
        self.assertIn('import.season_overlap', validate_tariff(value))

    def test_tou_gap_is_rejected(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods']['RATE'] = [period(30, 0)]
        self.assertIn('import.tou_gap', validate_tariff(value))

    def test_tou_overlap_even_with_equal_price_is_rejected(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods']['RATE'].append(period(60, 120))
        self.assertIn('import.tou_overlap', validate_tariff(value))

    def test_weekday_gap_is_rejected(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods']['RATE'] = [period(first=0, last=5)]
        self.assertIn('import.tou_gap', validate_tariff(value))
        self.assertIn('import.weekday_specific_unsupported', validate_tariff(value))

    def test_weekday_split_and_wrapped_weekend_are_not_guessed(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods'] = {
            'WEEKDAY': [period(first=1, last=5)],
            'WEEKEND': [period(first=6, last=0)]}
        value['energy_charges']['YEAR'] = {'WEEKDAY': '.4', 'WEEKEND': '.2'}
        self.assertIn('import.weekday_specific_unsupported', validate_tariff(value))
        with self.assertRaises(TariffValidationError):
            rate_at(value, moment('2026-11-01T12:00:00Z'))

    def test_clock_dst_folds_evaluate_equally(self):
        value = tariff()
        first = moment('2026-10-25T00:30:00Z')
        second = moment('2026-10-25T01:30:00Z')
        self.assertEqual(rate_at(value, first), rate_at(value, second))

    def test_spring_dst_uses_local_clock(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods'] = {
            'EARLY': [period(0, 120)], 'LATE': [period(120, 0)]}
        value['energy_charges']['YEAR'] = {'EARLY': '.2', 'LATE': '.4'}
        self.assertEqual(rate_at(value, moment('2026-03-29T00:59:00Z')), Decimal('.2'))
        self.assertEqual(rate_at(value, moment('2026-03-29T01:00:00Z')), Decimal('.4'))

    def test_buy_sell_compatibility_can_be_separated_from_structure(self):
        value = tariff()
        value['energy_charges']['YEAR']['RATE'] = 0
        self.assertEqual(validate_tariff(value, strict_compatibility=False), [])
        self.assertEqual(validate_tariff(value), ['export_exceeds_import'])
        self.assertEqual(rate_at(value, moment('2026-11-01T12:00:00Z')), Decimal(0))

    def test_compatibility_checks_every_day_and_subhour_boundary(self):
        value = tariff()
        value['sell_tariff']['seasons']['YEAR']['tou_periods'] = {
            'LOW': [period(0, 30)], 'HIGH': [period(30, 0)]}
        value['sell_tariff']['energy_charges']['YEAR'] = {'LOW': '.1', 'HIGH': '.4'}
        self.assertIn('export_exceeds_import', validate_tariff(value))

    def test_invalid_prices_are_rejected_without_clamping(self):
        for price in (-1, True, False, float('nan'), float('inf'), 'NaN', 'bad', None, 10001):
            with self.subTest(price=price):
                value = tariff()
                value['energy_charges']['YEAR']['RATE'] = price
                self.assertIn('import.invalid_energy_price', validate_tariff(value))

    def test_missing_and_extra_price_labels(self):
        value = tariff()
        value['energy_charges']['YEAR'] = {'UNUSED': '.3'}
        errors = validate_tariff(value)
        self.assertIn('import.extra_price_label', errors)
        self.assertIn('import.missing_price_label', errors)

    def test_global_flat_energy_rate(self):
        value = tariff()
        value['energy_charges'] = {'ALL': {'ALL': '.3'}}
        self.assertEqual(validate_tariff(value), [])
        self.assertEqual(rate_at(value, moment('2026-11-01T12:00:00Z')), Decimal('.3'))

    def test_season_flat_energy_rate(self):
        value = tariff()
        value['energy_charges'] = {'YEAR': {'ALL': '.3'}}
        self.assertEqual(validate_tariff(value), [])
        self.assertEqual(rate_at(value, moment('2026-11-01T12:00:00Z')), Decimal('.3'))

    def test_zero_global_placeholder_with_season_prices(self):
        value = tariff()
        value['energy_charges']['ALL'] = {'ALL': 0}
        self.assertEqual(validate_tariff(value), [])

    def test_nonzero_global_with_season_prices_is_not_guessed(self):
        value = tariff()
        value['energy_charges']['ALL'] = {'ALL': '.01'}
        self.assertIn('import.ambiguous_charge_composition', validate_tariff(value))

    def test_nonzero_demand_and_daily_charges_block(self):
        for field in ('demand_charges', 'daily_charges'):
            value = tariff()
            value[field] = {'ALL': {'ALL': '.01'}}
            self.assertIn('import.unsupported_' + field, validate_tariff(value))

    def test_invalid_date_and_clock_fields_do_not_raise_arbitrary_errors(self):
        value = tariff()
        value['seasons']['YEAR']['fromDay'] = 32
        self.assertIn('import.invalid_season_date', validate_tariff(value))
        value = tariff()
        value['seasons']['YEAR']['tou_periods']['RATE'][0]['toHour'] = 24
        self.assertIn('import.invalid_tou_range', validate_tariff(value))

    def test_other_overnight_range_is_not_assumed(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods']['RATE'] = [period(1380, 60)]
        self.assertIn('import.invalid_tou_range', validate_tariff(value))

    def test_malformed_and_nested_sell_tariffs(self):
        for bad in (None, [], 'bad', 1):
            value = tariff()
            value['sell_tariff'] = bad
            self.assertIn('export.not_object', validate_tariff(value))
        value = tariff()
        value['sell_tariff']['sell_tariff'] = {}
        self.assertIn('export.nested_sell_tariff', validate_tariff(value))

    def test_bad_input_is_safe(self):
        self.assertEqual(validate_tariff(None), ['export.not_object', 'import.not_object'])
        with self.assertRaisesRegex(ValueError, 'aware_datetime_required'):
            rate_at(tariff(), datetime(2026, 11, 1))
        with self.assertRaisesRegex(ValueError, 'invalid_direction'):
            rate_at(tariff(), datetime.now(timezone.utc), 'bad')
        with self.assertRaises(TariffValidationError):
            rate_at({}, datetime.now(timezone.utc))

    def test_currency_is_not_guessed(self):
        value = tariff()
        value['sell_tariff']['currency'] = 'EUR'
        self.assertIn('export.unsupported_currency', validate_tariff(value))

    def test_canonical_hash_ignores_mapping_order_not_price_changes(self):
        value = tariff()
        reordered = {key: value[key] for key in reversed(value)}
        self.assertEqual(canonical_hash(value), canonical_hash(reordered))
        reordered = copy.deepcopy(value)
        reordered['energy_charges']['YEAR']['RATE'] = '.31'
        self.assertNotEqual(canonical_hash(value), canonical_hash(reordered))
        with self.assertRaises(ValueError):
            canonical_hash({'bad': float('nan')})

    def test_get_and_documented_post_wrappers_are_equivalent(self):
        flat = tariff()
        wrapped = to_upload_tariff(flat)
        self.assertEqual(wrapped['energy_charges']['YEAR'], {'rates': {'RATE': '0.3'}})
        self.assertEqual(wrapped['demand_charges']['ALL'], {'rates': {'ALL': 0}})
        self.assertEqual(wrapped['seasons']['YEAR']['tou_periods']['RATE'],
                         {'periods': [period()]})
        self.assertEqual(validate_tariff(wrapped), [])
        for direction in ('import', 'export'):
            self.assertEqual(rate_at(flat, moment('2026-11-01T12:00:00Z'), direction),
                             rate_at(wrapped, moment('2026-11-01T12:00:00Z'), direction))
        self.assertEqual(semantic_hash(flat), semantic_hash(wrapped))
        self.assertNotEqual(canonical_hash(flat), canonical_hash(wrapped))

    def test_upload_normalization_preserves_metadata_and_does_not_mutate(self):
        value = tariff()
        value['description_metadata'] = {'unrecognized_but_preserved': ['value']}
        value['sell_tariff']['utility'] = 'Test supplier'
        value['seasons']['YEAR']['metadata'] = {'x': True}
        value['seasons']['YEAR']['tou_periods']['RATE'][0]['extra_period_field'] = 'kept'
        original = copy.deepcopy(value)
        wrapped = to_upload_tariff(value)
        self.assertEqual(value, original)
        self.assertEqual(wrapped['version'], 1)
        self.assertEqual(wrapped['description_metadata'], value['description_metadata'])
        self.assertEqual(wrapped['sell_tariff']['utility'], 'Test supplier')
        self.assertEqual(wrapped['seasons']['YEAR']['metadata'], {'x': True})
        self.assertEqual(wrapped['seasons']['YEAR']['tou_periods']['RATE']['periods'][0]['extra_period_field'], 'kept')
        self.assertEqual(to_upload_tariff(wrapped), wrapped)

    def test_mixed_flat_and_nested_charge_rows_are_rejected(self):
        value = tariff()
        value['energy_charges']['YEAR'] = {'rates': {'RATE': '.3'}, 'OTHER': '.2'}
        self.assertIn('import.mixed_charge_formats', validate_tariff(value))
        with self.assertRaises(TariffValidationError):
            to_upload_tariff(value)
        value = tariff()
        value['energy_charges']['ALL'] = {'rates': {'ALL': 0}}
        self.assertIn('import.mixed_charge_formats', validate_tariff(value))

    def test_mixed_flat_and_nested_tou_rows_are_rejected(self):
        value = tariff()
        value['seasons']['YEAR']['tou_periods'] = {
            'A': [period(0, 30)], 'B': {'periods': [period(30, 0)]}}
        value['energy_charges']['YEAR'] = {'A': '.3', 'B': '.3'}
        self.assertIn('import.mixed_tou_formats', validate_tariff(value))

    def test_unknown_wrapper_siblings_are_rejected_not_dropped(self):
        value = to_upload_tariff(tariff())
        value['energy_charges']['YEAR']['extra_metadata'] = {'cannot_interpret': True}
        with self.assertRaises(TariffValidationError):
            to_upload_tariff(value)
        value = to_upload_tariff(tariff())
        value['seasons']['YEAR']['tou_periods']['RATE']['extra_metadata'] = 'unknown'
        with self.assertRaises(TariffValidationError):
            to_upload_tariff(value)

    def test_semantic_hash_ignores_names_and_metadata(self):
        value = tariff()
        renamed = copy.deepcopy(value)
        for side in (renamed, renamed['sell_tariff']):
            side['energy_charges']['RENAMED'] = {'NEW_LABEL': side['energy_charges'].pop('YEAR')['RATE']}
            season = side['seasons'].pop('YEAR')
            season['tou_periods']['NEW_LABEL'] = season['tou_periods'].pop('RATE')
            side['seasons']['RENAMED'] = season
            side['demand_charges'] = {'ALL': {'ALL': 0}}
            side['code'] = 'OTHER_CODE'
            side['name'] = 'Other display name'
        self.assertEqual(semantic_hash(value), semantic_hash(renamed))
        renamed['sell_tariff']['energy_charges']['RENAMED']['NEW_LABEL'] = '.11'
        self.assertNotEqual(semantic_hash(value), semantic_hash(renamed))

    def test_semantic_hash_merges_adjacent_equal_clock_rates(self):
        value = tariff()
        split = copy.deepcopy(value)
        split['seasons']['YEAR']['tou_periods'] = {
            'A': [period(0, 30)], 'B': [period(30, 0)]}
        split['energy_charges']['YEAR'] = {'A': '.30', 'B': '.3000'}
        self.assertEqual(semantic_hash(value), semantic_hash(split))

    def test_semantic_hash_merges_identical_date_schedules(self):
        value = tariff()
        split = copy.deepcopy(value)
        a = copy.deepcopy(split['seasons'].pop('YEAR'))
        b = copy.deepcopy(a)
        a.update(toMonth=6, toDay=30)
        b.update(fromMonth=7, fromDay=1)
        split['seasons'] = {'A': a, 'B': b}
        split['energy_charges'] = {'A': {'RATE': '.3'}, 'B': {'RATE': '.3'}}
        self.assertEqual(semantic_hash(value), semantic_hash(split))
        split['energy_charges']['B']['RATE'] = '.31'
        self.assertNotEqual(semantic_hash(value), semantic_hash(split))

    def test_supported_version_is_explicit_for_upload(self):
        self.assertEqual(to_upload_tariff(tariff())['version'], 1)
        for bad in (0, 2, True, '1', None):
            value = tariff()
            value['version'] = bad
            self.assertIn('import.unsupported_version', validate_tariff(value))


class InstalledTariffSelectionTests(unittest.TestCase):
    def test_saved_flat_configuration_wins_over_stale_rate_projection(self):
        old=tariff();saved=tariff();saved['energy_charges']['YEAR']['RATE']='.4'
        selected,source=select_installed_tariff({'tariff_content':saved},old)
        self.assertEqual(source,'site_info.tariff_content')
        self.assertEqual(semantic_hash(selected),semantic_hash(saved))
        self.assertNotEqual(semantic_hash(selected),semantic_hash(old))

    def test_valid_flat_configuration_wins_over_malformed_v2(self):
        saved=tariff()
        selected,source=select_installed_tariff({'tariff_content':saved,
            'tariff_content_v2':{'seasons':{'unsupported':'shape'}}},tariff())
        self.assertEqual(source,'site_info.tariff_content');self.assertEqual(selected,saved)

    def test_populated_invalid_flat_configuration_cannot_fall_back(self):
        invalid=tariff();invalid['seasons']['YEAR']['tou_periods']['RATE']=[period(30,0)]
        with self.assertRaises(TariffValidationError) as caught:
            select_installed_tariff({'tariff_content':invalid,'tariff_content_v2':tariff()},tariff())
        self.assertIn('installed_tariff_content_invalid',caught.exception.errors)
        self.assertIn('import.tou_gap',caught.exception.errors)

    def test_nonobject_present_configuration_is_not_treated_as_missing(self):
        for invalid in (False,0,[],['value'],''):
            with self.subTest(invalid=invalid),self.assertRaises(TariffValidationError):
                select_installed_tariff({'tariff_content':invalid},tariff())

    def test_v2_used_only_when_flat_is_absent(self):
        wrapped=to_upload_tariff(tariff())
        for absent in (None,{}):
            selected,source=select_installed_tariff({'tariff_content':absent,'tariff_content_v2':wrapped},None)
            self.assertEqual(source,'site_info.tariff_content_v2');self.assertEqual(selected,wrapped)
        with self.assertRaises(TariffValidationError):
            select_installed_tariff({'tariff_content_v2':{'invalid':'shape'}},tariff())

    def test_rate_fallback_requires_valid_complete_structure(self):
        selected,source=select_installed_tariff({},tariff())
        self.assertEqual(source,'tariff_rate');self.assertEqual(selected,tariff())
        for invalid in (None,{},False):
            with self.assertRaises(TariffValidationError):select_installed_tariff({},invalid)
        with self.assertRaises(TariffValidationError):select_installed_tariff(None,tariff())

    def test_read_selection_does_not_mutate_or_grant_write_compatibility(self):
        saved=tariff();saved['energy_charges']['YEAR']['RATE']='0'
        self.assertEqual(validate_tariff(saved),['export_exceeds_import'])
        selected,_source=select_installed_tariff({'tariff_content':saved},None)
        self.assertEqual(validate_tariff(selected),['export_exceeds_import'])
        selected['energy_charges']['YEAR']['RATE']='1'
        self.assertEqual(saved['energy_charges']['YEAR']['RATE'],'0')


if __name__ == '__main__':
    unittest.main()
