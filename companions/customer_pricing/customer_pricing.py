"""Account-holder-confirmed incentives over immutable supplier base prices.

Pure arithmetic and local policy loading only. No HA or Tesla calls. Prices
remain p/kWh; the confirmed three-pence amount is not multiplied by VAT again.
"""
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


def instant(value):
    if isinstance(value,datetime):result=value
    elif isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
        result=datetime.fromtimestamp(value,timezone.utc)
    elif isinstance(value,str):result=datetime.fromisoformat(value.replace('Z','+00:00'))
    else:raise ValueError('Expected an aware instant')
    if result.tzinfo is None:raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)


def number(value):
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)):raise ValueError('Price must be numeric')
    result=Decimal(str(value))
    if not result.is_finite():raise ValueError('Price must be finite')
    return result


class Policy:
    def __init__(self,config):
        self.config=dict(config)
        if config.get('schema_version')!=1 or config.get('source_prices_include_incentive') is not False:
            raise ValueError('Explicit raw-price basis required')
        self.rule_id=config['rule_id']
        if not isinstance(self.rule_id,str) or not self.rule_id or len(self.rule_id)>100:raise ValueError('Rule identifier required')
        if config.get('timezone')!='Europe/London':raise ValueError('UK local-time policy required')
        self.zone=ZoneInfo(config['timezone'])
        self.start=date.fromisoformat(config['start_date']);self.end=date.fromisoformat(config['end_date_exclusive'])
        if self.end<=self.start:raise ValueError('Bounded eligibility period required')
        self.amounts={'import':-number(config['import_discount_p']),'export':number(config['export_bonus_p'])}
        if self.amounts['import']>0 or self.amounts['export']<0:raise ValueError('Invalid incentive direction')
        self.windows={}
        for direction in ('import','export'):
            window=config[direction+'_window']
            def minute(value):
                parsed=datetime.strptime(value,'%H:%M')
                return parsed.hour*60+parsed.minute
            begin,finish=minute(window['start']),minute(window['end'])
            if not 0<=begin<finish<=1440:raise ValueError('Non-wrapping window required')
            self.windows[direction]=(begin,finish)
        self.sha256=hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def metadata(self):
        keys=('rule_id','timezone','start_date','end_date_exclusive','import_discount_p','import_window',
              'export_bonus_p','export_window','source_prices_include_incentive')
        return {**{k:self.config[k] for k in keys},'policy_sha256':self.sha256,
                'basis':'Customer-confirmed incentive over original E.ON API unit prices; no additional VAT conversion'}

    def _state(self,direction,when):
        local=when.astimezone(self.zone)
        eligible=self.start<=local.date()<self.end
        minute=local.hour*60+local.minute+local.second/60+local.microsecond/60_000_000
        begin,end=self.windows[direction]
        adjustment=self.amounts[direction] if eligible and begin<=minute<end else Decimal(0)
        return eligible,adjustment

    def adjust_price(self,raw,direction,start,end):
        if direction not in self.windows:raise ValueError('Unknown price direction')
        left,right=instant(start),instant(end)
        if not 0<(right-left).total_seconds()<=1800.000001:raise ValueError('Use split intervals up to thirty minutes')
        a,b=self._state(direction,left),self._state(direction,right-timedelta(microseconds=1))
        if a!=b:raise ValueError('Split interval at the incentive boundary')
        original=number(raw);eligible,adjustment=a
        effective=None if original is None else original+adjustment
        return {'raw_p_per_kwh':None if original is None else float(original),
                'effective_p_per_kwh':None if effective is None else float(effective),
                'adjustment_p_per_kwh':float(adjustment),'eligible':eligible,
                'applied':original is not None and adjustment!=0,'rule_id':self.rule_id,
                'reason':'missing_base_price' if original is None else 'incentive_applied' if adjustment else 'outside_window' if eligible else 'outside_eligibility'}


def load_policy(path):
    return None if not path else Policy(json.loads(Path(path).read_text()))
