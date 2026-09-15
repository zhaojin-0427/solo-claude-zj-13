"""新员工入职示例流程：步骤、前置材料、责任角色、产出、时限、正常/异常分支。

流程节点：
  receive_new_hire  前台接待          （入口）
  file_info         人事建档
  sign_contract     签订劳动合同
  open_account      IT 开通账号        （30 分钟时限）
  issue_equipment   行政发放设备       （当日时限）
  mentor_briefing   导师岗位说明        （终态）
  hr_escalation     主管/人事升级处理   （异常终态）

其中 open_account 故意只声明部分 timeout 出口，用来演示“分支无出口”的最少补充搜索。
"""
from __future__ import annotations

import sqlite3

from app import db
from app.schemas import (
    Branch,
    CandidateRule,
    Predicate,
    ProcessSpec,
    ResultCheck,
    Scenario,
    ScriptedRound,
    Step,
)

PROCESS_CODE = "new_hire_onboarding"


def build_spec() -> ProcessSpec:
    steps = [
        Step(
            code="receive_new_hire",
            name="前台接待与身份核验",
            role="前台",
            outputs=["id_verified"],
            time_limit_minutes=60,
            entry=True,
            note="核对录用通知与身份证件",
        ),
        Step(
            code="file_info",
            name="人事建立员工档案",
            role="人事专员",
            inputs=["id_verified"],
            outputs=["employee_profile"],
            time_limit_minutes=120,
            result_checks=[
                ResultCheck(key="employee_profile", op="truthy", label="员工档案须建档成功"),
                ResultCheck(key="doc_complete", op="eq", value=True, label="入职材料须齐全"),
            ],
            note="材料不齐退回接待环节补齐",
        ),
        Step(
            code="sign_contract",
            name="签订劳动合同",
            role="人事专员",
            inputs=["employee_profile"],
            outputs=["contract_signed"],
            time_limit_minutes=180,
            result_checks=[
                ResultCheck(key="contract_approved", op="eq", value=True, label="合同条款须审批通过"),
            ],
        ),
        Step(
            code="open_account",
            name="IT 开通办公账号",
            role="IT 支持",
            inputs=["contract_signed"],
            outputs=["it_account"],
            time_limit_minutes=30,
        ),
        Step(
            code="issue_equipment",
            name="行政发放办公设备",
            role="行政",
            inputs=["it_account"],
            outputs=["equipment_received"],
            time_limit_minutes=480,
            result_checks=[
                ResultCheck(key="equipment_available", op="eq", value=True, label="设备库存须可用"),
            ],
        ),
        Step(
            code="mentor_briefing",
            name="导师岗位说明与交接",
            role="导师",
            inputs=["equipment_received"],
            outputs=["onboarding_done"],
            terminal=True,
        ),
        Step(
            code="hr_escalation",
            name="主管/人事升级处理",
            role="人事主管",
            inputs=[],
            outputs=["escalation_result"],
            note="合同争议等异常的升级处置终点",
        ),
        Step(
            code="it_supervisor_followup",
            name="IT 主管跟进处理",
            role="IT 主管",
            outputs=["it_followup_done"],
            note="账号开通异常的临时处置，处理完继续主流程",
        ),
    ]

    branches = [
        # 正常主线
        Branch(source="receive_new_hire", target="file_info", trigger="normal",
               label="身份核验通过"),
        Branch(source="file_info", target="sign_contract", trigger="normal",
               label="建档完成"),
        Branch(source="sign_contract", target="open_account", trigger="normal",
               label="合同签订完成"),
        Branch(source="open_account", target="issue_equipment", trigger="normal",
               label="账号已开通"),
        Branch(source="issue_equipment", target="mentor_briefing", trigger="normal",
               label="设备已发放"),
        Branch(source="mentor_briefing", target=None, trigger="normal",
               terminal_outcome="入职完成", label="完成岗位说明"),
        # 材料缺失：退回补齐
        Branch(source="file_info", target="receive_new_hire", trigger="missing_any",
               label="缺少核验结果，回前台补齐"),
        Branch(source="sign_contract", target="file_info", trigger="missing_any",
               label="缺少员工档案，回建档环节"),
        Branch(source="open_account", target="sign_contract", trigger="missing_any",
               label="缺少已签合同，回合同环节"),
        # 结果不符：合同争议升级到主管（明确结局）
        Branch(source="sign_contract", target="hr_escalation", trigger="mismatch",
               label="合同条款审批不符，升级人事主管"),
        Branch(source="hr_escalation", target=None, trigger="normal",
               terminal_outcome="异常升级处理结束", label="升级处置完成"),
        # 建档结果不符：补齐材料后回到接待
        Branch(source="file_info", target="receive_new_hire", trigger="mismatch",
               label="入职材料不齐，退回补齐"),
        # 设备不可用：行政跟进后继续
        Branch(source="issue_equipment", target="admin_followup_placeholder",
               trigger="mismatch", label="（演示悬空引用，正式种子不使用此行）"),
        # IT 超时：仅在“账号主管已介入”时走向跟进；其它情况无出口（留给补充规则修复）
        Branch(source="open_account", target="it_supervisor_followup", trigger="timeout",
               condition=Predicate(key="it_supervisor_assigned", op="eq", value=True),
               label="账号主管已介入，IT 主管跟进"),
        Branch(source="it_supervisor_followup", target="open_account", trigger="normal",
               condition=Predicate(key="it_supervisor_assigned", op="eq", value=True),
               label="主管介入完成，重走账号开通"),
    ]
    # 移除占位悬空边，改为真实的设备跟进节点边
    branches = [b for b in branches if b.target != "admin_followup_placeholder"]
    branches.extend([
        Branch(source="issue_equipment", target="admin_followup", trigger="mismatch",
               label="设备缺货，行政跟进调拨"),
        Branch(source="issue_equipment", target="admin_followup", trigger="timeout",
               label="发放超时，行政跟进"),
        Branch(source="admin_followup", target="issue_equipment", trigger="normal",
               label="调拨完成，重新发放"),
    ])
    steps.append(Step(
        code="admin_followup",
        name="行政设备调拨跟进",
        role="行政主管",
        outputs=["equipment_rescheduled"],
        note="设备缺货/超时时的临时跟进节点",
    ))

    spec = ProcessSpec(
        code=PROCESS_CODE,
        name="新员工入职流程",
        version=1,
        steps=steps,
        branches=branches,
        description="面向应届毕业生的新员工入职岗位流程，含正常与异常分支。",
    )
    return spec


