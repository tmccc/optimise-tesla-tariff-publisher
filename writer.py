"""Unarmed one-shot tariff transport with explicit local token-file authentication.

The caller holds .control.lock and owns the durable attempt/lease journal and
independent-watchdog checks. No background process or network call starts on
import. Both private live policies and a final caller authorisation are needed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys

from date_compiler import build_date_candidate
from runtime import FallbackAdapter, POLICY as FALLBACK_POLICY, live_policy_enabled, private_document
from auth import secret_reader_source
from config import ROOT, STATE_DIR, task_env
from tariff_schema import semantic_hash, to_upload_tariff, validate_tariff
from reply_metadata import reply_metadata_source

UTC = timezone.utc
PUBLISHER_POLICY = STATE_DIR / 'publisher-policy.json'
HASH = re.compile(r'[0-9a-f]{64}')


def _instant(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(UTC)


def _protected(value):
    return (isinstance(value, dict)
        and set(value) == {'backup_reserve', 'grid_charging', 'export_permission'}
        and type(value['backup_reserve']) in {int, float}
        and 0 <= value['backup_reserve'] <= 100
        and (type(value['grid_charging']) is bool or value['grid_charging'] == 'not_reported')
        and value['export_permission'] in {'battery_ok', 'pv_only', 'never'})


def validate_candidate(candidate, manifest, *, now):
    """Pure one-shot eligibility checks, including the claimed frozen baseline."""
    errors = set(validate_tariff(candidate))
    if errors:
        return sorted(errors)
    try:
        clock = _instant(now)
        if not isinstance(manifest, dict):
            return ['manifest_invalid']
        if (manifest.get('currency') != 'GBP' or manifest.get('timezone') != 'Europe/London'
            or manifest.get('unquoted_policy') != 'frozen_current_quote'
            or manifest.get('unquoted_static_pair_is_observed_current_quote') is not True
            or manifest.get('unquoted_static_pair_is_future_forecast') is not False
            or manifest.get('unquoted_prices_are_optimise_quotes') is not False):
            errors.add('planning_assumption_invalid')
        for field in ('source_last_success', 'source_captured_at'):
            age = (clock - _instant(manifest.get(field))).total_seconds()
            if not 0 <= age < 900:
                errors.add('source_not_fresh')
        rows = manifest.get('quoted_intervals')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            return sorted(errors | {'quoted_intervals_invalid'})
        if manifest.get('quoted_interval_count') != len(rows):
            errors.add('quoted_interval_count_mismatch')
        first = _instant(rows[0].get('valid_from'))
        first_end = _instant(rows[0].get('valid_to'))
        until = _instant(rows[-1].get('valid_to'))
        if not first <= clock < first_end:
            errors.add('current_quote_missing')
        if (_instant(manifest.get('known_valid_from')) != first
            or _instant(manifest.get('known_valid_until')) != until
            or until <= clock + timedelta(seconds=60)):
            errors.add('known_window_invalid')
        assumed = manifest.get('unquoted_assumed_pair')
        if (not isinstance(assumed, dict)
            or _instant(assumed.get('observed_valid_from')) != first
            or _instant(assumed.get('observed_valid_to')) != first_end
            or Decimal(str(assumed.get('import_gbp_per_kwh'))) != Decimal(str(rows[0].get('import_gbp_per_kwh')))
            or Decimal(str(assumed.get('export_gbp_per_kwh'))) != Decimal(str(rows[0].get('export_gbp_per_kwh')))):
            errors.add('frozen_pair_mismatch')
        # Recompile solely from the claimed genuine quote rows. Under the
        # approved frozen policy, the baseline's unquoted rates are ignored.
        # This verifies all annual unquoted slots as well as every quote.
        preview = {'valid': True, 'errors': [], 'currency': 'GBP', 'timezone': 'Europe/London',
            'source_last_success': manifest.get('source_last_success'),
            'source_captured_at': manifest.get('source_captured_at'), 'periods': deepcopy(rows)}
        rebuilt = build_date_candidate(preview, candidate, now=clock,
            unquoted_policy='frozen_current_quote')
        if not rebuilt.get('candidate_valid') or rebuilt.get('tariff') is None:
            errors.add('quote_rebuild_failed')
        elif semantic_hash(rebuilt['tariff']) != semantic_hash(candidate):
            errors.add('candidate_quote_semantics_mismatch')
    except Exception:
        # Never stringify an arbitrary manifest/provider value.
        errors.add('candidate_metadata_invalid')
    return sorted(errors)


def publisher_policy_enabled(path, *, candidate_hash, expected_site_id, expected_installed_hash):
    try:
        policy = private_document(path)
        mode_approved=(policy.get('one_shot_only') is True
            or (policy.get('one_shot_only') is False and policy.get('recurring_enabled') is True))
        return (policy.get('live_execution_enabled') is True
            and policy.get('allow_tariff_writes') is True
            and mode_approved
            and policy.get('unquoted_policy') == 'frozen_current_quote'
            and policy.get('unquoted_planning_assumption_approved') is True
            and policy.get('fallback_design_approved') is True
            and policy.get('fallback_policy') == 'self_powered'
            and type(policy.get('energy_site_id')) is int
            and policy.get('energy_site_id') == expected_site_id
            and policy.get('approved_candidate_semantic_hash') == candidate_hash
            and policy.get('expected_installed_semantic_hash') == expected_installed_hash)
    except (OSError, ValueError, TypeError):
        return False


REMOTE = r'''
import asyncio,json,logging,re,sys,types
from datetime import datetime,timezone
from pathlib import Path
import aiohttp
logging.disable(logging.CRITICAL)
ATTEMPTED=False
POST_STATUS=None
POST_COMPLETED_AT=None
schema=types.ModuleType('_writer_tariff_schema')
sys.modules[schema.__name__]=schema
exec(SCHEMA_SOURCE,schema.__dict__)
exec(AUTH_SOURCE,globals())
exec(REPLY_METADATA_SOURCE,globals())

async def main():
    if LIVE_AUTHORIZED is not True or schema.validate_tariff(CANDIDATE):
        return {'command_sent':False,'reason':'remote_validation_failed'}
    if schema.semantic_hash(CANDIDATE)!=EXPECTED.get('candidate_tariff_hash'):
        return {'command_sent':False,'reason':'remote_candidate_hash_mismatch'}
    token=read_secret_file('TESSIE_TOKEN_FILE')
    allowed={'/auth/tesla_scopes','/api/1/products'}
    async with aiohttp.ClientSession(trust_env=False,
        timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def request(method,path,body=None):
            global ATTEMPTED,POST_STATUS,POST_COMPLETED_AT
            if path not in allowed:raise ValueError('endpoint_denied')
            if method!='GET' and not (method=='POST' and path==write_path
                and LIVE_AUTHORIZED is True
                and body=={'tou_settings':{'tariff_content_v2':CANDIDATE}}):
                raise ValueError('mutation_denied')
            if method=='POST':ATTEMPTED=True
            async with session.request(method,'https://api.tessie.com'+path,
                headers={'Authorization':'Bearer '+token},json=body,
                allow_redirects=False) as response:
                if method=='POST':POST_STATUS=response.status
                pieces=[];size=0
                while size<=2*1024*1024:
                    piece=await response.content.read(min(65536,2*1024*1024+1-size))
                    if not piece:break
                    pieces.append(piece);size+=len(piece)
                if response.status!=200 or size>2*1024*1024:
                    if method=='POST':POST_COMPLETED_AT=datetime.now(timezone.utc).isoformat()
                    raise ValueError('response_unavailable')
                if method=='POST':POST_COMPLETED_AT=datetime.now(timezone.utc).isoformat()
                return json.loads(b''.join(pieces))
        scopes_reply=await request('GET','/auth/tesla_scopes')
        scopes=scopes_reply.get('scopes') if isinstance(scopes_reply,dict) else None
        if not isinstance(scopes,list) or any(not isinstance(x,str) for x in scopes):
            raise ValueError('scope_read_failed')
        products_reply=await request('GET','/api/1/products')
        products=products_reply.get('response') if isinstance(products_reply,dict) else None
        if not isinstance(products,list):raise ValueError('products_unavailable')
        sites=[x for x in products if isinstance(x,dict)
            and type(x.get('energy_site_id')) is int
            and isinstance(x.get('components'),dict) and x['components'].get('battery') is True]
        if len(sites)!=1 or sites[0]['energy_site_id']!=EXPECTED.get('energy_site_id'):
            return {'command_sent':False,'reason':'site_selection_changed'}
        site=sites[0]['energy_site_id']
        prefix='/api/1/energy_sites/'+str(site)+'/'
        write_path=prefix+'time_of_use_settings'
        allowed.update({prefix+'site_info',prefix+'tariff_rate'})
        async def snapshot():
            tariff_reply=await request('GET',prefix+'tariff_rate')
            info_reply=await request('GET',prefix+'site_info')
            tariff=tariff_reply.get('response') if isinstance(tariff_reply,dict) else None
            info=info_reply.get('response') if isinstance(info_reply,dict) else None
            if not isinstance(info,dict) or not isinstance(tariff,dict):
                raise ValueError('readback_unavailable')
            components=info.get('components',{})
            if not isinstance(components,dict):components={}
            reserve=info.get('backup_reserve_percent')
            disallow=components.get('disallow_charge_from_grid_with_solar_installed')
            export=components.get('customer_preferred_export_rule')
            keep={'backup_reserve':reserve if type(reserve) in {int,float} and 0<=reserve<=100 else None,
                'grid_charging':(not disallow if isinstance(disallow,bool)
                    else 'not_reported' if 'disallow_charge_from_grid_with_solar_installed' not in components else None),
                'export_permission':export if export in {'battery_ok','pv_only','never'} else None}
            mode=info.get('default_real_mode')
            if mode not in {'autonomous','self_consumption'}:mode='unknown'
            return {'energy_site_id':site,'current_tariff_hash':schema.semantic_hash(tariff),
                'mode':mode,'preserve_settings':keep,
                'energy_commands_granted':'energy_cmds' in scopes,
                'live_last_success':datetime.now(timezone.utc).isoformat()}
        before=await snapshot()
        if (before['current_tariff_hash']!=EXPECTED.get('current_tariff_hash')
            or before['mode'] not in EXPECTED.get('allowed_cloud_modes',['autonomous'])
            or before['preserve_settings']!=EXPECTED.get('preserve_settings')
            or any(x is None for x in before['preserve_settings'].values())
            or before['energy_commands_granted'] is not True):
            return {'command_sent':False,'reason':'remote_preflight_changed'}
        ready=dict(before,preflight_ready=True,candidate_tariff_hash=EXPECTED['candidate_tariff_hash'],
            ready_at=datetime.now(timezone.utc).isoformat())
        print(json.dumps(ready,allow_nan=False),flush=True)
        ack=json.loads(sys.stdin.readline(4096))
        until=datetime.fromisoformat(ack.get('valid_until',''))
        if (ack.get('proceed') is not True or until.tzinfo is None
            or not 0<(until-datetime.now(timezone.utc)).total_seconds()<=5):
            return {'command_sent':False,'reason':'final_authorization_denied'}
        allowed.add(write_path)
        accepted=None
        acknowledged_at=None
        response_code=None
        reply_metadata=None
        try:
            reply=await request('POST',write_path,{'tou_settings':{'tariff_content_v2':CANDIDATE}})
            reply_metadata=safe_reply_metadata(reply)
            body=reply.get('response') if isinstance(reply,dict) else None
            if isinstance(body,dict) and type(body.get('code')) is int and 0<=body['code']<=599:
                response_code=body['code']
            if isinstance(body,dict) and isinstance(body.get('result'),bool):accepted=body['result']
            if accepted is True:acknowledged_at=datetime.now(timezone.utc).isoformat()
        except Exception:
            pass
        result={'command_sent':True,'control_requests':1,'write_accepted':accepted,
            'write_response_code':response_code,
            'write_http_completed_at':POST_COMPLETED_AT,'reply_metadata':reply_metadata,
            'explicit_rejection':reply_explicitly_rejected(reply_metadata),
            'write_acknowledged':accepted is True,'write_acknowledged_at':acknowledged_at,
            'write_http_status':POST_STATUS,'candidate_tariff_hash':EXPECTED['candidate_tariff_hash'],
            'expected_installed_hash':EXPECTED['current_tariff_hash']}
        try:
            after=await snapshot()
            after['observed_at']=datetime.now(timezone.utc).isoformat()
            result.update(after)
            result['readback']=dict(after)
            result['readback_verified']=(after['current_tariff_hash']==EXPECTED['candidate_tariff_hash']
                and after['mode'] in EXPECTED.get('allowed_cloud_modes',['autonomous'])
                and after['preserve_settings']==EXPECTED['preserve_settings'])
        except Exception:
            result.update({'readback_verified':False,'outcome_unknown':True})
        return result
try:
    print(json.dumps(asyncio.run(main()),allow_nan=False),flush=True)
except Exception:
    print(json.dumps({'command_sent':True if ATTEMPTED else False,
        'control_requests':1 if ATTEMPTED else 0,'readback_verified':False,
        'write_http_status':POST_STATUS,'write_http_completed_at':POST_COMPLETED_AT,
        'outcome_unknown':True if ATTEMPTED else False,
        'reason':'private_error_suppressed'}),flush=True)
'''


class TariffWriter:
    def __init__(self, *, policy_path=PUBLISHER_POLICY, fallback_policy_path=FALLBACK_POLICY,
                 transport=None, enrollment_refresher=None, final_authorizer=None, clock=None):
        self.policy_path = policy_path
        self.fallback_policy_path = fallback_policy_path
        self.transport = transport or self._child
        self.enrollment_refresher = enrollment_refresher or (lambda: FallbackAdapter._read_enrollment(force_refresh=True))
        self.final_authorizer = final_authorizer
        self.clock = clock or (lambda: datetime.now(UTC))
        self._last_confirmation = None
        self._go_sent = False

    def _policy(self, context):
        return (publisher_policy_enabled(self.policy_path,
            candidate_hash=context['candidate_tariff_hash'], expected_site_id=context['energy_site_id'],
            expected_installed_hash=context['current_tariff_hash'])
            and live_policy_enabled(self.fallback_policy_path))

    def _confirm(self, ready, context):
        try:
            if not self._policy(context) or not callable(self.final_authorizer):return False
            if (ready.get('energy_site_id') != context['energy_site_id']
                or ready.get('current_tariff_hash') != context['current_tariff_hash']
                or ready.get('candidate_tariff_hash') != context['candidate_tariff_hash']
                or ready.get('mode') not in context['allowed_cloud_modes']
                or ready.get('energy_commands_granted') is not True
                or ready.get('preserve_settings') != context['preserve_settings']):return False
            refreshed = self.enrollment_refresher()
            clock = _instant(self.clock())
            if (refreshed.get('battery_enrolled') is not False
                or not 0 <= (clock - _instant(refreshed.get('enrolled_last_success'))).total_seconds() < 30
                or not 0 <= (clock - _instant(ready.get('ready_at'))).total_seconds() < 15
                or validate_candidate(context['candidate'], context['manifest'], now=clock)):
                return False
            final = dict(ready, **refreshed)
            final['expected_active_mode']=context['operation_mode']
            final['cloud_default_mode']=ready.get('mode')
            if self.final_authorizer(deepcopy(final)) is not True:return False
            clock = _instant(self.clock())
            if (not self._policy(context)
                or not 0 <= (clock - _instant(ready.get('ready_at'))).total_seconds() < 15
                or not 0 <= (clock - _instant(refreshed.get('enrolled_last_success'))).total_seconds() < 30
                or validate_candidate(context['candidate'], context['manifest'], now=clock)):
                return False
            self._last_confirmation = final
            return True
        except Exception:
            return False

    def _child(self, context, confirm):
        expected = {key: context[key] for key in ('energy_site_id','current_tariff_hash',
                                                'candidate_tariff_hash','preserve_settings','allowed_cloud_modes')}
        source = ('SCHEMA_SOURCE=' + repr((ROOT/'tariff_schema.py').read_text()) + '\n'
            + 'AUTH_SOURCE=' + repr(secret_reader_source()) + '\n'
            + 'REPLY_METADATA_SOURCE=' + repr(reply_metadata_source()) + '\n'
            + 'CANDIDATE=' + repr(context['candidate']) + '\n'
            + 'EXPECTED=' + repr(expected) + '\nLIVE_AUTHORIZED=True\n' + REMOTE)
        wrapper = 'import json,sys;exec(json.loads(sys.stdin.readline()))'
        env = task_env()
        process = subprocess.Popen([sys.executable,'-c',wrapper],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            process.stdin.write(json.dumps(source)+'\n');process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                if not selector.select(timeout=100):raise ValueError('preflight_timeout')
                line=process.stdout.readline(16385)
            ready=json.loads(line)
            if not isinstance(ready,dict) or ready.get('preflight_ready') is not True:
                return ready if isinstance(ready,dict) else {'command_sent':False,'reason':'preflight_invalid'}
            proceed=confirm(ready)
            ack={'proceed':proceed,'valid_until':(_instant(self.clock())+timedelta(seconds=4)).isoformat()}
            # Record uncertainty before handing any GO bytes to the child.
            if proceed:self._go_sent=True
            process.stdin.write(json.dumps(ack)+'\n');process.stdin.flush()
            output,_private_error=process.communicate(timeout=70)
            if process.returncode or len(output)>1024*1024:raise ValueError('private_transport_error')
            result=json.loads(output)
            if not isinstance(result,dict):raise ValueError('private_transport_error')
            return result
        finally:
            if process.poll() is None:
                process.kill();process.communicate(timeout=5)

    def publish(self, candidate_tariff, *, manifest, expected_site_id,
                expected_installed_hash, preserve_settings, operation_mode='autonomous'):
        """Attempt one pinned tariff write; never grant permissions or alter modes."""
        self._last_confirmation=None
        self._go_sent=False
        blocked={'command_sent':False,'control_requests':0,'readback_verified':False}
        try:
            errors=validate_candidate(candidate_tariff,manifest,now=self.clock())
            if errors:return dict(blocked,reason='candidate_invalid',validation_errors=errors)
            if (type(expected_site_id) is not int or expected_site_id<=0
                or not isinstance(expected_installed_hash,str) or not HASH.fullmatch(expected_installed_hash)
                or not _protected(preserve_settings)
                or operation_mode not in {'autonomous','self_consumption'}):
                return dict(blocked,reason='expected_state_invalid')
            candidate=to_upload_tariff(candidate_tariff)
            context={'candidate':candidate,'manifest':deepcopy(manifest),'energy_site_id':expected_site_id,
                'candidate_tariff_hash':semantic_hash(candidate),'current_tariff_hash':expected_installed_hash,
                'preserve_settings':deepcopy(preserve_settings),'operation_mode':operation_mode}
            policy=private_document(self.policy_path)
            recurring=(policy.get('recurring_enabled') is True and policy.get('one_shot_only') is False)
            if not recurring and operation_mode!='autonomous':
                return dict(blocked,reason='one_shot_savings_mode_required')
            context['allowed_cloud_modes']=(['autonomous','self_consumption'] if recurring else ['autonomous'])
            if not self._policy(context):return dict(blocked,reason='live_execution_disabled')
            if not callable(self.final_authorizer):return dict(blocked,reason='final_authorizer_required')
            if context['candidate_tariff_hash']==expected_installed_hash:
                return dict(blocked,reason='candidate_already_installed')
            result=self.transport(context,lambda ready:self._confirm(ready,context))
            if not isinstance(result,dict):raise ValueError('transport_result_invalid')
            result=deepcopy(result)
            result['write_acknowledged']=result.get('write_accepted') is True
            if self._last_confirmation is not None:
                result.update({key:self._last_confirmation.get(key)
                    for key in ('battery_enrolled','enrolled_last_success')})
            readback=result.get('readback')
            if not isinstance(readback,dict):
                readback={key:result.get(key) for key in ('energy_site_id','current_tariff_hash',
                    'mode','preserve_settings','energy_commands_granted','live_last_success')}
                readback['observed_at']=result.get('observed_at') or result.get('live_last_success')
            readback.update({key:result.get(key) for key in ('battery_enrolled','enrolled_last_success')})
            result['readback']=readback
            result['fresh_readback_tariff_hash']=readback.get('current_tariff_hash')
            # Independently classify observed state; an acknowledgement alone
            # never establishes that all timed prices were installed.
            result['readback_verified']=bool(result.get('command_sent') is True
                and result.get('current_tariff_hash')==context['candidate_tariff_hash']
                and result.get('energy_site_id')==expected_site_id
                and result.get('mode') in context['allowed_cloud_modes']
                and result.get('preserve_settings')==preserve_settings
                and self._last_confirmation is not None)
            result['candidate_tariff_hash']=context['candidate_tariff_hash']
            result['active_mode_verification_required']=True
            result['expected_active_mode']=operation_mode
            result['expected_installed_hash']=expected_installed_hash
            return result
        except Exception:
            return {'command_sent':None if self._go_sent else False,
                'control_requests':None if self._go_sent else 0,'readback_verified':False,
                'outcome_unknown':self._go_sent,'reason':'private_writer_error_suppressed'}
