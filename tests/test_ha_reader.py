from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch

import ha_reader

UTC=timezone.utc


def states():
    now=datetime.now(UTC)-timedelta(seconds=2)
    period={'valid_from':now.replace(minute=0,second=0,microsecond=0).isoformat(),
        'valid_to':(now.replace(minute=0,second=0,microsecond=0)+timedelta(minutes=30)).isoformat(),
        'value':30,'quality':'current','estimate':False,'interval_basis':'provisional_30m'}
    def get(key):
        if key in ('import','export'):
            attrs={'unit_of_measurement':'p/kWh','currency':'GBP','forecast':[],
                'price_basis':'import_cost' if key=='import' else 'export_earnings',
                'period':dict(period,value=30 if key=='import' else 20)}
            return {'entity_id':ha_reader._entity(key),'state':'30' if key=='import' else '20','attributes':attrs}
        if key=='battery_enrolled':return {'state':'off'}
        return {'state':now.isoformat()}
    return get


class ReaderTests(unittest.TestCase):
    def test_prices_are_projected_without_second_export_negation(self):
        with patch.object(ha_reader,'_state',side_effect=states()):
            source=ha_reader.read_live_source()
        self.assertEqual(source['source_validation_errors'],[])
        self.assertEqual(source['channels']['export'][0]['value'],20)
        self.assertEqual(source['unit'],'p/kWh')
        self.assertNotIn('now',source)

    def test_bad_metadata_and_unavailable_values_are_not_accepted(self):
        original=states()
        def bad(key):
            row=original(key)
            if key=='import':row['state']='unavailable';row['attributes']['currency']='USD'
            return row
        with patch.object(ha_reader,'_state',side_effect=bad):source=ha_reader.read_live_source()
        self.assertIn('import_metadata_or_availability_invalid',source['source_validation_errors'])
        self.assertIn('import_state_value_invalid',source['source_validation_errors'])

    def test_customer_adjusted_entity_cannot_be_discounted_again(self):
        original=states()
        def adjusted(key):
            row=original(key)
            if key=='import':row['attributes']['customer_pricing_rule']='existing-adjustment'
            return row
        with patch.object(ha_reader,'_state',side_effect=adjusted):source=ha_reader.read_live_source()
        self.assertIn('import_customer_adjusted_source_rejected',source['source_validation_errors'])
        self.assertEqual(source['price_basis'],'raw_eon_optimise_api')

    def test_enrollment_unknown_does_not_become_false(self):
        original=states()
        def unknown(key):return {'state':'unavailable'} if key=='battery_enrolled' else original(key)
        with patch.object(ha_reader,'_state',side_effect=unknown):result=ha_reader.read_enrollment()
        self.assertIsNone(result['battery_enrolled'])

    def test_explicit_off_with_fresh_timestamp_is_known(self):
        with patch.object(ha_reader,'_state',side_effect=states()):result=ha_reader.read_enrollment()
        self.assertIs(result['battery_enrolled'],False)

    def test_stale_enrollment_does_not_become_known_false(self):
        def old(key):
            return {'state':'off'} if key=='battery_enrolled' else {
                'state':(datetime.now(UTC)-timedelta(minutes=46)).isoformat()}
        with patch.object(ha_reader,'_state',side_effect=old):result=ha_reader.read_enrollment()
        self.assertIsNone(result['battery_enrolled'])

    def test_refresh_requires_explicit_additional_opt_in(self):
        with patch.dict(os.environ,{'HASS_ALLOW_STATUS_REFRESH':'0'}),patch.object(ha_reader,'_request') as send:
            with self.assertRaisesRegex(ValueError,'explicit_telemetry'):ha_reader.read_enrollment(refresh=True)
        send.assert_not_called()

    def test_refresh_that_does_not_advance_timestamp_remains_unknown(self):
        with patch.dict(os.environ,{'HASS_ALLOW_STATUS_REFRESH':'1'}),\
            patch.object(ha_reader,'_request') as send,patch.object(ha_reader,'_state',side_effect=states()):
            result=ha_reader.read_enrollment(refresh=True)
        self.assertIsNone(result['battery_enrolled'])
        send.assert_called_once_with('/api/services/homeassistant/update_entity',
            refresh_entity='binary_sensor.optimise_battery_enrolled')

    def test_configured_origin_rejects_credentials_or_cleartext_remote(self):
        for url in ('http://example.invalid:8123','https://user:PRIVATE@example.invalid','https://example.invalid/path'):
            with self.subTest(url=url),patch.dict(os.environ,{'HASS_URL':url}):
                with self.assertRaisesRegex(ValueError,'home_assistant_url_invalid') as caught:ha_reader._base_url()
                self.assertNotIn('PRIVATE',str(caught.exception))

    def test_configured_entity_cannot_change_domain_or_path(self):
        with patch.dict(os.environ,{'HASS_BATTERY_ENROLLED_ENTITY':'switch.anything'}):
            with self.assertRaisesRegex(ValueError,'configured_entity'):ha_reader._entity('battery_enrolled')

    def test_arbitrary_period_values_are_removed(self):
        row=ha_reader._period({'valid_from':'not timestamp','value':True,'token':'PRIVATE'})
        self.assertIsNone(row['valid_from']);self.assertIsNone(row['value'])
        self.assertNotIn('PRIVATE',json.dumps(row));self.assertNotIn('token',row)


if __name__=='__main__':unittest.main()
