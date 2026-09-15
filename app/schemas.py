"""Pydantic 模型：流程定义（步骤/前置材料/责任角色/产出/时限/分支）与演练情境。

所有外部入参都由本模块校验；引擎内部只使用这些模型，保证同版本行为确定。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 流程定义
# ---------------------------------------------------------------------------

# 判定操作符：对某个上下文键求值（上下文 = 情境参数 + 本轮已产出材料）
Operator = Literal[
    "eq", "ne", "exists", "missing", "in", "truthy", "falsy", "gte", "lte",
]


class Predicate(BaseModel):
    """条件判定：context[key] op value。"""

    key: str = Field(..., description="情境参数或材料键名")
    op: Operator = "eq"
    value: Any = None

    def evaluate(self, context: dict[str, Any]) -> bool:
        actual = context.get(self.key)
        if self.op == "eq":
            return actual == self.value
        if self.op == "ne":
            return actual != self.value
        if self.op == "exists":
            return actual is not None
        if self.op == "missing":
            return actual is None
        if self.op == "in":
            return actual in (self.value or [])
        if self.op == "truthy":
            return bool(actual)
        if self.op == "falsy":
            return not actual
        if self.op == "gte":
            return actual is not None and actual >= self.value
        if self.op == "lte":
            return actual is not None and actual <= self.value
        raise ValueError(f"未知操作符: {self.op}")


class ResultCheck(BaseModel):
    """结果校验：产出材料必须满足的条件（不符则进入结果不符异常）。"""

    key: str
    op: Operator = "eq"
    value: Any = None
    label: str = ""

    def evaluate(self, context: dict[str, Any]) -> bool:
        return Predicate(key=self.key, op=self.op, value=self.value).evaluate(context)


class Step(BaseModel):
    """流程中的一个步骤（有向图节点）。"""

    code: str = Field(..., description="步骤唯一编码，如 receive_new_hire")
    name: str = Field(..., description="步骤名称")
    role: str | None = Field(None, description="责任角色；为空即“无人承接”")
    inputs: list[str] = Field(default_factory=list, description="前置材料编码")
    outputs: list[str] = Field(default_factory=list, description="本步骤产出材料编码")
    time_limit_minutes: int | None = Field(None, ge=0, description="时限（分钟）")
    entry: bool = Field(False, description="是否为演练入口")
    terminal: bool = Field(False, description="是否为明确结局节点")
    on_timeout: str | None = Field(None, description="超时分支指向步骤编码")
    result_checks: list[ResultCheck] = Field(default_factory=list)
    note: str = ""

    @field_validator("code")
    @classmethod
    def _code_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("步骤编码不能为空")
        return v.strip()


class Branch(BaseModel):
    """正常/异常分支（有向图边）。

    trigger:
      - normal      正常分支（最多一条，缺省时按 default 走）
      - missing_*   材料缺失异常，missing_<材料编码>；通用 missing_any 表示任一前置缺失
      - mismatch    结果不符异常
      - timeout     超时异常（亦可由步骤 on_timeout 声明）
      - escalate    升级类异常
      - default     无其它分支命中时的兜底出口
    """

    source: str
    target: str | None = Field(None, description="目标步骤；None 表示就地终结（结局）")
    trigger: str = "normal"
    condition: Predicate | None = Field(
        None, description="分支附加条件（命中 trigger 且条件成立才走此边）"
    )
    label: str = ""
    terminal_outcome: str | None = Field(
        None, description="就地终结时的结局说明（target 为空时使用）"
    )
    implicit: bool = Field(
        False, description="是否由步骤 on_timeout 物化出的隐式边（校验/引擎内部使用）"
    )


class ProcessSpec(BaseModel):
    """一份完整的岗位流程定义（一个不可变版本的内容）。"""

    code: str = Field(..., description="流程编码")
    name: str
    version: int = Field(1, ge=1)
    steps: list[Step]
    branches: list[Branch] = Field(default_factory=list)
    description: str = ""

    @field_validator("steps")
    @classmethod
    def _steps_nonempty(cls, v: list[Step]) -> list[Step]:
        if not v:
            raise ValueError("流程至少需要一个步骤")
        return v

    def step_map(self) -> dict[str, Step]:
        return {s.code: s for s in self.steps}

    def step_codes(self) -> list[str]:
        return [s.code for s in self.steps]


# ---------------------------------------------------------------------------
# 演练情境
# ---------------------------------------------------------------------------


class ScriptedRound(BaseModel):
    """情境脚本中的一轮：学生在某步骤的动作与现场情况。

    produce    —— 本轮实际产出（材料编码 -> 值/内容）
    elapsed    —— 距上一动作经过的分钟数（用于判定是否超过步骤时限）
    jump_to    —— 学生试图直接提交的另一步骤（测试跳步拦截）
    force      —— 异常已发生仍要求按默认方向继续（用于复盘“漏升级”）
    role       —— 本轮实际承接角色（与步骤责任角色不符会被拦截）
    """

    at: str = Field(..., description="本步骤编码（学生应当所处节点）")
    produce: dict[str, Any] = Field(default_factory=dict)
    elapsed_minutes: int = Field(0, ge=0)
    jump_to: str | None = None
    force: bool = False
    role: str | None = None
    note: str = ""


class Scenario(BaseModel):
    """可重复演练的情境：初始参数 + 脚本化过程 + 预期结局。"""

    code: str
    name: str
    process_code: str
    entry: str = Field(..., description="演练入口步骤")
    params: dict[str, Any] = Field(
        default_factory=dict, description="情境参数（如证件类型、审批结论）"
    )
    initial_materials: dict[str, Any] = Field(
        default_factory=dict, description="交接时已有的材料（材料编码 -> 值）"
    )
    rounds: list[ScriptedRound] = Field(default_factory=list)
    expected_outcome: str | None = Field(
        None, description="预期结局（终态步骤编码或就地终结的 outcome 文本）"
    )
    description: str = ""


# ---------------------------------------------------------------------------
# API 请求体
# ---------------------------------------------------------------------------


class SessionStart(BaseModel):
    process_code: str
    version: int | None = Field(None, description="缺省取最新版本")
    scenario_code: str | None = None
    entry: str | None = Field(None, description="不使用情境时，可从任一入口开始")
    params: dict[str, Any] = Field(default_factory=dict)
    initial_materials: dict[str, Any] = Field(default_factory=dict)
    actor: str = "新人"


class ActionRequest(BaseModel):
    round_no: int = Field(..., ge=1, description="当前轮次，从 1 开始递增")
    produce: dict[str, Any] = Field(default_factory=dict, description="本轮产出材料")
    elapsed_minutes: int = Field(0, ge=0, description="本步骤已耗时（分钟）")
    role: str | None = Field(None, description="实际承接角色")
    force_default: bool = Field(
        False, description="材料缺失/结果不符时强行按默认方向继续（会被复盘标记）"
    )


class JumpAttempt(BaseModel):
    round_no: int = Field(..., ge=1)
    from_step: str
    to_step: str
    reason: str = ""


class LockRequest(BaseModel):
    mentor: str
    note: str = ""


class CandidateRule(BaseModel):
    """候选补充规则。kind 决定字段组合：

    add_branch  需要 trigger/source/target（可带 condition）
    set_role    需要 source + role
    mark_entry  需要 source
    fix_terminal 需要 source（把该节点标记为终态/出口）
    """

    code: str
    kind: Literal["add_branch", "set_role", "mark_entry", "fix_terminal"]
    source: str
    target: str | None = None
    trigger: str = "normal"
    condition: Predicate | None = None
    role: str | None = None
    label: str = ""
    terminal_outcome: str | None = None


class RepairRequest(BaseModel):
    process_code: str
    version: int | None = None
    scenario_codes: list[str] = Field(..., min_length=1)
    candidates: list[CandidateRule] = Field(..., min_length=1)
    allow_locked: bool = False


class SimulateRepair(BaseModel):
    """对某个情境按脚本完整跑一遍（用于修复验证与预期路径生成）。"""

    scenario_code: str
    candidate_codes: list[str] = Field(default_factory=list)
