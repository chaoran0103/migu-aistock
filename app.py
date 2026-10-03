"""觅股研究台：输入驱动、主动执行取数、真实数据可视化。"""
from __future__ import annotations
import hashlib
import os
import re
import uuid
from html import escape
import pandas as pd
import streamlit as st
from market_preview import load_preview, SOURCE as PREVIEW_SOURCE
from screening_jobs import start_screening
from explain import explain_stock,explain_near_miss
from metrics import METRIC_REGISTRY
from schema import Condition,ScreeningRequest
from screen import result_table,fmt
import ui
from conversation import (Conversation, respond, accept, dismiss, sync_manual,
                          can_execute, conflicts, describe, condition_text, request_signature, say, result_reply)

APP_NAME='觅股-意图选股AI平台'
st.set_page_config(page_title=APP_NAME,page_icon='◈',layout='wide',initial_sidebar_state='expanded')
ui.style()

def table(frame):
    try:st.dataframe(frame,hide_index=True,width='stretch')
    except Exception:st.table(frame)

def setting(name):
    if os.getenv(name):return os.getenv(name)
    try:return str(st.secrets.get(name,''))
    except Exception:return ''

def clear_results():
    st.session_state.pop('result',None);st.session_state.pop('executed_universe',None)

def goto(page):st.session_state.page=page

def hash_request(request):return hashlib.sha256(request.model_dump_json().encode()).hexdigest()

def get_request():
    payload=st.session_state.get('ir')
    if not payload or payload['query']!=st.session_state.get('query',''):return None
    return ScreeningRequest.model_validate(dict(payload,kline_cap=st.session_state.get('kline_cap',150)))

def get_result():
    request=get_request()
    if request is None or st.session_state.get('result_hash')!=hash_request(request):return None
    return st.session_state.get('result')

def current_result_context():
    return dict(result=st.session_state.get('result'),universe=st.session_state.get('executed_universe'),
                elapsed=st.session_state.get('last_elapsed',0),requests=st.session_state.get('request_count',0),
                cache_hits=st.session_state.get('data_cache_hits',0))

def source_caption(result):
    s=result.sources
    return f'行情 {s["snapshot"].get("time",s["snapshot"]["as_of"])} · 腾讯  /  财报 {s["financial"]["report_period"]} · 东方财富（AkShare）'

# 保留现有输入、条件和用户已执行的结果；新会话不自动请求数据。
if st.session_state.get('flow_version')!='intent_steps_v3':
    for key in ('ir','result','result_hash','executed_universe','new_metric'):st.session_state.pop(key,None)
    st.session_state.flow_version='intent_steps_v3'
st.session_state.setdefault('editor_version',0)
st.session_state.setdefault('page','workspace')
# 非工作台页没有输入框；显式保留草稿，避免 Streamlit 清理隐藏控件的值。
st.session_state.pop('workspace_mode',None)
if 'query' in st.session_state:
    st.session_state.query=st.session_state.query

# 凭据只存在于服务端配置与调用中，不创建密码控件或向浏览器传递值。
for key in ('llm_key','llm_base','llm_model','config_signature'):
    st.session_state.pop(key,None)
base_url=setting('LLM_BASE_URL');api_key=setting('LLM_API_KEY');model=setting('LLM_MODEL')
if 'last_screening' not in st.session_state and get_result() is not None:
    st.session_state.last_screening=current_result_context()

