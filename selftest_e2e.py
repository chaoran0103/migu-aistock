"""纯HTTP现场验收；报告文件不是应用缓存，不参与后续数据加载。"""
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json,time,sys,socket
import pandas as pd
from data import DataClient,ROOT,SPOT_FIELDS,UNKNOWN_FIELDS
from schema import default_ir,Condition,ScreeningRequest
from screen import run_screen
from explain import explain_stock,explain_near_miss,verify_explanations

class Tee:
 def __init__(self,*streams):self.streams=streams
 def write(self,s):
  for stream in self.streams:stream.write(s);stream.flush()
 def flush(self):
  for stream in self.streams:stream.flush()

def main():
 started=time.perf_counter();client=DataClient()
 u=client.build_universe()
 print('数据状态',u.status,'时点',u.snapshot_time,'最新财报',u.report_period,flush=True)
 request=default_ir(u.as_of)
 pinned=DataClient(universe=u)
 def progress(done,total):
  if done%25==0 or done==total:print('阶段B',done,total,flush=True)
 result=run_screen(request,pinned,progress)
 print('DEFAULT漏斗\n',result.funnel.to_string(index=False))
 print('前10\n',result.rows[['code6','name','industry','pe_ttm','np_yoy','vol_252','match_score']].head(10).to_string(index=False))
 print('解释',explain_stock(result.rows.iloc[0]))
 print('near-miss')
 for _,row in result.near_miss.head(3).iterrows():print(explain_near_miss(row))
 print('敏感性\n',result.sensitivity.to_string(index=False))
 print('verify_explanations',verify_explanations(result.rows))
 cold=time.perf_counter()-started
 # 同一内存快照复算，不把已过60秒的行情假装成新行情。
 with patch.object(socket.socket,'connect',side_effect=AssertionError('同快照缓存复跑不应联网')):
  t=time.perf_counter();again=run_screen(request,DataClient(universe=u,logger=None));warm=time.perf_counter()-t
 pd.testing.assert_frame_equal(result.rows,again.rows)
 print('同快照内存缓存二次耗时',warm,'首轮总耗时',cold)
 bank=[v for v in sorted(u.frame.industry.dropna().unique()) if v.replace('Ⅱ','').replace('Ⅰ','')=='银行']
 examples=[
  ScreeningRequest(query='银行股，PE低于10，波动小',as_of=u.as_of,include_industries=bank,
   conditions=[Condition(id='pe',phrase='PE低于10',metric='pe_ttm',op='<=',threshold=10),Condition(id='vol',phrase='波动小',metric='vol_252',op='<=',threshold=.25,type='soft')]),
  ScreeningRequest(query='近半年涨幅大，市值100亿以上',as_of=u.as_of,conditions=[Condition(id='cap',phrase='市值100亿以上',metric='total_mv_yi',op='>=',threshold=100),Condition(id='mom',phrase='近半年涨幅大',metric='ret_120',op='>=',threshold=.20)]),
  ScreeningRequest(query='毛利率高，不要ST，不要新股',as_of=u.as_of,conditions=[Condition(id='gross',phrase='毛利率高',metric='gross_margin',op='>=',threshold=40)])]
 reports=[]
 for example in examples:
  t=time.perf_counter();out=run_screen(example,DataClient(universe=u),progress)
  assert verify_explanations(out.rows)['passed']
  print('意图验收',example.query,'IR',example.model_dump_json(),'统计',out.stats,flush=True)
  print(out.rows[['code6','name','industry','match_score']].head(3).to_string(index=False))
  reports.append(dict(query=example.query,request=example.model_dump(mode='json'),stats=out.stats,top3=out.rows[['code6','name','industry','match_score']].head(3).to_dict('records'),seconds=time.perf_counter()-t))
 # 故障注入：数据层实际走无财报路径，不用本地历史parquet兜底。
 degraded_client=DataClient(financial_disabled=True)
 degraded=degraded_client.build_universe()
 assert not degraded.financial_available
 outage=run_screen(request,DataClient(universe=degraded),progress)
 assert len(outage.rows)>0 and 'improving' in outage.stats['skipped_conditions']
 assert not any(d['metric']=='np_yoy_rising' for d in outage.rows.iloc[0].condition_details)
 print('东财不可达模拟',outage.stats,'\n',outage.funnel.to_string(index=False))
 print('PASS：纯HTTP默认链路、三组手工IR、缺财报降级、同快照禁网缓存复跑。')
 summary=dict(status=u.status,snapshot_time=u.snapshot_time,report_period=u.report_period,
  funnel=result.funnel.to_dict('records'),top10=result.rows[['code6','name','industry','pe_ttm','np_yoy','vol_252','match_score']].head(10).to_dict('records'),
  explanation=explain_stock(result.rows.iloc[0]),near_miss=[explain_near_miss(r) for _,r in result.near_miss.head(3).iterrows()],sensitivity=result.sensitivity.to_dict('records'),
  stats=result.stats,cold_seconds=cold,warm_seconds=warm,examples=reports,degraded=outage.stats,confirmed=SPOT_FIELDS,skipped_fields=UNKNOWN_FIELDS)
 (ROOT/'outputs'/'http_upgrade_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str))
if __name__=='__main__':
 (ROOT/'outputs').mkdir(exist_ok=True)
 with (ROOT/'outputs'/'http_upgrade_e2e.txt').open('w') as f:
  with redirect_stdout(Tee(sys.stdout,f)),redirect_stderr(Tee(sys.stderr,f)):main()
