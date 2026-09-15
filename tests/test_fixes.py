"""五个语义修复的回归测试：
1. on_timeout 悬空引用在 validate / 入库时被拒绝，并指出源步骤；
2. 必需产出未提交不得进入 normal；
3. 非终态步骤正常完成但无 normal/default 出口，不得标记完成；
4. stale_output 只在旧实例被实际消费后被取代时产生；
5. /repair/minimal 在 allow_locked=false 时拒绝改动锁定步骤 normal 主线的候选。
"""
PROCESS = "new_hire_onboarding"


# --------------------------------------------------------------------------
# 1. on_timeout 悬空
# --------------------------------------------------------------------------


def test_validate_rejects_on_timeout_ghost_with_source_node(client):
    spec = {
        "code": "ghost_timeout", "name": "超时悬空", "version": 1,
        "steps": [
            {"code": "a", "name": "A", "role": "r", "entry": True,
             "outputs": ["x"], "time_limit_minutes": 10, "on_timeout": "ghost"},
            {"code": "b", "name": "B", "role": "r", "outputs": ["y"],
             "terminal": True},
        ],
        "branches": [
            {"source": "a", "target": "b", "trigger": "normal"},
            {"source": "b", "target": None, "trigger": "normal",
             "terminal_outcome": "done"},
        ],
    }
    r = client.post("/processes/validate", json=spec)
    data = r.json()
    assert data["valid"] is False
    dangling = [i for i in data["issues"] if i["kind"] == "dangling_ref"]
    assert dangling, data["issues"]
    # nodes 必须指出发起引用的步骤 a，而不是幽灵节点
    assert dangling[0]["nodes"] == ["a"]
    assert "ghost" in dangling[0]["detail"] and "on_timeout" in dangling[0]["detail"]


def test_submit_rejects_on_timeout_ghost_and_does_not_persist(client):
    spec = {
        "code": "ghost_timeout2", "name": "超时悬空2", "version": 1,
        "steps": [
            {"code": "a", "name": "A", "role": "r", "entry": True,
             "outputs": ["x"], "time_limit_minutes": 10, "on_timeout": "ghost"},
        ],
        "branches": [
            {"source": "a", "target": None, "trigger": "normal",
             "terminal_outcome": "done"},
        ],
    }
    r = client.post("/processes", json=spec)
    assert r.status_code == 422
    kinds = {i["kind"] for i in r.json()["detail"]["issues"]}
    assert "dangling_ref" in kinds
    assert client.get("/processes/ghost_timeout2/versions").json()["versions"] == []


# --------------------------------------------------------------------------
# 2. 必需产出
# --------------------------------------------------------------------------


def test_required_output_missing_blocks_normal(client):
    sid = client.post("/sessions", json={
        "process_code": PROCESS, "scenario_code": "S1_normal_onboarding"}
    ).json()["session_id"]
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {}, "role": "前台"})
    result = r.json()["result"]
    assert result["blocked"] is True
    assert result["blocked_reason"] == "missing_required_output"
    assert result["missing_outputs"] == ["id_verified"]
    # 未推进、未计时
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "receive_new_hire"
    # 补齐后正常前进
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"id_verified": "核验"}, "role": "前台"})
    assert r.json()["result"]["fired"] == "normal"
    assert r.json()["result"]["next_step"] == "file_info"


def test_extra_output_does_not_satisfy_required(client):
    sid = client.post("/sessions", json={
        "process_code": PROCESS, "scenario_code": "S1_normal_onboarding"}
    ).json()["session_id"]
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"something_else": 1}, "role": "前台"})
    result = r.json()["result"]
    assert result["blocked"] is True
    assert result["missing_outputs"] == ["id_verified"]


# --------------------------------------------------------------------------
# 3. 非终态正常无出口
# --------------------------------------------------------------------------


def test_non_terminal_normal_without_exit_not_completed(client):
    spec = {
        "code": "only_timeout_exit", "name": "仅超时出口", "version": 1,
        "steps": [
            {"code": "a", "name": "A", "role": "r", "entry": True,
             "outputs": ["x"], "time_limit_minutes": 10},
            {"code": "b", "name": "B", "role": "r", "outputs": ["y"],
             "terminal": True},
        ],
        "branches": [
            {"source": "a", "target": "b", "trigger": "timeout"},
            {"source": "b", "target": None, "trigger": "normal",
             "terminal_outcome": "done"},
        ],
    }
    # 入库：结构校验允许（a 的 timeout 能到终态 b，反向传播通过）
    assert client.post("/processes", json=spec).status_code == 200
    client.post("/scenarios", json={
        "code": "only_t", "name": "仅超时出口",
        "process_code": "only_timeout_exit", "entry": "a",
        "rounds": []})
    sid = client.post("/sessions", json={
        "process_code": "only_timeout_exit", "scenario_code": "only_t"}
    ).json()["session_id"]
    # 正常提交（未超时、产出齐全）：a 非终态且无 normal/default 边 -> 停滞，不完成
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"x": 1}, "elapsed_minutes": 1, "role": "r"})
    result = r.json()["result"]
    assert result["fired"] == "normal"
    assert result["completed"] is False
    assert result["no_exit"] is True
    assert "无出口" in result["outcome"]
    # 仍停留在 a，下一轮不会 409
    prompt = client.get(f"/sessions/{sid}/prompt").json()["prompt"]
    assert prompt["current_step"] == "a"
    # 超时后可以沿 timeout 出口到 b
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 2, "produce": {"x": 2}, "elapsed_minutes": 20, "role": "r"})
    result = r.json()["result"]
    assert result["fired"] == "timeout"
    assert result["next_step"] == "b"


