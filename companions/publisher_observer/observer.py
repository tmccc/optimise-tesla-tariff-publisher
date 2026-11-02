"""Read-only publisher health and one explicitly scheduled measurement cycle.

Writes only its own HA sensor states and private evidence files. It cannot
call HA services, write Tesla settings, or change the publisher's policies.
"""
import argparse
from datetime import date, datetime, timezone
import csv
import fcntl
import html
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from observation_store import Store, POWER_FIELDS, epoch, finite, iso
from accounting import summarize_window, simulate_self_powered

ROOT_STATUS=Path.home()/'.local/state/optimise-tesla-tariff-publisher/output'
GATEWAY='http://127.0.0.1:8675'
MODE_NAMES={'autonomous':'Savings','self_consumption':'Self-Powered','backup':'Backup'}
HEALTH_SCALARS=('actual_mode','backup_reserve','last_cycle','last_verified_tariff','lease_expires','watchdog_last_seen')
CYCLE_SCALARS=('net_cost','import_cost','export_revenue','grid_import','grid_export','coverage')
ALLOWED_ENTITIES={'sensor.optimise_publisher_health','sensor.optimise_cycle_observation'}|{'sensor.optimise_publisher_'+s for s in HEALTH_SCALARS}|{'sensor.optimise_cycle_'+s for s in CYCLE_SCALARS}


def read_json(path,default=None):
    try:
        with path.open('rb') as source:data=source.read(200001)
        if len(data)>200000:raise ValueError('Oversized status file')
        return json.loads(data)
    except (OSError,ValueError):return {} if default is None else default


def write_json(path,data):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,indent=2,allow_nan=False)+'\n');temporary.chmod(0o600);temporary.replace(path)


def age(now,value):
    ts=epoch(value)
    return round(now-ts,1) if ts is not None else None


def fresh(now,value,maximum):
    seconds=age(now,value)
    return seconds is not None and -30<=seconds<=maximum


def safe_codes(value):
    if not isinstance(value,list):return []
    return [v for v in value[:30] if isinstance(v,str) and re.fullmatch(r'[a-z0-9_:-]{1,100}',v)]


def display_pause(value):
    """Retain only known human-readable pause fields, never arbitrary objects."""
    if isinstance(value,str):
        return value if re.fullmatch(r'[a-z0-9_:-]{1,100}',value) else 'unrecognised_pause'
    if isinstance(value,dict):
        fields=[value.get(k) for k in ('kind','reason')]
        safe=[v for v in fields if isinstance(v,str) and re.fullmatch(r'[a-z0-9_-]{1,100}',v)]
        return ':'.join(safe) if safe else 'unrecognised_pause'
    return None


def unavailable_planning(status='unavailable',reason='planning_receipt_unavailable'):
    return {'status':status,'reason':reason,'basis':'tesla_planning_surrogate',
            'scope':'quoted_intervals_only','verified_at':None,'current_import_p':None,
            'current_export_p':None,'raw_import_p':None,'raw_export_p':None,
            'rule_id':None,'start_date':None,'end_date_exclusive':None}


