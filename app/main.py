"""岗位流程情境演练 API。

路由分组：
  /processes      流程版本提交（结构校验、不可变版本、幂等）
  /locks          导师锁定步骤
  /scenarios      情境参数与脚本
  /sessions       逐轮演练（提示/动作/跳步/路径/结局）
  /review         复盘：跳步、旧产出、漏升级
  /repair         候选补充规则与最少补充项搜索
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app import db, seed
from app.engine import (
    current_prompt,
    execute_round,
    init_state,
    record_jump_attempt,
    replay,
    simulate,
)
from app.graph import build_graph, validate_graph
from app.repair import apply_candidates, evaluate_repair, find_minimal
from app.review import review_session
from app.schemas import (
    ActionRequest,
    CandidateRule,
    JumpAttempt,
    LockRequest,
    ProcessSpec,
    RepairRequest,
    Scenario,
    SessionStart,
    SimulateRepair,
)


def init_app(db_path: str | None = None, seed_demo: bool = True) -> dict | None:
    """初始化数据库（可指定 SQLite 文件路径），并种入入职示例流程。"""
    db.init_db(db_path)
    with _state_lock:
        _states.clear()
    summary = None
    with db.get_conn(db_path) as conn:
        if seed_demo and not db.list_versions(conn, seed.PROCESS_CODE):
            summary = seed.seed(conn)
    return summary


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_app(getattr(app.state, "db_path", None))
    yield


app = FastAPI(
    title="岗位流程情境演练 API",
    version="1.0",
    description="新人交接场景下的岗位流程定义、逐轮演练、复盘与最少补充规则搜索。",
    lifespan=lifespan,
)


def conn_dep():
    with db.get_conn(getattr(app.state, "db_path", None)) as conn:
        yield conn


# 内存态：session_id -> (EngineState, 下一事件 seq)。状态可随时由事件流重放重建。
import threading

_state_lock = threading.Lock()
_states: dict[int, dict] = {}


def _ok(**kwargs):
    return {"ok": True, **kwargs}


def _load_version(conn, process_code: str, version: int | None):
    record = db.get_version(conn, process_code, version)
    if record is None:
        raise HTTPException(404, f"流程 {process_code} 的指定版本不存在")
    return record


def _lock_violation(rule, locks: set[str]) -> dict | None:
    """候选规则触碰导师锁定步骤时返回违规说明；否则 None。

    锁定步骤：不允许改角色/入口/终态属性；不允许新增 normal/default 边改变主线，
    但允许补充异常出口（timeout/missing/mismatch/escalate 等）。
    """
    if rule.source not in locks:
        return None
    if rule.kind in ("set_role", "mark_entry", "fix_terminal"):
        return {
            "message": f"步骤 {rule.source} 已被导师锁定，候选 {rule.code} 不得改动其责任/入口/终态属性",
            "candidate": rule.code, "locked_step": rule.source,
        }
    if rule.kind == "add_branch" and rule.trigger in ("normal", "default"):
        return {
            "message": (
                f"步骤 {rule.source} 已被导师锁定正常主线，候选 {rule.code} 不得新增 "
                f"{rule.trigger} 分支（仅允许补充异常出口）"
            ),
            "candidate": rule.code, "locked_step": rule.source,
        }
    return None


# ---------------------------------------------------------------------------
# 健康检查 / 概览
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    return {
        "service": "岗位流程情境演练 API",
        "docs": "/docs",
        "endpoints": [
            "POST /processes/validate", "POST /processes", "GET /processes/{code}/versions",
            "GET /processes/{code}/versions/{version}",
            "POST /locks/{code}/versions/{version}/steps/{step}",
            "GET /locks/{code}/versions/{version}",
            "POST /scenarios", "GET /scenarios", "GET /scenarios/{code}",
            "POST /sessions", "GET /sessions/{sid}/prompt",
            "POST /sessions/{sid}/rounds", "POST /sessions/{sid}/jump",
            "GET /sessions/{sid}/path", "GET /sessions/{sid}/review",
            "POST /simulate/expected-path",
            "POST /candidates/{code}/versions/{version}",
            "GET /candidates/{code}/versions/{version}",
            "POST /repair/minimal", "POST /repair/evaluate",
            "POST /determinism/check",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 流程定义：校验与不可变版本
# ---------------------------------------------------------------------------


@app.post("/processes/validate")
def validate_only(spec: ProcessSpec):
    """仅做结构校验，不落库。返回全部问题及具体节点。"""
    issues = validate_graph(spec)
    return _ok(valid=not issues, issue_count=len(issues),
               issues=[i.to_dict() for i in issues])


@app.post("/processes")
def submit_process(spec: ProcessSpec, conn=Depends(conn_dep)):
    """提交流程定义；结构校验通过才写入不可变版本，内容相同则幂等返回旧版本。"""
    issues = validate_graph(spec)
    if issues:
        raise HTTPException(
            422,
            detail={"message": "流程结构校验未通过，未写入任何版本",
                    "issues": [i.to_dict() for i in issues]},
        )
    db.upsert_process_meta(conn, spec)
    version, content_hash, created = db.save_version(conn, spec)
    conn.commit()
    return _ok(process_code=spec.code, version=version, content_hash=content_hash,
               created_new=created, immutable=True)


@app.get("/processes/{code}/versions")
def versions(code: str, conn=Depends(conn_dep)):
    return _ok(process_code=code, versions=db.list_versions(conn, code))


@app.get("/processes/{code}/versions/{version}")
def get_one_version(code: str, version: int, conn=Depends(conn_dep)):
    record = _load_version(conn, code, version)
    spec = record["spec"]
    return _ok(
        process_code=code,
        version=record["version"],
        content_hash=record["content_hash"],
        created_at=record["created_at"],
        immutable=True,
        spec=spec.model_dump(mode="json"),
        graph_overview=_graph_overview(spec),
    )


def _graph_overview(spec: ProcessSpec) -> dict:
    from app.graph import materialize_edges

    build_graph(spec)
    return {
        "nodes": [
            {"code": s.code, "name": s.name, "role": s.role, "entry": s.entry,
             "terminal": s.terminal, "time_limit_minutes": s.time_limit_minutes,
             "on_timeout": s.on_timeout,
             "inputs": s.inputs, "outputs": s.outputs}
            for s in spec.steps
        ],
        "edges": [
            {"source": b.source, "target": b.target, "trigger": b.trigger,
             "has_condition": b.condition is not None, "label": b.label,
             "implicit": b.implicit}
            for b in materialize_edges(spec)
        ],
        "entry_nodes": [s.code for s in spec.steps if s.entry],
        "terminal_nodes": [s.code for s in spec.steps if s.terminal],
    }


# ---------------------------------------------------------------------------
# 导师锁定
# ---------------------------------------------------------------------------


@app.post("/locks/{code}/versions/{version}/steps/{step}")
def lock_step(code: str, version: int, step: str, body: LockRequest,
              conn=Depends(conn_dep)):
    record = _load_version(conn, code, version)
    if step not in record["spec"].step_map():
        raise HTTPException(404, f"步骤 {step} 不存在于该版本")
    acquired = db.lock_step(conn, code, version, step, body.mentor, body.note)
    conn.commit()
    if not acquired:
        existing = [l for l in db.list_locks(conn, code, version) if l["step_code"] == step][0]
        raise HTTPException(409, detail={
            "message": "该步骤已被导师锁定，不可重复锁定/覆盖",
            "existing": existing,
        })
    return _ok(locked_step=step, mentor=body.mentor, version=version)


@app.get("/locks/{code}/versions/{version}")
def list_locks(code: str, version: int, conn=Depends(conn_dep)):
    _load_version(conn, code, version)
    return _ok(locks=db.list_locks(conn, code, version))


# ---------------------------------------------------------------------------
# 情境
# ---------------------------------------------------------------------------


@app.post("/scenarios")
def upsert_scenario(scenario: Scenario, conn=Depends(conn_dep)):
    record = db.get_version(conn, scenario.process_code)
    if record is None:
        raise HTTPException(404, f"情境引用的流程 {scenario.process_code} 不存在")
    spec = record["spec"]
    if scenario.entry not in spec.step_map():
        raise HTTPException(422, f"情境入口 {scenario.entry} 不是该流程中的步骤")
    db.save_scenario(conn, scenario)
    conn.commit()
    return _ok(scenario_code=scenario.code, process_version=record["version"])


@app.get("/scenarios")
def list_scenarios(process_code: str | None = None, conn=Depends(conn_dep)):
    return _ok(scenarios=db.list_scenarios(conn, process_code))


@app.get("/scenarios/{code}")
def get_scenario(code: str, conn=Depends(conn_dep)):
    scenario = db.get_scenario(conn, code)
    if scenario is None:
        raise HTTPException(404, f"情境 {code} 不存在")
    return _ok(scenario=scenario.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 演练会话
# ---------------------------------------------------------------------------


@app.post("/sessions")
def start_session(body: SessionStart, conn=Depends(conn_dep)):
    """开始演练。可绑定情境，也可只给入口与参数，从任一入口逐步演练。"""
    record = _load_version(conn, body.process_code, body.version)
    spec = record["spec"]
    graph = build_graph(spec)

    params = dict(body.params)
    materials = dict(body.initial_materials)
    scenario_code = body.scenario_code
    entry = body.entry

    if body.scenario_code:
        scenario = db.get_scenario(conn, body.scenario_code)
        if scenario is None:
            raise HTTPException(404, f"情境 {body.scenario_code} 不存在")
        if scenario.process_code != body.process_code:
            raise HTTPException(422, "情境与流程不匹配")
        entry = scenario.entry
        params = {**scenario.params, **params}
        materials = {**scenario.initial_materials, **materials}
        scenario_code = scenario.code

    if not entry:
        entries = [s.code for s in spec.steps if s.entry]
        if len(entries) != 1:
            raise HTTPException(422, f"未指定入口；请在 {entries} 中选择一个")
        entry = entries[0]
    if entry not in spec.step_map():
        raise HTTPException(422, f"入口步骤 {entry} 不存在")
    step = spec.step_map()[entry]
    if not body.scenario_code and not step.entry:
        raise HTTPException(422,
                            f"{entry} 不是入口步骤；自由演练只允许从 entry=true 的节点开始")

    session_id = db.create_session(
        conn, body.process_code, record["version"], entry, body.actor,
        params, materials, scenario_code,
    )
    state = init_state(spec, graph, entry, params, materials)
    with _state_lock:
        _states[session_id] = {"state": state, "seq": 1}
        db.add_event(conn, session_id, seq=0, kind="start", at_step=entry, payload={
            "process_code": body.process_code,
            "version": record["version"],
            "scenario_code": scenario_code,
            "entry": entry,
            "params": params,
            "initial_materials": materials,
            "actor": body.actor,
        })
    conn.commit()
    return _ok(session_id=session_id, version=record["version"],
               content_hash=record["content_hash"], entry=entry,
               prompt=current_prompt(state))


def _restore(conn, session_id: int):
    row = db.get_session(conn, session_id)
    if row is None:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    with _state_lock:
        cached = _states.get(session_id)
        if cached is not None:
            return row, cached["state"], cached
    record = db.get_version(conn, row["process_code"], row["version"])
    spec = record["spec"]
    graph = build_graph(spec)
    events = db.get_events(conn, session_id)
    state = replay(spec, graph, events)
    with _state_lock:
        _states[session_id] = {"state": state, "seq": len(events)}
    return row, state, _states[session_id]


@app.get("/sessions/{session_id}/prompt")
def get_prompt(session_id: int, conn=Depends(conn_dep)):
    """每轮只给出当下可执行的动作和依据。"""
    _, state, _ = _restore(conn, session_id)
    return _ok(session_id=session_id, prompt=current_prompt(state))


@app.post("/sessions/{session_id}/rounds")
def submit_round(session_id: int, body: ActionRequest, conn=Depends(conn_dep)):
    """提交本轮动作；材料缺失/结果不符/超时按异常分支走，记录实际路径。"""
    row, state, cache = _restore(conn, session_id)
    result = execute_round(
        state,
        round_no=body.round_no,
        produce=body.produce,
        elapsed_minutes=body.elapsed_minutes,
        role=body.role,
        force_default=body.force_default,
    )
    if result.get("error"):
        raise HTTPException(409, result["detail"])
    with _state_lock:
        seq = cache["seq"]
        db.add_event(
            conn, session_id, seq=seq, kind="action",
            round_no=body.round_no, at_step=result.get("at_step"),
            payload={
                **{k: v for k, v in result.items() if k not in ("kind",)},
                "role": body.role,
                "elapsed_delta": body.elapsed_minutes,
                "produce_full": body.produce,
                "force_default": body.force_default,
            },
        )
        cache["seq"] = seq + 1
    conn.commit()
    return _ok(session_id=session_id, result=result,
               prompt=None if result.get("completed") else current_prompt(state))


@app.post("/sessions/{session_id}/jump")
def submit_jump(session_id: int, body: JumpAttempt, conn=Depends(conn_dep)):
    """学生试图跳步：被拦截并记录，复盘时作为 jump_attempt 呈现。"""
    _, state, cache = _restore(conn, session_id)
    if body.from_step != state.current:
        raise HTTPException(422, f"当前节点是 {state.current}，并非 {body.from_step}")
    note = record_jump_attempt(state, body.round_no, body.from_step, body.to_step, body.reason)
    with _state_lock:
        seq = cache["seq"]
        db.add_event(conn, session_id, seq=seq, kind="jump",
                     round_no=body.round_no, at_step=body.from_step, payload=note)
        cache["seq"] = seq + 1
    conn.commit()
    return _ok(session_id=session_id, jump=note)


@app.get("/sessions/{session_id}/path")
def get_path(session_id: int, conn=Depends(conn_dep)):
    row, state, _ = _restore(conn, session_id)
    events = db.get_events(conn, session_id)
    return _ok(
        session_id=session_id,
        actual_path=state.path,
        completed=state.completed,
        outcome_step=state.outcome_step,
        outcome=state.outcome,
        jump_attempts=[e["payload"] for e in events if e["kind"] == "jump"],
        events=events,
    )


@app.get("/sessions/{session_id}/review")
def get_review(session_id: int, conn=Depends(conn_dep)):
    """复盘：对照情境预期路径，定位跳步、沿用旧产出与漏掉升级条件。"""
    row, _, _ = _restore(conn, session_id)
    record = _load_version(conn, row["process_code"], row["version"])
    spec = record["spec"]
    graph = build_graph(spec)
    events = db.get_events(conn, session_id)
    scenario = db.get_scenario(conn, row["scenario_code"]) if row["scenario_code"] else None
    report = review_session(spec, graph, events, scenario)
    return _ok(session_id=session_id, version=row["version"], review=report)


# ---------------------------------------------------------------------------
# 预期路径仿真 / 确定性自检
# ---------------------------------------------------------------------------


@app.post("/simulate/expected-path")
def expected_path(body: SimulateRepair, conn=Depends(conn_dep)):
    scenario = db.get_scenario(conn, body.scenario_code)
    if scenario is None:
        raise HTTPException(404, f"情境 {body.scenario_code} 不存在")
    record = _load_version(conn, scenario.process_code, None)
    spec = record["spec"]
    graph = build_graph(spec)
    sim = simulate(spec, graph, scenario)
    return _ok(scenario_code=scenario.code, simulation=sim.to_dict())


class DeterminismRequest(BaseModel):
    scenario_code: str
    repetitions: int = 3


@app.post("/determinism/check")
def determinism_check(body: DeterminismRequest, conn=Depends(conn_dep)):
    """同一版本对同一情境重复演练多次，结果必须完全一致。"""
    scenario = db.get_scenario(conn, body.scenario_code)
    if scenario is None:
        raise HTTPException(404, f"情境 {body.scenario_code} 不存在")
    record = _load_version(conn, scenario.process_code, None)
    spec = record["spec"]
    graph = build_graph(spec)
    runs = [simulate(spec, graph, scenario).to_dict() for _ in range(max(1, body.repetitions))]
    first = runs[0]
    consistent = all(r == first for r in runs[1:])
    return _ok(version=record["version"], content_hash=record["content_hash"],
               repetitions=len(runs), consistent=consistent, representative=first)


# ---------------------------------------------------------------------------
# 候选补充规则
# ---------------------------------------------------------------------------


@app.post("/candidates/{code}/versions/{version}")
def submit_candidate(code: str, version: int, rule: CandidateRule,
                     conn=Depends(conn_dep)):
    record = _load_version(conn, code, version)
    spec = record["spec"]
    locks = db.get_locks(conn, code, version)

    # 校验候选本身可应用；触及导师锁定步骤的改动按统一规则拒绝
    if rule.source not in spec.step_map():
        raise HTTPException(422, f"候选源步骤 {rule.source} 不存在")
    violation = _lock_violation(rule, locks)
    if violation is not None:
        raise HTTPException(409, detail=violation)
    try:
        apply_candidates(spec, [rule])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.save_candidate(conn, code, version, rule.code, rule.model_dump(mode="json"))
    conn.commit()
    return _ok(candidate_code=rule.code, version=version)


@app.get("/candidates/{code}/versions/{version}")
def list_candidate(code: str, version: int, conn=Depends(conn_dep)):
    _load_version(conn, code, version)
    return _ok(candidates=db.list_candidates(conn, code, version))


@app.post("/repair/evaluate")
def repair_evaluate(body: RepairRequest, conn=Depends(conn_dep)):
    record = _load_version(conn, body.process_code, body.version)
    spec = record["spec"]
    rules, scenarios = _resolve_repair_inputs(conn, body, spec, record["version"])
    result = evaluate_repair(spec, rules, scenarios)
    return _ok(version=record["version"], **result)


@app.post("/repair/minimal")
def repair_minimal(body: RepairRequest, conn=Depends(conn_dep)):
    """搜索最少补充项，使指定情境都能抵达明确结局（且满足各情境预期结局）。"""
    record = _load_version(conn, body.process_code, body.version)
    spec = record["spec"]
    rules, scenarios = _resolve_repair_inputs(conn, body, spec, record["version"])

    if not body.allow_locked:
        locks = db.get_locks(conn, body.process_code, record["version"])
        for rule in rules:
            violation = _lock_violation(rule, locks)
            if violation is not None:
                raise HTTPException(409, detail=violation)

    result = find_minimal(spec, rules, scenarios)
    db.save_repair_run(
        conn, body.process_code, record["version"],
        [s.code for s in scenarios], result.get("minimal", []), result["feasible"],
    )
    conn.commit()
    return _ok(version=record["version"],
               scenario_codes=[s.code for s in scenarios], **result)


def _resolve_repair_inputs(conn, body: RepairRequest, spec, version):
    rules = body.candidates
    scenario_codes = body.scenario_codes
    scenarios = []
    for code in scenario_codes:
        scenario = db.get_scenario(conn, code)
        if scenario is None:
            raise HTTPException(404, f"情境 {code} 不存在")
        if scenario.process_code != body.process_code:
            raise HTTPException(422, f"情境 {code} 不属于流程 {body.process_code}")
        scenarios.append(scenario)
    return rules, scenarios