with st.sidebar:
    ui.html('<div class="brand"><div class="brand-symbol">◈</div><span>觅股<small>意图选股AI平台</small></span></div><div class="side-kicker">你的研究空间</div>')
    for page,label,icon in [('workspace','选股工作台',':material/dashboard:'),('insights','结果洞察',':material/donut_small:'),('sources','数据与设置',':material/tune:')]:
        st.button(label,key='nav_'+page,icon=icon,width='stretch',type='primary' if st.session_state.page==page else 'secondary',on_click=goto,args=(page,))
    st.write('')
    with st.expander('走势计算范围',icon=':material/query_stats:'):
        cap=st.slider('走势候选上限',50,400,150,10,key='kline_cap')
        st.caption('仅有走势条件时使用。先初筛，再计算前N只；不会为near-miss拉日线。')
    with st.expander('配置语言模型',icon=':material/auto_awesome:'):
        ready=all(v.strip() for v in (api_key,base_url,model))
        st.caption('模型服务已配置' if ready else '模型服务待配置；当前可使用规则解析')
        if model:st.caption('模型：'+model)
        st.caption('连接信息由服务端管理。模型拆解想法，行情与财报由在线数据接口返回。')
    ui.html('<div class="side-note"><span class="side-note-line">先输入想法，再确认条件。</span><span>由你决定何时执行与查询最新数据。</span><span class="side-note-line side-note-footer">免费公开数据 · 研究学习用途</span></div>')

page=st.session_state.page
header={'workspace':(APP_NAME,'请描述你的任何选股想法，我将为你解析具体条件、查询最新数据并给出股票推荐及其理由哦'), 'insights':('结果洞察','看清候选、边界，以及每一个条件的影响。'), 'sources':('数据与设置','了解每一个数字从哪里来、何时更新。')}
llm_ready=all(v.strip() for v in (api_key,base_url,model))
parse_mode='模型解析' if llm_ready else ('模型配置待补全' if api_key.strip() else '规则解析')
ui.page_header(*header[page],mode=parse_mode)


def stats_cards(context=None):
    context=context or current_result_context()
    result=context['result']
    if result is None:return
    with st.container(key='stats_layout'):
        columns=st.columns(3,gap='medium')
        s=result.stats
        values=[('基础股票池',f'{s["universe_count"]:,}','本轮在线数据 · 基础过滤后','sage','只','◷'),('符合条件',f'{s["qualified_count"]:,}',f'当前展示 {s["displayed_count"]} 只','butter','只','↗'),('本轮耗时',f'{context["elapsed"]:.1f}','每次执行重新在线请求','lilac','秒','↻')]
        for column,args in zip(columns,values):
            with column:ui.card(*args)