def planning_observation(now,receipt,publisher,owner,lease,publication,gateway):
    """Read-only proof of current quoted planning prices from committed records.

    Configured rules or an old successful upload alone do not establish that
    the matching tariff is currently owned, leased, fresh, and in quote scope.
    """
    result=unavailable_planning()
    if not isinstance(receipt,dict) or not receipt:return result
    result=unavailable_planning('configured_unverified','planning_verification_incomplete')
    if not all(isinstance(value,dict) for value in (publisher,owner,lease,publication,gateway)):return result
    policy=receipt.get('planning_policy')
    if not isinstance(policy,dict):return result
    rule_id=policy.get('rule_id')
    if (not isinstance(rule_id,str) or not re.fullmatch(r'[a-z0-9-]{1,100}',rule_id)
            or policy.get('kind')!='eon_earlybird_planning_v1'
            or policy.get('source_price_basis')!='raw_eon_optimise_api'
            or policy.get('timezone')!='Europe/London' or policy.get('unquoted_scope')!='quoted_only'
            or receipt.get('planning_adjustment_scope')!='quoted_intervals_only'):
        return result
    try:
        begin=date.fromisoformat(policy['start_date']);end=date.fromisoformat(policy['end_date_exclusive'])
        if not begin<end or (end-begin).days>367:raise ValueError()
        if any(type(policy.get(k)) not in (int,float) or policy[k]!=3
               for k in ('overnight_reduction_p_per_kwh','peak_export_bonus_p_per_kwh')):raise ValueError()
        digest=hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    except (KeyError,TypeError,ValueError):return result
    if digest!=receipt.get('planning_policy_sha256'):return result
    result.update(rule_id=rule_id,start_date=policy['start_date'],end_date_exclusive=policy['end_date_exclusive'])
    target=receipt.get('tariff_hash');verified=epoch(receipt.get('verified_at'))
    owner_verified=epoch(owner.get('last_verified_at'))
    quoted_from,quoted_until=epoch(receipt.get('known_valid_from')),epoch(receipt.get('known_valid_until'))
    publication_at=epoch(publication.get('resolved_at'))
    generations=[receipt.get('policy_generation'),owner.get('policy_generation'),lease.get('policy_generation')]
    proof=bool(receipt.get('verified') is True and isinstance(target,str) and re.fullmatch(r'[a-f0-9]{64}',target)
        and verified is not None and verified<=now and fresh(now,receipt.get('verified_at'),240)
        and owner_verified is not None and owner_verified<=verified
        and fresh(now,owner.get('last_verified_at'),240) and fresh(now,publisher.get('assessed_at'),240)
        and quoted_from is not None and quoted_until is not None and quoted_from<=now<quoted_until
        and owner.get('verified_publisher_write') is True and owner.get('tariff_semantic_hash')==target
        and owner.get('last_verified_tariff_hash')==target and lease.get('tariff_semantic_hash')==target
        and lease.get('intended_tariff_hash')==target and lease.get('guard_lease_trusted') is True
        and lease.get('publisher_owns_installed_tariff') is True and lease.get('recovery_only') is not True
        and epoch(lease.get('guard_lease_until')) is not None and epoch(lease.get('guard_lease_until'))>now
        and all(isinstance(g,str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,150}',g) for g in generations)
        and generations[0]==generations[1]==generations[2]
        and publisher.get('source_valid') is True and publisher.get('owns_installed_tariff') is True
        and not publisher.get('pause_latch') and 'pause' not in str(publisher.get('action',''))
        and gateway.get('stale') is False
        and publication.get('verified') is True and publication.get('candidate_hash')==target
        and publication_at is not None and verified>=publication_at)
    if not proof:return result
    def current(rows):
        if not isinstance(rows,list) or len(rows)>200:return None
        selected=[]
        for row in rows:
            if not isinstance(row,dict):return None
            left,right=epoch(row.get('valid_from')),epoch(row.get('valid_to'))
            if left is not None and right is not None and left<=now<right:
                if right-left!=1800:return None
                selected.append(row)
        return selected[0] if len(selected)==1 else None
    raw,current_plan=current(receipt.get('quoted_intervals')),current(receipt.get('planning_quoted_intervals'))
    if not raw or not current_plan:return result
    if any(raw.get(k)!=current_plan.get(k) for k in ('valid_from','valid_to')):return result
    def price(row,direction):
        value=row.get(direction+'_gbp_per_kwh')
        if isinstance(value,bool) or not isinstance(value,(str,int,float)):return None
        try:return finite(float(value)*100)
        except (TypeError,ValueError,OverflowError):return None
    values={prefix+direction+'_p':price(row,direction) for prefix,row in (('raw_',raw),('current_',current_plan)) for direction in ('import','export')}
    if any(v is None or v<0 for v in values.values()):return result
    if values['current_import_p']<values['current_export_p']:return result
    local=datetime.fromtimestamp(epoch(raw['valid_from']),timezone.utc).astimezone(ZoneInfo('Europe/London'))
    minute=local.hour*60+local.minute
    eligible=begin<=local.date()<end
    offsets={'import':-3 if eligible and minute<360 else 0,
             'export':-3 if eligible and minute<360 else 3 if eligible and 960<=minute<1140 else 0}
    if any(abs(values['current_'+direction+'_p']-values['raw_'+direction+'_p']-offsets[direction])>1e-7 for direction in ('import','export')):return result
    result.update(status='verified',reason='matching_committed_planning_tariff',verified_at=receipt['verified_at'],
                  tariff_hash=target,policy_generation=generations[0],**values)
    return result


def get_json(url,token=None,body=None):
    headers={'Accept':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    if body is not None:headers['Content-Type']='application/json'
    req=Request(url,headers=headers,data=None if body is None else json.dumps(body,allow_nan=False).encode())
    with urlopen(req,timeout=8) as response:data=response.read(2000001)
    if len(data)>2000000:raise ValueError('Response exceeds limit')
    return json.loads(data)


class Observer:
    def __init__(self,config):
        self.config=config
        self.directory=Path(config['state_dir']);self.directory.mkdir(parents=True,mode=0o700,exist_ok=True)
        if config['hass_url']!='http://127.0.0.1:8123':raise ValueError('Only local HA is permitted')
        self.store=Store(self.directory/'cycle.sqlite3')
        self.start=epoch(config['record_from']);self.until=epoch(config['capture_until'])
        self.windows={k:(epoch(v[0]),epoch(v[1])) for k,v in config['windows'].items()}
        if self.until-self.start>172800 or self.until<=self.start:raise ValueError('Measurement must be bounded to two days')
        self.ha_token=None
        self.current_tesla_planning=unavailable_planning()
        self.customer_pricing=None
        if config.get('customer_pricing_file'):
            sys.path.insert(0,str(Path.home()/'.local/lib/optimise-customer-pricing'))
            from customer_pricing import load_policy
            self.customer_pricing=load_policy(config['customer_pricing_file'])

    def ha(self,entity,data=None):
        if data is not None and entity not in ALLOWED_ENTITIES:raise ValueError('Only observer sensor writes are permitted')
        if not re.fullmatch(r'(sensor|binary_sensor)\.[a-z0-9_]+',entity):raise ValueError('Invalid sensor path')
        if self.ha_token is None:self.ha_token=Path(self.config['token_file']).read_text().strip()
        return get_json(self.config['hass_url']+'/api/states/'+entity,self.ha_token,data)

    def state(self,key):
        try:return self.ha(self.config['entities'][key])
        except Exception:return {}

    def local(self,path,errors,name):
        try:return get_json(GATEWAY+path)
        except Exception as error:errors[name]=type(error).__name__;return {}

    def snapshot(self,now):
        errors={}
        operation=self.local('/api/operation',errors,'operation')
        meters=self.local('/api/meters/aggregates',errors,'meters')
        system=self.local('/api/system_status',errors,'storage')
        soe=self.local('/api/system_status/soe',errors,'soc')
        try:monitor=get_json('http://127.0.0.1:8677/metrics')
        except Exception as error:errors['freshness']=type(error).__name__;monitor={}
        connection=monitor.get('connection') or {}
        source_fresh=bool(monitor.get('monitor_fresh') is True and connection.get('fresh') is True and connection.get('online') is True and finite(connection.get('data_age_seconds')) is not None and 0<=connection['data_age_seconds']<=30)
        counters={}
        for k in ('site','load','battery','solar'):
            meter=meters.get(k) or {}
            counters[k]={n:finite(meter.get(n)) for n in ('instant_power','energy_imported','energy_exported')}
            counters[k]['last_communication_time']=meter.get('last_communication_time')
        inventory={'remaining_kwh':finite(system.get('nominal_energy_remaining')),'full_pack_kwh':finite(system.get('nominal_full_pack_energy')),
                   'soc_percent':finite(soe.get('percentage')),'raw_soc_percent':finite(soe.get('raw_percentage')),
                   'max_charge_kw':finite(system.get('max_charge_power')),'max_discharge_kw':finite(system.get('max_discharge_power')),
                   'native_inventory_fetch_time_unverified':True}
        for k in ('remaining_kwh','full_pack_kwh','max_charge_kw','max_discharge_kw'):
            if inventory[k] is not None:inventory[k]/=1000
        return {'captured_at':now,'source_fresh':source_fresh,'source_age_seconds':finite(connection.get('data_age_seconds')),
                'source_timestamp':finite(connection.get('last_source_timestamp')),'monitor_updated_at':monitor.get('updated_at'),
                'inventory':inventory,'counters':counters,'gateway':{k:operation.get(k) for k in ('real_mode','backup_reserve_percent','grid_charging','grid_export','stale')},'errors':errors}

    def health(self,now,observation,prices,status,eon):
        p=read_json(ROOT_STATUS/'recurring-status.json');w=read_json(ROOT_STATUS/'watchdog-health.json')
        wo=read_json(ROOT_STATUS/'watchdog-observation.json');lease=read_json(ROOT_STATUS/'publisher-lease.json');publication=read_json(ROOT_STATUS/'last-tariff-publication.json')
        owner=read_json(ROOT_STATUS/'publisher-ownership.json');planning=read_json(ROOT_STATUS/'planning-status.json')
        prices_stamp=prices.get('state');eon_stamp=status.get('state')
        enrolled={'on':True,'off':False}.get(eon.get('state'))
        lease_end=epoch(lease.get('guard_lease_until'))
        publisher={'last_cycle':p.get('assessed_at'),'age_seconds':age(now,p.get('assessed_at')),'action':p.get('action'),
                   'reasons':safe_codes(p.get('reason_codes')),'pause_latch':display_pause(p.get('pause_latch')),
                   'pause_created_at':iso(epoch(p['pause_latch'].get('created_at'))) if isinstance(p.get('pause_latch'),dict) and epoch(p['pause_latch'].get('created_at')) is not None else None,
                   'source_valid':p.get('source_valid') is True,'owns_tariff':p.get('owns_installed_tariff') is True,
                   'last_verified_publication':publication.get('resolved_at') if publication.get('verified') is True else None,
                   'publication_verified':publication.get('verified') is True,'lease_expires':lease.get('guard_lease_until'),
                   'lease_remaining_seconds':round(lease_end-now,1) if lease_end is not None else None}
        watchdog={'last_seen':w.get('cycle_completed_at'),'age_seconds':age(now,w.get('cycle_completed_at')),'state':w.get('state'),
                  'status':wo.get('status'),'fresh':fresh(now,w.get('cycle_completed_at'),120)}
        gateway=observation['gateway'];gate={'actual_mode':MODE_NAMES.get(gateway.get('real_mode'),'Unknown'),
            'backup_reserve_percent':finite(gateway.get('backup_reserve_percent')),'grid_charging':gateway.get('grid_charging'),
            'grid_export':gateway.get('grid_export'),'stale':gateway.get('stale')}
        state='healthy'
        if not fresh(now,p.get('assessed_at'),240) or not watchdog['fresh'] or gate['stale'] is not False:state='stale'
        elif publisher['pause_latch'] or (isinstance(p.get('action'),str) and 'pause' in p['action']):state='paused'
        elif not publisher['source_valid'] or not publisher['owns_tariff'] or lease_end is None or lease_end<=now or not observation['source_fresh']:state='degraded'
        self.current_tesla_planning=planning_observation(now,planning,p,owner,lease,publication,observation['gateway'])
        attrs={'friendly_name':'Optimise publisher health','updated_at':iso(now),'publisher':publisher,'watchdog':watchdog,'gateway':gate,'planning':self.current_tesla_planning,
               'eon':{'enrolled':enrolled,'last_success':eon_stamp,'fresh':fresh(now,eon_stamp,2700)},
               'prices':{'last_success':prices_stamp,'fresh':fresh(now,prices_stamp,900)}}
        return state,attrs

    def capture_quotes(self,now,prices_success):
        current={}
        for channel in ('import','export'):
            state=self.state('optimise_'+channel);a=state.get('attributes') or {};p=a.get('period') or {}
            current[channel+'_p']=finite(p.get('value'))
            if fresh(now,prices_success,900) and a.get('unit_of_measurement')=='p/kWh':
                begin,end=epoch(p.get('valid_from')),epoch(p.get('valid_to'))
                if begin is not None and end is not None and begin<=now<end:
                    self.store.add_quote('optimise_'+channel,p['valid_from'],p['valid_to'],p.get('value'),now)
            stamp=state.get('last_updated')
            metadata_key='forecast_'+channel
            if stamp and self.store.metadata(metadata_key)!=stamp:
                allowed=('valid_from','valid_to','value','quality','estimate','interval_basis')
                rows=[{k:p.get(k) for k in allowed} for p in [a.get('period') or {}]+(a.get('forecast') or [])[:100]]
                self.store.db.execute('INSERT INTO metadata(key,value) VALUES(?,?)',('forecast_snapshot_'+channel+'_'+str(now),json.dumps({'captured_at':iso(now),'rows':rows},allow_nan=False)))
                self.store.set_metadata(metadata_key,stamp)
        comparison=self.state('comparison');a=comparison.get('attributes') or {}
        for channel in ('import','export'):
            source=(a.get('sources') or {}).get('agile_'+channel) or {}
            if fresh(now,source.get('last_success'),3600):
                for row in (a.get('rows') or [])[:250]:
                    self.store.add_quote('agile_'+channel,row.get('start'),row.get('end'),row.get('agile_'+channel),now)
        return current

    def raw_samples(self,now,observation,previous):
        # Successful query coverage is distinct from the last available sample:
        # real data gaps must not trap the cursor, and a backlog must drain before
        # finalization. Three bounded 30-minute reads recover at most 90 min/cycle.
        for _ in range(3):
            cursor=self.store.metadata('power_query_cursor',self.start-10)
            if cursor>=min(now,self.until):return
            start=max(self.start-10,cursor-30)
            end=min(now,self.until,cursor+1800)
            if start>=end:return
            result=get_json(GATEWAY+'/api/timeseries/samples?'+urlencode({'gateway':'default','start':start,'end':end,'limit':500}))
            rows=result.get('samples') or []
            if not isinstance(rows,list) or len(rows)>500:raise ValueError('Invalid bounded power samples')
            truncated=len(rows)==500
            rows=[r for r in rows if isinstance(r,dict) and finite(r.get('ts')) is not None and start<=r['ts']<=end]
            self.store.add_samples(rows,self.start,observation,previous,record_until=self.until)
            advanced=end if not truncated else max((r['ts'] for r in rows),default=cursor)
            if advanced<=cursor:raise ValueError('Power history query made no progress')
            self.store.set_metadata('power_query_cursor',advanced)

    def summaries(self,now):
        overall_start,overall_end=self.windows['overall']
        segments=self.store.priced_segments(overall_start,overall_end)
        summary={k:summarize_window(segments,start,end,now=now) for k,(start,end) in self.windows.items()}
        for key,(start,end) in self.windows.items():
            if now<start:
                for field in ('coverage_percent','priced_coverage_percent','monitored_coverage_percent','net_cost_gbp','import_cost_gbp','export_revenue_gbp','same_trace_agile_net_cost_gbp'):
                    summary[key][field]=None
        return segments,summary

    def customer_segments(self,segments):
        """Apply the personal offer in memory; never rewrite captured quotes."""
        if self.customer_pricing is None:return segments
        effective=[]
        for original in segments:
            row=dict(original)
            for direction in ('import','export'):
                field='optimise_'+direction+'_p'
                raw=original.get('raw_'+field,original.get(field))
                adjusted=self.customer_pricing.adjust_price(raw,direction,original['start'],original['end'])
                row['raw_'+field]=raw
                row[field]=adjusted['effective_p_per_kwh']
                row[direction+'_adjustment_p']=adjusted['adjustment_p_per_kwh']
            effective.append(row)
        return effective

    def customer_summaries(self,now,segments,raw_summary):
        effective=self.customer_segments(segments)
        if self.customer_pricing is None:return effective,raw_summary
        summaries={scope:summarize_window(effective,start,end,now=now) for scope,(start,end) in self.windows.items()}
        for scope,(start,end) in self.windows.items():
            raw=raw_summary[scope];summary=summaries[scope]
            for source,target,sign in (('import_cost_gbp','import_discount_gbp',-1),('export_revenue_gbp','export_bonus_gbp',1),('net_cost_gbp','total_incentive_gbp',-1)):
                a,b=finite(raw.get(source)),finite(summary.get(source))
                summary[target]=None if a is None or b is None else sign*(b-a)
            summary['pricing_basis']='customer_adjusted_optimise'
            if now<start:
                for field in ('coverage_percent','priced_coverage_percent','monitored_coverage_percent','net_cost_gbp','import_cost_gbp','export_revenue_gbp','same_trace_agile_net_cost_gbp','import_discount_gbp','export_bonus_gbp','total_incentive_gbp'):
                    summary[field]=None
        return effective,summaries

    def model(self,now,segments):
        start,end=self.windows['overall']
        if now<self.until:return {'status':'pending_complete_cycle','notes':['Controller savings need a counterfactual and final battery inventory.']}
        first,last=self.store.inventory_near(start),self.store.inventory_near(end)
        if not first or not last:return {'status':'not_estimated','reason':'Missing boundary inventory observation'}
        observations=self.store.observations_between(start,end)
        reserves={o.get('gateway',{}).get('backup_reserve_percent') for o in observations}
        if reserves!={10} or any(o.get('eon_enrolled') is not False or o.get('eon_fresh') is not True or o.get('publisher_pause') for o in observations):
            return {'status':'not_estimated','reason':'Reserve or control ownership changed; requires review'}
        inventory=first['inventory'];final=last['inventory']
        required=('remaining_kwh','full_pack_kwh','max_charge_kw','max_discharge_kw')
        if any(finite(inventory.get(k)) is None for k in required) or finite(final.get('remaining_kwh')) is None:
            return {'status':'not_estimated','reason':'Missing or invalid physical model inputs'}
        if (inventory['full_pack_kwh']<=0 or min(inventory['max_charge_kw'],inventory['max_discharge_kw'])<=0
                or not 0<=inventory['remaining_kwh']<=inventory['full_pack_kwh']
                or not 0<=final['remaining_kwh']<=inventory['full_pack_kwh']):
            return {'status':'not_estimated','reason':'Physical model inputs outside supported bounds'}
        scenarios=[]
        for reserve_fraction in (0.10,0.145):
            for efficiency in (0.85,0.90,0.95):
                result=simulate_self_powered(segments,start,end,inventory['remaining_kwh'],inventory['full_pack_kwh'],inventory['full_pack_kwh']*reserve_fraction,efficiency,min(inventory['max_charge_kw'],inventory['max_discharge_kw']),actual_final_stored_kwh=final['remaining_kwh'])
                scenarios.append({'raw_capacity_reserve_fraction':reserve_fraction,'roundtrip_efficiency':efficiency,'result':result})
        return {'status':'illustrative_model_only' if any(s['result'].get('eligible') for s in scenarios) else 'not_estimated','scenarios':scenarios,'notes':[
            'Same observed household/EV load and solar generation; no grid charging or deliberate battery export in the Self-Powered model.',
            'Native energy inventory freshness is independently unverified. Displayed SOC is not multiplied by raw full-pack capacity.',
            'Reserve mapping and efficiency are sensitivity assumptions, not verified hardware measurements.',
            'Terminal energy values are scenarios, not future price quotes. Curtailment or changed EV behaviour can invalidate this counterfactual.',
            'These estimates are not a measured causal saving or a supplier bill.']}

    def window_results(self,now,summary,raw_summary=None):
        """Freeze each result after a short ingestion buffer and drained history.

        Readiness means the observation is ready to review, including any gaps;
        it never implies complete pricing or proven savings. HA owns alerts.
        """
        results={}
        raw_summary=summary if raw_summary is None else raw_summary
        for scope in ('overnight','peak'):
            start,end=self.windows[scope]
            key=hashlib.sha256(json.dumps([self.start,self.until,scope,start,end],separators=(',',':')).encode()).hexdigest()[:20]
            saved=self.store.metadata('window_result_'+key)
            if saved:
                entry=saved
            else:entry={'status':'pending','key':key,'start':iso(start),'end':iso(end),'ready_at':None}
            if not saved and now>=end+120 and self.store.metadata('power_query_cursor',0)>=min(end+120,self.until):
                initial,ending=self.store.inventory_near(start),self.store.inventory_near(end)
                entry.update(status='ready',ready_at=iso(now),summary=raw_summary[scope],inventory={
                    'initial_remaining_kwh':(initial or {}).get('inventory',{}).get('remaining_kwh'),
                    'ending_remaining_kwh':(ending or {}).get('inventory',{}).get('remaining_kwh'),
                    'initial_soc_percent':(initial or {}).get('inventory',{}).get('soc_percent'),
                    'ending_soc_percent':(ending or {}).get('inventory',{}).get('soc_percent')})
                write_json(self.directory/(scope+'-result.json'),entry)
                self.store.set_metadata('window_result_'+key,entry)
                self.store.commit()
            if entry['status']=='ready':
                if self.customer_pricing is None:
                    summary[scope]=entry['summary']
                else:
                    pricing=self.customer_pricing.metadata()
                    revision_key='window_result_'+key+'_pricing_'+pricing['policy_sha256'][:20]
                    revised=self.store.metadata(revision_key)
                    if revised is None:
                        revised={**entry,'summary':summary[scope],'raw_summary':raw_summary[scope],
                                 'pricing':{**pricing,'basis':'customer_adjusted_optimise'},
                                 'current_tesla_planning':self.current_tesla_planning,
                                 'revision_created_at':iso(now),'revision_of_raw_key':key,
                                 'revision_notes':['Original unadjusted result and captured provider quotes are preserved.']}
                        if entry['summary']!=raw_summary[scope]:revised['original_raw_summary']=entry['summary']
                        rule_id=pricing['rule_id']
                        if not re.fullmatch(r'[a-z0-9-]{1,100}',rule_id):raise ValueError('Unsafe pricing rule identifier')
                        write_json(self.directory/(scope+'-result-'+rule_id+'.json'),revised)
                        self.store.set_metadata(revision_key,revised);self.store.commit()
                    entry=revised
                    summary[scope]=entry['summary']
                    raw_summary[scope]=entry['raw_summary']
            results[scope]=entry
        return results

    def report(self,now,observation):
        final_ready=now>=self.until and self.store.metadata('power_query_cursor',0)>=self.until
        raw_segments,raw_summary=self.summaries(now)
        segments,summary=self.customer_summaries(now,raw_segments,raw_summary)
        window_results=self.window_results(now,summary,raw_summary)
        pricing=({'basis':'raw_optimise_api_rates','customer_adjustment_applied':False} if self.customer_pricing is None
                 else {**self.customer_pricing.metadata(),'basis':'customer_adjusted_optimise','customer_adjustment_applied':True})
        first=self.store.inventory_near(self.windows['overall'][0])
        ending=self.store.inventory_near(self.windows['overall'][1]) if now>=self.windows['overall'][1] else None
        observed_inventory=(ending or observation)['inventory']
        inventory={'initial_remaining_kwh':(first or {}).get('inventory',{}).get('remaining_kwh'),
                   'current_remaining_kwh':observed_inventory.get('remaining_kwh'),'full_pack_kwh':observed_inventory.get('full_pack_kwh'),
                   'initial_soc_percent':(first or {}).get('inventory',{}).get('soc_percent'),'current_soc_percent':observed_inventory.get('soc_percent')}
        if now<self.windows['overall'][0]:phase='prelude'
        elif now<self.windows['overnight'][1]:phase='overnight'
        elif now<self.windows['peak'][0]:phase='daytime'
        elif now<self.windows['peak'][1]:phase='peak'
        elif now<self.until:phase='post_peak'
        else:phase='complete' if final_ready else 'finalizing'
        attrs={'friendly_name':'Optimise cycle observation','updated_at':iso(now),'record_from':iso(self.start),'capture_until':iso(self.until),
               'analysis_start':iso(self.windows['overall'][0]),'analysis_end':iso(self.windows['overall'][1]),'current_phase':phase,
               'raw_samples':self.store.sample_count(),'measurement_coverage_percent':summary['overall'].get('coverage_percent'),
               'summary':summary,'raw_summary':raw_summary,'pricing':pricing,'current_tesla_planning':observation.get('current_tesla_planning',self.current_tesla_planning),'inventory':inventory,'model_status':'pending_complete_cycle','window_results':window_results,
               'notes':['Energy is integrated from sampled gateway directional power, with gaps over 30 seconds left uncovered.',
                        'Source freshness is checked at observation endpoints; individual raw power samples lack underlying source-age metadata.',
                        'Costs use observed Optimise delivery-period quotes, not forecasts or settled bills; incomplete priced energy is shown separately.',
                        'Grid exports include solar. Export income is not automatically a battery-trading profit.',
                        'Same-flow Agile repricing compares tariffs only, not different battery operation.'],
               'quote_revisions':self.store.quote_revisions()}
        if self.customer_pricing is not None:
            attrs['notes'].append('Personal early-bird offer is applied once to measured unit costs: 3p/kWh import discount at 00:00-06:00 and 3p/kWh export bonus at 16:00-19:00, Europe/London; raw prices and original reports remain preserved.')
        if final_ready and not self.store.metadata('complete_report'):
            model=self.model(now,segments)
            model['pricing']=pricing
            attrs['model_status']=model['status']
            complete={'recording':attrs,'controller_saving_estimate':model}
            write_json(self.directory/'final-report.json',complete)
            with (self.directory/'priced-segments.csv').open('w',newline='') as output:
                fields=('start','end',*(k+'_kwh' for k in POWER_FIELDS),'raw_optimise_import_p','raw_optimise_export_p','optimise_import_p','optimise_export_p','import_adjustment_p','export_adjustment_p','agile_import_p','agile_export_p','quality')
                writer=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(segments)
            analysis_date=datetime.fromtimestamp(self.windows['overall'][0],timezone.utc).astimezone(ZoneInfo('Europe/London')).strftime('%d %B %Y')
            content='<h1>Optimise overnight and peak observation</h1><p>'+html.escape(analysis_date)+' · sampled gateway estimates, not a settled bill.</p>'
            if self.customer_pricing is not None:
                content+='<p>Customer-adjusted costs include the early-bird import discount and export bonus. Original supplier quotes and raw totals are retained.</p>'
            for name,data in summary.items():
                content+='<h2>'+html.escape(name.title())+'</h2><table>'+''.join('<tr><th>'+html.escape(k)+'</th><td>'+html.escape(str(v))+'</td></tr>' for k,v in data.items() if not isinstance(v,(list,dict)))+'</table>'
            if self.customer_pricing is not None:
                content+='<h2>Unadjusted provider-rate totals</h2><pre>'+html.escape(json.dumps(raw_summary,indent=2))+'</pre>'
            content+='<h2>Conditional Self-Powered comparison</h2><pre>'+html.escape(json.dumps(model,indent=2))+'</pre>'
            (self.directory/'final-report.html').write_text('<!doctype html><meta charset="utf-8"><title>Optimise cycle report</title><style>body{font:16px Arial;max-width:900px;margin:40px auto;line-height:1.5}th,td{padding:4px 15px;text-align:left;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}</style>'+content)
            self.store.set_metadata('complete_report',{'created_at':iso(now),'model_status':model['status']});self.store.commit()
        complete=read_json(self.directory/'final-report.json') if now>=self.until else {}
        if complete:
            model=complete.get('controller_saving_estimate') or {}
            attrs['model_status']=model.get('status','not_estimated')
            eligible=[s['result'] for s in model.get('scenarios',[]) if s['result'].get('eligible')]
            attrs['model_summary']={'eligible_scenarios':len(eligible),'notes':model.get('notes',[])}
            for key,target in (('unadjusted_cash_difference_gbp','cash_difference_gbp'),('ending_inventory_delta_kwh','ending_inventory_delta_kwh')):
                values=[r[key] for r in eligible if finite(r.get(key)) is not None]
                attrs['model_summary'][target+'_min']=min(values) if values else None
                attrs['model_summary'][target+'_max']=max(values) if values else None
            values=[r['cash_difference_gbp'] for s in eligible for r in s.get('adjusted_estimates',[]) if finite(r.get('cash_difference_gbp')) is not None]
            attrs['model_summary']['inventory_adjusted_scenario_gbp_min']=min(values) if values else None
            attrs['model_summary']['inventory_adjusted_scenario_gbp_max']=max(values) if values else None
        write_json(self.directory/'summary.json',attrs)
        return ('complete' if final_ready else 'recording'),attrs

    def publish(self,state,health,cycle_state,cycle):
        self.ha('sensor.optimise_publisher_health',{'state':state,'attributes':health})
        self.ha('sensor.optimise_cycle_observation',{'state':cycle_state,'attributes':cycle})
        p,w,g=health['publisher'],health['watchdog'],health['gateway']
        values={'actual_mode':g['actual_mode'],'backup_reserve':g['backup_reserve_percent'],'last_cycle':p['last_cycle'],
                'last_verified_tariff':p['last_verified_publication'],'lease_expires':p['lease_expires'],'watchdog_last_seen':w['last_seen']}
        for key,value in values.items():
            attrs={'friendly_name':'Optimise publisher '+key.replace('_',' '),'updated_at':health['updated_at']}
            if key=='backup_reserve':attrs['unit_of_measurement']='%'
            elif key not in ('actual_mode',):attrs['device_class']='timestamp'
            self.ha('sensor.optimise_publisher_'+key,{'state':'unavailable' if value is None else value,'attributes':attrs})
        overall=cycle['summary']['overall']
        for key,source,unit in [('net_cost','net_cost_gbp','GBP'),('import_cost','import_cost_gbp','GBP'),('export_revenue','export_revenue_gbp','GBP'),('grid_import','grid_import_kwh','kWh'),('grid_export','grid_export_kwh','kWh'),('coverage','coverage_percent','%')]:
            value=overall.get(source);attrs={'friendly_name':'Optimise cycle '+key.replace('_',' '),'unit_of_measurement':unit,'updated_at':cycle['updated_at']}
            self.ha('sensor.optimise_cycle_'+key,{'state':'unavailable' if value is None else round(value,6),'attributes':attrs})

    def cycle(self):
        now=time.time();self.ha_token=None
        observation=self.snapshot(now)
        prices=self.state('prices_success');status=self.state('status_success');enrolled=self.state('battery_enrolled')
        health_state,health=self.health(now,observation,prices,status,enrolled)
        observation['eon_enrolled']=health['eon']['enrolled'];observation['eon_fresh']=health['eon']['fresh'];observation['publisher_pause']=health['publisher']['pause_latch']
        observation['publisher']=health['publisher']
        observation['current_tesla_planning']=health.get('planning',unavailable_planning())
        if now<=self.until or not self.store.metadata('complete_report'):
            previous=self.store.previous_observation()
            try:self.raw_samples(now,observation,previous)
            except Exception as error:observation['errors']['raw_samples']=type(error).__name__
            health['prices'].update(self.capture_quotes(now,prices.get('state')))
            self.store.add_observation(observation)
            self.store.commit()
        cycle_state,cycle=self.report(now,observation)
        self.publish(health_state,health,cycle_state,cycle)
        result={'at':iso(now),'publisher_health':health_state,'cycle':cycle_state,'phase':cycle['current_phase'],'raw_samples':cycle['raw_samples'],'errors':observation['errors']}
        write_json(self.directory/'observer-status.json',result);print(json.dumps(result),flush=True)


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--watch',action='store_true');a=p.parse_args()
    observer=Observer(json.loads(a.config.read_text()));lock=(observer.directory/'observer.lock').open('w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('Observer already running')
    while True:
        try:observer.cycle()
        except Exception as error:
            print(json.dumps({'at':iso(time.time()),'error':type(error).__name__}),flush=True)
            if not a.watch:raise SystemExit(1)
        if not a.watch:break
        time.sleep(60-time.time()%60+3)


if __name__=='__main__':main()
