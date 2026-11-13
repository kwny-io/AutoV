"""闭包计算：从种子沿着边向下走，逐节点判 disposition。

这段刻意与 service.py 分开：闭包只读图、只接受纯数据，回源与写库都在外面。
§6.5 要求闭包在同一个读快照里完成，而"回源在事务外"——两件事不在一个函数里
才守得住。

三件必须守住的事：

1. **缺边不可静默**。`EXPECTED_KINDS` 给出「走到某类节点时，若该类的边一条都
   没有，就是这个 kind 缺了」的规则。缺了要么 BLOCKING（阻断）、要么 DEGRADED
   （降保守），绝不允许当成"没有下游"直接少算。§9 的原话：先做闭包后做 gap
   检测会给出"更小的闭包，看起来更快更省钱，实际漏节点"。
2. **深度与规模有界**。超过就 FAILED，不自动分页——分页会给出不自洽的闭包。
3. **成环停在该分支**，不无限循环。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

MAX_DEPTH = 12
MAX_EDGES = 2 * 10 ** 5
MAX_NODES = 2 * 10 ** 4

# 哪些 kind 缺失是阻断的，哪些只降级。severity 由**缺哪种边**决定，
# 不是由「下游是哪个服务」决定——字幕缺边可以保守重算，资产绑定缺边则
# 无法判断资产换版的影响，这两种不能同码（§2.5）。
GAP_SEVERITY: dict[str, str] = {
    "PACK_USES_CLAIM": "BLOCKING",
    "SCRIPT_DERIVED_FROM_PACK": "BLOCKING",
    "SCENE_DERIVED_FROM_SCRIPT": "BLOCKING",
    "SHOT_DERIVED_FROM_SCENE": "BLOCKING",
    "SHOT_FIELD_DEPENDS": "BLOCKING",
    "ARTIFACT_FROM_SHOT": "BLOCKING",
    "ASSET_BINDING": "BLOCKING",
    "MEMORY_DEPENDS": "BLOCKING",
    "AUDIO_FROM_DIALOGUE": "DEGRADED",
    "SUBTITLE_FROM_AUDIO": "DEGRADED",
    "EDIT_FROM_ARTIFACT": "DEGRADED",
}

# QC / 连续性报告类：上游重算它只需要复检，不必重做（§4.2 倒数第二条）。
RECHECK_SERVICES = ("S40", "S46", "S47")

# 已交付的集只 RECHECK，永不 REGENERATE（§4.3）。
DELIVERED_SERVICE = "S44"


class GraphError(Exception):
    """图结构本身非法。调用方翻成 FAILED / GRAPH_CYCLE。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Ref:
    service_id: str
    object_id: str
    revision: int
    content_hash: str = ""

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.service_id, self.object_id, self.revision)

    def to_dict(self) -> dict[str, Any]:
        return {"service_id": self.service_id, "id": self.object_id,
                "revision": self.revision, "content_hash": self.content_hash}


@dataclass(frozen=True)
class Edge:
    edge_id: str
    edge_kind: str
    source: Ref
    target: Ref
    field_path: str | None


@dataclass
class Gap:
    gap_id: str
    expected_kind: str
    anchor: dict[str, Any]
    severity: str
    detail: str


@dataclass
class NodeVerdict:
    key: str          # 内部身份 (service,id,revision)，闭包与去重都用它
    node_id: str      # 对外 node_id：manifest 里那个名字，S07 拿去拼 ChangePlan
    disposition: str
    reason_code: str
    path: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClosureResult:
    nodes: dict[str, NodeVerdict]
    gaps: list[Gap]
    edges_scanned: int
    episode_ids: list[str]
    source_services: set[str]
    derived_adjustments: list[str]
    cycle_nodes: list[str]


def node_key(ref: Ref) -> str:
    return f"{ref.service_id}:{ref.object_id}@{ref.revision}"