def condition_editor(request, pending=False):
    payload=request.model_dump(mode='json');before=request.model_dump_json();version=st.session_state.editor_version
    all_metrics=[m for m,s in METRIC_REGISTRY.items() if s.get('selectable',True)]
    edited=[];deleted=False
    low=sum(c.needs_confirm for c in request.conditions)
    ui.pills([(f'{sum(c.type=="hard" for c in request.conditions)} 项硬条件',''),(f'{sum(c.type=="soft" for c in request.conditions)} 项软条件','soft')]+([(f'{low} 项建议门槛','pending')] if low else []))
    for position,c in enumerate(request.conditions):
        spec=METRIC_REGISTRY[c.metric];prefix=f'{version}_{c.id}_{c.metric}'
        operator='同比 ≥' if c.op=='rising_for' else c.op.replace('<=','≤').replace('>=','≥')
        with st.expander(f'{c.phrase}  ·  {spec["name"]} {operator} {fmt(c.threshold,spec["unit"])}',expanded=False):
            if c.needs_confirm:st.caption('◌ 这是建议门槛，请核对后采用。' if pending else '◌ 这是对话中已采用的建议门槛，仍可随时调整。')
            fields=st.columns([2,1,1.5])
            chosen=fields[0].selectbox('指标',all_metrics,index=all_metrics.index(c.metric),format_func=lambda m:METRIC_REGISTRY[m]['name'],key=prefix+'_metric')
            if chosen!=c.metric:
                other=METRIC_REGISTRY[chosen]
                replacement=Condition(id=c.id,phrase=c.phrase,metric=chosen,op=other['op'],threshold=other['default_threshold'],type=other['type'],weight=other['weight'])
                payload['conditions'][position]=replacement.model_dump()
                persist_condition_edit(payload,pending,reset_widgets=True)
            ops=['rising_for'] if chosen=='np_yoy_rising' else ['>=','<=','>','<']
            op=fields[1].selectbox('运算符',ops,index=ops.index(c.op),key=prefix+'_op')
            ratio=spec['unit']=='比例';factor=100 if ratio else 1
            bounds=(0.,100.) if chosen in ('above_ma250','mdd_252') else ((-100. if chosen.startswith('ret_') else 0.,1000.) if ratio else (-1000000.,1000000.))
            threshold=fields[2].number_input('阈值（'+('%' if ratio else spec['unit'])+'）',min_value=min(bounds[0],float(c.threshold)*factor),max_value=max(bounds[1],float(c.threshold)*factor),value=float(c.threshold)*factor,step=1. if ratio or spec['unit']=='%' else .1,format='%.2f',key=prefix+'_threshold')/factor
            extras=st.columns([1,1,1])
            kind=extras[0].selectbox('条件类型',['hard','soft'],index=['hard','soft'].index(c.type),format_func=lambda v:'硬条件' if v=='hard' else '软条件',key=prefix+'_type')
            weight=extras[1].number_input('权重',min_value=.01,value=float(c.weight),step=.1,disabled=kind=='hard',key=prefix+'_weight')
            extras[2].write('')
            if extras[2].button('删除',icon=':material/delete_outline:',key=prefix+'_delete'):
                deleted=True;continue
            edited.append(Condition(id=c.id,phrase=c.phrase,metric=chosen,op=op,threshold=threshold,params=c.params,type=kind,weight=weight,confidence=c.confidence).model_dump())
            st.caption(spec['formula']+' · '+spec['source'])
    payload.update(conditions=edited,kline_cap=cap,as_of=None)
    if deleted:
        persist_condition_edit(payload,pending,reset_widgets=True)
    addcols=st.columns([3,1],vertical_alignment='bottom')
    addition=addcols[0].selectbox('添加条件',all_metrics,index=None,placeholder='选择你想添加的指标',format_func=lambda m:METRIC_REGISTRY[m]['name'],key='new_metric')
    if addcols[1].button('添加',icon=':material/add:',key='add',width='stretch',disabled=addition is None):
        spec=METRIC_REGISTRY[addition]
        payload['conditions'].append(Condition(id=uuid.uuid4().hex[:10],phrase='手动添加：'+spec['name'],metric=addition,op=spec['op'],threshold=spec['default_threshold'],type=spec['type'],weight=spec['weight']).model_dump())
        persist_condition_edit(payload,pending,reset_widgets=True)
    st.caption('行业范围')
    industries=st.columns(2)
    include=industries[0].text_input('纳入行业',value='、'.join(payload['include_industries']),placeholder='例如：半导体',key=f'include_{version}')
    exclude=industries[1].text_input('排除行业',value='、'.join(payload['exclude_industries']),placeholder='可留空',key=f'exclude_{version}')
    def tokens(v):return list(dict.fromkeys(x.strip() for x in re.split(r'[，,、;；\n]+',v) if x.strip()))
    payload.update(include_industries=tokens(include),exclude_industries=tokens(exclude))
    changed=ScreeningRequest.model_validate(payload)
    if changed.model_dump_json()!=before:persist_condition_edit(changed.model_dump(mode='json'),pending)
    return changed


def job_running():
    job=st.session_state.get('screening_job')
    return job is not None and job.snapshot()['status']=='running'


def collect_job():
    job=st.session_state.get('screening_job')
    if job is None:return False
    snapshot=job.snapshot()
    if snapshot['status']=='running' or st.session_state.get('handled_job')==job.id:return False
    st.session_state.handled_job=job.id
    if snapshot['status']=='succeeded':
        context=snapshot['context']
        st.session_state.last_screening=context
        st.session_state.result=context['result']
        st.session_state.result_hash=hashlib.sha256(job.request_json.encode()).hexdigest()
        st.session_state.executed_universe=context['universe']
        st.session_state.last_elapsed=context['elapsed']
        st.session_state.request_count=context['requests']
        st.session_state.data_cache_hits=context['cache_hits']
        if 'conversation' in st.session_state:
            say(st.session_state.conversation,'assistant','刚才执行的筛选已完成。\n\n'+result_reply(context))
    elif 'conversation' in st.session_state:
        say(st.session_state.conversation,'assistant','刚才执行的筛选未完成，请查看任务提示后重试。当前条件没有自动调整。')
    return True


