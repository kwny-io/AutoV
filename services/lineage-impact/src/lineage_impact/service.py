"""S09 业务逻辑：依赖边、字段投影、影响闭包、缺口。

边界（§0）：S09 是「谁引用了谁」的唯一写者。它只持有 ref 与 hash，不持有事实
原文——对白、prompt、claim 文本、资产描述永远只有 `value_hash`。六个白名单标量
是唯一例外，且白名单在数据库里执行（`fp_whitelist_insert`）。

S09 不回答「这一集现在是什么」（S08），也不回答「这条内容是什么」（各所有者）。
`node_ref` 只记「这个 revision 在那份 manifest 里叫哪个 node_id」——这是把边的
(service, id, revision) 落回 manifest 节点名的索引，不是第二份 manifest。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from . import closure
from .closure import Edge, GraphError, Ref
from .schema import (
    EDGE_KINDS,
    WHITELIST,
    schema_sql,
    whitelist_insert_sql,
)

SOURCE = "s09.lineage-impact"
TOPIC = "drama.lineage-impact.v2"
EVENT_TYPE = "lineage.impact.computed"

ANALYSIS_TTL_SECONDS = 30 * 60
MAX_EDGES_PER_BATCH = 500
MAX_DEPTH_UP = 8

CONSUMERS = {
    "pack": "s09.edges.pack",
    "content": "s09.edges.content",
    "claim": "s09.edges.claim",
    "manifest": "s09.manifest",
    "memory": "s09.memory",
    "asset": "s09.edges.asset",
}

# 事件类型 → (consumer, 处理函数名, 是否推进水位, 水位源)
EVENT_ROUTES = {
    "context.pack.created": ("pack", "ingest_pack_created", True, "S18"),
    "snapshot.manifest.committed": ("manifest", "ingest_manifest_committed", True, "S08"),
    "memory.canon.committed": ("memory", "ingest_memory_committed", True, "S16"),
    "storyboard.shot.revision.created": ("content", "ingest_shot_revision", True, "S06"),
    "script.revision.created": ("content", "ingest_script_revision", True, "S04"),
    "asset.binding.changed": ("asset", "ingest_asset_binding", True, "S10"),
    "artifact.archived": ("content", "ingest_artifact_archived", True, "S28"),
    "alignment.completed": ("content", "ingest_alignment", True, "S37"),
}

# 闭包恒需要的源（§4.1 步骤 3），加上按路径出现的源。
ALWAYS_SOURCES = ("S06", "S08")

# 真正会向 S09 投递事件的源。只有它们"没消息"才意味着水位落后；
# 其余服务出现在图里但不产生 S09 消费的事件，不该把闭包拖成保守模式。
EVENT_SOURCES = frozenset(route[3] for route in EVENT_ROUTES.values())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _plus_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z")


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_hash(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def check_field_path(path) -> None:
    """RFC 6901 JSON Pointer。SQLite 表达不了正则，形态在代码里守。"""
    if path is None:
        return
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 256:
        raise ServiceError(400, "INVALID_FIELD_PATH", f"field_path {path!r} is not a JSON Pointer")
    if "//" in path or path.endswith("/"):
        raise ServiceError(400, "INVALID_FIELD_PATH", f"field_path {path!r} has an empty segment")
    body = path[1:].replace("~1", "").replace("~0", "")
    if "/" in body and "" in body.split("/"):
        raise ServiceError(400, "INVALID_FIELD_PATH", f"field_path {path!r} has an empty segment")


def parse_ref(raw, *, name: str) -> Ref:
    if not isinstance(raw, dict):
        raise ServiceError(422, "SCHEMA_INVALID", f"{name} must be an object")
    service_id = raw.get("service_id")
    object_id = raw.get("id")
    revision = raw.get("revision")
    content_hash = raw.get("content_hash", "")
    if not isinstance(service_id, str) or not service_id:
        raise ServiceError(422, "SCHEMA_INVALID", f"{name}.service_id is required")
    if not isinstance(object_id, str) or not object_id:
        raise ServiceError(422, "SCHEMA_INVALID", f"{name}.id is required")
    if not isinstance(revision, int) or revision < 1:
        raise ServiceError(422, "SCHEMA_INVALID", f"{name}.revision must be >= 1")
    if content_hash and not _is_hash(content_hash):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{name}.content_hash must be 64 lowercase hex chars")
    return Ref(service_id, object_id, revision, content_hash)


class LineageImpactService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema_sql())
        for row in WHITELIST:
            self.conn.execute(whitelist_insert_sql(), row)
        self.outbox = Outbox(conn)
        self.inbox = Inbox(conn)

    # ---- 内部小工具 ------------------------------------------------------------

    def _watermark(self, tenant_id: str, branch_id: str, source: str) -> int:
        """这个源在 S09 里已知的最新 sequence：已应用与观测到的头，取大者。

        写边用的 `valid_from` 要的是「这条边从哪一格开始生效」，那是源侧的
        进度，不是"S09 已经处理到哪一条"。只读 `applied_sequence` 会让一条
        在自己的事件里写下的边记成上一格——重放同一条事件时算出的
        `edge_id` 就变了（`edge_id` 含 `valid_from`），`ON CONFLICT DO
        NOTHING` 兜不住，同一条边落下两行。
        """
        row = self.conn.execute(
            "SELECT applied_sequence, source_head_sequence FROM projection_watermark "
            "WHERE tenant_id = ? AND branch_id = ? AND source_service = ?",
            (tenant_id, branch_id, source)).fetchone()
        return max(row[0], row[1]) if row else 0

    def _advance_watermark(self, tenant_id: str, branch_id: str, source: str,
                           sequence: int, event_id: str | None = None) -> None:
        """单调 CAS（§6.4）。乱序事件先到不能把水位往回拉。"""
        now = _now()
        self.conn.execute(
            "INSERT INTO projection_watermark (tenant_id, branch_id, source_service,"
            " applied_sequence, applied_event_id, source_head_sequence, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, branch_id, source_service) DO NOTHING",
            (tenant_id, branch_id, source, sequence, event_id, sequence, now))
        self.conn.execute(
            "UPDATE projection_watermark SET applied_sequence = ?, applied_event_id = ?,"
            " source_head_sequence = max(source_head_sequence, ?), updated_at = ?"
            " WHERE tenant_id = ? AND branch_id = ? AND source_service = ?"
            " AND applied_sequence < ?",
            (sequence, event_id, sequence, now, tenant_id, branch_id, source, sequence))
        # 跟上之后落后归零。不写这一步，某个源瞬时抖过一次就永远背着
        # `observed_lag_ms`，后面每次闭包都判它超预算——降级会粘住。
        self.conn.execute(
            "UPDATE projection_watermark SET observed_lag_ms = 0"
            " WHERE tenant_id = ? AND branch_id = ? AND source_service = ?"
            " AND source_head_sequence >= applied_sequence",
            (tenant_id, branch_id, source))

    def observe_source_head(self, tenant_id: str, branch_id: str, source: str,
                            head_sequence: int) -> None:
        """只更新"源侧最新"，不动 applied_sequence。对账入口用它。"""
        self._note_source_head(tenant_id, branch_id, source, head_sequence)

    def observe_lag(self, tenant_id: str, branch_id: str, source: str,
                    lag_ms: int, *, budget_ms: int | None = None) -> None:
        """记下「这个源现在落后多少」。闭包靠它判 EXACT / CONSERVATIVE。

        这是 S09 唯一需要源侧配合的地方：它不主动去问每个源"你到第几个
        sequence 了"——那会把一次闭包变成一次扇出调用。对账任务（§7.3）
        量完写这里，闭包读它。
        """
        self.conn.execute(
            "INSERT INTO projection_watermark (tenant_id, branch_id, source_service,"
            " applied_sequence, applied_event_id, source_head_sequence, lag_budget_ms,"
            " observed_lag_ms, updated_at)"
            " VALUES (?, ?, ?, 0, NULL, 0, ?, ?, ?)"
            " ON CONFLICT (tenant_id, branch_id, source_service) DO UPDATE SET"
            " observed_lag_ms = excluded.observed_lag_ms,"
            " lag_budget_ms = CASE WHEN ? IS NULL THEN lag_budget_ms ELSE ? END,"
            " updated_at = excluded.updated_at",
            (tenant_id, branch_id, source, budget_ms or 2000, max(lag_ms, 0), _now(),
             budget_ms, budget_ms or 2000))

    @staticmethod
    def _edge_id(tenant_id: str, branch_id: str, kind: str, source: Ref,
                 target: Ref, field_path: str | None, valid_from: int) -> str:
        """确定性 edge_id：同一事件重放算出同一个 id，落地靠唯一索引兜底。"""
        material = "|".join((
            tenant_id, branch_id, kind,
            f"{source.service_id}/{source.object_id}/{source.revision}",
            f"{target.service_id}/{target.object_id}/{target.revision}",
            field_path or "", str(valid_from)))
        return "e_" + _sha256(material)[:32]

    def _insert_edge(self, tenant_id: str, branch_id: str, kind: str, source: Ref,
                     target: Ref, field_path: str | None, origin: str,
                     origin_event_id: str | None, valid_from: int) -> tuple[str, str]:
        """返回 (edge_id, 'accepted'|'duplicate')。"""
        edge_id = self._edge_id(tenant_id, branch_id, kind, source, target,
                                field_path, valid_from)
        cur = self.conn.execute(
            "INSERT INTO lineage_edge (tenant_id, branch_id, edge_id, edge_kind,"
            " source_service_id, source_id, source_revision, source_hash,"
            " target_service_id, target_id, target_revision, target_hash,"
            " field_path, origin, origin_event_id, valid_from, valid_to, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)"
            " ON CONFLICT DO NOTHING",
            (tenant_id, branch_id, edge_id, kind,
             source.service_id, source.object_id, source.revision,
             source.content_hash or "", target.service_id, target.object_id,
             target.revision, target.content_hash or "", field_path, origin,
             origin_event_id, valid_from, _now()))
        return edge_id, ("accepted" if cur.rowcount == 1 else "duplicate")

    def _note_source_head(self, tenant_id: str, branch_id: str, source: str,
                          sequence) -> None:
        """在 handler **开头**记下「这条事件是第几格」。

        `valid_from`/`valid_to` 要的是「这条变更落在哪一格」，而 `consume`
        是在 handler **跑完之后**才推进 `applied_sequence` 的。handler 里读
        `_watermark` 读到的还是上一条，于是：写给自己的边记成上一格（重放
        时 `edge_id` 变了，同一条边落两行），关旧边在「该源第一条事件」时
        算出 `valid_to = 0`，被 `valid_to > valid_from` 拒掉。先记头，后面
        两个都读得到本格。
        """
        if not isinstance(sequence, int):
            return
        self.conn.execute(
            "INSERT INTO projection_watermark (tenant_id, branch_id, source_service,"
            " applied_sequence, applied_event_id, source_head_sequence, updated_at)"
            " VALUES (?, ?, ?, 0, NULL, ?, ?)"
            " ON CONFLICT (tenant_id, branch_id, source_service) DO UPDATE SET"
            " source_head_sequence = max(source_head_sequence, excluded.source_head_sequence),"
            " updated_at = excluded.updated_at",
            (tenant_id, branch_id, source, sequence, _now()))

    def _close_edges(self, tenant_id: str, branch_id: str, kind: str, source: Ref,
                     target: Ref, valid_to: int) -> int:
        """关有效期，不物理删（§3.2）。"""
        cur = self.conn.execute(
            "UPDATE lineage_edge SET valid_to = ?"
            " WHERE tenant_id = ? AND branch_id = ? AND edge_kind = ?"
            " AND source_service_id = ? AND source_id = ? AND source_revision = ?"
            " AND target_service_id = ? AND target_id = ? AND target_revision = ?"
            " AND valid_to IS NULL AND valid_from < ?",
            (valid_to, tenant_id, branch_id, kind, source.service_id, source.object_id,
             source.revision, target.service_id, target.object_id, target.revision,
             valid_to))
        return cur.rowcount

    # ---- 写边 ----------------------------------------------------------------

    def write_edges(self, tenant_id: str, body: dict) -> dict:
        """§5.1 `POST /lineage-edges`。整批原子：任一失败全批拒绝。"""
        # 租户先判：一个跨租户的请求不该先被"edges 为空"之类的形状错误
        # 挡下——那会让调用方以为改改 body 就能过，而它根本进不来。
        declared_tenant = body.get("tenant_id")
        if declared_tenant is not None and declared_tenant != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "body tenant_id disagrees with the signed context")
        raw_edges = body.get("edges")
        if not isinstance(raw_edges, list) or not raw_edges:
            raise ServiceError(422, "SCHEMA_INVALID", "edges must be a non-empty list")
        if len(raw_edges) > MAX_EDGES_PER_BATCH:
            raise ServiceError(422, "BATCH_TOO_LARGE",
                               f"at most {MAX_EDGES_PER_BATCH} edges per call, "
                               f"got {len(raw_edges)}")

        branch_id = body.get("branch_id") or "main"
        parsed = [self._parse_edge(tenant_id, e) for e in raw_edges]

        # 校验 4：不许写"未来的边"。S09 只知道它见过的最高 revision。
        for kind, source, target, _ in parsed:
            self._check_known_revision(tenant_id, branch_id, source, target)

        accepted = duplicates = 0
        ids: list[str] = []
        watermark = min(self._watermark(tenant_id, branch_id, s) for s in ALWAYS_SOURCES)
        with self.conn:
            for kind, source, target, field_path in parsed:
                edge_id, result = self._insert_edge(
                    tenant_id, branch_id, kind, source, target, field_path,
                    "SELF_REPORTED", None, watermark)
                ids.append(edge_id)
                if result == "accepted":
                    accepted += 1
                else:
                    duplicates += 1
            # 补边成功后，指向这条边的 gap 才算解决（§2.5：gap 不靠人工关）。
            # anchor 是「缺了什么边的那一端」，也就是新边的 **target**。
            for kind, _, target, _ in parsed:
                self._resolve_gaps_for(tenant_id, branch_id, kind, [target])

        return {"accepted": accepted, "duplicates": duplicates,
                "superseded": 0, "edge_ids": ids}

    def _parse_edge(self, tenant_id: str, raw) -> tuple[str, Ref, Ref, str | None]:
        if not isinstance(raw, dict):
            raise ServiceError(422, "SCHEMA_INVALID", "each edge must be an object")
        kind = raw.get("edge_kind")
        if kind not in EDGE_KINDS:
            raise ServiceError(422, "SCHEMA_INVALID", f"edge_kind {kind!r} is not in the closed set")
        source = parse_ref(raw.get("source"), name="source")
        target = parse_ref(raw.get("target"), name="target")
        field_path = raw.get("field_path")
        check_field_path(field_path)
        if (source.service_id == target.service_id and source.object_id == target.object_id
                and source.revision == target.revision):
            raise ServiceError(422, "SCHEMA_INVALID", "an edge may not point at itself")
        return kind, source, target, field_path

    def _check_known_revision(self, tenant_id: str, branch_id: str,
                              source: Ref, target: Ref) -> None:
        known = self.conn.execute(
            "SELECT max(object_revision) FROM node_ref WHERE tenant_id = ?"
            " AND branch_id = ? AND service_id = ? AND object_id = ?",
            (tenant_id, branch_id, source.service_id, source.object_id)).fetchone()[0]
        if known is not None and source.revision > known:
            raise ServiceError(
                500, "EDGE_DANGLING_REF",
                f"edge source {source.service_id}/{source.object_id}@{source.revision} "
                f"is ahead of the highest revision this service has seen ({known}); "
                "the upstream event is inconsistent")

    def close_edge(self, tenant_id: str, branch_id: str, kind: str, source: Ref,
                   target: Ref) -> int:
        """显式关边。语义变更（不是换 revision）时用，例如资产解绑。"""
        with self.conn:
            return self._close_edges(tenant_id, branch_id, kind, source, target,
                                     self._watermark(tenant_id, branch_id, source.service_id))

    # ---- 字段投影 ------------------------------------------------------------

    def project_fields(self, tenant_id: str, branch_id: str, body: dict) -> dict:
        """写入字段 hash。非白名单字段带 scalar 会在库里被触发器拒绝。"""
        service_id = body.get("service_id")
        object_id = body.get("id")
        revision = body.get("revision")
        fields = body.get("fields")
        if not isinstance(service_id, str) or not service_id:
            raise ServiceError(422, "SCHEMA_INVALID", "service_id is required")
        if not isinstance(object_id, str) or not object_id:
            raise ServiceError(422, "SCHEMA_INVALID", "id is required")
        if not isinstance(revision, int) or revision < 1:
            raise ServiceError(422, "SCHEMA_INVALID", "revision must be >= 1")
        if not isinstance(fields, dict) or not fields:
            raise ServiceError(422, "SCHEMA_INVALID", "fields must be a non-empty object")

        written = 0
        try:
            with self.conn:
                for path, value in fields.items():
                    check_field_path(path)
                    value_type = _value_type(value)
                    value_hash = _sha256(_jcs(value))
                    scalar = json.dumps(value, ensure_ascii=False) if value_type in (
                        "string", "number", "boolean") else None
                    self.conn.execute(
                        "INSERT INTO field_projection (tenant_id, branch_id, service_id,"
                        " object_id, revision, field_path, value_type, value_hash,"
                        " scalar_value, projected_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (tenant_id, branch_id, service_id, object_id,"
                        " revision, field_path) DO UPDATE SET"
                        " value_type = excluded.value_type,"
                        " value_hash = excluded.value_hash,"
                        " scalar_value = excluded.scalar_value,"
                        " projected_at = excluded.projected_at",
                        (tenant_id, branch_id, service_id, object_id, revision, path,
                         value_type, value_hash, scalar, _now()))
                    written += 1
        except sqlite3.IntegrityError as exc:
            if "SCALAR_PROJECTION_NOT_ALLOWED" in str(exc):
                raise ServiceError(
                    422, "SCALAR_PROJECTION_NOT_ALLOWED",
                    "only whitelisted structural fields may carry a value; "
                    "everything else is hash-only",
                    details={"whitelist": [f"{s}{p}" for s, p, _, _ in WHITELIST]}) from None
            raise
        return {"projected": written}

    def scalar(self, tenant_id: str, service_id: str, object_id: str,
               revision: int, field_path: str):
        """读白名单标量。不在白名单里返回 None——不猜。"""
        row = self.conn.execute(
            "SELECT scalar_value FROM field_projection WHERE tenant_id = ?"
            " AND service_id = ? AND object_id = ? AND revision = ? AND field_path = ?",
            (tenant_id, service_id, object_id, revision, field_path)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def field_hash(self, tenant_id: str, service_id: str, object_id: str,
                   revision: int, field_path: str) -> str | None:
        row = self.conn.execute(
            "SELECT value_hash FROM field_projection WHERE tenant_id = ?"
            " AND service_id = ? AND object_id = ? AND revision = ? AND field_path = ?",
            (tenant_id, service_id, object_id, revision, field_path)).fetchone()
        return row[0] if row else None

    # ---- 事件消费 ------------------------------------------------------------

    def consume(self, envelope: dict, *, side_effects: bool = True) -> dict:
        """接一条事件。inbox 按用途分开去重（§2.7）。"""
        event_type = envelope.get("type")
        route = EVENT_ROUTES.get(event_type)
        if route is None:
            # S09 不认识的事件不是错误，只是不归它管。不回 4xx 让上游重试。
            return {"consumed": False, "reason": "unrouted"}
        consumer_key, handler_name, advances, source = route
        tenant_id = envelope.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ServiceError(422, "SCHEMA_INVALID", "event lacks tenant_id")
        event_id = envelope.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(422, "SCHEMA_INVALID", "event lacks id")

        consumer = CONSUMERS[consumer_key]
        with self.conn:
            first = self.inbox.consume(consumer, event_id)
            if not first:
                return {"consumed": False, "reason": "duplicate"}
            handler = getattr(self, handler_name)
            result = handler(tenant_id, envelope, side_effects=side_effects)
            if advances:
                sequence = envelope.get("sequence", 0)
                if isinstance(sequence, int):
                    self._advance_watermark(
                        tenant_id, result.get("branch_id", "main"), source, sequence, event_id)
        return {"consumed": True, **result}

    def _data(self, envelope: dict) -> dict:
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ServiceError(422, "SCHEMA_INVALID", "event data must be an object")
        return data

    def ingest_pack_created(self, tenant_id: str, envelope: dict,
                            *, side_effects: bool = True) -> dict:
        """S18 context.pack.created → `PACK_USES_CLAIM` 边（§5.4）。"""
        data = self._data(envelope)
        branch_id = data.get("branch_id") or "main"
        pack = parse_ref(
            {"service_id": "S18", "id": data.get("pack_id"),
             "revision": data.get("pack_revision", 1),
             "content_hash": data.get("content_hash", "")}, name="pack")
        claim_refs = data.get("claim_refs") or []
        self._note_source_head(tenant_id, branch_id, "S18", envelope.get("sequence"))
        watermark = self._watermark(tenant_id, branch_id, "S18")
        written = duplicates = 0
        for raw in claim_refs:
            claim = parse_ref(raw, name="claim_refs[]")
            _, result = self._insert_edge(
                tenant_id, branch_id, "PACK_USES_CLAIM", claim, pack, None,
                "EVENT_DERIVED", envelope["id"] if not claim_refs[1:] else None, watermark)
            if result == "accepted":
                written += 1
            else:
                duplicates += 1
        return {"branch_id": branch_id, "edges_written": written,
                "duplicates": duplicates, "pack": pack.to_dict()}

    def ingest_manifest_committed(self, tenant_id: str, envelope: dict,
                                  *, side_effects: bool = True) -> dict:
        """S08 snapshot.manifest.committed → node_ref 与 fence，不写边。

        S09 不复制 manifest；只记「这些 revision 在这份 manifest 里叫什么节点」。
        fence 前进时把在算的 analysis 标 STALE_FENCE（§3.1）。
        """
        data = self._data(envelope)
        manifest_id = data.get("manifest_id") or envelope.get("aggregate_id")
        revision = data.get("revision") or envelope.get("sequence")
        branch_id = data.get("branch_id") or "main"
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ServiceError(422, "SCHEMA_INVALID", "manifest event lacks manifest_id")
        if not isinstance(revision, int) or revision < 1:
            raise ServiceError(422, "SCHEMA_INVALID", "manifest event lacks revision")
        fence = data.get("execution_fence", 1)
        series_id = data.get("series_id", "")
        episode_id = data.get("episode_id", "")

        self.conn.execute(
            "INSERT INTO manifest_head (tenant_id, branch_id, manifest_id, revision,"
            " execution_fence, series_id, episode_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, branch_id, manifest_id) DO UPDATE SET"
            " revision = max(revision, excluded.revision),"
            " execution_fence = max(execution_fence, excluded.execution_fence),"
            " series_id = excluded.series_id, episode_id = excluded.episode_id,"
            " updated_at = excluded.updated_at",
            (tenant_id, branch_id, manifest_id, revision, fence, series_id,
             episode_id, _now()))

        nodes = data.get("node_states") or []
        written = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ref = node.get("ref") or node.get("accepted_artifact_ref") or {}
            service_id = ref.get("service_id") or node.get("service_id") or "S06"
            object_id = ref.get("id") or node.get("node_id")
            object_revision = ref.get("revision") or node.get("node_epoch") or 1
            if not isinstance(object_id, str) or not object_id:
                continue
            self.conn.execute(
                "INSERT INTO node_ref (tenant_id, branch_id, manifest_id,"
                " manifest_revision, node_id, service_id, object_id, object_revision,"
                " content_hash, execution_fence, episode_id, binding_set_ref, asset_refs,"
                " expected_edges)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, branch_id, manifest_id, manifest_revision,"
                " node_id) DO UPDATE SET"
                " object_revision = excluded.object_revision,"
                " content_hash = excluded.content_hash,"
                " execution_fence = excluded.execution_fence,"
                " asset_refs = excluded.asset_refs,"
                " expected_edges = excluded.expected_edges",
                (tenant_id, branch_id, manifest_id, revision, node.get("node_id"),
                 service_id, object_id, int(object_revision),
                 ref.get("content_hash", ""), fence, episode_id,
                 _json_or_none(data.get("binding_set_ref")),
                 json.dumps(node.get("asset_refs") or [], ensure_ascii=False),
                 json.dumps(node.get("expected_edges") or [], ensure_ascii=False)))
            written += 1

        stale = 0
        if side_effects:
            cur = self.conn.execute(
                "UPDATE impact_analysis SET status = 'STALE_FENCE'"
                " WHERE tenant_id = ? AND branch_id = ? AND status = 'COMPUTED'"
                " AND execution_fence < ?",
                (tenant_id, branch_id, fence))
            stale = cur.rowcount
        return {"branch_id": branch_id, "nodes_indexed": written, "stale_analyses": stale}

    def ingest_memory_committed(self, tenant_id: str, envelope: dict,
                                *, side_effects: bool = True) -> dict:
        """S16 memory.canon.committed：只登记变化 + 关掉被 supersede 的边。

        §4.3：S09 不消费它就改闭包——闭包永远按需算。这里只做两件事。
        """
        data = self._data(envelope)
        branch_id = data.get("branch_id") or "main"
        sequence = data.get("commit_sequence")
        if not isinstance(sequence, int):
            raise ServiceError(422, "SCHEMA_INVALID", "memory event lacks commit_sequence")
        self.conn.execute(
            "INSERT INTO memory_change (tenant_id, branch_id, commit_sequence, event_id,"
            " payload, received_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, branch_id, commit_sequence) DO NOTHING",
            (tenant_id, branch_id, sequence, envelope["id"],
             json.dumps(data, ensure_ascii=False), _now()))

        self._note_source_head(tenant_id, branch_id, "S16", sequence)
        # §3.2：`valid_to` 是「这次变更落在哪一格」，所以要的是源侧进度本身。
        # 直接用 commit_sequence——它就是这条记忆变更的格号，比读水位可靠：
        # 首条记忆事件时水位还是 0，拿它关边会算出 `valid_to = 0` 被 CHECK 拒掉。
        valid_to = max(sequence, 1)
        closed = 0
        for raw in data.get("superseded_claim_refs") or []:
            claim = parse_ref(raw, name="superseded_claim_refs[]")
            # 旧 revision 的每条 PACK_USES_CLAIM 出边一起关：claim 换了代，
            # 引用它的 pack 也就不再引用那条事实。
            rows = self.conn.execute(
                "SELECT edge_id, target_service_id, target_id, target_revision, field_path"
                " FROM lineage_edge WHERE tenant_id = ? AND branch_id = ?"
                " AND edge_kind = 'PACK_USES_CLAIM' AND source_service_id = ?"
                " AND source_id = ? AND source_revision = ? AND valid_to IS NULL",
                (tenant_id, branch_id, claim.service_id, claim.object_id,
                 claim.revision)).fetchall()
            for row in rows:
                self.conn.execute(
                    "UPDATE lineage_edge SET valid_to = ? WHERE tenant_id = ? AND edge_id = ?",
                    (valid_to, tenant_id, row["edge_id"]))
                closed += 1
        return {"branch_id": branch_id, "claims_closed": closed,
                "changed_claims": len(data.get("changed_claim_refs") or [])}

    def ingest_shot_revision(self, tenant_id: str, envelope: dict,
                             *, side_effects: bool = True) -> dict:
        return self._ingest_declared_edges(
            tenant_id, envelope, "S06", "SHOT_DERIVED_FROM_SCENE")

    def ingest_script_revision(self, tenant_id: str, envelope: dict,
                               *, side_effects: bool = True) -> dict:
        return self._ingest_declared_edges(
            tenant_id, envelope, "S04", "SCRIPT_DERIVED_FROM_PACK")

    def ingest_artifact_archived(self, tenant_id: str, envelope: dict,
                                 *, side_effects: bool = True) -> dict:
        return self._ingest_declared_edges(
            tenant_id, envelope, "S28", "ARTIFACT_FROM_SHOT")

    def ingest_alignment(self, tenant_id: str, envelope: dict,
                         *, side_effects: bool = True) -> dict:
        return self._ingest_declared_edges(
            tenant_id, envelope, "S37", "SUBTITLE_FROM_AUDIO")

    def ingest_asset_binding(self, tenant_id: str, envelope: dict,
                             *, side_effects: bool = True) -> dict:
        """S10 asset.binding.changed：写新边，关旧边。"""
        data = self._data(envelope)
        branch_id = data.get("branch_id") or "main"
        self._note_source_head(tenant_id, branch_id, "S10", envelope.get("sequence"))
        watermark = self._watermark(tenant_id, branch_id, "S10")
        written = closed = 0
        for raw in data.get("bindings") or []:
            if not isinstance(raw, dict):
                continue
            asset = parse_ref(raw.get("asset"), name="bindings[].asset")
            holder = raw.get("holder")
            if not isinstance(holder, dict):
                continue
            target = parse_ref(holder, name="bindings[].holder")
            action = raw.get("action", "bind")
            if action == "unbind":
                closed += self._close_edges(tenant_id, branch_id, "ASSET_BINDING",
                                            asset, target, watermark)
            else:
                _, result = self._insert_edge(
                    tenant_id, branch_id, "ASSET_BINDING", asset, target, None,
                    "EVENT_DERIVED", envelope["id"], watermark)
                written += 1 if result == "accepted" else 0
        return {"branch_id": branch_id, "edges_written": written, "edges_closed": closed}

    def _ingest_declared_edges(self, tenant_id: str, envelope: dict,
                               default_service: str, default_kind: str) -> dict:
        """内容服务自报的边：事件里带 `edges[]`，S09 只做校验与落地。"""
        data = self._data(envelope)
        branch_id = data.get("branch_id") or "main"
        self._note_source_head(tenant_id, branch_id, default_service,
                               envelope.get("sequence"))
        watermark = self._watermark(tenant_id, branch_id, default_service)
        written = duplicates = 0
        for raw in data.get("edges") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("edge_kind") or default_kind
            source = raw.get("source")
            target = raw.get("target")
            if source is None and raw.get("scene_ref"):
                source = raw["scene_ref"]
            if target is None:
                target = {"service_id": default_service,
                          "id": data.get("shot_id") or data.get("artifact_id")
                          or data.get("object_id") or raw.get("shot_id"),
                          "revision": data.get("revision", 1)}
            if source is None or target.get("id") is None:
                continue
            src_ref = parse_ref(source, name="source")
            tgt_ref = parse_ref(target, name="target")
            field_path = raw.get("field_path")
            check_field_path(field_path)
            _, result = self._insert_edge(
                tenant_id, branch_id, kind, src_ref, tgt_ref, field_path,
                "EVENT_DERIVED", envelope["id"], watermark)
            if result == "accepted":
                written += 1
            else:
                duplicates += 1
        return {"branch_id": branch_id, "edges_written": written,
                "duplicates": duplicates}

    # ---- 缺口 ----------------------------------------------------------------

    def _gap_id(self, tenant_id: str, branch_id: str, kind: str,
                anchor_key: str) -> str:
        return "g_" + _sha256(f"{tenant_id}|{branch_id}|{kind}|{anchor_key}")[:24]

    def record_gap(self, tenant_id: str, branch_id: str, expected_kind: str,
                   anchor: dict, severity: str, detail: str,
                   detected_by: str = "CLOSURE") -> str:
        anchor_key = "|".join(str(anchor.get(k, "")) for k in
                              ("service_id", "id", "revision"))
        gap_id = self._gap_id(tenant_id, branch_id, expected_kind, anchor_key)
        self.conn.execute(
            "INSERT INTO lineage_gap (tenant_id, gap_id, branch_id, expected_kind,"
            " anchor_ref, detected_by, severity, detail, resolved_at,"
            " resolved_edge_ids, first_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)"
            " ON CONFLICT (tenant_id, gap_id) DO NOTHING",
            (tenant_id, gap_id, branch_id, expected_kind,
             json.dumps(anchor, ensure_ascii=False, sort_keys=True), detected_by,
             severity, detail, _now()))
        return gap_id

    def _resolve_gaps_for(self, tenant_id: str, branch_id: str,
                          kind: str, sources: list[Ref]) -> int:
        """补边后自动关 gap。§2.5：gap 不靠人工关。

        只关「anchor 正是这条新边的 source、且 expected_kind 对得上」的 gap。
        按 source 无条件关会顺带关掉同一对象的别的缺口，那是掩盖而不是修复。
        """
        resolved = 0
        for source in sources:
            anchor_key = "|".join((source.service_id, source.object_id,
                                   str(source.revision)))
            rows = self.conn.execute(
                "SELECT gap_id FROM lineage_gap WHERE tenant_id = ? AND branch_id = ?"
                " AND expected_kind = ? AND resolved_at IS NULL",
                (tenant_id, branch_id, kind)).fetchall()
            for row in rows:
                stored = self.conn.execute(
                    "SELECT anchor_ref FROM lineage_gap WHERE tenant_id = ? AND gap_id = ?",
                    (tenant_id, row["gap_id"])).fetchone()[0]
                anchor = json.loads(stored)
                if "|".join((anchor.get("service_id", ""), anchor.get("id", ""),
                             str(anchor.get("revision", "")))) != anchor_key:
                    continue
                self.conn.execute(
                    "UPDATE lineage_gap SET resolved_at = ?, resolved_edge_ids = ?"
                    " WHERE tenant_id = ? AND gap_id = ?",
                    (_now(), json.dumps([]), tenant_id, row["gap_id"]))
                resolved += 1
        return resolved

    def open_gaps(self, tenant_id: str, branch_id: str,
                  severity: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM lineage_gap WHERE tenant_id = ? AND branch_id = ?"
               " AND resolved_at IS NULL")
        args: list = [tenant_id, branch_id]
        if severity:
            sql += " AND severity = ?"
            args.append(severity)
        rows = self.conn.execute(sql + " ORDER BY first_seen_at", args).fetchall()
        return [self._render_gap(r) for r in rows]

    @staticmethod
    def _render_gap(row) -> dict:
        return {"gap_id": row["gap_id"], "branch_id": row["branch_id"],
                "expected_kind": row["expected_kind"],
                "anchor_ref": json.loads(row["anchor_ref"]),
                "severity": row["severity"], "detail": row["detail"],
                "detected_by": row["detected_by"],
                "first_seen_at": row["first_seen_at"],
                "resolved_at": row["resolved_at"]}
    # ---- 影响闭包 ------------------------------------------------------------

    def _edges_from(self, tenant_id: str, branch_id: str,
                    keys: set[tuple[str, str, int]]) -> dict:
        """一次把闭包会用到的出边取好（§6.5：同一快照内）。"""
        out: dict[tuple[str, str, int], list[Edge]] = {}
        for key in keys:
            rows = self.conn.execute(
                "SELECT edge_id, edge_kind, source_service_id, source_id,"
                " source_revision, source_hash, target_service_id, target_id,"
                " target_revision, target_hash, field_path FROM lineage_edge"
                " WHERE tenant_id = ? AND branch_id = ? AND valid_to IS NULL"
                " AND source_service_id = ? AND source_id = ? AND source_revision = ?",
                (tenant_id, branch_id, *key)).fetchall()
            out[key] = [_edge_of(r) for r in rows]
        return out

    def _edges_to(self, tenant_id: str, branch_id: str,
                  keys: set[tuple[str, str, int]]) -> dict:
        out: dict[tuple[str, str, int], list[Edge]] = {}
        for key in keys:
            rows = self.conn.execute(
                "SELECT edge_id, edge_kind, source_service_id, source_id,"
                " source_revision, source_hash, target_service_id, target_id,"
                " target_revision, target_hash, field_path FROM lineage_edge"
                " WHERE tenant_id = ? AND branch_id = ? AND valid_to IS NULL"
                " AND target_service_id = ? AND target_id = ? AND target_revision = ?",
                (tenant_id, branch_id, *key)).fetchall()
            out[key] = [_edge_of(r) for r in rows]
        return out

    def _changed_fields(self, tenant_id: str, source: Ref,
                        fields: list[str]) -> set[str]:
        """哪些点名字段真的变了。

        判据是同一字段在上一版的 hash 与本版不同。缺上一版投影时按"变了"处理
        ——"没变"是更强的断言，没有证据不能给。evidence 里记 baseline_missing，
        让这个保守选择在结论里看得见。
        """
        changed: set[str] = set()
        for path in fields:
            current = self.field_hash(tenant_id, source.service_id, source.object_id,
                                      source.revision, path)
            if current is None:
                changed.add(path)
                continue
            previous = None
            if source.revision > 1:
                previous = self.field_hash(tenant_id, source.service_id,
                                           source.object_id, source.revision - 1, path)
            if previous is None or previous != current:
                changed.add(path)
        return changed

    def _gap_inputs(self, tenant_id: str, branch_id: str, keys):
        """每个节点的「该有哪些入边」与「实际有哪些入边」。

        声明来自 `node_ref.expected_edges`（S08 的 manifest 里节点自己写的），
        实际来自 `lineage_edge`。两者之差就是缺口——这是唯一不靠猜的判据。
        """
        expected: dict[tuple[str, str, int], list[dict]] = {}
        for key in keys:
            row = self.conn.execute(
                "SELECT expected_edges FROM node_ref WHERE tenant_id = ? AND branch_id = ?"
                " AND service_id = ? AND object_id = ? AND object_revision = ?"
                " ORDER BY manifest_revision DESC LIMIT 1",
                (tenant_id, branch_id, *key)).fetchone()
            if row is None:
                continue
            claims = json.loads(row[0] or "[]")
            if claims:
                expected[key] = claims

        present: dict[tuple[str, str, int], set[str]] = {}
        for key in keys:
            rows = self.conn.execute(
                "SELECT DISTINCT edge_kind FROM lineage_edge WHERE tenant_id = ?"
                " AND branch_id = ? AND valid_to IS NULL AND target_service_id = ?"
                " AND target_id = ? AND target_revision = ?",
                (tenant_id, branch_id, *key)).fetchall()
            present[key] = {r[0] for r in rows}
        return expected, present

    def _episode_of(self, tenant_id: str, branch_id: str,
                    keys: set[tuple[str, str, int]]) -> dict:
        out: dict[tuple[str, str, int], str] = {}
        for key in keys:
            row = self.conn.execute(
                "SELECT episode_id FROM node_ref WHERE tenant_id = ? AND branch_id = ?"
                " AND service_id = ? AND object_id = ? AND object_revision = ?"
                " ORDER BY manifest_revision DESC LIMIT 1",
                (tenant_id, branch_id, *key)).fetchone()
            if row and row[0]:
                out[key] = row[0]
        return out

    def _name_of(self, tenant_id: str, branch_id: str,
                 keys: set[tuple[str, str, int]]) -> dict[tuple[str, str, int], str]:
        """(service,id,revision) → manifest 里的 node_id。

        对外结论文档（`regenerate_node_ids`、`impact_analysis_node.node_id`）用
        这个名字，因为 S07 会把它原样塞进 ChangePlan，那里认的是 manifest 的
        节点名（`shot_3_2.video`），不是 S09 的复合键。同一 revision 在不同
        manifest 里名字可能不同——取最新那份，与 `_episode_of` 同口径。
        """
        out: dict[tuple[str, str, int], str] = {}
        for key in keys:
            row = self.conn.execute(
                "SELECT node_id FROM node_ref WHERE tenant_id = ? AND branch_id = ?"
                " AND service_id = ? AND object_id = ? AND object_revision = ?"
                " ORDER BY manifest_revision DESC LIMIT 1",
                (tenant_id, branch_id, *key)).fetchone()
            if row and row[0]:
                out[key] = row[0]
        return out

    def _lag_state(self, tenant_id: str, branch_id: str,
                   sources: set[str]) -> tuple[dict[str, int], list[str], int]:
        """按用到的源求水位与降级原因（§2.6：只取用到的源）。

        T09-27：一个从不消费 S41 的项目不该被 S41 的积压拖成 CONSERVATIVE。
        """
        watermark_source: dict[str, int] = {}
        degraded: list[str] = []
        consumed: list[int] = []
        for source in sorted(sources):
            row = self.conn.execute(
                "SELECT applied_sequence, observed_lag_ms, lag_budget_ms"
                " FROM projection_watermark WHERE tenant_id = ? AND branch_id = ?"
                " AND source_service = ?", (tenant_id, branch_id, source)).fetchone()
            has_feed = source in EVENT_SOURCES
            if row is None:
                watermark_source[source] = 0
                if has_feed:
                    # 这个源会给 S09 发事件，却一条都没到——这是"追不上"，不是"最新"。
                    degraded.append(f"{source}: no events applied yet")
                # 没有事件源的服务（S31/S38/S41/S44……）不是"落后"，是"不产生
                # S09 消费的事件"。把它们算进 min 会让水位恒为 0，也会把每次闭包
                # 都拖成保守——那是用错误的理由放大了成本。
                continue
            watermark_source[source] = row["applied_sequence"]
            if has_feed:
                # 只有会向 S09 发事件的源，`applied_sequence` 才是"追到第
                # 几条"。其余源的这一列恒为 0（`observe_lag` 建行时写 0），
                # 把它算进 min 会让水位恒为 0——§2.6 说的「只对实际用到的
                # 源取 min」指的是这些真喂数据的源。
                consumed.append(row["applied_sequence"])
            if row["observed_lag_ms"] > row["lag_budget_ms"]:
                degraded.append(
                    f"{source}: lag {row['observed_lag_ms']}ms over budget "
                    f"{row['lag_budget_ms']}ms")
        watermark = min(consumed) if consumed else 0
        return watermark_source, degraded, watermark

    def query_impact(self, tenant_id: str, body: dict) -> dict:
        """§5.2 `POST /impact-queries`。

        顺序是刻意的：先算、后校验 fence、再写（§4.5 F-06）。若开头校验一次就
        写，计算期间被取消的 run 仍会拿到一个 COMPUTED，S07 会拿它去给已取消的
        run 生成修改计划。
        """
        base_ref = body.get("base_manifest_ref")
        if not isinstance(base_ref, dict) or not isinstance(base_ref.get("id"), str):
            raise ServiceError(422, "SCHEMA_INVALID", "base_manifest_ref.id is required")
        # 契约里 `base_manifest_ref` 是 required[ id, revision, content_hash ]
        # 且 `additionalProperties: false`，三样一起才能让 S07 判 analysis 是否
        # stale（LNI-4）。只收 id 的话 S09 会发出一条下游 schema 验不过的事件，
        # 而它自己的单测照样绿——校验必须在入口做完。
        base_rev = base_ref.get("revision")
        if not isinstance(base_rev, int) or isinstance(base_rev, bool) or base_rev < 1:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "base_manifest_ref.revision must be an integer >= 1")
        base_hash = base_ref.get("content_hash")
        if not _is_hash(base_hash):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "base_manifest_ref.content_hash must be 64 lowercase hex")
        branch_id = body.get("branch_id") or "main"
        seed = body.get("seed")
        if not isinstance(seed, dict):
            raise ServiceError(422, "SCHEMA_INVALID", "seed is required")
        seed_kind = seed.get("kind") or "CHANGE_SEED"
        refs = seed.get("refs")
        if not isinstance(refs, list) or not refs:
            raise ServiceError(422, "SCHEMA_INVALID", "seed.refs must be a non-empty list")
        mode_hint = (body.get("mode_hint") or "EXACT").upper()
        if mode_hint not in ("EXACT", "ALLOW_CONSERVATIVE"):
            raise ServiceError(422, "SCHEMA_INVALID", f"unknown mode_hint {mode_hint!r}")

        series_id = body.get("series_id", "")
        fence = body.get("execution_fence")
        if not isinstance(fence, int) or fence < 1:
            raise ServiceError(422, "SCHEMA_INVALID", "execution_fence is required")

        request_hash = _sha256(_jcs({k: v for k, v in body.items()
                                     if k not in ("command_id", "request_hash")}))
        existing = self.conn.execute(
            "SELECT analysis_id FROM impact_analysis WHERE tenant_id = ?"
            " AND request_hash = ? AND status = 'COMPUTED'",
            (tenant_id, request_hash)).fetchone()
        if existing is not None:
            # §6.2 查询级幂等：同 request_hash 永远同一个 analysis，
            # 不同 command_id 的等价查询也给同一个 analysis_hash。
            return {**self.get_analysis(tenant_id, existing[0]), "replayed": True}

        seed_entries = []
        for raw in refs:
            ref = parse_ref(raw, name="seed.refs[]")
            fields = raw.get("fields") or []
            if not isinstance(fields, list):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   "seed.refs[].fields must be a list")
            for path in fields:
                check_field_path(path)
            seed_entries.append((ref, fields))

        seeds = [ref for ref, _ in seed_entries]
        changed: dict[tuple[str, str, int], set[str]] = {}
        for ref, fields in seed_entries:
            if fields:
                changed[ref.key] = self._changed_fields(tenant_id, ref, fields)

        # 边的可达集合必须走全再交给 walk。walk 拿到的 map 若是半张图，
        # 结果就是"静默更小的闭包"——正是 §9 警告的那件事。
        out_edges: dict[tuple[str, str, int], list[Edge]] = {}
        scanned: set[tuple[str, str, int]] = {s.key for s in seeds}
        frontier = set(scanned)
        while frontier:
            out_edges.update(self._edges_from(tenant_id, branch_id, frontier))
            new: set[tuple[str, str, int]] = set()
            for edges in out_edges.values():
                for edge in edges:
                    if edge.target.key not in scanned:
                        new.add(edge.target.key)
            if len(scanned | new) > closure.MAX_NODES:
                raise ServiceError(422, "CLOSURE_TOO_LARGE",
                                   f"closure exceeds {closure.MAX_NODES} nodes; "
                                   "narrow the seed with a deeper ref")
            scanned |= new
            frontier = new

        episode_of = self._episode_of(tenant_id, branch_id, scanned)
        name_of = self._name_of(tenant_id, branch_id, scanned)
        expected_in, present_in = self._gap_inputs(tenant_id, branch_id, scanned)
        try:
            result = closure.walk(
                seeds=seeds, out_edges_of=out_edges, changed_fields=changed,
                fingerprint_changed={}, episode_of=episode_of,
                expected_in=expected_in, present_in=present_in, name_of=name_of)
        except GraphError as exc:
            with self.conn:
                self._store_analysis(
                    tenant_id, branch_id, series_id, base_ref, seed_kind, seed,
                    request_hash, "EXACT", "FAILED", fence, 0, {}, None, None, None)
            raise ServiceError(500, exc.code, str(exc), retryable=False) from None

        # 向上反向校验每个候选 REUSE（§4.1 步骤 7）。
        in_edges = self._edges_to(tenant_id, branch_id, scanned)
        closure.verify_reuse(result, in_edges_of=in_edges, changed_fields=changed)
        self._apply_reuse_rules(tenant_id, branch_id, result, seeds, changed)

        watermark_source, degraded, watermark = self._lag_state(
            tenant_id, branch_id, result.source_services | set(ALWAYS_SOURCES))
        # §2.5：DEGRADED 缺口的语义就是「这一档只能保守算」。只把缺口记下来
        # 而不降模式，等于对外宣称一个 EXACT 闭包——而它自己刚承认少了一条
        # 边。BLOCKING 在下面另走 INCOMPLETE，不在这里。
        degraded_gaps = [g for g in result.gaps if g.severity == "DEGRADED"]
        if degraded_gaps:
            degraded = [
                *degraded,
                *(f"{g.expected_kind} input missing at {g.anchor.get('service_id')}"
                  f"/{g.anchor.get('id')}" for g in degraded_gaps),
            ]
        mode = "CONSERVATIVE" if degraded else "EXACT"

        if mode == "CONSERVATIVE":
            shots = self._episode_shots(tenant_id, branch_id, result.episode_ids)
            # 并入的整集 shot 不在 `scanned` 里，`name_of` 没给它们名字。
            # 不补这一步，它们会以 `S06:shot_3_9@1` 的复合键出现在
            # `regenerate_node_ids` 里——S07 认不出，ChangePlan 直接 422。
            extra = {shot.key for group in shots.values() for shot in group}
            name_of = {**name_of, **self._name_of(
                tenant_id, branch_id, extra - set(name_of))}
            closure.apply_conservative(result, episode_shots=shots, name_of=name_of)

        blocking = [g for g in result.gaps if g.severity == "BLOCKING"]
        with self.conn:
            gap_ids = [self.record_gap(tenant_id, branch_id, g.expected_kind,
                                       g.anchor, g.severity, g.detail)
                       for g in result.gaps]
            for cycle_node in result.cycle_nodes:
                service_id, rest = cycle_node.split(":", 1)
                object_id, revision = rest.rsplit("@", 1)
                gap_ids.append(self.record_gap(
                    tenant_id, branch_id, "CYCLE",
                    {"service_id": service_id, "id": object_id,
                     "revision": int(revision)},
                    "DEGRADED", "closure edge cycle stopped here", "CLOSURE"))
            blocking_ids = [gid for gid, gap in zip(gap_ids, result.gaps)
                            if gap.severity == "BLOCKING"]

        if blocking:
            # §1.5 情形 C：缺必需边不静默降级。落一行 INCOMPLETE 让审计看得见，
            # 但不发事件、不给结论——否则 S07 会展示一个它没算过的成本区间。
            analysis_id = self._store_analysis(
                tenant_id, branch_id, series_id, base_ref, seed_kind, seed,
                request_hash, mode, "INCOMPLETE", fence, watermark, watermark_source,
                blocking_ids, None, None, result=result)
            raise ServiceError(
                409, "IMPACT_INCOMPLETE",
                "required lineage edges are missing; impact cannot be proven",
                retryable=True,
                details={"gap_ids": blocking_ids, "analysis_id": analysis_id})

        if mode == "CONSERVATIVE" and mode_hint == "EXACT":
            self._store_analysis(
                tenant_id, branch_id, series_id, base_ref, seed_kind, seed,
                request_hash, mode, "FAILED", fence, watermark, watermark_source,
                None, None, None, result=None)
            raise ServiceError(
                409, "CONSERVATIVE_REQUIRED",
                "an exact closure is not currently provable",
                retryable=False,
                details={"would_be_mode": "CONSERVATIVE",
                         "reason": "; ".join(degraded)})

        payload = self._impact_payload(result, watermark)
        analysis_hash = _sha256(_jcs(payload))

        # F-06：写库前再读一次 fence。
        current_fence = self._current_fence(tenant_id, branch_id, base_ref["id"])
        if current_fence is not None and fence < current_fence:
            self._store_analysis(
                tenant_id, branch_id, series_id, base_ref, seed_kind, seed,
                request_hash, mode, "STALE_FENCE", fence, watermark, watermark_source,
                None, None, None, result=None)
            raise ServiceError(
                409, "STALE_EXECUTION_FENCE",
                f"execution_fence advanced from {fence} to {current_fence} "
                "while the closure was being computed",
                retryable=True,
                details={"current_fence": current_fence})

        expires_at = _plus_seconds(ANALYSIS_TTL_SECONDS)
        with self.conn:
            analysis_id = self._store_analysis(
                tenant_id, branch_id, series_id, base_ref, seed_kind, seed,
                request_hash, mode, "COMPUTED", fence, watermark, watermark_source,
                None, payload, analysis_hash, result=result,
                expires_at=expires_at)
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_TYPE,
                subject=f"impact/{analysis_id}", tenant_id=tenant_id,
                home_cell=body.get("home_cell", "cell_a1"),
                aggregate_id=analysis_id, sequence=1,
                correlation_id=body.get("command_id", ""),
                causation_id=seed.get("plan_id"),
                data={
                    # 契约的 payload schema 把 `schema_version` 收进了 required，
                    # 意为"payload 自身版本"，与 envelope 上那个同名键不是一件事。
                    # 只由 `new_event` 写 envelope 会让发出的事件体在这条上验不过。
                    "schema_version": "2.0",
                    "analysis_id": analysis_id, "analysis_hash": analysis_hash,
                    "mode": mode, "base_manifest_ref": base_ref,
                    "execution_fence": fence, "lineage_watermark": watermark,
                    "seed_kind": seed_kind, "seed_ref": seed.get("ref", {}),
                    "regenerate_node_ids": payload["regenerate_node_ids"],
                    "recheck_node_ids": payload["recheck_node_ids"],
                    "affected_episode_ids": payload["affected_episode_ids"],
                    "superseded_claim_refs": payload["superseded_claim_refs"],
                    "expires_at": expires_at,
                }))

        return {
            "analysis_id": analysis_id, "analysis_hash": analysis_hash,
            "mode": mode, "status": "COMPUTED", "lineage_watermark": watermark,
            "watermark_source": watermark_source, "impact": payload,
            "expires_at": expires_at, "replayed": False,
        }

    def _apply_reuse_rules(self, tenant_id: str, branch_id: str, result, seeds,
                           changed) -> None:
        """§4.2 的复用判定：上游变了，但本节点**依赖的那个字段**没变。

        判据只有一条可证明的：这个节点的每条入边都声明了它依赖下游的哪个
        字段（`field_path`），且那些字段一个都没变。声明不全就无法证明，
        保持 `REGENERATE`——不做"我猜够用"。

        两条落地规则：

        - `AUDIO_FROM_DIALOGUE`（`/dialogue` 未变）→ `REUSE`
          `REUSABLE_TEXT_UNCHANGED`；`trim_allowed=false` 时降 `RECHECK`
          `DURATION_DERIVED`（T09-03：时长不可裁，产物可能仍可用，交给 QC 判）。
        - 其余种类 → `REUSE` `REUSABLE_FIRST_FRAME_UNCHANGED`（T09-01 的反面：
          `/camera/angle` 未变则关键帧可复用的那一条）。
        """
        seed_keys = {s.key for s in seeds}
        for key, verdict in list(result.nodes.items()):
            if verdict.disposition != "REGENERATE":
                continue
            if verdict.reason_code != "UPSTREAM_REGENERATED":
                continue
            if key in seed_keys or key in changed:
                continue
            upstream = self._edges_to(tenant_id, branch_id, {key}).get(key, [])
            if not upstream:
                continue
            # 每条入边都得说清它依赖哪个字段；有一条说不清，REUSE 就证明不了。
            if any(edge.field_path is None for edge in upstream):
                continue
            if any(edge.field_path in changed.get(edge.source.key, set())
                   for edge in upstream):
                continue

            kinds = {edge.edge_kind for edge in upstream}
            if "AUDIO_FROM_DIALOGUE" in kinds:
                source = next(e.source for e in upstream
                              if e.edge_kind == "AUDIO_FROM_DIALOGUE")
                trim = self.scalar(tenant_id, source.service_id, source.object_id,
                                   source.revision, "/duration/trim_allowed") is True
                if trim:
                    verdict.disposition = "REUSE"
                    verdict.reason_code = "REUSABLE_TEXT_UNCHANGED"
                else:
                    verdict.disposition = "RECHECK"
                    verdict.reason_code = "DURATION_DERIVED"
                continue

            verdict.disposition = "REUSE"
            verdict.reason_code = "REUSABLE_FIRST_FRAME_UNCHANGED"

    def _episode_shots(self, tenant_id: str, branch_id: str,
                       episode_ids: list[str]) -> dict[str, list[Ref]]:
        out: dict[str, list[Ref]] = {}
        for episode in episode_ids:
            rows = self.conn.execute(
                "SELECT DISTINCT service_id, object_id, object_revision FROM node_ref"
                " WHERE tenant_id = ? AND branch_id = ? AND episode_id = ?"
                " AND service_id = 'S06'", (tenant_id, branch_id, episode)).fetchall()
            out[episode] = [Ref(r[0], r[1], r[2]) for r in rows]
        return out

    def _current_fence(self, tenant_id: str, branch_id: str,
                       manifest_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT execution_fence FROM manifest_head WHERE tenant_id = ?"
            " AND branch_id = ? AND manifest_id = ?",
            (tenant_id, branch_id, manifest_id)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _impact_payload(result, watermark: int) -> dict:
        """与 enterprise-change-plan.v2 的 `impact` 逐字对齐（C09-5 已登记）。

        `derived_adjustments` 是那个 schema 的**顶层**键而不是 `impact` 的键
        （`additionalProperties: false` 会拒掉它）——§2.3 行 342「不需要二次
        映射」与它在契约里的位置直接矛盾，已登记为 LNI-3 / C09-5b，不在此
        自行选边：这里按 §2.3 的行文保留该键，S07 侧会 422，正好让冲突显形。
        """
        regen = sorted(v.node_id for v in result.nodes.values()
                       if v.disposition == "REGENERATE")
        recheck = sorted(v.node_id for v in result.nodes.values()
                         if v.disposition == "RECHECK")
        reuse = []
        for key, verdict in sorted(result.nodes.items()):
            if verdict.disposition != "REUSE":
                continue
            reuse.append({"service_id": key[0], "id": key[1], "revision": key[2],
                          "content_hash": verdict.evidence.get("content_hash", "")})
        return {
            "regenerate_node_ids": regen,
            "recheck_node_ids": recheck,
            "reuse_artifact_refs": reuse,
            "affected_episode_ids": list(result.episode_ids),
            "superseded_claim_refs": [],
            "lineage_watermark": watermark,
            "derived_adjustments": list(result.derived_adjustments),
        }

    def _store_analysis(self, tenant_id: str, branch_id: str, series_id: str,
                        base_ref: dict, seed_kind: str, seed: dict,
                        request_hash: str, mode: str, status: str, fence: int,
                        watermark: int, watermark_source: dict, gap_ids,
                        payload: dict | None, analysis_hash: str | None, *,
                        result=None, analysis_id: str | None = None,
                        expires_at: str | None = None) -> str:
        # 同一 request_hash 永远对应同一个 analysis（§6.2 `ia_idempotent_uniq`）。
        # 回源补齐后重算时若不沿用旧 id，INSERT..ON CONFLICT 会保留旧行、
        # 而节点明细却写到新 id 下——查询时节点凭空消失。
        if analysis_id is None:
            existing = self.conn.execute(
                "SELECT analysis_id FROM impact_analysis WHERE tenant_id = ?"
                " AND request_hash = ?", (tenant_id, request_hash)).fetchone()
            analysis_id = existing[0] if existing else "ia_" + uuid.uuid4().hex[:24]
        now = _now()
        self.conn.execute(
            "INSERT INTO impact_analysis (tenant_id, analysis_id, branch_id, series_id,"
            " base_manifest_ref, execution_fence, seed_kind, seed_ref, request_hash,"
            " mode, status, lineage_watermark, watermark_source, gap_ids, result,"
            " analysis_hash, expires_at, created_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, request_hash) DO UPDATE SET"
            " status = excluded.status, mode = excluded.mode,"
            " result = excluded.result, analysis_hash = excluded.analysis_hash,"
            " gap_ids = excluded.gap_ids, completed_at = excluded.completed_at",
            (tenant_id, analysis_id, branch_id, series_id,
             json.dumps(base_ref, ensure_ascii=False, sort_keys=True), fence,
             seed_kind, json.dumps(seed, ensure_ascii=False, sort_keys=True),
             request_hash, mode, status, watermark,
             json.dumps(watermark_source, sort_keys=True),
             json.dumps(gap_ids) if gap_ids else None,
             json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None,
             analysis_hash, expires_at or _plus_seconds(ANALYSIS_TTL_SECONDS),
             now, now))
        if result is not None:
            self.conn.execute(
                "DELETE FROM impact_analysis_node WHERE tenant_id = ? AND analysis_id = ?",
                (tenant_id, analysis_id))
            for node in closure.serialize_nodes(result.nodes):
                self.conn.execute(
                    "INSERT INTO impact_analysis_node (tenant_id, analysis_id, node_id,"
                    " disposition, reason_code, path, evidence)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, analysis_id, node["node_id"], node["disposition"],
                     node["reason_code"], json.dumps(node["path"]),
                     json.dumps(node["evidence"], ensure_ascii=False, sort_keys=True)))
        return analysis_id

    # ---- 查询 ----------------------------------------------------------------

    def get_analysis(self, tenant_id: str, analysis_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM impact_analysis WHERE tenant_id = ? AND analysis_id = ?",
            (tenant_id, analysis_id)).fetchone()
        if row is None:
            # 跨租户与不存在同码（E28）。
            raise ServiceError(404, "ANALYSIS_NOT_FOUND", "analysis does not exist")
        nodes = self.conn.execute(
            "SELECT node_id, disposition, reason_code, path, evidence"
            " FROM impact_analysis_node WHERE tenant_id = ? AND analysis_id = ?"
            " ORDER BY node_id", (tenant_id, analysis_id)).fetchall()
        return {
            "analysis_id": row["analysis_id"],
            "analysis_hash": row["analysis_hash"],
            "mode": row["mode"],
            "status": row["status"],
            "branch_id": row["branch_id"],
            "base_manifest_ref": json.loads(row["base_manifest_ref"]),
            "execution_fence": row["execution_fence"],
            "lineage_watermark": row["lineage_watermark"],
            "watermark_source": json.loads(row["watermark_source"]),
            "gap_ids": json.loads(row["gap_ids"]) if row["gap_ids"] else [],
            "impact": json.loads(row["result"]) if row["result"] else None,
            "nodes": [{"node_id": r["node_id"], "disposition": r["disposition"],
                       "reason_code": r["reason_code"], "path": json.loads(r["path"]),
                       "evidence": json.loads(r["evidence"])} for r in nodes],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
        }

    def list_analyses(self, tenant_id: str, branch_id: str | None = None,
                      after: str | None = None, limit: int = 50) -> dict:
        sql = "SELECT analysis_id FROM impact_analysis WHERE tenant_id = ?"
        args: list = [tenant_id]
        if branch_id:
            sql += " AND branch_id = ?"
            args.append(branch_id)
        rows = self.conn.execute(
            sql + " ORDER BY created_at DESC LIMIT ?", [*args, min(limit, 200)]).fetchall()
        items = [self.get_analysis(tenant_id, r[0]) for r in rows]
        if after:
            items = [i for i in items if i["created_at"] > after]
        return {"items": items}

    def edges(self, tenant_id: str, *, target_ref: dict | None = None,
              kind: str | None = None, field_path: str | None = None,
              limit: int = 200) -> dict:
        sql = "SELECT * FROM lineage_edge WHERE tenant_id = ? AND valid_to IS NULL"
        args: list = [tenant_id]
        if target_ref:
            ref = parse_ref(target_ref, name="target_ref")
            sql += " AND target_service_id = ? AND target_id = ? AND target_revision = ?"
            args.extend([ref.service_id, ref.object_id, ref.revision])
        if kind:
            sql += " AND edge_kind = ?"
            args.append(kind)
        if field_path:
            sql += " AND field_path = ?"
            args.append(field_path)
        total = self.conn.execute("SELECT count(*) FROM (" + sql + ")", args).fetchone()[0]
        # §5.3：上限 2000，超过 413。分页会给出不自洽的依赖视图，所以直接拒。
        if total > 2000:
            raise ServiceError(413, "EDGE_LIST_TOO_LARGE",
                               f"{total} edges match; narrow with target_ref or kind")
        rows = self.conn.execute(sql + " LIMIT ?", [*args, min(limit, 2000)]).fetchall()
        return {"total": total, "items": [_render_edge(r) for r in rows]}

    def traverse(self, tenant_id: str, *, branch_id: str, ref: dict,
                 direction: str = "downstream", depth: int = 1) -> dict:
        root = parse_ref(ref, name="ref")
        cap = closure.MAX_DEPTH if direction == "downstream" else MAX_DEPTH_UP
        depth = max(1, min(depth, cap))
        seen = {root.key}
        frontier = {root.key}
        levels: list[list[dict]] = []
        for _ in range(depth):
            if not frontier:
                break
            edges = (self._edges_from(tenant_id, branch_id, frontier)
                     if direction == "downstream"
                     else self._edges_to(tenant_id, branch_id, frontier))
            step: list[dict] = []
            nxt: set[tuple[str, str, int]] = set()
            for group in edges.values():
                for edge in group:
                    other = edge.target if direction == "downstream" else edge.source
                    step.append({"edge_id": edge.edge_id,
                                 "edge_kind": edge.edge_kind, **other.to_dict()})
                    if other.key not in seen:
                        nxt.add(other.key)
            seen |= nxt
            frontier = nxt
            levels.append(step)
        return {"ref": root.to_dict(), "direction": direction,
                "depth": depth, "levels": levels}

    def watermarks(self, tenant_id: str, branch_id: str = "main") -> dict:
        rows = self.conn.execute(
            "SELECT * FROM projection_watermark WHERE tenant_id = ? AND branch_id = ?"
            " ORDER BY source_service", (tenant_id, branch_id)).fetchall()
        return {"branch_id": branch_id, "items": [
            {"source_service": r["source_service"],
             "applied_sequence": r["applied_sequence"],
             "source_head_sequence": r["source_head_sequence"],
             "observed_lag_ms": r["observed_lag_ms"],
             "lag_budget_ms": r["lag_budget_ms"],
             "degraded": r["observed_lag_ms"] > r["lag_budget_ms"],
             "updated_at": r["updated_at"]} for r in rows]}


def _value_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _json_or_none(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value else None


def _edge_of(row) -> Edge:
    return Edge(
        edge_id=row["edge_id"], edge_kind=row["edge_kind"],
        source=Ref(row["source_service_id"], row["source_id"],
                   row["source_revision"], row["source_hash"]),
        target=Ref(row["target_service_id"], row["target_id"],
                   row["target_revision"], row["target_hash"]),
        field_path=row["field_path"])


def _render_edge(row) -> dict:
    return {
        "edge_id": row["edge_id"], "edge_kind": row["edge_kind"],
        "branch_id": row["branch_id"],
        "source": {"service_id": row["source_service_id"], "id": row["source_id"],
                   "revision": row["source_revision"],
                   "content_hash": row["source_hash"]},
        "target": {"service_id": row["target_service_id"], "id": row["target_id"],
                   "revision": row["target_revision"],
                   "content_hash": row["target_hash"]},
        "field_path": row["field_path"], "origin": row["origin"],
        "valid_from": row["valid_from"], "valid_to": row["valid_to"],
        "created_at": row["created_at"],
    }
