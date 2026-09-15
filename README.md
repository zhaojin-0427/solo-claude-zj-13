# 岗位流程情境演练 API

面向刚入职、主要靠零散交接笔记独立办事的毕业生：把岗位上的**步骤、前置材料、责任角色、
产出、时限以及正常/异常分支**用 FastAPI + Pydantic 接收，Python 编成有向图，SQLite
以**不可变版本**留存；学生提交情境参数后可从任一入口**逐轮演练**，系统每轮只给当下
可执行的动作和依据、记录实际路径；复盘时对照预期路径定位**跳步、沿用旧产出、漏掉升级
条件**；导师可锁定确认过的步骤，学生可提交候选补充规则，系统搜索**最少补充项**使指定
情境都能抵达明确结局。同一版本重复演练结果一致。

## 快速开始

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
# Swagger 文档： http://127.0.0.1:8000/docs
```

首次启动会自动建库（`data/drill.db`，可用环境变量 `DRILL_DB_PATH` 覆盖）并种入
"新员工入职流程"示例（含 4 个情境、3 条候选规则、3 个导师锁定步骤）。

```bash
python -m pytest tests -q      # 32 个测试
```

## 数据模型（`app/schemas.py`）

| 概念 | 字段要点 |
| --- | --- |
| `Step` 步骤（节点） | code、name、**role 责任角色**、**inputs 前置材料**、**outputs 产出**、**time_limit_minutes 时限**、entry、terminal、on_timeout、result_checks |
| `Branch` 分支（边） | source → target（`target=null` 表示就地终结）、trigger、condition、label、terminal_outcome |
| `ProcessSpec` 流程 | code、name、version、steps、branches |
| `Scenario` 情境 | 入口、params 情境参数、initial_materials 交接材料、rounds 脚本化轮次、expected_outcome |

触发器 `trigger` 固定优先级：**timeout（超时） > missing_any（材料缺失） >
mismatch（结果不符） > normal（正常） > default（兜底）**；同触发器还可用 `condition`
（Predicate：eq/ne/exists/in/gte…）区分具体情境。

## 图校验：五类结构问题都返回具体节点（`app/graph.py`）

`POST /processes/validate`（不落库）和 `POST /processes`（落库前强制校验）会报告：

| kind | 含义 |
| --- | --- |
| `missing_entry` | 缺少起点（没有 `entry=true` 的步骤） |
| `unreachable` | 从任一入口出发不可达的步骤 |
| `cycle` | 循环依赖：Tarjan 强连通分量中**没有任何边能离开**的环（可退出的返工环不算） |
| `no_role` | 无人承接（责任角色为空）的步骤 |
| `no_exit` | 分支无出口：反向传播后仍到不了 terminal 节点或就地终结边的节点 |
| `dangling_ref` / `duplicate_step` | 分支引用不存在的步骤 / 步骤编码重复（附加校验） |

校验不过的流程 **422 拒绝，不写任何版本**，响应体给出每个问题的节点编码与说明。

## SQLite 不可变版本（`app/db.py`）

* `versions` 表只 INSERT；内容 = 规范 JSON（去掉版本号后排序序列化）的 SHA-256。
  **同内容重复提交幂等返回同一版本号**；内容变化才产生新版本，旧版本 JSON 原样保留。
* 步骤/分支完整 JSON 随版本存储；导师锁定（`step_locks`）、情境、演练记录是旁路表，
  永不回改版本内容。

## 逐轮演练（`app/engine.py` + `/sessions` 路由）

1. `POST /sessions`：绑定情境，或只指定 `entry`（自由演练限入口节点）从任一入口开始。
2. `GET /sessions/{id}/prompt`：**只返回当下可执行动作**——责任角色、应产出、时限、
   前置材料到位情况、本节点的处置方向与依据；不透露后续步骤内部信息。
3. `POST /sessions/{id}/rounds`：提交本轮产出、耗时、实际承接角色：
   * 角色不符 → 直接拦截（`blocked`，状态不推进，事件仍留痕）；
   * 前置材料缺失 → 本轮产出不入账，走 `missing_any` 异常边（如退回补齐）；
   * 累计耗时超时限 → 走 `timeout` 边；`result_checks` 不通过 → 走 `mismatch` 边；
   * 异常节点没有对应出口 → 标记"无出口停滞"（不算明确结局，供修复使用）；
   * `force_default=true` 可强行按默认方向继续，但记录 `force_through` /
     `missed_escalation` 违规，供复盘定位"漏升级"。
4. `POST /sessions/{id}/jump`：学生试图跳步只记录不执行，复盘列为 `jump_attempt`。
5. `GET /sessions/{id}/path`：实际路径（每轮的步骤/触发器/是否强推）与结局。

**材料是版本化实例**：同名材料再次产出会让旧实例失效（`active=false`），取值永远取
最新有效实例；每次失效都会在动作事件里留下 `stale` 记录，复盘据此定位"沿用旧产出"。

引擎是无时钟、无随机的纯函数；会话状态可随时丢弃内存缓存、仅靠 SQLite 事件流
（`replay`）逐轮重建，结果一致。`POST /determinism/check` 对同版本同情境重复仿真多次
并比对完全相等。

## 复盘（`app/review.py`）

`GET /sessions/{id}/review` 用情境脚本跑出**预期路径**，与实际路径对照：

* `jump_attempt`（高）：显式跳步尝试；
* `skipped_step`（高）：预期步骤在实际路径中完全缺失（被异常重路由整段越过）；
* `extra_step`（中）：误入预期之外的异常节点；
* `stale_output`（中）：重新产出导致旧版本失效、不得再沿用；
* `missed_escalation`（高）/ `force_through`（中）：异常时强推默认方向、漏掉升级条件；
* 结局对照 `outcome_match`（match/mismatch）。

## 导师锁定与最少补充项（`app/repair.py`）

* `POST /locks/{proc}/versions/{v}/steps/{step}`：导师锁定确认过的步骤；重复锁定
  409 且不覆盖原导师痕迹。对锁定步骤提交 `set_role`/`mark_entry`/`fix_terminal`
  候选会 409；`add_branch` 只允许补异常出口、不允许改正常主线。
* `POST /candidates/...`：提交候选规则（加边/定角色/设入口/补终态）。
* `POST /repair/minimal`：对每条候选做副本补丁（原版本不动），按组合大小、再按
  候选编码字典序枚举（结果确定），找出使**所有指定情境都抵达明确结局且满足各自
  expected_outcome** 的最少候选集；`POST /repair/evaluate` 评估给定集合。

### 示例：S4 账号开通超时

原流程 `open_account` 的 timeout 边带条件 `it_supervisor_assigned==true`，S4 情境中
主管未介入 → 运行时无出口停滞。候选 C1（超时→IT主管跟进）、C2（跟进后→设备发放）、
C3（超时即暂缓入职）。C3 虽是单条边但结局是"暂缓"，不满足 S4 预期的"入职完成"，
最少集为 **{C1, C2}**：

```text
open_account -> it_supervisor_followup -> issue_equipment -> mentor_briefing => 入职完成
```

## 目录

```
app/
  schemas.py   Pydantic 入参模型（步骤/分支/情境/候选/请求体）
  graph.py     邻接表 + 五类结构校验（返回具体节点）
  engine.py    逐轮演练纯函数：分支优先级、材料版本化、事件回放、情境仿真
  review.py    预期路径对照与问题定位
  repair.py    候选补丁与最少补充项搜索
  db.py        SQLite：不可变版本、锁、情境、会话事件、候选、修复记录
  seed.py     新员工入职示例流程 / 4 情境 / 候选规则
  main.py      FastAPI 路由
tests/         32 个端到端与单元测试
```