def execute(request):
    if job_running():return
    clear_results()
    st.session_state.screening_job=start_screening(request.model_copy(deep=True))
    st.rerun()


@st.fragment(run_every=1 if job_running() else None)
def render_job_status():
    job=st.session_state.get('screening_job')
    if job is None:return
    if collect_job():st.rerun(scope='app')
    snapshot=job.snapshot()
    with st.container(key='job_status_panel'):
        if snapshot['status']=='running':
            st.markdown('**正在执行筛选**')
            st.caption('本轮想法：'+job.query)
            ui.html(f'''<div class="screening-loader" role="status">
                <span class="screening-loader-ring" aria-hidden="true"></span>
                <div class="screening-loader-copy">
                    <strong>{escape(snapshot['message'])}</strong>
                    <p>已用时 <span class="screening-elapsed">{int(snapshot['elapsed'])}</span> 秒；切换页面不会中断筛选。</p>
                </div>
            </div>''')
            if snapshot['total']:
                st.progress(snapshot['done']/max(snapshot['total'],1),text=f'走势 {snapshot["done"]}/{snapshot["total"]} · 预计还需 {snapshot["eta"]:.1f} 分钟')
        elif snapshot['status']=='failed':
            st.error('本轮筛选未完成：'+snapshot['error'])
            st.caption('可回到工作台重试。下方历史预览与上一轮已完成结果保留。')
        else:
            st.markdown('**筛选已完成，可查看结果并导出 CSV。**')


def save_conversation(state):
    st.session_state.conversation=state
    st.session_state.edit_chat_conditions=False
    if state.request is None:
        if st.session_state.get('ir') is not None:
            st.session_state.pop('ir',None);clear_results();st.session_state.editor_version+=1
        st.session_state.query=''
    else:
        before=get_request()
        if request_signature(before)!=request_signature(state.request):
            clear_results();st.session_state.editor_version+=1
        st.session_state.ir=state.request.model_dump(mode='json')
        st.session_state.query=state.request.query


def persist_condition_edit(payload,pending=False,reset_widgets=False):
    changed=ScreeningRequest.model_validate(payload)
    changed.query=describe(changed).replace('\n','；')
    state=st.session_state.conversation
    if pending:
        previous=state.pending
        state.pending=changed
        issues=conflicts(changed)
        if issues:
            state.pending_question='；'.join(issues)+'。请调整冲突条件。'
            state.clarification_topic='conflict';state.confirmable=False
        elif state.clarification_topic=='conflict':
            state.pending_question='条件冲突已消除，请检查这组条件后采用。'
            state.clarification_topic='';state.confirmable=True
        elif state.clarification_topic=='industry' and previous and (changed.include_industries!=previous.include_industries or changed.exclude_industries!=previous.exclude_industries):
            state.pending_question='行业范围已手动修改，请核对后采用；执行时仍会校验实际行业。'
            state.clarification_topic='';state.confirmable=True
    else:
        sync_manual(state,changed)
        st.session_state.ir=changed.model_dump(mode='json')
        st.session_state.query=changed.query
        clear_results()
    if reset_widgets:st.session_state.editor_version+=1
    st.rerun()


