"""输入流回归测试；这里的合成样本只用于测试，应用不会读取此文件。"""
from datetime import date
from threading import Event
from types import SimpleNamespace
import time
import streamlit as st
import json
from unittest.mock import patch
import socket
import pandas as pd
import data
from data import DataClient,UniverseData,FetchStats,MarketOverview
from parse import parse_query,rule_query
from schema import ScreeningRequest
from screen import run_screen
from streamlit.testing.v1 import AppTest

def assert_parse():
 cases={
  '银行股，PE低于10，波动小':{'pe_ttm','vol_252'},
  '近半年涨幅超过20%，市值100亿以上':{'ret_120','total_mv_yi'},
  '毛利率高，不要ST，不要新股':{'gross_margin'},
  '股价低于10元，ROE大于15%，不要银行':{'price','roe'},
  '经营改善、估值合理，芯片行业':{'np_yoy_rising','pe_ttm'},
  '负债率低，机构重仓':set(),
  '':set(),
 }
 with patch.object(socket.socket,'connect',side_effect=AssertionError('解析不得请求行情')):
  for query,expected in cases.items():
   result=parse_query(query,api_key='')
   assert {c.metric for c in result.conditions}==expected,(query,result)
   assert result.as_of is None
   print('解析PASS',query,[(c.metric,c.op,c.threshold) for c in result.conditions])
  bank=parse_query('银行股，PE低于10',api_key='');assert bank.include_industries==['银行']
  assert parse_query('经营改善、估值合理，芯片行业',api_key='').include_industries==['半导体']
  rng=parse_query('市值千亿以上，PB在1到2之间',api_key='')
  assert [(c.metric,c.op,c.threshold) for c in rng.conditions]==[('total_mv_yi','>=',1000.),('pb','>=',1.),('pb','<=',2.)]
  assert parse_query('净利润同比为正',api_key='').conditions[0].threshold==0
  with patch('parse._completion',side_effect=RuntimeError('test')):
   r=parse_query('股价低于10元',api_key='test',base_url='https://example.invalid/v1',model='test')
   assert [c.metric for c in r.conditions]==['price']
  assert not parse_query('我喜欢可爱的公司',api_key='').conditions
 print('PASS：无Key、失败回退、未知条件、区间、行业均按输入解析，没有旧模板')

def fixture():
 # 一只负利润的合成样本，证明单纯价格条件不被旧经营改善/走势守卫误筛掉。
 day=date.today();frame=pd.DataFrame([dict(code6='000001',name='测试样本',industry='银行Ⅱ',price=8.,pe_ttm=9.,np_value=-1.,np_yoy=-10.,total_mv_yi=200.,as_of=str(day),snapshot_time=str(day))])
 funnel=pd.DataFrame([dict(step='合成测试池',before=1,removed=0,remaining=1)])
 sources=dict(snapshot=dict(as_of=str(day),time=str(day)),financial=dict(report_period='测试'),kline=dict(source='测试无需日线',as_of=str(day)))
 return UniverseData(frame,funnel,day,'测试',{},sources,1.,[],available_metrics={'price','pe_ttm','np_value','np_yoy','total_mv_yi','industry'},snapshot_time=str(day),fetched_at=str(day),status=dict(tencent='测试',sina='测试',eastmoney='测试',degraded=False))

def assert_screen():
 client=DataClient(universe=fixture(),logger=None)
 with patch.object(client,'load_klines',side_effect=AssertionError('无走势条件不能请求日线')):
  r=run_screen(parse_query('股价低于10元',api_key=''),client)
  assert len(r.rows)==1 and r.stats['stage_b_count']==0 and r.stats['truncated']==0
  r=run_screen(parse_query('银行股，PE低于10',api_key=''),client)
  assert len(r.rows)==1 and r.request.include_industries==['银行Ⅱ']
  try:run_screen(ScreeningRequest(query='错误行业',conditions=[],include_industries=['不存在行业']),client)
  except ValueError:pass
  else:raise AssertionError('不应悄悄忽略无法匹配的行业')
  try:run_screen(ScreeningRequest(query='空条件',conditions=[]),client)
  except ValueError:pass
  else:raise AssertionError('空条件不应筛全市场')
 print('PASS：价格/行业筛选无旧经营条件、无额外日线过滤；空条件与无法匹配行业阻止执行')

 def loader():calls.append(1);return len(calls)
 calls=[];data._CACHE[('fresh_test',)]=(float('inf'),'旧数据')
 fresh=DataClient(force_refresh=True,logger=None)
 assert fresh.cached(('fresh_test',),3600,loader)==1
 assert fresh.cached(('fresh_test',),3600,loader)==2
 assert fresh.stats.cache_hits==0
 del data._CACHE[('fresh_test',)]
 print('PASS：强制在线读取绕过内存缓存，每次重新调用数据加载器')

