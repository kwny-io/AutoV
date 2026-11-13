"""S43 内核。方案：`plans/S43-delivery.md`。

一句话边界：**S43 打包、签名、在获得实时授权后外发，并记录外部回执。**
它不判断内容好坏，不产生第二权威。

## 不写的东西（§1 末表）

| 事实 | 权威 | S43 的做法 |
|---|---|---|
| accepted master 指针 | S08 | 只读，发布前实时复查；从不写 |
| 媒体 bytes / hash | S28 | 只读元数据，签名前复核 |
| 渲染证据 | S42 | 引用；G3/G5 通过注入回调取 |
| 字幕 | S38 | 引用 |
| QC 结论 | S35–S38 | 引用；`decision` 原样搬运，不重算 |
| 策略判定 | S02 | 每次外发 `lookup` + `evaluate`；从不缓存 allow |
| 金额 | S23 | 只引用 cost point-in-time |
| 审计链 | S45 | 只发意图、只存 record_id |
| 渠道凭据 | 平台密钥管理 | 不持有；经连接器 |
| 客户合同 | 未定（§11 C1） | 只引用 |

## 三条结构性纪律的代码形态

1. **包不可变。** 建包之后只有 `state` / `gate_results` / `sealed_at` /
   `published_at` 会动。没有 `update_item`、没有 `update_manifest`，修正 =
   新 `package_id`（§1.2）。`seal` 只向 `DRAFTING` 写，第二次调用拿回冻结的
   manifest。
2. **`execution_fence` 与 `node_epoch` 是两列。** fence 属 run/branch，
   epoch 属单个 `logical_node`。`_gate_g1` 只比较 fence 与 fence，从不读、
   写、比较 epoch（AGENTS.md）。本服务不持有也不推进任何接受指针。
3. **门是 fail-closed 的。** 依赖 503 → 命令整体 503
   `DEPENDENCY_UNAVAILABLE`，并指出是哪道门，**不是 pass**。`deferred`
   （创建时的 G8）也绝不被读成通过：它只在 `deliverable` 的"可继续"判定里
   算数，外发前 G8 必须重新评估成 `allow`。

## 事务边界

创建与签名 / 外发与调用连接器都**不是**一个事务（§4.1 第 9–10 步、§4.3
第 4–6 步）：外部调用不能塞进数据库事务。代价是中间态（`DRAFTING`、
`IN_FLIGHT`、`UNKNOWN`），由状态机与幂等重投处理，而不是靠长事务假装原子。

## 外发开关

`channel.enabled` 默认 0（DDL），平台级 `external_publish_enabled` 构造参数
默认 False。两层都开才可能外发；任何一层没开都 503，不做"假装发过了"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event
from idempotency_sdk import canonical_request_hash

from .schema import (
    ATTEMPT_STATES,
    CANONICAL_VERSION,
    CANONICAL_VERSIONS,
    CHANNEL_KINDS,
    CONSUMERS,
    EVENT_CREATED,
    EVENT_FAILED,
    EVENT_MASTER_RENDERED,
    EVENT_PUBLISHED,
    EVENT_SEALED,
    EVENT_SNAPSHOT_COMMITTED,
    EVENT_SUBTITLE_READY,
    EVENT_VOICE_AUTH,
    FIXED_ROLES,
    GATE_IDS,
    GATE_RESULTS,
    ID_PREFIX,
    ITEM_ROLES,
    PACKAGE_STATES,
    PACKAGE_TYPES,
    SCHEMA,
    SCHEMA_VERSION,
    SEQUENCE_CREATED,
    SOURCE,
)

VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")


# ------------------------------------------------------------------ 工具

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _fmt(_now_dt())


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _hex64(value) -> str | None:
    """接受裸 64 位十六进制或 `sha256:` 前缀；其它一律不算摘要。"""
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def _artifact_id(value) -> str | None:
    """把 `"art_x"` / `{"id": "art_x"}` / `{"artifact_id": "art_x"}` 归一。"""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("artifact_id", "id"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
    return None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def stable_request_hash(body: dict) -> str:
    """命令幂等摘要：业务字段全进，trace 与时间戳不进。

    与 S37 / S41 同一份易变键清单。重投时 trace_id 变了不该被当成
    "同键不同 body"（AGENTS.md 要求 409 只给真正的不同 body）。
    """
    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    for key in ("context", "execution_context"):
        context = candidate.get(key)
        if isinstance(context, dict):
            candidate[key] = {k: v for k, v in context.items()
                              if k not in VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。

    已经处在事务里时**不再开第二个**（SQLite 没有嵌套事务）：内层只跟着
    外层的提交/回滚走。这样 `_settle_attempt` 这类"必须在调用方事务内"的
    函数，和被直接调用时也能自己开事务的辅助函数，共用同一套写法。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owned = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self.owned = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.owned:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class _AlreadySealed(Exception):
    """并发封包：另一个请求先拿到了 `DRAFTING → SEALED` 这条边。

    只在本进程内用作控制流，绝不外泄成 HTTP 错误——调用方应该拿到赢家的
    冻结清单（§7.2：同键重复 = 同一个结果）。
    """


# ------------------------------------------------------- manifest / hash

def canonical_item(item: dict) -> dict:
    """manifest 里的一项。**只放确定性字段**，顺序由调用方排序。"""
    return {
        "role": item["role"],
        "ordinal": int(item.get("ordinal") or 0),
        "artifact_ref": item.get("artifact_ref"),
        "content_hash": item.get("content_hash"),
        "byte_size": item.get("byte_size"),
        "mime": item.get("mime"),
        "locale": item.get("locale"),
        "external_ref": item.get("external_ref"),
    }


def build_manifest(package_id: str, items: list[dict]) -> dict:
    """§4.2 第 4–5 步：按 (role, ordinal) 排序，排除自身 manifest 项。

    `manifest` 项不存在于 `item` 表（它就是这份清单），所以这里天然不包含它
    ——§2.4 #1 的"排除在签名内容外"不是靠过滤实现的，是结构上不成立。
    """
    ordered = sorted((canonical_item(item) for item in items),
                     key=lambda entry: (entry["role"], entry["ordinal"]))
    return {"package_id": package_id, "items": ordered}


def manifest_hash_for(manifest: dict, canonical_version: str) -> str:
    """§4.2 第 5 步：`sha256(canonical_json(manifest) || canonical_version)`。

    规范化版本**拼在哈希输入里**：换实现就换 hash，旧签名自然失效，
    不会出现"同一清单在两版服务上算出不同 hash 却都叫 manifest_hash"。
    """
    return _sha256_hex(_jcs(manifest) + canonical_version)


def package_hash_for(package_id: str, package_type: str, episode_id,
                     branch_id, master_candidate_id, execution_fence,
                     node_epoch, items: list[dict]) -> str:
    """`package_hash`（S43-9）。**含 `package_id`**：否则两次合法的同内容
    交付会撞 `UNIQUE(tenant_id, package_hash)`。裸小写 hex，不带前缀。"""
    snapshot = {
        "package_id": package_id,
        "package_type": package_type,
        "episode_id": episode_id,
        "branch_id": branch_id,
        "master_candidate_id": master_candidate_id,
        # 两个坐标分别入快照，只用于"这是哪个包"的指纹；不参与任何比较。
        "execution_fence": int(execution_fence),
        "node_epoch": None if node_epoch is None else int(node_epoch),
        "items": sorted((canonical_item(item) for item in items),
                        key=lambda entry: (entry["role"], entry["ordinal"])),
    }
    return _sha256_hex(_jcs(snapshot))


# ------------------------------------------------------------ 门结果汇总

def _worst(results: dict) -> str:
    order = {"fail": 4, "deferred": 3, "pass": 2, "not_applicable": 1}
    return max(results.values(), key=lambda value: order.get(value, 0),
               default="not_applicable")


def deliverable_of(package_type: str, state: str,
                   gate_results: dict, gaps: list) -> bool:
    """`deliverable` = 能作为**正式交付**使用。

    `deferred` 在这里算"可继续"，因为 G8 在外发时会重新评估——这**不是**
    把它当 pass：`gate_results` 原样返回 `deferred`，消费方看得到。
    预览包与缺素材包永远 `deliverable=false`（§1.1）。
    """
    if package_type != "formal" or state != "SEALED":
        return False
    if gaps:
        return False
    return all(value in ("pass", "not_applicable", "deferred")
               for value in gate_results.values())


# ------------------------------------------------------------ 门

def _pass(detail: dict | None = None) -> dict:
    return {"result": "pass", "code": None, "detail": detail or {},
            "role": None}


def _fail(code: str, detail: dict | None = None,
          role: str | None = None) -> dict:
    return {"result": "fail", "code": code, "detail": detail or {},
            "role": role}


def _na(detail: dict | None = None) -> dict:
    return {"result": "not_applicable", "code": None, "detail": detail or {},
            "role": None}


# 每种包类型**必须**挡住创建的门的集合（§1.1 / §2.3）。不在集合里的门
# 仍然可以被评估（例如预览包的 G5），结果只是记录，不挡创建。
BLOCKING_GATES = {
    "formal": tuple(GATE_IDS),
    "preview": ("G2", "G3", "G6"),
    "missing_material": (),
}

# 未配置的上游不是 `None` 而是必然 503 的桩：少写一个 `if source is None`
# 就会得到"没查 = 通过"，这里从结构上堵住。
# 桩上留 `_unavailable_code`：`/health/readiness` 靠它如实报出"哪些门现在
# 根本判不了"，而不是去猜哪个依赖没配。
def _missing_source(code: str, why: str):
    def _stub(*_args, **_kwargs):
        raise ServiceError(503, code, why)
    _stub._unavailable_code = code
    return _stub


class DeliveryService:
    """S43 的写路径。

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox.__init__` 都含
    `executescript`，而它会隐式提交。两者都必须在任何 `BEGIN IMMEDIATE`
    之前建一次，否则命令事务会被悄悄提交，ROLLBACK 失效。
    """

    def __init__(self, conn: sqlite3.Connection, *,
                 accepted_master_source=None, artifact_source=None,
                 timeline_source=None, candidate_source=None,
                 qc_source=None, subtitle_source=None, rights_source=None,
                 policy_source=None, memory_source=None, binding_source=None,
                 cost_source=None, audit_sink=None, signer=None,
                 connector=None, receipt_verifier=None,
                 external_publish_enabled: bool = False,
                 unknown_ttl_hours: float = 24.0,
                 signer_key_ref: dict | None = None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)

        self.accepted_master_source = accepted_master_source or _missing_source(
            "ACCEPTED_MASTER_SOURCE_UNAVAILABLE",
            "S08 is the only authority on accepted masters; S43 will not "
            "assume one is accepted (plan §2.3 G1)")
        self.artifact_source = artifact_source or _missing_source(
            "ARTIFACT_SOURCE_UNAVAILABLE",
            "S43 will not seal a package whose artifacts it cannot verify "
            "(plan §2.3 G2)")
        self.timeline_source = timeline_source or _missing_source(
            "TIMELINE_SOURCE_UNAVAILABLE",
            "S43 will not claim a timeline is complete without S41 "
            "(plan §2.3 G3)")
        self.candidate_source = candidate_source or _missing_source(
            "CANDIDATE_SOURCE_UNAVAILABLE",
            "S42 is design_only; deliverability cannot be verified "
            "(plan §2.3 G3/G5, S43-4)")
        self.qc_source = qc_source or _missing_source(
            "QC_SOURCE_UNAVAILABLE",
            "missing QC evidence is not a pass (plan §2.3 G4)")
        self.subtitle_source = subtitle_source or _missing_source(
            "SUBTITLE_SOURCE_UNAVAILABLE",
            "S38 is design_only; subtitle freshness cannot be verified "
            "(plan §2.3 G6, S43-4)")
        self.rights_source = rights_source or _missing_source(
            "RIGHTS_SOURCE_UNAVAILABLE",
            "S43 will not publish without a live rights check "
            "(plan §2.3 G7)")
        self.policy_source = policy_source or _missing_source(
            "POLICY_SOURCE_UNAVAILABLE",
            "S02 is the only authority for publish authorization; S43 never "
            "caches or invents an allow (plan §2.2)")
        self.memory_source = memory_source or _missing_source(
            "MEMORY_SOURCE_UNAVAILABLE",
            "the formal package requires a memory checkpoint (plan §2.4)")
        self.binding_source = binding_source or _missing_source(
            "BINDING_SOURCE_UNAVAILABLE",
            "the formal package requires asset bindings (plan §2.4)")
        self.cost_source = cost_source or _missing_source(
            "COST_SOURCE_UNAVAILABLE",
            "the formal package requires a cost point-in-time (plan §2.4)")
        self.audit_sink = audit_sink or _missing_source(
            "AUDIT_SINK_UNAVAILABLE",
            "S43 will not claim an audit record exists when it could not "
            "reach S45")
        self.signer = signer or _missing_source(
            "SIGNING_UNAVAILABLE",
            "signing requires an external KMS/HSM (plan §2.1, §11 C6)")
        self.connector = connector or _missing_source(
            "CONNECTOR_UNAVAILABLE",
            "external delivery must go through a channel connector "
            "(plan §4.3)")
        self.receipt_verifier = receipt_verifier or _missing_source(
            "RECEIPT_VERIFIER_UNAVAILABLE",
            "S43 will not accept an unverified receipt (plan §4.6)")

        # 平台级外发开关，默认关闭（§10 末条：双层关闭）。
        self.external_publish_enabled = bool(external_publish_enabled)
        self.unknown_ttl = timedelta(hours=float(unknown_ttl_hours))
        self.signer_key_ref = dict(signer_key_ref or {"algorithm": "ed25519"})
        self.metrics: dict[str, int] = {}

    def unavailable_dependencies(self) -> list[str]:
        """还没配的上游（按注入的桩判定）。

        `/health/readiness` 用它：一个上游没配就有一道门判不了，formal 包
        必然 503。把这种状态报成 ready，编排层要等到第一次派单才知道。
        """
        names = ("accepted_master_source", "artifact_source",
                 "timeline_source", "candidate_source", "qc_source",
                 "subtitle_source", "rights_source", "policy_source",
                 "memory_source", "binding_source", "cost_source",
                 "audit_sink", "signer", "connector", "receipt_verifier")
        return sorted({getattr(getattr(self, name), "_unavailable_code", None)
                       for name in names} - {None})

    # ------------------------------------------------------------ 工具
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _dep(self, gate: str, upstream: str, call):
        """上游不可达 → 503 `DEPENDENCY_UNAVAILABLE`，**绝不用缓存结论兜底**。

        §2.3：「任何一项无法判定（依赖 503）→ 整体失败，不是门通过」。
        4xx 原样抛给调用方，由各门自己解释（例如 S28 的 404 折成
        `ARTIFACT_MISSING`）。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{gate}: {upstream} is unreachable; a gate that cannot be "
                f"evaluated is not a pass ({exc})",
                retryable=True,
                details={"gate": gate, "upstream": upstream,
                         "upstream_code": "UNREACHABLE"}) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    f"{gate}: {upstream} answered {exc.status} "
                    f"({exc.code}); a gate that cannot be evaluated is not a "
                    "pass",
                    retryable=True,
                    details={"gate": gate, "upstream": upstream,
                             "upstream_code": exc.code}) from exc
            raise

    # --------------------------------------------------------- 包与渠道
    def _package(self, tenant_id: str, package_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM package WHERE tenant_id=? AND package_id=?",
            (tenant_id, package_id)).fetchone())
        if row is None:
            # 跨租户与不存在返回同一个 404，不泄漏存在性（E28-S43-01）。
            raise ServiceError(404, "PACKAGE_NOT_FOUND",
                               f"no delivery package {package_id!r}")
        return row

    def _items(self, tenant_id: str, package_id: str) -> list[dict]:
        """`artifact_ref` / `external_ref` 落库时是 JCS 文本，读回来必须是
        对象。否则 G2 / G4 拿到的"引用"是字符串：`ref["id"]` 直接抛
        `TypeError`，而 seal 的任何一次复查都会走到这里。
        """
        rows = self.conn.execute(
            "SELECT * FROM item WHERE tenant_id=? AND package_id=?"
            " ORDER BY role, ordinal", (tenant_id, package_id)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["artifact_ref"] = _loads(item.get("artifact_ref"), None)
            item["external_ref"] = _loads(item.get("external_ref"), None)
            items.append(item)
        return items

    def _channel(self, tenant_id: str, channel_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM channel WHERE tenant_id=? AND channel_id=?",
            (tenant_id, channel_id)).fetchone())
        if row is None:
            raise ServiceError(404, "CHANNEL_NOT_FOUND",
                               f"no distribution channel {channel_id!r}")
        return row

    # ------------------------------------------------------------ 门实现
    def _gate_g1(self, tenant_id: str, draft: dict, master: dict) -> dict:
        """接受门。**只比较 fence 与 fence**；`node_epoch` 不参与。"""
        manifest_id = draft.get("manifest_ref_id")
        node_id = master.get("node_id")
        if not manifest_id or not node_id:
            return _fail("MASTER_NOT_ACCEPTED", {
                "reason": "master_ref needs node_id and context.manifest_ref",
                "has_node_id": bool(node_id),
                "has_manifest_ref": bool(manifest_id)})
        data = self._dep("G1", "S08",
                         lambda: self.accepted_master_source(tenant_id,
                                                             manifest_id))
        header = data.get("manifest") or {}
        header_fence = _int(header.get("execution_fence"))
        if header_fence is None or header_fence != int(draft["execution_fence"]):
            return _fail("STALE_FENCE", {
                "package_fence": int(draft["execution_fence"]),
                "manifest_fence": header_fence})
        pointers = (data.get("pointers") or {}).get("nodes") or []
        for pointer in pointers:
            if str(pointer.get("node_id")) != str(node_id):
                continue
            accepted = pointer.get("accepted_artifact_ref")
            if str(accepted) == str(master.get("id")):
                return _pass({"node_id": node_id,
                              "accepted_at_revision":
                                  pointer.get("accepted_at_revision")})
            return _fail("MASTER_NOT_ACCEPTED", {
                "node_id": node_id, "accepted": accepted,
                "referenced": master.get("id")})
        return _fail("MASTER_NOT_ACCEPTED",
                     {"node_id": node_id, "reason": "no accepted pointer"})

    def _gate_g2(self, tenant_id: str, items: list[dict], *,
                 allow_missing: bool = False,
                 missing: list | None = None) -> dict:
        """素材门：存在 + hash 一致。**不下载字节**。

        `allow_missing` 只给 `missing_material` 包用：§2.3「缺素材包只要求
        G2 覆盖已存在的项」，§1.1「缺素材清单本身是正常产品」。此时"找不到"
        记进 `missing`（成为包的 `gaps`），而不是把包挡在 DRAFTING。hash
        不一致仍然失败——那是内容被换掉，不是"还差这个"。
        """
        checked = 0
        total_bytes = 0
        for item in items:
            ref = item.get("artifact_ref") or {}
            if not ref:
                continue

            def _absent(detail: dict):
                if not allow_missing:
                    return _fail("ARTIFACT_MISSING", detail,
                                 role=item["role"])
                if missing is not None:
                    missing.append({"role": item["role"],
                                    "code": "ARTIFACT_MISSING",
                                    "detail": detail, "gate": "G2"})
                return None

            try:
                meta = self._dep(
                    "G2", "S28",
                    lambda ref=ref: self.artifact_source(tenant_id, ref))
            except ServiceError as exc:
                if exc.status in (404, 410):
                    failed = _absent({"artifact_id": ref.get("id"),
                                      "upstream_code": exc.code})
                    if failed is not None:
                        return failed
                    continue
                raise
            if not isinstance(meta, dict):
                failed = _absent({"artifact_id": ref.get("id")})
                if failed is not None:
                    return failed
                continue
            if str(meta.get("state") or "").lower() in ("deleted", "purged",
                                                        "expired"):
                failed = _absent({"artifact_id": ref.get("id"),
                                  "state": meta.get("state")})
                if failed is not None:
                    return failed
                continue
            if meta.get("permission_filtered") is True:
                # 上游按权限过滤了：不猜测存在性，按缺失处理（E28-S43-02）。
                failed = _absent({"artifact_id": ref.get("id"),
                                  "reason": "permission_filtered"})
                if failed is not None:
                    return failed
                continue
            expected = _hex64(item.get("content_hash"))
            actual = _hex64(meta.get("content_hash"))
            if expected and actual and expected != actual:
                return _fail("ARTIFACT_HASH_MISMATCH",
                             {"artifact_id": ref.get("id"),
                              "expected": expected, "actual": actual},
                             role=item["role"])
            checked += 1
            size = _int(meta.get("size_bytes"))
            if size is None:
                size = _int(item.get("byte_size"))
            if size:
                total_bytes += size
        return _pass({"artifacts": checked, "total_bytes": total_bytes})

    def _gate_g3(self, tenant_id: str, draft: dict, master: dict,
                 candidate: dict | None) -> dict:
        """完整性门：timeline 可正式交付 + 候选 deliverable + 无 stale 输入。"""
        if draft["package_type"] == "formal":
            timeline_ref = draft.get("timeline_ref") or {}
            if not timeline_ref.get("id") or timeline_ref.get("revision") is None:
                return _fail("INCOMPLETE_TIMELINE",
                             {"reason": "formal packages need timeline_ref "
                                        "with id and revision"})
            report = self._dep(
                "G3", "S41",
                lambda: self.timeline_source(tenant_id, timeline_ref))
            if not (report.get("eligible") is True
                    and report.get("formal_master_eligible") is True):
                return _fail("INCOMPLETE_TIMELINE", {
                    "eligible": report.get("eligible"),
                    "formal_master_eligible":
                        report.get("formal_master_eligible"),
                    "state": report.get("state")})
        if candidate is None:
            return _na({"candidate": None})
        evidence = self._dep(
            "G3", "S42", lambda: self.candidate_source(tenant_id, candidate))
        stale = evidence.get("stale_inputs") or []
        if evidence.get("deliverable") is not True or stale:
            return _fail("NOT_DELIVERABLE", {
                "candidate_id": candidate.get("id"),
                "deliverable": evidence.get("deliverable"),
                "stale_inputs": stale})
        return _pass({"candidate_id": candidate.get("id")})

    def _gate_g4(self, tenant_id: str, items: list[dict]) -> dict:
        """QC 门。**结论原样搬运**，只用它回答"有没有阻断"。"""
        reports = []
        for item in items:
            ref = item.get("external_ref") or {}
            if item["role"] != "qc_evidence" or not ref.get("report_id"):
                continue
            reports.append(ref)
        if not reports:
            return _na({"reports": 0})
        for ref in reports:
            report = self._dep(
                "G4", "S35-S38", lambda ref=ref: self.qc_source(tenant_id, ref))
            decision = report.get("decision")
            blocking = _int(report.get("blocking_count")) or 0
            # `decision` 是上游的词（S35–S37 用 pass/repair/human_review/
            # abstain）。**只有明确的 `pass` 放行**；缺失或别的值都不放行
            # ——"没读懂结论"不是通过。
            if blocking > 0 or decision != "pass":
                return _fail("QC_BLOCKING_ISSUE", {
                    "report_id": ref.get("report_id"),
                    "service_id": ref.get("service_id"),
                    "decision": decision,
                    "blocking_count": blocking},
                    role="qc_evidence")
        return _pass({"reports": len(reports)})

    def _gate_g5(self, tenant_id: str, candidate: dict | None) -> dict:
        """响度/规格门。证据来自候选（S42）；没有证据不是 pass。"""
        if candidate is None:
            return _na({"candidate": None})
        evidence = self._dep(
            "G5", "S42", lambda: self.candidate_source(tenant_id, candidate))
        loudness = str(evidence.get("loudness_gate") or "").upper()
        spec_ok = evidence.get("master_spec_ok")
        if loudness not in ("PASSED", "PASS") or spec_ok is False:
            return _fail("LOUDNESS_GATE_FAILED", {
                "loudness_gate": evidence.get("loudness_gate"),
                "master_spec_ok": spec_ok})
        return _pass({"loudness_gate": loudness})

    def _gate_g6(self, tenant_id: str, draft: dict,
                 items: list[dict]) -> dict:
        """字幕门：要求的每个 locale 都有字幕，且 revision 与 master 一致。"""
        timeline_revision = _int((draft.get("timeline_ref") or {})
                                 .get("revision"))
        locales = list(draft.get("requested_locales") or [])
        if not locales:
            locales = sorted({item.get("locale") for item in items
                              if item["role"] == "subtitle"
                              and item.get("locale")})
        if not locales:
            return _na({"locales": []})
        for locale in locales:
            query = {"target_locale": locale,
                     "timeline_id": (draft.get("timeline_ref") or {}).get("id"),
                     "timeline_revision": timeline_revision}
            try:
                found = self._dep(
                    "G6", "S38",
                    lambda query=query: self.subtitle_source(tenant_id, query))
            except ServiceError as exc:
                if exc.status in (404, 410):
                    return _fail("SUBTITLE_MISSING", {"locale": locale},
                                 role="subtitle")
                raise
            tracks = found.get("tracks") if isinstance(found, dict) else None
            track = found
            if isinstance(found, dict) and isinstance(tracks, list):
                if not tracks:
                    return _fail("SUBTITLE_MISSING", {"locale": locale},
                                 role="subtitle")
                track = tracks[0]
            if not isinstance(track, dict) or not (
                    track.get("artifact_ref") or track.get("artifact")):
                return _fail("SUBTITLE_MISSING", {"locale": locale},
                             role="subtitle")
            found_revision = _int(track.get("timeline_revision"))
            if timeline_revision is not None and found_revision is not None \
                    and found_revision != timeline_revision:
                return _fail("SUBTITLE_STALE", {
                    "locale": locale,
                    "subtitle_revision": found_revision,
                    "master_revision": timeline_revision}, role="subtitle")
        return _pass({"locales": locales})

    def _gate_g7(self, tenant_id: str, voice_refs: list[dict]) -> dict:
        """权利门。**每个 voice 实时问 S15**，读本地投影不算数。"""
        if not voice_refs:
            return _na({"voices": 0})
        result = self._dep(
            "G7", "S15",
            lambda: self.rights_source(tenant_id, {"voice_refs": voice_refs}))
        if result.get("allowed") is not True:
            return _fail("RIGHTS_REVOKED",
                         {"denied": result.get("denied") or []})
        return _pass({"voices": len(voice_refs)})

    def _gate_g10(self, items: list[dict]) -> dict:
        """清单门：formal 必须逐项覆盖 docs/04 §5 的固定内容（§2.4）。"""
        present = {item["role"] for item in items}
        missing = [role for role in FIXED_ROLES if role not in present]
        if missing:
            return _fail("MANIFEST_INCOMPLETE", {"missing_roles": missing})
        return _pass({"fixed_roles": list(FIXED_ROLES)})



    def _gate_g9(self, tenant_id: str, package_id: str) -> dict:
        """签名门：formal 必须有**验过**的签名。"""
        row = _row(self.conn.execute(
            "SELECT verified, verify_error FROM integrity_signature "
            "WHERE tenant_id=? AND package_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id, package_id)).fetchone())
        if row is None:
            return _fail("SIGNATURE_INVALID", {"reason": "no signature"})
        if int(row.get("verified") or 0) != 1:
            return _fail("SIGNATURE_INVALID",
                         {"reason": row.get("verify_error") or "unverified"})
        return _pass({})

    # ---------------------------------------------------------- 固定内容
    def _collect_fixed(self, tenant_id: str, draft: dict) -> list[dict]:
        """§2.4 的固定内容。**取不到的记为缺口**，不是"留空"。

        每一项都带 `external_ref`（不是 S28 的 artifact）：这些内容归上游，
        S43 只引用。formal 缺任何一项都会被 G10（`MANIFEST_INCOMPLETE`）挡住。
        """
        items: list[dict] = []
        series_id = draft["series_id"]
        episode_id = draft.get("episode_id")

        def _add(role: str, ref: dict, *, code: str) -> None:
            items.append({"role": role, "ordinal": 0, "artifact_ref": None,
                          "content_hash": None, "byte_size": None,
                          "mime": None, "locale": None, "external_ref": ref,
                          "_gap_code": code})

        try:
            memory = self._dep("G10", "S16", lambda: self.memory_source(
                tenant_id, {"series_id": series_id,
                            "branch_id": draft.get("branch_id"),
                            "minimum_checkpoint": 0}))
            _add("memory_checkpoint", {
                "service_id": "S16", "checkpoint": memory.get("checkpoint"),
                "checkpoint_hash": memory.get("checkpoint_hash")},
                code="MEMORY_CHECKPOINT_MISSING")
        except ServiceError as exc:
            if exc.status != 503:
                raise
            items.append({"role": "memory_checkpoint", "ordinal": 0,
                          "artifact_ref": None, "content_hash": None,
                          "byte_size": None, "mime": None, "locale": None,
                          "external_ref": None,
                          "_gap_code": "MEMORY_CHECKPOINT_MISSING",
                          "_gap_detail": "S16 unavailable"})
        if episode_id:
            try:
                bindings = self._dep("G10", "S09", lambda: self.binding_source(
                    tenant_id, {"binding_set_id": draft.get("series_id"),
                                "revision": draft.get("binding_revision", 0)}))
                _add("asset_bindings", {
                    "service_id": "S09", "binding_set_id":
                        bindings.get("binding_set_id", series_id),
                    "revision": bindings.get("revision")},
                    code="ASSET_BINDINGS_MISSING")
            except ServiceError as exc:
                if exc.status != 503:
                    raise
                items.append({"role": "asset_bindings", "ordinal": 0,
                              "artifact_ref": None, "content_hash": None,
                              "byte_size": None, "mime": None, "locale": None,
                              "external_ref": None,
                              "_gap_code": "ASSET_BINDINGS_MISSING",
                              "_gap_detail": "S09 unavailable"})
            try:
                cost = self._dep("G10", "S23", lambda: self.cost_source(
                    tenant_id, episode_id))
                _add("cost_snapshot", {"service_id": "S23",
                                       "episode_id": episode_id,
                                       "as_of": cost.get("as_of"),
                                       "currencies": cost.get("currencies")},
                     code="COST_SNAPSHOT_MISSING")
            except ServiceError as exc:
                if exc.status != 503:
                    raise
                items.append({"role": "cost_snapshot", "ordinal": 0,
                              "artifact_ref": None, "content_hash": None,
                              "byte_size": None, "mime": None, "locale": None,
                              "external_ref": None,
                              "_gap_code": "COST_SNAPSHOT_MISSING",
                              "_gap_detail": "S23 unavailable"})
        return items

    # ------------------------------------------------------------ 门编排
    def _evaluate(self, tenant_id: str, draft: dict,
                  items: list[dict]) -> dict:
        """创建时的门。返回 `{gate_id: {...}}`。

        每道门都留一条可读结果，而不是"提前 return"：`gate_results` 里缺
        的那道门会被消费方读成通过，那正是 E37-S43-04 要防的事。
        """
        package_type = draft["package_type"]
        blocking = set(BLOCKING_GATES[package_type])
        results: dict[str, dict] = {}
        # 缺素材包：G2 只覆盖已存在的项，缺的逐条记进 gaps（E37-S43-01）。
        # 挂在 draft 上而不是 self 上：service 可能被多个请求共用。
        draft["g2_missing"] = []

        def _run(gate: str, fn) -> None:
            if gate not in blocking:
                results[gate] = _na({"skipped": "not blocking for "
                                                f"{package_type}"})
                return
            results[gate] = fn()

        master = draft.get("master")
        candidate = draft.get("candidate")

        _run("G1", lambda: self._gate_g1(tenant_id, draft, master)
             if master else _na({"master": None}))
        if package_type == "missing_material":
            # 缺素材包仍然要**真查** S28：不查就没法告诉客户"还差什么"。
            # 但「查不到」不是门失败，而是清单内容（§2.3）。
            results["G2"] = self._gate_g2(
                tenant_id, items, allow_missing=True,
                missing=draft["g2_missing"])
        else:
            _run("G2", lambda: self._gate_g2(tenant_id, items))
        _run("G3", lambda: self._gate_g3(tenant_id, draft, master, candidate))
        _run("G4", lambda: self._gate_g4(tenant_id, items))
        _run("G5", lambda: self._gate_g5(tenant_id, candidate))
        _run("G6", lambda: self._gate_g6(tenant_id, draft, items))
        _run("G7", lambda: self._gate_g7(tenant_id,
                                         draft.get("voice_refs") or []))
        # G8 是策略门：创建时不判定（§6.1 行 612）。
        results["G8"] = {"result": "deferred", "code": None,
                         "detail": {"reason": "evaluated at publish"},
                         "role": None}
        # G9 在签名之后才有东西可判（`seal` 会覆盖它）。
        results["G9"] = {"result": "deferred", "code": None,
                         "detail": {"reason": "no signature yet"}, "role": None}
        _run("G10", lambda: self._gate_g10(items))
        return results

    def _gaps(self, gates: dict, dropped: list[dict]) -> list[dict]:
        """`BLOCKED` 的全部可观测内容（§4.1 第 7 步、§6.1）。

        元素形状 `{role, code, detail, gate}` 是本实现自定的（S43-10）。
        """
        gaps = []
        for gate_id in GATE_IDS:
            gate = gates.get(gate_id) or {}
            if gate.get("result") != "fail":
                continue
            gaps.append({"role": gate.get("role"), "code": gate.get("code"),
                         "detail": gate.get("detail") or {}, "gate": gate_id})
        for entry in dropped:
            code = entry.get("_gap_code")
            gaps.append({"role": entry.get("role"),
                         "code": code or "ARTIFACT_MISSING",
                         "detail": {"reason": entry.get("_gap_detail")
                                    or "no verifiable content reference"},
                         "gate": "G10" if code else "G2"})
        return gaps

    # ------------------------------------------------------------ 创建
    def create(self, tenant_id: str, command_id: str, body: dict,
               request_hash: str) -> dict:
        """`POST /delivery-packages`（§4.1）。

        外部依赖调用在事务**之前**（门评估）与事务**之后**（签名），数据库
        事务里只有本地事实 + outbox（§4.1 第 9–10 步）。
        """
        draft, items, dropped, gates = self._prepare(tenant_id, body)
        gaps = self._gaps(gates, dropped)
        # 缺素材包：G2 没失败的项里也可能有"找不到"的（§2.3），已经记在
        # `draft["g2_missing"]`。它们不是失败门，是包的**内容**——正是客户
        # 要看的「还差什么」，所以并入 gaps 而 state 仍可封包（E37-S43-01）。
        for entry in draft.get("g2_missing") or []:
            if entry not in gaps:
                gaps.append(entry)
        blocking_failed = any(gates[gate]["result"] == "fail"
                             for gate in BLOCKING_GATES[draft["package_type"]])
        draft["state"] = "BLOCKED" if blocking_failed else "DRAFTING"
        draft["gaps"] = gaps
        draft["gate_results"] = {gate: gates[gate]["result"]
                                 for gate in GATE_IDS}
        draft["items"] = items

        with Transaction(self.conn):
            self._insert_package(draft)
            self._emit_created(tenant_id, draft, command_id)
            result = self._view(tenant_id, draft["package_id"])
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))

        if draft["state"] == "DRAFTING":
            # 签名是事务外的第二步（§4.1 第 10 步）。不可用就停在
            # DRAFTING，把 202 + retryable 交给调用方——不假装封好了。
            try:
                # audit=True：E39-S43-01 要求**每次** seal 都有 S45 意图。
                # 创建时顺带封包也是一次 seal，不能因为"同一条命令"就省掉
                # ——审计要回答"这个包什么时候被封的、谁触发的"，而不是
                # "调用方按了几次按钮"。
                self.seal(tenant_id, command_id, draft["package_id"],
                          request_hash, audit=True)
            except ServiceError as exc:
                # 包已经提交了：这里**不能**再抛 4xx，否则调用方拿到错误、
                # 库里却留着一个包，同 command 重投还会命中那条记录。
                # 封不上就把原因如实带回去，状态仍是 DRAFTING。
                return {**self._view(tenant_id, draft["package_id"]),
                        "retryable": exc.status >= 500,
                        "seal_error": exc.code,
                        "seal_error_detail": exc.details}
        return {**self._view(tenant_id, draft["package_id"]),
                "retryable": False}

    def _prepare(self, tenant_id: str, body: dict):
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
        context = body.get("context")
        if context is None:
            context = body.get("execution_context")
        if not isinstance(context, dict):
            raise ServiceError(422, "CONTEXT_REQUIRED",
                               "execution context is required")
        for field in ("project_id", "series_id", "execution_fence"):
            if context.get(field) in (None, ""):
                raise ServiceError(422, "CONTEXT_INVALID",
                                   f"context.{field} is required")
        # `execution-context.v2` 里 `manifest_ref` 是对象。传字符串时下面
        # `(context.get("manifest_ref") or {}).get("id")` 会 AttributeError
        # 冒成 500 —— "请求体形状不对"不是服务器错误。
        if not isinstance(context.get("manifest_ref", {}), dict):
            raise ServiceError(422, "CONTEXT_INVALID",
                               "context.manifest_ref must be an object")

        package_type = body.get("package_type")
        # §1.1 末条：不存在"默认 formal"。
        if package_type is None:
            raise ServiceError(422, "PACKAGE_TYPE_REQUIRED",
                               "package_type is required; there is no "
                               "implicit formal default")
        if package_type not in PACKAGE_TYPES:
            raise ServiceError(422, "INVALID_PACKAGE_TYPE",
                               "package_type must be one of "
                               + ", ".join(PACKAGE_TYPES))

        episode_id = body.get("episode_id") or context.get("episode_id")
        branch_id = body.get("branch_id") or context.get("branch_id")
        if body.get("episode_id") and context.get("episode_id") \
                and body["episode_id"] != context["episode_id"]:
            raise ServiceError(422, "CONTEXT_INVALID",
                               "episode_id disagrees with the signed context")
        if body.get("branch_id") and context.get("branch_id") \
                and body["branch_id"] != context["branch_id"]:
            raise ServiceError(422, "CONTEXT_INVALID",
                               "branch_id disagrees with the signed context")
        if package_type in ("formal", "preview") and not episode_id:
            # S43-20：formal/preview 按集寻址（G3/G4/G6 与 cost 都用 episode）。
            raise ServiceError(422, "EPISODE_REQUIRED",
                               f"{package_type} packages are episode-scoped; "
                               "series-level delivery is an open decision "
                               "(S43-20)")

        master = self._master_ref(body)
        if package_type in ("formal", "preview"):
            if not master or not master.get("id"):
                raise ServiceError(422, "MASTER_REQUIRED",
                                   f"{package_type} packages require a master "
                                   "reference")
            if not master.get("node_id"):
                # S43-3：S08 只有 node 级接受指针。没有 node_id 就复查不了
                # G1，缺了是 422 而不是"跳过 G1"。
                raise ServiceError(
                    422, "MASTER_NODE_REQUIRED",
                    "master reference needs node_id so G1 can compare the "
                    "node-level accepted pointer in S08 (S43-3)")

        node_epoch = _int(context.get("node_epoch"))
        if node_epoch is None and master is not None:
            node_epoch = _int(master.get("node_epoch"))

        draft = {
            # tenant_id 只来自签名过的 `X-Tenant-Id`（API 层传进来），
            # 请求体里的同名/嵌套字段一律不采信，只用于冲突检测。
            "tenant_id": tenant_id,
            "package_id": _new_id("package"),
            "project_id": str(context["project_id"]),
            "series_id": str(context["series_id"]),
            "episode_id": episode_id,
            "branch_id": branch_id,
            "package_type": package_type,
            "purpose": body.get("purpose"),
            "home_cell": str(context.get("home_cell") or "unknown"),
            "requested_locales": list(body.get("requested_locales") or []),
            "voice_refs": list(body.get("voice_refs") or []),
            "candidate": ({"id": str(body["master_candidate_id"])}
                          if body.get("master_candidate_id") else None),
            "master_candidate_id": body.get("master_candidate_id"),
            "master": master,
            "timeline_ref": body.get("timeline_ref"),
            "contract_ref": body.get("contract_ref"),
            "manifest_ref_id": (context.get("manifest_ref") or {}).get("id"),
            # 两个坐标分两列（AGENTS.md）。epoch 只是引用，不参与比较。
            "execution_fence": int(context["execution_fence"]),
            "node_epoch": node_epoch,
            "created_by": context.get("actor")
                          or {"principal_id": "unknown",
                              "principal_type": "service"},
            "canonical_version": CANONICAL_VERSION,
        }

        supplied = self._input_items(body.get("items") or [], package_type)
        fixed = self._collect_fixed(tenant_id, draft)
        combined = supplied + fixed
        dropped = [entry for entry in combined
                   if entry.get("artifact_ref") is None
                   and entry.get("external_ref") is None]
        kept = [entry for entry in combined if entry not in dropped]

        gates = self._evaluate(tenant_id, draft, kept)
        return draft, kept, dropped, gates

    def _master_ref(self, body: dict) -> dict | None:
        raw = body.get("master_ref")
        if raw is None:
            artifact_id = _artifact_id(body.get("master_artifact_id"))
            if artifact_id is None:
                return None
            return {"id": artifact_id,
                    "node_id": body.get("master_node_id"),
                    "revision": body.get("master_revision"),
                    "content_hash": _hex64(body.get("master_content_hash"))}
        if isinstance(raw, str):
            return {"id": raw, "node_id": body.get("master_node_id")}
        if not isinstance(raw, dict):
            raise ServiceError(422, "MASTER_REQUIRED",
                               "master_ref must be an object")
        ref = dict(raw)
        ref["id"] = _artifact_id(ref)
        ref["node_id"] = ref.get("node_id") or body.get("master_node_id")
        return ref

    def _input_items(self, raw_items, package_type: str) -> list[dict]:
        if not isinstance(raw_items, list):
            raise ServiceError(422, "ITEMS_INVALID", "items must be an array")
        items = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ServiceError(422, "ITEMS_INVALID",
                                   f"items[{index}] must be an object")
            role = raw.get("role")
            if role not in ITEM_ROLES:
                # C-S43-04：不认识的 role 拒绝，不静默忽略。
                raise ServiceError(422, "UNKNOWN_ITEM_ROLE",
                                   f"items[{index}].role {role!r} is not a "
                                   "known role")
            artifact = raw.get("artifact_ref")
            if isinstance(artifact, str):
                artifact = {"id": artifact}
            if isinstance(artifact, dict):
                if not artifact.get("id"):
                    raise ServiceError(422, "ITEMS_INVALID",
                                       f"items[{index}].artifact_ref needs "
                                       "an id")
                artifact = {k: v for k, v in artifact.items()
                            if v is not None}
            elif artifact is not None:
                raise ServiceError(422, "ITEMS_INVALID",
                                   f"items[{index}].artifact_ref must be an "
                                   "object or id")
            external = raw.get("external_ref")
            if external is not None and not isinstance(external, dict):
                raise ServiceError(422, "ITEMS_INVALID",
                                   f"items[{index}].external_ref must be an "
                                   "object")
            if artifact is None and external is None:
                raise ServiceError(422, "ITEMS_INVALID",
                                   f"items[{index}] needs artifact_ref or "
                                   "external_ref")
            items.append({
                "role": role,
                "ordinal": int(raw.get("ordinal") or 0),
                "artifact_ref": artifact,
                "content_hash": _hex64(raw.get("content_hash")),
                "byte_size": _int(raw.get("byte_size")),
                "mime": raw.get("mime"),
                "locale": raw.get("locale"),
                "external_ref": external,
            })
        return items

    def _insert_package(self, draft: dict) -> None:
        hashes = package_hash_for(
            draft["package_id"], draft["package_type"], draft.get("episode_id"),
            draft.get("branch_id"), draft.get("master_candidate_id"),
            draft["execution_fence"], draft.get("node_epoch"), draft["items"])
        draft["package_hash"] = hashes
        self.conn.execute(
            "INSERT INTO package (tenant_id, package_id, project_id, series_id,"
            " episode_id, branch_id, package_type, purpose, home_cell,"
            " requested_locales, voice_refs, master_candidate_id,"
            " master_artifact_ref, timeline_ref, manifest_ref, execution_fence,"
            " node_epoch, manifest_hash, canonical_version, package_hash,"
            " state, gaps, gate_results, contract_ref, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft["tenant_id"], draft["package_id"], draft["project_id"],
             draft["series_id"], draft.get("episode_id"), draft.get("branch_id"),
             draft["package_type"], draft.get("purpose"), draft["home_cell"],
             _jcs(draft["requested_locales"]), _jcs(draft["voice_refs"]),
             draft.get("master_candidate_id"),
             _jcs(draft["master"]) if draft.get("master") else None,
             _jcs(draft["timeline_ref"]) if draft.get("timeline_ref") else None,
             draft.get("manifest_ref_id"), draft["execution_fence"],
             draft.get("node_epoch"), None, draft["canonical_version"],
             hashes, draft["state"], _jcs(draft["gaps"]),
             _jcs(draft["gate_results"]),
             _jcs(draft["contract_ref"]) if draft.get("contract_ref") else None,
             _jcs(draft["created_by"]), _now()))
        for item in draft["items"]:
            self.conn.execute(
                "INSERT INTO item (tenant_id, package_id, role, ordinal,"
                " artifact_ref, content_hash, byte_size, mime, locale,"
                " external_ref) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (draft["tenant_id"], draft["package_id"], item["role"],
                 item["ordinal"],
                 _jcs(item["artifact_ref"]) if item.get("artifact_ref") else None,
                 item.get("content_hash"), item.get("byte_size"),
                 item.get("mime"), item.get("locale"),
                 _jcs(item["external_ref"])
                 if item.get("external_ref") else None))

    def _emit_created(self, tenant_id: str, draft: dict,
                      command_id: str) -> None:
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_CREATED,
            subject=draft["package_id"], tenant_id=tenant_id,
            home_cell=draft["home_cell"],
            aggregate_id=draft["package_id"], sequence=SEQUENCE_CREATED,
            correlation_id=str((draft.get("created_by") or {}).get(
                "principal_id") or command_id),
            causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "package_id": draft["package_id"],
                "package_type": draft["package_type"],
                "episode_id": draft.get("episode_id"),
                "purpose": draft.get("purpose"),
                "master_candidate_id": draft.get("master_candidate_id"),
                "state": draft["state"],
                "gaps": draft["gaps"],
                "gate_results": draft["gate_results"],
                "created_at": _now(),
            },
            data_ref={"service_id": SOURCE, "id": draft["package_id"],
                      "revision": 1,
                      "content_hash": draft["package_hash"]},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------ 封包
    def seal(self, tenant_id: str, command_id: str, package_id: str,
             request_hash: str, *, audit: bool = True) -> dict:
        """`POST /delivery-packages/{id}:seal`（§4.2）。

        幂等：已封过的包**返回冻结的 manifest，不重算 hash**（E12-S43-03）。
        """
        package = self._package(tenant_id, package_id)
        if package["state"] in ("SEALED", "PUBLISHED", "PUBLISH_FAILED"):
            return self._seal_view(tenant_id, package)
        if package["state"] == "BLOCKED":
            raise ServiceError(409, "PACKAGE_BLOCKED",
                               "a BLOCKED package cannot be sealed; fix the "
                               "gaps and create a new package (plan §1.2)",
                               details={"gaps": _loads(package["gaps"], [])})

        items = self._items(tenant_id, package_id)
        if not items:
            raise ServiceError(409, "MANIFEST_INCOMPLETE",
                               "the package has no items to seal")

        # §4.2 第 3 步：签名前的最后一次素材复查。它在事务之外，因为它调 S28。
        # 缺素材包对「查不到」豁免（§2.3），但仍要复查 hash：内容被换掉是
        # 另一回事，必须挡住。
        gate2 = self._gate_g2(
            tenant_id, items,
            allow_missing=package["package_type"] == "missing_material")
        if gate2["result"] == "fail":
            self._record_gate(tenant_id, package_id, "G2", gate2)
            raise ServiceError(409, gate2["code"] or "ARTIFACT_MISSING",
                               "the package changed since it was created; "
                               "create a new package (plan §1.2)",
                               details=gate2["detail"])

        manifest = build_manifest(package_id, items)
        manifest_hash = manifest_hash_for(manifest,
                                         package["canonical_version"])
        total_bytes = sum(int(item.get("byte_size") or 0) for item in items)

        # §4.2 第 6 步：签名是外部调用，不进事务。不可用 → 仍 DRAFTING。
        signature = self._dep("G9", "KMS", lambda: self.signer(
            tenant_id, manifest_hash, self._key_ref(tenant_id)))

        sealed_at = _now()
        signature_id = _new_id("signature")
        verified = bool(signature.get("verified"))
        audit_id = None
        try:
            with Transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO manifest (tenant_id, package_id,"
                    " manifest_hash, canonical_version, manifest, item_count,"
                    " total_bytes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, package_id, manifest_hash,
                     package["canonical_version"], _jcs(manifest), len(items),
                     total_bytes, sealed_at))
                self.conn.execute(
                    "INSERT INTO integrity_signature (tenant_id, signature_id,"
                    " package_id, manifest_hash, key_ref, algorithm,"
                    " signature_b64, verified, verified_at, verify_error,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, signature_id, package_id, manifest_hash,
                     _jcs(self._key_ref(tenant_id)),
                     str(signature.get("algorithm") or "unknown"),
                     str(signature.get("signature_b64") or ""),
                     1 if verified else 0,
                     sealed_at if verified else None,
                     None if verified else str(signature.get("error")
                                               or "signer returned "
                                                  "verified=false"),
                     sealed_at))
                gates = _loads(package["gate_results"], {})
                gates["G9"] = "pass" if verified else "fail"
                cur = self.conn.execute(
                    "UPDATE package SET state='SEALED', manifest_hash=?,"
                    " sealed_at=?, gate_results=? WHERE tenant_id=?"
                    " AND package_id=? AND state='DRAFTING'",
                    (manifest_hash, sealed_at, _jcs(gates), tenant_id,
                     package_id))
                if cur.rowcount != 1:
                    # 并发封包：另一个请求先封上了。回滚本次插入，返回它的
                    # 冻结清单（§7.2：同键重复 = 同一结果）。
                    raise _AlreadySealed()
                self.outbox.append(new_event(
                    source=SOURCE, type=EVENT_SEALED, subject=package_id,
                    tenant_id=tenant_id, home_cell=package["home_cell"],
                    aggregate_id=package_id,
                    sequence=self._next_sequence(tenant_id, package_id),
                    correlation_id=command_id, causation_id=command_id,
                    data={
                        "schema_version": SCHEMA_VERSION,
                        "package_id": package_id,
                        "package_type": package["package_type"],
                        "episode_id": package.get("episode_id"),
                        "master_candidate_id":
                            package.get("master_candidate_id"),
                        "manifest_hash": manifest_hash,
                        "canonical_version": package["canonical_version"],
                        "item_count": len(items),
                        "total_bytes": total_bytes,
                        "sealed_at": sealed_at,
                    },
                    data_ref={"service_id": SOURCE, "id": package_id,
                              "revision": 2, "content_hash": manifest_hash},
                    schema_version=SCHEMA_VERSION))
                if audit:
                    audit_id = self._audit_intent(
                        tenant_id, package_id, None, "seal",
                        "delivery.package.sealed", "SUCCEEDED",
                        {"manifest_hash": manifest_hash,
                         "item_count": len(items), "signed": verified})
        except _AlreadySealed:
            return self._seal_view(tenant_id,
                                   self._package(tenant_id, package_id))

        if audit_id:
            self._deliver_audits(tenant_id, [audit_id])
        result = self._seal_view(tenant_id,
                                 self._package(tenant_id, package_id))
        result["signature_verified"] = verified
        result["audit_intent_id"] = audit_id
        return result

    def _record_gate(self, tenant_id: str, package_id: str, gate_id: str,
                     gate: dict) -> None:
        with Transaction(self.conn):
            row = _row(self.conn.execute(
                "SELECT gate_results FROM package WHERE tenant_id=?"
                " AND package_id=?", (tenant_id, package_id)).fetchone())
            if row is None:
                return
            gates = _loads(row.get("gate_results"), {})
            gates[gate_id] = gate["result"]
            self.conn.execute(
                "UPDATE package SET gate_results=? WHERE tenant_id=?"
                " AND package_id=?", (_jcs(gates), tenant_id, package_id))

    def _key_ref(self, tenant_id: str) -> dict:
        """签名 key 按租户隔离：`key_ref` 里带 tenant_id（E28-S43-03）。"""
        return {**self.signer_key_ref, "tenant_id": tenant_id}

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        """同一包的 `aggregate_sequence` 单调 +1（§6.3 行 661）。

        从 outbox 取本聚合已发出的最大序号；包不可变，事件只追加。
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM outbox"
            " WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row[0]) + 1

    def _seal_view(self, tenant_id: str, package: dict) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM manifest WHERE tenant_id=? AND package_id=?",
            (tenant_id, package["package_id"])).fetchone())
        signature = _row(self.conn.execute(
            "SELECT * FROM integrity_signature WHERE tenant_id=?"
            " AND package_id=? ORDER BY created_at DESC LIMIT 1",
            (tenant_id, package["package_id"])).fetchone())
        return {
            "package_id": package["package_id"],
            "state": package.get("state"),
            "manifest_hash": (row or {}).get("manifest_hash")
                             or package.get("manifest_hash"),
            "canonical_version": (row or {}).get("canonical_version")
                                 or package.get("canonical_version"),
            "item_count": (row or {}).get("item_count"),
            "total_bytes": (row or {}).get("total_bytes"),
            "sealed_at": package.get("sealed_at"),
            "signature": None if signature is None else {
                "signature_id": signature["signature_id"],
                "algorithm": signature["algorithm"],
                "verified": bool(signature["verified"]),
                "verify_error": signature["verify_error"],
            },
            # 重复封包（E12-S43-03）与首次封包返回**同一形状**：调用方不该
            # 因为"是不是第一次"而拿到缺字段的响应。值来自冻结的签名。
            "signature_verified": (False if signature is None
                                   else bool(signature["verified"])),
            "audit_intent_id": None,
        }

    # ------------------------------------------------------------ 外发
    def publish(self, tenant_id: str, command_id: str, package_id: str,
                body: dict, request_hash: str) -> dict:
        """`POST /delivery-packages/{id}/publish`（§4.3）。

        顺序刻意：状态 → 双层开关 → 渠道 → **实时门** → 授权判定 → 建
        attempt → 连接器。任何一步在事务外失败，attempt 都不落库。
        """
        package = self._package(tenant_id, package_id)
        state = package["state"]
        if state == "BLOCKED":
            raise ServiceError(409, "PACKAGE_BLOCKED",
                               "a BLOCKED package cannot be published",
                               details={"gaps": _loads(package["gaps"], [])})
        if state == "DRAFTING":
            raise ServiceError(409, "PACKAGE_NOT_SEALED",
                               "only a SEALED package can be published")
        if state == "PUBLISH_FAILED":
            # §3.1 的 FSM 没有从 PUBLISH_FAILED 出发的边：终态失败之后要么
            # 新建包，要么人工升级。静默允许重试会把"渠道拒收"变成重发循环。
            raise ServiceError(
                409, "PACKAGE_PUBLISH_FAILED",
                "this package ended in a terminal publish failure; create a "
                "new package or escalate (plan §3.1)")

        channel_id = body.get("channel_id")
        if not channel_id:
            raise ServiceError(422, "CHANNEL_REQUIRED", "channel_id is required")
        channel = self._channel(tenant_id, str(channel_id))

        # 双层关闭（§10 末条）。任一层没开都 503，不"假装发过了"。
        if not self.external_publish_enabled:
            raise ServiceError(
                503, "CHANNEL_DISABLED",
                "EXTERNAL_PUBLISH_ENABLED is off; S43 will not distribute "
                "anything (plan §10)")
        if int(channel["enabled"]) != 1:
            raise ServiceError(
                503, "CHANNEL_DISABLED",
                f"channel {channel_id!r} is disabled; enable it explicitly "
                "before distributing (plan §10)",
                details={"channel_id": channel_id})

        if state == "PUBLISHED":
            existing = self._latest_attempt(tenant_id, package_id,
                                            str(channel_id))
            if existing is None:
                raise ServiceError(409, "PACKAGE_ALREADY_PUBLISHED",
                                   "the package is already published")
            return {**self._attempt_view(
                existing, package,
                note="package is already published; no second send"),
                # 没有新外发：HTTP 层据此返回 200 而不是 202（plan §6.1 的
                # 202 专指"这一次真的尝试了外发"）。
                "dispatched": False}

        # 同一渠道已有在飞的 attempt：拒绝第二个（§7.2），返回第一个。
        live = self._live_attempt(tenant_id, package_id, str(channel_id))
        if live is not None:
            return {**self._attempt_view(
                live, package,
                note="an attempt for this channel is still live; not sending "
                     "twice"),
                "dispatched": False}

        # §6.3 的 published payload 要求 `master_candidate_id` 非空；没有候选
        # 就发不出去（S43-35），在调用任何上游之前拒绝。
        if not package.get("master_candidate_id"):
            raise ServiceError(
                409, "MASTER_CANDIDATE_REQUIRED",
                "the published event contract requires master_candidate_id; "
                "a package without a master candidate cannot be published "
                "(S43-35)")

        # formal 缺有效签名：外发前挡（§3.3）。
        if package["package_type"] == "formal":
            gate9 = self._gate_g9(tenant_id, package_id)
            if gate9["result"] != "pass":
                raise ServiceError(409, "SIGNATURE_REQUIRED",
                                   "a formal package needs a verified "
                                   "signature before publishing",
                                   details=gate9["detail"])

        items = self._items(tenant_id, package_id)
        self._check_channel(channel, items)

        allow_superseded = bool(body.get("allow_superseded"))
        # §4.3 第 3 步：**实时**重新评估。创建时通过不代表现在通过。
        self._recheck_g1(tenant_id, package, allow_superseded)
        gate2 = self._gate_g2(tenant_id, items)
        if gate2["result"] == "fail":
            raise ServiceError(409, gate2["code"] or "ARTIFACT_MISSING",
                               "an artifact disappeared since sealing; the "
                               "package must be recreated (plan §3.3)",
                               details=gate2["detail"])
        gate7 = self._gate_g7(tenant_id,
                              _loads(package["voice_refs"], []))
        if gate7["result"] == "fail":
            raise ServiceError(403, "RIGHTS_REVOKED",
                               "authorization was revoked; refusing to "
                               "publish (plan §2.3 G7)",
                               details=gate7["detail"])

        authorization = self._authorize(tenant_id, package, body)

        attempt = self._open_attempt(tenant_id, package, channel, command_id,
                                     authorization)
        # 回调可能早于 attempt 落库（E17-S43-01）：现在把它应用掉。
        staged = self._apply_staged_receipts(tenant_id, attempt)
        if staged is not None:
            # 已验证的回执证明渠道**已经收下了**。再调一次连接器就是重复
            # 投递（客户收到两份、平台重复上架）。把这次 attempt 直接按回执
            # 结掉，不发。
            outcome = {"outcome": "succeeded",
                       "remote_id": staged.get("remote_id"),
                       "bytes_sent": 0,
                       "receipt_id": staged.get("receipt_id"),
                       "receipt": _loads(staged.get("raw_ref"), None),
                       "acknowledged_at": staged.get("acknowledged_at")}
            with Transaction(self.conn):
                result = self._settle_attempt(tenant_id, package, channel,
                                              attempt, outcome, command_id)
            self._deliver_audits(tenant_id)
            # 没有调连接器，所以不是 202；回执本身说明了已送达。
            return {**result, "dispatched": False,
                    "note": "a verified receipt arrived before this attempt "
                            "was recorded; the channel already has the "
                            "package, so nothing was sent again"}

        # §4.3 第 5 步：经连接器外发。事务之外。
        request = {
            "channel_id": channel["channel_id"],
            "kind": channel["kind"],
            "endpoint_ref": _loads(channel["endpoint_ref"], {}),
            "package_id": package_id,
            "attempt_id": attempt["attempt_id"],
            "manifest_hash": package.get("manifest_hash"),
            "items": [{"role": item["role"],
                       "artifact_ref": _loads(item["artifact_ref"], None),
                       "byte_size": item["byte_size"]}
                      for item in items],
            "total_bytes": sum(int(item["byte_size"] or 0)
                               for item in items),
        }
        outcome = self._send(tenant_id, request, channel)

        with Transaction(self.conn):
            result = self._settle_attempt(tenant_id, package, channel,
                                          attempt, outcome, command_id)
        self._deliver_audits(tenant_id)
        # 走到这里说明**这一次真的调了连接器**（哪怕是人工交接/结果未知），
        # 所以 202。上面两条"复用既有 attempt"的分支显式标 False。
        return {**result, "dispatched": True}

    def _check_channel(self, channel: dict, items: list[dict]) -> None:
        """渠道不接受的格式/大小 → 拒绝，**不转码**（§2.5）。"""
        accepted = _loads(channel["accepted_formats"], [])
        required = _loads(channel["required_artifacts"], [])
        present = {item["role"] for item in items}
        missing = [role for role in required if role not in present]
        if missing:
            raise ServiceError(409, "CHANNEL_REQUIRED_ARTIFACT_MISSING",
                               "the channel requires artifacts this package "
                               "does not contain",
                               details={"missing": missing})
        for item in items:
            mime = item.get("mime")
            if mime and accepted and mime not in accepted:
                # S43 不做格式转换：换格式 = S42 按该渠道重渲染（§2.5）。
                raise ServiceError(409, "CHANNEL_FORMAT_UNSUPPORTED",
                                   f"{item['role']} is {mime!r}, this channel "
                                   f"accepts {accepted}",
                                   details={"role": item["role"], "mime": mime,
                                            "accepted": accepted})
            limit = _int(channel["max_file_bytes"])
            size = _int(item.get("byte_size"))
            if limit and size and size > limit:
                raise ServiceError(409, "CHANNEL_FILE_TOO_LARGE",
                                   f"{item['role']} is {size} bytes, limit is "
                                   f"{limit}",
                                   details={"role": item["role"],
                                            "byte_size": size, "limit": limit})

    def _recheck_g1(self, tenant_id: str, package: dict,
                    allow_superseded: bool) -> None:
        """§4.3 a：accepted pointer 还是本包的 master 吗？

        `MASTER_SUPERSEDED` **不是错误**，是正常业务（§3.3）。默认拒绝；
        `allow_superseded` 只跳过这道检查，策略门仍然要过。
        """
        master = _loads(package.get("master_artifact_ref"), None)
        if not master:
            return
        draft = {
            "manifest_ref_id": package.get("manifest_ref"),
            "execution_fence": package["execution_fence"],
            "package_type": package["package_type"],
        }
        gate1 = self._gate_g1(tenant_id, draft, master)
        if gate1["result"] == "pass":
            return
        if gate1["code"] == "STALE_FENCE":
            raise ServiceError(409, "STALE_FENCE",
                               "the branch's execution fence moved since "
                               "sealing; this package is stale",
                               details=gate1["detail"])
        if not allow_superseded:
            raise ServiceError(
                409, "MASTER_SUPERSEDED",
                "a newer master was accepted for this node; publishing the "
                "old one needs allow_superseded plus a fresh allow (plan §4.5)",
                details=gate1["detail"])
        self._bump("delivery_superseded_publish_total")

    def _authorize(self, tenant_id: str, package: dict, body: dict) -> dict:
        """§2.2 的四条硬规则 + §6.1 的"回查同一个 decision_id"。

        调用方给的是一张**引用**，不是结论：`lookup` 确认它存在、未过期、
        结论一致；`evaluate` 拿一次新的判定。两次都 `allow` 才放行。
        """
        raw = body.get("publish_authorization_ref")
        if raw is None:
            raise ServiceError(422, "AUTHORIZATION_REQUIRED",
                               "publish_authorization_ref is required; a "
                               "publish without a policy decision is not "
                               "authorized")
        ref = {"policy_decision_id": raw} if isinstance(raw, str) else dict(raw)
        decision_id = ref.get("policy_decision_id") or ref.get("decision_id")
        if not decision_id:
            raise ServiceError(422, "AUTHORIZATION_REQUIRED",
                               "publish_authorization_ref needs "
                               "policy_decision_id")
        decision_id = str(decision_id)

        # 1) 回查调用方那张票（E27-S43-02 伪造的 id 在这里落地）。
        try:
            stored = self._dep("G8", "S02", lambda: self.policy_source(
                tenant_id, {"op": "lookup", "decision_id": decision_id}))
        except ServiceError as exc:
            if exc.status in (404, 410):
                raise ServiceError(
                    403, "POLICY_DENIED",
                    "the referenced policy decision does not exist for this "
                    "tenant (plan §6.1)",
                    details={"policy_decision_id": decision_id,
                             "reason": "decision_not_found"}) from exc
            raise
        if not isinstance(stored, dict) or not stored:
            raise ServiceError(403, "POLICY_DENIED",
                               "S02 returned no decision for that id",
                               details={"policy_decision_id": decision_id})

        verdict = stored.get("verdict") or stored.get("decision")
        expected_hash = _hex64(ref.get("decision_hash"))
        actual_hash = _hex64(stored.get("decision_hash"))
        if expected_hash and actual_hash and expected_hash != actual_hash:
            raise ServiceError(403, "POLICY_DENIED",
                               "the decision hash does not match what S02 "
                               "holds; the caller's reference is stale or "
                               "forged",
                               details={"policy_decision_id": decision_id})
        if verdict != "allow":
            raise ServiceError(403, "POLICY_DENIED",
                               f"the referenced decision is {verdict!r}, not "
                               "allow",
                               details={"policy_decision_id": decision_id,
                                        "reasons": stored.get("reasons") or []})
        if self._expired(stored.get("valid_until")):
            raise ServiceError(403, "POLICY_DENIED",
                               "the referenced decision has expired "
                               "(plan §2.2 rule 4)",
                               details={"policy_decision_id": decision_id,
                                        "valid_until":
                                            stored.get("valid_until")})

        # 2) 实时评估一次（§2.2 rule 1：不复用上一次的 allow）。
        fresh = self._dep("G8", "S02", lambda: self.policy_source(
            tenant_id, {"op": "evaluate",
                        "gate_type": "external_publish",
                        "package_id": package["package_id"],
                        "package_type": package["package_type"],
                        "episode_id": package.get("episode_id"),
                        "channel_id": body.get("channel_id"),
                        "policy_decision_id": decision_id,
                        "allow_superseded":
                            bool(body.get("allow_superseded"))}))
        if not isinstance(fresh, dict) or not fresh:
            raise ServiceError(403, "POLICY_DENIED",
                               "S02 returned no fresh decision")
        fresh_verdict = fresh.get("verdict") or fresh.get("decision")
        if fresh.get("kill_switch") is True:
            raise ServiceError(403, "POLICY_DENIED",
                               "the external-publish kill switch is on; "
                               "previous allows are not reused (E27-S43-04)",
                               details={"reasons": fresh.get("reasons") or []})
        if self._expired(fresh.get("valid_until")):
            raise ServiceError(403, "POLICY_DENIED",
                               "the live decision is already expired",
                               details={"valid_until":
                                            fresh.get("valid_until")})
        if fresh_verdict == "require_human":
            # §2.2 rule 3：需要人的决定就必须有一张 S40 的引用；缺了不是
            # "稍后再试"，是拒绝。
            if not body.get("human_decision_ref"):
                raise ServiceError(
                    403, "POLICY_DENIED",
                    "the decision requires a human decision reference and "
                    "none was supplied (plan §2.2 rule 3)",
                    details={"verdict": fresh_verdict,
                             "reasons": fresh.get("reasons") or []})
        elif fresh_verdict != "allow":
            raise ServiceError(403, "POLICY_DENIED",
                               f"the live decision is {fresh_verdict!r}, not "
                               "allow",
                               details={"reasons": fresh.get("reasons") or []})

        return {
            "policy_decision_id": decision_id,
            "decision_hash": actual_hash,
            "verdict": "allow",
            "human_decision_ref": body.get("human_decision_ref"),
            "valid_until": (fresh.get("valid_until")
                            or stored.get("valid_until")),
            "evaluated_at": _now(),
            "reasons": fresh.get("reasons") or [],
        }

    @staticmethod
    def _expired(valid_until) -> bool:
        moment = _parse_ts(valid_until)
        return moment is not None and moment <= _now_dt()

    def _open_attempt(self, tenant_id: str, package: dict, channel: dict,
                      command_id: str, authorization: dict) -> dict:
        attempt_id = _new_id("attempt")
        with Transaction(self.conn):
            row = self.conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) FROM distribution_attempt"
                " WHERE tenant_id=? AND package_id=? AND channel_id=?",
                (tenant_id, package["package_id"],
                 channel["channel_id"])).fetchone()
            attempt_no = int(row[0]) + 1
            auth_ref_id = _new_id("auth_ref")
            self.conn.execute(
                "INSERT INTO publish_authorization_ref (tenant_id, auth_ref_id,"
                " package_id, policy_decision_id, decision_hash, verdict,"
                " gate_type, human_decision_ref, reasons, valid_until,"
                " evaluated_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, auth_ref_id, package["package_id"],
                 authorization["policy_decision_id"],
                 authorization.get("decision_hash"), "allow",
                 "external_publish",
                 _jcs(authorization["human_decision_ref"])
                 if authorization.get("human_decision_ref") else None,
                 _jcs(authorization.get("reasons") or []),
                 authorization.get("valid_until"),
                 authorization.get("evaluated_at") or _now(), _now()))
            self.conn.execute(
                "INSERT INTO distribution_attempt (tenant_id, attempt_id,"
                " package_id, channel_id, state, attempt_no, bytes_sent,"
                " remote_ref, failure_code, failure_detail, reconcile_after,"
                " auth_ref_id, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, attempt_id, package["package_id"],
                 channel["channel_id"], "IN_FLIGHT", attempt_no, 0, None,
                 None, None, None, auth_ref_id, _now(), None))
        return self._get_attempt(tenant_id, attempt_id)

    def _send(self, tenant_id: str, request: dict, channel: dict) -> dict:
        """调连接器。`manual_handoff` 不走网络（§10 第 6 步）。"""
        if channel["kind"] == "manual_handoff":
            return {"outcome": "pending",
                    "failure_detail": {
                        "reason": "awaiting_manual_handoff",
                        "endpoint_ref": request["endpoint_ref"]}}
        try:
            reply = self.connector(tenant_id, request)
        except ConnectionError as exc:
            # 连接断在中途：**UNKNOWN，不是失败**（§3.2）。
            return {"outcome": "unknown",
                    "failure_code": "NETWORK_UNREACHABLE",
                    "failure_detail": {"error": str(exc)}}
        except ServiceError as exc:
            if exc.status >= 500:
                return {"outcome": "unknown",
                        "failure_code": exc.code or "CHANNEL_UNAVAILABLE",
                        "failure_detail": {"status": exc.status,
                                           "error": str(exc)}}
            if exc.status in (401, 403):
                return {"outcome": "failed", "terminal": True,
                        "failure_code": "CHANNEL_AUTH_FAILED",
                        "failure_detail": {"status": exc.status}}
            if exc.status == 429:
                return {"outcome": "failed", "retryable": True,
                        "failure_code": "CHANNEL_RATE_LIMITED",
                        "failure_detail": {"status": 429}}
            return {"outcome": "failed", "terminal": True,
                    "failure_code": exc.code or "PUBLISH_REJECTED",
                    "failure_detail": {"status": exc.status}}
        if not isinstance(reply, dict):
            return {"outcome": "unknown",
                    "failure_code": "CHANNEL_UNAVAILABLE",
                    "failure_detail": {"reason": "connector returned no body"}}
        return reply

    def _settle_attempt(self, tenant_id: str, package: dict, channel: dict,
                        attempt: dict, outcome: dict,
                        command_id: str) -> dict:
        """**必须**在 `publish` 的事务内调用；本函数不再开事务。"""
        attempt_id = attempt["attempt_id"]
        state = str(outcome.get("outcome") or "unknown").lower()
        bytes_sent = _int(outcome.get("bytes_sent")) or 0
        remote_id = outcome.get("remote_id")

        if state == "pending":
            # 人工交接：attempt 留在 IN_FLIGHT，等回执或人工确认。**不是送达。**
            self.conn.execute(
                "UPDATE distribution_attempt SET bytes_sent=?,"
                " failure_detail=? WHERE tenant_id=? AND attempt_id=?",
                (bytes_sent, _jcs(outcome.get("failure_detail") or {}),
                 tenant_id, attempt_id))
            self._bump("delivery_publish_pending_total")
            return self._attempt_view(
                self._get_attempt(tenant_id, attempt_id), package,
                note="manual handoff: the package is not delivered until a "
                     "receipt arrives")

        if state == "succeeded":
            receipt = self._apply_receipt(
                tenant_id, attempt, remote_id=remote_id,
                acknowledged_at=outcome.get("acknowledged_at") or _now(),
                raw_ref=outcome.get("receipt"),
                receipt_id=outcome.get("receipt_id"), verified=True)
            self.conn.execute(
                "UPDATE distribution_attempt SET state='SUCCEEDED',"
                " bytes_sent=?, remote_ref=?, finished_at=?"
                " WHERE tenant_id=? AND attempt_id=?",
                (bytes_sent, remote_id or outcome.get("remote_ref"), _now(),
                 tenant_id, attempt_id))
            self.conn.execute(
                "UPDATE package SET state='PUBLISHED', published_at=?"
                " WHERE tenant_id=? AND package_id=?",
                (_now(), tenant_id, package["package_id"]))
            settled = self._get_attempt(tenant_id, attempt_id)
            updated = self._package(tenant_id, package["package_id"])
            self._emit_published(tenant_id, updated, settled, channel,
                                 remote_id, command_id)
            self._audit_intent(tenant_id, package["package_id"], attempt_id,
                               "publish", "delivery.package.published",
                               "SUCCEEDED",
                               {"channel_id": channel["channel_id"],
                                "bytes_sent": bytes_sent,
                                "remote_id": remote_id,
                                "receipt_id": receipt["receipt_id"]})
            self._bump("delivery_publish_success_total")
            return self._attempt_view(settled, updated)

        if state in ("unknown", "interrupted"):
            # §3.2 / §4.4：先对账，不重传。TTL 到了才转 FAILED_UNRESOLVED。
            reconcile_after = _fmt(_now_dt() + self.unknown_ttl)
            self.conn.execute(
                "UPDATE distribution_attempt SET state='UNKNOWN',"
                " bytes_sent=?, remote_ref=?, failure_code=?,"
                " failure_detail=?, reconcile_after=?"
                " WHERE tenant_id=? AND attempt_id=?",
                (bytes_sent, remote_id,
                 outcome.get("failure_code") or "UNKNOWN",
                 _jcs(outcome.get("failure_detail") or {}), reconcile_after,
                 tenant_id, attempt_id))
            self._bump("delivery_publish_unknown_total")
            return self._attempt_view(
                self._get_attempt(tenant_id, attempt_id), package,
                note="outcome unknown: reconcile before resending; S43 will "
                     "not resend automatically")

        # failed
        code = outcome.get("failure_code") or "CHANNEL_UNAVAILABLE"
        retryable = bool(outcome.get("retryable"))
        terminal = bool(outcome.get("terminal")) or not retryable
        self.conn.execute(
            "UPDATE distribution_attempt SET state='FAILED', bytes_sent=?,"
            " remote_ref=?, failure_code=?, failure_detail=?, finished_at=?"
            " WHERE tenant_id=? AND attempt_id=?",
            (bytes_sent, remote_id, code,
             _jcs(outcome.get("failure_detail") or {}), _now(),
             tenant_id, attempt_id))
        self.conn.execute(
            "UPDATE package SET state=? WHERE tenant_id=? AND package_id=?",
            ("PUBLISH_FAILED" if terminal else "SEALED",
             tenant_id, package["package_id"]))
        settled = self._get_attempt(tenant_id, attempt_id)
        self._emit_failed(tenant_id, package, settled, channel, code, "FAILED",
                          bytes_sent, outcome.get("failure_detail"), remote_id,
                          command_id)
        self._audit_intent(tenant_id, package["package_id"], attempt_id,
                           "publish", "delivery.publish.failed", "ERROR",
                           {"channel_id": channel["channel_id"],
                            "failure_code": code,
                            "bytes_sent": bytes_sent,
                            "retryable": retryable})
        self._bump("delivery_publish_failed_total")
        return self._attempt_view(
            settled, self._package(tenant_id, package["package_id"]),
            note=("retryable failure: the package is back to SEALED; retry "
                  "with a new attempt" if not terminal
                  else "terminal failure: the package will not be resent"))

    # ---------------------------------------------------------- 对账与回执
    def reconcile(self, tenant_id: str, command_id: str, package_id: str,
                  body: dict, request_hash: str) -> dict:
        """`UNKNOWN` 的**唯一**出口（§3.2 / §4.4）。

        三件事按顺序：先看有没有已到的回执 → 再问渠道（支持查询才问）→
        最后看 TTL。**任何一步都不重传**；"确认没收到"只把 attempt 放回
        `PENDING`，重发仍是一个显式的 publish。
        """
        package = self._package(tenant_id, package_id)
        attempt_id = body.get("attempt_id")
        attempt = (self._get_attempt(tenant_id, str(attempt_id))
                   if attempt_id
                   else self._unknown_attempt(tenant_id, package_id))
        if attempt is None:
            raise ServiceError(404, "ATTEMPT_NOT_FOUND",
                               "no attempt to reconcile")
        if attempt["state"] != "UNKNOWN":
            # 已经是终态：对账不再改变结果（E17-S43-04 的不倒退）。
            return self._attempt_view(
                attempt, package,
                note=f"attempt is {attempt['state']}; reconcile is a no-op")

        channel = self._channel(tenant_id, attempt["channel_id"])
        reconciled_at = _now()

        # 1) 渠道侧回执可能已经躺在暂存区里（回调先于 attempt 或被 TTL 拖住）。
        staged = self._apply_staged_receipts(tenant_id, attempt)
        if staged:
            return self._finish_from_receipt(tenant_id, package, attempt,
                                             channel, staged, command_id,
                                             reconciled_at)

        # 2) 渠道支持查询才问；不支持就是"不知道"，不是"没收到"。
        if int(channel["supports_query"]) == 1:
            reply = self._query_channel(tenant_id, attempt, channel)
            received = reply.get("received")
            if received is True:
                receipt = self._apply_receipt(
                    tenant_id, attempt,
                    remote_id=reply.get("remote_id") or attempt.get("remote_ref"),
                    acknowledged_at=reply.get("acknowledged_at")
                    or reconciled_at,
                    raw_ref=reply.get("receipt"), receipt_id=None,
                    verified=True)
                return self._finish_from_receipt(tenant_id, package, attempt,
                                                 channel, receipt, command_id,
                                                 reconciled_at)
            if received is False:
                with Transaction(self.conn):
                    self.conn.execute(
                        "UPDATE distribution_attempt SET state='PENDING',"
                        " failure_code='RECONCILE_NOT_RECEIVED',"
                        " reconcile_after=NULL, finished_at=?"
                        " WHERE tenant_id=? AND attempt_id=?",
                        (reconciled_at, tenant_id, attempt["attempt_id"]))
                    self.conn.execute(
                        "UPDATE package SET state='SEALED'"
                        " WHERE tenant_id=? AND package_id=?",
                        (tenant_id, package_id))
                self._bump("delivery_reconcile_not_received_total")
                return self._attempt_view(
                    self._get_attempt(tenant_id, attempt["attempt_id"]),
                    self._package(tenant_id, package_id),
                    note="the channel confirms it did not receive this "
                         "attempt; it is back to PENDING and can be resent "
                         "explicitly (no automatic resend)")

        # 3) TTL：超过截止时间仍无法确认 → FAILED_UNRESOLVED，告警，不重发。
        deadline = _parse_ts(attempt.get("reconcile_after"))
        if deadline is not None and _now_dt() >= deadline:
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE distribution_attempt SET state='FAILED_UNRESOLVED',"
                    " failure_code='RECONCILE_TIMEOUT',"
                    " failure_detail=?, finished_at=?"
                    " WHERE tenant_id=? AND attempt_id=?",
                    (_jcs({"reconcile_after": attempt.get("reconcile_after"),
                           "reconciled_at": reconciled_at}),
                     reconciled_at, tenant_id, attempt["attempt_id"]))
                self.conn.execute(
                    "UPDATE package SET state='PUBLISH_FAILED'"
                    " WHERE tenant_id=? AND package_id=?",
                    (tenant_id, package_id))
                settled = self._get_attempt(tenant_id, attempt["attempt_id"])
                self._emit_failed(tenant_id, package, settled, channel,
                                  "RECONCILE_TIMEOUT", "FAILED_UNRESOLVED",
                                  int(settled.get("bytes_sent") or 0),
                                  {"reconcile_after":
                                       attempt.get("reconcile_after")},
                                  settled.get("remote_ref"), command_id)
                self._audit_intent(
                    tenant_id, package_id, attempt["attempt_id"], "reconcile",
                    "delivery.publish.failed", "REPORTED_UNKNOWN",
                    {"attempt_no": attempt["attempt_no"],
                     "failure_code": "RECONCILE_TIMEOUT",
                     "requires_human": True})
            self._bump("delivery_reconcile_timeout_total")
            return self._attempt_view(
                self._get_attempt(tenant_id, attempt["attempt_id"]),
                self._package(tenant_id, package_id),
                note="reconcile TTL expired: FAILED_UNRESOLVED, alert raised, "
                     "no automatic resend")

        return self._attempt_view(
            attempt, package,
            note="still unknown; the channel has not confirmed either way and "
                 "the reconcile TTL has not expired")

    def _query_channel(self, tenant_id: str, attempt: dict,
                       channel: dict) -> dict:
        try:
            reply = self.connector(tenant_id, {
                "op": "reconcile",
                "channel_id": channel["channel_id"],
                "kind": channel["kind"],
                "endpoint_ref": _loads(channel["endpoint_ref"], {}),
                "attempt_id": attempt["attempt_id"],
                "package_id": attempt["package_id"],
                "remote_ref": attempt.get("remote_ref"),
            })
        except (ConnectionError, ServiceError) as exc:
            # 查不了 ≠ 没收到。保持 UNKNOWN，等下一次或 TTL。
            return {"received": None, "error": str(exc)}
        if not isinstance(reply, dict):
            return {"received": None}
        return reply

    def _finish_from_receipt(self, tenant_id: str, package: dict,
                             attempt: dict, channel: dict, receipt: dict,
                             command_id: str, moment: str) -> dict:
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE distribution_attempt SET state='SUCCEEDED',"
                " remote_ref=COALESCE(remote_ref, ?), finished_at=?"
                " WHERE tenant_id=? AND attempt_id=?",
                (receipt.get("remote_id"), moment, tenant_id,
                 attempt["attempt_id"]))
            self.conn.execute(
                "UPDATE package SET state='PUBLISHED', published_at=?"
                " WHERE tenant_id=? AND package_id=?",
                (moment, tenant_id, package["package_id"]))
            settled = self._get_attempt(tenant_id, attempt["attempt_id"])
            updated = self._package(tenant_id, package["package_id"])
            self._emit_published(tenant_id, updated, settled, channel,
                                 receipt.get("remote_id"), command_id)
            self._audit_intent(
                tenant_id, package["package_id"], attempt["attempt_id"],
                "reconcile", "delivery.package.published", "SUCCEEDED",
                {"receipt_id": receipt["receipt_id"],
                 "remote_id": receipt.get("remote_id"),
                 "reconciled": True})
        self._deliver_audits(tenant_id)
        self._bump("delivery_reconcile_success_total")
        return self._attempt_view(
            self._get_attempt(tenant_id, attempt["attempt_id"]),
            self._package(tenant_id, package["package_id"]),
            note="the channel confirmed receipt; SUCCEEDED")

    def ingest_receipt(self, tenant_id: str, command_id: str,
                       body: dict, request_hash: str) -> dict:
        """渠道回调（§4.6）。**验不了签名就不算送达**。

        回调可能早于 attempt 落库（E17-S43-01）：那时暂存原始载荷，
        attempt 出现后在 `publish` / `reconcile` 里应用。
        """
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
        channel_id = body.get("channel_id")
        if not channel_id:
            raise ServiceError(422, "CHANNEL_REQUIRED", "channel_id is required")
        receipt = body.get("receipt") if isinstance(body.get("receipt"),
                                                    dict) else {
            k: v for k, v in body.items()
            if k not in ("package_id", "channel_id", "attempt_id",
                         "command_id", "request_hash")}
        package_id = body.get("package_id")
        attempt_id = body.get("attempt_id")

        # 1) 验签。未配置 → 503（deps 桩），配置了但说 false → 403 + 审计。
        valid = False
        try:
            valid = bool(self._dep(
                "G-RECEIPT", "channel-verifier",
                lambda: self.receipt_verifier(tenant_id, package_id,
                                              attempt_id, receipt)))
        except ServiceError as exc:
            if exc.status != 503:
                raise
            self._audit_intent(
                tenant_id, package_id or "unknown", attempt_id, "receipt",
                "delivery.receipt.rejected", "ERROR",
                {"channel_id": channel_id,
                 "reason": "verifier_unavailable"})
            self._deliver_audits(tenant_id)
            raise
        if not valid:
            self._audit_intent(
                tenant_id, package_id or "unknown", attempt_id, "receipt",
                "delivery.receipt.rejected", "DENIED",
                {"channel_id": channel_id,
                 "reason": "signature_invalid"})
            self._deliver_audits(tenant_id)
            self._bump("delivery_receipt_rejected_total")
            raise ServiceError(403, "RECEIPT_SIGNATURE_INVALID",
                               "the receipt signature cannot be verified; it "
                               "is not evidence of delivery (plan §4.6)")

        attempt = (self._get_attempt(tenant_id, str(attempt_id))
                   if attempt_id else self._find_attempt_for_receipt(
                       tenant_id, package_id, str(channel_id),
                       body.get("remote_id")))

        if attempt is None:
            # 2) 无处可归：暂存，不丢（E17-S43-01）。
            callback_id = _new_id("callback")
            with Transaction(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO receipt_callback (tenant_id,"
                    " callback_id, package_id, channel_id, attempt_id,"
                    " remote_id, acknowledged_at, payload, received_at,"
                    " applied_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (tenant_id, callback_id, package_id, str(channel_id),
                     attempt_id, body.get("remote_id"),
                     str(body.get("acknowledged_at") or _now()),
                     _jcs(body), _now()))
            self._bump("delivery_receipt_staged_total")
            return {"accepted": True, "staged": True,
                    "callback_id": callback_id,
                    "note": "the attempt is not known yet; the payload is "
                            "staged and will be applied once it appears"}

        with Transaction(self.conn):
            record = self._apply_receipt(
                tenant_id, attempt,
                remote_id=body.get("remote_id"),
                acknowledged_at=str(body.get("acknowledged_at") or _now()),
                raw_ref=receipt, receipt_id=body.get("receipt_id"),
                verified=True)
            package = self._package(tenant_id, attempt["package_id"])
            advanced = False
            if attempt["state"] not in ("SUCCEEDED", "FAILED",
                                        "FAILED_UNRESOLVED"):
                # 终态不倒退（E17-S43-04）：只在非终态时推进。
                self.conn.execute(
                    "UPDATE distribution_attempt SET state='SUCCEEDED',"
                    " remote_ref=COALESCE(remote_ref, ?), finished_at=?"
                    " WHERE tenant_id=? AND attempt_id=?",
                    (record.get("remote_id"), _now(), tenant_id,
                     attempt["attempt_id"]))
                if package["state"] != "PUBLISHED":
                    self.conn.execute(
                        "UPDATE package SET state='PUBLISHED', published_at=?"
                        " WHERE tenant_id=? AND package_id=?",
                        (_now(), tenant_id, package["package_id"]))
                    advanced = True
            if advanced:
                channel = self._channel(tenant_id, attempt["channel_id"])
                updated = self._package(tenant_id, package["package_id"])
                self._emit_published(
                    tenant_id, updated,
                    self._get_attempt(tenant_id, attempt["attempt_id"]),
                    channel, record.get("remote_id"), command_id)
            self._audit_intent(
                tenant_id, attempt["package_id"], attempt["attempt_id"],
                "receipt", "delivery.receipt.accepted", "SUCCEEDED",
                {"receipt_id": record["receipt_id"],
                 "remote_id": record.get("remote_id"),
                 "advanced": advanced})
        self._deliver_audits(tenant_id)
        return {"accepted": True, "staged": False,
                "receipt_id": record["receipt_id"],
                "attempt_id": attempt["attempt_id"],
                "attempt_state": self._get_attempt(
                    tenant_id, attempt["attempt_id"])["state"],
                "note": ("the receipt is recorded; the attempt was already "
                         "terminal so its state did not change"
                         if not advanced else "delivery confirmed")}

    def _apply_receipt(self, tenant_id: str, attempt: dict, *,
                       remote_id, acknowledged_at: str, raw_ref,
                       receipt_id, verified: bool) -> dict:
        """一次尝试只有一条回执（`UNIQUE(tenant_id, attempt_id)`）。"""
        existing = _row(self.conn.execute(
            "SELECT * FROM receipt WHERE tenant_id=? AND attempt_id=?",
            (tenant_id, attempt["attempt_id"])).fetchone())
        if existing is not None:
            return existing
        rid = str(receipt_id) if receipt_id else _new_id("receipt")
        self.conn.execute(
            "INSERT OR IGNORE INTO receipt (tenant_id, receipt_id, attempt_id,"
            " package_id, channel_id, remote_id, acknowledged_at,"
            " signature_valid, raw_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, rid, attempt["attempt_id"], attempt["package_id"],
             attempt["channel_id"], remote_id, acknowledged_at,
             1 if verified else 0,
             _jcs(raw_ref) if raw_ref is not None else None, _now()))
        return _row(self.conn.execute(
            "SELECT * FROM receipt WHERE tenant_id=? AND attempt_id=?",
            (tenant_id, attempt["attempt_id"])).fetchone())

    def _apply_staged_receipts(self, tenant_id: str,
                               attempt: dict) -> dict | None:
        """把暂存区里属于这次 attempt 的回执应用掉。返回最新的那条。"""
        rows = self.conn.execute(
            "SELECT * FROM receipt_callback WHERE tenant_id=?"
            " AND applied_at IS NULL AND (attempt_id=? OR (attempt_id IS NULL"
            " AND package_id=? AND channel_id=?))"
            " ORDER BY received_at",
            (tenant_id, attempt["attempt_id"], attempt["package_id"],
             attempt["channel_id"])).fetchall()
        applied = None
        for row in rows:
            record = self._apply_receipt(
                tenant_id, attempt,
                remote_id=row["remote_id"],
                acknowledged_at=row["acknowledged_at"],
                raw_ref=_loads(row["payload"], {}), receipt_id=None,
                verified=True)
            self.conn.execute(
                "UPDATE receipt_callback SET applied_at=? WHERE tenant_id=?"
                " AND callback_id=?", (_now(), tenant_id,
                                       row["callback_id"]))
            applied = record
        return applied

    # ------------------------------------------------------------- 查询
    def _view(self, tenant_id: str, package_id: str) -> dict:
        package = self._package(tenant_id, package_id)
        gates = _loads(package["gate_results"], {})
        gaps = _loads(package["gaps"], [])
        attempts = self.conn.execute(
            "SELECT COUNT(*) FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=?", (tenant_id, package_id)).fetchone()[0]
        return {
            "package_id": package["package_id"],
            "package_type": package["package_type"],
            "state": package["state"],
            "project_id": package["project_id"],
            "series_id": package["series_id"],
            "episode_id": package["episode_id"],
            "branch_id": package["branch_id"],
            "purpose": package["purpose"],
            "master_candidate_id": package["master_candidate_id"],
            "package_hash": package["package_hash"],
            "manifest_hash": package["manifest_hash"],
            "canonical_version": package["canonical_version"],
            "gaps": gaps,
            "gate_results": gates,
            "deliverable": deliverable_of(package["package_type"],
                                          package["state"], gates, gaps),
            "execution_fence": package["execution_fence"],
            "node_epoch": package["node_epoch"],
            "attempt_count": int(attempts),
            "created_at": package["created_at"],
            "sealed_at": package["sealed_at"],
            "published_at": package["published_at"],
            # S43-6：OpenAPI 返回 `Operation`，这里给一个由 package_id 决定的
            # 稳定 id —— 重投必须拿到同一个值，不能用随机数。
            "operation_id": "op_" + _sha256_hex(package_id)[:20],
        }

    def get_package(self, tenant_id: str, package_id: str) -> dict:
        return self._view(tenant_id, package_id)

    def list_packages(self, tenant_id: str, *, episode_id: str | None = None,
                      package_type: str | None = None,
                      state: str | None = None, limit: int = 50) -> dict:
        clauses = ["tenant_id=?"]
        params: list = [tenant_id]
        if episode_id:
            clauses.append("episode_id=?")
            params.append(episode_id)
        if package_type:
            if package_type not in PACKAGE_TYPES:
                raise ServiceError(422, "INVALID_PACKAGE_TYPE",
                                   "unknown package_type filter")
            clauses.append("package_type=?")
            params.append(package_type)
        if state:
            if state not in PACKAGE_STATES:
                raise ServiceError(422, "INVALID_STATE",
                                   "unknown state filter")
            clauses.append("state=?")
            params.append(state)
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            "SELECT package_id FROM package WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return {"items": [self._view(tenant_id, row[0]) for row in rows]}

    def get_manifest(self, tenant_id: str, package_id: str,
                     *, signed: bool = False) -> dict:
        """`GET /delivery-packages/{id}/manifest[?signed=true]`（§6.2）。"""
        package = self._package(tenant_id, package_id)
        row = _row(self.conn.execute(
            "SELECT * FROM manifest WHERE tenant_id=? AND package_id=?",
            (tenant_id, package_id)).fetchone())
        if row is None:
            raise ServiceError(409, "MANIFEST_NOT_FROZEN",
                               "the package has not been sealed yet")
        body = {
            "package_id": package_id,
            "state": package["state"],
            "manifest_hash": row["manifest_hash"],
            "canonical_version": row["canonical_version"],
            "manifest": _loads(row["manifest"], {}),
        }
        if signed:
            signature = _row(self.conn.execute(
                "SELECT * FROM integrity_signature WHERE tenant_id=?"
                " AND package_id=? ORDER BY created_at DESC LIMIT 1",
                (tenant_id, package_id)).fetchone())
            if signature is None:
                raise ServiceError(409, "SIGNATURE_REQUIRED",
                                   "this package has no signature")
            body["signature"] = {
                "signature_id": signature["signature_id"],
                "manifest_hash": signature["manifest_hash"],
                "key_ref": _loads(signature["key_ref"], {}),
                "algorithm": signature["algorithm"],
                "signature_b64": signature["signature_b64"],
                "verified": bool(signature["verified"]),
                "created_at": signature["created_at"],
            }
        return body

    def verify_manifest(self, tenant_id: str, package_id: str,
                        *, canonical_version: str | None = None) -> dict:
        """重算并比对 `manifest_hash`（C-S43-01 / C-S43-03 / C-S43-04）。

        升级方向：本版本必须能校验**旧** `canonical_version` 冻结的清单。
        不认识的角色直接拒绝——静默跳过会让"回滚后少一项"变成"校验通过"。
        """
        package = self._package(tenant_id, package_id)
        row = _row(self.conn.execute(
            "SELECT * FROM manifest WHERE tenant_id=? AND package_id=?",
            (tenant_id, package_id)).fetchone())
        if row is None:
            raise ServiceError(409, "MANIFEST_NOT_FROZEN",
                               "the package has not been sealed yet")
        version = canonical_version or row["canonical_version"]
        # jcs-v1 是当前唯一的实现；冻结在别的版本上的清单仍按它自己的版本
        # 重算，而不是按"今天的默认值"。
        if version not in CANONICAL_VERSIONS:
            raise ServiceError(
                409, "UNSUPPORTED_CANONICAL_VERSION",
                f"this build cannot canonicalize {version!r}; refusing to "
                "claim a hash it cannot compute (C-S43-03)")
        manifest = _loads(row["manifest"], {})
        for entry in manifest.get("items") or []:
            role = entry.get("role")
            if role not in ITEM_ROLES:
                # C-S43-04：旧版本不认识新 role 时拒绝整包。
                raise ServiceError(
                    409, "UNKNOWN_ITEM_ROLE",
                    f"the manifest contains role {role!r}, which this build "
                    "does not know; refusing to silently drop it (C-S43-04)")
        recomputed = manifest_hash_for(manifest, version)
        ok = recomputed == row["manifest_hash"]
        return {
            "package_id": package_id,
            "manifest_hash": row["manifest_hash"],
            "recomputed": recomputed,
            "canonical_version": version,
            "valid": ok,
            "package_state": package["state"],
        }

    def get_evidence(self, tenant_id: str, package_id: str) -> dict:
        """`GET /delivery-packages/{id}/evidence`（§6.2）。

        只给**引用**：本服务不复制报告内容，也不重算结论。
        """
        package = self._package(tenant_id, package_id)
        items = self._items(tenant_id, package_id)
        attempts = self._attempt_rows(tenant_id, package_id)
        auth_refs = [
            dict(row) for row in self.conn.execute(
                "SELECT auth_ref_id, policy_decision_id, decision_hash,"
                " verdict, human_decision_ref, valid_until, evaluated_at"
                " FROM publish_authorization_ref WHERE tenant_id=?"
                " AND package_id=? ORDER BY created_at", (
                    tenant_id, package_id)).fetchall()]
        for ref in auth_refs:
            ref["human_decision_ref"] = _loads(ref["human_decision_ref"], None)
        return {
            "package_id": package_id,
            "state": package["state"],
            "manifest_hash": package["manifest_hash"],
            "master_candidate_id": package["master_candidate_id"],
            "master_artifact_ref": _loads(package["master_artifact_ref"], None),
            "timeline_ref": _loads(package["timeline_ref"], None),
            "manifest_ref": package["manifest_ref"],
            "gaps": _loads(package["gaps"], []),
            "gate_results": _loads(package["gate_results"], {}),
            "items": [{k: v for k, v in item.items()} for item in items],
            "policy_refs": auth_refs,
            "attempts": [self._attempt_row(row) for row in attempts],
            "audit_intents": self._audit_rows(tenant_id, package_id),
        }

    def get_attempts(self, tenant_id: str, package_id: str) -> dict:
        self._package(tenant_id, package_id)
        rows = self._attempt_rows(tenant_id, package_id)
        return {"package_id": package_id,
                "attempts": [self._attempt_row(row) for row in rows]}

    def _attempt_rows(self, tenant_id: str, package_id: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=? ORDER BY attempt_no", (
                tenant_id, package_id)).fetchall()]

    @staticmethod
    def _attempt_row(row: dict) -> dict:
        return {
            "attempt_id": row["attempt_id"],
            "channel_id": row["channel_id"],
            "state": row["state"],
            "attempt_no": row["attempt_no"],
            "bytes_sent": row["bytes_sent"],
            "remote_ref": row["remote_ref"],
            "failure_code": row["failure_code"],
            "failure_detail": _loads(row.get("failure_detail"), {}),
            "auth_ref_id": row.get("auth_ref_id"),
            "reconcile_after": row.get("reconcile_after"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
        }

    def _audit_rows(self, tenant_id: str, package_id: str) -> list[dict]:
        return [{
            "intent_id": row["intent_id"],
            "verb": row["verb"],
            "action": row["action"],
            "outcome": row["outcome"],
            "record_id": row["record_id"],
            "delivered": bool(row["delivered"]),
            "sent_at": row["sent_at"],
        } for row in self.conn.execute(
            "SELECT * FROM audit_intent WHERE tenant_id=? AND package_id=?"
            " ORDER BY sent_at", (tenant_id, package_id)).fetchall()]

    def _get_attempt(self, tenant_id: str, attempt_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND attempt_id=?", (tenant_id, attempt_id)).fetchone())
        if row is None:
            # 跨租户与不存在同样是 404（E28-S43-01）。
            raise ServiceError(404, "ATTEMPT_NOT_FOUND",
                               f"no distribution attempt {attempt_id!r}")
        return row

    def _attempt_view(self, attempt: dict, package: dict,
                      note: str | None = None) -> dict:
        body = self._attempt_row(attempt)
        body["package_id"] = package["package_id"]
        body["package_state"] = package["state"] if "state" in package else None
        body["operation_id"] = ("op_" + _sha256_hex(
            f"{package['package_id']}:{attempt['attempt_id']}")[:20])
        if note:
            body["note"] = note
        return body

    def _latest_attempt(self, tenant_id: str, package_id: str,
                        channel_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=? AND channel_id=?"
            " ORDER BY attempt_no DESC LIMIT 1",
            (tenant_id, package_id, channel_id)).fetchone())

    def _live_attempt(self, tenant_id: str, package_id: str,
                      channel_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=? AND channel_id=?"
            " AND state IN ('IN_FLIGHT','UNKNOWN')"
            " ORDER BY attempt_no DESC LIMIT 1",
            (tenant_id, package_id, channel_id)).fetchone())

    def _pending_attempt(self, tenant_id: str, package_id: str,
                         channel_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=? AND channel_id=? AND state='PENDING'"
            " ORDER BY attempt_no DESC LIMIT 1",
            (tenant_id, package_id, channel_id)).fetchone())

    def _unknown_attempt(self, tenant_id: str,
                         package_id: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM distribution_attempt WHERE tenant_id=?"
            " AND package_id=? AND state='UNKNOWN'"
            " ORDER BY attempt_no DESC LIMIT 1",
            (tenant_id, package_id)).fetchone())

    def _find_attempt_for_receipt(self, tenant_id: str, package_id,
                                  channel_id: str,
                                  remote_id) -> dict | None:
        if not package_id:
            return None
        if remote_id:
            row = _row(self.conn.execute(
                "SELECT * FROM distribution_attempt WHERE tenant_id=?"
                " AND package_id=? AND channel_id=? AND remote_ref=?"
                " ORDER BY attempt_no DESC LIMIT 1",
                (tenant_id, package_id, channel_id,
                 str(remote_id))).fetchone())
            if row is not None:
                return row
        return self._live_attempt(tenant_id, package_id, channel_id)

    # ------------------------------------------------------------ 渠道
    def put_channel(self, tenant_id: str, channel_id: str, body: dict) -> dict:
        """`POST /channels`（S43-26）。`enabled` 默认 false，不预填 true。"""
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
        kind = body.get("kind")
        if kind not in CHANNEL_KINDS:
            raise ServiceError(422, "INVALID_CHANNEL_KIND",
                               "kind must be one of " + ", ".join(CHANNEL_KINDS))
        if not isinstance(body.get("endpoint_ref", {}), dict):
            raise ServiceError(422, "INVALID_CHANNEL_ENDPOINT",
                               "endpoint_ref must be an object")
        accepted = body.get("accepted_formats") or []
        required = body.get("required_artifacts") or []
        unknown = [role for role in required if role not in ITEM_ROLES]
        if unknown:
            raise ServiceError(422, "UNKNOWN_ITEM_ROLE",
                               f"required_artifacts contains unknown roles "
                               f"{unknown}")
        enabled = 1 if body.get("enabled") is True else 0
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO channel (tenant_id, channel_id, kind,"
                " endpoint_ref, accepted_formats, required_artifacts,"
                " max_file_bytes, supports_resume, supports_query, enabled,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, channel_id) DO UPDATE SET"
                " kind=excluded.kind, endpoint_ref=excluded.endpoint_ref,"
                " accepted_formats=excluded.accepted_formats,"
                " required_artifacts=excluded.required_artifacts,"
                " max_file_bytes=excluded.max_file_bytes,"
                " supports_resume=excluded.supports_resume,"
                " supports_query=excluded.supports_query,"
                " enabled=excluded.enabled, updated_at=excluded.updated_at",
                (tenant_id, channel_id, kind, _jcs(body.get("endpoint_ref")
                                                   or {}),
                 _jcs(list(accepted)), _jcs(list(required)),
                 _int(body.get("max_file_bytes")),
                 1 if body.get("supports_resume") is True else 0,
                 1 if body.get("supports_query") is True else 0,
                 enabled, now, now))
        return self._channel_view(self._channel(tenant_id, channel_id))

    def list_channels(self, tenant_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM channel WHERE tenant_id=? ORDER BY channel_id",
            (tenant_id,)).fetchall()
        return {"items": [self._channel_view(row) for row in rows]}

    @staticmethod
    def _channel_view(row: dict) -> dict:
        """渠道的**唯一**对外形状（`POST` 与 `GET` 必须是同一个）。

        库里 `enabled` / `supports_*` 是 0/1，`endpoint_ref` 等是 JCS 文本；
        原样返回会让 `POST /channels` 给出 `"enabled": 0` 而 `GET` 给出
        `false`——同一资源两种形状，调用方无法判断自己读到的是不是布尔。
        """
        return {
            "channel_id": row["channel_id"],
            "kind": row["kind"],
            "endpoint_ref": _loads(row["endpoint_ref"], {}),
            "accepted_formats": _loads(row["accepted_formats"], []),
            "required_artifacts": _loads(row["required_artifacts"], []),
            "max_file_bytes": row["max_file_bytes"],
            "supports_resume": bool(row["supports_resume"]),
            "supports_query": bool(row["supports_query"]),
            "enabled": bool(row["enabled"]),
        }

    # ------------------------------------------------------------ 消费事件
    def consume_event(self, tenant_id: str, envelope: dict) -> dict:
        """消费事件只更新**投影**。投影永不放行任何门（S43-25）。"""
        if not isinstance(envelope, dict):
            raise ServiceError(400, "INVALID_BODY",
                               "event envelope must be an object")
        event_id = str(envelope.get("event_id") or envelope.get("id") or "")
        if not event_id:
            raise ServiceError(400, "INVALID_BODY", "event_id is required")
        event_name = str(envelope.get("type") or envelope.get("event_name")
                         or envelope.get("event_type") or "")
        consumer = CONSUMERS.get(event_name)
        if consumer is None:
            raise ServiceError(422, "UNKNOWN_EVENT",
                               f"S43 does not consume {event_name!r}")
        envelope_tenant = envelope.get("tenant_id")
        if envelope_tenant is not None and str(envelope_tenant) != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "the event belongs to another tenant")
        data = (envelope.get("data") if isinstance(envelope.get("data"), dict)
                else {})
        now = _now()

        with Transaction(self.conn):
            if not self.inbox.consume(consumer, event_id):
                return {"accepted": True, "duplicate": True,
                        "event": event_name}

            if event_name == EVENT_VOICE_AUTH:
                voice_id = str(data.get("voice_id") or "")
                if not voice_id:
                    raise ServiceError(400, "INVALID_BODY",
                                       "data.voice_id is required")
                seq = _int(data.get("state_seq")) or 0
                # 乱序：更旧的序号不能覆盖更新的投影（E17-S43-04 同类）。
                self.conn.execute(
                    "INSERT INTO voice_authorization_projection (tenant_id,"
                    " voice_id, state, state_seq, effective_at, event_id,"
                    " updated_at) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(tenant_id, voice_id) DO UPDATE SET"
                    " state=excluded.state, state_seq=excluded.state_seq,"
                    " effective_at=excluded.effective_at,"
                    " event_id=excluded.event_id, updated_at=excluded.updated_at"
                    " WHERE excluded.state_seq > state_seq",
                    (tenant_id, voice_id, str(data.get("to_state") or
                                              data.get("state") or "unknown"),
                     seq, data.get("effective_at"), event_id, now))
                return {"accepted": True, "event": event_name,
                        "voice_id": voice_id}

            if event_name == EVENT_SUBTITLE_READY:
                track_id = str(data.get("track_id") or "")
                if not track_id:
                    raise ServiceError(400, "INVALID_BODY",
                                       "data.track_id is required")
                revision = _int(data.get("subtitle_revision")) or 0
                self.conn.execute(
                    "INSERT INTO subtitle_projection (tenant_id, track_id,"
                    " target_locale, timeline_revision, subtitle_revision,"
                    " qc_decision, defect_count, artifact_ref, event_id,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(tenant_id, track_id) DO UPDATE SET"
                    " target_locale=excluded.target_locale,"
                    " timeline_revision=excluded.timeline_revision,"
                    " subtitle_revision=excluded.subtitle_revision,"
                    " qc_decision=excluded.qc_decision,"
                    " defect_count=excluded.defect_count,"
                    " artifact_ref=excluded.artifact_ref,"
                    " event_id=excluded.event_id,"
                    " updated_at=excluded.updated_at"
                    " WHERE excluded.subtitle_revision > subtitle_revision",
                    (tenant_id, track_id,
                     str(data.get("target_locale") or ""),
                     _int(data.get("timeline_revision")) or 0, revision,
                     data.get("qc_decision"),
                     _int(data.get("defect_count")) or 0,
                     _jcs(data.get("artifact_ref"))
                     if data.get("artifact_ref") else None,
                     event_id, now))
                return {"accepted": True, "event": event_name,
                        "track_id": track_id}

            if event_name == EVENT_SNAPSHOT_COMMITTED:
                manifest_id = str(data.get("manifest_id") or "")
                if not manifest_id:
                    raise ServiceError(400, "INVALID_BODY",
                                       "data.manifest_id is required")
                revision = _int(data.get("revision")) or 0
                # 注意：这里的 `execution_fence` 是 S08 的 manifest 头坐标，
                # 与 `node_epoch` 无关，只作投影，不参与任何比较。
                self.conn.execute(
                    "INSERT INTO manifest_projection (tenant_id, manifest_id,"
                    " revision, execution_fence, accepted_revision, event_id,"
                    " updated_at) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(tenant_id, manifest_id) DO UPDATE SET"
                    " revision=excluded.revision,"
                    " execution_fence=excluded.execution_fence,"
                    " accepted_revision=excluded.accepted_revision,"
                    " event_id=excluded.event_id,"
                    " updated_at=excluded.updated_at"
                    " WHERE excluded.revision > revision",
                    (tenant_id, manifest_id, revision,
                     _int(data.get("execution_fence")) or 0,
                     _int(data.get("accepted_revision")), event_id, now))
                return {"accepted": True, "event": event_name,
                        "manifest_id": manifest_id}

            # EVENT_MASTER_RENDERED
            candidate_id = str(data.get("candidate_id")
                               or data.get("master_candidate_id") or "")
            if not candidate_id:
                raise ServiceError(400, "INVALID_BODY",
                                   "data.candidate_id is required")
            self.conn.execute(
                "INSERT INTO candidate_projection (tenant_id, candidate_id,"
                " deliverable, timeline_revision, event_id, updated_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, candidate_id) DO UPDATE SET"
                " deliverable=excluded.deliverable,"
                " timeline_revision=excluded.timeline_revision,"
                " event_id=excluded.event_id, updated_at=excluded.updated_at",
                (tenant_id, candidate_id,
                 1 if data.get("deliverable") is True else 0,
                 _int(data.get("timeline_revision")), event_id, now))
            return {"accepted": True, "event": event_name,
                    "candidate_id": candidate_id}

    # ------------------------------------------------------------ 事件与审计
    def _emit_published(self, tenant_id: str, package: dict, attempt: dict,
                        channel: dict, remote_id, command_id: str) -> None:
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_PUBLISHED,
            subject=package["package_id"], tenant_id=tenant_id,
            home_cell=package["home_cell"], aggregate_id=package["package_id"],
            sequence=self._next_sequence(tenant_id, package["package_id"]),
            correlation_id=command_id, causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "package_id": package["package_id"],
                "package_type": package["package_type"],
                "episode_id": package.get("episode_id"),
                "master_candidate_id": package["master_candidate_id"],
                "package_hash": package["package_hash"],
                "manifest_hash": package["manifest_hash"],
                "channel_id": channel["channel_id"],
                "attempt_id": attempt["attempt_id"],
                "remote_id": remote_id,
                "published_at": package.get("published_at") or _now(),
                "policy_decision_id": self._policy_decision_id(
                    tenant_id, attempt),
            },
            data_ref={"service_id": SOURCE, "id": package["package_id"],
                      "revision": 3,
                      "content_hash": package["manifest_hash"]},
            schema_version=SCHEMA_VERSION))

    def _emit_failed(self, tenant_id: str, package: dict, attempt: dict,
                     channel: dict, failure_code: str, state: str,
                     bytes_sent: int, failure_detail, remote_ref,
                     command_id: str) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "package_id": package["package_id"],
            "package_type": package["package_type"],
            "episode_id": package.get("episode_id"),
            "channel_id": channel["channel_id"],
            "attempt_id": attempt["attempt_id"],
            "attempt_no": attempt["attempt_no"],
            "state": state,
            "failure_code": failure_code,
            "bytes_sent": int(bytes_sent or 0),
            "remote_ref": remote_ref,
            "finished_at": attempt.get("finished_at") or _now(),
        }
        if failure_detail:
            data["failure_detail"] = (failure_detail
                                      if isinstance(failure_detail, dict)
                                      else {"detail": str(failure_detail)})
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_FAILED,
            subject=package["package_id"], tenant_id=tenant_id,
            home_cell=package["home_cell"], aggregate_id=package["package_id"],
            sequence=self._next_sequence(tenant_id, package["package_id"]),
            correlation_id=command_id, causation_id=command_id,
            data=data,
            data_ref={"service_id": SOURCE, "id": attempt["attempt_id"],
                      "revision": attempt["attempt_no"],
                      "content_hash": package.get("manifest_hash")},
            schema_version=SCHEMA_VERSION))

    def _policy_decision_id(self, tenant_id: str, attempt: dict) -> str:
        row = _row(self.conn.execute(
            "SELECT policy_decision_id FROM publish_authorization_ref"
            " WHERE tenant_id=? AND auth_ref_id=?",
            (tenant_id, attempt.get("auth_ref_id"))).fetchone())
        if row is None:
            # 走到这里说明 attempt 没有授权引用——状态写错了，不能编一个 id。
            raise ServiceError(500, "AUTHORIZATION_REF_MISSING",
                               "the attempt has no publish authorization "
                               "reference; refusing to emit a published event "
                               "without one")
        return row["policy_decision_id"]

    def _audit_intent(self, tenant_id: str, package_id: str,
                      attempt_id: str | None, verb: str, action: str,
                      outcome: str, details: dict) -> str:
        """先落本地意图，再尝试送达 S45（§4.3 第 7 步 / E39）。"""
        intent_id = _new_id("audit")
        payload = {
            "producer_id": SOURCE,
            "tenant_id": tenant_id,
            "action": action,
            "outcome": outcome,
            "package_id": package_id,
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "details": details,
        }
        digest = _sha256_hex(_jcs(payload))
        self.conn.execute(
            "INSERT INTO audit_intent (tenant_id, intent_id, package_id,"
            " attempt_id, verb, action, outcome, record_id, delivered,"
            " payload_digest, sent_at) VALUES (?,?,?,?,?,?,?,NULL,0,?,?)",
            (tenant_id, intent_id, package_id, attempt_id, verb, action,
             outcome, digest, _now()))
        return intent_id

    def _deliver_audits(self, tenant_id: str,
                        intent_ids: list[str] | None = None) -> list[dict]:
        """尝试把未送达的意图发给 S45。

        S45 不可达**不影响已提交的包状态**（意图已在本库，`delivered=0`），
        但也不会被写成"已审计"。
        """
        if intent_ids:
            placeholders = ",".join("?" for _ in intent_ids)
            rows = self.conn.execute(
                "SELECT * FROM audit_intent WHERE tenant_id=? AND delivered=0"
                f" AND intent_id IN ({placeholders})",
                [tenant_id, *intent_ids]).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_intent WHERE tenant_id=? AND delivered=0"
                " ORDER BY sent_at LIMIT 50", (tenant_id,)).fetchall()
        results = []
        for row in rows:
            intent = {
                "producer_id": SOURCE,
                "tenant_id": tenant_id,
                "action": row["action"],
                "outcome": row["outcome"],
                "package_id": row["package_id"],
                "attempt_id": row["attempt_id"],
                "intent_id": row["intent_id"],
                "payload_digest": row["payload_digest"],
                "occurred_at": row["sent_at"],
            }
            try:
                reply = self.audit_sink(tenant_id, intent)
            except (ServiceError, ConnectionError) as exc:
                results.append({"intent_id": row["intent_id"],
                                "delivered": False, "error": str(exc)})
                continue
            record_id = None
            if isinstance(reply, dict):
                record_id = reply.get("record_id") or reply.get("id")
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE audit_intent SET delivered=1, record_id=?"
                    " WHERE tenant_id=? AND intent_id=?",
                    (record_id, tenant_id, row["intent_id"]))
            results.append({"intent_id": row["intent_id"], "delivered": True,
                            "record_id": record_id})
        return results

    def pending_audits(self, tenant_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM audit_intent WHERE tenant_id=? AND"
            " delivered=0", (tenant_id,)).fetchone()
        return {"undelivered_audit_intents": int(rows[0])}