def render_condition_message(state):
    pending=state.pending is not None
    request=state.pending if pending else state.request
    if request is None and not state.awaiting:return
    with st.chat_message('assistant'):
        with st.container(key='chat_condition_card'):
            ui.heading('待确认的筛选条件' if pending else ('当前筛选条件' if request else '先确认一下'))
            if state.awaiting:
                st.write(state.pending_question)
                if pending:st.caption('这组修改尚未生效；采用后才会替换当前条件。')
            if request is not None:
                if request.include_industries:ui.pills([('纳入行业 · '+'、'.join(request.include_industries),'')])
                if request.exclude_industries:ui.pills([('排除行业 · '+'、'.join(request.exclude_industries),'')])
                for c in request.conditions:
                    ui.html('<div class="chat-condition-row">'+escape(condition_text(c))+'</div>')
                st.caption(f'最多展示 {request.limit} 只 · 默认排除 ST、新股、停牌；新股采用财报历史近似口径。')
                if not state.awaiting:
                    for issue in conflicts(request):st.warning(issue)
                if request.warnings:
                    with st.expander('条件提示',expanded=False):
                        for warning in request.warnings:st.warning(warning)
            if state.awaiting:
                controls=st.columns(2)
                if controls[0].button('采用这组建议条件',key='chat_accept',disabled=not state.confirmable,width='stretch'):
                    save_conversation(accept(state));st.rerun()
                if controls[1].button('取消本次待确认内容',key='chat_dismiss',width='stretch'):
                    save_conversation(dismiss(state));st.rerun()
                if not state.confirmable:st.caption('请回答澄清问题，或点击修改条件补充信息。')
            if request is not None:
                actions=st.columns(2)
                if actions[0].button('执行筛选',key='run',icon=':material/play_arrow:',width='stretch',type='primary',disabled=not can_execute(state) or job_running()):execute(state.request)
                editing=st.session_state.get('edit_chat_conditions',False)
                if actions[1].button('收起编辑' if editing else '修改条件',key='chat_edit',icon=':material/edit:',width='stretch'):
                    st.session_state.edit_chat_conditions=not editing;st.rerun()
                if editing:
                    with st.container(key='chat_editor_panel'):
                        condition_editor(request,pending=pending)
                st.caption('也可以直接回复，例如“PE 改成 15 以下”或“删除波动率条件”。')


def render_conversation():
    if 'conversation' not in st.session_state:
        st.session_state.conversation=Conversation(request=get_request())
    state=st.session_state.conversation
    current=get_request()
    if request_signature(current)!=request_signature(state.request):sync_manual(state,current)
    with st.container(key='conversation_panel'):
        top=st.columns([3,1],vertical_alignment='center')
        with top[0]:ui.heading('与觅股对话','描述想法、确认条件、执行筛选，都在这里完成。')
        if top[1].button('新对话',key='new_conversation',disabled=job_running(),width='stretch',icon=':material/add_comment:'):
            save_conversation(Conversation());st.rerun()
        with st.container(height=640 if state.messages else 220,key='conversation_history',autoscroll=not st.session_state.get('edit_chat_conditions',False)):
            if not state.messages:
                with st.chat_message('assistant'):
                    st.write('你想找什么样的股票？可以先告诉我行业、估值、盈利或走势偏好。')
                    st.caption('例如：银行股，PE 低于 10。之后可以接着说“再加一个波动小”。')
            for index,message in enumerate(state.messages):
                with st.container(key=f'chat_{message["role"]}_{index}'):
                    with st.chat_message(message['role']):st.write(message['content'])
            render_condition_message(state)
            if st.session_state.get('screening_job') is not None:
                with st.chat_message('assistant'):render_job_status()
            if st.session_state.get('last_screening'):
                with st.chat_message('assistant'):
                    if st.button('查看结果与图表',key='chat_results',icon=':material/insert_chart:',on_click=goto,args=('insights',)):
                        pass
                    if get_result() is None:st.caption('当前显示的是上次完成结果；新条件需要重新执行。')
        text=st.chat_input('继续描述或调整条件…',key='dialogue_message',max_chars=2000)
        if text:
            loading=st.empty()
            loading.markdown('<div class="screening-loader intent-parse-loader" role="status"><span class="screening-loader-ring" aria-hidden="true"></span><div class="screening-loader-copy"><strong>正在理解你的想法与上下文…</strong></div></div>',unsafe_allow_html=True)
            try:
                updated=respond(state,text,api_key=api_key,base_url=base_url,model=model,context=st.session_state.get('last_screening'))
                if updated.request:updated.request.kline_cap=cap
                if updated.pending:updated.pending.kline_cap=cap
                save_conversation(updated)
            finally:loading.empty()
            st.rerun()
        st.caption('仅保留本次会话上下文；聊天不会查询行情，点击执行后才取数。')
        if any(message['role']=='user' for message in state.messages):
            if st.button('撤销上一步',key='chat_undo',disabled=not(state.undo or state.awaiting)):
                save_conversation(respond(state,'撤销上一步'));st.rerun()


