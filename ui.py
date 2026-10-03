"""展示组件与Vega-Lite图表；所有业务数字由本轮结果或用户条件传入。"""
from html import escape
from base64 import b64encode
from functools import lru_cache
from pathlib import Path
import pandas as pd
import streamlit as st
from metrics import METRIC_REGISTRY

PALETTE=['#242424','#606060','#909090','#b4b4b4','#cecece','#dedede']
def html(value):st.markdown(value,unsafe_allow_html=True)
@lru_cache(maxsize=1)
def background_image():
 return 'data:image/png;base64,'+b64encode(Path(__file__).with_name('assets').joinpath('shanghai-skyline.png').read_bytes()).decode('ascii')
def style():
 # 嵌入原图以兼容本地与云端子路径，无需外部图片服务或静态服务重启。
 css=Path(__file__).with_name('assets').joinpath('theme.css').read_text()
 html('<style>'+css.replace('__CITY_BACKGROUND__',background_image())+'</style>')
def heading(title,note='',kicker=''):
 html('<div class="section-heading">'+f'<div class="panel-title"><h3>{escape(title)}</h3><span>{escape(kicker)}</span></div>'+ (f'<p class="panel-desc">{escape(note)}</p>' if note else '')+'</div>')
def page_header(title,subtitle,mode):
 html(f'<div class="page-head"><div><h1 class="page-title">{escape(title)}</h1><p class="page-subtitle">{escape(subtitle)}</p></div><span class="mode-tag">{escape(mode)}</span></div>')
def card(title,value,note,tone='sage',unit='',mark='↗'):
 html(f'<div class="stat-card {tone}"><div class="stat-title">{escape(title)}<span class="stat-mark">{escape(mark)}</span></div><div class="stat-value">{escape(str(value))}<small>{escape(unit)}</small></div><div class="stat-note">{escape(note)}</div><div class="stat-decoration"></div></div>')
def empty(title,note,icon='◌'):
 html(f'<div class="empty-state"><div class="empty-icon">{escape(icon)}</div><strong>{escape(title)}</strong><p>{escape(note)}</p></div>')
def pills(items):
 html('<div class="condition-summary">'+''.join(f'<span class="pill {tone}">{escape(str(text))}</span>' for text,tone in items)+'</div>')
def workflow(parsed,executed):
 for n,title,note,done in [('01','表达你的想法','从行业、估值、盈利或走势开始',parsed),('02','检查每一项条件','门槛、权重和行业都由你决定',parsed),('03','执行并查看洞察','点击执行后才在线取数',executed)]:
  html(f'<div class="workflow-row {"done" if done else ""}"><b>{"✓" if done else n}</b><div><strong>{title}</strong><small>{note}</small></div></div>')
def chart(data,spec,key):
 spec=dict(spec)
 spec.setdefault('background','transparent')
 spec['config']={'view':{'stroke':None},'axis':{'domain':False,'tickSize':0,'labelColor':'#454545','titleColor':'#333333','gridColor':'#e4e4e4','labelFontSize':11,'titleFontSize':12,'labelPadding':9,'titlePadding':15},'legend':{'labelColor':'#454545','labelFontSize':12,'title':None},'font':'sans-serif'}
 st.vega_lite_chart(data,spec,width='stretch',theme=None,key=key)

def donut(groups,key,label='候选股票'):
 frame=pd.DataFrame(groups,columns=['category','value']);frame=frame.loc[frame.value.gt(0)]
 total=int(frame.value.sum())
 if not total:empty('等待你的想法','解析后显示条件组成，执行后显示真实候选分布。');return
 chart(frame,{'height':215,'layer':[
  {'mark':{'type':'arc','innerRadius':65,'outerRadius':91,'cornerRadius':7,'padAngle':.025,'stroke':'white','strokeWidth':1},'encoding':{'theta':{'field':'value','type':'quantitative'},'color':{'field':'category','type':'nominal','scale':{'domain':frame.category.tolist(),'range':PALETTE},'legend':None},'tooltip':[{'field':'category','type':'nominal','title':'类别'},{'field':'value','type':'quantitative','title':'数量'}]}},
  {'data':{'values':[{'text':str(total)}]},'mark':{'type':'text','fontSize':32,'fontWeight':650,'color':'#202020','dy':-5},'encoding':{'text':{'field':'text'}}},
  {'data':{'values':[{'text':label}]},'mark':{'type':'text','fontSize':10,'color':'#595959','dy':22},'encoding':{'text':{'field':'text'}}}
 ]},key)
 for i,row in enumerate(frame.itertuples()):
  html(f'<div class="legend-row"><span><i style="background:{PALETTE[i%len(PALETTE)]}"></i>{escape(str(row.category))}</span><b>{int(row.value):,} <span style="font-size:10px;color:#616161;margin-left:8px">{row.value/total:.0%}</span></b></div>')

def industry_groups(rows):
 counts=rows['industry'].fillna('').replace('','行业未披露').value_counts()
 groups=list(counts.head(5).items())
 if len(counts)>5:groups.append(('其他',int(counts.iloc[5:].sum())))
 return groups

