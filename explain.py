"""按实际生效IR动态生成解释；不调用LLM，不补造数字。"""
from __future__ import annotations
from screen import finite

def explain_stock(row):
    industry=row.get('industry','')
    head=f'{row["name"]}({row["code6"]}'+(f'，{industry}' if isinstance(industry,str) and industry else '')+')：'
    clauses=[]
    for detail in row.get('condition_details',[]):
        if detail['value'] is None:continue
        operator=detail['op'].replace('>=','≥').replace('<=','≤')
        if operator=='rising_for':operator='同比≥'
        status='达标' if detail['passed'] else '未达标，'+detail['margin']
        sentence=f'{detail["name"]}实际 {detail["actual"]}，要求 {operator}{detail["threshold_text"]}，{status}'
        if detail['metric']=='np_yoy_rising':
            if finite(row.get('np_yoy_prior_year')):sentence+=f'；上年同期同比 {row["np_yoy_prior_year"]:.2f}%'
            if finite(row.get('np_value')):sentence+=f'；本期净利润 {row["np_value"]/1e8:.2f} 亿元'
            if detail['passed']:sentence+='，最新两期同比及本期净利润为正'
        clauses.append(sentence)
    if not clauses:clauses.append('当前没有可执行的数值条件，结果仅供检查，不代表原意图全部满足')
    if finite(row.get('match_score')):clauses.append(f'匹配度 {row["match_score"]:.1f}')
    return head+'；'.join(clauses)+f'。快照时点 {row.get("snapshot_time",row.get("as_of","未知"))}。'

def explain_near_miss(row):
    return (f'{row["name"]}({row["code6"]})：{int(row["passed_hard"])} 项阶段A硬条件通过，'
            f'卡在「{row["phrase"]}」，{row["metric_name"]}实际 {row["actual"]}、阈值 {row["threshold"]}'
            f'（{row["margin"]}），阈值放宽到 {row["suggest"]} 可纳入初筛；未计算走势指标。')
def explain_funnel(result):
    s=result.stats
    return f'基础股票池 {s["universe_count"]} 只，阶段A通过 {s["stage_a_count"]} 只，走势覆盖 {s["stage_b_count"]} 只，合格 {s["qualified_count"]} 只，展示 {s["displayed_count"]} 只。'
def verify_explanations(rows,explanations=None):
    texts=explanations if explanations is not None else [explain_stock(r) for _,r in rows.iterrows()]
    assert len(texts)==len(rows)
    for (_,row),text in zip(rows.iterrows(),texts):
        assert row['code6'] in text
        for detail in row['condition_details']:
            if detail['value'] is not None:
                assert detail['actual'] in text,f'{row["code6"]}缺少{detail["metric"]}实际值'
                assert detail['threshold_text'] in text
    return dict(passed=True,checked=len(rows),basis='全部实际生效条件的真实值和阈值')