def render_results(result,context=None):
    context=context or current_result_context()
    st.caption(source_caption(result))
    source_status=context['universe'].status
    ui.pills([(f'{name} · {source_status.get(key,"状态未知")}', 'pending' if source_status.get('degraded') and key=='eastmoney' else '') for key,name in [('tencent','腾讯'),('sina','新浪'),('eastmoney','东财')]])
    important=[w for w in result.stats['warnings'] if '未配置LLM' not in w and '没有明确数值' not in w]
    if important:
        with st.expander(f'数据提示 · {len(important)} 条',expanded=True,icon=':material/info:'):
            for w in important:st.warning(w)
    st.caption(result.stats['disclosure'])
    tabs=st.tabs(['候选画像','筛选过程','差一点入选'])
    with tabs[0]:
        if result.ranked.empty:ui.empty('这一轮没有合格候选','查看筛选过程和near-miss，了解哪些条件挡住了股票。')
        else:
            with st.container(key='chart_layout'):
                left,right=st.columns([2.1,1],gap='large')
                with left:
                    with st.container(key='scatter_panel'):
                        ui.heading('候选画像','切换横纵轴；悬停查看数值，滚轮缩放、拖动平移。','EXPLORE')
                        available=[m for m,s in METRIC_REGISTRY.items() if s.get('selectable',True) and m!='np_yoy_rising' and m in result.ranked and pd.to_numeric(result.ranked[m],errors='coerce').notna().any()]
                        if available:
                            axes=st.columns(2)
                            x=axes[0].selectbox('横轴指标',available,index=available.index('pe_ttm') if 'pe_ttm' in available else 0,format_func=lambda m:METRIC_REGISTRY[m]['name'],key='chart_x')
                            y=axes[1].selectbox('纵轴指标',available,index=available.index('np_yoy') if 'np_yoy' in available else min(1,len(available)-1),format_func=lambda m:METRIC_REGISTRY[m]['name'],key='chart_y')
                            compress=st.toggle('压缩纵轴极值（对称对数刻度）',value=False,key='chart_symlog')
                            ui.scatter(result.ranked,x,y,'candidate_scatter',compress)
                            st.caption(f'覆盖本轮合格候选 {len(result.ranked):,} 只；不代表全市场。坐标缺失的股票不绘制。')
                with right:
                    with st.container(key='donut_panel'):
                        ui.heading('行业分布','仅本轮合格候选','COMPOSITION')
                        ui.donut(ui.industry_groups(result.ranked),'industry_donut','合格候选')
            with st.container(key='candidates_panel'):
                title,download=st.columns([3,1],vertical_alignment='center')
                with title:ui.heading('候选名单',f'展示前 {len(result.rows)} 只 · 先按匹配度、再按总市值排序')
                data=result_table(result)
                download.download_button('导出 CSV',data.to_csv(index=False).encode('utf-8-sig'),file_name='screening_results.csv',mime='text/csv',icon=':material/download:',key='download_results',on_click='ignore')
                table(data)
            with st.container(key='detail_panel'):
                ui.heading('逐只查看理由','解释由真实指标与本轮条件直接生成。','EVIDENCE')
                code=st.selectbox('选择候选股票',result.rows.code6.tolist(),format_func=lambda value:result.rows.set_index('code6').loc[value,'name']+' · '+value,key='detail_stock')
                row=result.rows.set_index('code6',drop=False).loc[code]
                for detail in row.condition_details:
                    status='达标' if detail['passed'] else '未达标'
                    operator='同比 ≥' if detail['op']=='rising_for' else detail['op'].replace('<=','≤').replace('>=','≥')
                    ui.html(f'<div class="signal-row"><div>{escape(detail["name"])} <span class="pill {"" if detail["passed"] else "pending"}">{status}</span><small>要求 {escape(operator)} {escape(detail["threshold_text"])} · {escape(detail["margin"])}</small></div><strong>{escape(detail["actual"])}</strong></div>')
                ui.html('<div class="explain-box">'+escape(explain_stock(row))+'</div>')
    with tabs[1]:
        with st.container(key='funnel_panel'):
            ui.heading('条件漏斗','每一步保留了多少股票，移除了多少股票。','THE PROCESS')
            ui.funnel(result.funnel,'funnel_chart')
            with st.expander('查看完整漏斗数据'):table(result.funnel.rename(columns={'step':'步骤','before':'此前数量','removed':'移除数量','remaining':'剩余数量'}))
        with st.container(key='sensitivity_panel'):
            ui.heading('如果门槛变一点？','比较三档门槛下的独立通过数量，不会修改当前条件或重新联网。','SENSITIVITY')
            sensitivity=result.sensitivity
            if sensitivity.empty:st.caption('本轮没有可计算敏感性的数值硬条件。')
            else:
                ids=sensitivity.condition_id.drop_duplicates().tolist()
                chosen=st.selectbox('观察条件',ids,format_func=lambda v:sensitivity.loc[sensitivity.condition_id.eq(v),'name'].iloc[0]+' · '+v,key='sensitivity_metric')
                part=sensitivity.loc[sensitivity.condition_id.eq(chosen)]
                ui.sensitivity(part,'sensitivity_chart')
                ui.pills([(str(row.level)+' '+str(row.threshold_text),'soft' if row.current else '') for row in part.itertuples()])
                st.caption(str(part.scope.iloc[0])+' · 含相应守卫 · 非累计漏斗数量')
                with st.expander('查看敏感性明细'):table(part[['name','level','threshold_text','count','sample_count','scope']])
    with tabs[2]:
        with st.container(key='near_panel'):
            ui.heading('离入选，只差一步','仅一项阶段A硬条件未通过且接近门槛；不为这些股票请求日线。','NEAR MISS')
            near=result.near_miss
            if near.empty:ui.empty('本轮没有接近门槛的股票','试试调整数值硬条件，或查看敏感性图表。')
            else:
                table(near[['code6','name','phrase','metric_name','actual','threshold','margin','suggest']].rename(columns={'code6':'代码','name':'名称','phrase':'卡住条件','metric_name':'指标','actual':'实际值','threshold':'阈值','margin':'差距','suggest':'建议阈值'}))
                for _,row in near.iterrows():
                    with st.expander(row['name']+' · '+row['metric_name']+' '+row['actual']):st.write(explain_near_miss(row))
    with st.expander('数据时点与本轮取数记录',icon=':material/database:'):
        u=context['universe']
        st.write(f'行情交易日：{u.as_of}')
        st.caption(f'本次在线获取：{u.fetched_at}；HTTP请求 {context["requests"]} 次；旧数据缓存命中 {context["cache_hits"]} 次。')
        st.caption(source_caption(result))
        st.caption('日线来源：'+result.sources['kline']['source'] if result.stats['stage_b_count'] else '本轮未请求日线。')


