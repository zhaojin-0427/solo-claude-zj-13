"""候选补充规则与最少补充项搜索。

候选规则（app.schemas.CandidateRule）四类：
  add_branch   增加一条分支（可带条件）
  set_role     为无人承接的步骤指定角色
  mark_entry   把步骤标记为入口（修复缺少起点/不可达）
  fix_terminal 把节点标记为明确结局（修复分支无出口/停滞）

搜索目标：用最少数目的候选，使指定情境都能抵达明确结局（集合覆盖式枚举，
组合按 code 字典序展开，结果确定）。
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from app.engine import simulate
from app.graph import build_graph, validate_graph
from app.schemas import (
    Branch,
    CandidateRule,
    ProcessSpec,
    Scenario,
)


def apply_candidate(spec: ProcessSpec, rule: CandidateRule) -> ProcessSpec:
    """在副本上应用一条候选规则，返回新的流程定义（原对象不变）。"""
    updated = spec.model_copy(deep=True)
    steps = updated.step_map()

    if rule.source not in steps:
        raise ValueError(f"候选 {rule.code} 的源步骤 {rule.source} 不存在")

    if rule.kind == "add_branch":
        branch = Branch(
            source=rule.source,
            target=rule.target,
            trigger=rule.trigger,
            condition=rule.condition,
            label=rule.label,
            terminal_outcome=rule.terminal_outcome,
        )
        updated.branches.append(branch)

    elif rule.kind == "set_role":
        if not rule.role:
            raise ValueError(f"候选 {rule.code}（set_role）缺少 role")
        steps[rule.source].role = rule.role

    elif rule.kind == "mark_entry":
        steps[rule.source].entry = True

    elif rule.kind == "fix_terminal":
        steps[rule.source].terminal = True
        if rule.terminal_outcome:
            steps[rule.source].note = (
                steps[rule.source].note or ""
            )
        # 若没有就地终结边，补一条带结局说明的 default 就地终结边
        has_end_edge = any(
            b.source == rule.source and b.target is None
            for b in updated.branches
        )
        if not has_end_edge:
            updated.branches.append(
                Branch(
                    source=rule.source,
                    target=None,
                    trigger="default",
                    label=rule.label or "补充的明确结局",
                    terminal_outcome=rule.terminal_outcome or f"在 {rule.source} 明确收束",
                )
            )
    else:  # pragma: no cover - 由 Pydantic 枚举保证
        raise ValueError(f"未知候选类型: {rule.kind}")
    return updated


def apply_candidates(spec: ProcessSpec, rules: list[CandidateRule]) -> ProcessSpec:
    result = spec
    for rule in sorted(rules, key=lambda r: r.code):
        result = apply_candidate(result, rule)
    return result


def _scenario_reaches_end(spec: ProcessSpec, scenario: Scenario) -> tuple[bool, dict[str, Any]]:
    graph = build_graph(spec)
    issues = validate_graph(spec)
    if issues:
        return False, {
            "scenario_code": scenario.code,
            "ok": False,
            "reason": "应用候选后结构校验仍有问题",
            "issues": [i.to_dict() for i in issues],
        }
    sim = simulate(spec, graph, scenario)
    if not sim.completed:
        return False, {
            "scenario_code": scenario.code,
            "ok": False,
            "reason": sim.stopped_reason or "脚本结束后仍未抵达终态",
            "path": [p["step"] for p in sim.path],
        }
    if scenario.expected_outcome is not None:
        token = sim.outcome or sim.outcome_step
        if token != scenario.expected_outcome:
            return False, {
                "scenario_code": scenario.code,
                "ok": False,
                "reason": f"抵达结局 {token!r}，但预期为 {scenario.expected_outcome!r}",
                "path": [p["step"] for p in sim.path],
            }
    return True, {
        "scenario_code": scenario.code,
        "ok": True,
        "outcome_step": sim.outcome_step,
        "outcome": sim.outcome,
        "path": [p["step"] for p in sim.path],
    }


def evaluate_repair(
    spec: ProcessSpec,
    rules: list[CandidateRule],
    scenarios: list[Scenario],
) -> dict[str, Any]:
    patched = apply_candidates(spec, rules)
    details = []
    all_ok = True
    for scenario in scenarios:
        ok, detail = _scenario_reaches_end(patched, scenario)
        all_ok = all_ok and ok
        details.append(detail)
    return {"feasible": all_ok, "details": details}


def find_minimal(
    spec: ProcessSpec,
    candidates: list[CandidateRule],
    scenarios: list[Scenario],
    max_combinations: int = 100_000,
) -> dict[str, Any]:
    """搜索使所有情境抵达明确结局的最少候选集。

    候选按 code 排序后，组合也按 code 序列字典序枚举，因此返回值确定。
    """
    ordered = sorted(candidates, key=lambda c: c.code)
    by_code = {c.code: c for c in ordered}

    # 单个候选自身必须可应用（源步骤存在等）
    application_errors: dict[str, str] = {}
    for rule in ordered:
        try:
            apply_candidate(spec, rule)
        except ValueError as exc:
            application_errors[rule.code] = str(exc)
    usable = [c for c in ordered if c.code not in application_errors]

    checked = 0
    for size in range(0, len(usable) + 1):
        minimal_sets: list[list[str]] = []
        for combo in combinations(usable, size):
            checked += 1
            if checked > max_combinations:
                return {
                    "feasible": False,
                    "minimal": [],
                    "reason": f"组合空间超过上限 {max_combinations}，请缩小候选范围",
                    "checked": checked,
                }
            rules = list(combo)
            patched = apply_candidates(spec, rules)
            if validate_graph(patched):
                continue
            ok = True
            for scenario in scenarios:
                scenario_ok, _ = _scenario_reaches_end(patched, scenario)
                if not scenario_ok:
                    ok = False
                    break
            if ok:
                minimal_sets.append([r.code for r in rules])
        if minimal_sets:
            minimal_sets.sort()
            chosen = minimal_sets[0]
            final_eval = evaluate_repair(
                spec, [by_code[c] for c in chosen], scenarios
            )
            return {
                "feasible": True,
                "minimal": chosen,
                "minimal_size": len(chosen),
                "all_minimal_sets": minimal_sets,
                "checked": checked,
                "application_errors": application_errors,
                "details": final_eval["details"],
            }
    return {
        "feasible": False,
        "minimal": [],
        "reason": "全部候选的任意组合都无法让指定情境抵达明确结局",
        "checked": checked,
        "application_errors": application_errors,
    }
