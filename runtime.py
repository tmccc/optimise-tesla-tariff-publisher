"""Unarmed Self-Powered fallback using an explicit local Tessie token file.

No process is launched at import time. A fixed local policy file must explicitly
enable live execution before the only mutation endpoint can be invoked. That
policy is disabled for this preparation task. There is no tariff-upload route.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import selectors
import subprocess
import sys

from active_mode import read_active_mode
from auth import secret_reader_source
from config import ROOT, STATE_DIR, task_env

POLICY=STATE_DIR/'execution-policy.json'

REMOTE=r'''
import asyncio,hashlib,json,logging,re,sys,types
from datetime import datetime,timezone
from pathlib import Path
import aiohttp
logging.disable(logging.CRITICAL)
CONTROL_ATTEMPTED=False
schema=types.ModuleType('_fallback_tariff_schema')
sys.modules[schema.__name__]=schema
exec(SCHEMA_SOURCE,schema.__dict__)
exec(AUTH_SOURCE,globals())

async def main():
    token=read_secret_file('TESSIE_TOKEN_FILE')
    allowed={'/auth/tesla_scopes','/api/1/products'}
    async with aiohttp.ClientSession(trust_env=False,
        timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def request(method,path,body=None):
            global CONTROL_ATTEMPTED
            if path not in allowed:raise ValueError('endpoint_denied')
            if method!='GET' and not (method=='POST' and ACTION=='self_powered'
                and LIVE_AUTHORIZED is True and path.endswith('/operation')
                and body=={'default_real_mode':'self_consumption'}):
                raise ValueError('mutation_denied')
            if method=='POST':CONTROL_ATTEMPTED=True
            async with session.request(method,'https://api.tessie.com'+path,
                headers={'Authorization':'Bearer '+token},json=body,
                allow_redirects=False) as response:
                pieces=[];size=0
                while size<=2*1024*1024:
                    piece=await response.content.read(min(65536,2*1024*1024+1-size))
                    if not piece:break
                    pieces.append(piece);size+=len(piece)
                if response.status!=200 or size>2*1024*1024:
                    raise ValueError('endpoint_response_unavailable')
                return json.loads(b''.join(pieces))
        scopes_reply=await request('GET','/auth/tesla_scopes')
        scopes=scopes_reply.get('scopes')
        if not isinstance(scopes,list) or any(not isinstance(x,str) for x in scopes):
            raise ValueError('scope_read_failed')
        products=(await request('GET','/api/1/products')).get('response')
        if not isinstance(products,list):raise ValueError('products_unavailable')
        sites=[p for p in products if isinstance(p,dict)
            and type(p.get('energy_site_id')) is int
            and p.get('components',{}).get('battery') is True]
        if len(sites)!=1:raise ValueError('site_selection_required')
        site=sites[0]['energy_site_id']
        if site<=0:raise ValueError('site_invalid')
        prefix='/api/1/energy_sites/'+str(site)+'/'
        allowed.update({prefix+'site_info',prefix+'tariff_rate'})
        info=(await request('GET',prefix+'site_info')).get('response')
        tariff=(await request('GET',prefix+'tariff_rate')).get('response')
        if not isinstance(info,dict) or not isinstance(tariff,dict):
            raise ValueError('readback_unavailable')
        digest=schema.semantic_hash(tariff)
        mode=info.get('default_real_mode')
        if mode not in {'autonomous','self_consumption'}:raise ValueError('mode_unknown')
        components=info.get('components',{})
        if not isinstance(components,dict):components={}
        reserve=info.get('backup_reserve_percent')
        grid_disallowed=components.get('disallow_charge_from_grid_with_solar_installed')
        export=components.get('customer_preferred_export_rule')
        preserved={
            'backup_reserve':reserve if type(reserve) in {int,float} and 0<=reserve<=100 else None,
            'grid_charging':(not grid_disallowed if isinstance(grid_disallowed,bool)
                else ('not_reported' if 'disallow_charge_from_grid_with_solar_installed' not in components else None)),
            'export_permission':export if isinstance(export,str)
                and re.fullmatch(r'[A-Za-z_]{1,64}',export) else None,
        }
        output={'live_last_success':datetime.now(timezone.utc).isoformat(),
            'energy_site_id':site,'mode':mode,'current_tariff_hash':digest,
            'energy_commands_granted':'energy_cmds' in scopes,
            'preserve_settings':preserved,'command_sent':False}
        if ACTION=='self_powered':
            if LIVE_AUTHORIZED is not True:raise ValueError('live_execution_disabled')
            if (site!=EXPECTED.get('energy_site_id') or mode!='autonomous'
                or digest!=EXPECTED.get('current_tariff_hash')
                or preserved!=EXPECTED.get('preserve_settings')
                or any(x is None for x in preserved.values())
                or 'energy_cmds' not in scopes):
                return {'command_sent':False,'preflight_verified':False}
            # Host repeats local arming/lease checks and explicitly refreshes
            # E.ON telemetry AFTER these Tesla reads, immediately before POST.
            print(json.dumps({'preflight_ready':True,
                'ready_at':datetime.now(timezone.utc).isoformat()}),flush=True)
            acknowledgement=json.loads(sys.stdin.readline(4096))
            deadline=datetime.fromisoformat(acknowledgement.get('valid_until',''))
            if (acknowledgement.get('proceed') is not True or deadline.tzinfo is None
                or not 0<(deadline-datetime.now(timezone.utc)).total_seconds()<=5):
                return {'command_sent':False,'preflight_verified':False}
            allowed.add(prefix+'operation')
            # Exactly one mode field; no reserve, grid/export or tariff writes.
            reply=await request('POST',prefix+'operation',{'default_real_mode':'self_consumption'})
            result=reply.get('response',{}) if isinstance(reply,dict) else {}
            accepted=result.get('result') if isinstance(result,dict) else None
            output={'command_sent':True,'preflight_verified':True,
                'accepted':accepted if isinstance(accepted,bool) else None}
        return output
try:
    print(json.dumps(asyncio.run(main()),allow_nan=False))
except Exception:
    if CONTROL_ATTEMPTED:
        print(json.dumps({'command_sent':True,'preflight_verified':True,'outcome_unknown':True}))
    else:
        print(json.dumps({'adapter_failed':True,'reason':'private_error_suppressed'}))
        raise SystemExit(1)
'''


def private_document(path):
    path=Path(path)
    if path.is_symlink():raise ValueError('private_document_symlink')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('private_document_permissions')
        with os.fdopen(fd,'rb',closefd=False) as handle:raw=handle.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('private_document_oversized')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('private_document_invalid')
        return value
    finally:os.close(fd)


def live_policy_enabled(path=POLICY):
    try:
        policy=private_document(path)
        return (policy.get('fallback_design_approved') is True
            and policy.get('live_execution_enabled') is True
            and policy.get('allowed_mode')=='self_consumption'
            and policy.get('allow_tariff_writes') is False)
    except (OSError,ValueError,TypeError):return False


def _instant(value):
    value=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(timezone.utc)


class FallbackAdapter:
    def __init__(self, ownership, *, policy_path=POLICY, transport=None, enrollment_reader=None,
                 enrollment_refresher=None, active_mode_reader=None, lease_path=None, lock_path=None):
        self.ownership=dict(ownership)
        self.policy_path=policy_path
        self.transport=transport or self._child
        self.enrollment_reader=enrollment_reader or self._read_enrollment
        self.enrollment_refresher=enrollment_refresher or (lambda:self._read_enrollment(force_refresh=True))
        self.active_mode_reader=active_mode_reader or read_active_mode
        self.lease_path=Path(lease_path) if lease_path else STATE_DIR/'output'/'publisher-lease.json'
        self.lock_path=Path(lock_path) if lock_path else STATE_DIR/'.control.lock'

    def _child(self,action, expected, live_authorized):
        source=('SCHEMA_SOURCE='+repr((ROOT/'tariff_schema.py').read_text())+'\n'
            +'AUTH_SOURCE='+repr(secret_reader_source())+'\n'
            +'ACTION='+repr(action)+'\nEXPECTED='+repr(expected)+'\n'
            +'LIVE_AUTHORIZED='+repr(live_authorized)+'\n'+REMOTE)
        env=task_env()
        if action=='self_powered':
            wrapper='import json,sys;exec(json.loads(sys.stdin.readline()))'
            process=subprocess.Popen([sys.executable,'-c',wrapper],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
            try:
                process.stdin.write(json.dumps(source)+'\n');process.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ)
                    if not selector.select(timeout=100):raise ValueError('remote_preflight_timeout')
                    line=process.stdout.readline(8193)
                ready=json.loads(line)
                if not isinstance(ready,dict) or ready.get('preflight_ready') is not True:
                    return ready if isinstance(ready,dict) else {'command_sent':False,'preflight_verified':False}
                proceed=self._confirm_remote_preflight(expected,remote_ready_at=ready.get('ready_at'))
                from datetime import timedelta
                ack={'proceed':proceed,'valid_until':
                    (datetime.now(timezone.utc)+timedelta(seconds=4)).isoformat()}
                process.stdin.write(json.dumps(ack)+'\n');process.stdin.flush()
                output,_private_error=process.communicate(timeout=30)
                if process.returncode or len(output)>1024*1024:
                    raise ValueError('adapter_request_failed_private_errors_suppressed')
                value=json.loads(output)
            finally:
                if process.poll() is None:
                    process.kill();process.communicate(timeout=5)
        else:
            result=subprocess.run([sys.executable,'-'],
                input=source,text=True,capture_output=True,timeout=110,env=env)
            if result.returncode or len(result.stdout)>1024*1024:
                raise ValueError('adapter_request_failed_private_errors_suppressed')
            value=json.loads(result.stdout)
        if not isinstance(value,dict) or value.get('adapter_failed'):
            raise ValueError('adapter_response_invalid')
        return value

    @staticmethod
    def _read_enrollment(*,force_refresh=False):
        from ha_reader import read_enrollment
        return read_enrollment(refresh=force_refresh)

    def _confirm_remote_preflight(self,expected,*,remote_ready_at=None):
        if not live_policy_enabled(self.policy_path):return False
        refreshed=self.enrollment_refresher()
        active=self.active_mode_reader()
        try:
            age=(datetime.now(timezone.utc)-_instant(refreshed.get('enrolled_last_success'))).total_seconds()
            ready_age=(datetime.now(timezone.utc)-_instant(
                remote_ready_at or expected.get('live_last_success'))).total_seconds()
        except (ValueError,TypeError,OverflowError):return False
        expected_hardware=expected.get('active_mode_observation',{}).get('hardware_preserve_settings')
        return (refreshed.get('battery_enrolled') is False and 0<=age<30 and 0<=ready_age<15
            and active.get('mode')=='autonomous'
            and (expected_hardware is None or active.get('hardware_preserve_settings')==expected_hardware)
            and live_policy_enabled(self.policy_path)
            and self._expired_lease(expected.get('current_tariff_hash'))==getattr(self,'_pending_lease',None)
            and getattr(self,'_pending_lease',None) is not None)

    def read_snapshot(self):
        snapshot=self.transport('read',{},False)
        snapshot.update(self.enrollment_reader())
        active=self.active_mode_reader()
        # default_real_mode is a saved cloud default. The supervised test
        # established that it can differ from the gateway's active real_mode.
        snapshot['cloud_default_mode']=snapshot.get('mode')
        snapshot['mode']=active['mode']
        snapshot['mode_source']=active['mode_source']
        snapshot['active_mode_observation']=active
        snapshot['publisher_owns_installed_tariff']=bool(
            self.ownership.get('verified_publisher_write') is True
            and snapshot.get('energy_site_id')==self.ownership.get('energy_site_id')
            and snapshot.get('current_tariff_hash')==self.ownership.get('tariff_semantic_hash'))
        snapshot['observed_at']=datetime.now(timezone.utc).isoformat()
        return snapshot

    def set_self_powered(self, *, expected_tariff_hash, preserve_settings):
        # Default policy forbids a mutation before credentials or network access.
        if not live_policy_enabled(self.policy_path):
            return {'command_sent':False,'preflight_verified':False,'reason':'live_execution_disabled'}
        if (not isinstance(preserve_settings,dict)
            or set(preserve_settings)!={'backup_reserve','grid_charging','export_permission'}
            or type(preserve_settings.get('backup_reserve')) not in {int,float}
            or not 0<=preserve_settings['backup_reserve']<=100
            or (type(preserve_settings.get('grid_charging')) is not bool
                and preserve_settings.get('grid_charging')!='not_reported')
            or preserve_settings.get('export_permission') not in {'battery_ok','pv_only','never'}):
            return {'command_sent':False,'preflight_verified':False,'reason':'protected_settings_unknown'}
        fd=os.open(self.lock_path,os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self._set_locked(expected_tariff_hash,preserve_settings)
        except BlockingIOError:
            return {'command_sent':False,'preflight_verified':False,'reason':'controller_busy'}
        finally:os.close(fd)

    def _expired_lease(self, expected_tariff_hash):
        try:
            lease=private_document(self.lease_path)
            age=(datetime.now(timezone.utc)-_instant(lease.get('guard_lease_until'))).total_seconds()
            valid=(age>=0 and lease.get('guard_lease_trusted') is True
                and lease.get('verified_publisher_write') is True
                and lease.get('tariff_semantic_hash')==expected_tariff_hash
                and lease.get('energy_site_id')==self.ownership.get('energy_site_id'))
            return lease if valid else None
        except (OSError,ValueError,TypeError,OverflowError):return None

    def _set_locked(self, expected_tariff_hash, preserve_settings):
        lease=self._expired_lease(expected_tariff_hash)
        if lease is None:
            return {'command_sent':False,'preflight_verified':False,'reason':'trusted_expired_lease_required'}
        before=self.read_snapshot()
        try:
            fresh=(datetime.now(timezone.utc)-_instant(before.get('enrolled_last_success'))).total_seconds()
        except (ValueError,TypeError,OverflowError):fresh=-1
        try:live_age=(datetime.now(timezone.utc)-_instant(before.get('live_last_success'))).total_seconds()
        except (ValueError,TypeError,OverflowError):live_age=-1
        if (before.get('publisher_owns_installed_tariff') is not True
            or before.get('current_tariff_hash')!=expected_tariff_hash
            or before.get('preserve_settings')!=preserve_settings
            or before.get('battery_enrolled') is not False or not 0<=fresh<45*60
            or not 0<=live_age<5*60
            or before.get('mode')!='autonomous'
            or before.get('energy_commands_granted') is not True):
            return {'command_sent':False,'preflight_verified':False,'reason':'state_changed'}
        # Recheck arming after network preflight. No cross-provider CAS exists;
        # E.ON/Tesla can still change independently between this check and POST.
        if not live_policy_enabled(self.policy_path):
            return {'command_sent':False,'preflight_verified':False,'reason':'live_execution_disabled'}
        if self._expired_lease(expected_tariff_hash)!=lease:
            return {'command_sent':False,'preflight_verified':False,'reason':'publisher_lease_changed'}
        self._pending_lease=lease
        try:return self.transport('self_powered',before,True)
        finally:self._pending_lease=None
