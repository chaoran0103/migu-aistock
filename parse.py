"""当前输入逐条解析：有Key用LLM，无Key/调用失败用可检查的规则；不套模板。"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.error
import urllib.request
from metrics import METRIC_REGISTRY, UNSUPPORTED_METRICS
from schema import Condition, ScreeningRequest

# 仅描述语义，不包含股票、报价或财报数据。模糊意图的阈值必须在工作台确认。
ALIASES = {
 'np_yoy_rising':r'经营改善',
 'np_yoy':r'净利润同比(?:增长率|增长|增速)?|净利润(?:增长率|增长|增速)|盈利增长|利润增长',
 'rev_yoy':r'营(?:业)?收(?:入)?同比(?:增长率|增长|增速)?|营收(?:增长率|增长|增速)',
 'np_value':r'净利润(?:金额|绝对值)?|赚钱|盈利(?:能力强)?|不亏损',
 'pe_dynamic':r'动态(?:市盈率|PE)', 'pe_static':r'静态(?:市盈率|PE)',
 'pe_ttm':r'PE[-_ ]?TTM|市盈率|(?<![a-z])PE(?![a-z])|估值合理|低估值|估值低|低估|便宜',
 'pb':r'市净率|(?<![a-z])PB(?![a-z])|破净',
 'float_mv_yi':r'流通市值', 'total_mv_yi':r'总市值|市值|大盘(?:股)?|小盘(?:股)?',
 'price':r'最新价|股价|价格|低价(?:股)?',
 'turnover_pct':r'换手率', 'volume_ratio':r'量比', 'amplitude':r'振幅',
 'roe':r'净资产收益率|(?<![a-z])ROE(?![a-z])',
 'gross_margin':r'销售毛利率|毛利率', 'ocfps':r'每股经营现金流(?:量)?',
 'eps':r'每股收益|(?<![a-z])EPS(?![a-z])', 'bps':r'每股净资产',
 'ret_120':r'近?半年(?:涨跌幅|涨幅|收益率|涨得多)?|(?:近)?120(?:个)?(?:交易)?日(?:涨跌幅|涨幅|收益率)',
 'ret_60':r'(?:近)?60(?:个)?(?:交易)?日(?:涨跌幅|涨幅|收益率)|近?三个月(?:涨跌幅|涨幅|收益率)|近期涨得多|强势(?:股)?',
 'ret_20':r'(?:近)?20(?:个)?(?:交易)?日(?:涨跌幅|涨幅|收益率)|近?一个月(?:涨跌幅|涨幅|收益率)',
 'vol_252':r'年化波动率|波动率|波动(?:小|低|大|高)|走势(?:相对)?稳定|稳健|稳定',
 'mdd_252':r'最大回撤(?:幅度)?|回撤(?:小|低|大|高)?',
 'above_ma250':r'站(?:上|在)250日均线(?:上方)?(?:占比)?|年线占比|站上年线',
}
# 其他已注册指标仍可用中文全名或metric id加数值操作符输入。
for _metric,_spec in METRIC_REGISTRY.items():
 if _spec.get('selectable',True):
  ALIASES[_metric]='(?:'+ALIASES.get(_metric,re.escape(_spec['name']))+'|'+re.escape(_metric)+')'
ALIAS_RE=re.compile('|'.join(f'(?P<{m}>{p})' for m,p in ALIASES.items()),re.I)
INDUSTRIES=('银行','证券','券商','保险','白酒','半导体','芯片','软件开发','互联网服务','计算机','通信设备','汽车整车','汽车零部件','房地产','煤炭','钢铁','有色金属','电力','光伏设备','风电设备','电池','医疗器械','化学制药','中药','医药','物流','燃气','环保','食品饮料','家用电器','军工','科技','新能源')
AMBIGUOUS={'科技','新能源','军工','医药','环保','计算机'}
UNSUPPORTED_RE=re.compile(r'资产负债率|负债率|商誉|机构(?:持仓|重仓)|北向(?:资金)?|分红(?:率)?|股息率|52周(?:最高|最低)(?:价)?|创新高')
NUMBER=r'[+-]?\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+'
UNIT=r'万亿|亿(?:元|股|手)?|万(?:元|股|手)?|%|％|元|块|倍|手|股'
TOKEN=rf'(?P<n>{NUMBER})\s*(?P<u>{UNIT})?'
OP_WORDS={'不超过':'<=','不高于':'<=','至多':'<=','最多':'<=','低于':'<','小于':'<','少于':'<','不低于':'>=','不少于':'>=','至少':'>=','高于':'>','大于':'>','超过':'>','>=':'>=','<=':'<=','≥':'>=','≤':'<=','>':'>','<':'<','以上':'>=','以下':'<='}
OP_WORDS.update({'不大于':'<=','不小于':'>=','小于等于':'<=','大于等于':'>=','不足':'<','以内':'<=','之内':'<='})

def chinese_number(s):
 try:return float(s)
 except ValueError:pass
 digits=dict(zip('零一二三四五六七八九',range(10)));digits['两']=2
 total=number=0
 for char in s:
  if char in digits:number=digits[char]
  elif char in '十百千':total+=(number or 1)*{'十':10,'百':100,'千':1000}[char];number=0
 return float(total+number)

def scaled(value,unit,metric):
 value=chinese_number(value)
 if metric in ('total_mv_yi','float_mv_yi'):
  value*= {'万亿':10000,'万元':.0001,'万':.0001,'元':1e-8}.get(unit,1)
 elif metric=='np_value':value*= {'万亿':1e12,'亿':1e8,'亿元':1e8,'万':1e4,'万元':1e4}.get(unit,1)
 elif metric=='amount_wan':value*= {'亿':1e4,'亿元':1e4,'元':.0001}.get(unit,1)
 elif metric in ('volume_hand','float_shares','total_shares'):
  value*=1e8 if unit and unit.startswith('亿') else (1e4 if unit and unit.startswith('万') else 1)
  if metric=='volume_hand' and unit and unit.endswith('股'):value/=100
 elif METRIC_REGISTRY[metric]['unit']=='比例':
  if unit in ('%','％') or abs(value)>1:value/=100
 return value

def numerical_terms(text,metric):
 # 区间保留两个独立硬条件，而非丢弃一边。
 number=rf'({NUMBER})\s*({UNIT})?'
 rng=re.search(number+r'\s*(?:到|至|~|～|—|-)\s*'+number,text)
 if rng:
  unit1=rng.group(2) or rng.group(4);unit2=rng.group(4) or rng.group(2)
  return [('>=',scaled(rng.group(1),unit1,metric)),('<=',scaled(rng.group(3),unit2,metric))]
 ops='|'.join(map(re.escape,sorted(OP_WORDS,key=len,reverse=True)))
 matches=list(re.finditer(rf'(?P<op>{ops})\s*{TOKEN}',text))
 if matches:return [(OP_WORDS[m['op']],scaled(m['n'],m['u'],metric)) for m in matches]
 matches=list(re.finditer(TOKEN+rf'\s*(?P<op>以上|以下|以内|之内)',text))
 if matches:return [(OP_WORDS[m['op']],scaled(m['n'],m['u'],metric)) for m in matches]
 return []

def rule_query(query,as_of=None):
 conditions=[];included=[];excluded=[];warnings=[]
 clauses=[s.strip() for s in re.split(r'[，,；;。\n、]+|并且|而且|同时|以及|但是|但|且',query) if s.strip()]
 for phrase in clauses:
  if re.search(r'或者|或',phrase) and ALIAS_RE.search(phrase):
   warnings.append(f'「{phrase}」包含数值条件的“或”逻辑，当前仅支持AND，未强行改写，请拆成单次筛选')
   continue
  unknowns=UNSUPPORTED_RE.findall(phrase)
  for item in unknowns:warnings.append(f'「{item}」当前数据源不支持，该条件无法执行，已忽略')
  masked=UNSUPPORTED_RE.sub(lambda m:' '*len(m[0]),phrase)
  recognized=bool(unknowns)
  # 行业保留语义名称，执行时再与新取财报里的行业核对，不提前请求股票数据。
  for industry in INDUSTRIES:
   if industry not in masked:continue
   recognized=True
   if industry in AMBIGUOUS:
    warnings.append(f'「{industry}」是宽泛主题，不能唯一对应行业，暂未执行；请写具体行业或用LLM进一步拆解')
   else:
    target=excluded if re.search(r'不要|排除|剔除|不选|不含|非',masked[:masked.find(industry)]) else included
    value={'券商':'证券','芯片':'半导体'}.get(industry,industry)
    if value not in target:target.append(value)
   masked=masked.replace(industry,' '*len(industry))
  baseline=re.compile(r'(?:不要|剔除|排除|不选)?\s*(?:\*?ST|新股|停牌(?:股)?)',re.I)
  if baseline.search(masked):recognized=True;masked=baseline.sub(lambda m:' '*len(m[0]),masked)
  matches=list(ALIAS_RE.finditer(masked))
  for index,match in enumerate(matches):
   recognized=True;metric=match.lastgroup;spec=METRIC_REGISTRY[metric]
   start=0 if index==0 else match.start();end=matches[index+1].start() if index+1<len(matches) else len(phrase)
   fragment=phrase[start:end].strip(' 和与及的，,') or match[0]
   terms=numerical_terms(fragment,metric)
   if not terms and re.search(r'为正|正数|正值',fragment):terms=[('>',0)]
   explicit=bool(terms)
   if metric=='np_yoy_rising':terms=[('rising_for',terms[0][1] if terms else spec['default_threshold'])]
   if not terms:
    op=spec['op'];threshold=spec['default_threshold']
    if '小盘' in fragment:op='<=';threshold=100
    if '大盘' in fragment:op='>=';threshold=1000
    if '破净' in fragment:op='<';threshold=1
    if '毛利率高' in fragment:op='>=';threshold=40
    if metric in ('vol_252','mdd_252') and re.search(r'(?:波动|回撤)(?:率)?(?:大|高)',fragment):op='>='
    if re.search(r'较?低|较?小',fragment) and metric not in ('np_value','np_yoy_rising'):op='<='
    if re.search(r'较?高|较?大',fragment) and metric not in ('np_value','np_yoy_rising'):op='>='
    terms=[(op,threshold)]
   # 不把“不要高PE”反向解释成要高PE；模糊否定会明确提示确认。
   negated=bool(re.search(r'不要|排除|不选',fragment))
   for op,threshold in terms:
    if negated and op!='rising_for':op={'<':'>=','<=':'>','>':'<=','>=':'<'}[op]
    confidence=.95 if explicit else .55
    if fragment in ('赚钱','盈利','不亏损','破净'):confidence=.9
    condition=Condition(id=f'rule_{len(conditions)+1}',phrase=fragment,metric=metric,op=op,threshold=threshold,
     type='soft' if not explicit and metric in ('vol_252','mdd_252') else 'hard',weight=spec['weight'],confidence=confidence)
    conditions.append(condition)
    if condition.needs_confirm:warnings.append(f'「{fragment}」没有明确数值，{spec["name"]}门槛为建议值，请人工确认')
  if not recognized:warnings.append(f'「{phrase}」尚无法映射到可执行指标，已保留提示，未添加替代条件')
 if not conditions and not included and not excluded:warnings.append('没有可执行筛选条件，请补充具体指标或手动添加；不会使用旧版条件代替')
 return ScreeningRequest(query=query,as_of=as_of,conditions=conditions,include_industries=included,exclude_industries=excluded,warnings=list(dict.fromkeys(warnings)))

def resolve_industry(value,industries):
 if value in industries:return value
 def plain(v):return re.sub(r'(?:行业|股)$','',str(v).strip().replace('Ⅱ','').replace('Ⅰ','').replace('Ⅲ',''))
 target={'券商':'证券','芯片':'半导体'}.get(plain(value),plain(value))
 matches=[v for v in industries if plain(v)==target]
 return matches[0] if len(matches)==1 else None

def validate_payload(payload,query,as_of=None,industries=()):
 warnings=[str(w) for w in payload.get('warnings',[])];conditions=[]
 for item in payload.get('conditions',[]):
  metric=item.get('metric')
  if metric not in METRIC_REGISTRY or not METRIC_REGISTRY[metric].get('selectable',True):
   warnings.append(f'模型使用了未注册指标 {metric}，已忽略');continue
  condition=Condition.model_validate(item)
  if not condition.phrase.strip() or condition.phrase not in query:
   warnings.append(f'条件「{condition.phrase}」不是当前输入片段，已忽略');continue
  # 明确门槛不能被注册表的 soft 默认值放宽成只打分。
  if condition.type=='soft' and numerical_terms(condition.phrase,metric) and not re.search(r'尽量|优先|偏好|最好|倾向|加分',condition.phrase):
   condition.type='hard'
   warnings=[w for w in warnings if not (condition.phrase in w and re.search(r'软|不硬性|不剔除',w))]
   warnings.append(f'「{condition.phrase}」包含明确门槛，按必须满足的硬条件执行')
  if condition.needs_confirm:warnings.append(f'「{condition.phrase}」理解拿不准，请人工确认')
  conditions.append(condition.model_dump())
 filters={}
 for key in ('include_industries','exclude_industries'):
  filters[key]=[]
  for value in payload.get(key,[]):
   match=resolve_industry(value,industries) if industries else str(value).strip()
   if match:filters[key].append(match)
   else:warnings.append(f'行业「{value}」未唯一匹配实际行业，已忽略，请人工确认')
 if not conditions and not any(filters.values()):warnings.append('没有可执行条件，请修改想法或手动添加；未套用默认模板')
 payload=dict(payload,query=query,as_of=as_of,conditions=conditions,**filters,warnings=warnings,universe_exclude=['ST','新股','停牌'],logic='AND')
 return ScreeningRequest.model_validate(payload)

def build_system_prompt(as_of,industries):
 registry=[{k:spec[k] for k in ('id','name','meaning','unit','default_threshold','type','op','guard','requires_kline','source','frequency','selectable')} for spec in METRIC_REGISTRY.values()]
 examples=[rule_query(q).model_dump(mode='json') for q in ('银行股，PE低于10，波动小','近半年涨幅超过20%，市值100亿以上')]
 return '\n'.join([
  '逐条拆解当前用户选股想法，只输出ScreeningRequest JSON。禁止套用固定条件或添加用户没有表达的要求。',
  '每个conditions项包含用户原文phrase、注册metric、op、threshold、hard/soft、confidence；每个未能执行的片段必须在warnings逐条说明。',
  '只用注册指标；比例单位用小数，财报同比/换手率的%用百分数。模糊高低门槛为建议值，confidence<0.6。',
  '明确的数值上限/下限必须是hard（例如波动率不超过25%=>vol_252<=0.25、hard）。只有“尽量/优先/偏好”或没有明确数值的走势偏好才可为soft；注册表默认类型不能覆盖用户明确要求。',
  '行业使用顶层include_industries/exclude_industries。没有提供行业白名单时保留具体行业名称，执行时再与在线数据核对。科技/新能源等宽泛主题不能猜具体行业。',
  '赚钱=>np_value>0；估值=>pe_ttm/pb；市值=>total_mv_yi；动量=>ret_20/60/120；稳定=>vol_252；回撤=>mdd_252；价格=>price。经营改善仅在用户表达该意图时使用np_yoy_rising。',
  '不支持的负债率/机构重仓/分红等必须warning，不能用无关指标替代。ST、新股、停牌默认排除。负PE不能当便宜，守卫由程序执行。',
  'as_of为null，执行前不要假装已取得行情。用户输入为空或无支持指标时conditions为空，不回退默认模板。',
  '注册表：'+json.dumps(registry,ensure_ascii=False),'不支持指标：'+json.dumps(UNSUPPORTED_METRICS,ensure_ascii=False),
  '可用行业：'+json.dumps(sorted(industries),ensure_ascii=False),'JSON Schema：'+json.dumps(ScreeningRequest.model_json_schema(),ensure_ascii=False),
  '不同想法对应不同条件示例：'+json.dumps(examples,ensure_ascii=False),
  '反例：用户只说银行和低PE，却添加经营改善或波动率；用户要低负债却改用低PE。均禁止。'])

def _completion(base_url,api_key,model,messages):
 body=dict(model=model,messages=messages,temperature=0,response_format={'type':'json_object'})
 try:
  from openai import OpenAI,APIConnectionError
  import httpx
 except ImportError:
  request=urllib.request.Request(base_url.rstrip('/')+'/chat/completions',data=json.dumps(body).encode(),method='POST',headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'})
  for trust_env in (False,True):
   opener=urllib.request.build_opener() if trust_env else urllib.request.build_opener(urllib.request.ProxyHandler({}))
   try:
    with opener.open(request,timeout=30) as response:return json.load(response)['choices'][0]['message']['content']
   except urllib.error.HTTPError:raise
   except urllib.error.URLError:
    if trust_env:raise
 else:
  # 系统代理可能阻断 TLS；优先直连，仅连接故障时再尝试系统代理。
  for trust_env in (False,True):
   try:
    with OpenAI(base_url=base_url,api_key=api_key,timeout=30,max_retries=0,http_client=httpx.Client(trust_env=trust_env,timeout=30)) as client:
     return client.chat.completions.create(**body).choices[0].message.content
   except APIConnectionError:
    if trust_env:raise

def parse_query(query,*,as_of=None,industries=(),base_url=None,api_key=None,model=None):
 base_url=os.getenv('LLM_BASE_URL','') if base_url is None else base_url
 api_key=os.getenv('LLM_API_KEY','') if api_key is None else api_key
 model=os.getenv('LLM_MODEL','') if model is None else model
 def fallback(reason):
  result=rule_query(query,as_of);result.warnings.insert(0,reason);return result
 if not query.strip():return ScreeningRequest(query=query,as_of=as_of,conditions=[],warnings=['请先输入选股想法'])
 if not api_key.strip():return fallback('未配置LLM Key：已按当前输入逐条规则解析；模糊门槛请确认，未识别片段会明确提示')
 if not base_url.strip() or not model.strip():return fallback('LLM配置不完整，已改用当前输入的规则解析')
 messages=[{'role':'system','content':build_system_prompt(as_of,industries)},{'role':'user','content':query}]
 for attempt in range(2):
  try:return validate_payload(json.loads(_completion(base_url,api_key,model,messages)),query,as_of,industries)
  except Exception as exc:
   error=type(exc).__name__
   if attempt==0:time.sleep(.5)
 return fallback(f'LLM两次调用失败（{error}），已改用当前输入的规则解析；没有套用旧条件')
