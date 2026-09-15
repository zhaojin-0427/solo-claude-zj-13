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


def test_s3_timeout_and_stale_output(client):
    sid = _start(client, scenario="S3_equipment_timeout")
    path_bodies = [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "初版"}, "role": "人事专员"},
        {"round_no": 3, "produce": {"contract_signed": "c"}, "role": "人事专员"},
        {"round_no": 4, "produce": {"it_account": "a@corp"}, "elapsed_minutes": 20, "role": "IT 支持"},
    ]
    for body in path_bodies:
        client.post(f"/sessions/{sid}/rounds", json=body)
    # 设备发放超时 -> admin_followup
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 5, "produce": {"equipment_received": "未发放"},
        "elapsed_minutes": 500, "role": "行政"})
    result = r.json()["result"]
    assert result["fired"] == "timeout"
    assert result["next_step"] == "admin_followup"
    # 调拨后重走发放：同名材料再次产出，旧实例 stale
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
    assert any(f["type"] == "stale_output" for f in review["findings"])
    assert review["outcome_match"] == "match"
    assert review["scenario_stopped_reason"] is None


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
    """缺材料时按 missing_any 回退边回到上游；补齐材料后才能继续。

    用一个临时提交流程：file_info 需要 id_verified；当学生从入口进来但接待轮
    未产出核验结果时，下一步动作依据会明确标出材料缺失。
    """
    sid = _start(client, scenario="S1_normal_onboarding")
    # 接待轮不产出 id_verified：正常边仍可走，但 file_info 的依据会暴露缺失
    client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {}, "role": "前台"})
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "file_info"
    assert prompt["inputs_status"][0]["available"] is False
    # 在 file_info 提交动作：缺材料 -> missing_any -> 回到 receive_new_hire
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"employee_profile": "无核验建档"}, "role": "人事专员"})
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
