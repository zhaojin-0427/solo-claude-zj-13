"""复盘：对照预期路径，定位跳步、沿用旧产出、漏掉升级条件。

旧产出（stale_output）的语义：
  引擎在每个被接受的动作注册产出前，快照该步骤本轮实际消费了哪些前置材料
  及其当时所属实例（state.consumptions）。只有当
    1) 该实例后来被同键的新产出取代（inactive），且
    2) 消费它的步骤在此之后没有再消费该键的更新实例（没有重跑“愈合”），
  才判定为“沿用旧产出”。单纯重新产出一份材料但无人消费旧版本，不构成问题
  （例如设备调拨后重新发放，旧发放记录从未被下游使用）。
"""
from __future__ import annotations

from typing import Any

from app.engine import latest_active, replay, simulate
from app.graph import Graph
from app.schemas import ProcessSpec, Scenario


def _event_path(events: list[dict[str, Any]]) -> list[str]:
    return [e["at_step"] for e in events
            if e["kind"] == "action" and not e["payload"].get("blocked")]


def _stale_consumptions(actual_state) -> list[dict[str, Any]]:
    """依据消费快照与材料实例版本，找出真正沿用了旧产出的消费点。"""
    # 每个材料键的全部实例（按产出轮次有序）；active 为最新有效版本
    instances_by_key: dict[str, list] = {}
    for key, instances in actual_state.materials.items():
        instances_by_key[key] = instances

    # (step, material) 在所有消费中用到的最大产出轮次（重跑愈合依据）
    latest_consumed_round: dict[tuple[str, str], int] = {}
    for c in actual_state.consumptions:
        k = (c["step"], c["material"])
        latest_consumed_round[k] = max(
            latest_consumed_round.get(k, -1), c["produced_round"]
        )

    stale: list[dict[str, Any]] = []
    for c in actual_state.consumptions:
        key = c["material"]
        inst_round = c["produced_round"]
        inst = next(
            (i for i in instances_by_key.get(key, [])
             if i.produced_round == inst_round and i.produced_at_step == c["produced_at_step"]),
            None,
        )
        if inst is None or inst.active:
            continue  # 消费的实例至今仍有效，不是旧产出
        if latest_consumed_round[(c["step"], key)] > inst_round:
            continue  # 该步骤后来重跑并消费了更新版本，旧消费已被愈合
        current = latest_active(instances_by_key[key])
        stale.append({
            "type": "stale_output",
            "severity": "high",
            "step": c["step"],
            "round_no": c["round_no"],
            "material": key,
            "detail": (
                f"第 {c['round_no']} 轮在 {c['step']} 使用的 {key} 是第 "
                f"{inst_round} 轮（{inst.produced_at_step}）的旧产出；该材料已被"
                f"{('第 ' + str(current.produced_round) + ' 轮 ' + current.produced_at_step) if current else '新版本'}"
                f"取代，而 {c['step']} 没有基于新版本重跑，属于沿用旧产出"
            ),
        })
    return stale


def review_session(
    spec: ProcessSpec,
    graph: Graph,
    events: list[dict[str, Any]],
    scenario: Scenario | None = None,
) -> dict[str, Any]:
    """生成复盘报告。有情境时对照情境脚本得到预期路径。"""
    actual_steps = _event_path(events)
    actual_state = replay(spec, graph, events)

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

    # 隐式跳步：预期路径中完全没有执行到的步骤（异常重路由整段越过）
    if expected_steps:
        skipped = [s for s in expected_steps[:-1] if s not in set(actual_steps)]
        for s in skipped:
            findings.append({
                "type": "skipped_step", "severity": "high", "step": s,
                "detail": f"预期路径中的步骤 {s} 在实际路径中完全没有执行",
            })
        extra = [s for s in actual_steps if s not in set(expected_steps)]
        for s in extra:
            findings.append({
                "type": "extra_step", "severity": "medium", "step": s,
                "detail": f"实际路径进入了预期之外的步骤 {s}（可能误入异常分支）",
            })

    # 2) 沿用旧产出：只统计“旧实例被实际消费且未重跑愈合”的消费点
    stale_findings = _stale_consumptions(actual_state)
    findings.extend(stale_findings)

    # 材料版本现状（提示性：哪些键发生过版本更替；不等于旧产出问题）
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
                "consumed_by_steps": sorted({
                    c["step"] for c in actual_state.consumptions if c["material"] == key
                }),
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
            outcome_match = "match" if actual_token == scenario.expected_outcome else "mismatch"
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
