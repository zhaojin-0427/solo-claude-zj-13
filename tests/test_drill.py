"""逐轮演练 API：从入口逐步执行、异常路由、跳步拦截、旧产出、复盘。"""
PROCESS = "new_hire_onboarding"


def _start(client, scenario=None, entry=None):
    payload = {"process_code": PROCESS}
    if scenario:
        payload["scenario_code"] = scenario
    if entry:
        payload["entry"] = entry
    r = client.post("/sessions", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_seed_and_root(client):
    r = client.get("/")
    assert r.status_code == 200
    r = client.get(f"/processes/{PROCESS}/versions")
    assert r.json()["versions"][0]["version"] == 1


def test_s1_normal_full_path(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    rounds = [
        {"round_no": 1, "produce": {"id_verified": "证件核验通过"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "档案#001"}, "role": "人事专员"},
        {"round_no": 3, "produce": {"contract_signed": "合同#001"}, "role": "人事专员"},
        {"round_no": 4, "produce": {"it_account": "a@corp"}, "elapsed_minutes": 20, "role": "IT 支持"},
        {"round_no": 5, "produce": {"equipment_received": "笔记本#A100"}, "elapsed_minutes": 60, "role": "行政"},
        {"round_no": 6, "produce": {"onboarding_done": True}, "role": "导师"},
    ]
    for body in rounds:
        r = client.post(f"/sessions/{sid}/rounds", json=body)
        assert r.status_code == 200, r.text
        data = r.json()["result"]
        assert data["fired"] == "normal"
    last = rounds[-1]
    # 最后一轮已完成
    final = client.get(f"/sessions/{sid}/path").json()
    assert final["completed"] is True
    assert final["outcome"] == "入职完成"


def test_prompt_only_exposes_current_step(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "receive_new_hire"
    assert prompt["action"]["role"] == "前台"
    text = str(prompt)
    # 不能透露后续步骤的产出细节
    assert "file_info" not in text
    assert "employee_profile" not in text


def test_s2_mismatch_routes_to_escalation(client):
    sid = _start(client, scenario="S2_contract_mismatch")
    for body in [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"},
    ]:
        client.post(f"/sessions/{sid}/rounds", json=body)
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 3, "produce": {"contract_signed": "争议合同"}, "role": "人事专员"})
    data = r.json()["result"]
    assert data["fired"] == "mismatch"
    assert data["next_step"] == "hr_escalation"
    # 按异常分支继续升级
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 4, "produce": {"escalation_result": "改签"}, "role": "人事主管"})
    assert r.json()["result"]["completed"] is True


def test_free_drill_rejects_non_entry_node(client):
    """自由演练（不绑定情境）只能从 entry=true 的入口开始。"""
    sid = _start(client, scenario="S1_normal_onboarding")
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"})
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"})
    r = client.post("/sessions", json={"process_code": PROCESS, "entry": "file_info"})
    assert r.status_code == 422
    sid2 = _start(client, scenario="S1_normal_onboarding")
    client.post(f"/sessions/{sid2}/rounds", json={
        "round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"})
    prompt = client.get(f"/sessions/{sid2}/prompt").json()["prompt"]
    assert prompt["current_step"] == "file_info"
    assert prompt["inputs_status"][0]["available"] is True


def test_role_mismatch_blocks_round(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"id_verified": "x"}, "role": "无关人员"})
    data = r.json()["result"]
    assert data["blocked"] is True
    assert data["blocked_reason"] == "role_mismatch"
    # 状态未推进
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "receive_new_hire"


def test_jump_attempt_recorded_and_review_flags_it(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    r = client.post(f"/sessions/{sid}/jump", json={
        "round_no": 1, "from_step": "receive_new_hire", "to_step": "mentor_briefing",
        "reason": "想跳过中间步骤"})
    assert r.json()["jump"]["accepted"] is False
    review = client.get(f"/sessions/{sid}/review").json()["review"]
    assert review["jump_attempts"][0]["to_step"] == "mentor_briefing"
    assert any(f["type"] == "jump_attempt" for f in review["findings"])


def test_force_default_marks_missed_escalation(client):
    sid = _start(client, scenario="S2_contract_mismatch")
    for body in [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"},
    ]:
        client.post(f"/sessions/{sid}/rounds", json=body)
    # 结果不符却强推默认方向
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 3, "produce": {"contract_signed": "争议合同"},
        "role": "人事专员", "force_default": True})
    result = r.json()["result"]
    assert any(v["type"] == "missed_escalation" for v in result["violations"])
    review = client.get(f"/sessions/{sid}/review").json()["review"]
    assert any(f["type"] == "missed_escalation" for f in review["findings"])