def walk(
    *,
    seeds: Iterable[Ref],
    out_edges_of: dict[tuple[str, str, int], list[Edge]],
    changed_fields: dict[tuple[str, str, int], set[str]],
    fingerprint_changed: dict[tuple[str, str, int], str],
    episode_of: dict[tuple[str, str, int], str],
    expected_in: dict[tuple[str, str, int], list[dict]],
    present_in: dict[tuple[str, str, int], set[str]],
    name_of: dict[tuple[str, str, int], str],
) -> ClosureResult:
    """BFS 向下闭包。

    `out_edges_of` 由调用方从库里一次取好：键是 (service, id, revision)，值是
    出边列表。传 map 而不是传 connection，让这里可测且不碰 IO。

    `changed_fields`：这些字段的 value_hash 变了 —— 用于 DIRECT_FIELD_CHANGED。
    `fingerprint_changed`：这些对象换了 revision 且内容变了 —— 用于下游的
    UPSTREAM_REGENERATED，以及"上游其实没变"的 REUSE 判定。

    `expected_in`：节点自己声明的「这里本该有哪些入边」。
    `present_in`：该节点实际有的入边 kind 集合。
    `name_of`：manifest 里的 node_id。**对外结论必须用它**：S07 会把
    `regenerate_node_ids` 原样塞进 ChangePlan，那里认的是 manifest 的节点名，
    不是 S09 自己编的复合键。查不到就退回复合键——宁可让 S07 收到一个它不认识
    的 id 被校验拦下，也不要猜一个可能指错节点的名字。

    缺边判定只信这两者之差。不用「下游是哪个服务」去猜：猜多会把正常叶子
    报成阻断，猜少就是 §9 警告的「静默更小的闭包」。没有声明的节点不报缺口
    ——那不是"完整"，是"没被告知该有边"。

    闭包本身不判 REUSE：REUSE 要靠 §4.1 步骤 7 的向上反向校验，见 `verify_reuse`。
    """
    verdicts: dict[tuple[str, str, int], NodeVerdict] = {}
    gaps: list[Gap] = []
    episodes: set[str] = set()
    sources: set[str] = set()
    cycles: set[str] = set()

    queue: deque[tuple[Ref, int, list[str], bool]] = deque()
    depth_of: dict[tuple[str, str, int], int] = {}
    for seed in seeds:
        sources.add(seed.service_id)
        depth_of[seed.key] = 0
        queue.append((seed, 0, [], True))

    edges_scanned = 0

    while queue:
        ref, depth, path, from_seed = queue.popleft()

        if depth > MAX_DEPTH:
            raise GraphError("GRAPH_TOO_DEEP",
                             f"closure exceeded {MAX_DEPTH} levels at {node_key(ref)}")
        if len(verdicts) >= MAX_NODES:
            raise GraphError("CLOSURE_TOO_LARGE",
                             f"closure exceeded {MAX_NODES} nodes")
        if edges_scanned > MAX_EDGES:
            raise GraphError("CLOSURE_TOO_LARGE",
                             f"closure exceeded {MAX_EDGES} edges")

        changed = changed_fields.get(ref.key, set())
        out_edges = out_edges_of.get(ref.key, ())
        edges_scanned += len(out_edges)

        key = node_key(ref)
        name = name_of.get(ref.key, key)
        episode = episode_of.get(ref.key, "")
        if episode:
            episodes.add(episode)

        if ref.key not in verdicts:
            if changed:
                verdicts[ref.key] = NodeVerdict(
                    key=key, node_id=name, disposition="REGENERATE",
                    reason_code="DIRECT_FIELD_CHANGED", path=list(path),
                    evidence={"field_paths": sorted(changed),
                              "episode_id": episode,
                              "content_hash": ref.content_hash})
            elif ref.service_id in RECHECK_SERVICES:
                verdicts[ref.key] = NodeVerdict(
                    key=key, node_id=name, disposition="RECHECK",
                    reason_code="UPSTREAM_REGENERATED", path=list(path),
                    evidence={"episode_id": episode,
                              "content_hash": ref.content_hash})
            else:
                verdicts[ref.key] = NodeVerdict(
                    key=key, node_id=name, disposition="REGENERATE",
                    reason_code="UPSTREAM_REGENERATED", path=list(path),
                    evidence={"episode_id": episode,
                              "content_hash": ref.content_hash,
                              "upstream": fingerprint_changed.get(ref.key, "")})

        # 已交付的集只复检不下发（§4.3）。放在定级之后统一改写，避免分支里漏。
        if ref.service_id == DELIVERED_SERVICE:
            verdicts[ref.key].disposition = "RECHECK"

        # 缺边检测：节点声明了该有的入边，实际没有 → 缺口。与有没有出边无关。
        have = present_in.get(ref.key, set())
        for claim in expected_in.get(ref.key, ()):
            kind = claim.get("edge_kind")
            if not isinstance(kind, str) or kind in have:
                continue
            severity = claim.get("severity") or GAP_SEVERITY.get(kind, "BLOCKING")
            gaps.append(Gap(
                gap_id="", expected_kind=kind, anchor=ref.to_dict(),
                severity=severity,
                detail=f"{key} declares a {kind} input that has no edge; "
                       f"cannot prove what it depends on"))

        if not out_edges:
            continue

        for edge in out_edges:
            nxt = edge.target
            sources.add(nxt.service_id)
            if nxt.key in depth_of:
                # 已经走过：要么菱形回边（正常），要么成环（要记）。判据是
                # "它比我现在浅或一样浅"——那样再走一次就会回到原地。
                if depth_of[nxt.key] <= depth:
                    cycles.add(node_key(nxt))
                continue
            depth_of[nxt.key] = depth + 1
            queue.append((nxt, depth + 1, [*path, edge.edge_id], False))

    return ClosureResult(
        nodes=verdicts, gaps=gaps, edges_scanned=edges_scanned,
        episode_ids=sorted(episodes), source_services=sources,
        derived_adjustments=[], cycle_nodes=sorted(cycles))