# ---------------------------------------------------------------------------
# 情境
# ---------------------------------------------------------------------------


def scenario_normal() -> Scenario:
    return Scenario(
        code="S1_normal_onboarding",
        name="情境S1：材料齐全的顺利入职",
        process_code=PROCESS_CODE,
        entry="receive_new_hire",
        params={"doc_complete": True, "contract_approved": True,
                "equipment_available": True},
        initial_materials={},
        rounds=[
            ScriptedRound(at="receive_new_hire", role="前台",
                          produce={"id_verified": "证件核验通过"}),
            ScriptedRound(at="file_info", role="人事专员",
                          produce={"employee_profile": "档案#001"}),
            ScriptedRound(at="sign_contract", role="人事专员",
                          produce={"contract_signed": "合同#001"}),
            ScriptedRound(at="open_account", role="IT 支持", elapsed_minutes=20,
                          produce={"it_account": "zhang.san@corp"}),
            ScriptedRound(at="issue_equipment", role="行政", elapsed_minutes=60,
                          produce={"equipment_received": "笔记本#A100"}),
            ScriptedRound(at="mentor_briefing", role="导师",
                          produce={"onboarding_done": True}),
        ],
        expected_outcome="入职完成",
    )


def scenario_contract_dispute() -> Scenario:
    return Scenario(
        code="S2_contract_mismatch",
        name="情境S2：合同条款不符，升级人事主管",
        process_code=PROCESS_CODE,
        entry="receive_new_hire",
        params={"doc_complete": True, "contract_approved": False,
                "equipment_available": True},
        rounds=[
            ScriptedRound(at="receive_new_hire", role="前台",
                          produce={"id_verified": "证件核验通过"}),
            ScriptedRound(at="file_info", role="人事专员",
                          produce={"employee_profile": "档案#002"}),
            ScriptedRound(at="sign_contract", role="人事专员",
                          produce={"contract_signed": "合同#002（争议）"}),
            ScriptedRound(at="hr_escalation", role="人事主管",
                          produce={"escalation_result": "协商改签，另行约定"}),
        ],
        expected_outcome="异常升级处理结束",
    )


def scenario_equipment_timeout() -> Scenario:
    """设备发放超时 -> 行政跟进 -> 重走发放；第二版档案演示旧产出失效（stale）。"""
    return Scenario(
        code="S3_equipment_timeout",
        name="情境S3：设备发放超时后调拨补发",
        process_code=PROCESS_CODE,
        entry="receive_new_hire",
        params={"doc_complete": True, "contract_approved": True,
                "equipment_available": True},
        rounds=[
            ScriptedRound(at="receive_new_hire", role="前台",
                          produce={"id_verified": "证件核验通过"}),
            ScriptedRound(at="file_info", role="人事专员",
                          produce={"employee_profile": "档案#003-初版"}),
            ScriptedRound(at="sign_contract", role="人事专员",
                          produce={"contract_signed": "合同#003"}),
            ScriptedRound(at="open_account", role="IT 支持", elapsed_minutes=20,
                          produce={"it_account": "li.si@corp"}),
            ScriptedRound(at="issue_equipment", role="行政", elapsed_minutes=500,
                          produce={"equipment_received": "未发放"}),
            ScriptedRound(at="admin_followup", role="行政主管",
                          produce={"equipment_rescheduled": "调拨#B7"}),
            ScriptedRound(at="issue_equipment", role="行政", elapsed_minutes=120,
                          produce={"equipment_received": "笔记本#A101"}),
            ScriptedRound(at="mentor_briefing", role="导师",
                          produce={"onboarding_done": True}),
        ],
        expected_outcome="入职完成",
    )


