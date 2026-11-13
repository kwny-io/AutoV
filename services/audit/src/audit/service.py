"""S45 内核。方案：`plans/S45-audit.md`。

一句话边界：**S45 只记录"发生过什么"，不判定对错、不保存正文、不给任何服务
当第二权威。**

三条设计不变量，它们不是"最佳实践"，是本服务存在的理由：

1. **摄入对传输失败宽容，对内容形状严格。** 别的服务把审计当可降级依赖
   （S19 的方案明说派发审计失败不能阻塞业务），所以这里不因上游抖动拒收一条
   已经成形的记录；但"正文"（payload/prompt/arguments）一律 422——那是形状
   违规，不是脱敏问题（§2.2：details 只放引用与计数）。
2. **脱敏在哈希之前，且永不拒收。** `redaction-policy.v2` 定死
   `redact_and_store`：存下来的就是 canonical 原样，否则这条记录以及它之后的
   每一条都无法复算 `record_hash`（§2.5、§3.3）。拒收等于让生产方用一串密钥
   形状的字符串抹掉自己的审计行。
3. **链只增不改。** `audit_record` 的两条写路径（UPDATE/DELETE）在 schema 层
   就被触发器挡死（§1.5）；检出不连续时冻结并告警，**不自动修复**——用当前
   内容覆盖归档内容，那正好抹掉了要查的差异（§3.5）。

本内核在 SQLite 上跑，方案假设的是 PG 分区表。没有实现的部分全部登记在
`schema.CONTRACT_GAPS`（NEW-2 最要紧：销毁在本内核里只是"标记为 DESTROYED 并
从查询中排除"，字节仍留在库里）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import CONTRACT_GAPS, ID_PREFIX, PERMISSIONS, SCHEMA

SOURCE = "S45"
SCHEMA_VERSION = "1.0"
EVENT_EXPORT_READY = "audit.export.ready"
TOPIC_AUDIT = "drama.audit.v2"
CONSUMER_INGRESS = "s45.ingress"

GENESIS_HASH = "0" * 64

# §3.4 封印条件
SEGMENT_MAX_RECORDS = 10_000
SEGMENT_MAX_AGE_SECONDS = 3600
SEGMENT_IDLE_SECONDS = 900

# §4.4：details 超过 64KB 就说明正文走错门了。
MAX_DETAILS_BYTES = 64 * 1024

# §3.3 canonical 覆盖的键。**这不是"记录的全部字段"**——on_behalf_of、
# source_verb、primary_target_index、source_intent_id 落库但不在哈希里，
# 因为方案自己的复算公式（§3.3）逐字列了这 18 个键。照方案实现，
# 偏差登记为 NEW-5，并在 get_record 里标出 covered_by_hash=false。
CANONICAL_KEYS = (
    "tenant_id", "stream_key", "stream_seq", "recorded_at", "occurred_at",
    "source_service", "source_event_id", "action", "actor", "targets",
    "policy_ref", "evidence_refs", "outcome", "outcome_detail", "details",
    "correlation_id", "causation_id", "schema_version",
)

OUTCOMES = ("SUCCEEDED", "DENIED", "ERROR", "REPORTED_UNKNOWN")

# §2.2："details 是引用与计数，不是内容"。出现这些键说明生产方把正文塞进来了。
# 这不是密钥问题（密钥走脱敏），是形状问题——正文应当给 data_ref。
RAW_CONTENT_KEYS = frozenset({
    "arguments", "tool_arguments", "tool_input", "function_arguments",
    "payload", "raw_payload", "request_body", "response_body",
    "prompt", "prompt_text", "system_prompt", "script", "script_text",
    "manuscript", "file_content", "media_bytes", "blob",
})

# 授权拒绝的错误码。§4.4 只给了 read / export 两个；destroy 与
# retention.change 没有自己的码（并入 NEW-1 的说明）。
DENY_CODE = {
    PERMISSIONS["read"]: "AUDIT_READ_DENIED",
    PERMISSIONS["export"]: "AUDIT_EXPORT_DENIED",
    PERMISSIONS["destroy"]: "AUDIT_DESTROY_DENIED",
    PERMISSIONS["retention_change"]: "AUDIT_RETENTION_CHANGE_DENIED",
}

# 保留策略未配置时的默认档位（§3.7 的"默认"一列）。
DEFAULT_POLICY = {
    "retention_hot_days": 90,
    "retention_warm_days": 730,
    "retention_total_days": None,
    "worm_required": 0,
    "anchor_required": 0,
    "strict_audit": 0,
}

# ------------------------------------------------------------ 脱敏（§2.5）

# 六个类全部来自 contracts/redaction-policy.v2.json 的 must_match。
# `api_key` 的第三支是 32–64 位字母数字的通用分支，契约明确要求
# "已被 schema 标为 content_hash / id / revision 的值必须先豁免"，
# 所以扫描是**带字段名**的：_GENERIC_EXEMPT_KEYS 里的值不套通用分支。
# 顺序是有语义的：**带前缀的形状必须先于通用分支**。反过来的话
# `Bearer qqqq…`（32 个 q）会先被 api_key 的"32–64 位字母数字"吃掉，
# 于是类名报成 api_key、HMAC 的可关联性也跟着错。这也是为什么
# `api_key` 被拆成两条：只有**通用**那一条受字段名豁免约束，
# `sk-` / `ak_live_` 这种明确前缀在任何字段名里都要脱敏。
SECRET_PATTERNS = (
    ("bearer_token", re.compile(
        r"(?i)(\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
     False),
    ("signature_secret", re.compile(
        r"(?i)(\bwhsec_[A-Za-z0-9+/=_-]{8,}"
        r"|X-Drama-Signature\s*[:=]\s*t=\d+\s*,\s*v1=[0-9a-f]{16,}"
        r"|\bv1=[0-9a-f]{32,})"), False),
    ("private_key_material", re.compile(
        r"(?is)(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
        r"|\b(?:password|passwd|pwd)\b\s*[:=]\s*\S{4,})"), False),
    ("connection_string", re.compile(
        r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
        r"|(?<![A-Za-z])[?&](X-Amz-Signature|X-Goog-Signature|sig|token)="
        r"[A-Za-z0-9%._~+/=-]{16,})"), False),
    ("provider_credential", re.compile(
        r"(?i)((?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
        r"|\bclient_secret\b\s*[:=]\s*\S{8,})"), False),
    ("api_key", re.compile(
        r"(?i)(\bak_(live|test)_[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9_-]{16,})"),
     False),
    # 通用分支：没有前缀，只能靠长度与字符集判断，因此必须让位于字段名豁免。
    ("api_key", re.compile(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9]{32,64}(?![A-Za-z0-9])"), True),
)

# class_a：高熵、可关联（HMAC 截断）；class_b：低熵、不可关联。
CLASS_A = frozenset({"api_key", "bearer_token", "provider_credential"})
_GENERIC_EXEMPT_KEYS = re.compile(
    r"(?i)(^|_)(hash|sha256|digest|id|ids|revision|rev|seq|epoch|version)($|_)")
_SIXTY_FOUR_HEX = re.compile(r"^[0-9a-f]{64}$")

# 已存在的标记形态。用于 (a) 幂等（第二遍不再加工自己的产物）
# (b) 调用方内容逃逸的判定。
_MARKER = re.compile(r"^\\?REDACTED:(?:hmac_[a-z_]+:[0-9a-f]{16}|[a-z_]+)(?:$|[:\s].*)$",
                     re.S)

# 测试白名单。生产构建里这段路径不存在，不是一个可以打开的开关。
_TEST_ALLOWLIST = re.compile(r"(?i)^TEST_?SECRET_")

SCANNER_UNAVAILABLE_MARKER = "REDACTED:scanner_unavailable"


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _fmt(_now_dt())


def _fmt(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def redact_leaf(text: str, path: str, key: str | None,
                secret_key: bytes | None) -> tuple[str, list[str]]:
    """对单个字符串叶子执行 `PAYLOAD_SECRET_SCAN`。

    三个契约细节在这里落地：

    - **只替换命中的子串**，其余字节保留（structural_rule）；
    - 已存在 `REDACTED:` 前缀的调用方内容加反斜杠逃逸，但已经是标记形态的值
      不再加工——否则第二遍扫描会改字节，破坏 `scan_execution.idempotence`；
    - 通用 32–64 位分支对 `*_hash` / `*_id` / `*_revision` 一类字段豁免
      （api_key 的 false_positive_note），否则每个 content_hash 都会被抹掉。
    """
    if not text:
        return text, []
    hits: list[str] = []
    result = text
    for name, pattern, generic in SECRET_PATTERNS:
        if generic and _generic_exempt(key, result):
            continue
        match = pattern.search(result)
        while match is not None:
            secret = match.group(0)
            if _TEST_ALLOWLIST.match(secret):
                break
            if name in CLASS_A and secret_key is not None:
                digest = hmac.new(secret_key, secret.encode("utf-8"),
                                  hashlib.sha256).hexdigest()[:16]
                replacement = f"REDACTED:hmac_{name}:{digest}"
            elif name in CLASS_A:
                # C45-7 R-1 未裁决：没有 KMS 键时退化为不可关联形态，
                # 但**绝不**落明文。
                replacement = f"REDACTED:{name}"
            else:
                replacement = f"REDACTED:{name}"
            result = result[:match.start()] + replacement + result[match.end():]
            hits.append(name)
            match = pattern.search(result, match.start() + len(replacement))
    if result.startswith("REDACTED:") and not _MARKER.match(result):
        result = "\\" + result
    return result, hits


def _generic_exempt(key: str | None, value: str) -> bool:
    if key and _GENERIC_EXEMPT_KEYS.search(key):
        return True
    return bool(_SIXTY_FOUR_HEX.match(value))


def _truncate8(value: str) -> str:
    return value[:8] + SCANNER_UNAVAILABLE_MARKER


def _walk_strings(value, path: str, fn):
    """叶子替换，保持外层 JSON 形状（数组长度、键名都不变）。"""
    if isinstance(value, str):
        return fn(value, path)
    if isinstance(value, list):
        return [_walk_strings(item, f"{path}[{i}]", fn)
                for i, item in enumerate(value)]
    if isinstance(value, dict):
        return {k: _walk_strings(v, f"{path}.{k}" if path else k, fn)
                for k, v in value.items()}
    return value


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.retry_after = retry_after


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 服务

class AuditService:
    def __init__(self, conn: sqlite3.Connection, *, authorization_source=None,
                 scanner=None, backpressure_source=None,
                 redact_key: bytes | None = None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 同 model-routing：`Outbox.__init__` / `Inbox.__init__` 会 executescript，
        # 而 executescript 隐式提交当前事务。只能在事务外建一次，之后复用，
        # 否则第一条写入就会把命令事务悄悄提交掉。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        # 未配置授权源 = fail closed（E27）。写路径**不**问 S01（§2.4），
        # 所以摄入在没有授权源时仍然可用——这正是"可降级"的形态。
        self.authorization_source = authorization_source
        # 扫描器不可用必须让 readiness 失败（redaction-policy invariants），
        # 所以 None 不是"跳过扫描"，是"这个服务不该接流量"。
        self.scanner = scanner if scanner is not None else redact_leaf
        self.backpressure_source = backpressure_source or (lambda: False)
        self.redact_key = redact_key

    # ------------------------------------------------------------ 授权
    def _require(self, tenant_id: str, principal_id: str, scope_kind: str,
                 scope_id: str, permission: str) -> dict:
        if not self.authorization_source:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S01 authorization source is not configured; refusing to "
                "serve audit reads/exports without an authorization context "
                "(plan §10.2, fail closed)", retryable=True)
        try:
            result = self.authorization_source(
                tenant_id, principal_id, scope_kind, scope_id, permission)
        except ConnectionError as exc:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        except ServiceError:
            raise
        if not result.get("allowed"):
            raise ServiceError(403, DENY_CODE.get(permission, "AUDIT_READ_DENIED"),
                               f"principal is not granted {permission}")
        return result

    # ------------------------------------------------------------ 载荷校验
    def _validate(self, intent: dict) -> dict:
        """§2.2 的四条硬规则 + §4.4 的错误码。

        返回规范化后的副本：时间戳统一成定宽 UTC 毫秒串，`targets` 补齐为列表。
        **不认识的 major 版本直接拒**，不猜（§2.2 末条）。
        """
        if not isinstance(intent, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "audit intent must be a JSON object")
        version = intent.get("schema_version")
        if not version or not isinstance(version, str):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "schema_version is required")
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ServiceError(422, "SCHEMA_INVALID",
                               f"unknown schema major: {version}")

        out = dict(intent)
        occurred = _parse_ts(intent.get("occurred_at"))
        if occurred is None:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "occurred_at must be an RFC3339 timestamp")
        out["occurred_at"] = _fmt(occurred)

        for field in ("source_service", "source_verb", "action"):
            value = intent.get(field)
            if not isinstance(value, str) or not value:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"{field} is required")

        actor = intent.get("actor")
        if not isinstance(actor, dict) or not actor.get("principal_id"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "actor.principal_id is required")
        if actor.get("principal_type") not in ("human", "service",
                                               "api_client", "mcp_client"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "actor.principal_type must be human|service|"
                               "api_client|mcp_client")

        targets = intent.get("targets")
        # §4.4：targets 为空属 400（这条记录无法回答"对什么"）。
        if not isinstance(targets, list) or not targets:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "targets must be a non-empty array")
        for target in targets:
            if (not isinstance(target, dict)
                    or not target.get("kind") or not target.get("id")
                    or not target.get("service_id")):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   "each target needs service_id/kind/id")
        index = intent.get("primary_target_index", 0)
        if not isinstance(index, int) or not 0 <= index < len(targets):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "primary_target_index is out of range")
        out["primary_target_index"] = index
        out["targets"] = targets

        outcome = intent.get("outcome")
        if outcome not in OUTCOMES:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "outcome must be one of " + ", ".join(OUTCOMES))

        for field in ("policy_ref", "evidence_refs", "outcome_detail", "details"):
            value = intent.get(field)
            if value is not None and not isinstance(value, (dict, list)):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"{field} must be an object or array")

        details = intent.get("details") or {}
        if isinstance(details, dict):
            size = len(_jcs(details).encode("utf-8"))
            if size > MAX_DETAILS_BYTES:
                raise ServiceError(
                    422, "PAYLOAD_TOO_LARGE",
                    f"details is {size} bytes; bodies belong behind data_ref",
                    details={"limit": MAX_DETAILS_BYTES, "actual": size})
            offending = _find_raw_content(details)
            if offending:
                # §2.2：正文（payload/prompt/arguments）不属于 details。
                # 这与"密钥不拒收"不冲突：那是脱敏，这是形状（NEW-6）。
                raise ServiceError(
                    422, "SCHEMA_INVALID",
                    "details must contain references and counts, not content",
                    details={"forbidden_field": offending,
                             "hint": "store a data_ref to the owning service"})

        out["details"] = details
        out["outcome_detail"] = intent.get("outcome_detail") or {}
        out["evidence_refs"] = intent.get("evidence_refs") or []
        out["policy_ref"] = intent.get("policy_ref")
        out["on_behalf_of"] = intent.get("on_behalf_of")
        out["correlation_id"] = intent.get("correlation_id")
        out["causation_id"] = intent.get("causation_id")
        return out

    # ------------------------------------------------------------ 脱敏
    def _redact(self, intent: dict) -> tuple[dict, list[dict]]:
        """在计算 canonical **之前**执行 `PAYLOAD_SECRET_SCAN`（§2.5）。

        只扫 details / outcome_detail / on_behalf_of / policy_ref /
        evidence_refs。action / actor / targets / outcome 一律不动——一条
        "发生过一次脱敏但没说是什么操作"的记录通不过 E39（NEW-7）。
        """
        hits: list[dict] = []

        def visit(value, path, key):
            try:
                new_value, classes = self.scanner(value, path, key,
                                                  self.redact_key)
                if classes is True:
                    # 扫描器自己决定"认不出"，按失败闭合处理。
                    return _truncate8(value), ["scanner_unavailable"]
                for name in classes or []:
                    hits.append({"path": path, "class": name})
                return new_value
            except Exception:  # noqa: BLE001 - 失败闭合，绝不放行明文
                hits.append({"path": path, "class": "scanner_unavailable"})
                return _truncate8(value)

        out = dict(intent)
        for field in ("details", "outcome_detail", "on_behalf_of",
                      "policy_ref", "evidence_refs"):
            value = intent.get(field)
            if value is None:
                continue
            out[field] = _walk_strings(
                value, field,
                lambda text, path: visit(text, path, path.rsplit(".", 1)[-1]
                                         .split("[")[0]))
        return out, hits

    # ------------------------------------------------------------ 追加
    def _append(self, tenant_id: str, intent: dict, *, source_event_id,
                source_intent_id, record_id: str | None = None,
                recorded_at: str | None = None, allow_self_record: bool = True):
        """§3.1 的本地部分：redact → canonical → hash → insert → index → head。"""
        if self.backpressure_source():
            # §7.1 / §4.4：调用方保留自己的 outbox 行，退避重试。
            raise ServiceError(429, "AUDIT_SINK_BACKPRESSURE",
                               "audit ingest queue is full", retryable=True,
                               retry_after=2)

        if source_intent_id:
            existing = self.conn.execute(
                "SELECT record_id, stream_seq, record_hash FROM audit_record "
                "WHERE tenant_id=? AND source_intent_id=?",
                (tenant_id, source_intent_id)).fetchone()
            if existing is not None:
                # §10.1：intent_id 全局唯一。同意图换个 event_id 再来一次，
                # 仍是同一条事实——回放，不追加。
                return {"duplicate": True, "record_id": existing["record_id"],
                        "stream_seq": existing["stream_seq"],
                        "record_hash": existing["record_hash"],
                        "redactions": []}

        redacted, hits = self._redact(intent)

        targets = redacted["targets"]
        index = redacted["primary_target_index"]
        primary = targets[index]
        stream_key = f"tenant:{tenant_id}:subject:{primary['kind']}:{primary['id']}"
        stream_kind = "subject"

        head = self.conn.execute(
            "SELECT head_seq, head_hash, seal_state FROM audit_stream "
            "WHERE tenant_id=? AND stream_key=?",
            (tenant_id, stream_key)).fetchone()
        if head is None:
            seq, prev_hash = 1, GENESIS_HASH
            self.conn.execute(
                "INSERT INTO audit_stream (tenant_id, stream_key, stream_kind,"
                " head_seq, head_hash, seal_state, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, 'OPEN', ?, ?)",
                (tenant_id, stream_key, stream_kind, GENESIS_HASH,
                 _now(), _now()))
        else:
            if head["seal_state"] != "OPEN":
                # §3.4：不让追加绕过封印，否则 root hash 与记录集合不一致。
                raise ServiceError(409, "STREAM_SEALING",
                                   "stream is sealing; retry after the seal "
                                   "completes", retryable=True, retry_after=2)
            seq, prev_hash = head["head_seq"] + 1, head["head_hash"]

        recorded_at = recorded_at or _now()
        canonical = _jcs({
            "tenant_id": tenant_id,
            "stream_key": stream_key,
            "stream_seq": seq,
            "recorded_at": recorded_at,
            "occurred_at": redacted["occurred_at"],
            "source_service": redacted["source_service"],
            "source_event_id": source_event_id,
            "action": redacted["action"],
            "actor": redacted["actor"],
            "targets": targets,
            "policy_ref": redacted.get("policy_ref"),
            "evidence_refs": redacted.get("evidence_refs") or [],
            "outcome": redacted["outcome"],
            "outcome_detail": redacted.get("outcome_detail") or {},
            "details": redacted.get("details") or {},
            "correlation_id": redacted.get("correlation_id"),
            "causation_id": redacted.get("causation_id"),
            "schema_version": redacted["schema_version"],
        })
        record_hash = _sha256_hex(prev_hash + canonical)
        canonical_sha256 = _sha256_hex(canonical)
        record_id = record_id or _new_id("record")

        occurred = _parse_ts(redacted["occurred_at"])
        skew = None
        if occurred is not None:
            skew = int((_parse_ts(recorded_at) - occurred).total_seconds() * 1000)

        self.conn.execute(
            "INSERT INTO audit_record (record_id, tenant_id, stream_key,"
            " stream_seq, recorded_at, occurred_at, clock_skew_ms,"
            " source_service, source_verb, source_event_id, source_intent_id,"
            " correlation_id, causation_id, action, actor, on_behalf_of,"
            " targets, primary_target_index, policy_ref, evidence_refs,"
            " outcome, outcome_detail, details, schema_version, prev_hash,"
            " record_hash, canonical_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, tenant_id, stream_key, seq, recorded_at,
             redacted["occurred_at"], skew, redacted["source_service"],
             redacted["source_verb"], source_event_id, source_intent_id,
             redacted.get("correlation_id"), redacted.get("causation_id"),
             redacted["action"], _jcs(redacted["actor"]),
             _jcs(redacted["on_behalf_of"])
             if redacted.get("on_behalf_of") is not None else None,
             _jcs(targets), index,
             _jcs(redacted["policy_ref"])
             if redacted.get("policy_ref") is not None else None,
             _jcs(redacted.get("evidence_refs") or []),
             redacted["outcome"], _jcs(redacted.get("outcome_detail") or {}),
             _jcs(redacted.get("details") or {}), redacted["schema_version"],
             prev_hash, record_hash, canonical_sha256))

        for target in targets:
            self.conn.execute(
                "INSERT INTO audit_record_target (tenant_id, target_service,"
                " target_kind, target_id, recorded_at, record_id, stream_key,"
                " stream_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, target["service_id"], target["kind"],
                 target["id"], recorded_at, record_id, stream_key, seq))

        self.conn.execute(
            "UPDATE audit_stream SET head_seq=?, head_hash=?, updated_at=? "
            "WHERE tenant_id=? AND stream_key=?",
            (seq, record_hash, _now(), tenant_id, stream_key))

        self._touch_segment(tenant_id, stream_key, stream_kind, seq,
                            prev_hash, record_hash, recorded_at)

        if hits and allow_self_record:
            # §2.5：一次脱敏一条自审记录，写进 meta 链，只带类名与受影响 id。
            self._append(
                tenant_id,
                {
                    "schema_version": SCHEMA_VERSION,
                    "occurred_at": recorded_at,
                    "source_service": SOURCE,
                    "source_verb": "audit.payload.redacted",
                    "action": "audit.payload.redacted",
                    "actor": {"principal_id": "svc_s45",
                              "principal_type": "service",
                              "delegation_id": None},
                    "targets": [{"service_id": SOURCE, "kind": "audit_record",
                                 "id": record_id}],
                    "primary_target_index": 0,
                    "policy_ref": {"service_id": SOURCE,
                                   "id": "redaction-policy.v2", "revision": 2},
                    "evidence_refs": [],
                    "outcome": "REPORTED_UNKNOWN",
                    "outcome_detail": {"reason_code": "PAYLOAD_SECRET_SCAN"},
                    "details": {"affected_record_id": record_id,
                                "classes": sorted({h["class"] for h in hits}),
                                "count": len(hits)},
                    "correlation_id": redacted.get("correlation_id"),
                    "causation_id": None,
                },
                source_event_id=f"internal:{uuid.uuid4().hex}",
                source_intent_id=None,
                recorded_at=recorded_at,
                allow_self_record=False)

        return {"duplicate": False, "record_id": record_id, "stream_seq": seq,
                "stream_key": stream_key, "record_hash": record_hash,
                "prev_hash": prev_hash, "canonical_sha256": canonical_sha256,
                "redactions": hits}

    # ------------------------------------------------------------ 分段
    def _touch_segment(self, tenant_id, stream_key, stream_kind, seq,
                       prev_hash, record_hash, recorded_at):
        open_seg = self.conn.execute(
            "SELECT * FROM audit_segment WHERE tenant_id=? AND stream_key=? "
            "AND state='OPEN' ORDER BY from_seq DESC LIMIT 1",
            (tenant_id, stream_key)).fetchone()
        if open_seg is None:
            # 非重叠：新段的 from_seq 必须大于任何已存段的 to_seq
            # （PG 侧由 EXCLUDE 保证，SQLite 没有，见 schema 注释）。
            clash = self.conn.execute(
                "SELECT segment_id FROM audit_segment WHERE tenant_id=? AND"
                " stream_key=? AND from_seq<=? AND to_seq>=?",
                (tenant_id, stream_key, seq, seq)).fetchone()
            if clash is not None:
                raise ServiceError(
                    409, "SEGMENT_OVERLAP",
                    f"segment {clash['segment_id']} already covers seq {seq}; "
                    "this is an integrity defect, not a retryable conflict")
            self.conn.execute(
                "INSERT INTO audit_segment (segment_id, tenant_id, stream_key,"
                " stream_kind, from_seq, to_seq, from_prev_hash, root_hash,"
                " record_count, always_from, always_to, state, storage_class) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'OPEN', 'HOT_PG')",
                (_new_id("segment"), tenant_id, stream_key, stream_kind, seq,
                 seq, prev_hash, record_hash, recorded_at, recorded_at))
        else:
            self.conn.execute(
                "UPDATE audit_segment SET to_seq=?, root_hash=?,"
                " record_count=record_count+1, always_to=? "
                "WHERE segment_id=?",
                (seq, record_hash, recorded_at, open_seg["segment_id"]))
        self._maybe_seal(tenant_id, stream_key)

    def _maybe_seal(self, tenant_id, stream_key):
        """§3.4 的三个封印条件。命中即 SEALING→SEALED（同事务）。"""
        seg = self.conn.execute(
            "SELECT * FROM audit_segment WHERE tenant_id=? AND stream_key=? "
            "AND state='OPEN' ORDER BY from_seq DESC LIMIT 1",
            (tenant_id, stream_key)).fetchone()
        if seg is None:
            return
        now = _now_dt()
        opened = _parse_ts(seg["always_from"])
        last = _parse_ts(seg["always_to"])
        due = (
            seg["record_count"] >= SEGMENT_MAX_RECORDS
            or (opened and (now - opened).total_seconds() >= SEGMENT_MAX_AGE_SECONDS)
            or (last and (now - last).total_seconds() >= SEGMENT_IDLE_SECONDS)
        )
        if due:
            self._seal(tenant_id, seg["segment_id"])

    def _seal(self, tenant_id, segment_id):
        seg = self.conn.execute(
            "SELECT * FROM audit_segment WHERE tenant_id=? AND segment_id=?",
            (tenant_id, segment_id)).fetchone()
        if seg is None:
            raise ServiceError(404, "NOT_FOUND", "segment not found")
        if seg["state"] != "OPEN":
            return seg["state"]
        # 重算 root hash，而不是信任累积值：封印是"把这一段的结论固定下来"。
        last = self.conn.execute(
            "SELECT record_hash FROM audit_record WHERE tenant_id=? AND"
            " stream_key=? AND stream_seq=?",
            (tenant_id, seg["stream_key"], seg["to_seq"])).fetchone()
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_record WHERE tenant_id=? AND"
            " stream_key=? AND stream_seq BETWEEN ? AND ?",
            (tenant_id, seg["stream_key"], seg["from_seq"],
             seg["to_seq"])).fetchone()["n"]
        self.conn.execute(
            "UPDATE audit_segment SET state='SEALED', root_hash=?,"
            " record_count=?, sealed_at=?, integrity_checked_at=? "
            "WHERE segment_id=?",
            (last["record_hash"] if last else seg["root_hash"], count, _now(),
             _now(), segment_id))
        self.conn.execute(
            "UPDATE audit_stream SET open_segment_id=NULL, updated_at=? "
            "WHERE tenant_id=? AND stream_key=?",
            (_now(), tenant_id, seg["stream_key"]))
        return "SEALED"

    def seal_segment(self, tenant_id: str, segment_id: str) -> dict:
        with Transaction(self.conn):
            state = self._seal(tenant_id, segment_id)
            return {"segment_id": segment_id, "state": state}

    def archive_segment(self, tenant_id: str, segment_id: str,
                        storage_ref: str | None = None) -> dict:
        """SEALED → ARCHIVED。§3.7 前置条件 1 要求"归档完成，不是正在归档"。"""
        with Transaction(self.conn):
            seg = self.conn.execute(
                "SELECT * FROM audit_segment WHERE tenant_id=? AND segment_id=?",
                (tenant_id, segment_id)).fetchone()
            if seg is None:
                raise ServiceError(404, "NOT_FOUND", "segment not found")
            if seg["state"] not in ("SEALED", "ARCHIVED", "ANCHORED"):
                raise ServiceError(
                    409, "SEGMENT_NOT_SEALED",
                    f"segment is {seg['state']}; archive requires SEALED")
            if seg["state"] == "SEALED":
                self.conn.execute(
                    "UPDATE audit_segment SET state='ARCHIVED',"
                    " storage_class='WARM_S3', storage_ref=?, archived_at=? "
                    "WHERE segment_id=?",
                    (storage_ref or f"s3://audit/{tenant_id}/{segment_id}.jsonl",
                     _now(), segment_id))
            return {"segment_id": segment_id, "state": "ARCHIVED"}

    def list_segments(self, tenant_id: str, *, stream_key=None, state=None,
                      limit: int = 100) -> dict:
        sql = ("SELECT * FROM audit_segment WHERE tenant_id=?")
        params: list = [tenant_id]
        if stream_key:
            sql += " AND stream_key=?"
            params.append(stream_key)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY always_from DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        return {"source": SOURCE, "as_of": _now(), "segments": rows,
                "coverage": self._coverage(tenant_id, stream_key, rows, [])}

    def segment_proof(self, tenant_id: str, segment_id: str) -> dict:
        seg = self.conn.execute(
            "SELECT * FROM audit_segment WHERE tenant_id=? AND segment_id=?",
            (tenant_id, segment_id)).fetchone()
        if seg is None:
            # 跨租户与不存在同一答案（E28 / AEX-3）。
            raise ServiceError(404, "NOT_FOUND", "segment not found")
        previous = self.conn.execute(
            "SELECT segment_id, root_hash, to_seq FROM audit_segment "
            "WHERE tenant_id=? AND stream_key=? AND to_seq < ? "
            "ORDER BY to_seq DESC LIMIT 1",
            (tenant_id, seg["stream_key"], seg["from_seq"])).fetchone()
        return {
            "source": SOURCE, "as_of": _now(),
            "segment_id": seg["segment_id"], "stream_key": seg["stream_key"],
            "from_seq": seg["from_seq"], "to_seq": seg["to_seq"],
            "from_prev_hash": seg["from_prev_hash"],
            "root_hash": seg["root_hash"], "state": seg["state"],
            "record_count": seg["record_count"],
            "anchor_ref": seg["anchor_ref"], "anchored_at": seg["anchored_at"],
            "previous_segment": _row(previous),
            "link_ok": (previous is None
                        or previous["root_hash"] == seg["from_prev_hash"]),
        }

    # ------------------------------------------------------------ 命令：追加
    def record(self, tenant_id: str, command_id: str, intent: dict,
               request_hash: str) -> dict:
        # 幂等占位与结果落库在调用方的 `command()` 里（与 model-routing 同构），
        # 所以这里只管一件事：把这个事务里的记录追加进去。
        validated = self._validate(intent)
        intent_id = intent.get("intent_id") or _new_id("intent")
        with Transaction(self.conn):
            appended = self._append(
                tenant_id, validated, source_event_id=None,
                source_intent_id=intent_id)
        return {"record_id": appended["record_id"],
                "stream_key": appended.get("stream_key"),
                "stream_seq": appended["stream_seq"],
                "record_hash": appended["record_hash"],
                "prev_hash": appended.get("prev_hash"),
                "duplicate": appended["duplicate"],
                "redactions": appended["redactions"]}

    # ------------------------------------------------------------ 事件入口
    def consume_intent(self, envelope: dict, *,
                       producer_id: str | None = None) -> dict:
        """主路径（§2.1）。至少一次投递，按 `(consumer, event_id)` 去重。

        `producer_id` 是"认证的 producer 身份"；调用方拿不到时传 None，
        返回值如实写 `producer_verified=false`（NEW-4），不假装核对过。

        这里**不碰** `idempotency_record`：事件入口的重复由 `(consumer,
        event_id)` 挡住，HTTP 入口的重复由 `(tenant_id, command_id)` 挡住，
        两把尺子各管各的（§10.1）。
        """
        event_id = envelope.get("id")
        tenant_id = envelope.get("tenant_id")
        if not event_id or not tenant_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event envelope needs id and tenant_id")
        source = envelope.get("source")
        verified = False
        if producer_id:
            if producer_id != source:
                raise ServiceError(403, "SOURCE_MISMATCH",
                                   f"producer {producer_id} cannot write as {source}")
            verified = True

        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event data must be the audit intent object")
        intent = dict(data)
        intent["source_service"] = intent.get("source_service") or source
        validated = self._validate(intent)
        intent_id = validated.get("intent_id")

        with Transaction(self.conn):
            if not self.inbox.consume(CONSUMER_INGRESS, event_id):
                # 重复投递：回放第一次的结果，**不**再追加一行，
                # 也不制造一条新的链尾——否则同一个事件会被记成两次。
                row = self.conn.execute(
                    "SELECT record_id, stream_seq, record_hash FROM audit_record"
                    " WHERE tenant_id=? AND (source_event_id=?"
                    " OR source_intent_id=?) LIMIT 1",
                    (tenant_id, event_id, intent_id)).fetchone()
                return {
                    "record_id": row["record_id"] if row else None,
                    "stream_seq": row["stream_seq"] if row else None,
                    "record_hash": row["record_hash"] if row else None,
                    "redactions": [], "duplicate": True,
                    "producer_verified": verified,
                }

            appended = self._append(tenant_id, validated,
                                    source_event_id=event_id,
                                    source_intent_id=intent_id)
        return {**appended, "producer_verified": verified}

    # ------------------------------------------------------------ 查询
    def _coverage(self, tenant_id, stream_key, segments, records) -> dict:
        hot_months = sorted({(r.get("recorded_at") or "")[:7].replace("-", "_")
                             for r in records if r.get("recorded_at")})
        archived = sum(1 for s in segments
                       if s["state"] in ("SEALED", "ARCHIVED", "ANCHORED"))
        proof = sum(1 for s in segments if s["state"] == "DESTROYED")
        gaps = []
        for seg in segments:
            if seg["state"] == "DESTROYED":
                gaps.append({"from_seq": seg["from_seq"],
                             "to_seq": seg["to_seq"], "reason": "DESTROYED"})
            elif seg["state"] == "INTEGRITY_FAILED":
                gaps.append({"from_seq": seg["from_seq"],
                             "to_seq": seg["to_seq"],
                             "reason": "INTEGRITY_FAILED"})
        return {"hot_partitions": [m for m in hot_months if m],
                "archived_segments_queried": archived,
                "proof_only_segments": proof,
                "gaps": gaps}

    def _segments_for(self, tenant_id, stream_key) -> list[dict]:
        sql = "SELECT * FROM audit_segment WHERE tenant_id=?"
        params: list = [tenant_id]
        if stream_key:
            sql += " AND stream_key=?"
            params.append(stream_key)
        sql += " ORDER BY stream_key, from_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _log_access(self, tenant_id, principal, access_kind, *,
                    query_hash=None, export_id=None, result_count=None,
                    coverage=None, purpose=None, trace_id=None):
        """§3.8：读本身是审计事实；**不写回读到的正文**，只写 query_hash。

        递归在这里截断：读 `audit_access_log` 只写结构化日志，不再产生
        access log（本内核不暴露 access log 的查询接口，所以这条更不会被
        触发）。
        """
        self.conn.execute(
            "INSERT INTO audit_access_log (access_id, tenant_id, accessed_at,"
            " principal, access_kind, query_hash, export_id, result_count,"
            " coverage, purpose, trace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("access"), tenant_id, _now(), _jcs(principal), access_kind,
             query_hash, export_id, result_count,
             _jcs(coverage) if coverage is not None else None, purpose,
             trace_id))

    def query_records(self, tenant_id: str, *, principal: dict,
                      stream_key=None, from_seq=None, to_seq=None,
                      action=None, actor=None, occurred_from=None,
                      occurred_to=None, outcome=None, limit: int = 50,
                      cursor=None, purpose=None) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["read"])
        query = {k: v for k, v in {
            "stream_key": stream_key, "from_seq": from_seq, "to_seq": to_seq,
            "action": action, "actor": actor, "occurred_from": occurred_from,
            "occurred_to": occurred_to, "outcome": outcome,
        }.items() if v is not None}
        query_hash = _sha256_hex(_jcs(query))

        sql = "SELECT r.* FROM audit_record r WHERE r.tenant_id=?"
        params: list = [tenant_id]
        if stream_key:
            sql += " AND r.stream_key=?"
            params.append(stream_key)
        if from_seq is not None:
            sql += " AND r.stream_seq>=?"
            params.append(from_seq)
        if to_seq is not None:
            sql += " AND r.stream_seq<=?"
            params.append(to_seq)
        if action:
            sql += " AND r.action=?"
            params.append(action)
        if actor:
            sql += " AND json_extract(r.actor, '$.principal_id')=?"
            params.append(actor)
        if outcome:
            sql += " AND r.outcome=?"
            params.append(outcome)
        since = _parse_ts(occurred_from)
        if since is not None:
            sql += " AND r.occurred_at>=?"
            params.append(_fmt(since))
        until = _parse_ts(occurred_to)
        if until is not None:
            sql += " AND r.occurred_at<=?"
            params.append(_fmt(until))
        if cursor:
            anchor = self.conn.execute(
                "SELECT recorded_at FROM audit_record WHERE tenant_id=? AND"
                " record_id=?", (tenant_id, cursor)).fetchone()
            if anchor is None:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "cursor does not name a visible record")
            # 行值比较：(recorded_at, record_id) < 游标，避免同一毫秒丢记录。
            sql += " AND (r.recorded_at, r.record_id) < (?, ?)"
            params.extend([anchor["recorded_at"], cursor])
        limit = max(1, min(int(limit), 500))
        sql += " ORDER BY r.recorded_at DESC, r.record_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        has_more = len(rows) > limit
        rows = rows[:limit]

        segments = self._segments_for(tenant_id, stream_key)
        coverage = self._coverage(tenant_id, stream_key, segments, rows)
        self._log_access(tenant_id, principal, "QUERY", query_hash=query_hash,
                         result_count=len(rows), coverage=coverage,
                         purpose=purpose)
        return {
            "source": SOURCE,
            "as_of": _now(),
            "coverage": coverage,
            "records": [_public_record(r) for r in rows],
            "next_cursor": rows[-1]["record_id"] if has_more else None,
        }

    def get_record(self, tenant_id: str, record_id: str, *,
                   principal: dict, purpose=None) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["read"])
        row = self.conn.execute(
            "SELECT * FROM audit_record WHERE tenant_id=? AND record_id=?",
            (tenant_id, record_id)).fetchone()
        if row is None:
            # 跨租户一律 404，响应不含该租户的任何标识（E28）。
            raise ServiceError(404, "NOT_FOUND", "record not found")
        record = _public_record(dict(row))
        record["canonical"] = _canonical_of(dict(row))
        record["covered_by_hash"] = list(CANONICAL_KEYS)
        # §3.3 的复算键集逐字不含这四个字段（NEW-5）。落库、可查、可索引，
        # 但它们改动后链仍然"通过"——读的人必须知道这一点。
        record["not_covered_by_hash"] = [
            "on_behalf_of", "source_verb", "primary_target_index",
            "source_intent_id"]
        self._log_access(tenant_id, principal, "QUERY",
                         query_hash=_sha256_hex(_jcs({"record_id": record_id})),
                         result_count=1, purpose=purpose)
        return {"source": SOURCE, "as_of": _now(), "record": record}

    def by_target(self, tenant_id: str, *, service_id: str, kind: str,
                  target_id: str, occurred_from=None, occurred_to=None,
                  principal: dict, limit: int = 50) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["read"])
        sql = ("SELECT r.* FROM audit_record_target t JOIN audit_record r"
               " ON r.tenant_id=t.tenant_id AND r.record_id=t.record_id"
               " WHERE t.tenant_id=? AND t.target_service=? AND t.target_kind=?"
               " AND t.target_id=?")
        params: list = [tenant_id, service_id, kind, target_id]
        since = _parse_ts(occurred_from)
        if since is not None:
            sql += " AND t.recorded_at>=?"
            params.append(_fmt(since))
        until = _parse_ts(occurred_to)
        if until is not None:
            sql += " AND t.recorded_at<=?"
            params.append(_fmt(until))
        sql += " ORDER BY t.recorded_at DESC, r.record_id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        coverage = self._coverage(tenant_id, None, self._segments_for(tenant_id, None), rows)
        self._log_access(tenant_id, principal, "QUERY",
                         query_hash=_sha256_hex(_jcs(
                             {"service_id": service_id, "kind": kind,
                              "id": target_id})),
                         result_count=len(rows), coverage=coverage)
        return {"source": SOURCE, "as_of": _now(), "coverage": coverage,
                "records": [_public_record(r) for r in rows]}

    # ------------------------------------------------------------ 导出
    def create_export(self, tenant_id: str, command_id: str, body: dict,
                      request_hash: str, *, principal: dict) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["export"])
        query = _normalize_export_query(body)

        # NEW-11：没有后台构建器。方案给的 202 + "稍后再查"在这里会永远停在
        # BUILDING，所以同步建完、直接返回 READY——语义不变（不会有人说
        # "已排队"却永远不完成），只是省略了中间态。
        with Transaction(self.conn):
            records = self._export_records(tenant_id, query)
            segments = self._segments_for(tenant_id, query.get("stream_key"))
            coverage = self._coverage(tenant_id, query.get("stream_key"),
                                      segments, records)
            export_id = _new_id("export")
            bundle = _bundle_digest(export_id, query, records, segments,
                                    coverage)
            expires_at = _fmt(_now_dt() + timedelta(days=7))
            self.conn.execute(
                "INSERT INTO audit_export (export_id, tenant_id, query,"
                " query_hash, state, segment_ids, record_count, bundle_ref,"
                " chain_continuous, gaps, requested_by, purpose, created_at,"
                " completed_at, expires_at) "
                "VALUES (?, ?, ?, ?, 'READY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (export_id, tenant_id, _jcs(query),
                 _sha256_hex(_jcs(query)), _jcs(bundle["segment_ids"]),
                 len(records), _jcs(bundle["bundle_ref"]),
                 0 if coverage["gaps"] else 1, _jcs(coverage["gaps"]),
                 _jcs(principal), body.get("purpose"), _now(), _now(),
                 expires_at))
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_EXPORT_READY,
                subject=f"audit-export/{export_id}", tenant_id=tenant_id,
                # NEW-12：无可信 home_cell，就显式写未知，不按自报值填。
                home_cell="cell_unassigned", aggregate_id=export_id,
                sequence=1, correlation_id=command_id,
                causation_id=None,
                data={"export_id": export_id, "state": "READY",
                      "record_count": len(records),
                      "segment_count": len(bundle["segment_ids"]),
                      "chain_continuous": not coverage["gaps"],
                      "bundle_ref": {"service_id": SOURCE, "id": export_id,
                                     "revision": 1,
                                     "content_hash": bundle["sha256"]},
                      "expires_at": expires_at},
                data_ref=f"audit-export/{export_id}/manifest.json"))
            self._log_access(tenant_id, principal, "EXPORT_CREATE",
                             query_hash=_sha256_hex(_jcs(query)),
                             export_id=export_id, result_count=len(records),
                             coverage=coverage, purpose=body.get("purpose"))
            return {"export_id": export_id, "state": "READY",
                    "record_count": len(records),
                    "segment_count": len(bundle["segment_ids"]),
                    "chain_continuous": not coverage["gaps"],
                    "bundle_ref": bundle["bundle_ref"],
                    "expires_at": expires_at}

    def _export_records(self, tenant_id, query) -> list[dict]:
        sql = "SELECT * FROM audit_record WHERE tenant_id=?"
        params: list = [tenant_id]
        if query.get("stream_key"):
            sql += " AND stream_key=?"
            params.append(query["stream_key"])
        if query.get("from_seq") is not None:
            sql += " AND stream_seq>=?"
            params.append(query["from_seq"])
        if query.get("to_seq") is not None:
            sql += " AND stream_seq<=?"
            params.append(query["to_seq"])
        if query.get("action"):
            sql += " AND action=?"
            params.append(query["action"])
        if query.get("outcome"):
            sql += " AND outcome=?"
            params.append(query["outcome"])
        since = _parse_ts(query.get("occurred_from"))
        if since is not None:
            sql += " AND occurred_at>=?"
            params.append(_fmt(since))
        until = _parse_ts(query.get("occurred_to"))
        if until is not None:
            sql += " AND occurred_at<=?"
            params.append(_fmt(until))
        sql += " ORDER BY stream_key, stream_seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_export(self, tenant_id: str, export_id: str, *,
                   principal: dict) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["export"])
        row = self.conn.execute(
            "SELECT * FROM audit_export WHERE tenant_id=? AND export_id=?",
            (tenant_id, export_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "export not found")
        body = dict(row)
        body["segment_ids"] = _loads(body["segment_ids"], [])
        body["gaps"] = _loads(body["gaps"], [])
        body["bundle_ref"] = _loads(body["bundle_ref"], None)
        body["query"] = _loads(body["query"], {})
        self._log_access(tenant_id, principal, "EXPORT_DOWNLOAD",
                         export_id=export_id,
                         query_hash=body["query_hash"])
        return {"source": SOURCE, "as_of": _now(), "export": body}

    def verify_export(self, tenant_id: str, export_id: str, *,
                      principal: dict) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["export"])
        row = self.conn.execute(
            "SELECT * FROM audit_export WHERE tenant_id=? AND export_id=?",
            (tenant_id, export_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "export not found")
        if row["state"] != "READY":
            raise ServiceError(409, "EXPORT_NOT_READY",
                               f"export is {row['state']}", retryable=True)
        query = _loads(row["query"], {})
        records = self._export_records(tenant_id, query)
        ok, results = _verify_chain(records)
        segments = self._segments_for(tenant_id, query.get("stream_key"))
        self._log_access(tenant_id, principal, "VERIFY", export_id=export_id,
                         result_count=len(records))
        return {"source": SOURCE, "as_of": _now(), "export_id": export_id,
                "chain_ok": ok, "segments": results,
                "coverage": self._coverage(tenant_id, query.get("stream_key"),
                                           segments, records)}

    # ------------------------------------------------------------ 保留策略
    def put_retention_policy(self, tenant_id: str, policy: dict, *,
                             principal: dict) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["retention_change"])
        merged = {**DEFAULT_POLICY,
                  **{k: v for k, v in policy.items() if k in DEFAULT_POLICY}}
        # 阶梯必须单调：hot ≤ warm ≤ total。hot/warm 的关系与 total 是否配置无关，
        # 只有第二段比较需要 total 存在（§5.4 的两条 CHECK）。
        if merged["retention_hot_days"] > merged["retention_warm_days"]:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "retention_hot_days must not exceed warm days")
        if (merged["retention_total_days"] is not None
                and merged["retention_warm_days"] > merged["retention_total_days"]):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "retention_warm_days must not exceed total days")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO audit_retention_policy (tenant_id,"
                " retention_hot_days, retention_warm_days,"
                " retention_total_days, worm_required, anchor_required,"
                " strict_audit, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET"
                " retention_hot_days=excluded.retention_hot_days,"
                " retention_warm_days=excluded.retention_warm_days,"
                " retention_total_days=excluded.retention_total_days,"
                " worm_required=excluded.worm_required,"
                " anchor_required=excluded.anchor_required,"
                " strict_audit=excluded.strict_audit,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (tenant_id, merged["retention_hot_days"],
                 merged["retention_warm_days"], merged["retention_total_days"],
                 int(bool(merged["worm_required"])),
                 int(bool(merged["anchor_required"])),
                 int(bool(merged["strict_audit"])), _jcs(principal), _now()))
        return {"tenant_id": tenant_id, "policy": merged,
                "note": "strict_audit is a marker only; it blocks nothing "
                        "(C45-5)"}

    def _policy(self, tenant_id) -> dict:
        row = self.conn.execute(
            "SELECT * FROM audit_retention_policy WHERE tenant_id=?",
            (tenant_id,)).fetchone()
        return {**DEFAULT_POLICY, **_row(row)} if row is not None else dict(DEFAULT_POLICY)

    # ------------------------------------------------------------ 法务扣留
    def create_legal_hold(self, tenant_id: str, body: dict, *,
                          principal: dict) -> dict:
        kind = body.get("scope_kind")
        if kind not in ("tenant", "subject", "time_range"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "scope_kind must be tenant|subject|time_range")
        if not body.get("reason"):
            raise ServiceError(422, "SCHEMA_INVALID", "reason is required")
        scope = body.get("scope") or {}
        if kind == "subject" and not scope.get("stream_key"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "subject hold needs scope.stream_key")
        if kind == "time_range" and not (scope.get("from") and scope.get("to")):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "time_range hold needs scope.from/scope.to")
        hold_id = _new_id("hold")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO legal_hold (hold_id, tenant_id, scope_kind, scope,"
                " reason, case_ref, authorized_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hold_id, tenant_id, kind, _jcs(scope), body["reason"],
                 body.get("case_ref"), _jcs(principal), _now()))
        return {"hold_id": hold_id, "tenant_id": tenant_id,
                "scope_kind": kind, "scope": scope}

    def release_legal_hold(self, tenant_id: str, hold_id: str, *,
                           principal: dict) -> dict:
        row = self.conn.execute(
            "SELECT * FROM legal_hold WHERE tenant_id=? AND hold_id=?",
            (tenant_id, hold_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "legal hold not found")
        if row["released_at"]:
            return {"hold_id": hold_id, "released_at": row["released_at"],
                    "already_released": True}
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE legal_hold SET released_at=?, released_by=? "
                "WHERE tenant_id=? AND hold_id=?",
                (_now(), _jcs(principal), tenant_id, hold_id))
        return {"hold_id": hold_id, "released_at": _now(),
                "already_released": False}

    def _holds_covering(self, tenant_id, stream_key, from_ts, to_ts) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM legal_hold WHERE tenant_id=? AND released_at IS NULL",
            (tenant_id,)).fetchall()
        covering = []
        for row in rows:
            scope = _loads(row["scope"], {})
            if row["scope_kind"] == "tenant":
                covering.append(dict(row))
            elif row["scope_kind"] == "subject":
                if stream_key and scope.get("stream_key") == stream_key:
                    covering.append(dict(row))
            elif row["scope_kind"] == "time_range":
                start = _parse_ts(scope.get("from"))
                end = _parse_ts(scope.get("to"))
                if start and end and from_ts and to_ts:
                    if from_ts <= end and to_ts >= start:
                        covering.append(dict(row))
        return covering

    # ------------------------------------------------------------ 销毁
    def _destruction_preconditions(self, tenant_id, scope) -> tuple[dict, list[dict]]:
        """§3.7 的五条前置条件，逐条返回结果，不合并成一句"不行"。"""
        stream_key = scope.get("stream_key")
        to_seq = scope.get("to_seq")
        sql = "SELECT * FROM audit_segment WHERE tenant_id=?"
        params: list = [tenant_id]
        if stream_key:
            sql += " AND stream_key=?"
            params.append(stream_key)
        if to_seq is not None:
            sql += " AND to_seq<=?"
            params.append(to_seq)
        segments = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        policy = self._policy(tenant_id)

        archive_ok = bool(segments) and all(
            s["state"] in ("ARCHIVED", "ANCHORED", "DESTROYED") for s in segments)
        # 前置条件 2：回读校验。本内核里"回读"就是重算库内这些分段的链。
        verify_ok = True
        for seg in segments:
            records = [dict(r) for r in self.conn.execute(
                "SELECT * FROM audit_record WHERE tenant_id=? AND stream_key=?"
                " AND stream_seq BETWEEN ? AND ? ORDER BY stream_seq",
                (tenant_id, seg["stream_key"], seg["from_seq"],
                 seg["to_seq"])).fetchall()]
            ok, _ = _verify_chain(records)
            if records and not ok:
                verify_ok = False
                break
        from_ts = min((_parse_ts(s["always_from"]) for s in segments
                       if _parse_ts(s["always_from"])), default=None)
        to_ts = max((_parse_ts(s["always_to"]) for s in segments
                     if _parse_ts(s["always_to"])), default=None)
        holds = self._holds_covering(tenant_id, stream_key, from_ts, to_ts)
        hold_ok = not holds

        retention_ok = False
        total_days = policy.get("retention_total_days")
        if total_days is not None and from_ts is not None:
            retention_ok = (_now_dt() >= from_ts + timedelta(days=int(total_days)))

        # 前置条件 4：销毁令已签署。`authorized_by` 非空即视为已签署；
        # 角色匹配属于 S01 判定（NEW-1：没有 audit.destroy 键）。
        approved = bool(scope.get("authorized_by"))

        result = {
            "archive_complete": archive_ok,
            "readback_verified": verify_ok,
            "no_legal_hold": hold_ok,
            "order_signed": approved,
            "retention_expired": retention_ok,
        }
        return result, segments

    def create_destruction_order(self, tenant_id: str, body: dict, *,
                                 principal: dict) -> dict:
        self._require(tenant_id, principal.get("principal_id", ""), "tenant",
                      tenant_id, PERMISSIONS["destroy"])
        scope = body.get("scope") or {}
        if not scope.get("stream_key") and scope.get("to_seq") is None:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "scope needs stream_key and/or to_seq")
        scope = {**scope, "authorized_by": _jcs(principal)}
        preconditions, segments = self._destruction_preconditions(tenant_id, scope)
        holds = self._holds_covering(
            tenant_id, scope.get("stream_key"),
            min((_parse_ts(s["always_from"]) for s in segments
                 if _parse_ts(s["always_from"])), default=None),
            max((_parse_ts(s["always_to"]) for s in segments
                 if _parse_ts(s["always_to"])), default=None))
        blocked = [name for name, ok in preconditions.items() if not ok]
        state = "REQUESTED" if not blocked else "BLOCKED"
        order_id = _new_id("order")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO destruction_order (order_id, tenant_id, scope,"
                " authorized_by, policy_ref, hold_check, preconditions, state,"
                " blocked_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, tenant_id, _jcs(scope), _jcs(principal),
                 _jcs({"policy": self._policy(tenant_id)}),
                 _jcs({"holds": [h["hold_id"] for h in holds],
                       "checked_at": _now()}),
                 _jcs(preconditions), state,
                 blocked[0] if blocked else None, _now()))
        return {"order_id": order_id, "state": state,
                "preconditions": preconditions,
                "blocked_reason": _blocked_reason(blocked[0]) if blocked else None}

    def execute_destruction_order(self, tenant_id: str, order_id: str, *,
                                  principal: dict) -> dict:
        row = self.conn.execute(
            "SELECT * FROM destruction_order WHERE tenant_id=? AND order_id=?",
            (tenant_id, order_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "destruction order not found")
        scope = _loads(row["scope"], {})
        preconditions, segments = self._destruction_preconditions(tenant_id, scope)
        holds = self._holds_covering(
            tenant_id, scope.get("stream_key"),
            min((_parse_ts(s["always_from"]) for s in segments
                 if _parse_ts(s["always_from"])), default=None),
            max((_parse_ts(s["always_to"]) for s in segments
                 if _parse_ts(s["always_to"])), default=None))
        if holds:
            # §4.4：唯一有专属错误码的阻塞分支。
            raise ServiceError(409, "LEGAL_HOLD_ACTIVE",
                               "a legal hold covers the target range",
                               details={"hold_refs": [h["hold_id"] for h in holds]})
        blocked = [name for name, ok in preconditions.items() if not ok]
        if blocked:
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE destruction_order SET state='BLOCKED',"
                    " preconditions=?, blocked_reason=? WHERE order_id=?",
                    (_jcs(preconditions), blocked[0], order_id))
            return {"order_id": order_id, "state": "BLOCKED",
                    "preconditions": preconditions,
                    "blocked_reason": _blocked_reason(blocked[0])}

        with Transaction(self.conn):
            for seg in segments:
                if seg["state"] == "DESTROYED":
                    continue
                self.conn.execute(
                    "UPDATE audit_segment SET state='DESTROYED',"
                    " destruction_order_id=? WHERE segment_id=?",
                    (order_id, seg["segment_id"]))
            self.conn.execute(
                "UPDATE destruction_order SET state='EXECUTED',"
                " preconditions=?, executed_at=?, record_count_destroyed=?"
                " WHERE order_id=?",
                (_jcs(preconditions), _now(),
                 sum(s["record_count"] for s in segments), order_id))
            # §3.7：销毁本身也是审计记录的对象，写进 meta 链。
            # 注意它落在同一事务里，因此与"令已执行"同生共死。
            self._append(
                tenant_id,
                {
                    "schema_version": SCHEMA_VERSION,
                    "occurred_at": _now(),
                    "source_service": SOURCE,
                    "source_verb": "audit.segment.destroy",
                    "action": "audit.segment.destroy",
                    "actor": {"principal_id": principal.get("principal_id"),
                              "principal_type": "service",
                              "delegation_id": None},
                    "targets": [{"service_id": SOURCE, "kind": "audit",
                                 "id": order_id}],
                    "primary_target_index": 0,
                    "policy_ref": {"service_id": SOURCE,
                                   "id": "retention-policy", "revision": 1},
                    "evidence_refs": [],
                    "outcome": "SUCCEEDED",
                    "outcome_detail": {},
                    "details": {"order_id": order_id,
                                "segment_ids": [s["segment_id"]
                                                for s in segments],
                                "record_count": sum(s["record_count"]
                                                    for s in segments)},
                    "correlation_id": order_id,
                    "causation_id": None,
                },
                source_event_id=f"internal:{uuid.uuid4().hex}",
                source_intent_id=None,
                allow_self_record=False)
        # NEW-2：SQLite 里没有 DETACH/DROP。这里只是把分段标成 DESTROYED，
        # 让查询把它当"只有证明、没有正文"处理——字节仍在库里。
        return {"order_id": order_id, "state": "EXECUTED",
                "record_count_destroyed": sum(s["record_count"]
                                              for s in segments),
                "proof_only": True}

    # ------------------------------------------------------------ 保留查询
    def retention_check(self, tenant_id: str, *, service_id: str, kind: str,
                        target_id: str) -> dict:
        """§10.3：S28 的 `REFERENCE_CHECK`。纯查索引，命中即停。

        `proof_only` 的情形返回 `referenced=true`——正文没了但证明还在用这个
        id，S28 必须能区分它和"从没出现过"。
        """
        hit = self.conn.execute(
            "SELECT r.recorded_at, r.stream_key, r.stream_seq"
            " FROM audit_record_target t JOIN audit_record r"
            " ON r.tenant_id=t.tenant_id AND r.record_id=t.record_id"
            " WHERE t.tenant_id=? AND t.target_service=? AND t.target_kind=?"
            " AND t.target_id=? ORDER BY t.recorded_at ASC LIMIT 1",
            (tenant_id, service_id, kind, target_id)).fetchone()
        if hit is None:
            return {"referenced": False, "reason": None, "hold_refs": [],
                    "earliest_releasable_at": None, "checked_at": _now()}
        policy = self._policy(tenant_id)
        first = _parse_ts(hit["recorded_at"])
        holds = self._holds_covering(tenant_id, hit["stream_key"], first, first)
        segments = self.conn.execute(
            "SELECT state FROM audit_segment WHERE tenant_id=? AND stream_key=?"
            " AND from_seq<=? AND to_seq>=?",
            (tenant_id, hit["stream_key"], hit["stream_seq"],
             hit["stream_seq"])).fetchall()
        states = [s["state"] for s in segments]
        if holds:
            reason = "LEGAL_HOLD"
        elif states and all(s == "DESTROYED" for s in states):
            reason = "PROOF_ONLY"
        else:
            reason = "RETENTION_ACTIVE"
        releasable = None
        total_days = policy.get("retention_total_days")
        if reason != "LEGAL_HOLD" and total_days is not None and first is not None:
            releasable = _fmt(first + timedelta(days=int(total_days)))
        return {"referenced": True, "reason": reason,
                "hold_refs": [h["hold_id"] for h in holds],
                "earliest_releasable_at": releasable, "checked_at": _now()}

    # ------------------------------------------------------------ 自检
    def verify_stream(self, tenant_id: str, stream_key: str) -> dict:
        records = [dict(r) for r in self.conn.execute(
            "SELECT * FROM audit_record WHERE tenant_id=? AND stream_key=?"
            " ORDER BY stream_seq", (tenant_id, stream_key)).fetchall()]
        ok, results = _verify_chain(records)
        return {"source": SOURCE, "as_of": _now(), "stream_key": stream_key,
                "chain_ok": ok, "segments": results,
                "records_checked": len(records)}


def _blocked_reason(name: str) -> str:
    return {
        "archive_complete": "ARCHIVE_INCOMPLETE",
        "readback_verified": "READBACK_MISMATCH",
        "no_legal_hold": "LEGAL_HOLD_ACTIVE",
        "order_signed": "ORDER_UNSIGNED",
        "retention_expired": "RETENTION_NOT_EXPIRED",
    }.get(name, name.upper())


def _find_raw_content(value, path: str = "") -> str | None:
    """找出 details 里不该出现的正文键。深度有界：details 本就该是浅的。"""
    if isinstance(value, dict):
        if path.count(".") > 6:
            return None
        for key, item in value.items():
            here = f"{path}.{key}" if path else key
            if key.lower() in RAW_CONTENT_KEYS and item not in (None, "", [], {}):
                return here
            found = _find_raw_content(item, here)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_raw_content(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _canonical_of(row: dict) -> str:
    return _jcs({key: _loads(row[key], row[key])
                 if key in ("actor", "targets", "policy_ref", "evidence_refs",
                            "outcome_detail", "details") else row.get(key)
                 for key in CANONICAL_KEYS})


def _public_record(row: dict) -> dict:
    out = dict(row)
    for field in ("actor", "targets", "policy_ref", "evidence_refs",
                  "outcome_detail", "details", "on_behalf_of"):
        if field in out and isinstance(out[field], str):
            out[field] = _loads(out[field], out[field])
    return out


def _verify_chain(records: list[dict]) -> tuple[bool, list[dict]]:
    """重算每一段的链。返回 (全段均可, 每段结果)。

    这里刻意**不**修任何东西：断链只报告（§3.5「绝不允许在校验失败时重建」）。
    """
    by_stream: dict[str, list[dict]] = {}
    for record in records:
        by_stream.setdefault(record["stream_key"], []).append(record)
    ok_all = True
    results = []
    for stream_key, rows in by_stream.items():
        rows.sort(key=lambda r: r["stream_seq"])
        expected_prev = rows[0]["prev_hash"]
        broken_at = None
        for record in rows:
            recomputed = _sha256_hex(expected_prev + _canonical_of(record))
            if recomputed != record["record_hash"]:
                broken_at = record["stream_seq"]
                break
            expected_prev = record["record_hash"]
        if broken_at is None:
            results.append({"stream_key": stream_key, "status": "CHAIN_OK",
                            "from_seq": rows[0]["stream_seq"],
                            "to_seq": rows[-1]["stream_seq"],
                            "root_hash": rows[-1]["record_hash"]})
        else:
            ok_all = False
            results.append({"stream_key": stream_key,
                            "status": "CHAIN_BROKEN", "broken_at": broken_at})
    return ok_all, results


def _normalize_export_query(body: dict) -> dict:
    allowed = ("stream_key", "from_seq", "to_seq", "action", "actor",
               "outcome", "occurred_from", "occurred_to")
    query = {}
    for key in allowed:
        value = body.get(key)
        if value is None:
            continue
        if key in ("from_seq", "to_seq"):
            if not isinstance(value, int) or value < 1:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"{key} must be a positive integer")
        if key in ("occurred_from", "occurred_to"):
            parsed = _parse_ts(value)
            if parsed is None:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"{key} must be an RFC3339 timestamp")
            value = _fmt(parsed)
        if key in ("stream_key", "action", "actor", "outcome") and not value:
            raise ServiceError(422, "SCHEMA_INVALID", f"{key} must be non-empty")
        query[key] = value
    return query


def _bundle_digest(export_id, query, records, segments, coverage) -> dict:
    """`audit-export-bundle.v1` 的摘要（C45-2：形状由本服务自建）。

    拼接顺序固定：manifest.json → 各 segments/<id>.jsonl（按 segment_id 排序）
    → chain-proof.json。摘要对**这些文件的字节**求 sha256，因此第三方只要按
    同一顺序读回文件就能复核（T45-11）。
    """
    segment_ids = sorted({s["segment_id"] for s in segments
                          if any(r["stream_key"] == s["stream_key"]
                                 and s["from_seq"] <= r["stream_seq"] <= s["to_seq"]
                                 for r in records)})
    manifest = {
        "bundle_id": export_id,
        "query": query,
        "record_count": len(records),
        "segment_ids": segment_ids,
        "chain_continuous": not coverage["gaps"],
        "gaps": coverage["gaps"],
        "segments": [{"segment_id": s["segment_id"],
                      "from_seq": s["from_seq"], "to_seq": s["to_seq"],
                      "root_hash": s["root_hash"]}
                     for s in segments if s["segment_id"] in segment_ids],
    }
    proof = {"segments": manifest["segments"],
             "chain_continuous": manifest["chain_continuous"]}
    chunks = [b"manifest.json\n" + _jcs(manifest).encode("utf-8") + b"\n"]
    for seg_id in segment_ids:
        seg = next(s for s in segments if s["segment_id"] == seg_id)
        lines = [_jcs(_public_record(r)) for r in records
                 if r["stream_key"] == seg["stream_key"]
                 and seg["from_seq"] <= r["stream_seq"] <= seg["to_seq"]]
        body = "\n".join(lines).encode("utf-8")
        chunks.append(f"segments/{seg_id}.jsonl\n".encode("utf-8")
                      + body + (b"\n" if lines else b""))
    chunks.append(b"chain-proof.json\n" + _jcs(proof).encode("utf-8") + b"\n")
    blob = b"".join(chunks)
    return {
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "segment_ids": segment_ids,
        "bundle_ref": {"storage_ref": f"bundle://{export_id}",
                       "sha256": hashlib.sha256(blob).hexdigest(),
                       "size_bytes": len(blob)},
    }