def assert_ui():
 calls=[];release=Event();release.set();entered=Event()
 class TestClient:
  fail=False
  def __init__(self,**kwargs):
   assert kwargs.get('force_refresh') is True
   self.stats=FetchStats();self.warnings=[];self.pinned_universe=None
  def build_universe(self):
   if self.pinned_universe is not None:return self.pinned_universe
   calls.append('online');self.stats.http_requests+=1;entered.set()
   assert release.wait(10)
   if self.fail:raise RuntimeError('模拟在线接口暂时不可用')
   return fixture()
  def load_klines(self,*args,**kwargs):raise AssertionError('价格意图不需要日线')
 def wait_done(app):
  app.session_state.screening_job._thread.join(timeout=5)
  app.run();assert not app.exception,app.exception
 sentinel='server-only-test-key-do-not-display'
 with patch.dict('os.environ',{'LLM_API_KEY':sentinel}),patch('data.DataClient',TestClient),patch('conversation._completion',side_effect=RuntimeError('规则测试')),patch.object(socket.socket,'connect',side_effect=AssertionError('UI测试禁止真实网络')):
  app=AppTest.from_file('app.py',default_timeout=10).run()
  assert not app.exception and not calls and not app.dataframe
  assert len(app.chat_input)==1 and not app.radio and not app.text_area
  assert not any(b.key=='run' for b in app.button)
  assert not app.text_input
  assert all(sentinel.encode() not in node.proto.SerializeToString() for node in app if getattr(node,'proto',None) is not None)
  app.button(key='nav_insights').click().run()
  assert any('此处基于历史真实数据作为可视化预览' in e.value for e in app.markdown)
  app.selectbox(key='market_sort').select('pct_chg').run()
  assert not calls
  app.button(key='back_workspace').click().run()
  app.chat_input[0].set_value('股价低于10元').run()
  assert not app.exception and not calls
  assert [c['metric'] for c in app.session_state.ir['conditions']]==['price']
  assert any('当前筛选条件' in e.value for e in app.markdown)
  assert not any('条件工作台' in e.value for e in app.markdown)
  app.button(key='chat_edit').click().run()
  assert app.selectbox(key='new_metric').value is None
  key=next(n.key for n in app.number_input if n.key.endswith('_threshold'))
  app.number_input(key=key).set_value(9.).run()
  assert app.session_state.ir['conditions'][0]['threshold']==9.
  assert app.session_state.conversation.request.conditions[0].threshold==9.
  app.chat_input[0].set_value('股价改为10元以下').run()
  assert app.session_state.ir['conditions'][0]['threshold']==10.
  assert not app.session_state.edit_chat_conditions and not calls
  release.clear();entered.clear()
  app.button(key='run').click().run()
  assert entered.wait(2)
  app.button(key='nav_insights').click().run()
  assert any('正在执行筛选' in e.value for e in app.markdown)
  assert any('市场行情概览' in e.value for e in app.markdown)
  app.button(key='back_workspace').click().run()
  assert app.button(key='run').disabled
  release.set();wait_done(app)
  assert calls==['online']
  assert any('筛选已完成' in m['content'] for m in app.session_state.conversation.messages)
  app.button(key='chat_results').click().run()
  assert not app.exception and any('测试样本' in str(t.value) for t in app.dataframe)
  assert any('市场行情概览' in e.value for e in app.markdown)
  app.toggle(key='chart_symlog').set_value(True).run()
  assert calls==['online']
  app.button(key='nav_sources').click().run()
  assert not any('当前不支持的指标'==e.label for e in app.expander)
  app.button(key='nav_workspace').click().run()
  app.chat_input[0].set_value('股价低于5元').run()
  assert not app.dataframe and calls==['online']
  app.button(key='run').click().run();wait_done(app)
  assert app.session_state.last_screening['result'].stats['qualified_count']==0
  app.button(key='chat_results').click().run()
  assert any('市场行情概览' in e.value for e in app.markdown)
  TestClient.fail=True
  app.button(key='back_workspace').click().run()
  app.button(key='run').click().run();wait_done(app)
  app.button(key='chat_results').click().run()
  assert any('模拟在线接口暂时不可用' in e.value for e in app.error)
  assert app.session_state.last_screening['result'].stats['qualified_count']==0
  assert calls==['online','online','online']
  independent=AppTest.from_file('app.py',default_timeout=10).run()
  independent.button(key='nav_insights').click().run()
  assert not independent.exception and not independent.error
  assert not any('最近一次已完成' in e.value for e in independent.caption)
 print('PASS：纯对话入口、内嵌条件卡、手动修改同步、自然语言继续修改、执行前零取数')
 print('PASS：历史预览离线浏览、后台跨页执行、图表零额外网络、失败与零结果保留预览')
 print('PASS：密钥不进入浏览器、独立用户会话隔离、原始筛选与解释保持一致')

def assert_model_parse():
 def completion(base_url,api_key,model,messages):
  assert base_url=='https://example.invalid/v1' and model=='test-model'
  payload=rule_query(messages[-1]['content']).model_dump(mode='json')
  for condition in payload['conditions']:
   if condition['metric']=='vol_252':condition['type']='soft'
  payload['conditions'].extend(rule_query('经营改善').model_dump(mode='json')['conditions'])
  return json.dumps(payload)
 with patch('parse._completion',side_effect=completion) as mocked:
  for query,expected in [('银行股，PE低于10',{'pe_ttm'}),('毛利率高',{'gross_margin'}),('市值100亿以上',{'total_mv_yi'}),('波动率不超过25%',{'vol_252'})]:
   result=parse_query(query,api_key='test-only',base_url='https://example.invalid/v1',model='test-model')
   assert {c.metric for c in result.conditions}==expected
   if query=='波动率不超过25%':assert result.conditions[0].type=='hard'
  assert mocked.call_count==4
 print('PASS：模型路径逐次使用当前输入，丢弃不属于当前原话的旧条件')

if __name__=='__main__':
 assert_parse();assert_model_parse();assert_screen();assert_ui()
 print('ALL PASS：意图输入、固定历史预览、跨页后台筛选、结果共存、凭据不出服务端')
