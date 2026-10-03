"""纯HTTP数据探针，无磁盘缓存依赖。"""
from data import DataClient,SPOT_FIELDS,UNKNOWN_FIELDS
from metrics import METRIC_REGISTRY,calculate_trend_metrics

def main():
    client=DataClient()
    client.probe_fields()
    u=client.build_universe()
    print('股票池漏斗\n'+u.funnel.to_string(index=False))
    print('动态数据时点',u.snapshot_time,'财报期',u.report_period,'状态',u.status)
    print('指标数量',len(METRIC_REGISTRY),'确认索引',SPOT_FIELDS,'跳过索引',UNKNOWN_FIELDS)
    for code in client.load_basic_pool().head(3).code6:
        frame=client.load_kline(code)
        print(code,calculate_trend_metrics(frame))
    assert u.frame.code6.str.fullmatch(r'\d{6}').all()
    assert not u.frame.code6.duplicated().any()
    assert all('source' in spec and 'frequency' in spec for spec in METRIC_REGISTRY.values())
    print('PASS：HTTP动态数据、字段注册、股票代码与趋势指标自检')
if __name__=='__main__': main()
