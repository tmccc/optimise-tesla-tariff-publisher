"""Execute the literal mode child with synthetic credentials and fake HTTP only."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from auth import secret_reader_source
from reply_metadata import reply_metadata_source
from runtime import REMOTE
from tariff_schema import semantic_hash

UTC=timezone.utc


class ModeTransportTests(unittest.TestCase):
    def run_child(self, *, action='self_powered', cloud_mode='autonomous',
                  reply=None, post_status=200, changed_settings=False, proceed=True,
                  allow_child_failure=False):
        period={'fromDayOfWeek':0,'toDayOfWeek':6,'fromHour':0,'fromMinute':0,
                'toHour':0,'toMinute':0}
        channel={'currency':'GBP','energy_charges':{'Y':{'R':.3}},
                 'seasons':{'Y':{'fromMonth':1,'fromDay':1,'toMonth':12,'toDay':31,
                                 'tou_periods':{'R':[period]}}}}
        tariff=deepcopy(channel);tariff['sell_tariff']=deepcopy(channel)
        preserve={'backup_reserve':10,'grid_charging':'not_reported','export_permission':'battery_ok'}
        expected={'energy_site_id':123,'current_tariff_hash':semantic_hash(tariff),
                  'preserve_settings':preserve}
        calls=[]
        class Response:
            def __init__(self,body,status=200):
                self.status=status;self.bytes=json.dumps(body).encode();self.content=self
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            async def read(self,n):
                first,self.bytes=self.bytes[:n],self.bytes[n:];return first
        class Session:
            def __init__(self,**kwargs):
                if kwargs.get('trust_env') is not False:raise AssertionError('Proxy inherited')
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def request(self,method,url,**kwargs):
                if not url.startswith('https://api.tessie.com/'):raise AssertionError('Wrong host')
                if kwargs.get('allow_redirects') is not False:raise AssertionError('Redirect allowed')
                if kwargs.get('ssl') is False:raise AssertionError('TLS disabled')
                path=url.removeprefix('https://api.tessie.com')
                calls.append((method,path,deepcopy(kwargs.get('json'))))
                if method=='POST':
                    target={'self_powered':'self_consumption','savings':'autonomous'}.get(action)
                    if path!='/api/1/energy_sites/123/operation':raise AssertionError('Wrong command')
                    if kwargs.get('json')!={'default_real_mode':target}:raise AssertionError('Wrong body')
                    return Response({'response':{'result':True}} if reply is None else reply,post_status)
                if method!='GET':raise AssertionError('Wrong method')
                if path=='/auth/tesla_scopes':return Response({'scopes':['energy_cmds']})
                if path=='/api/1/products':
                    return Response({'response':[{'energy_site_id':123,'components':{'battery':True}}]})
                if path.endswith('/tariff_rate'):return Response({'response':tariff})
                if path.endswith('/site_info'):
                    return Response({'response':{'default_real_mode':cloud_mode,
                        'backup_reserve_percent':20 if changed_settings else 10,
                        'components':{'customer_preferred_export_rule':'battery_ok'}}})
                raise AssertionError('Unexpected endpoint')
        module=types.ModuleType('aiohttp');module.ClientSession=Session
        module.ClientTimeout=lambda **kwargs:kwargs
        namespace={'SCHEMA_SOURCE':(Path(__file__).resolve().parents[1]/'tariff_schema.py').read_text(),
            'AUTH_SOURCE':secret_reader_source(),'REPLY_METADATA_SOURCE':reply_metadata_source(),
            'ACTION':action,'EXPECTED':expected,'LIVE_AUTHORIZED':True}
        stdin=io.StringIO(json.dumps({'proceed':proceed,
            'valid_until':(datetime.now(UTC)+timedelta(seconds=4)).isoformat()})+'\n')
        stdout=io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'explicit-synthetic-token'
            token.write_text('SYNTHETIC_AUTH_MUST_NOT_APPEAR');token.chmod(0o600)
            with patch.dict('sys.modules',{'aiohttp':module}),\
                patch.dict(os.environ,{'TESSIE_TOKEN_FILE':str(token)}),\
                patch('sys.stdin',stdin),contextlib.redirect_stdout(stdout):
                try:
                    exec(REMOTE,namespace)
                except SystemExit as failure:
                    if not allow_child_failure or failure.code != 1:
                        raise
        self.assertNotIn('SYNTHETIC_AUTH_MUST_NOT_APPEAR',stdout.getvalue())
        return [json.loads(line) for line in stdout.getvalue().splitlines()],calls

    def test_fallback_sends_one_fixed_field_after_four_reads(self):
        replies,calls=self.run_child()
        self.assertTrue(replies[0]['preflight_ready']);result=replies[-1]
        self.assertTrue(result['accepted']);self.assertEqual(result['mode_http_status'],200)
        self.assertEqual(result['requested_mode'],'self_consumption')
        self.assertIsNotNone(datetime.fromisoformat(result['mode_http_completed_at']).tzinfo)
        self.assertEqual([row[0] for row in calls],['GET']*4+['POST'])
        self.assertEqual(calls[-1][2],{'default_real_mode':'self_consumption'})

    def test_savings_uses_fixed_autonomous_target_with_lagging_cloud_default(self):
        replies,calls=self.run_child(action='savings',cloud_mode='autonomous')
        self.assertEqual(replies[-1]['requested_mode'],'autonomous')
        self.assertEqual(calls[-1][2],{'default_real_mode':'autonomous'})
        self.assertEqual(len([row for row in calls if row[0]=='POST']),1)

    def test_backup_cloud_default_is_readable_for_manual_takeover_detection(self):
        replies,calls=self.run_child(action='read',cloud_mode='backup')
        self.assertEqual(replies[-1]['mode'],'backup')
        self.assertFalse(replies[-1]['command_sent'])
        self.assertEqual([row[0] for row in calls],['GET']*4)

    def test_backup_cloud_default_still_blocks_both_mode_mutations(self):
        for action in ('self_powered','savings'):
            with self.subTest(action=action):
                replies,calls=self.run_child(action=action,cloud_mode='backup',
                    allow_child_failure=True)
                self.assertTrue(replies[-1]['adapter_failed'])
                self.assertFalse(any(row[0]=='POST' for row in calls))

    def test_setting_drift_or_host_denial_sends_no_post(self):
        for options in ({'changed_settings':True},{'proceed':False}):
            replies,calls=self.run_child(**options)
            self.assertFalse(replies[-1]['command_sent'])
            self.assertFalse(any(row[0]=='POST' for row in calls))

    def test_unknown_ack_keeps_completion_and_only_safe_metadata(self):
        replies,_calls=self.run_child(reply={'data':{'status':'queued','message':'PRIVATE_RESULT'}})
        result=replies[-1]
        self.assertIsNone(result['accepted']);self.assertEqual(result['mode_http_status'],200)
        self.assertIsNotNone(result['mode_http_completed_at'])
        self.assertFalse(result['explicit_rejection']);self.assertNotIn('PRIVATE_RESULT',json.dumps(replies))

    def test_known_rejection_and_non200_are_observable_without_raw_response(self):
        replies,_calls=self.run_child(reply={'result':False,'message':'PRIVATE_REJECT'})
        self.assertTrue(replies[-1]['explicit_rejection'])
        self.assertNotIn('PRIVATE_REJECT',json.dumps(replies))
        replies,calls=self.run_child(post_status=503)
        self.assertTrue(replies[-1]['outcome_unknown'])
        self.assertEqual(replies[-1]['mode_http_status'],503)
        self.assertIsNotNone(replies[-1]['mode_http_completed_at'])
        self.assertEqual(len([row for row in calls if row[0]=='POST']),1)
