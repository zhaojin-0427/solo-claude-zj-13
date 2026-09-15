"""最少补充项搜索、导师锁定保护与版本不可变。"""
PROCESS = "new_hire_onboarding"


def test_repair_minimal_for_s4(client):
    r = client.post("/repair/minimal", json={
        "process_code": PROCESS,
        "scenario_codes": ["S4_account_timeout_broken"],
        "candidates": [
            {"code": "C1_timeout_to_it_supervisor", "kind": "add_branch",
             "source": "open_account", "target": "it_supervisor_followup",
             "trigger": "timeout", "label": "超时转IT主管"},
            {"code": "C2_followup_to_equipment", "kind": "add_branch",
             "source": "it_supervisor_followup", "target": "issue_equipment",
             "trigger": "normal", "label": "跟进后去发放"},
            {"code": "C3_timeout_end_contract_hold", "kind": "add_branch",
             "source": "open_account", "target": None, "trigger": "timeout",
             "terminal_outcome": "账号开通超时，入职暂缓并升级",
             "label": "超时暂缓"},
        ],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    # 单条 C3 只到暂缓结局，不满足预期“入职完成”；最少必须 C1+C2
    assert set(data["minimal"]) == {"C1_timeout_to_it_supervisor", "C2_followup_to_equipment"}
    assert data["minimal_size"] == 2
    # 修复后该情境仿真能到 mentor_briefing
    detail = next(d for d in data["details"] if d["scenario_code"] == "S4_account_timeout_broken")
    assert detail["ok"] is True
    assert detail["outcome_step"] == "mentor_briefing"


def test_repair_evaluate_no_candidates_infeasible(client):
    r = client.post("/repair/evaluate", json={
        "process_code": PROCESS,
        "scenario_codes": ["S4_account_timeout_broken"],
        "candidates": [{
            "code": "X", "kind": "add_branch", "source": "open_account",
            "target": "it_supervisor_followup", "trigger": "timeout",
        }],
    })
    # 只有 C1 没有 C2：跟进后 S4 参数下原条件边不命中，无出口
    assert r.json()["feasible"] is False


def test_candidates_persisted_from_seed(client):
    r = client.get(f"/candidates/{PROCESS}/versions/1")
    codes = {c["code"] for c in r.json()["candidates"]}
    assert {"C1_timeout_to_it_supervisor", "C2_followup_to_equipment",
            "C3_timeout_end_contract_hold"} <= codes


def test_locked_step_cannot_change_role(client):
    # seed 已锁定 file_info
    r = client.post(f"/candidates/{PROCESS}/versions/1", json={
        "code": "R1", "kind": "set_role", "source": "file_info", "role": "外包"})
    assert r.status_code == 409
    # 非锁定步骤可以
    r = client.post(f"/candidates/{PROCESS}/versions/1", json={
        "code": "R2", "kind": "set_role", "source": "it_supervisor_followup", "role": "IT 主管"})
    assert r.status_code == 200


def test_locked_step_rejects_new_normal_edge(client):
    r = client.post(f"/candidates/{PROCESS}/versions/1", json={
        "code": "R3", "kind": "add_branch", "source": "sign_contract",
        "target": "mentor_briefing", "trigger": "normal", "label": "跳过设备"})
    assert r.status_code == 409
    # 异常出口允许补充
    r = client.post(f"/candidates/{PROCESS}/versions/1", json={
        "code": "R4", "kind": "add_branch", "source": "sign_contract",
        "target": "it_supervisor_followup", "trigger": "timeout", "label": "超时IT跟进"})
    assert r.status_code == 200


def test_lock_is_idempotent_conflict(client):
    body = {"mentor": "李导师", "note": "再确认"}
    r = client.post(f"/locks/{PROCESS}/versions/1/steps/file_info", json=body)
    assert r.status_code == 409
    locks = client.get(f"/locks/{PROCESS}/versions/1").json()["locks"]
    mentors = {l["step_code"]: l["mentor"] for l in locks}
    assert mentors["file_info"] == "导师·王经理"  # 原确认不被覆盖


def test_version_immutable_and_idempotent(client):
    from app.seed import build_spec
    spec = build_spec().model_dump(mode="json")
    r1 = client.post("/processes", json=spec)
    assert r1.status_code == 200
    assert r1.json()["created_new"] is False  # 相同内容幂等
    assert r1.json()["version"] == 1
    versions = client.get(f"/processes/{PROCESS}/versions").json()["versions"]
    assert len(versions) == 1


def test_changed_content_creates_new_version(client):
    from app.seed import build_spec
    spec = build_spec()
    spec2 = spec.model_copy(deep=True)
    spec2.steps[0].note = "更新了接待口径"
    spec2.version = 1
    r = client.post("/processes", json=spec2.model_dump(mode="json"))
    assert r.json()["created_new"] is True
    assert r.json()["version"] == 2
    # v1 内容不受影响
    v1 = client.get(f"/processes/{PROCESS}/versions/1").json()
    assert v1["spec"]["steps"][0]["note"] != "更新了接待口径"


def test_invalid_spec_rejected_with_concrete_nodes(client):
    bad = {
        "code": "broken_flow", "name": "坏流程", "version": 1,
        "steps": [
            {"code": "a", "name": "A", "role": "r", "entry": True},
            {"code": "b", "name": "B", "role": None},
        ],
        "branches": [
            {"source": "a", "target": "b", "trigger": "normal"},
            {"source": "b", "target": "ghost", "trigger": "normal"},
        ],
    }
    r = client.post("/processes", json=bad)
    assert r.status_code == 422
    kinds = {i["kind"] for i in r.json()["detail"]["issues"]}
    assert {"no_role", "dangling_ref", "no_exit"} <= kinds
    # 未落库
    assert client.get("/processes/broken_flow/versions").json()["versions"] == []
