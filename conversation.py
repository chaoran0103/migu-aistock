"""短期对话编排。只调用现有解析/解释工具，不修改数据或筛选规则。

状态由 Streamlit 会话独占，不写磁盘；模型只返回条件增量，不能执行网络取数。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from metrics import METRIC_REGISTRY, UNSUPPORTED_METRICS
from parse import (ALIAS_RE, AMBIGUOUS, UNSUPPORTED_RE, _completion,
                   numerical_terms, rule_query, scaled)
from schema import Condition, ScreeningRequest

MAX_INPUT = 2000
MAX_MESSAGES = 40


class ConditionEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    metric: str | None = None
    phrase: str | None = None
    op: Literal['>=', '<=', '>', '<', 'rising_for'] | None = None
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    params: dict | None = None
    type: Literal['hard', 'soft'] | None = None
    weight: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['add', 'update', 'remove', 'industry', 'limit']
    target_id: str | None = None
    condition: ConditionEdit | None = None
    field: Literal['include_industries', 'exclude_industries'] | None = None
    action: Literal['append', 'remove', 'replace'] | None = None
    values: list[str] = Field(default_factory=list, max_length=30)
    limit: int | None = Field(default=None, ge=1, le=400)


class DialoguePlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actions: list[Operation] = Field(default_factory=list, max_length=30)
    question: str = Field(default='', max_length=1000)
    warnings: list[str] = Field(default_factory=list, max_length=30)
    intent: Literal['edit', 'clarify', 'capabilities', 'results'] = 'edit'
    needs_answer: bool = False


@dataclass
class Conversation:
    request: ScreeningRequest | None = None
    messages: list[dict] = field(default_factory=list)
    pending: ScreeningRequest | None = None
    pending_question: str = ''
    pending_ids: set[str] = field(default_factory=set)
    clarification_topic: str = ''
    # A question without a proposal cannot be silently accepted.
    awaiting: bool = False
    confirmable: bool = False
    undo: list[ScreeningRequest | None] = field(default_factory=list)
    revision: int = 0
    mode: str = '规则解析'
    warnings: list[str] = field(default_factory=list)


def say(state, role, content):
    state.messages.append({'role': role, 'content': content})
    state.messages = state.messages[-MAX_MESSAGES:]


def has_filters(request):
    return bool(request and (request.conditions or request.include_industries or request.exclude_industries))


def condition_text(c):
    spec = METRIC_REGISTRY[c.metric]
    factor = 100 if spec['unit'] == '比例' else 1
    unit = '%' if spec['unit'] == '比例' else spec['unit']
    operator = '最新同比 ≥' if c.op == 'rising_for' else c.op.replace('<=', '≤').replace('>=', '≥')
    suffix = '（并满足同比改善、最近两期为正及净利润为正）' if c.metric == 'np_yoy_rising' else ''
    return f'{spec["name"]} {operator} {float(c.threshold)*factor:g}{unit}{suffix} · {"必须满足" if c.type == "hard" else "优先满足"}'


def describe(request):
    if request is None:
        return '尚未设置筛选条件。'
    lines = [condition_text(c) for c in request.conditions]
    if request.include_industries:
        lines.insert(0, '纳入行业：' + '、'.join(request.include_industries))
    if request.exclude_industries:
        lines.append('排除行业：' + '、'.join(request.exclude_industries))
    return '\n'.join('• ' + line for line in lines) or '尚未设置筛选条件。'


def request_signature(request):
    if request is None:
        return None
    payload = request.model_dump(mode='json')
    for key in ('query', 'warnings', 'as_of'):
        payload.pop(key, None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def conflicts(request):
    """只在编排层提示互斥门槛，不改引擎的判断规则。"""
    if request is None:
        return []
    issues = []
    both = set(request.include_industries) & set(request.exclude_industries)
    if both:
        issues.append('行业同时被纳入和排除：' + '、'.join(sorted(both)))
    for metric in {c.metric for c in request.conditions}:
        cs = [c for c in request.conditions if c.metric == metric and c.type == 'hard']
        lower = [(float(c.threshold), c.op == '>') for c in cs if c.op in ('>', '>=')]
        upper = [(float(c.threshold), c.op == '<') for c in cs if c.op in ('<', '<=')]
        if METRIC_REGISTRY[metric].get('guard') in ('>0', 'positive', 'value>0'):
            lower.append((0., True))
        # Guard text is descriptive in the original registry; use registered PE/PB guards.
        if metric in ('pe_ttm', 'pe_dynamic', 'pe_static', 'pb'):
            lower.append((0., True))
        if lower and upper:
            lo, hi = max(lower), min(upper, key=lambda x: (x[0], not x[1]))
            if lo[0] > hi[0] or (lo[0] == hi[0] and (lo[1] or hi[1])):
                issues.append(METRIC_REGISTRY[metric]['name'] + '的上下限互相冲突')
    return issues


def commit(state, request):
    if request_signature(state.request) != request_signature(request):
        state.undo.append(deepcopy(state.request))
        state.undo = state.undo[-10:]
        state.revision += 1
    state.request = deepcopy(request)
    state.pending = None
    state.pending_question = ''
    state.pending_ids = set()
    state.clarification_topic = ''
    state.awaiting = False
    state.confirmable = False


def sync_manual(state, request):
    """手工工作台是同一份条件的另一个入口；外部修改作废未确认提案。"""
    if request_signature(state.request) != request_signature(request):
        commit(state, request)
        say(state, 'assistant', '已同步你在对话卡片中的修改，后续对话会沿用这些条件。')


def accept(state):
    state = deepcopy(state)
    if state.pending is None or not state.confirmable:
        say(state, 'assistant', '还需要你回答上面的澄清问题，暂时没有可以直接采用的条件。')
        return state
    issues = conflicts(state.pending)
    if issues:
        say(state, 'assistant', '请先解决条件冲突：' + '；'.join(issues))
        return state
    commit(state, state.pending)
    say(state, 'assistant', '已采用这组条件。你可以继续修改，或点击“执行筛选”查询最新数据。')
    return state


def dismiss(state):
    state = deepcopy(state)
    state.pending = None
    state.pending_question = ''
    state.pending_ids = set()
    state.clarification_topic = ''
    state.awaiting = False
    state.confirmable = False
    say(state, 'assistant', '已取消本次待确认内容，之前生效的条件保持不变。')
    return state


def can_execute(state):
    return has_filters(state.request) and not state.awaiting and not conflicts(state.request)


def apply_plan(base, plan, text):
    request = deepcopy(base) if base else ScreeningRequest(query=text, conditions=[])
    by_id = {c.id: c for c in request.conditions}
    for op in plan.actions:
        if op.kind in ('add', 'update'):
            if not op.condition:
                raise ValueError('缺少条件内容')
            data = op.condition.model_dump(exclude_none=True)
            if op.kind == 'update':
                if op.target_id not in by_id:
                    raise ValueError('要修改的条件已不存在')
                previous = by_id[op.target_id]
                data = dict(previous.model_dump(), **data, id=previous.id)
            else:
                if 'metric' in data and data['metric'] in METRIC_REGISTRY:
                    spec = METRIC_REGISTRY[data['metric']]
                    data.setdefault('weight', spec['weight'])
                    data.setdefault('type', spec['type'])
                data['id'] = 'chat_' + uuid.uuid4().hex[:10]
            if not str(data.get('phrase', '')).strip() or data['phrase'] not in text:
                raise ValueError('条件必须对应本轮用户原话')
            if '权重' not in text and 'weight' not in text.lower():
                if op.kind == 'add' and data.get('metric') in METRIC_REGISTRY:
                    data['weight'] = METRIC_REGISTRY[data['metric']]['weight']
                elif op.kind == 'update':
                    data['weight'] = previous.weight
            c = Condition.model_validate(data)
            if UNSUPPORTED_RE.search(c.phrase) and not ALIAS_RE.search(UNSUPPORTED_RE.sub('', c.phrase)):
                raise ValueError('不支持的要求不能替换为无关指标')
            # Explicit words and units win over model guesses (低于 is <, not <=).
            terms = numerical_terms(c.phrase, c.metric)
            if len(terms) == 1:
                operator, threshold = terms[0]
                c.op = 'rising_for' if c.metric == 'np_yoy_rising' else operator
                c.threshold = threshold
            explicit_answer = bool(re.fullmatch(r'\s*\d+(?:\.\d+)?\s*(?:%|％|倍|元)?\s*[。！!]?\s*', c.phrase))
            if (terms or explicit_answer) and not re.search(r'尽量|优先|偏好|最好|倾向|加分', c.phrase):
                c.type = 'hard'
            # An inferred numeric preference is always a proposal, even if the model overstates confidence.
            if 'threshold' in op.condition.model_fields_set and not terms and not re.search(r'\d', c.phrase) and c.phrase not in ('赚钱','盈利','不亏损','破净') and not re.search('为正|正数|正值',c.phrase):
                c.confidence = min(c.confidence, .55)
            c = Condition.model_validate(c.model_dump())
            by_id[c.id] = c
        elif op.kind == 'remove':
            if op.target_id not in by_id:
                raise ValueError('要删除的条件已不存在')
            del by_id[op.target_id]
        elif op.kind == 'industry':
            if not op.field or not op.action:
                raise ValueError('缺少行业操作')
            values = list(dict.fromkeys(v.strip() for v in op.values if v.strip()))
            if any(v in AMBIGUOUS for v in values):
                raise ValueError('宽泛主题需要澄清为具体行业')
            existing = getattr(request, op.field)
            if op.action == 'replace':
                result = values
            elif op.action == 'remove':
                result = [v for v in existing if v not in values]
            else:
                result = list(dict.fromkeys(existing + values))
            setattr(request, op.field, result)
        elif op.kind == 'limit':
            if op.limit is None:
                raise ValueError('缺少展示数量')
            request.limit = op.limit
    request.conditions = list(by_id.values())
    request.as_of = None
    request.warnings = list(plan.warnings)
    # Compact factual condition summary prevents an ever-growing query across turns.
    request.query = describe(request).replace('\n', '；')
    return ScreeningRequest.model_validate(request.model_dump())


def build_prompt(state):
    registry = [{k: s[k] for k in ('id', 'name', 'meaning', 'unit', 'default_threshold', 'op', 'type', 'guard', 'selectable')} for s in METRIC_REGISTRY.values()]
    base = state.pending if state.pending is not None else state.request
    return '\n'.join([
        '你是觅股的对话选股条件助手，只输出DialoguePlan JSON，不输出股票数据或投资结论。',
        '当前条件是唯一事实。返回本轮所需的增量actions，不要重复添加、重建整套条件或清空未提及项。用户没有要求修改条件时actions必须为空。',
        'update/remove必须使用当前条件的准确id；update.condition只写修改字段及phrase；add.condition需要metric/op/threshold/phrase/type/weight/confidence。phrase必须是本轮用户原话片段。',
        '同指标上下限可能是两个条件。修改上限只修改上限；不明确指向哪一项就提问，不丢弃另一边。不要/排除银行是行业排除，取消PE条件才是remove。',
        'industry操作字段只能include_industries/exclude_industries，action是append/remove/replace。只有明确说“只看/换成”才替换该行业列表。明确改为排除某行业时同时移除原纳入列表中的该行业，反之亦然；不要保留互斥行业。科技/新能源/军工等不能猜行业，先问具体行业。',
        '模糊门槛给建议confidence<0.6并question询问，系统将整组变更暂存待确认；有歧义无法建议就只提问并needs_answer=true。回答“25%”可参考待确认条件，多个可能目标时继续澄清。',
        '条件的hard/soft、单位、公式遵循注册表。比例类用小数（25%=0.25），财报同比类用百分数。明确数值且没有优先/尽量含义，必须hard。',
        '仅允许AND。不支持的指标逐条warnings明确不能执行，不能替换为无关指标。不支持的排序、纳入ST/新股/停牌等也须明确说明；当前排序由既有引擎确定。',
        '保留已有的base universe排除规则；不能自己查询外网、输出价格、伪造完成、泄露配置。执行由用户点击按钮进行。',
        '询问能力返回intent=capabilities；询问结果返回intent=results；普通澄清返回intent=clarify。',
        '例：当前银行、PE<=15(id=p)、波动率<=0.3(id=v)；用户“PE改成10以下”：actions=[{"kind":"update","target_id":"p","condition":{"phrase":"PE改成10以下","threshold":10,"op":"<=","confidence":0.95}}]；其他条件不动。',
        '例：用户“再稳一点”，当前没有风险指标：actions=[]，question="你希望控制年化波动率还是最大回撤？"。',
        '注册表：' + json.dumps(registry, ensure_ascii=False),
        '不支持指标：' + json.dumps(UNSUPPORTED_METRICS, ensure_ascii=False),
        '输出Schema：' + json.dumps(DialoguePlan.model_json_schema(), ensure_ascii=False),
        '当前条件（若有提案则基于提案继续修订）：' + (base.model_dump_json() if base else 'null'),
        '上一个待回答问题：' + state.pending_question,
    ])


def rule_plan(state, text):
    """无模型时复用原规则解析器，补充可确定的上下文引用；不猜代词。"""
    base = state.pending if state.pending is not None else state.request
    cs = base.conditions if base else []
    if re.search(r'支持.*(?:指标|条件)|能做什么|怎么用|有哪些功能', text):
        return DialoguePlan(intent='capabilities')
    if re.search(r'为什么.*(?:入选|结果|没有|选中)|(?:解释|分析|查看).*(?:结果|候选)|筛选结果|筛选进度', text):
        return DialoguePlan(intent='results')
    if re.search(r'或者|或', text) and ALIAS_RE.search(text):
        return DialoguePlan(question='当前筛选只支持条件同时满足。请把“或”拆成两轮筛选，或改成明确的“且”条件。')
    if re.search(r'排序|按.*(?:从高到低|从低到高)', text):
        return DialoguePlan(question='当前排序固定为匹配度优先、总市值其次。是否保留这个排序，并补充需要的筛选条件？', warnings=['本轮未改变排序。'])
    metrics = list(dict.fromkeys(m.lastgroup for m in ALIAS_RE.finditer(text)))
    deleting = bool(re.search(r'删除|去掉|移除|取消|不限制|不要.*(?:条件|指标)', text))
    if deleting:
        if not metrics:
            found = rule_query(text)
            sectors = found.include_industries + found.exclude_industries
            if sectors and base:
                actions = [Operation(kind='industry', field=f, action='remove', values=sectors) for f in ('include_industries', 'exclude_industries') if set(getattr(base, f)) & set(sectors)]
                if actions: return DialoguePlan(actions=actions)
        targets = [c for c in cs if c.metric in metrics]
        if len(targets) > 1 and len(metrics) == 1:
            if re.search('上限', text): targets = [c for c in targets if c.op in ('<', '<=')]
            elif re.search('下限', text): targets = [c for c in targets if c.op in ('>', '>=')]
            elif not re.search('全部|所有|整个', text):
                return DialoguePlan(question='这个指标有多项条件，你想删除上限、下限，还是全部？')
        if targets:
            return DialoguePlan(actions=[Operation(kind='remove', target_id=c.id) for c in targets])
        return DialoguePlan(question='你想删除哪一个指标的条件？可以写“删除 PE 条件”。')
    # Resolve an explicit numerical answer to the single outstanding suggestion.
    if state.awaiting and not metrics:
        low = [c for c in cs if c.id in state.pending_ids]
        if len(low) == 1 and re.search(r'\d', text) and not re.search(r'[a-zA-Z\u4e00-\u9fff]{8,}', text):
            c = low[0]
            terms = numerical_terms(text, c.metric)
            match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(%|％|倍|元)?\s*[。！!]?\s*', text)
            if terms or match:
                op, value = terms[0] if terms else (c.op, scaled(match[1], match[2], c.metric))
                return DialoguePlan(actions=[Operation(kind='update', target_id=c.id, condition=dict(phrase=text, op=op, threshold=value, confidence=.95, type='hard'))])
    # Unspecified relative changes must be resolved before altering thresholds.
    if re.search(r'再.*(?:稳|便宜)|更稳|放宽|放松|收紧|严格一点|宽松一点|那个|这个条件|上限改|下限改', text) and not metrics:
        return DialoguePlan(question='你想调整哪个指标、改到多少？例如“PE 上限改为 20 倍”，我会保留其他条件。')
    parsed = rule_query(text)
    actions = []
    for c in parsed.conditions:
        existing = [x for x in cs if x.metric == c.metric]
        targets = [x for x in existing if (x.op in ('<', '<=') and c.op in ('<', '<=')) or (x.op in ('>', '>=') and c.op in ('>', '>=')) or x.op == c.op]
        if not targets and len(existing) == 1 and re.search(r'改|换', text): targets = existing
        if len(targets) > 1:
            return DialoguePlan(question='同一指标存在多条相同方向的条件，请点击卡片中的“修改条件”指定要修改哪一条。')
        if len(targets) == 1 and not numerical_terms(c.phrase, c.metric):
            m = re.search(r'(?:改成|改为|改到|设为)\s*(\d+(?:\.\d+)?)\s*(%|％|倍|元)?', c.phrase)
            if m:
                c.op = targets[0].op
                c.threshold = scaled(m[1], m[2], c.metric)
                c.confidence = .95
                c.needs_confirm = False
                parsed.warnings = [w for w in parsed.warnings if c.phrase not in w]
        attrs = c.model_dump(exclude={'id', 'needs_confirm'})
        if targets: actions.append(Operation(kind='update', target_id=targets[0].id, condition=attrs))
        else: actions.append(Operation(kind='add', condition=attrs))
    for field in ('include_industries', 'exclude_industries'):
        values = getattr(parsed, field)
        if values:
            action = 'replace' if re.search(r'只看|只要|仅看|换成|改成|改为', text) else 'append'
            opposite = 'exclude_industries' if field == 'include_industries' else 'include_industries'
            if base and set(getattr(base, opposite)) & set(values):
                actions.append(Operation(kind='industry', field=opposite, action='remove', values=values))
            actions.append(Operation(kind='industry', field=field, action=action, values=values))
    limit = re.search(r'(?:展示|显示|返回|前)\s*(\d+)\s*(?:只|个|家)', text)
    if limit and 1 <= int(limit[1]) <= 400:
        actions.append(Operation(kind='limit', limit=int(limit[1])))
    ambiguous = [v for v in AMBIGUOUS if v in text]
    question = ('“' + '、'.join(ambiguous) + '”覆盖多个行业，请写具体行业，例如半导体、软件开发或电池。') if ambiguous else ''
    if not actions and not question:
        question = '这句话暂时没有转成可执行条件。请补充具体指标、行业或数值，我会保留之前的条件。'
    return DialoguePlan(actions=actions, question=question, warnings=parsed.warnings)


def capabilities():
    groups = [
        '可以连续告诉我行业、估值、盈利、市值、价格、动量和风险要求，也可以修改、删除条件或撤销上一步。',
        '例如：先说“银行股，PE 低于 10”，再说“加上波动小”。模糊门槛会先请你确认。',
        '数字条件同时满足；原筛选引擎按匹配度、总市值排序。走势只计算初筛后的前 N 只。',
        '资产负债率、机构持仓等当前没有可执行指标，我会提示，不能编造或替换。',
        '短期上下文仅保留在本次会话中。确认条件后点击执行，才会查询股票数据。',
    ]
    return '\n\n'.join(groups)


def result_reply(context, text=''):
    if not context:
        return '还没有已完成的个人筛选结果。请先确认条件并执行；市场历史预览不作为个人筛选结果。'
    from explain import explain_stock
    result = context['result']
    for _, row in result.rows.iterrows():
        if str(row['code6']) in text or str(row['name']) in text:
            return explain_stock(row)
    stats = result.stats
    lines = [f'最近一次已完成筛选：初始股票池 {stats["universe_count"]:,} 只，符合条件 {stats["qualified_count"]:,} 只，展示 {stats["displayed_count"]:,} 只。',
             '该结果对应条件：\n' + describe(result.request)]
    if not result.funnel.empty:
        part = result.funnel.sort_values('removed', ascending=False).head(3)
        lines.append('移除数量最多的步骤：' + '；'.join(f'{r.step}移除 {int(r.removed)} 只' for r in part.itertuples()))
    lines += [stats.get('disclosure', ''), '可在结果洞察查看候选理由、差一点入选和敏感性；不会自动放宽条件。']
    lines += [str(w) for w in stats.get('warnings', [])]
    return '\n\n'.join(line for line in lines if line)


def respond(state, text, *, api_key='', base_url='', model='', context=None):
    state = deepcopy(state)
    # Preserve live sessions when the local module is hot reloaded during an upgrade.
    if not hasattr(state, 'pending_ids'):
        state.pending_ids = {c.id for c in state.pending.conditions if c.needs_confirm} if state.pending else set()
    if not hasattr(state, 'clarification_topic'): state.clarification_topic = ''
    text = text.strip()
    if not text:
        return state
    if len(text) > MAX_INPUT:
        say(state, 'assistant', f'单条消息请控制在 {MAX_INPUT} 字以内；当前条件没有改变。')
        return state
    say(state, 'user', text)
    simple = re.sub(r'[\s，,。.!！?？]', '', text)
    if simple in ('新一轮', '重新开始', '清空条件', '开始新的选股'):
        commit(state, None)
        say(state, 'assistant', '已清空当前条件。请描述新的选股想法；上一轮结果仍可在结果洞察查看。')
        return state
    if simple in ('撤销', '撤销上一步', '回到上一步'):
        if state.pending is not None or state.awaiting:
            return dismiss(state)
        if state.undo:
            state.request = state.undo.pop()
            state.revision += 1
            say(state, 'assistant', '已撤销上一次条件修改。\n\n' + describe(state.request))
        else:
            say(state, 'assistant', '当前没有可以撤销的条件修改。')
        return state
    if simple in ('确认', '确认条件', '采用建议', '就这样', '好的', '可以'):
        if state.awaiting:
            return accept(state)
        say(state, 'assistant', '条件已保留。点击下方“执行筛选”即可查询最新数据。' if has_filters(state.request) else '请先描述你的选股想法。')
        return state
    if simple in ('取消建议', '取消本次修改', '不要这个建议'):
        return dismiss(state)
    if simple in ('查看当前条件', '当前条件', '现在有哪些条件'):
        say(state, 'assistant', describe(state.request))
        return state
    if simple in ('执行', '执行筛选', '开始筛选', '确认并执行', '开始查询'):
        say(state, 'assistant', '请先回答澄清问题或采用建议条件，再执行。' if state.awaiting else '请核对下方条件并点击“执行筛选”，我会展示进度和结果。')
        return state
    original = deepcopy(state)
    fallback_note = ''
    try:
        if all(v.strip() for v in (api_key, base_url, model)):
            try:
                messages = [{'role': 'system', 'content': build_prompt(state)}] + state.messages[-12:]
                for attempt in range(2):
                    raw = _completion(base_url, api_key, model, messages)
                    try:
                        plan = DialoguePlan.model_validate_json(raw)
                        apply_plan(state.pending if state.pending is not None else state.request, plan, text)
                        break
                    except (ValidationError, ValueError, TypeError):
                        if attempt: raise
                        # One bounded format-repair attempt; never include exception bodies or credentials.
                        messages += [{'role':'assistant','content':raw[:12000]},
                                     {'role':'user','content':'JSON 或条件校验失败。请严格按给定 Schema 修正，使用当前条件准确 id 和本轮原话；不添加字段或未注册指标。只输出完整 JSON。'}]
                state.mode = '模型对话'
            except Exception:
                plan = rule_plan(state, text)
                state.mode = '规则对话'
                fallback_note = '模型本轮未返回可用条件，已使用规则解析；需要时请补充明确指标。'
        else:
            plan = rule_plan(state, text)
            state.mode = '规则对话'
        if plan.intent == 'capabilities':
            say(state, 'assistant', capabilities())
            return state
        if plan.intent == 'results':
            say(state, 'assistant', result_reply(context, text))
            return state
        candidate = apply_plan(state.pending if state.pending is not None else state.request, plan, text)
        # Unsupported requests never silently create unrelated substitutes.
        warnings = list(plan.warnings)
        for item in UNSUPPORTED_RE.findall(text):
            note = f'当前免费数据源不支持“{item}”，该条件无法执行。'
            if note not in warnings: warnings.append(note)
        if fallback_note: warnings.insert(0, fallback_note)
        state.warnings = list(dict.fromkeys(warnings))
        issues = conflicts(candidate)
        pending_ids = state.pending_ids
        low = [c for c in candidate.conditions if c.needs_confirm and (c.id in pending_ids or any(a.kind in ('add', 'update') and (a.condition.phrase if a.condition else None) == c.phrase for a in plan.actions))]
        question = plan.question
        unresolved_industry = state.clarification_topic == 'industry' and not any(a.kind == 'industry' for a in plan.actions)
        if unresolved_industry: question = state.pending_question
        if issues: question = '；'.join(issues) + '。你希望保留哪一个门槛？'
        if low and not issues and not plan.needs_answer and not unresolved_industry and not any(v in text for v in AMBIGUOUS):
            question = '这些描述没有唯一门槛，我建议：' + '；'.join(condition_text(c) for c in low) + '。是否采用，或告诉我具体数值？'
        if question or low:
            state.pending = candidate if plan.actions else state.pending
            state.pending_question = question
            state.pending_ids = {c.id for c in low}
            state.clarification_topic = 'industry' if unresolved_industry or any(v in text for v in AMBIGUOUS) else ('conflict' if issues else '')
            state.awaiting = True
            state.confirmable = bool(state.pending and low and not issues and not plan.needs_answer and not unresolved_industry and not any(v in text for v in AMBIGUOUS))
            if not plan.actions and plan.question: state.confirmable = False
            say(state, 'assistant', question)
        elif plan.actions:
            commit(state, candidate)
            say(state, 'assistant', '已更新你本轮提到的条件，其他条件保持不变。\n\n' + describe(candidate) + '\n\n可以继续补充，确认后点击“执行筛选”。')
        else:
            say(state, 'assistant', '当前条件没有改变。请补充具体指标或数值。')
        if warnings:
            say(state, 'assistant', '\n'.join(dict.fromkeys(warnings)))
    except Exception:
        state = original
        say(state, 'assistant', '本轮条件未能安全合并，之前的条件没有改变。请写明要调整的指标、运算符和数值，或点击卡片中的“修改条件”。')
    return state