def scatter(rows,x,y,key,symlog=False):
 data=rows[['code6','name','industry',x,y]].copy() if x!=y else rows[['code6','name','industry',x]].copy()
 data=data.dropna(subset=[x,y]);data['x']=pd.to_numeric(data[x],errors='coerce');data['y']=pd.to_numeric(data[y],errors='coerce')
 for axis,metric in [('x',x),('y',y)]:
  if METRIC_REGISTRY[metric]['unit']=='比例':data[axis]*=100
 def title(metric):
  spec=METRIC_REGISTRY[metric];unit='%' if spec['unit']=='比例' else spec['unit']
  return f'{spec["name"]}（{unit}）'
 if data.empty:empty('暂无可绘制数据','该指标在本轮候选中缺失，可切换其他指标。');return
 chart(data,{'height':300,'params':[{'name':'zoom','select':'interval','bind':'scales'}],
 'mark':{'type':'circle','size':160,'opacity':.8,'stroke':'white','strokeWidth':2},
 'encoding':{'x':{'field':'x','type':'quantitative','title':title(x),'scale':{'zero':False}},
 'y':{'field':'y','type':'quantitative','title':title(y),'scale':{'zero':False,'type':'symlog' if symlog else 'linear'}},
 'color':{'field':'industry','type':'nominal','scale':{'range':PALETTE},'legend':None},
 'tooltip':[{'field':'code6','type':'nominal','title':'代码'},{'field':'name','type':'nominal','title':'名称'},{'field':'industry','type':'nominal','title':'行业'},{'field':'x','type':'quantitative','title':title(x),'format':',.2f'},{'field':'y','type':'quantitative','title':title(y),'format':',.2f'}]}},key)

def funnel(frame,key):
 data=frame.copy();data['order']=range(len(data))
 # 标签只做省略，悬停仍显示完整条件。
 data['short']=data.step.map(lambda s:s if len(s)<=19 else s[:18]+'…')
 chart(data,{'height':max(220,min(520,len(data)*32)),'padding':{'left':0,'right':40,'top':5,'bottom':5},'layer':[
 {'mark':{'type':'bar','cornerRadiusEnd':5,'height':17},'encoding':{'x':{'field':'remaining','type':'quantitative','title':'剩余股票数量','axis':{'format':',d'}},'y':{'field':'short','type':'nominal','sort':{'field':'order'},'title':None,'axis':{'labelLimit':230}},'color':{'condition':{'test':'datum.removed > 0','value':'#242424'},'value':'#aaaaaa'},'tooltip':[{'field':'step','type':'nominal','title':'步骤'},{'field':'before','type':'quantitative','title':'此前数量'},{'field':'removed','type':'quantitative','title':'移除数量'},{'field':'remaining','type':'quantitative','title':'剩余数量'}]}},
 {'mark':{'type':'text','align':'left','dx':6,'color':'#444444','fontSize':10},'encoding':{'x':{'field':'remaining','type':'quantitative'},'y':{'field':'short','type':'nominal','sort':{'field':'order'}},'text':{'field':'remaining','type':'quantitative','format':',d'}}}
 ]},key)

def sensitivity(frame,key):
 chart(frame,{'height':260,'mark':{'type':'bar','cornerRadiusTopLeft':10,'cornerRadiusTopRight':10,'width':55},'encoding':{
 'x':{'field':'level','type':'ordinal','sort':['收紧一档','当前','放松一档'],'title':None,'axis':{'labelAngle':0}},
 'y':{'field':'count','type':'quantitative','title':'独立通过数量','axis':{'format':',d'}},
 'color':{'condition':{'test':'datum.current','value':'#242424'},'value':'#b0b0b0'},
 'tooltip':[{'field':'level','title':'档位'},{'field':'threshold_text','title':'阈值'},{'field':'count','type':'quantitative','title':'通过数量'},{'field':'scope','title':'计算范围'}]}},key)

def market_cap_chart(frame,key):
 """绘制实时行情中的市值前十二名，数值与代码直接来自同一批快照。"""
 data=frame.loc[pd.to_numeric(frame.total_mv_yi,errors='coerce').gt(0)].nlargest(12,'total_mv_yi').copy()
 data['label']=data['name']+' · '+data.code6
 if data.empty:
  empty('市值数据暂缺','本轮数据源没有返回有效市值。');return
 chart(data,{'height':330,'mark':{'type':'bar','cornerRadiusEnd':6,'color':'#242424','height':15},'encoding':{
  'x':{'field':'total_mv_yi','type':'quantitative','title':'总市值（亿元）'},
  'y':{'field':'label','type':'nominal','sort':'-x','title':None,'axis':{'labelLimit':145}},
  'tooltip':[{'field':'code6','title':'代码'},{'field':'name','title':'名称'},{'field':'total_mv_yi','type':'quantitative','title':'总市值（亿元）','format':',.2f'}]}},key)