def scenario_account_timeout_broken() -> Scenario:
    """账号开通超时且主管未介入：原流程 timeout 边带条件不成立，运行时无出口。

    要求最终仍“入职完成”，因此单条“超时即暂缓”的补充（C3）不满足，
    必须同时补上 超时→IT主管跟进（C1）与 跟进后→设备发放（C2）。
    """
    return Scenario(
        code="S4_account_timeout_broken",
        name="情境S4：账号开通超时且无人介入（流程缺陷情境）",
        process_code=PROCESS_CODE,
        entry="open_account",
        params={"it_supervisor_assigned": False, "equipment_available": True,
                "doc_complete": True, "contract_approved": True},
        initial_materials={
            "id_verified": "证件核验通过",
            "employee_profile": "档案#004",
            "contract_signed": "合同#004",
        },
        rounds=[
            ScriptedRound(at="open_account", role="IT 支持", elapsed_minutes=45,
                          produce={"it_account": None}),
            ScriptedRound(at="it_supervisor_followup", role="IT 主管",
                          produce={"it_followup_done": True,
                                   "it_account": "wang.wu@corp（主管手工开通）"}),
            ScriptedRound(at="issue_equipment", role="行政", elapsed_minutes=60,
                          produce={"equipment_received": "笔记本#A102"}),
            ScriptedRound(at="mentor_briefing", role="导师",
                          produce={"onboarding_done": True}),
        ],
        expected_outcome="入职完成",
        description="原流程在此情境运行时无 timeout 出口；需最少候选集修复。",
    )


# ---------------------------------------------------------------------------
# 候选补充规则（供 S4 最少补充搜索）
# ---------------------------------------------------------------------------


def candidates_for_account_timeout() -> list[CandidateRule]:
    return [
        CandidateRule(
            code="C1_timeout_to_it_supervisor",
            kind="add_branch",
            source="open_account",
            target="it_supervisor_followup",
            trigger="timeout",
            label="补充：账号超时统一转 IT 主管跟进",
        ),
        CandidateRule(
            code="C2_followup_to_equipment",
            kind="add_branch",
            source="it_supervisor_followup",
            target="issue_equipment",
            trigger="normal",
            label="补充：IT 主管处置后直接进入设备发放",
        ),
        CandidateRule(
            code="C3_timeout_end_contract_hold",
            kind="add_branch",
            source="open_account",
            target=None,
            trigger="timeout",
            terminal_outcome="账号开通超时，入职暂缓并升级",
            label="补充：超时直接收束为暂缓入职（独立结局）",
        ),
    ]


def seed(conn: sqlite3.Connection, reset: bool = False) -> dict:
    """写入示例流程、情境、候选；结构校验通过才允许落版本。返回摘要。"""
    spec = build_spec()
    from app.graph import validate_graph

    issues = validate_graph(spec)
    issue_dicts = [i.to_dict() for i in issues]
    if issues:
        return {"ok": False, "issues": issue_dicts}

    db.upsert_process_meta(conn, spec)
    version, content_hash, created = db.save_version(conn, spec)

    for scenario in (
        scenario_normal(),
        scenario_contract_dispute(),
        scenario_equipment_timeout(),
        scenario_account_timeout_broken(),
    ):
        db.save_scenario(conn, scenario)

    for rule in candidates_for_account_timeout():
        conn.execute(
            "INSERT OR IGNORE INTO candidates(process_code, version, code, content_json, created_at)"
            " VALUES(?,?,?,?,?)",
            (PROCESS_CODE, version, rule.code,
             rule.model_dump_json(), db._now()),
        )

    # 导师锁定前三个主线步骤（演示锁定后不可被候选规则改动）
    for step_code, mentor_note in (
        ("receive_new_hire", "接待口径已确认"),
        ("file_info", "建档材料清单已确认"),
        ("sign_contract", "合同模板已确认"),
    ):
        db.lock_step(conn, PROCESS_CODE, version, step_code, "导师·王经理", mentor_note)

    conn.commit()
    return {
        "ok": True,
        "process_code": PROCESS_CODE,
        "version": version,
        "content_hash": content_hash,
        "created_new": created,
        "scenarios": ["S1_normal_onboarding", "S2_contract_mismatch",
                      "S3_equipment_timeout", "S4_account_timeout_broken"],
        "candidate_codes": [c.code for c in candidates_for_account_timeout()],
    }
