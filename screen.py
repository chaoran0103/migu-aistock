"""两阶段筛选。守卫先行，硬条件按IR顺序；near-miss永不触发日线请求。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import time

import pandas as pd

from data import DataClient, UniverseData
from metrics import METRIC_REGISTRY, calculate_trend_metrics, operating_improvement, plan_stage_b
from schema import Condition, ScreeningRequest
from parse import resolve_industry


def finite(value):
    try: return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError): return False


def fmt(value, unit='', digits=2):
    if not finite(value): return '缺失'
    number = float(value) * (100 if unit == '比例' else 1)
    suffix = '%' if unit in ('%', '比例') else unit
    return f'{number:.{digits}f}{suffix}'


def numeric(frame, column):
    return pd.to_numeric(frame.get(column, pd.Series(float('nan'), index=frame.index)), errors='coerce')


def condition_mask(frame, condition):
    if condition.metric == 'np_yoy_rising':
        return operating_improvement(numeric(frame, 'np_yoy'), numeric(frame, 'np_yoy_previous'),
                                     numeric(frame, 'np_yoy_prior_year'), numeric(frame, 'np_value'),
                                     float(condition.threshold)).fillna(False)
    values = numeric(frame, condition.metric)
    return {'<=':values.le, '>=':values.ge, '>':values.gt, '<':values.lt}[condition.op](condition.threshold).fillna(False)


def condition_detail(row, condition):
    spec = METRIC_REGISTRY[condition.metric]
    actual = row.get('np_yoy' if condition.metric == 'np_yoy_rising' else condition.metric)
    passed = bool(condition_mask(pd.DataFrame([row]), condition).iloc[0])
    threshold = float(condition.threshold)
    if not finite(actual): margin = '数据缺失'
    else:
        distance = abs(float(actual) - threshold)
        suffix = 'pct' if spec['unit'] in ('%', '比例') else spec['unit']
        distance *= 100 if spec['unit'] == '比例' else 1
        direction = '高于' if float(actual) >= threshold else '低于'
        margin = f'{direction}阈值 {distance:.2f}{suffix}'
        if condition.metric == 'np_yoy_rising' and not passed:
            reasons = []
            if not finite(row.get('np_yoy_prior_year')) or not float(actual) > row['np_yoy_prior_year']:
                reasons.append('未高于上年同期或同期缺失')
            if not finite(row.get('np_yoy_previous')) or not row['np_yoy_previous'] > 0 or not float(actual) > 0:
                reasons.append('最新两期同比未均为正')
            if not finite(row.get('np_value')) or not row['np_value'] > 0: reasons.append('净利润未为正')
            if reasons: margin += '；' + '；'.join(reasons)
    return dict(id=condition.id, phrase=condition.phrase, metric=condition.metric, name=spec['name'],
                unit=spec['unit'], type=condition.type, op=condition.op, threshold=threshold,
                threshold_text=fmt(threshold, spec['unit']), value=float(actual) if finite(actual) else None,
                actual=fmt(actual, spec['unit']), passed=passed, margin=margin)


def soft_score(value, threshold, op):
    if not finite(value): return None
    value, threshold = float(value), float(threshold)
    # 阈值为零仍定义连续尺度，避免除零；负阈值使用绝对幅度。
    scale = .5 * abs(threshold) if threshold else 1.0
    violation = value - threshold if op in ('<=','<') else threshold - value
    return max(0.0, 1.0 - max(0.0, violation) / scale)


def _append_filter(funnel, frame, mask, label):
    result = frame.loc[mask.fillna(False)].copy()
    funnel.append(dict(step=label, before=len(frame), removed=len(frame) - len(result), remaining=len(result)))
    return result


def _guarded(request, universe, funnel):
    frame = universe.copy()
    metrics = {c.metric for c in request.conditions}
    # 公共适用性守卫先于硬条件，单列计数，避免亏损股被标成低估值。
    applied_guards=set()
    for metric in METRIC_REGISTRY:
        if metric not in metrics: continue
        for guard in METRIC_REGISTRY[metric]['guard']:
            field = guard.get('metric', metric)
            if field in applied_guards:continue
            applied_guards.add(field)
            label = {'pe_ttm': 'PE-TTM 为正（守卫）', 'np_value': '最新净利润 np_value>0（守卫）'}.get(field, f'{METRIC_REGISTRY[metric]["name"]}为正（守卫）')
            frame = _append_filter(funnel, frame, numeric(frame, field).gt(guard['threshold']), label)
    missing_industry = int(frame['industry'].fillna('').str.strip().eq('').sum())
    if request.include_industries:
        frame = _append_filter(funnel,frame,frame['industry'].fillna('').isin(request.include_industries),
            '纳入行业：'+ '、'.join(request.include_industries)+'（缺失行业无法确认，剔除）')
    for industry in request.exclude_industries:
        frame = _append_filter(funnel, frame, ~frame['industry'].fillna('').eq(industry), f'排除行业：{industry}（缺失行业保留）')
    return frame, missing_industry


def stage_a(request, universe):
    funnel = universe.funnel.to_dict('records')
    guarded, missing_industry = _guarded(request, universe.frame, funnel)
    frame = guarded.copy()
    hard = [c for c in request.conditions if c.type == 'hard' and METRIC_REGISTRY[c.metric]['stage'] == 'A']
    for condition in hard:
        title = f'{METRIC_REGISTRY[condition.metric]["name"]} {condition.op} {fmt(condition.threshold, METRIC_REGISTRY[condition.metric]["unit"])}'
        frame = _append_filter(funnel, frame, condition_mask(frame, condition), title)
    # 各硬条件离阈值的有利边际，以阈值绝对值归一化并截断，避免单项极值统治。
    components = []
    for condition in hard:
        values = numeric(frame, 'np_yoy' if condition.metric == 'np_yoy_rising' else condition.metric)
        t = float(condition.threshold)
        edge = (t - values) if condition.op in ('<=','<') else (values - t)
        components.append((edge / max(abs(t), 1.0)).clip(0, 1).fillna(0))
    frame['stage_a_score'] = pd.concat(components, axis=1).mean(axis=1) if components else 0.0
    frame['stage_a_hard_pass'] = True
    return frame, guarded, funnel, missing_industry


def near_misses(request, guarded):
    hard = [c for c in request.conditions if c.type == 'hard' and METRIC_REGISTRY[c.metric]['stage'] == 'A']
    if not hard or guarded.empty: return pd.DataFrame()
    masks = pd.DataFrame({c.id: condition_mask(guarded, c) for c in hard}, index=guarded.index)
    candidates = guarded.loc[(~masks).sum(axis=1).eq(1)]
    records = []
    for index, row in candidates.iterrows():
        failed_id = masks.loc[index].index[~masks.loc[index]][0]
        condition = next(c for c in hard if c.id == failed_id)
        value = row.get('np_yoy' if condition.metric == 'np_yoy_rising' else condition.metric)
        threshold = float(condition.threshold)
        if not finite(value): continue
        tolerance = 2.0 if condition.metric == 'np_yoy_rising' else abs(threshold) * .2
        if abs(value - threshold) > tolerance + 1e-10: continue
        if condition.metric == 'np_yoy_rising':
            # 只改门槛就必须能通过全部子条件，不能拿结构性失败冒充near-miss。
            relaxed = condition.model_copy(update={'threshold': float(value)})
            if not condition_mask(pd.DataFrame([row]), relaxed).iloc[0]: continue
        digits = 4 if METRIC_REGISTRY[condition.metric]['unit'] == '比例' else 2
        quantum = Decimal('1').scaleb(-digits)
        suggestion = float(Decimal(str(float(value))).quantize(quantum,
                           rounding=ROUND_CEILING if condition.op in ('<=','<') else ROUND_FLOOR))
        if condition.op=='<': suggestion += float(quantum)
        if condition.op=='>': suggestion -= float(quantum)
        detail = condition_detail(row, condition)
        record = row.to_dict()
        record.update(failed_condition_id=condition.id, failed_metric=condition.metric,
                      phrase=condition.phrase, metric_name=METRIC_REGISTRY[condition.metric]['name'],
                      actual=detail['actual'], threshold=detail['threshold_text'], margin=detail['margin'],
                      suggest_value=suggestion, suggest=fmt(suggestion, detail['unit']),
                      passed_hard=len(hard) - 1, distance=abs(value - threshold) / max(abs(threshold), 1),
                      missing_stage_b=True)
        records.append(record)
    if not records: return pd.DataFrame()
    return pd.DataFrame(records).sort_values(['distance', 'total_mv_yi'], ascending=[True, False]).head(10).reset_index(drop=True)


def sensitivity_table(request, universe, evaluated):
    rows=[]
    for condition in request.conditions:
        if condition.type!='hard':continue
        metric=condition.metric; spec=METRIC_REGISTRY[metric]; threshold=float(condition.threshold)
        if metric=='np_yoy_rising': step=5.0
        elif metric.startswith('pe_'): step=10.0
        elif metric in ('vol_252','mdd_252'): step=.05
        else: step=abs(threshold)*.2 or 1.0
        tighter=threshold-step if condition.op in ('<=','<') else threshold+step
        looser=threshold+step if condition.op in ('<=','<') else threshold-step
        frame=evaluated if spec['stage']=='B' else universe
        for level,t in zip(['收紧一档','当前','放松一档'],[tighter,threshold,looser]):
            altered=condition.model_copy(update={'threshold':t})
            mask=condition_mask(frame,altered)
            for guard in spec['guard']:
                mask &= numeric(frame,guard.get('metric',metric)).gt(guard['threshold'])
            rows.append(dict(condition_id=condition.id,metric=metric,name=spec['name'],level=level,
                threshold=t,threshold_text=fmt(t,spec['unit']),current=t==threshold,count=int(mask.sum()),
                sample_count=len(frame),scope='仅初筛候选集' if spec['stage']=='B' else '全市场基础股票池'))
    return pd.DataFrame(rows,columns=['condition_id','metric','name','level','threshold','threshold_text','current','count','sample_count','scope'])


def effective_request(request,universe):
    result=request.model_copy(deep=True)
    warnings=list(universe.warnings);skipped=[]
    active=[]
    for condition in result.conditions:
        spec=METRIC_REGISTRY[condition.metric]
        if spec['stage']=='A' and condition.metric not in universe.available_metrics:
            warnings.append(f'「{condition.phrase}」依赖的{spec["name"]}数据不可用，该条件已跳过；不视为达标')
            skipped.append(condition.id)
        else:active.append(condition)
    result.conditions=active
    if not universe.financial_available:
        if result.include_industries or result.exclude_industries:warnings.append('财务行业不可用：行业纳入/排除均已跳过')
        result.include_industries=[];result.exclude_industries=[]
    result.warnings=list(dict.fromkeys(result.warnings+warnings))
    return result,skipped


@dataclass
class ScreenResult:
    rows: pd.DataFrame
    ranked: pd.DataFrame
    evaluated: pd.DataFrame
    near_miss: pd.DataFrame
    sensitivity: pd.DataFrame
    funnel: pd.DataFrame
    sources: dict
    stats: dict
    request: ScreeningRequest


def run_screen(request: ScreeningRequest, data: DataClient, progress_callback=None):
    started = time.perf_counter()
    universe = data.build_universe()
    request = request.model_copy(deep=True, update={'as_of': universe.as_of})
    request,skipped=effective_request(request,universe)
    if not request.conditions and not request.include_industries and not request.exclude_industries:
        raise ValueError('当前没有可执行条件（或所需数据均不可用），请修改条件；不会使用默认条件筛选')
    if universe.financial_available:
        choices=universe.frame['industry'].dropna().unique()
        for field in ('include_industries','exclude_industries'):
            resolved=[]
            for value in getattr(request,field):
                match=resolve_industry(value,choices)
                if match is None:raise ValueError(f'行业「{value}」未匹配本次在线财报中的具体行业，请改写或手动调整；未放宽该条件')
                if match not in resolved:resolved.append(match)
            setattr(request,field,resolved)
    initial, guarded, funnel, missing_industry = stage_a(request, universe)
    for condition_id in skipped:
        funnel.append(dict(step=f'数据不可用：条件{condition_id}已跳过',before=len(initial),removed=0,remaining=len(initial)))
    near = near_misses(request, guarded)
    needs_kline=any(METRIC_REGISTRY[c.metric]['requires_kline'] for c in request.conditions)
    hits_before = data.stats.cache_hits
    stage_b_started = time.perf_counter()
    trend_columns=['code6','vol_252','mdd_252','above_ma250','valid_count','window_count','ma250_count','eligible','note','ret_20','ret_60','ret_120']
    trend=pd.DataFrame(columns=trend_columns)
    if needs_kline:
        plan=plan_stage_b(initial,request.kline_cap)
        selected=plan.selected.copy();truncated=plan.truncated_count;disclosure=plan.disclosure
        funnel.append(dict(step=f'阶段B上限截断（前{request.kline_cap}只）',before=len(initial),removed=truncated,remaining=len(selected)))
        def progress(done,total,eta,source):
            if progress_callback:progress_callback(done,total)
        histories=data.load_klines(selected['code6'].tolist(),progress=progress)
        records=[{'code6':code,**calculate_trend_metrics(history)} for code,history in histories.items()]
        if records:trend=pd.DataFrame(records)
        evaluated=selected.merge(trend,on='code6',how='left',validate='one_to_one')
        frame=_append_filter(funnel,evaluated,evaluated['eligible'].fillna(False).astype(bool),'有效日线至少200根')
        missing={m:int(numeric(evaluated,m).isna().sum()) for m,spec in METRIC_REGISTRY.items() if spec['stage']=='B'}
        for condition in request.conditions:
            if condition.type=='hard' and METRIC_REGISTRY[condition.metric]['stage']=='B':
                frame=_append_filter(funnel,frame,condition_mask(frame,condition),f'{METRIC_REGISTRY[condition.metric]["name"]} {condition.op} {fmt(condition.threshold,METRIC_REGISTRY[condition.metric]["unit"])}（含缺失剔除）')
    else:
        selected=initial.iloc[0:0];truncated=0;missing={}
        evaluated=initial.copy();frame=initial.copy()
        disclosure='本次条件不需要走势指标，未拉取日线，也未按走势候选上限截断。'
        funnel.append(dict(step='当前条件无需日线，跳过阶段B',before=len(frame),removed=0,remaining=len(frame)))
    stage_b_seconds=time.perf_counter()-stage_b_started
    output = []
    for _, series in frame.iterrows():
        row = series.to_dict()
        details = [condition_detail(row, c) for c in request.conditions]
        numerator = denominator = 0.0
        for condition, detail in zip(request.conditions, details):
            if condition.type != 'soft': continue
            score = (float(detail['passed']) if finite(detail['value']) else None) if condition.metric == 'np_yoy_rising' else soft_score(detail['value'], condition.threshold, condition.op)
            if score is not None:
                numerator += condition.weight * score
                denominator += condition.weight
        row['match_score'] = round(100 * numerator / denominator, 1) if denominator else (100.0 if not any(c.type == 'soft' for c in request.conditions) else float('nan'))
        row['score_note'] = '无有效软条件，匹配度缺失' if denominator == 0 and any(c.type == 'soft' for c in request.conditions) else ''
        row['condition_details'] = details
        output.append(row)
    ranked = pd.DataFrame(output) if output else frame.assign(match_score=pd.Series(dtype=float), condition_details=pd.Series(dtype=object))
    ranked = ranked.sort_values(['match_score', 'total_mv_yi'], ascending=[False, False], na_position='last', kind='stable').reset_index(drop=True)
    rows = ranked.head(request.limit).copy()
    funnel.append(dict(step=f'结果展示上限{request.limit}只', before=len(ranked), removed=len(ranked) - len(rows), remaining=len(rows)))
    stats = dict(universe_count=len(universe.frame), stage_a_count=len(initial), stage_b_count=len(selected),
                 truncated=truncated, qualified_count=len(ranked), displayed_count=len(rows),
                 disclosure=disclosure, industry_missing=missing_industry, metric_missing=missing,
                 stage_b_seconds=stage_b_seconds, elapsed_seconds=time.perf_counter() - started,
                 stage_b_cache_hits=data.stats.cache_hits - hits_before, snapshot_coverage=universe.snapshot_coverage,
                 warnings=list(dict.fromkeys(request.warnings+data.warnings)), skipped_conditions=skipped,
                 snapshot_time=universe.snapshot_time, source_status=universe.status,
                 ma250_denominators=trend['ma250_count'].value_counts().sort_index().to_dict())
    return ScreenResult(rows, ranked, evaluated, near, sensitivity_table(request, universe.frame, evaluated),
                        pd.DataFrame(funnel), universe.sources, stats, request)


def result_table(result):
    rows = []
    for _, row in result.rows.iterrows():
        item = {'代码': row['code6'], '名称': row['name'], '行业': row.get('industry', ''), '匹配度': row['match_score']}
        for detail in row['condition_details']:
            prefix = f'{detail["name"]} [{detail["id"]}]'
            item[prefix] = detail['actual']
            item[prefix + '通过'] = '✓' if detail['passed'] else '✗'
            item[prefix + '边际'] = detail['margin']
        rows.append(item)
    return pd.DataFrame(rows)
