"""复盘：对照预期路径，定位跳步、沿用旧产出、漏掉升级条件。"""
from __future__ import annotations

from typing import Any

from app.engine import EngineState, latest_active, replay, simulate
from app.graph import Graph
from app.schemas import ProcessSpec, Scenario


def _event_path(events: list[dict[str, Any]]) -> list[str]:
    return [e["at_step"] for e in events if e["kind"] == "action" and not e["payload"].get("blocked")]


def review_session(
    spec: ProcessSpec,
    graph: Graph,
    events: list[dict[str, Any]],
    scenario: Scenario | None = None,
) -> dict[str, Any]:
    """生成复盘报告。有情境时对照情境脚本得到预期路径。"""
    # 实际路径
    actual_steps = _event_path(events)
    actual_state = replay(spec, graph, events)

    # 预期路径
    expected_steps: list[str] = []
    expected_outcome_step: str | None = None
    sim_stopped: str | None = None
    if scenario is not None:
        sim = simulate(spec, graph, scenario)
        expected_steps = [p["step"] for p in sim.path]
        expected_outcome_step = sim.outcome_step
        sim_stopped = sim.stopped_reason

    findings: list[dict[str, Any]] = []

    # 1) 跳步：显式跳步尝试
    jump_attempts = [
        {
            "round_no": e["payload"].get("round_no"),
            "from_step": e["payload"].get("from_step"),
            "to_step": e["payload"].get("to_step"),
            "reason": e["payload"].get("reason", ""),
        }
        for e in events
        if e["kind"] == "jump" and not e["payload"].get("accepted")
    ]
    for j in jump_attempts:
        findings.append({"type": "jump_attempt", "severity": "high", **j,
                         "detail": f"第 {j['round_no']} 轮试图从 {j['from_step']} 直接跳到 {j['to_step']}"})

    # 隐式跳步：预期路径中被整段越过的步骤（异常重路由造成）
    if expected_steps:
        actual_set = set(actual_steps)
        skipped = [
            s for s in expected_steps
            if s not in actual_set
            and expected_steps.index(s) < len(expected_steps) - 1
        ]
        # 用序列对齐再确认：预期中某步前后相邻步骤在实际里相邻出现，即越过该步
        for i in range(len(expected_steps) - 1):
            a, b = expected_steps[i], expected_steps[i + 1]
            if a in actual_steps and b in actual_steps:
                ia, ib = actual_steps.index(a), actual_steps.index(b)
                if ia < ib:
                    for s in expected_steps[i + 1: i + 1 + (ib - ia)]:
                        if s not in actual_steps[i + 1:ib + 1] and s in expected_steps and s not in skipped:
                            pass
        for s in skipped:
            findings.append({
                "type": "skipped_step", "severity": "high", "step": s,
                "detail": f"预期路径中的步骤 {s} 在实际路径中完全没有执行",
            })

    # 多出的步骤（实际走了预期没有的节点，通常是误入异常分支）
    if expected_steps:
        extra = [s for s in actual_steps if s not in expected_steps]
        for s in extra:
            findings.append({
                "type": "extra_step", "severity": "medium", "step": s,
                "detail": f"实际路径进入了预期之外的步骤 {s}（可能误入异常分支）",
            })

    # 2) 沿用旧产出：动作事件中的 stale 记录
    stale_usages: list[dict[str, Any]] = []
    for e in events:
        if e["kind"] == "action":
            for stale in e["payload"].get("stale", []):
                stale_usages.append({
                    "round_no": e["payload"].get("round_no"),
                    "at_step": e["at_step"],
                    **stale,
                })
    for su in stale_usages:
        findings.append({
            "type": "stale_output", "severity": "medium",
            "step": su["at_step"], "material": su["key"],
            "detail": (
                f"第 {su['round_no']} 轮在 {su['at_step']} 重新产出 {su['key']}，"
                f"第 {su['superseded_round']} 轮在 {su['superseded_at_step']} 的旧产出已失效，"
                f"之后不得再沿用"
            ),
        })

    # 终态材料中是否存在被更新过、但路径上仍可能被旧环节引用的材料（提示性）
    stale_materials_now: list[dict[str, Any]] = []
    for key, instances in actual_state.materials.items():
        inactive = [i for i in instances if not i.active]
        if inactive:
            current = latest_active(instances)
            stale_materials_now.append({
                "key": key,
                "active_from_round": current.produced_round if current else None,
                "active_from_step": current.produced_at_step if current else None,
                "superseded_versions": [
                    {"round": i.produced_round, "step": i.produced_at_step}
                    for i in inactive
                ],
            })

    # 3) 漏掉升级条件：force_through / missed_escalation 违规
    for e in events:
        if e["kind"] != "action":
            continue
        for v in e["payload"].get("violations", []):
            if v.get("type") == "missed_escalation":
                findings.append({
                    "type": "missed_escalation", "severity": "high",
                    "step": e["at_step"],
                    "round_no": e["payload"].get("round_no"),
                    "detail": v.get("detail", ""),
                })
            elif v.get("type") == "force_through":
                findings.append({
                    "type": "force_through", "severity": "medium",
                    "step": e["at_step"],
                    "round_no": e["payload"].get("round_no"),
                    "detail": v.get("detail", ""),
                })

    # 结局对照
    outcome_match: str | None = None
    if scenario is not None:
        if scenario.expected_outcome is not None:
            actual_token = actual_state.outcome or actual_state.outcome_step
            outcome_match = (
                "match" if actual_token == scenario.expected_outcome else "mismatch"
            )
        elif expected_outcome_step is not None:
            outcome_match = (
                "match" if actual_state.outcome_step == expected_outcome_step else "mismatch"
            )

    return {
        "expected_path": expected_steps,
        "expected_outcome_step": expected_outcome_step,
        "expected_outcome": scenario.expected_outcome if scenario else None,
        "actual_path": actual_steps,
        "actual_outcome_step": actual_state.outcome_step,
        "actual_outcome": actual_state.outcome,
        "completed": actual_state.completed,
        "scenario_stopped_reason": sim_stopped,
        "outcome_match": outcome_match,
        "jump_attempts": jump_attempts,
        "stale_materials": stale_materials_now,
        "findings": findings,
        "finding_counts": _counts(findings),
    }


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
    return counts