def verify_reuse(
    result: ClosureResult,
    *,
    in_edges_of: dict[tuple[str, str, int], list[Edge]],
    changed_fields: dict[tuple[str, str, int], set[str]],
) -> None:
    """§4.1 步骤 7：REUSE 的唯一依据是"向上走一遍，没有经过任何已变字段"。

    只信 BFS"没到达"是不够的——BFS 到达与否取决于边走没走全，而边走全与否正是
    这次要证明的东西。所以对每个候选 REUSE 节点独立反向遍历一次，只要碰到一个
    已变字段就连带降级。反向遍历同样要有界：环上不能转圈。
    """
    for verdict in result.nodes.values():
        if verdict.disposition != "REUSE":
            continue
        if _reaches_change(verdict.key, in_edges_of, changed_fields):
            verdict.disposition = "REGENERATE"
            verdict.reason_code = "UPSTREAM_REGENERATED"


def _reaches_change(key: tuple[str, str, int], in_edges_of, changed_fields) -> bool:
    """从 node 反向找，是否能到达任何字段已变的上游。"""
    start = key
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        if changed_fields.get(current):
            return True
        for edge in in_edges_of.get(current, ()):
            if edge.source.key not in seen:
                seen.add(edge.source.key)
                stack.append(edge.source.key)
    return False


def mark_reusable(
    result: ClosureResult,
    *,
    seeds: dict[tuple[str, str, int], set[str]],
    unchanged_downstream: dict[tuple[str, str, int], str],
) -> None:
    """把"上游确实没变"的节点标成 REUSE，理由按输入类型区分。

    `unchanged_downstream`：服务层证明过的 (key -> reason_code)，例如
    AUDIO_FROM_DIALOGUE 的目标在 /dialogue 未变时可以 REUSE。
    这里只做落地，不重算证明——证明需要字段 hash，那是服务层拿的。
    """
    for key, reason in unchanged_downstream.items():
        verdict = result.nodes.get(key)
        if verdict is None or verdict.disposition != "REGENERATE":
            continue
        if key in seeds:
            continue
        verdict.disposition = "REUSE"
        verdict.reason_code = reason


def apply_conservative(
    result: ClosureResult,
    *,
    episode_shots: dict[str, list[Ref]],
    name_of: dict[tuple[str, str, int], str],
) -> None:
    """CONSERVATIVE 后处理，必须最后执行。

    §1.5：保守 = 并入整个 episode 的所有 shot 节点 + RECHECK 全升 REGENERATE。
    不做"部分保守"——部分保守没法证明不漏。
    """
    for episode, shots in episode_shots.items():
        for shot in shots:
            key = node_key(shot)
            if shot.key in result.nodes:
                continue
            result.nodes[shot.key] = NodeVerdict(
                key=key, node_id=name_of.get(shot.key, key),
                disposition="REGENERATE",
                reason_code="CONSERVATIVE_OVERAPPROX", path=[],
                evidence={"episode_id": episode})
    for verdict in result.nodes.values():
        if verdict.disposition == "RECHECK":
            verdict.disposition = "REGENERATE"
            verdict.reason_code = "CONSERVATIVE_UPGRADE"


def serialize_nodes(nodes) -> list[dict[str, Any]]:
    return [
        {"node_id": v.node_id, "disposition": v.disposition,
         "reason_code": v.reason_code, "path": v.path, "evidence": v.evidence}
        for v in sorted(nodes.values(), key=lambda x: x.node_id)]
