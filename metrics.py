"""指标单一事实源；比例指标统一用小数，财报/换手率保留百分数。"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd


def _metric(id: str, name: str, stage: str, unit: str, formula: str,
            threshold: float | bool, *, enabled=False, type='hard', weight=1.0,
            op='>=', guard=None, **extra) -> dict[str, Any]:
    return dict(id=id, name=name, stage=stage, unit=unit, formula=formula,
                default_threshold=threshold, default_enabled=enabled,
                type=type, weight=weight, op=op, guard=guard or [], meaning=formula, requires_kline=stage == 'B',
                source='腾讯HTTP前复权日线' if stage == 'B' else '腾讯HTTP快照',
                frequency='每个交易日（执行时请求）' if stage == 'B' else '实时（每次执行重新请求）', selectable=True, **extra)


METRIC_REGISTRY = {
    'pe_ttm': _metric('pe_ttm', '市盈率 TTM', 'A', '倍', '腾讯市盈率TTM，最近12个月利润口径', 35,
                     enabled=True, op='<=', guard=[{'op': '>', 'threshold': 0}]),
    'pb': _metric('pb', '市净率', 'A', '倍', '腾讯市净率，市价相对于每股净资产', 3, op='<=',
                  guard=[{'op': '>', 'threshold': 0}]),
    'total_mv_yi': _metric('total_mv_yi', '总市值', 'A', '亿元', '腾讯行情提供的公司总市值', 100),
    'turnover_pct': _metric('turnover_pct', '换手率', 'A', '%', '腾讯行情提供的当日换手率', 1),
    'volume_ratio': _metric('volume_ratio', '量比', 'A', '倍', '腾讯行情提供的量比', 1),
    'np_yoy': _metric('np_yoy', '净利润同比增长', 'A', '%', '最新报告期净利润-同比增长', 10),
    'rev_yoy': _metric('rev_yoy', '营业收入同比增长', 'A', '%', '最新报告期营业总收入-同比增长', 10),
    'roe': _metric('roe', '净资产收益率', 'A', '%', '最新报告期净资产收益率', 10),
    'gross_margin': _metric('gross_margin', '销售毛利率', 'A', '%', '最新报告期销售毛利率', 20),
    'ocfps': _metric('ocfps', '每股经营现金流量', 'A', '元/股', '最新报告期每股经营现金流量', 0),
    'np_yoy_rising': _metric(
        'np_yoy_rising', '经营改善', 'A', '%',
        '最新净利润同比≥门槛，严格大于上年同类报告期，最新两期同比均>0，最新净利润绝对金额>0', 10,
        enabled=True, op='rising_for', guard=[{'metric': 'np_value', 'op': '>', 'threshold': 0}],
        params={'positive_periods': 2, 'compare': 'same_period_last_year'}),
    'vol_252': _metric('vol_252', '年化波动率', 'B', '比例',
                      '最近至多252个有效交易日 std(pctChg/100, ddof=1)×sqrt(252)', .30,
                      enabled=True, type='soft', weight=.8, op='<='),
    'mdd_252': _metric('mdd_252', '最大回撤幅度', 'B', '比例',
                      '最近至多252个有效交易日 max(1-close/历史最高close)，正值表示损失幅度', .20, op='<='),
    'above_ma250': _metric('above_ma250', '站上250日均线占比', 'B', '比例',
                          '600自然日内close.rolling(250).mean()；有效根数减249为分母，至少60根才计算close>MA250占比', .50),
}


# 指标扩展只使用已实测的字段；未确认的52周高低点不进入可执行注册表。
for _id, _name, _unit, _threshold, _op in [
    ('price','最新价','元',10,'<='), ('prev_close','昨收价','元',10,'<='),
    ('open','今开价','元',10,'<='), ('high','当日最高价','元',10,'<='), ('low','当日最低价','元',10,'<='),
    ('price_change','当日涨跌额','元',0,'>='), ('pct_chg','当日涨跌幅','%',3,'>='),
    ('amplitude','当日振幅','%',5,'<='), ('volume_hand','成交量','手',100000,'>='),
    ('amount_wan','成交额','万元',10000,'>='), ('vwap','成交均价','元',10,'<='),
    ('limit_up','当日涨停价','元',10,'<='), ('limit_down','当日跌停价','元',10,'<='),
    ('float_shares','流通股数','股',1000000000,'>='), ('total_shares','总股数','股',1000000000,'>='),
    ('float_mv_yi','流通市值','亿元',100,'>='),
    ('pe_dynamic','动态市盈率','倍',35,'<='), ('pe_static','静态市盈率','倍',35,'<='),
]:
    METRIC_REGISTRY[_id] = _metric(_id,_name,'A',_unit,'腾讯快照直接提供，字段已探活核对',_threshold,op=_op,
        guard=[{'op':'>','threshold':0}] if _id.startswith('pe_') else None)
for _id,_name,_unit,_threshold,_op in [('eps','每股收益','元/股',1,'>='),('bps','每股净资产','元/股',5,'>='),('np_value','净利润金额','元',0,'>')]:
    METRIC_REGISTRY[_id]=_metric(_id,_name,'A',_unit,'最新报告期财报原始金额；不是取绝对值运算',_threshold,op=_op)
FINANCIAL_METRICS={'eps','bps','rev_yoy','np_yoy','roe','ocfps','gross_margin','np_value','np_yoy_rising','industry','include_industries','exclude_industries'}
for _id in ('industry','include_industries','exclude_industries'):
    METRIC_REGISTRY[_id]=_metric(_id,{'industry':'行业','include_industries':'纳入行业','exclude_industries':'排除行业'}[_id],
        'A','分类','六期财报中最近非空行业；使用IR顶层行业列表',0)
    METRIC_REGISTRY[_id]['selectable']=False
for _id in FINANCIAL_METRICS:
    METRIC_REGISTRY[_id].update(source='东方财富业绩报表，经AkShare',frequency='季度（每次执行取最新披露）')
for _n in (20,60,120):
    _id=f'ret_{_n}'
    METRIC_REGISTRY[_id]=_metric(_id,f'近{_n}交易日涨跌幅','B','比例',
        f'前复权close[-1]/close[-({_n}+1)]-1；仅初筛候选集',.20,op='>=')
METRIC_REGISTRY['vol_252']['formula']='腾讯前复权close的日收益率样本标准差×sqrt(252)，最近252日'
METRIC_REGISTRY['vol_252']['meaning']=METRIC_REGISTRY['vol_252']['formula']
UNSUPPORTED_METRICS=[
    {'name':name,'reason':'当前免费数据源未提供可验证字段，无法执行，已忽略','approximation':None}
    for name in ['资产负债率','商誉','机构持仓','北向资金','分红率','52周最高价','52周最低价']
]


def same_period_last_year(report: str) -> str:
    if len(report) != 8 or report[4:] not in ('0331', '0630', '0930', '1231'):
        raise ValueError(f'不是有效季度报告期: {report}')
    return str(int(report[:4]) - 1) + report[4:]


def operating_improvement(latest, previous, last_year_same_period, np_value, minimum=10.0):
    """标量或Series均可；缺少任何比较值时不通过。"""
    result = ((latest >= minimum) & (latest > last_year_same_period) &
              (latest > 0) & (previous > 0) & (np_value > 0))
    return result.fillna(False) if isinstance(result, pd.Series) else bool(result)


def calculate_trend_metrics(kline: pd.DataFrame) -> dict[str, Any]:
    frame = kline.copy()
    for column in ('close', 'pctChg'):
        if column not in frame: raise ValueError(f'日线缺少 {column}')
        frame[column] = pd.to_numeric(frame[column], errors='coerce')
    frame['date'] = pd.to_datetime(frame['date'], errors='coerce')
    frame = frame.sort_values('date').drop_duplicates('date', keep='last')
    valid = (frame['date'].notna() & frame['close'].map(lambda x: math.isfinite(x)) &
             frame['close'].gt(0))
    # 有成交量时排除无成交停牌行，使根数代表有交易的有效观察。
    if 'volume' in frame:
        valid &= pd.to_numeric(frame['volume'], errors='coerce').gt(0)
    frame = frame.loc[valid].copy()
    count = len(frame)
    result = dict(vol_252=None, mdd_252=None, above_ma250=None,
                  valid_count=count, window_count=min(count, 252),
                  ma250_count=max(0, count - 249), eligible=count >= 200, excluded_count=int(count < 200),
                  data_end=frame['date'].max().date().isoformat() if count else None,
                  source=kline.attrs.get('source'), note='', ret_20=None,ret_60=None,ret_120=None)
    for n in (20,60,120):
        if count > n:result[f'ret_{n}']=float(frame['close'].iloc[-1]/frame['close'].iloc[-n-1]-1)
    if count < 200:
        result['note'] = f'有效日线仅{count}根，不足200根，剔除'
        return result
    frame['ma250'] = frame['close'].rolling(250, min_periods=250).mean()
    window = frame.tail(252)
    result['vol_252'] = float((window['pctChg'] / 100).std(ddof=1) * math.sqrt(252))
    result['mdd_252'] = float((1 - window['close'] / window['close'].cummax()).max())
    observable = frame['ma250'].notna()
    denominator = int(observable.sum())
    result['ma250_count'] = denominator
    if denominator >= 60:
        result['above_ma250'] = float(frame.loc[observable, 'close'].gt(frame.loc[observable, 'ma250']).mean())
    notes = [f'最近{len(window)}个有效交易日']
    if count < 252: notes.append(f'不足252根，使用实际{count}根')
    notes.append(f'MA250评估分母{denominator}根' + ('，不足60根，占比为空' if denominator < 60 else ''))
    result['note'] = '；'.join(notes)
    return result


@dataclass
class StageBPlan:
    selected: pd.DataFrame
    qualified_count: int
    truncated_count: int
    disclosure: str


def plan_stage_b(stage_a_result: pd.DataFrame, cap: int = 150) -> StageBPlan:
    """只规划数据拉取；hard_pass/score由后续筛选引擎提供，本模块不执行筛选条件。"""
    if not 50 <= cap <= 400: raise ValueError('走势候选上限必须为50–400')
    required = {'code6', 'stage_a_hard_pass', 'stage_a_score'}
    if not required.issubset(stage_a_result.columns): raise ValueError(f'阶段A结果必须包含{required}')
    if not pd.api.types.is_bool_dtype(stage_a_result['stage_a_hard_pass']):
        raise ValueError('stage_a_hard_pass必须为布尔值')
    qualified = stage_a_result.loc[stage_a_result['stage_a_hard_pass'].fillna(False)].copy()
    if 'near_miss' in qualified:
        if not pd.api.types.is_bool_dtype(qualified['near_miss']): raise ValueError('near_miss必须为布尔值')
        qualified = qualified.loc[~qualified['near_miss'].fillna(False)]
    if qualified['code6'].duplicated().any(): raise ValueError('阶段A结果包含重复股票代码')
    qualified['stage_a_score'] = pd.to_numeric(qualified['stage_a_score'], errors='coerce')
    if qualified['stage_a_score'].isna().any(): raise ValueError('阶段A匹配度不能为空')
    qualified = qualified.sort_values(['stage_a_score', 'code6'], ascending=[False, True], kind='stable')
    selected = qualified.head(cap).copy()
    return StageBPlan(selected, len(qualified), max(0, len(qualified) - cap),
                      f'走势类指标仅计算初筛后的前{len(selected)}只，非全市场；near-miss不进入阶段B')
