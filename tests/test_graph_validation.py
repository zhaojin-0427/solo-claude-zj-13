"""结构校验：缺少起点、不可达、循环依赖、无人承接、分支无出口（返回具体节点）。"""
from app.graph import build_graph, validate_graph
from app.schemas import Branch, ProcessSpec, Step


def _spec(steps, branches=(), code="p"):
    return ProcessSpec(code=code, name="测试流程", version=1, steps=list(steps),
                       branches=list(branches))


def test_missing_entry_returns_node():
    spec = _spec([Step(code="a", name="A", terminal=True)])
    issues = validate_graph(spec)
    kinds = {i.kind for i in issues}
    assert "missing_entry" in kinds
    issue = next(i for i in issues if i.kind == "missing_entry")
    assert issue.nodes == ["a"]


def test_unreachable_node_identified():
    spec = _spec(
        [Step(code="a", name="A", entry=True, terminal=True),
         Step(code="b", name="B")],
        [Branch(source="a", target=None, trigger="normal", terminal_outcome="结束")],
    )
    issues = validate_graph(spec)
    unreachable = [i for i in issues if i.kind == "unreachable"]
    assert len(unreachable) == 1
    assert unreachable[0].nodes == ["b"]


def test_closed_cycle_reported_as_cycle_and_no_exit():
    spec = _spec(
        [Step(code="a", name="A", entry=True), Step(code="b", name="B")],
        [Branch(source="a", target="b", trigger="normal"),
         Branch(source="b", target="a", trigger="normal")],
    )
    issues = validate_graph(spec)
    kinds = {i.kind: i for i in issues}
    assert "cycle" in kinds
    assert set(kinds["cycle"].nodes) == {"a", "b"}
    assert "no_exit" in kinds


def test_cycle_with_escape_is_not_cycle_issue():
    spec = _spec(
        [Step(code="a", name="A", entry=True),
         Step(code="b", name="B"),
         Step(code="c", name="C", terminal=True)],
        [Branch(source="a", target="b", trigger="normal"),
         Branch(source="b", target="a", trigger="normal"),
         Branch(source="b", target="c", trigger="mismatch", label="异常升级")],
    )
    issues = validate_graph(spec)
    assert "cycle" not in {i.kind for i in issues}
    assert "no_exit" not in {i.kind for i in issues}


def test_no_role_returns_every_unassigned_node():
    spec = _spec([
        Step(code="a", name="A", entry=True, role="前台"),
        Step(code="b", name="B", role=None),
        Step(code="c", name="C", role="", terminal=True),
    ], [Branch(source="a", target="b", trigger="normal"),
        Branch(source="b", target="c", trigger="normal")])
    issues = validate_graph(spec)
    no_role = next(i for i in issues if i.kind == "no_role")
    assert set(no_role.nodes) == {"b", "c"}


def test_branch_without_exit_reports_node():
    spec = _spec(
        [Step(code="a", name="A", entry=True, role="r"),
         Step(code="b", name="B", role="r"),
         Step(code="c", name="C", role="r", terminal=True)],
        [Branch(source="a", target="b", trigger="normal"),
         Branch(source="a", target="c", trigger="normal")],
    )
    # b 没有任何出边，也不是终态
    issues = validate_graph(spec)
    no_exit = next(i for i in issues if i.kind == "no_exit")
    assert no_exit.nodes == ["b"]


def test_dangling_branch_target_reported():
    spec = _spec(
        [Step(code="a", name="A", entry=True, role="r", terminal=True)],
        [Branch(source="a", target="ghost", trigger="normal")],
    )
    issues = validate_graph(spec)
    assert "dangling_ref" in {i.kind for i in issues}


def test_valid_seed_spec_has_no_blocking_issues():
    from app.seed import build_spec
    issues = validate_graph(build_spec())
    assert issues == [], [i.to_dict() for i in issues]
    build_graph(build_spec())  # 邻接表可构建
