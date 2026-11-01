import ast
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from date_compiler import build_date_candidate
from tariff_schema import semantic_hash
from writer import REMOTE, TariffWriter, publisher_policy_enabled, validate_candidate
from auth import secret_reader_source
from reply_metadata import reply_metadata_source

UTC=timezone.utc
NOW=datetime(2026,11,1,12,5,tzinfo=UTC)
OLD='1'*64
KEEP={'backup_reserve':10,'grid_charging':'not_reported','export_permission':'battery_ok'}


def data():
    season={'fromMonth':1,'fromDay':1,'toMonth':12,'toDay':31,'tou_periods':{
        'R':[{'fromDayOfWeek':0,'toDayOfWeek':6,'fromHour':0,'fromMinute':0,
              'toHour':0,'toMinute':0}]}}
    baseline={'currency':'GBP','energy_charges':{'Y':{'R':.3}},'seasons':{'Y':deepcopy(season)}}
    baseline['sell_tariff']={'currency':'GBP','energy_charges':{'Y':{'R':.1}},'seasons':{'Y':deepcopy(season)}}
    start=NOW.replace(minute=0)
    rows=[]
    for i,(buy,sell) in enumerate((('.3','.1'),('.4','.2'),('.35','.15'))):
        first=start+timedelta(minutes=30*i)
        rows.append({'valid_from':first.isoformat(),'valid_to':(first+timedelta(minutes=30)).isoformat(),
            'import_gbp_per_kwh':buy,'export_gbp_per_kwh':sell})
    preview={'valid':True,'errors':[],'currency':'GBP','timezone':'Europe/London',
        'source_last_success':(NOW-timedelta(seconds=5)).isoformat(),
        'source_captured_at':(NOW-timedelta(seconds=1)).isoformat(),'periods':rows}
    candidate=build_date_candidate(preview,baseline,now=NOW,unquoted_policy='frozen_current_quote')
    assert candidate['candidate_valid'],candidate['errors']
    return candidate['tariff'],candidate['manifest']


class WriterTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.policy=self.root/'publisher-policy.json'
        self.fallback=self.root/'execution-policy.json'
        self.candidate,self.manifest=data()
        self.candidate_hash=semantic_hash(self.candidate)
        self.calls=[]
        self.eon={'battery_enrolled':False,'enrolled_last_success':NOW.isoformat()}
        self.authorized=True
        self.reply={}
        self.write(self.policy,{'live_execution_enabled':False,'allow_tariff_writes':False,
            'one_shot_only':True,'unquoted_policy':'frozen_current_quote',
            'unquoted_planning_assumption_approved':True,'fallback_design_approved':True,
            'fallback_policy':'self_powered','approved_candidate_semantic_hash':self.candidate_hash,
            'expected_installed_semantic_hash':OLD,'energy_site_id':123})
        self.write(self.fallback,{'live_execution_enabled':False,'fallback_design_approved':True,
            'allowed_mode':'self_consumption','allow_tariff_writes':False})

    def write(self,path,value):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as h:json.dump(value,h)

    def arm(self):
        value=json.loads(self.policy.read_text())
        value.update(live_execution_enabled=True,allow_tariff_writes=True)
        self.write(self.policy,value)
        value=json.loads(self.fallback.read_text());value['live_execution_enabled']=True
        self.write(self.fallback,value)

    def ready(self,context):
        return {'energy_site_id':123,'current_tariff_hash':OLD,
            'candidate_tariff_hash':context['candidate_tariff_hash'],
            'mode':'autonomous','preserve_settings':dict(KEEP),
            'energy_commands_granted':True,'ready_at':NOW.isoformat(),
            'live_last_success':NOW.isoformat(),'preflight_ready':True}

    def transport(self,context,confirm):
        self.calls.append('preflight')
        if not confirm(self.ready(context)):
            return {'command_sent':False,'reason':'final_authorization_denied'}
        self.calls.append('fake_post_only')
        result={'command_sent':True,'control_requests':1,'write_accepted':True,
            'energy_site_id':123,'current_tariff_hash':context['candidate_tariff_hash'],
            'mode':'autonomous','preserve_settings':dict(KEEP),
            'energy_commands_granted':True,'live_last_success':NOW.isoformat()}
        result.update(self.reply)
        return result

    def make(self,**kw):
        return TariffWriter(policy_path=self.policy,fallback_policy_path=self.fallback,
            transport=kw.pop('transport',self.transport),
            enrollment_refresher=kw.pop('enrollment_refresher',lambda:dict(self.eon)),
            final_authorizer=kw.pop('final_authorizer',lambda context:self.authorized),
            clock=kw.pop('clock',lambda:NOW),**kw)

    def publish(self,writer=None,manifest=None,candidate=None,**kw):
        return (writer or self.make()).publish(candidate or self.candidate,
            manifest=self.manifest if manifest is None else manifest,
            expected_site_id=kw.get('site',123),expected_installed_hash=kw.get('old',OLD),
            preserve_settings=kw.get('keep',KEEP),operation_mode=kw.get('operation_mode','autonomous'))

    def test_disabled_policy_has_zero_auth_or_network_calls(self):
        self.assertEqual(self.publish()['reason'],'live_execution_disabled')
        self.assertEqual(self.calls,[])

    def test_fallback_policy_must_also_be_armed(self):
        self.arm();value=json.loads(self.fallback.read_text());value['live_execution_enabled']=False
        self.write(self.fallback,value)
        self.assertFalse(self.publish()['command_sent']);self.assertEqual(self.calls,[])

    def test_candidate_and_installed_state_are_pinned_in_private_policy(self):
        self.arm()
        for key,value in (('approved_candidate_semantic_hash','2'*64),
                          ('expected_installed_semantic_hash','3'*64),('energy_site_id',456)):
            policy=json.loads(self.policy.read_text());original=policy[key];policy[key]=value
            self.write(self.policy,policy)
            self.assertFalse(self.publish()['command_sent']);self.assertEqual(self.calls,[])
            policy[key]=original;self.write(self.policy,policy)

    def test_public_or_symlink_policy_refuses_before_network(self):
        self.arm();self.policy.chmod(0o644)
        self.assertFalse(self.publish()['command_sent']);self.assertEqual(self.calls,[])
        link=self.root/'link';link.symlink_to(self.policy)
        self.assertFalse(publisher_policy_enabled(link,candidate_hash=self.candidate_hash,
            expected_site_id=123,expected_installed_hash=OLD))

    def test_final_authorizer_is_required_before_network(self):
        self.arm()
        self.assertEqual(self.publish(self.make(final_authorizer=None))['reason'],'final_authorizer_required')
        self.assertEqual(self.calls,[])

    def test_valid_fake_transport_verifies_all_required_observations(self):
        self.arm();result=self.publish()
        self.assertEqual(self.calls,['preflight','fake_post_only'])
        self.assertTrue(result['readback_verified'])
        self.assertFalse(result['battery_enrolled'])
        self.assertEqual(result['expected_installed_hash'],OLD)

    def test_acknowledgement_is_not_substituted_for_readback(self):
        self.arm();self.reply['current_tariff_hash']=OLD
        result=self.publish();self.assertTrue(result['write_accepted'])
        self.assertFalse(result['readback_verified'])

    def test_missing_ack_with_actual_verified_state_is_explicit(self):
        self.arm();self.reply['write_accepted']=None
        result=self.publish();self.assertIsNone(result['write_accepted'])
        self.assertTrue(result['readback_verified'])

    def test_readback_mode_or_protected_setting_changes_fail_verification(self):
        self.arm()
        for change in ({'mode':'self_consumption'},{'preserve_settings':dict(KEEP,backup_reserve=20)},
                       {'energy_site_id':456}):
            self.reply=change
            self.assertFalse(self.publish()['readback_verified'])

    def test_enrolled_unknown_and_stale_eon_all_deny_final_go(self):
        self.arm()
        for enrolled,stamp in ((True,NOW),(None,NOW),(False,NOW-timedelta(seconds=31))):
            self.eon={'battery_enrolled':enrolled,'enrolled_last_success':stamp.isoformat()}
            self.calls.clear();self.assertFalse(self.publish()['command_sent'])
            self.assertEqual(self.calls,['preflight'])

    def test_caller_health_or_lease_rejection_prevents_go(self):
        self.arm();self.authorized=False
        self.assertFalse(self.publish()['command_sent'])
        self.assertEqual(self.calls,['preflight'])

    def test_changed_remote_settings_or_permissions_prevent_go(self):
        self.arm()
        original=self.ready
        for changes in ({'mode':'self_consumption'},{'current_tariff_hash':'f'*64},
                        {'preserve_settings':dict(KEEP,backup_reserve=20)},
                        {'energy_commands_granted':False}):
            self.ready=lambda context,changes=changes:dict(original(context),**changes)
            self.calls.clear();self.assertFalse(self.publish()['command_sent'])
            self.assertEqual(self.calls,['preflight'])

    def test_policy_revocation_during_authorizer_prevents_go(self):
        self.arm()
        def revoke(context):
            value=json.loads(self.policy.read_text());value['live_execution_enabled']=False
            self.write(self.policy,value);return True
        self.assertFalse(self.publish(self.make(final_authorizer=revoke))['command_sent'])
        self.assertEqual(self.calls,['preflight'])

    def test_slow_final_authorizer_ages_out_preflight(self):
        self.arm();clock=[NOW]
        def slow(context):clock[0]=NOW+timedelta(seconds=16);return True
        writer=self.make(clock=lambda:clock[0],final_authorizer=slow)
        self.assertFalse(self.publish(writer)['command_sent'])
        self.assertEqual(self.calls,['preflight'])

    def test_source_staleness_future_clock_and_current_slot_block_without_network(self):
        self.arm()
        for field,value in (('source_last_success',NOW-timedelta(seconds=900)),
                            ('source_captured_at',NOW+timedelta(seconds=1))):
            manifest=deepcopy(self.manifest);manifest[field]=value.isoformat()
            self.assertEqual(self.publish(manifest=manifest)['reason'],'candidate_invalid')
        writer=self.make(clock=lambda:NOW+timedelta(minutes=30))
        self.assertEqual(self.publish(writer)['reason'],'candidate_invalid')
        self.assertEqual(self.calls,[])

    def test_candidate_unquoted_rates_cannot_silently_differ_from_frozen_pair(self):
        candidate=deepcopy(self.candidate)
        # Pick an all-day fallback season outside the quote date.
        name=next(name for name,season in candidate['seasons'].items()
                  if len(season['tou_periods'])==1)
        label=next(iter(candidate['energy_charges'][name]['rates']))
        candidate['energy_charges'][name]['rates'][label]=.31
        self.assertIn('candidate_quote_semantics_mismatch',
            validate_candidate(candidate,self.manifest,now=NOW))

    def test_crossed_and_negative_quotes_are_not_clamped(self):
        for amount in (-.1,.05):
            candidate=deepcopy(self.candidate)
            name=next(iter(candidate['energy_charges']))
            label=next(iter(candidate['energy_charges'][name]['rates']))
            candidate['energy_charges'][name]['rates'][label]=amount
            self.assertTrue(validate_candidate(candidate,self.manifest,now=NOW))

    def test_metadata_lies_and_incomplete_windows_block(self):
        for change in ({'unquoted_policy':'disabled_zero_placeholder'},
                       {'quoted_interval_count':100},
                       {'known_valid_until':NOW.isoformat()},
                       {'quoted_intervals':[]},
                       {'unquoted_static_pair_is_future_forecast':True}):
            manifest=deepcopy(self.manifest);manifest.update(change)
            self.assertTrue(validate_candidate(self.candidate,manifest,now=NOW))

    def arm_recurring(self):
        self.arm();policy=json.loads(self.policy.read_text())
        policy.update(one_shot_only=False,recurring_enabled=True)
        self.write(self.policy,policy)

    def test_recurring_is_an_explicit_distinct_policy_mode(self):
        self.arm();policy=json.loads(self.policy.read_text());policy['one_shot_only']=False
        self.write(self.policy,policy)
        self.assertFalse(self.publish()['command_sent']);self.assertEqual(self.calls,[])
        policy['recurring_enabled']=True;self.write(self.policy,policy)
        self.assertTrue(self.publish()['command_sent'])

    def test_initial_self_powered_publish_requires_recurring_permission(self):
        self.arm()
        self.assertEqual(self.publish(operation_mode='self_consumption')['reason'],'one_shot_savings_mode_required')
        self.assertEqual(self.calls,[])
        self.arm_recurring()
        result=self.publish(operation_mode='self_consumption')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(result['expected_active_mode'],'self_consumption')
        self.assertTrue(result['active_mode_verification_required'])

    def test_recurring_accepts_known_lagging_cloud_mode_but_requires_final_caller(self):
        self.arm_recurring();original=self.ready
        self.ready=lambda context:dict(original(context),mode='self_consumption')
        self.reply={'mode':'self_consumption'}
        seen=[]
        def check(context):
            seen.append(context)
            return context['expected_active_mode']=='autonomous' and context['cloud_default_mode']=='self_consumption'
        result=self.publish(self.make(final_authorizer=check))
        self.assertTrue(result['readback_verified']);self.assertEqual(len(seen),1)
        self.authorized=False
        self.assertFalse(self.publish()['command_sent'])

    def test_recurring_rechecks_current_cycle_hash_pins(self):
        self.arm_recurring();policy=json.loads(self.policy.read_text())
        policy['approved_candidate_semantic_hash']='f'*64;self.write(self.policy,policy)
        self.assertFalse(self.publish(operation_mode='self_consumption')['command_sent'])
        self.assertEqual(self.calls,[])

    def test_unknown_exception_never_exposes_credential_like_text(self):
        self.arm()
        def fail(context,confirm):raise ValueError('private token=DO_NOT_EXPORT')
        result=self.publish(self.make(transport=fail))
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(result))
        self.assertFalse(result['command_sent'])

    def test_embedded_remote_source_parses(self):
        ast.parse(REMOTE)

    def remote_fixture(self, *, accepted=True, apply=True, post_status=200, changed_before=False,
                       token_configured=True, reply_body=None, recurring=False, cloud_mode='autonomous',
                       saved_configuration=False, stale_rate=False, invalid_configuration=False):
        """Execute actual embedded logic with fake HTTP/auth boundaries only."""
        candidate=deepcopy(self.candidate)
        old=deepcopy(candidate)
        for row in old['energy_charges'].values():
            for key,value in row['rates'].items():row['rates'][key]=value+.01
        old_hash=semantic_hash(old)
        expected={'energy_site_id':123,'current_tariff_hash':old_hash,
            'candidate_tariff_hash':self.candidate_hash,'preserve_settings':dict(KEEP)}
        if recurring:expected['allowed_cloud_modes']=['autonomous','self_consumption']
        calls=[];posted=[False]
        info={'default_real_mode':cloud_mode,'backup_reserve_percent':10,
              'components':{'customer_preferred_export_rule':'battery_ok'}}
        token_path=self.root/'synthetic-tessie-token'
        descriptor=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'w') as handle:handle.write('FAKE_AUTH_BOUNDARY_ONLY\n')
        class Response:
            def __init__(self,body,status=200):
                self.status=status
                self.bytes=json.dumps(body).encode()
                self.content=self
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            async def read(self,n):
                result,self.bytes=self.bytes[:n],self.bytes[n:]
                return result
        class Session:
            def __init__(self,**kwargs):
                self.kwargs=kwargs
                if kwargs.get('trust_env') is not False:raise AssertionError('Proxy inherited')
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def request(self,method,url,**kwargs):
                if not url.startswith('https://api.tessie.com/'):raise AssertionError('Unexpected host')
                if kwargs.get('allow_redirects') is not False:raise AssertionError('Redirect allowed')
                if kwargs.get('ssl') is False:raise AssertionError('TLS disabled')
                path=url.removeprefix('https://api.tessie.com')
                calls.append((method,path,deepcopy(kwargs.get('json'))))
                if method=='POST':
                    if path!='/api/1/energy_sites/123/time_of_use_settings':raise AssertionError('Wrong command')
                    if kwargs.get('json')!={'tou_settings':{'tariff_content_v2':candidate}}:raise AssertionError('Wrong body')
                    posted[0]=apply
                    return Response({'response':{'result':accepted}} if reply_body is None else reply_body,post_status)
                if method!='GET':raise AssertionError('Wrong HTTP method')
                if path=='/auth/tesla_scopes':return Response({'scopes':['energy_device_data','energy_cmds']})
                if path=='/api/1/products':return Response({'response':[{'energy_site_id':123,'components':{'battery':True}}]})
                if path.endswith('/tariff_rate'):
                    return Response({'response':candidate if posted[0] and not stale_rate else old})
                if path.endswith('/site_info'):
                    body=deepcopy(info)
                    if saved_configuration:
                        body['tariff_content']=deepcopy(candidate if posted[0] else old)
                        body['tariff_content_v2']={'invalid':'shape'}
                    if invalid_configuration:body['tariff_content']={'invalid':'shape'}
                    if changed_before:body['backup_reserve_percent']=20
                    return Response({'response':body})
                raise AssertionError('Unexpected endpoint')
        fake_aiohttp=types.ModuleType('aiohttp')
        fake_aiohttp.ClientSession=Session
        fake_aiohttp.ClientTimeout=lambda **kwargs:kwargs
        namespace={'SCHEMA_SOURCE':(Path(__file__).resolve().parents[1]/'tariff_schema.py').read_text(),
            'AUTH_SOURCE':secret_reader_source(),
            'REPLY_METADATA_SOURCE':reply_metadata_source(),
            'CANDIDATE':candidate,'EXPECTED':expected,'LIVE_AUTHORIZED':True}
        stdin=io.StringIO(json.dumps({'proceed':True,'valid_until':
            (datetime.now(UTC)+timedelta(seconds=4)).isoformat()})+'\n')
        stdout=io.StringIO()
        # Authentication reads only this synthetic explicit0600 token file.
        # HTTP is entirely in-memory, so no network socket exists.
        with patch.dict('sys.modules',{'aiohttp':fake_aiohttp}),\
            patch.dict(os.environ,{'TESSIE_TOKEN_FILE':str(token_path) if token_configured else ''}),\
            patch('sys.stdin',stdin),contextlib.redirect_stdout(stdout):
            exec(REMOTE,namespace)
        values=[json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertNotIn('FAKE_AUTH_BOUNDARY_ONLY',stdout.getvalue())
        return values,calls

    def test_actual_child_logic_posts_once_and_returns_ack_then_verified_readback(self):
        values,calls=self.remote_fixture()
        self.assertTrue(values[0]['preflight_ready'])
        result=values[-1]
        self.assertTrue(result['write_acknowledged'])
        self.assertTrue(result['readback_verified'])
        self.assertLessEqual(datetime.fromisoformat(result['write_acknowledged_at']),
                             datetime.fromisoformat(result['readback']['observed_at']))
        self.assertEqual([method for method,_,_ in calls],['GET','GET','GET','GET','POST','GET','GET'])

    def test_actual_child_explicit_rejection_does_not_claim_readback_success(self):
        values,calls=self.remote_fixture(accepted=False,apply=False)
        result=values[-1]
        self.assertFalse(result['write_acknowledged'])
        self.assertIsNone(result['write_acknowledged_at'])
        self.assertFalse(result['readback_verified'])
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)

    def test_actual_child_unknown_ack_can_observe_state_without_claiming_ack(self):
        values,_calls=self.remote_fixture(post_status=500,apply=True)
        result=values[-1]
        self.assertIsNone(result['write_accepted'])
        self.assertFalse(result['write_acknowledged'])
        self.assertTrue(result['readback_verified'])

    def test_actual_child_changed_preflight_never_posts(self):
        values,calls=self.remote_fixture(changed_before=True)
        self.assertFalse(values[-1]['command_sent'])
        self.assertFalse(any(method=='POST' for method,_,_ in calls))

    def test_actual_child_without_explicit_token_never_uses_http(self):
        values,calls=self.remote_fixture(token_configured=False)
        self.assertEqual(calls,[])
        self.assertFalse(values[-1]['command_sent'])
        self.assertEqual(values[-1]['reason'],'private_error_suppressed')

    def test_actual_child_records_completed_http200_with_unknown_nested_ack(self):
        values,calls=self.remote_fixture(reply_body={'data':{'status':'queued','message':'PRIVATE_TOKEN_DO_NOT_SAVE'}})
        result=values[-1]
        self.assertIsNone(result['write_accepted']);self.assertEqual(result['write_http_status'],200)
        self.assertIsNotNone(result['write_http_completed_at'])
        self.assertLessEqual(datetime.fromisoformat(result['write_http_completed_at']),
            datetime.fromisoformat(result['readback']['observed_at']))
        self.assertNotIn('PRIVATE_TOKEN_DO_NOT_SAVE',json.dumps(result))
        self.assertFalse(result['explicit_rejection'])
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)

    def test_actual_child_detects_explicit_failure_in_alternate_wrapper(self):
        values,_calls=self.remote_fixture(reply_body={'result':False,'error':'PRIVATE_ERROR_DO_NOT_SAVE'},apply=False)
        result=values[-1]
        self.assertTrue(result['explicit_rejection'])
        self.assertIsNone(result['write_accepted'])
        self.assertNotIn('PRIVATE_ERROR_DO_NOT_SAVE',json.dumps(result))

    def test_actual_child_can_write_valid_tariff_while_cloud_default_self_powered(self):
        values,calls=self.remote_fixture(recurring=True,cloud_mode='self_consumption')
        self.assertTrue(values[0]['preflight_ready'])
        self.assertTrue(values[-1]['readback_verified'])
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)

    def test_actual_child_verifies_saved_configuration_while_rate_projection_lags(self):
        values,calls=self.remote_fixture(saved_configuration=True,stale_rate=True)
        self.assertTrue(values[0]['preflight_ready'])
        self.assertTrue(values[-1]['readback_verified'])
        self.assertEqual(values[-1]['tariff_observation_source'],'site_info.tariff_content')
        self.assertEqual(values[-1]['readback']['tariff_observation_source'],'site_info.tariff_content')
        self.assertEqual(sum(method=='POST' for method,_,_ in calls),1)

    def test_actual_child_invalid_saved_configuration_cannot_hide_behind_rate(self):
        values,calls=self.remote_fixture(invalid_configuration=True)
        self.assertFalse(values[-1]['command_sent'])
        self.assertFalse(any(method=='POST' for method,_,_ in calls))


if __name__=='__main__':unittest.main()
