from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

import readback


def tariff():
    def side(value):
        return {'currency':'GBP','energy_charges':{'Year':{'Flat':value}},
            'seasons':{'Year':{'fromMonth':1,'fromDay':1,'toMonth':12,'toDay':31,
                'tou_periods':{'Flat':[{'fromDayOfWeek':0,'toDayOfWeek':6,
                    'fromHour':0,'fromMinute':0,'toHour':0,'toMinute':0}]}}}}
    result=side(.30);result['sell_tariff']=side(.10)
    return result


class ReadbackTests(unittest.TestCase):
    def fixtures(self, *, sites=None, rate=None):
        calls=[]
        def get(path):
            calls.append(path)
            if path=='auth/tesla_scopes':return {'scopes':['energy_device_data','energy_cmds']}
            if path=='api/1/products':return {'response':sites if sites is not None else [
                {'energy_site_id':42,'components':{'battery':True}}]}
            if path.endswith('/site_info'):return {'response':{
                'default_real_mode':'autonomous','backup_reserve_percent':10,
                'private_address':'NEVER_EXPORT'}}
            if path.endswith('/tariff_rate'):return {'response':rate if rate is not None else tariff()}
            raise AssertionError('Unexpected endpoint')
        return get,calls

    def test_only_four_reads_return_private_tariff_and_sanitized_metadata(self):
        get,calls=self.fixtures()
        with patch.dict(os.environ,{'TESLA_ENERGY_SITE_ID':''}),patch.object(readback,'_get',side_effect=get),\
            patch.object(readback,'read_active_mode',return_value={'mode':'self_consumption','mode_source':'fake_gateway'}):
            backup,metadata=readback.read_live_readback()
        self.assertEqual(len(calls),4)
        self.assertTrue(metadata['energy_commands_scope'])
        self.assertEqual(metadata['active_mode'],'self_consumption')
        self.assertEqual(metadata['site_settings']['default_real_mode'],'autonomous')
        self.assertEqual(metadata['control_requests'],0)
        self.assertNotIn('NEVER_EXPORT',json.dumps((backup,metadata)))

    def test_multiple_sites_require_explicit_selection(self):
        sites=[{'energy_site_id':i,'components':{'battery':True}} for i in (42,43)]
        get,calls=self.fixtures(sites=sites)
        with patch.dict(os.environ,{'TESLA_ENERGY_SITE_ID':''}),patch.object(readback,'_get',side_effect=get):
            with self.assertRaisesRegex(ValueError,'selection_required'):readback.read_live_readback()
        self.assertEqual(len(calls),2)

    def test_configured_owned_site_selection(self):
        sites=[{'energy_site_id':i,'components':{'battery':True}} for i in (42,43)]
        get,calls=self.fixtures(sites=sites)
        with patch.dict(os.environ,{'TESLA_ENERGY_SITE_ID':'43'}),patch.object(readback,'_get',side_effect=get),\
            patch.object(readback,'read_active_mode',return_value={'mode':'autonomous','mode_source':'fake_gateway'}):
            backup,_=readback.read_live_readback()
        self.assertEqual(backup['energy_site_id'],43)
        self.assertIn('api/1/energy_sites/43/tariff_rate',calls)

    def test_sensitive_extra_tariff_fields_are_not_exported(self):
        bad=tariff();bad['credentials']={'token':'PRIVATE'}
        get,_=self.fixtures(rate=bad)
        with patch.dict(os.environ,{'TESLA_ENERGY_SITE_ID':''}),patch.object(readback,'_get',side_effect=get):
            with self.assertRaisesRegex(ValueError,'sensitive_tariff') as caught:readback.read_live_readback()
        self.assertNotIn('PRIVATE',str(caught.exception))

    def test_control_or_path_traversal_endpoints_are_rejected_before_auth(self):
        for path in ('api/1/energy_sites/42/operation','api/1/energy_sites/42/../../tariff_rate','https://example.invalid'):
            with patch.object(readback,'read_secret_file') as secret:
                with self.assertRaisesRegex(ValueError,'endpoint_denied'):readback._get(path)
                secret.assert_not_called()

    def test_scope_failure_is_unknown_error_not_false_authority(self):
        with patch.object(readback,'_get',return_value={'scopes':None}):
            with self.assertRaisesRegex(ValueError,'scope_response_invalid'):readback.read_live_readback()


if __name__=='__main__':unittest.main()
