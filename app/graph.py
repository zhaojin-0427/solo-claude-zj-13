"""有向图构建与结构校验。

五类结构性问题都会返回“具体节点编码”，便于交接者定位：
  missing_entry  缺少起点
  unreachable    步骤不可达
  cycle          循环依赖（无出口的强连通分量）
  no_role        无人承接（责任角色为空）
  no_exit        分支无出口（无论沿哪条边走都到不了明确结局）
另有 dangling_ref（分支或 on_timeout 指向不存在的步骤）与 duplicate_step 两类引用问题。

步骤自身的 on_timeout 声明被物化为一条隐式 timeout 边参与全部分析；
若同节点已存在显式 timeout 分支，则以显式分支为准（on_timeout 仅兜底）。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.schemas import Branch, ProcessSpec, Step


def materialize_edges(spec: ProcessSpec) -> list[Branch]:
    """返回流程的全部边：显式分支 + on_timeout 隐式超时边。"""
    edges = list(spec.branches)
    explicit_timeout_sources = {
        b.source for b in spec.branches if b.trigger == "timeout"
    }
    for step in spec.steps:
        if step.on_timeout and step.code not in explicit_timeout_sources:
            edges.append(
                Branch(
                    source=step.code,
                    target=step.on_timeout,
                    trigger="timeout",
                    label=f"{step.code} 声明的 on_timeout 兜底",
                    implicit=True,
                )
            )
    return edges


@dataclass
class Graph:
    spec: ProcessSpec
    steps: dict[str, Step]
    adjacency: dict[str, list[Branch]] = field(default_factory=lambda: defaultdict(list))

    def outgoing(self, code: str) -> list[Branch]:
        return self.adjacency.get(code, [])

    def targets(self, code: str) -> list[str]:
        return [b.target for b in self.adjacency.get(code, []) if b.target]


def build_graph(spec: ProcessSpec) -> Graph:
    """构建邻接表（含 on_timeout 隐式边）；不做语义校验（由 validate_graph 报告）。"""
    graph = Graph(spec=spec, steps=spec.step_map())
    for branch in materialize_edges(spec):
        graph.adjacency[branch.source].append(branch)
    # 声明顺序保持确定：同节点的边按 trigger、target 排序
    for edges in graph.adjacency.values():
        edges.sort(key=lambda b: (b.trigger, b.target or ""))
    return graph


@dataclass
class GraphIssue:
    kind: str
    nodes: list[str]
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "nodes": self.nodes, "detail": self.detail}


def validate_graph(spec: ProcessSpec) -> list[GraphIssue]:
    """对流程定义做完整结构校验，返回全部问题（不做短路）。"""
    issues: list[GraphIssue] = []
    codes = [s.code for s in spec.steps]
    code_set = set(codes)

    # 重复步骤编码
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    if dupes:
        issues.append(GraphIssue("duplicate_step", dupes, f"步骤编码重复: {dupes}"))

    steps = {c: s for c in code_set for s in spec.steps if s.code == c}
    all_edges = materialize_edges(spec)

    # 分支引用了不存在的源/目标（含 on_timeout 隐式边，节点指向发起该引用的步骤）
    dangling: dict[str, list[str]] = defaultdict(list)
    for b in all_edges:
        if b.source not in code_set:
            dangling[b.source].append(f"源 {b.source} 不存在")
        if b.target is not None and b.target not in code_set:
            if b.implicit:
                dangling[b.source].append(
                    f"步骤 {b.source} 的 on_timeout 指向未定义步骤 {b.target}"
                )
            else:
                dangling[b.target].append(
                    f"分支 {b.source} --{b.trigger}--> {b.target} 的目标不存在"
                )
    for node, details in sorted(dangling.items()):
        issues.append(GraphIssue("dangling_ref", [node], "；".join(details)))

    # 1) 缺少起点
    entries = sorted(c for c, s in steps.items() if s.entry)
    if not entries:
        issues.append(
            GraphIssue("missing_entry", codes[:1] or ["?"], "流程没有任何入口步骤（entry=true）")
        )

    # 3) 循环依赖：Tarjan 强连通分量，找出“没有任何边离开分量”的环
    adj: dict[str, set[str]] = defaultdict(set)
    for b in all_edges:
        if b.source in code_set and b.target in code_set:
            adj[b.source].add(b.target)

    index_counter = [0]
    stack: list[str] = []
    on_stack: dict[str, bool] = {}
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in sorted(adj[v]):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            sccs.append(sorted(comp))

    for c in sorted(code_set):
        if c not in indices:
            strongconnect(c)

    for comp in sccs:
        is_cycle = len(comp) > 1 or (len(comp) == 1 and comp[0] in adj.get(comp[0], set()))
        if not is_cycle:
            continue
        leaving = {
            t for c in comp for t in adj[c] if t not in comp
        }
        if not leaving:
            issues.append(
                GraphIssue(
                    "cycle",
                    comp,
                    f"步骤 {comp} 互相依赖形成闭环，且没有任何分支可以离开该环",
                )
            )

    # 邻接表（用于可达性，忽略悬空边）
    fwd: dict[str, set[str]] = defaultdict(set)
    has_explicit_end: set[str] = set()  # 有 target=None 就地终结边的节点
    for b in all_edges:
        if b.source in code_set:
            if b.target is None:
                has_explicit_end.add(b.source)
            elif b.target in code_set:
                fwd[b.source].add(b.target)

    # 2) 步骤不可达：从任一入口正向遍历
    reachable: set[str] = set()
    frontier = list(entries)
    while frontier:
        cur = frontier.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        frontier.extend(sorted(fwd[cur]))
    unreachable = sorted(code_set - reachable)
    if unreachable:
        issues.append(
            GraphIssue(
                "unreachable",
                unreachable,
                f"从入口 {entries} 出发无法到达: {unreachable}",
            )
        )

    # 4) 无人承接
    no_role = sorted(c for c, s in steps.items() if not (s.role and s.role.strip()))
    if no_role:
        issues.append(
            GraphIssue(
                "no_role",
                no_role,
                f"以下步骤没有指定责任角色: {no_role}",
            )
        )

    # 5) 分支无出口：反向传播“能否到达明确结局”。
    #    明确结局 = terminal 步骤自身，或声明了 target=None 就地终结的分支。
    can_end: set[str] = {c for c, s in steps.items() if s.terminal} | has_explicit_end
    rev: dict[str, set[str]] = defaultdict(set)
    for src, tgts in fwd.items():
        for t in tgts:
            rev[t].add(src)
    frontier = list(can_end)
    while frontier:
        cur = frontier.pop()
        for src in rev[cur]:
            if src not in can_end:
                can_end.add(src)
                frontier.append(src)
    no_exit = sorted(code_set - can_end)
    if no_exit:
        issues.append(
            GraphIssue(
                "no_exit",
                no_exit,
                f"以下节点的所有分支都无法到达明确结局（terminal 步骤或就地终结分支）: {no_exit}",
            )
        )

    return issues