def render_market_overview():
    overview=load_preview()
    frame=overview.frame
    with st.container(key='preview_notice'):
        st.markdown('**此处基于历史真实数据作为可视化预览**')
        st.write(f'数据日期：{overview.snapshot_time[:10]} · 行情时点：{overview.snapshot_time[11:]}')
        st.caption(f'来源：{PREVIEW_SOURCE} · 实际抓取时间：{overview.fetched_at}')
    ui.heading('市场行情概览','固定的沪深 A 股历史行情。','MARKET PREVIEW')
    change=pd.to_numeric(frame.pct_chg,errors='coerce')
    positive_pe=pd.to_numeric(frame.pe_ttm,errors='coerce');positive_pe=positive_pe[positive_pe.gt(0)]
    pe=positive_pe.median() if len(positive_pe) else None
    with st.container(key='preview_stats_layout'):cards=st.columns(3,gap='medium')
    with cards[0]:ui.card('行情覆盖',f'{len(frame):,}','沪深 A 股','sage','只','◷')
    with cards[1]:ui.card('上涨股票',f'{int(change.gt(0).sum()):,}',f'有效涨跌幅样本 {int(change.notna().sum()):,} 只','butter','只','↗')
    with cards[2]:ui.card('PE-TTM 中位数',f'{pe:.2f}' if pe is not None else '暂无',f'仅正 PE 样本 {len(positive_pe):,} 只','lilac','倍' if pe is not None else '','◈')
    left,right=st.columns([2.1,1],gap='large')
    with left:
        with st.container(key='preview_scatter_panel'):
            ui.heading('市值分布','按总市值展示前 12 只，悬停查看真实数值。','MARKET CAP')
            ui.market_cap_chart(frame,'market_cap_chart')
    with right:
        with st.container(key='preview_donut_panel'):
            ui.heading('市场涨跌分布','相对上一交易日收盘价。','COMPOSITION')
            ui.donut([('上涨',int(change.gt(0).sum())),('下跌',int(change.lt(0).sum())),('平盘',int(change.eq(0).sum())),('缺失',int(change.isna().sum()))],'market_breadth','行情股票')
    with st.container(key='preview_candidates_panel'):
        ui.heading('行情一览','历史行情列表仅用于可视化预览；你的筛选候选单独显示在上方。')
        sort=st.selectbox('行情排序',['total_mv_yi','pct_chg','turnover_pct','volume_ratio'],format_func=lambda m:METRIC_REGISTRY[m]['name']+' · 从高到低',key='market_sort')
        names={'code6':'代码','name':'名称','price':'最新价','pct_chg':'涨跌幅%','pe_ttm':'PE-TTM','pb':'市净率','total_mv_yi':'总市值（亿元）','turnover_pct':'换手率%','volume_ratio':'量比'}
        shown=frame.sort_values([sort,'code6'],ascending=[False,True],na_position='last').head(30)
        table(shown[list(names)].round(2).rename(columns=names))
        st.caption('展示历史快照中当前排序前 30 只，不代表实时行情或个人筛选结果。')