def test_on_timeout_edge_reachable_at_runtime_when_valid(client):
    """合法的 on_timeout 声明在运行时确实作为 timeout 出口生效。"""
    spec = {
        "code": "valid_on_timeout", "name": "合法超时", "version": 1,
        "steps": [
            {"code": "a", "name": "A", "role": "r", "entry": True,
             "outputs": ["x"], "time_limit_minutes": 10, "on_timeout": "b"},
            {"code": "b", "name": "B", "role": "r", "outputs": ["y"],
             "terminal": True},
        ],
        "branches": [
            {"source": "a", "target": "b", "trigger": "normal"},
            {"source": "b", "target": None, "trigger": "normal",
             "terminal_outcome": "done"},
        ],
    }
    assert client.post("/processes", json=spec).status_code == 200
    client.post("/scenarios", json={
        "code": "vot", "name": "合法on_timeout",
        "process_code": "valid_on_timeout", "entry": "a",
        "rounds": []})
    sid = client.post("/sessions", json={
        "process_code": "valid_on_timeout", "scenario_code": "vot"}
    ).json()["session_id"]
    r = client.post(f"/sessions/{sid}/rounds", json={
        "round_no": 1, "produce": {"x": 1}, "elapsed_minutes": 11, "role": "r"})
    result = r.json()["result"]
    assert result["fired"] == "timeout"
    assert result["next_step"] == "b"
    assert result["edge"]["implicit"] is True


# --------------------------------------------------------------------------
# 4. stale_output 语义（消费才报）
# --------------------------------------------------------------------------


def test_s3_reissue_is_not_flagged_as_stale_output(client):
    sid = client.post("/sessions", json={
        "process_code": PROCESS, "scenario_code": "S3_equipment_timeout"}
    ).json()["session_id"]
    bodies = [
        {"round_no": 1, "produce": {"id_verified": "x"}, "role": "前台"},
        {"round_no": 2, "produce": {"employee_profile": "初版"}, "role": "人事专员"},
        {"round_no": 3, "produce": {"contract_signed": "c"}, "role": "人事专员"},
        {"round_no": 4, "produce": {"it_account": "a@corp"}, "elapsed_minutes": 20, "role": "IT 支持"},
        {"round_no": 5, "produce": {"equipment_received": "未发放"}, "elapsed_minutes": 500, "role": "行政"},
        {"round_no": 6, "produce": {"equipment_rescheduled": "调拨#B7"}, "role": "行政主管"},
        {"round_no": 7, "produce": {"equipment_received": "笔记本#A101"}, "elapsed_minutes": 120, "role": "行政"},
        {"round_no": 8, "produce": {"onboarding_done": True}, "role": "导师"},
    ]
    for body in bodies:
        r = client.post(f"/sessions/{sid}/rounds", json=body)
        assert r.status_code == 200, r.text
    review = client.get(f"/sessions/{sid}/review").json()["review"]
    assert not any(f["type"] == "stale_output" for f in review["findings"])
    assert review["outcome_match"] == "match"


# --------------------------------------------------------------------------
# 5. repair 锁定保护
# --------------------------------------------------------------------------


def test_repair_minimal_rejects_normal_edge_on_locked_step(client):
    r = client.post("/repair/minimal", json={
        "process_code": PROCESS,
        "scenario_codes": ["S1_normal_onboarding"],
        "allow_locked": False,
        "candidates": [
            {"code": "EVIL", "kind": "add_branch", "source": "sign_contract",
             "target": "mentor_briefing", "trigger": "normal",
             "label": "锁定步骤上改主线"},
        ],
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["locked_step"] == "sign_contract"
    assert detail["candidate"] == "EVIL"


def test_repair_minimal_rejects_set_role_on_locked_step(client):
    r = client.post("/repair/minimal", json={
        "process_code": PROCESS,
        "scenario_codes": ["S1_normal_onboarding"],
        "allow_locked": False,
        "candidates": [
            {"code": "ROLE", "kind": "set_role", "source": "file_info",
             "role": "外包"},
        ],
    })
    assert r.status_code == 409
    assert r.json()["detail"]["locked_step"] == "file_info"


def test_repair_minimal_allows_exception_edge_on_locked_step(client):
    """锁定步骤上补 timeout 异常出口不被拦截（仍需能让情境到结局）。"""
    r = client.post("/repair/minimal", json={
        "process_code": PROCESS,
        "scenario_codes": ["S1_normal_onboarding"],
        "allow_locked": False,
        "candidates": [
            {"code": "SAFE_ESC", "kind": "add_branch", "source": "sign_contract",
             "target": "hr_escalation", "trigger": "timeout",
             "label": "签合同超时也升级（不影响正常主线）"},
        ],
    })
    # 不应被锁定检查拦截（409）；该候选不改变 S1 正常路径，最少集为空即可行
    assert r.status_code == 200
    assert r.json()["minimal"] == []


def test_repair_minimal_allow_locked_overrides(client):
    r = client.post("/repair/minimal", json={
        "process_code": PROCESS,
        "scenario_codes": ["S1_normal_onboarding"],
        "allow_locked": True,
        "candidates": [
            {"code": "ROLE", "kind": "set_role", "source": "file_info",
             "role": "外包"},
        ],
    })
    assert r.status_code == 200
