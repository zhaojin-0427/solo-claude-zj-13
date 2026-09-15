"""逐轮演练引擎（纯函数、确定性）。

核心约定：
  * 每轮只返回“当下可执行动作”及其依据，不泄露后续步骤。
  * 材料为版本化实例：再次产出同名材料会使旧实例失效（stale），自动取最新有效实例。
  * 分支判定优先级固定：超时 > 材料缺失 > 结果不符 > 正常。
  * 同一版本、同一情境、同一提交序列，结果必然一致（无时钟、无随机）。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.graph import Graph
from app.schemas import Branch, ProcessSpec, Scenario, ScriptedRound, Step

# 触发器固定优先级（越靠前越优先）
TRIGGER_PRIORITY = ["timeout", "missing_any", "mismatch", "normal", "default"]


@dataclass
class MaterialInstance:
    key: str
    value: Any
    produced_round: int  # 0 表示情境初始材料
    produced_at_step: str  # "__initial__" 表示交接时已有
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "produced_round": self.produced_round,
            "produced_at_step": self.produced_at_step,
            "active": self.active,
        }


@dataclass
class EngineState:
    spec: ProcessSpec
    graph: Graph
    entry: str
    params: dict[str, Any]
    materials: dict[str, list[MaterialInstance]] = field(default_factory=dict)
    path: list[dict[str, Any]] = field(default_factory=list)  # 实际路径（仅被接受的动作）
    current: str | None = None
    elapsed_at_step: dict[str, int] = field(default_factory=dict)
    completed: bool = False
    outcome_step: str | None = None
    outcome: str | None = None

    def context(self) -> dict[str, Any]:
        """判定上下文：情境参数 + 最新有效材料值。"""
        ctx = dict(self.params)
        for key, instances in self.materials.items():
            active = latest_active(instances)
            if active is not None:
                ctx[key] = active.value
        return ctx

    def has_material(self, key: str) -> bool:
        return latest_active(self.materials.get(key, [])) is not None

    def active_instance(self, key: str) -> MaterialInstance | None:
        return latest_active(self.materials.get(key, []))

    def produce(self, key: str, value: Any, round_no: int, at_step: str) -> None:
        for inst in self.materials.get(key, []):
            inst.active = False
        self.materials.setdefault(key, []).append(
            MaterialInstance(key=key, value=value, produced_round=round_no,
                             produced_at_step=at_step)
        )


def latest_active(instances: list[MaterialInstance]) -> MaterialInstance | None:
    for inst in reversed(instances):
        if inst.active:
            return inst
    return None


def init_state(
    spec: ProcessSpec,
    graph: Graph,
    entry: str,
    params: dict[str, Any] | None = None,
    initial_materials: dict[str, Any] | None = None,
) -> EngineState:
    state = EngineState(spec=spec, graph=graph, entry=entry, params=dict(params or {}))
    state.current = entry
    for key, value in (initial_materials or {}).items():
        state.materials.setdefault(key, []).append(
            MaterialInstance(key=key, value=value, produced_round=0,
                             produced_at_step="__initial__")
        )
    return state


# ---------------------------------------------------------------------------
# 分支选择
# ---------------------------------------------------------------------------


def _matching_edge(edges: list[Branch], trigger: str, context: dict[str, Any]) -> Branch | None:
    """在同 trigger 的边中，按声明顺序找第一条附加条件成立的边；无条件边总成立。"""
    same_trigger = [e for e in edges if e.trigger == trigger]
    for edge in same_trigger:
        if edge.condition is None or edge.condition.evaluate(context):
            return edge
    return None


def select_edge(
    graph: Graph,
    step: Step,
    fired: str,
    context: dict[str, Any],
    force_default: bool = False,
) -> tuple[Branch | None, str | None, str]:
    """返回 (命中边, 目标步骤, 选择依据)。fired 为本次触发的异常类型或 normal。"""
    edges = graph.outgoing(step.code)

    if not force_default:
        edge = _matching_edge(edges, fired, context)
        if edge is not None:
            return edge, edge.target, f"命中分支触发器 {fired}"
        # 超时可用步骤自身声明兜底
        if fired == "timeout" and step.on_timeout:
            return None, step.on_timeout, f"步骤 {step.code} 声明的超时去向 {step.on_timeout}"
        if fired != "normal":
            return None, None, f"发生 {fired} 异常，但该节点没有可走的出口（分支无出口）"
        # normal 无显式正常边：default 兜底
        edge = _matching_edge(edges, "default", context)
        if edge is not None:
            return edge, edge.target, "正常完成，走默认出口"
        return None, None, "正常完成，本节点无后续边（就地结束）"

    # 学生强行继续：按 normal -> default -> 第一条可用边 的顺序
    for trig in ("normal", "default"):
        edge = _matching_edge(edges, trig, context)
        if edge is not None:
            return edge, edge.target, f"异常 {fired} 被学生强行忽略，按 {trig} 出口继续"
    if edges:
        edge = edges[0]
        return edge, edge.target, f"异常 {fired} 被学生强行忽略，沿第一条边继续"
    return None, None, f"异常 {fired} 被学生强行忽略，且无任何出口"


# ---------------------------------------------------------------------------
# 单轮执行
# ---------------------------------------------------------------------------


def execute_round(
    state: EngineState,
    round_no: int,
    produce: dict[str, Any] | None = None,
    elapsed_minutes: int = 0,
    role: str | None = None,
    force_default: bool = False,
) -> dict[str, Any]:
    """执行一轮。阻塞性问题（角色不符）直接拦截；异常按固定优先级选边。"""
    produce = produce or {}
    step = state.graph.steps.get(state.current or "")
    if step is None:
        return {"error": "invalid_current", "detail": f"当前节点 {state.current} 不存在"}
    if state.completed:
        return {"error": "session_completed", "detail": "演练已结束"}

    bases: list[str] = []
    violations: list[dict[str, str]] = []

    # 角色承接
    role_ok = True
    if step.role and role and role != step.role:
        role_ok = False
        bases.append(f"责任角色应为 {step.role}，实际由 {role} 承接")
    if not role_ok:
        return {
            "kind": "action",
            "round_no": round_no,
            "at_step": step.code,
            "blocked": True,
            "blocked_reason": "role_mismatch",
            "bases": bases,
            "next_step": state.current,
            "completed": False,
        }

    # 前置材料（先判定；缺失则直接走异常边，本轮产出不入账）
    missing = [k for k in step.inputs if not state.has_material(k)]
    if missing:
        fired = "missing_any"
        bases.append(f"缺少前置材料: {missing}，本轮不注册任何产出")
        edge, target, edge_basis = select_edge(
            state.graph, step, fired, state.context(), force_default=force_default
        )
        bases.append(edge_basis)
        no_exit = edge is None and target is None
        violations: list[dict[str, str]] = []
        if force_default:
            violations.append({
                "type": "force_through",
                "detail": f"在 {step.code} 发生 {fired} 时强行按默认方向继续",
            })
        result = {
            "kind": "action",
            "round_no": round_no,
            "at_step": step.code,
            "blocked": False,
            "fired": fired,
            "missing": missing,
            "failed_checks": [],
            "timed_out": False,
            "elapsed_minutes": state.elapsed_at_step.get(step.code, 0),
            "produced": [],
            "rejected_produce": sorted(produce.keys()),
            "stale": [],
            "edge": {
                "trigger": edge.trigger if edge else fired,
                "target": target,
                "label": edge.label if edge else "",
                "terminal_outcome": edge.terminal_outcome if edge else None,
            },
            "next_step": target,
            "completed": False,
            "outcome": None,
            "bases": bases,
            "violations": violations,
            "no_exit": no_exit,
        }
        state.path.append({
            "round_no": round_no, "step": step.code,
            "fired": fired, "forced": force_default,
        })
        if target is None:
            if no_exit:
                result["outcome"] = f"异常 {fired} 无出口，流程在 {step.code} 停滞"
            else:
                state.completed = True
                state.outcome_step = step.code
                state.outcome = edge.terminal_outcome if edge else None
                result["completed"] = True
                result["outcome"] = state.outcome
        else:
            if target != step.code:
                state.elapsed_at_step[target] = 0
            state.current = target
        return result

    # 注册本轮产出（旧实例自动失效）
    stale_events: list[dict[str, Any]] = []
    for key, value in produce.items():
        prev = state.active_instance(key)
        if prev is not None and prev.produced_at_step != "__initial__":
            stale_events.append(
                {"key": key, "superseded_round": prev.produced_round,
                 "superseded_at_step": prev.produced_at_step}
            )
        state.produce(key, value, round_no, step.code)

    context = state.context()

    # 超时累计
    state.elapsed_at_step[step.code] = state.elapsed_at_step.get(step.code, 0) + elapsed_minutes
    elapsed = state.elapsed_at_step[step.code]
    timed_out = step.time_limit_minutes is not None and elapsed > step.time_limit_minutes

    # 结果校验（只在未超时、材料齐的情况下判定）
    failed_checks = []
    if not timed_out and not missing:
        for check in step.result_checks:
            if not check.evaluate(context):
                failed_checks.append(
                    {"key": check.key, "expected": f"{check.op} {check.value!r}",
                     "actual": context.get(check.key),
                     "label": check.label}
                )

    # 触发器优先级
    if timed_out:
        fired = "timeout"
        bases.append(f"已耗时 {elapsed} 分钟，超过时限 {step.time_limit_minutes} 分钟")
    elif missing:
        fired = "missing_any"
        bases.append(f"缺少前置材料: {missing}")
    elif failed_checks:
        fired = "mismatch"
        for fc in failed_checks:
            bases.append(
                f"结果不符：{fc['label'] or fc['key']} 实际为 {fc['actual']!r}，要求 {fc['expected']}"
            )
    else:
        fired = "normal"
        bases.append(f"步骤 {step.name}（{step.code}）正常执行，责任角色 {step.role or '未指定'}")

    edge, target, edge_basis = select_edge(
        state.graph, step, fired, context, force_default=force_default
    )
    bases.append(edge_basis)

    no_exit = fired != "normal" and edge is None and target is None
    if force_default and fired != "normal":
        violations.append({
            "type": "force_through",
            "detail": f"在 {step.code} 发生 {fired} 时强行按默认方向继续",
        })
    if fired == "mismatch" and force_default:
        violations.append({"type": "missed_escalation",
                           "detail": f"{step.code} 结果不符未按异常分支升级"})
    if fired == "timeout" and force_default:
        violations.append({"type": "missed_escalation",
                           "detail": f"{step.code} 超时未按异常分支升级"})

    result: dict[str, Any] = {
        "kind": "action",
        "round_no": round_no,
        "at_step": step.code,
        "blocked": False,
        "fired": fired,
        "missing": missing,
        "failed_checks": failed_checks,
        "timed_out": timed_out,
        "elapsed_minutes": elapsed,
        "produced": sorted(produce.keys()),
        "stale": stale_events,
        "edge": {
            "trigger": (edge.trigger if edge else fired),
            "target": target,
            "label": edge.label if edge else "",
            "terminal_outcome": edge.terminal_outcome if edge else None,
        },
        "next_step": target,
        "completed": False,
        "outcome": None,
        "bases": bases,
        "violations": violations,
        "no_exit": no_exit,
    }

    # 推进状态
    state.path.append({
        "round_no": round_no,
        "step": step.code,
        "fired": fired,
        "forced": force_default and fired != "normal",
    })
    if target is None:
        if no_exit:
            # 异常发生却没有出口：流程在本节点停滞，不算抵达明确结局
            state.current = step.code
            result["completed"] = False
            result["outcome"] = f"异常 {fired} 无出口，流程在 {step.code} 停滞"
        else:
            state.completed = True
            state.outcome_step = step.code
            outcome_text = edge.terminal_outcome if edge else (
                f"在步骤 {step.name} 结束" if fired == "normal" else None
            )
            state.outcome = outcome_text
            result["completed"] = True
            result["outcome"] = outcome_text
    else:
        if target != step.code:
            state.elapsed_at_step[target] = 0
        state.current = target
    return result


def record_jump_attempt(
    state: EngineState, round_no: int, from_step: str, to_step: str, reason: str = ""
) -> dict[str, Any]:
    """学生试图跳步：只记录，不改变当前节点（逐轮演练不允许越过当前动作）。"""
    return {
        "kind": "jump",
        "round_no": round_no,
        "from_step": from_step,
        "to_step": to_step,
        "reason": reason,
        "accepted": False,
        "basis": f"当前只可执行 {state.current} 处的动作，不能直接跳到 {to_step}",
    }


# ---------------------------------------------------------------------------
# 当下动作提示（每轮只给当前可执行动作 + 依据）
# ---------------------------------------------------------------------------


def current_prompt(state: EngineState) -> dict[str, Any]:
    step = state.graph.steps.get(state.current or "")
    if state.completed or step is None:
        return {
            "current_step": state.current,
            "completed": state.completed,
            "outcome": state.outcome,
            "action": None,
            "bases": ["演练已结束"],
        }
    inputs_status = []
    for key in step.inputs:
        inst = state.active_instance(key)
        if inst is None:
            inputs_status.append({"key": key, "available": False, "from": None})
        else:
            inputs_status.append({
                "key": key,
                "available": True,
                "from": inst.produced_at_step,
                "produced_round": inst.produced_round,
            })
    # 只暴露与当前节点直接相关的分支依据，不透露后续步骤内部细节
    branch_basis = []
    for edge in state.graph.outgoing(step.code):
        desc = edge.trigger
        if edge.label:
            desc += f"（{edge.label}）"
        branch_basis.append(desc)
    return {
        "current_step": step.code,
        "current_step_name": step.name,
        "completed": False,
        "action": {
            "role": step.role,
            "must_produce": step.outputs,
            "time_limit_minutes": step.time_limit_minutes,
            "note": step.note,
        },
        "bases": [
            f"依据流程 v{state.spec.version} 步骤 {step.name}（{step.code}）",
            f"责任角色: {step.role or '未指定（无人承接）'}",
            f"前置材料: {[i['key'] + ('✓' if i['available'] else '✗缺失') for i in inputs_status]}",
            f"本步骤时限: {step.time_limit_minutes} 分钟" if step.time_limit_minutes else "本步骤无明确时限",
            f"本节点可能的处置方向: {branch_basis}" if branch_basis else "本节点无后续分支",
        ],
        "inputs_status": inputs_status,
    }


# ---------------------------------------------------------------------------
# 事件回放：从持久化事件重建确定状态
# ---------------------------------------------------------------------------


def replay(spec: ProcessSpec, graph: Graph, events: list[dict[str, Any]]) -> EngineState:
    """从持久化事件重建状态。动作事件保存了完整产出，回放结果确定且一致。"""
    state: EngineState | None = None
    for ev in events:
        payload = ev["payload"]
        if ev["kind"] == "start":
            state = init_state(
                spec, graph,
                entry=payload["entry"],
                params=payload.get("params", {}),
                initial_materials=payload.get("initial_materials", {}),
            )
        elif ev["kind"] == "action" and state is not None and not payload.get("blocked"):
            execute_round(
                state,
                round_no=payload["round_no"],
                produce=payload.get("produce_full", {}),
                elapsed_minutes=payload.get("elapsed_delta", 0),
                role=payload.get("role"),
                force_default=payload.get("force_default", False),
            )
    assert state is not None, "事件流缺少 start 事件"
    return state


# ---------------------------------------------------------------------------
# 情境脚本仿真（用于复盘的预期路径、修复验证）
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    path: list[dict[str, Any]]
    completed: bool
    outcome_step: str | None
    outcome: str | None
    stopped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "completed": self.completed,
            "outcome_step": self.outcome_step,
            "outcome": self.outcome,
            "stopped_reason": self.stopped_reason,
        }


def simulate(spec: ProcessSpec, graph: Graph, scenario: Scenario,
             rounds: list[ScriptedRound] | None = None) -> SimResult:
    """按情境脚本逐轮仿真，全程不访问外部状态（确定性）。"""
    rounds = rounds if rounds is not None else scenario.rounds
    state = init_state(
        spec, graph, entry=scenario.entry,
        params=scenario.params, initial_materials=scenario.initial_materials,
    )
    results: list[dict[str, Any]] = []
    for idx, sr in enumerate(rounds, start=1):
        if state.completed:
            return SimResult(state.path, True, state.outcome_step, state.outcome,
                             stopped_reason="脚本在结局之后仍有轮次")
        if sr.at != state.current:
            return SimResult(
                deepcopy(state.path), state.completed, state.outcome_step, state.outcome,
                stopped_reason=f"脚本要求在 {sr.at} 行动，但当前节点是 {state.current}（可能发生了跳步或缺边）",
            )
        res = execute_round(
            state, round_no=idx,
            produce=sr.produce, elapsed_minutes=sr.elapsed_minutes,
            role=sr.role, force_default=sr.force,
        )
        if res.get("blocked"):
            return SimResult(deepcopy(state.path), False, state.outcome_step, state.outcome,
                             stopped_reason=f"{sr.at} 被拦截: {res.get('blocked_reason')}")
        if res.get("no_exit"):
            return SimResult(deepcopy(state.path), False, state.outcome_step, state.outcome,
                             stopped_reason=res.get("outcome"))
        results.append(res)
    return SimResult(deepcopy(state.path), state.completed, state.outcome_step, state.outcome)