def render_sources():
    left,right=st.columns([2,1],gap='large')
    with left:
        with st.container(key='sources_panel'):
            ui.heading('数据从哪里来','每次执行重新请求。页面交互与图表切换不会触发行情请求。','DATA SOURCES')
            for title,note in [('新浪 · 股票池','沪深A股，执行时获取'),('腾讯 · 行情快照','最新返回报价，可能有延迟'),('东方财富 / AkShare · 财报','自动探测最近六个非空报告期'),('腾讯 · 前复权日线','只有选用了走势指标才请求')]:
                ui.html(f'<div class="source-row">{escape(title)}<span>{escape(note)}</span></div>')
            st.write('')
            st.caption('实时请求不等于指标实时更新。财报按报告期披露；休市时行情仍为最近交易日。新股用财报历史近似过滤，不保证上市满一年。')
            st.caption('如果免费源不可达，会明确提示缺失或降级，不用本地旧数据回填。走势计算仅覆盖初筛后的前N只。')
            result=get_result()
            if result:
                st.caption('本轮数据时点：'+source_caption(result))
                st.caption(f'本轮在线获取：{st.session_state.executed_universe.fetched_at}')
    with right:
        with st.container(key='guide_panel'):
            ui.heading('设置提示')
            st.caption('语言模型连接由服务端私有配置管理，页面只显示服务状态与模型名称。未配置模型时仍可解析常见条件。')
            st.caption('“走势计算范围”影响需要日线的筛选；仅快照或财报条件不受该上限限制。')

collect_job()

if page=='workspace':
    render_conversation()
elif page=='insights':
    st.button('前往选股工作台',icon=':material/arrow_back:',key='back_workspace',on_click=goto,args=('workspace',),type='primary')
    render_job_status()
    context=st.session_state.get('last_screening')
    if context:
        result=context['result']
        st.caption('最近一次已完成的筛选：'+result.request.query)
        if get_result() is None:st.caption('新条件执行完成后，这里会更新为最新结果。')
        stats_cards(context);render_results(result,context)
    render_market_overview()
else:
    render_job_status()
    render_sources()
ui.html(f'<div class="page-footer"><span>{APP_NAME}</span><span>仅供研究学习，不构成投资建议 · 免费数据源可能延迟或缺失</span></div>')
