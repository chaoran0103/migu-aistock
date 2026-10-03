"""与指标注册表共享白名单的IR契约；导入本模块不会联网。"""
from __future__ import annotations

from datetime import date
from copy import deepcopy
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from metrics import METRIC_REGISTRY


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    phrase: str
    metric: str
    op: Literal['>=', '<=', '>', '<', 'rising_for']
    threshold: float | bool
    params: dict[str, Any] = Field(default_factory=dict)
    type: Literal['hard', 'soft'] = 'hard'
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    confidence: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)
    needs_confirm: bool = False

    @field_validator('metric')
    @classmethod
    def metric_registered(cls, value):
        if value not in METRIC_REGISTRY: raise ValueError(f'未注册指标: {value}')
        if not METRIC_REGISTRY[value].get('selectable',True): raise ValueError('行业使用include_industries/exclude_industries')
        return value

    @model_validator(mode='after')
    def consistent_operator(self):
        self.needs_confirm = self.confidence < .6
        if self.metric == 'np_yoy_rising':
            if self.threshold is True: self.threshold = 10.0  # 兼容上一步IR
            if self.op != 'rising_for' or isinstance(self.threshold, bool) or not math.isfinite(self.threshold):
                raise ValueError('经营改善使用rising_for，threshold为最新同比数值门槛')
            expected = METRIC_REGISTRY[self.metric]['params']
            if 'min_latest_yoy' in self.params:
                if float(self.params['min_latest_yoy']) != float(self.threshold):
                    raise ValueError('经营改善门槛冲突，请只使用threshold')
                self.params = {k: v for k, v in self.params.items() if k != 'min_latest_yoy'}
            if self.params and self.params != expected: raise ValueError('经营改善参数必须符合注册表定义')
            self.params = dict(expected)
        else:
            if self.op == 'rising_for': raise ValueError('rising_for仅适用于经营改善')
            if isinstance(self.threshold, bool) or not math.isfinite(self.threshold):
                raise ValueError('数值指标threshold必须为有限数值')
        return self


class ScreeningRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str
    # 草稿解析不取行情；实际执行后由数据源时间戳填入。
    as_of: date | None = None
    universe_exclude: list[Literal['ST', '新股', '停牌']] = Field(default_factory=lambda: ['ST', '新股', '停牌'])
    logic: Literal['AND'] = 'AND'
    conditions: list[Condition]
    limit: int = Field(default=30, ge=1, le=400)
    kline_cap: int = Field(default=150, ge=50, le=400)
    include_industries: list[str] = Field(default_factory=list)
    exclude_industries: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator('conditions')
    @classmethod
    def unique_ids(cls, values):
        ids = [c.id for c in values]
        if len(ids) != len(set(ids)): raise ValueError('条件id不能重复')
        return values


DEFAULT_IR = dict(
    query='经营改善、估值合理、走势相对稳定，剔除 ST 和新股',
    universe_exclude=['ST', '新股', '停牌'], logic='AND', limit=30,
    conditions=[condition.model_dump() for condition in [
        Condition(id='improving', phrase='经营改善', metric='np_yoy_rising',
                  op='rising_for', threshold=10),
        Condition(id='valuation', phrase='估值合理', metric='pe_ttm', op='<=', threshold=35),
        Condition(id='stability', phrase='走势相对稳定', metric='vol_252', op='<=',
                  threshold=.30, type='soft', weight=.8),
    ]],
)


def default_ir(as_of: date | str | None = None) -> ScreeningRequest:
    """保留显式测试模板；界面与解析失败不再使用它，构造草稿不联网。"""
    payload = deepcopy(DEFAULT_IR)
    if as_of is not None: payload['as_of'] = as_of
    return ScreeningRequest.model_validate(payload)