def test_s3_timeout_and_benign_reissue(client):
    """设备发放超时走异常分支；调拨后重新发放。

    旧的 equipment_received 从未被下游消费（超时轮直接转 admin_followup），
    因此重新产出同名材料虽会产生版本更替（stale_materials 留痕），但复盘不应
    判定为 stale_output（没有沿用旧产出）。
    """
    sid = _start(client, scenario="S3_equipment_timeout")
    path_bodies = [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "初版"}, "role": "人事专员"},
        {"round_no": 3, "produce": {"contract_signed": "c"}, "role": "人事专员"},
        {"round_no": 4, "produce": {"it_account": "a@corp"}, "elapsed_minutes": 20, "role": "IT 支持"},
    ]
    for body in path_bodies:
        client.post(f"/sessions/{sid}/rounds", json=body)
    # 设备发放超时 -> admin_followup（本轮产出仍注册，异常分支照常）
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 5, "produce": {"equipment_received": "未发放"},
        "elapsed_minutes": 500, "role": "行政"})
    result = r.json()["result"]
    assert result["fired"] == "timeout"
    assert result["next_step"] == "admin_followup"
    # 调拨后重走发放：同名材料再次产出，事件中记录版本更替
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 6, "produce": {"equipment_rescheduled": "调拨#B7"}, "role": "行政主管"})
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 7, "produce": {"equipment_received": "笔记本#A101"},
        "elapsed_minutes": 120, "role": "行政"})
    result = r.json()["result"]
    assert result["stale"] and result["stale"][0]["key"] == "equipment_received"
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 8, "produce": {"onboarding_done": True}, "role": "导师"})
    review = client.get(f"/sessions/{sid}/review").json()["review"]
    # 没有任何步骤消费过旧的 equipment_received，不算沿用旧产出
    assert not any(f["type"] == "stale_output" for f in review["findings"])
    # 但材料版本更替仍在留痕信息中可见
    keys = {m["key"] for m in review["stale_materials"]}
    assert "equipment_received" in keys
    assert review["outcome_match"] == "match"
    assert review["scenario_stopped_reason"] is None


def test_stale_output_engine_semantics(client):
    """引擎级：只有旧实例被下游消费后又被取代、且未重跑愈合，才算 stale_output。"""
    from app.engine import execute_round, init_state
    from app.graph import build_graph
    from app.review import _stale_consumptions
    from app.schemas import Branch, ProcessSpec, ResultCheck, Step

    spec = ProcessSpec(code="stale_demo", name="旧产出演示", version=1, steps=[
        Step(code="a", name="制单", role="r", entry=True, outputs=["doc"],
             time_limit_minutes=100),
        Step(code="b", name="审核", role="r", inputs=["doc"], outputs=["checked"]),
        Step(code="c", name="归档", role="r", inputs=["doc", "checked"],
             outputs=["archived"], terminal=True,
             result_checks=[ResultCheck(key="archived", op="eq", value=True)]),
    ], branches=[
        Branch(source="a", target="b", trigger="normal"),
        Branch(source="b", target="c", trigger="normal"),
        Branch(source="c", target="a", trigger="mismatch", label="归档发现问题返工"),
        Branch(source="c", target=None, trigger="normal", terminal_outcome="done"),
    ])
    g = build_graph(spec)

    # 情形一：c 消费 doc v1（经 b），随后 a 重发出 v2，c 没有重跑 -> stale
    st = init_state(spec, g, entry="a")
    execute_round(st, 1, produce={"doc": "v1"}, role="r")
    execute_round(st, 2, produce={"checked": "ok-v1"}, role="r")
    res3 = execute_round(st, 3, produce={"archived": False}, role="r")  # 触发返工
    assert res3["next_step"] == "a"
    execute_round(st, 4, produce={"doc": "v2"}, role="r")  # a 重发，v1 失效
    stale = _stale_consumptions(st)
    assert {(f["step"], f["material"]) for f in stale} == {("b", "doc"), ("c", "doc")}

    # 情形二：b、c 基于 v2 重跑愈合 -> 无 stale
    execute_round(st, 5, produce={"checked": "ok-v2"}, role="r")
    execute_round(st, 6, produce={"archived": True}, role="r")
    assert _stale_consumptions(st) == []


