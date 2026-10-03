"""纯HTTP真实数据；仅进程内TTL缓存，不读取或写入任何行情缓存文件。"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests
from metrics import operating_improvement, same_period_last_year

ROOT = Path(__file__).resolve().parent
SNAPSHOT_TTL, DATA_TTL = 60, 3600
SOURCE_DESCRIPTIONS = {
 'spot': '腾讯HTTP实时快照（免费源可能延迟，实际时点以响应为准）',
 'basic': '新浪HTTP全市场列表，仅沪深A股',
 'financial': '东方财富业绩报表，经AkShare；报告期与公告日分开',
 'tencent_qfq': '腾讯HTTP前复权日线；收益率由前复权收盘价计算',
}
# 前一阶段已核实的字段，加上本次与新浪同日值/代数关系交叉核验的字段。
SPOT_FIELDS = dict(name=1, code6=2, price=3, prev_close=4, open=5,
 price_change=31, pct_chg=32, high=33, low=34, volume_hand=36, amount_wan=57,
 turnover_pct=38, pe_ttm=39, flag=40, amplitude=43, float_mv_yi=44,
 total_mv_yi=45, pb=46, limit_up=47, limit_down=48, volume_ratio=49,
 vwap=51, pe_dynamic=52, pe_static=53, float_shares=72, total_shares=73, quote_time=30)
SPOT_NUMERIC = [k for k in SPOT_FIELDS if k not in ('name','code6','flag','quote_time')]
FIN_NUMERIC = ['eps','rev_yoy','np_yoy','np_value','roe','bps','ocfps','gross_margin']
PROBE_CODES = ('600000','000001','600519')  # 用户要求的启动探针；不参与选股或排序。
UNKNOWN_FIELDS = [i for i in range(88) if i not in set(SPOT_FIELDS.values())]
FIELD_NOTES = '6与36、37与57、41/42与33/34样本值重复或精度不同，不另建指标；其余未知字段不作语义推断；未确认52周高低点。'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
_LOCK = threading.RLock()
_CACHE = {}
_MODE = None
_SESSION = None
_LAST_REQUEST = 0.0
_PROBED = False
_KLINE_BLOCKED_UNTIL = 0.0

class DataError(RuntimeError): pass
class EmptyReport(DataError): pass
class SourceBlocked(DataError): pass

def today_shanghai(): return datetime.now(ZoneInfo('Asia/Shanghai')).date()
def now_iso(): return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
def code6(value):
    value = re.sub(r'^(sh|sz|bj)\.?', '', str(value).strip().lower())
    if not value.isdigit() or len(value)>6: raise ValueError(f'无效代码: {value}')
    return value.zfill(6)
def market_symbol(value):
    value=code6(value)
    if value[0] in '69': return 'sh'+value
    if value[0] in '023': return 'sz'+value
    raise ValueError('仅支持沪深代码')
def clear_memory_cache():
    with _LOCK: _CACHE.clear()
def report_candidates(as_of, count=12):
    periods=[f'{year}{q}' for year in range(as_of.year-4,as_of.year+1) for q in ('0331','0630','0930','1231')]
    return sorted((p for p in periods if p<=as_of.strftime('%Y%m%d')),reverse=True)[:count]
def _numeric(frame,columns):
    for c in columns:
        if c in frame: frame[c]=pd.to_numeric(frame[c],errors='coerce').astype('float64')
    return frame

def _find_column(frame,*alternatives):
    for keywords in alternatives:
        if isinstance(keywords,str): keywords=(keywords,)
        hits=[c for c in frame.columns if all(k in str(c) for k in keywords)]
        if len(hits)==1:return hits[0]
    raise DataError(f'列无法唯一匹配{alternatives}；实际列={list(frame.columns)}')

@dataclass
class FetchStats:
    cache_hits:int=0
    cache_misses:int=0
    http_requests:int=0
    events:list=field(default_factory=list)

@dataclass
class UniverseData:
    frame:pd.DataFrame
    funnel:pd.DataFrame
    as_of:date
    report_period:str|None
    financials:dict
    sources:dict
    snapshot_coverage:float
    missing_snapshot_codes:list
    financial_available:bool=True
    warnings:list=field(default_factory=list)
    available_metrics:set=field(default_factory=set)
    snapshot_time:str=''
    fetched_at:str=''
    status:dict=field(default_factory=dict)

@dataclass
class MarketOverview:
    frame:pd.DataFrame
    snapshot_time:str
    fetched_at:str
    coverage:float
    missing_codes:list=field(default_factory=list)

class DataClient:
    def __init__(self, *, logger=print, force_refresh=False, financial_disabled=False, universe=None):
        self.logger=logger; self.force_refresh=force_refresh; self.financial_disabled=financial_disabled
        self.stats=FetchStats(); self.pinned_universe=universe
        self.today=today_shanghai(); self.warnings=[]
    def log(self,message):
        self.stats.events.append(str(message))
        if self.logger:self.logger(str(message))
    def cached(self,key,ttl,loader):
        with _LOCK:
            if self.force_refresh:
                self.stats.cache_misses+=1
                return loader()  # 用户主动执行：不读旧缓存，也不把本次数据写入共享缓存。
            expired=[k for k,(deadline,_) in _CACHE.items() if deadline<=time.monotonic()]
            for stale in expired:del _CACHE[stale]
            item=_CACHE.get(key)
            if not self.force_refresh and item and item[0]>time.monotonic():
                self.stats.cache_hits+=1
                return deepcopy(item[1])
            self.stats.cache_misses+=1
            value=loader()
            _CACHE[key]=(time.monotonic()+ttl,deepcopy(value))
            return value
    def _ensure_mode(self):
        global _MODE,_SESSION
        if _MODE is not None:return
        with _LOCK:
            if _MODE is not None:return
            outcomes={}
            for mode in ('direct','system_proxy'):
                try:
                    with requests.Session() as session:
                        session.trust_env=mode!='direct'
                        r=session.get('https://qt.gtimg.cn/q='+market_symbol(PROBE_CODES[0]),
                                      headers={'User-Agent':UA},timeout=15)
                        self.stats.http_requests+=1
                        outcomes[mode]=dict(status=r.status_code,valid=bool(self._quotes(r.content.decode('gbk'))))
                except Exception as exc:outcomes[mode]=dict(error=type(exc).__name__,valid=False)
            _MODE=next((m for m in ('direct','system_proxy') if outcomes[m]['valid']),None)
            self.log(f'HTTP模式探测={outcomes}；选定={_MODE}')
            if _MODE is None:raise DataError('腾讯直连和系统代理均不可达')
            _SESSION=requests.Session();_SESSION.trust_env=_MODE!='direct'
    def http_get(self,url,**kwargs):
        global _LAST_REQUEST,_KLINE_BLOCKED_UNTIL
        self._ensure_mode()
        with _LOCK:
            for attempt in range(3):
                try:
                    time.sleep(max(0,.4-(time.monotonic()-_LAST_REQUEST)))
                    _LAST_REQUEST=time.monotonic();self.stats.http_requests+=1
                    r=_SESSION.get(url,headers={'User-Agent':UA,'Referer':'https://quote.eastmoney.com/'},timeout=15,**kwargs)
                    if 'ifzq.gtimg.cn/' in url and (r.status_code==429 or 'waf.tencent.com' in r.text[:1500]):
                        _KLINE_BLOCKED_UNTIL=time.monotonic()+300
                        raise SourceBlocked('腾讯日线限流/访问校验，暂停新增请求5分钟；已有内存日线仍可用')
                    r.raise_for_status();return r
                except requests.RequestException as exc:
                    if attempt==2:raise DataError(f'{url.split("?")[0]}: {type(exc).__name__}') from exc
                    time.sleep(2**attempt)
    @staticmethod
    def _quotes(text):
        return [(symbol,payload.split('~')) for symbol,payload in re.findall(r'v_([^=\s]+)="([^"]*)"',text) if len(payload.split('~'))>=58]
    def probe_fields(self):
        global _PROBED
        with _LOCK:
            if _PROBED:return
            r=self.http_get('https://qt.gtimg.cn/q='+','.join(map(market_symbol,PROBE_CODES)))
            rows=self._quotes(r.content.decode('gbk'))
            for symbol,values in rows:
                self.log(f'腾讯字段实测 {symbol} {values[1]} 字段数={len(values)}')
                for index,value in enumerate(values):self.log(f'[{index}]={value!r}')
                p=float(values[4]);h=float(values[33]);l=float(values[34])
                assert abs(float(values[32])-(float(values[3])/p-1)*100)<.02
                assert abs(float(values[43])-(h-l)/p*100)<.02
                assert abs(float(values[72])*float(values[3])/1e8-float(values[44]))<.05
                assert abs(float(values[73])*float(values[3])/1e8-float(values[45]))<.05
                assert abs(float(values[47])-round(p*1.1,2))<.011
                assert abs(float(values[48])-round(p*.9,2))<.011
                if float(values[36])>0:
                    assert abs(float(values[51])-float(values[57])*100/float(values[36]))<.02
            self.log('已确认映射='+json.dumps(SPOT_FIELDS,ensure_ascii=False))
            self.log('未使用索引='+str(UNKNOWN_FIELDS)+'；'+FIELD_NOTES)
            _PROBED=True
    def load_basic_pool(self):
        def fetch():
            url='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'
            rows=[];previous=None
            for page in range(1,200):
                response=self.http_get(url,params={'page':page,'num':100,'sort':'symbol','asc':1,'node':'hs_a'})
                batch=response.json()
                if not batch:break
                if not isinstance(batch,list):raise DataError('新浪列表返回格式变化')
                signature=tuple(r.get('symbol') for r in batch)
                if signature==previous:raise DataError('新浪重复分页，拒绝不完整股票池')
                previous=signature;rows.extend(batch)
                self.log(f'新浪股票池第{page}页，累计{len(rows)}行')
                # 不假设服务器尊重num；空页才是结束标记。
            else:raise DataError('新浪分页超过合理上限')
            raw=pd.DataFrame(rows)
            if raw.empty:raise DataError('新浪股票池为空')
            required={'symbol','name','trade','changepercent','volume'}
            if not required.issubset(raw):raise DataError(f'新浪缺列：{list(raw.columns)}')
            frame=raw.loc[raw.symbol.str.match(r'^(sh6|sz00|sz30)')].copy()
            frame['code6']=frame.symbol.map(code6)
            frame=frame.rename(columns={'name':'sina_name','trade':'sina_price','changepercent':'sina_pct_chg','volume':'sina_volume'})
            frame=frame.drop_duplicates('code6').reset_index(drop=True)
            if len(frame)<1000:raise DataError(f'新浪仅取得{len(frame)}只，疑似覆盖不完整')
            frame.attrs.update(fetched_at=now_iso(),raw_count=len(raw))
            return frame[['code6','sina_name','sina_price','sina_pct_chg','sina_volume']]
        return self.cached(('sina_pool',self.today),DATA_TTL,fetch)
    def load_snapshot(self,basic=None):
        basic=self.load_basic_pool() if basic is None else basic
        codes=tuple(sorted(basic.code6))
        def fetch():
            self.probe_fields(); rows={}
            for start in range(0,len(codes),60):
                pending=set(codes[start:start+60])
                for attempt in range(3):
                    r=self.http_get('https://qt.gtimg.cn/q='+','.join(market_symbol(c) for c in sorted(pending)))
                    for symbol,fields in self._quotes(r.content.decode('gbk')):
                        code=code6(fields[2])
                        if code not in pending:continue
                        row={name:fields[i] if i<len(fields) else '' for name,i in SPOT_FIELDS.items()}
                        row['code6']=code;row['raw_quote']='~'.join(fields)
                        rows[code]=row
                    pending-=rows.keys()
                    if not pending:break
                if start%600==0:self.log(f'腾讯快照 {min(start+60,len(codes))}/{len(codes)}')
            frame=_numeric(pd.DataFrame(rows.values()),SPOT_NUMERIC)
            if frame.empty:raise DataError('腾讯快照为空')
            frame.attrs.update(fetched_at=now_iso(),source='tencent',coverage=len(frame)/len(codes),missing_codes=sorted(set(codes)-rows.keys()))
            self.log(f'腾讯覆盖率={frame.attrs["coverage"]:.4%}，缺失={frame.attrs["missing_codes"]}')
            return frame
        return self.cached(('tencent_spot',codes),SNAPSHOT_TTL,fetch)
    def load_market_overview(self):
        """结果洞察的标准行情视图；不附加个人条件、不拉财报或日线。"""
        def fetch():
            basic=self.load_basic_pool()
            frame=self.load_snapshot(basic)
            times=pd.to_datetime(frame['quote_time'],format='%Y%m%d%H%M%S',errors='coerce')
            latest=times.max()
            return MarketOverview(frame=frame.drop(columns=['raw_quote'],errors='ignore').copy(),
                snapshot_time=latest.isoformat() if pd.notna(latest) else '源未返回有效时间',
                fetched_at=frame.attrs.get('fetched_at',now_iso()),coverage=frame.attrs.get('coverage',0),
                missing_codes=frame.attrs.get('missing_codes',[]))
        return self.cached(('market_overview',self.today),SNAPSHOT_TTL,fetch)
    @contextmanager
    def _ak_transport(self):
        with _LOCK:
            original=requests.get
            def get(url,params=None,**kwargs):
                r=self.http_get(url,params=params)
                body=r.json()
                if body.get('result') is None and (body.get('code')==9201 or '空' in body.get('message','')):raise EmptyReport('本期未披露')
                return r
            requests.get=get
            try:yield
            finally:requests.get=original
    def _financial_period(self,period):
        def fetch():
            try:
                with self._ak_transport():raw=ak.stock_yjbb_em(date=period)
            except EmptyReport:raw=pd.DataFrame()
            if raw.empty:return pd.DataFrame(columns=['code6','financial_name',*FIN_NUMERIC,'industry','notice_date','report_period'])
            mapping={'code6':_find_column(raw,'股票代码','证券代码'),'financial_name':_find_column(raw,'股票简称','证券简称'),
                'eps':_find_column(raw,'每股收益'),'rev_yoy':_find_column(raw,('营业','同比')),
                'np_yoy':_find_column(raw,('净利润','同比')),'np_value':_find_column(raw,'净利润-净利润'),
                'roe':_find_column(raw,'净资产收益率'),'bps':_find_column(raw,'每股净资产'),
                'ocfps':_find_column(raw,('每股','经营','现金')),'gross_margin':_find_column(raw,'销售毛利率'),
                'industry':_find_column(raw,'所处行业'),'notice_date':_find_column(raw,('公告','日期'))}
            frame=raw[list(mapping.values())].rename(columns={v:k for k,v in mapping.items()}).copy()
            frame['code6']=frame.code6.map(code6);frame['report_period']=period
            frame['notice_date']=pd.to_datetime(frame.notice_date,errors='coerce').dt.strftime('%Y-%m-%d')
            frame['industry']=frame.industry.fillna('').astype(str)
            frame=_numeric(frame,FIN_NUMERIC)
            return self.deduplicate_financial(frame)
        return self.cached(('financial_period',period),DATA_TTL,fetch)
    @staticmethod
    def deduplicate_financial(frame):
        return frame.sort_values('notice_date',na_position='first',kind='stable').drop_duplicates(['code6','report_period'],keep='last')
    def load_financials(self):
        if self.financial_disabled:return {},['模拟东财不可达：财务与行业条件、新股近似过滤将跳过']
        def fetch():
            frames={};warnings=[]
            for period in report_candidates(self.today):
                try:frame=self._financial_period(period)
                except Exception as exc:
                    warnings.append(f'东财报告期{period}不可达或字段异常（{type(exc).__name__}），已记录并降级')
                    break
                self.log(f'财报 {period}：{len(frame)}行'+('，未披露跳过' if frame.empty else ''))
                if not frame.empty:frames[period]=frame
                if len(frames)==6:break
            if len(frames)<6:warnings.append(f'只取得{len(frames)}期财报；依赖缺失期的条件将跳过')
            return frames,warnings
        # 失败结果最多缓存一分钟，使东财恢复后能重试。
        return self.cached(('financial_bundle',self.today),SNAPSHOT_TTL,fetch)
    def build_universe(self):
        if self.pinned_universe is not None:return deepcopy(self.pinned_universe)
        def fetch():
            financials,warnings=self.load_financials()
            basic=self.load_basic_pool();spot=self.load_snapshot(basic)
            frame=basic.merge(spot,on='code6',how='inner',validate='one_to_one')
            times=pd.to_datetime(frame.quote_time,format='%Y%m%d%H%M%S',errors='coerce')
            if times.notna().sum()==0:raise DataError('快照没有有效时间戳')
            actual=times.max();day=actual.date();counts=[]
            counts.append(dict(step='新浪沪深股票池',before=len(basic),removed=0,remaining=len(basic)))
            counts.append(dict(step='取得腾讯快照',before=len(basic),removed=len(basic)-len(frame),remaining=len(frame)))
            def keep(label,mask):
                nonlocal frame
                before=len(frame);frame=frame.loc[mask.fillna(False)].copy()
                counts.append(dict(step=label,before=before,removed=before-len(frame),remaining=len(frame)))
            keep('排除ST/*ST/退（新浪或腾讯名称）',~(frame.name.fillna('').str.contains('ST|退',case=False)|frame.sina_name.fillna('').str.contains('ST|退',case=False)))
            periods=sorted(financials,reverse=True);latest=periods[0] if periods else None
            available=set(SPOT_NUMERIC)
            if latest:
                history_counts=pd.concat([f[['code6']] for p,f in financials.items() if p!=latest],ignore_index=True).value_counts('code6') if len(periods)>1 else pd.Series(dtype=int)
                current=financials[latest].drop(columns='industry')
                if len(periods)>=3:
                    keep('新股近似过滤：最新财报存在且至少两期历史财报（不保证满一年）',frame.code6.isin(current.code6)&frame.code6.map(history_counts).fillna(0).ge(2))
                else:
                    keep('最新财报存在（历史不足，新股近似过滤已跳过）',frame.code6.isin(current.code6))
                    warnings.append('历史财报不足两期，无法近似排除新股，已跳过该过滤')
                frame=frame.merge(current,on='code6',how='inner',validate='one_to_one')
                industry=pd.Series(dtype=str)
                for period in periods:
                    f=financials[period].set_index('code6')
                    industry=industry.combine_first(f.loc[f.industry.str.strip().ne(''),'industry'])
                    frame[f'np_yoy_{period}']=frame.code6.map(f.np_yoy)
                frame['industry']=frame.code6.map(industry).fillna('')
                prior=same_period_last_year(latest)
                frame['np_yoy_prior_year']=frame.get('np_yoy_'+prior,float('nan'))
                frame['np_yoy_previous']=frame.get('np_yoy_'+periods[1],float('nan')) if len(periods)>1 else float('nan')
                available.update(FIN_NUMERIC+['industry'])
                if prior in financials and len(periods)>1:available.add('np_yoy_rising')
            else:
                counts.append(dict(step='新股近似过滤：财务不可达，已跳过',before=len(frame),removed=0,remaining=len(frame)))
                for col in FIN_NUMERIC+['np_yoy_prior_year','np_yoy_previous']:frame[col]=float('nan')
                frame['industry']='';frame['report_period']='';frame['notice_date']=''
                warnings.append('东财财务不可用：无法执行财务/行业条件，也无法近似排除新股；结果为降级筛选')
            keep('排除停牌（腾讯S标记或成交量为0）',~(frame.flag.eq('S')|frame.volume_hand.eq(0)))
            keep('排除无有效价格或成交量的快照',frame.price.gt(0)&frame.volume_hand.notna())
            frame['np_yoy_rising']=operating_improvement(frame.np_yoy,frame.np_yoy_previous,frame.np_yoy_prior_year,frame.np_value)
            frame['as_of']=str(day);frame['snapshot_time']=times.max().isoformat()
            sources=dict(snapshot={'source':SOURCE_DESCRIPTIONS['spot'],'as_of':str(day),'time':actual.isoformat(),'fetched_at':spot.attrs['fetched_at']},
                financial={'source':SOURCE_DESCRIPTIONS['financial'],'report_period':latest or '不可用'},
                kline={'source':SOURCE_DESCRIPTIONS['tencent_qfq'],'as_of':str(day)})
            status=dict(tencent='可达',sina='可达',eastmoney='可达' if len(financials)==6 else ('部分可达' if latest else '不可达'),degraded=len(financials)<6)
            return UniverseData(frame.reset_index(drop=True),pd.DataFrame(counts),day,latest,financials,sources,
                spot.attrs['coverage'],spot.attrs['missing_codes'],bool(latest),warnings,available,actual.isoformat(),spot.attrs['fetched_at'],status)
        return self.cached(('universe',self.today,self.financial_disabled),SNAPSHOT_TTL,fetch)
    def latest_trade_day(self):return self.build_universe().as_of
    def load_kline(self,value,as_of=None):
        value=code6(value);end=as_of or self.today;start=end-timedelta(days=600)
        def fetch():
            if time.monotonic()<_KLINE_BLOCKED_UNTIL:
                raise SourceBlocked('腾讯日线处于5分钟冷却期；未绕过访问校验')
            symbol=market_symbol(value)
            r=self.http_get('https://ifzq.gtimg.cn/appstock/app/fqkline/get',params={'param':f'{symbol},day,{start},{end},640,qfq'})
            block=r.json().get('data',{}).get(symbol,{})
            if 'qfqday' not in block:raise DataError(f'{value}未返回qfqday，不以未复权day冒充')
            rows=block['qfqday']
            frame=pd.DataFrame([row[:6] for row in rows],columns=['date','open','close','high','low','volume_hand'])
            frame=_numeric(frame,['open','close','high','low','volume_hand'])
            frame=frame.drop_duplicates('date').sort_values('date').reset_index(drop=True)
            frame=frame.loc[frame.date.between(str(start),str(end))].copy()
            frame['volume']=frame.volume_hand*100
            frame['pctChg']=frame.close.pct_change(fill_method=None)*100
            frame['code6']=value;frame.attrs.update(source='tencent_qfq',fetched_at=now_iso())
            return frame
        return self.cached(('kline',value,str(end)),DATA_TTL,fetch)
    def load_klines(self,codes,progress=None):
        started=time.perf_counter();result={};blocked=[]
        for i,code in enumerate(codes):
            try:result[code]=self.load_kline(code)
            except Exception as exc:
                if isinstance(exc,SourceBlocked):blocked.append(code)
                else:self.warnings.append(f'{code}日线失败：{exc}；该股走势缺失')
                result[code]=pd.DataFrame(columns=['date','close','volume','pctChg'])
                result[code].attrs['source']='tencent_qfq'
            if progress:progress(i+1,len(codes),(time.perf_counter()-started)/(i+1)*(len(codes)-i-1)/60,'tencent_qfq')
        if blocked:self.warnings.append(f'腾讯日线限流/访问校验：{len(blocked)}只没有可用内存日线，已计入缺失并剔除；暂停新增日线请求5分钟。样例：'+','.join(blocked[:10]))
        return result

def latest_trade_day():return DataClient().latest_trade_day()