def test_session_replay_restores_state_after_restore(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"})
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"})
    # 丢弃内存态，强制由事件流重放
    client.main._states.pop(sid)
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "sign_contract"
    # 继续演练可正常推进
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 3, "produce": {"contract_signed": "c"}, "role": "人事专员"})
    assert r.status_code == 200


def test_s4_original_process_blocks_without_exit(client):
    r = client.post("/simulate/expected-path", json={"scenario_code": "S4_account_timeout_broken"})
    sim = r.json()["simulation"]
    assert sim["completed"] is False
    assert "无出口" in (sim["stopped_reason"] or "")


def test_determinism_endpoint(client):
    r = client.post("/determinism/check", json={
        "scenario_code": "S1_normal_onboarding", "repetitions": 4})
    data = r.json()
    assert data["consistent"] is True
    assert data["repetitions"] == 4


def test_s1_review_is_clean(client):
    sid = _start(client, scenario="S1_normal_onboarding")
    for body in [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"},
        {"round_no": 3, "produce": {"contract_signed": "c"}, "role": "人事专员"},
        {"round_no": 4, "produce": {"it_account": "a@corp"}, "elapsed_minutes": 10, "role": "IT 支持"},
        {"round_no": 5, "produce": {"equipment_received": "nb"}, "elapsed_minutes": 30, "role": "行政"},
        {"round_no": 6, "produce": {"onboarding_done": True}, "role": "导师"},
    ]:
        client.post(f"/sessions/{sid}/rounds", json=body)
    review = client.get(f"/sessions/{sid}/review").json()["review"]
    assert review["findings"] == []
    assert review["expected_path"] == review["actual_path"]
    assert review["outcome_match"] == "match"


def test_missing_material_backward_branch(client):
    """缺必需产出时拦截在原步骤；补齐后，若前置材料缺失则走 missing_any 回退边。"""
    sid = _start(client, scenario="S1_normal_onboarding")
    # 接待步骤声明必需产出 id_verified：空 produce 被拦截，不能进入 normal
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {}, "role": "前台"})
    result = r.json()["result"]
    assert result["blocked"] is True
    assert result["blocked_reason"] == "missing_required_output"
    assert result["missing_outputs"] == ["id_verified"]
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "receive_new_hire"  # 状态未推进

    # 补齐产出后前进到 file_info
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"id_verified": "核验通过"}, "role": "前台"})
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "file_info"

    # 另造一个缺前置材料的会话：通过自由入口无法直达 file_info，
    # 用自定义情境：从 file_info 入口且无初始材料
    r = client.post("/scenarios", json={
        "code": "TMP_missing_input", "name": "缺前置材料",
        "process_code": PROCESS,
        "entry": "file_info", "params": {"doc_complete": True},
        "rounds": []})
    assert r.status_code == 200, r.text
    sid2 = _start(client, scenario="TMP_missing_input")
    r = client.post(f"/sessions/{sid2}/rounds", json={
        "round_no": 1, "produce": {"employee_profile": "无核验建档"}, "role": "人事专员"})
    result = r.json()["result"]
    assert result["fired"] == "missing_any"
    assert result["missing"] == ["id_verified"]
    assert result["next_step"] == "receive_new_hire"


def test_same_version_repeated_drill_identical(client):
    """同版本、同情境、同提交序列演练两次，结果路径与结局完全一致。"""
    def run_once():
        sid = _start(client, scenario="S2_contract_mismatch")
        collected = []
        for body in [
            {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
            {"round_no": 2, "produce": {"employee_profile": "p"}, "role": "人事专员"},
            {"round_no": 3, "produce": {"contract_signed": "争议"}, "role": "人事专员"},
            {"round_no": 4, "produce": {"escalation_result": "改签"}, "role": "人事主管"},
        ]:
            r = client.post(f"/sessions/{sid}/rounds", json=body).json()["result"]
            collected.append({"fired": r["fired"], "next": r["next_step"],
                              "completed": r["completed"], "outcome": r["outcome"]})
        path = client.get(f"/sessions/{sid}/path").json()
        return collected, path["actual_path"], path["outcome"]

    a = run_once()
    b = run_once()
    assert a == b
