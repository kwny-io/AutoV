"""S15 内核。方案：`plans/S15-voice-assets.md`。

一句话边界：**S15 是"这个音色是什么、在哪个供应商用什么 ID、能不能被调用
（授权）"的唯一权威。** 它不判定情绪、不判定预算、不替代 S02 做整体策略、
**绝不自动换相似声线**（§0.2、反模式 #1）。

三条结构性约束，代码里的每一处都是它们的落点：

1. **授权状态是独立单行、强一致、不可缓存的。** §1.2 解释了为什么不能把它
   放进 `voice_revision`：引用旧 revision 的调用方会永远读到 `granted`。
   所以 `check_eligibility` 直接读 `voice_authorization` 单行（§4.2 第 1 条），
   本次实现里**没有任何缓存层**。
2. **到期在读时判定，不依赖定时任务。** §4.2 第 6 条：状态字段说 `granted`
   但 `expires_at` 已过，仍必须拒。`_effective_state()` 是这条的唯一实现点；
   它同时驱动 `renew` 的合法性判断（列里的 `granted` + 已过期 = 可以续约）。
3. **拒绝是 200 + `eligible=false`，不是 4xx。** §5.4 末段与反模式 #11：
   调用方需要完整状态来决策（记录、重试、切换预案），"不可用"不是请求错误。
   `check_eligibility` 里一条 `raise ServiceError` 都不会为"不可用"而发。

幂等由 HTTP 层的 `idempotency_sdk` 负责（键 `tenant_id + command_id` +
JCS hash，同键异 body 409）；本内核只写业务事实。`check_eligibility` 是
只读语义的 POST，**不走**幂等记录（§6.1 末条），也不调 S01——它在每次
dispatch 前的关键路径上（§7），把鉴权放进来会把最热的接口变成最慢的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from event_sdk import Outbox, new_event

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA

# 登记表里**唯一**与音色相关的键（S15-8）：`contracts/identity/permission-registry
# .v2.json` 共 40 个键，只有 `asset.voice.authorize`（risk=high）落在音色域，
# 且 `plans/S01-tenant-access.md:46` 明确把它指派给 S11–S15。
#
# 所以本服务**用它一个键**守全部写命令：授权的撤销/暂停/恢复/续约是它的字面
# 含义；注册音色、追加 revision、登记映射与词典也走它——因为注册表里没有第二个
# 可选，自己发明 `voice.asset.write` 只会让每条写命令在生产里 503，把一个
# 待裁决的粒度问题变成一个不可用的服务（这是 S13 的做法，不是这里的）。
#
# 这个"一个键两种含义"是缺口，不是已完成：见 CONTRACT_GAPS S15-8。变的是
# **粒度**，不是"谁都能写"——任何写命令仍然要求一条 high-risk 授权。
AUTHORIZATION_PERMISSION = "asset.voice.authorize"
CONTENT_PERMISSION = "asset.voice.authorize"


SOURCE = "S15"
SCHEMA_VERSION = "2.0"
EVENT_AUTHORIZATION_CHANGED = "asset.voice.authorization.changed"
TOPIC_VOICE = "drama.voice-assets.v2"

VOICE_KINDS = ("platform_preset", "cloned", "licensed_actor", "synthetic",
               "narrator")
# §2.1 / §2.3 第 3 条：这两类必须带期限。CHECK 是跨表的，SQLite 表达不了，
# 所以在应用层断言（方案明写"应用层断言"）。
EXPIRY_REQUIRED_KINDS = ("cloned", "licensed_actor")
# 只有这两个状态可以转出（§3.1）。`revoked` 不在此列，触发器兜底。
TERMINAL_STATES = ("revoked",)

# S15-3：authorized_uses 的 `prefix:value` 维度到 requested_use 字段的映射。
USE_DIMENSIONS = {
    "series": "series_id",
    "modality": "modality",
    "provider": "provider_id",
    "channel": "channel_id",
    "model_snapshot": "model_snapshot",
    "region": "region",
}
# §4.2 的请求体把 modality 写成必填（`modality:'speech'`）。没有它就无从判断
# "这次调用属于哪类用途"，宁可拒绝也不默认放行。
REQUIRED_USE_DIMENSION = "modality"

# S15-2：S28 的 source_kind 里哪一种是"隔离导入器"。`upload_session` 是唯一
# 走隔离导入路径的取值（`artifact-ingest` 的 DDL 注释）；其余三种要么来自
# 供应商 URL、要么是内部对象、要么是派生件，都不是"用户上传后先隔离检查"。
ISOLATED_SOURCE_KINDS = ("upload_session",)

# VAC-2：全仓词表只有 RIGHTS_WITHDRAWN 一个取值。其余由本服务定义并登记在
# 这里，等 owner 定稿；**不写进契约的 enum**（写了就是替 owner 做决定）。
REASON_CODES = (
    "INITIAL_GRANT",
    "RIGHTS_WITHDRAWN",
    "RIGHTS_EXPIRED",
    "SUPPLIER_UNAVAILABLE",
    "REVIEW_HOLD",
    "REVIEW_CLEARED",
    "RENEWED",
    "MANUAL_REVOCATION",
)

# §5.4 的码表 + S15-5 补的码。集中在这里，测试按这张表逐条覆盖。
ERROR_CODES = {
    400: ("MALFORMED_REQUEST",),
    403: ("TENANT_SCOPE_DENIED", "TENANT_MISMATCH"),
    404: ("NOT_FOUND", "CANONICAL_NOT_SET", "AUTHORIZATION_MISSING"),
    409: ("VERSION_CONFLICT", "ALREADY_REVOKED", "MAPPING_EXISTS",
          "DUPLICATE_VOICE_CONTENT", "CANDIDATE_NOT_PENDING",
          "STATE_TRANSITION_INVALID", "NAME_CONFLICT", "REGRESSION_FAILED"),
    422: ("SAMPLE_NOT_SCANNED", "AUTHORIZATION_EXPIRY_REQUIRED",
          "USE_SCOPE_REQUIRED", "REVOCATION_EVIDENCE_REQUIRED",
          "REVOCATION_TERMINAL", "LEXICON_ENTRY_INVALID", "ARTIFACT_NOT_FOUND",
          "ARTIFACT_HASH_MISMATCH", "VOICE_TOMBSTONED",
          "MAPPING_REGION_AMBIGUOUS"),
    429: ("WRITE_BACKPRESSURE",),
    503: ("DEPENDENCY_UNAVAILABLE",),
}

# check-eligibility 的 deny_reason 全集（§4.2 + S15-5 补的两个）。
# `renew` / `resume` 的"不动 expires_at"哨兵：None 会被写成 NULL，而这两个
# 转移必须保留原到期时间（续约显式给值，恢复不该顺手抹掉期限）。
_KEEP = object()

DENY_REASONS = (
    "AUTHORIZATION_REVOKED", "AUTHORIZATION_EXPIRED",
    "AUTHORIZATION_SUSPENDED", "AUTHORIZATION_MISSING",
    "USE_NOT_AUTHORIZED", "MAPPING_UNAVAILABLE", "REGRESSION_FAILED",
    "STATE_SEQ_STALE", "VOICE_TOMBSTONED", "MAPPING_REGION_AMBIGUOUS",
)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _fmt(_now_dt())


def _fmt(moment: datetime) -> str:
    """定宽 UTC，毫秒精度带 `Z`。定宽是为了让字符串比较 == 时间比较。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_time(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            ).astimezone(timezone.utc)


def _norm_time(value, code: str = "MALFORMED_REQUEST") -> str:
    """把外部时间统一成定宽形态。解析不了就 400，不猜、不落一个字符串比较
    会在毫秒位上错序的裸值。"""
    try:
        return _fmt(_parse_time(value))
    except (TypeError, ValueError) as exc:
        raise ServiceError(400, code, f"invalid timestamp: {value!r}") from exc


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _is_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


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


# ------------------------------------------------------------------ 纯函数

def revision_content_hash(content: dict) -> str:
    """revision 的内容指纹（F15 / §2.2 的 `vr_hash_uniq`）。

    只哈希内容，不含 `revision` / `accepted_at` / `accepted_by`——那些每次
    接受都不同，含进去会让"同内容重复接受"永远检测不到。

    **不含 `voice_id`**：§2.2 的唯一索引是 `(tenant_id, content_hash)`，按字面
    实现就是租户级——同租户下两个音色内容相同也会被挡。这与 F15 的措辞
    （"同内容重复接受 **revision**"读起来像是单条音色内）有张力，但索引是
    字面写死的 DDL，这里不自行收窄作用域。登记为 S15-10，等 owner 裁决。
    """
    projection = {
        "language_profile": content.get("language_profile"),
        "emotion_profile": content.get("emotion_profile"),
        "rate_profile": content.get("rate_profile"),
        "voice_sample_refs": content.get("voice_sample_refs"),
        "clone_artifact_ref": content.get("clone_artifact_ref"),
        "rights_ref": content.get("rights_ref"),
        "source_kind": content.get("source_kind"),
        "quality_gate_ref": content.get("quality_gate_ref"),
        "payload": content.get("payload"),
    }
    return _sha256_hex(_jcs(projection))


def effective_state(state: str, expires_at: str | None, now: str) -> str:
    """§4.2 第 6 条：到期在**读时**判定。

    定时任务没跑、状态字段还是 `granted`，也必须按 `expired` 处理。这是本
    服务"实时检查"的严格要求，也是反模式 #9 的反面。
    """
    if state == "granted" and expires_at and expires_at < now:
        return "expired"
    return state


def use_is_authorized(requested: dict, authorized_uses: list[str]) -> bool:
    """`requested_use` 是否落在 `authorized_uses` 内（S15-3）。

    规则（fail closed）：
    - `modality` 恒需命中；`authorized_uses` 里没有 modality 条目 = 用途无法
      解释 = 拒绝。**空列表在上游就被 USE_SCOPE_REQUIRED 拦掉了**，这里的
      空列表只可能来自历史数据，同样拒绝。
    - 其余前缀只在该前缀出现在 `authorized_uses` 里时约束：请求必须给出该
      维度且命中其中一条。缺维度算不命中（不 fail-open，§4.2 第 5 条的
      "常被漏掉"正是这种默认放行）。
    - 不认识的前缀不约束（不能凭一个词表外的前缀就拒，也不能拿它放行）。
    """
    if not isinstance(authorized_uses, list) or not authorized_uses:
        return False
    groups: dict[str, set[str]] = {}
    for item in authorized_uses:
        if not isinstance(item, str):
            return False
        prefix, sep, value = item.partition(":")
        if not sep:
            return False
        groups.setdefault(prefix, set()).add(value)

    modality = requested.get(REQUIRED_USE_DIMENSION)
    if not modality or modality not in groups.get(REQUIRED_USE_DIMENSION, set()):
        return False
    for prefix, values in groups.items():
        if prefix == REQUIRED_USE_DIMENSION:
            continue
        key = USE_DIMENSIONS.get(prefix)
        if key is None:
            continue
        if requested.get(key) not in values:
            return False
    return True


# ------------------------------------------------------------------ 服务

class VoiceAssetService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_scanner,
                 authorizer):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        # 声纹样本的存在性/扫描状态（S28）与写权限（S01）。两者都是闭包，
        # 不在本服务 import 里出现（AGENTS.md：不互相 import 业务 ORM）。
        self.artifact_scanner = artifact_scanner
        self.authorizer = authorizer

    # ------------------------------------------------------------ 内部助手

    def _authorize(self, tenant_id: str, *, principal: str | None,
                   permission: str, scope_kind: str, scope_id: str) -> None:
        """写命令前问 S01。S15 不自己发明放行规则（S15-8）。

        `principal` 缺失时用 `service:S15`：本服务的调用方是其他服务，它们
        在平台签名上下文里已经过认证；这里问的是"这个主体能不能改这个音色"。
        """
        result = self.authorizer(
            tenant_id, principal or "service:S15", scope_kind, scope_id,
            permission)
        if not result.get("allowed"):
            raise ServiceError(
                403, "TENANT_SCOPE_DENIED",
                f"principal is not allowed to {permission} on {scope_id}",
                details={"permission": permission})

    def _voice_row(self, tenant_id: str, voice_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM voice WHERE tenant_id = ? AND voice_id = ?",
            (tenant_id, voice_id)).fetchone()
        # E28 / F16：跨租户的 voice_id 在这里就是"不存在"——不返回 403，
        # 403 会泄漏"这个 id 在别的租户存在"。
        if row is None:
            raise ServiceError(404, "NOT_FOUND", f"voice {voice_id} not found")
        return row

    def _auth_row(self, tenant_id: str, voice_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM voice_authorization WHERE tenant_id = ?"
            " AND voice_id = ?", (tenant_id, voice_id)).fetchone()
        if row is None:
            # "没有授权记录" ≠ "没被撤销所以可以"。静默不是授权。
            raise ServiceError(
                404, "AUTHORIZATION_MISSING",
                f"voice {voice_id} has no authorization record; absence of a "
                "revocation is not consent")
        return row

    def _revision_row(self, tenant_id: str, voice_id: str,
                      revision: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM voice_revision WHERE tenant_id = ? AND voice_id = ?"
            " AND revision = ?", (tenant_id, voice_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"voice {voice_id} revision {revision} not found")
        return row

    # ------------------------------------------------------------ 事件

    def _emit_auth_changed(self, *, tenant_id: str, voice_id: str,
                           from_state: str | None, to_state: str, seq: int,
                           reason_code: str, evidence_ref: dict | None,
                           effective_at: str | None,
                           authorized_uses_before: list,
                           expires_at: str | None,
                           command_id: str, home_cell: str) -> None:
        """`asset.voice.authorization.changed`。**只此一个契约事件**（§5.3）。

        `aggregate_sequence = state_seq`（VAC-1 取 §5.3 的定义）。
        payload 是 `additionalProperties:false` 的 10 个字段，多一个都会让
        下游的校验失败——`actor`、`reason_text`、`scope_notes` 因此只在库里。
        """
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_AUTHORIZATION_CHANGED,
            subject=f"voice/{voice_id}",
            tenant_id=tenant_id,
            home_cell=home_cell or "unknown",
            aggregate_id=voice_id, sequence=seq,
            correlation_id=command_id, causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "voice_id": voice_id,
                "from_state": from_state,
                "to_state": to_state,
                "state_seq": seq,
                "reason_code": reason_code,
                "effective_at": effective_at,
                "evidence_ref": evidence_ref,
                "authorized_uses_before": authorized_uses_before,
                "expires_at": expires_at,
            },
            data_ref=None,
            schema_version=SCHEMA_VERSION))

    def _append_authorization_event(self, *, tenant_id, voice_id, seq,
                                    from_state, to_state, reason_code,
                                    reason_text, evidence_ref, effective_at,
                                    authorized_uses_before,
                                    expires_at_before, actor) -> None:
        self.conn.execute(
            "INSERT INTO authorization_event (tenant_id, voice_id, seq,"
            " from_state, to_state, reason_code, reason_text, evidence_ref,"
            " effective_at, authorized_uses_before, expires_at_before, actor,"
            " occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, voice_id, seq, from_state, to_state, reason_code,
             reason_text, None if evidence_ref is None else _dumps(evidence_ref),
             effective_at, _dumps(authorized_uses_before), expires_at_before,
             _dumps(actor), _now()))

    @staticmethod
    def _event_evidence(evidence: dict | None) -> dict | None:
        """VAC-6：契约的 `evidence_ref` 是 3 键，`rights_ref` 是 4 键。

        只在事件里做这个投影，库里保留原样——`rights_hash` 不能丢，它是
        "撤销依据没被换过"的唯一证据。
        """
        if not isinstance(evidence, dict):
            return None
        ref_id = evidence.get("id") or evidence.get("rights_id")
        if not ref_id:
            return None
        return {
            "service_id": evidence.get("service_id") or "S02",
            "id": ref_id,
            "revision": evidence.get("revision")
                        or evidence.get("rights_revision") or 1,
        }

    # ------------------------------------------------------------ 注册（§4.1）

    def register_voice(self, tenant_id: str, command_id: str, body: dict,
                       *, principal: str | None = None) -> dict:
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="tenant", scope_id=tenant_id)

        series_scope = body.get("series_scope")
        display_name = body.get("display_name")
        voice_kind = body.get("voice_kind")
        project_id = body.get("project_id")       # S15-1：§4.1 漏了这个字段
        rights_ref = body.get("rights_ref")
        authorization = body.get("authorization")
        if not isinstance(series_scope, str) or not series_scope:
            raise ServiceError(400, "MALFORMED_REQUEST", "series_scope is required")
        if not isinstance(display_name, str) or not display_name:
            raise ServiceError(400, "MALFORMED_REQUEST", "display_name is required")
        if voice_kind not in VOICE_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"voice_kind must be one of {list(VOICE_KINDS)}")
        if not isinstance(project_id, str) or not project_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "project_id is required")
        if not isinstance(rights_ref, dict) or not rights_ref:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "rights_ref is required for every voice")
        if not isinstance(authorization, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "authorization is required")
        authorized_uses = authorization.get("authorized_uses")
        if not isinstance(authorized_uses, list) or not authorized_uses:
            # F10：空表示"没有明确用途"，无法审计。
            raise ServiceError(
                422, "USE_SCOPE_REQUIRED",
                "authorization.authorized_uses must be a non-empty list; an "
                "empty list means no declared use and cannot be audited")
        expires_raw = authorization.get("expires_at")
        expires_at = None if expires_raw in (None, "") else _norm_time(expires_raw)
        if voice_kind in EXPIRY_REQUIRED_KINDS and not expires_at:
            # F9：cloned / licensed_actor 必须有期限。
            raise ServiceError(
                422, "AUTHORIZATION_EXPIRY_REQUIRED",
                f"voice_kind {voice_kind!r} requires authorization.expires_at")
        for use in authorized_uses:
            if not isinstance(use, str) or ":" not in use:
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "authorized_uses entries must be 'dimension:value' strings")

        samples = body.get("voice_sample_refs") or []
        if not isinstance(samples, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "voice_sample_refs must be a list")
        self._verify_samples(tenant_id, samples)

        payload = body.get("payload") or {}
        content = {
            "language_profile": body.get("language_profile") or {},
            "emotion_profile": body.get("emotion_profile") or {},
            "rate_profile": body.get("rate_profile") or {},
            "voice_sample_refs": samples,
            "clone_artifact_ref": body.get("clone_artifact_ref"),
            "rights_ref": rights_ref,
            "source_kind": ("cloned" if voice_kind in EXPIRY_REQUIRED_KINDS
                            and voice_kind == "cloned" else "preset_registered"),
            "quality_gate_ref": body.get("quality_gate_ref") or {},
            "payload": payload,
        }
        if not content["language_profile"] or not content["rate_profile"]:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "language_profile and rate_profile are required")
        digest = revision_content_hash(content)
        now = _now()
        voice_id = _new_id("voice")
        created_by = body.get("created_by") or {"principal_id": principal or
                                                "service:S15"}
        actor = {"principal_id": principal or "service:S15",
                 "kind": "service" if not principal else "principal"}

        with Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO voice (tenant_id, voice_id, project_id,"
                    " series_scope, display_name, voice_kind, character_ref,"
                    " status, canonical_revision, current_revision, created_by,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, voice_id, project_id, series_scope, display_name,
                     voice_kind, None if body.get("character_ref") is None
                     else _dumps(body["character_ref"]),
                     "active", 1, 1, _dumps(created_by), now))
            except sqlite3.IntegrityError as exc:
                if "voice.series_scope" in str(exc) or "UNIQUE" in str(exc):
                    raise ServiceError(
                        409, "NAME_CONFLICT",
                        f"a voice named {display_name!r} already exists in "
                        f"series_scope {series_scope!r}") from exc
                raise
            self._insert_revision(tenant_id, voice_id, 1, digest, content,
                                  parent_revision=None, accepted_by=created_by)
            self.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, voice_id, revision,"
                " decided_at, decided_by, decision_kind, reason_text,"
                " prior_canonical, superseded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, voice_id, 1, now, _dumps(created_by),
                 "initial_import", "initial registration", None, None))
            self.conn.execute(
                "INSERT INTO voice_authorization (tenant_id, voice_id, state,"
                " rights_ref, authorized_uses, scope_notes, granted_at,"
                " expires_at, state_changed_at, state_seq, changed_by,"
                " reason_code, reason_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, voice_id, "granted", _dumps(rights_ref),
                 _dumps(authorized_uses), authorization.get("scope_notes"),
                 now, expires_at, now, 1, _dumps(actor), "INITIAL_GRANT",
                 None))
            self._append_authorization_event(
                tenant_id=tenant_id, voice_id=voice_id, seq=1,
                from_state=None, to_state="granted",
                reason_code="INITIAL_GRANT", reason_text=None,
                evidence_ref=None, effective_at=None,
                authorized_uses_before=[], expires_at_before=None, actor=actor)
            self._emit_auth_changed(
                tenant_id=tenant_id, voice_id=voice_id, from_state=None,
                to_state="granted", seq=1, reason_code="INITIAL_GRANT",
                evidence_ref=None, effective_at=None,
                authorized_uses_before=[], expires_at=expires_at,
                command_id=command_id,
                home_cell=str(body.get("home_cell") or "unknown"))
        return self.get_voice(tenant_id, voice_id)

    def _verify_samples(self, tenant_id: str, samples: list) -> None:
        """§4.1 事务外第 1 步 / E29。

        三种结论必须分开：`True` 存在且过检、`False` 不存在或没过检、
        `None` 查不出来。**"查不出来"不能当成"没问题"**（S15-2）——声纹样本
        是生物特征，未过检的样本拿去克隆是事故。
        """
        for ref in samples:
            if not isinstance(ref, dict) or not (
                    ref.get("id") or ref.get("artifact_id")):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "voice_sample_refs entries need an id")
            scan = self.artifact_scanner(ref, tenant_id)
            # `None` 与非 dict 都是"问不到"：S28 的读接口不发布 `probe_ok` /
            # `source_kind`（S15-2）。**不把问不到当成没问题。**
            if not isinstance(scan, dict):
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    "S28 did not answer the sample scan; refusing to register "
                    "a voice on an unverified voiceprint sample",
                    retryable=True)
            if not scan.get("found", False):
                raise ServiceError(422, "ARTIFACT_NOT_FOUND",
                                   f"sample artifact {ref.get('id')} not found")
            declared = ref.get("content_hash")
            actual = scan.get("content_hash")
            if declared and actual and declared != actual:
                raise ServiceError(
                    422, "ARTIFACT_HASH_MISMATCH",
                    f"sample {ref.get('id')} hash differs from S28's")
            if scan.get("probe_ok") is not True:
                raise ServiceError(
                    422, "SAMPLE_NOT_SCANNED",
                    f"sample {ref.get('id')} has not passed malicious-media "
                    "inspection")
            if scan.get("source_kind") not in ISOLATED_SOURCE_KINDS:
                raise ServiceError(
                    422, "SAMPLE_NOT_SCANNED",
                    f"sample {ref.get('id')} did not come through the isolated "
                    f"importer (source_kind={scan.get('source_kind')!r})")

    def _insert_revision(self, tenant_id, voice_id, revision, digest, content,
                         *, parent_revision, accepted_by) -> None:
        try:
            self.conn.execute(
                "INSERT INTO voice_revision (tenant_id, voice_id, revision,"
                " content_hash, payload, language_profile, emotion_profile,"
                " rate_profile, voice_sample_refs, clone_artifact_ref,"
                " rights_ref, source_kind, parent_revision, quality_gate_ref,"
                " accepted_at, accepted_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, voice_id, revision, digest,
                 _dumps(content["payload"]),
                 _dumps(content["language_profile"]),
                 _dumps(content["emotion_profile"]),
                 _dumps(content["rate_profile"]),
                 _dumps(content["voice_sample_refs"]),
                 None if content["clone_artifact_ref"] is None
                 else _dumps(content["clone_artifact_ref"]),
                 _dumps(content["rights_ref"]), content["source_kind"],
                 parent_revision, _dumps(content["quality_gate_ref"]),
                 _now(), _dumps(accepted_by)))
        except sqlite3.IntegrityError as exc:
            # F15：同租户内同内容只接受一次。
            raise ServiceError(
                409, "DUPLICATE_VOICE_CONTENT",
                "this voice content has already been accepted "
                "(content_hash collision)") from exc

    # ------------------------------------------------- 资格检查（§4.2，核心）

    def check_eligibility(self, tenant_id: str, voice_id: str,
                          body: dict) -> dict:
        """调用前资格判定。**强一致、不缓存、拒绝也返回 200 语义。**

        判定顺序（顺序本身是语义）：
        1. 音色存在（跨租户 404）；
        2. 授权行存在（没有记录 ≠ 可以）；
        3. 状态：revoked > expired > suspended（安全优先，比 `if_state_seq`
           更必须被看到）；
        4. `if_state_seq` 乐观检查；
        5. 用途成员；
        6. 供应商映射（授权 + 可用映射才算完整可用，§4.2 第 4 条）。

        任何一步拒绝都继续带出 `state_seq` / `authorization_state` /
        `expires_at`——调用方要能区分"撤销了"和"这个用途没授权"。
        """
        now = _now()
        voice = self._voice_row(tenant_id, voice_id)

        try:
            auth = self._auth_row(tenant_id, voice_id)
        except ServiceError as exc:
            # 没有授权行：不是 404 请求错误，而是"不可用"。§5.4 末段。
            if exc.code != "AUTHORIZATION_MISSING":
                raise
            return self._eligibility_response(
                voice_id=voice_id, state=None, seq=0, expires_at=None,
                mapping=None, deny_reason="AUTHORIZATION_MISSING",
                detail="no authorization record exists for this voice",
                now=now)

        revision_no = body.get("voice_revision")
        if not isinstance(revision_no, int) or isinstance(revision_no, bool):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "voice_revision must be an integer")
        # C15-2：没有 revision accepted 事件，所以资格接口顺带确认 revision
        # 真实存在，订阅方至少有一个显式查询点。
        self._revision_row(tenant_id, voice_id, revision_no)

        requested = body.get("requested_use")
        if not isinstance(requested, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "requested_use must be an object")
        if not requested.get(REQUIRED_USE_DIMENSION):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "requested_use.modality is required")

        uses = _loads(auth["authorized_uses"], [])
        state = effective_state(auth["state"], auth["expires_at"], now)
        seq = int(auth["state_seq"])

        if voice["status"] == "tombstoned":
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason="VOICE_TOMBSTONED",
                detail="voice is tombstoned", now=now)
        if state == "revoked":
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason="AUTHORIZATION_REVOKED",
                detail="authorization was withdrawn; revoked is terminal",
                now=now)
        if state == "expired":
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason="AUTHORIZATION_EXPIRED",
                detail="authorization expired at " + str(auth["expires_at"]),
                now=now)
        if state == "suspended":
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason="AUTHORIZATION_SUSPENDED",
                detail="authorization is suspended pending resume", now=now)

        if_state_seq = body.get("if_state_seq")
        if if_state_seq is not None:
            if not isinstance(if_state_seq, int) or isinstance(if_state_seq, bool):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "if_state_seq must be an integer")
            if if_state_seq != seq:
                # F7 / E30-C9：调用方手里的判定已经陈旧，必须重新评估。
                return self._eligibility_response(
                    voice_id=voice_id, state=state, seq=seq,
                    expires_at=auth["expires_at"], mapping=None,
                    deny_reason="STATE_SEQ_STALE",
                    detail=f"if_state_seq={if_state_seq} but current is {seq}",
                    now=now)

        if not use_is_authorized(requested, uses):
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason="USE_NOT_AUTHORIZED",
                detail="requested_use is not covered by authorized_uses",
                now=now)

        mapping, deny, detail = self._select_mapping(
            tenant_id, voice_id, revision_no, requested)
        if mapping is None:
            return self._eligibility_response(
                voice_id=voice_id, state=state, seq=seq,
                expires_at=auth["expires_at"], mapping=None,
                deny_reason=deny, detail=detail, now=now)
        return self._eligibility_response(
            voice_id=voice_id, state=state, seq=seq,
            expires_at=auth["expires_at"], mapping=mapping, deny_reason=None,
            detail=None, now=now)

    @staticmethod
    def _eligibility_response(*, voice_id, state, seq, expires_at, mapping,
                              deny_reason, detail, now) -> dict:
        response = {
            "voice_id": voice_id,
            "eligible": deny_reason is None,
            "authorization_state": state,
            "state_seq": seq,
            "expires_at": expires_at,
            "provider_mapping": mapping,
            "deny_reason": deny_reason,
            "detail": detail,
            "checked_at": now,
        }
        if deny_reason is None:
            response.pop("detail")
        return response

    def _select_mapping(self, tenant_id, voice_id, revision_no, requested):
        """返回 `(mapping|None, deny_reason|None, detail|None)`。

        F5（无映射）与 F6（回归未通过）是两个不同的拒绝：前者是"没登记过"，
        后者是"登记了但不可信"。同一条映射同时可能遇到供应商下架（
        `mapping_status='unavailable'`）与授权撤销——后者已在上面判过，这里
        只判技术可用性。
        """
        sql = ("SELECT * FROM provider_voice_mapping WHERE tenant_id = ?"
               " AND voice_id = ? AND voice_revision = ?")
        params: list = [tenant_id, voice_id, revision_no]
        for field, key in (("provider_id", "provider_id"),
                           ("channel_id", "channel_id"),
                           ("model_snapshot", "model_snapshot"),
                           ("region", "region")):
            value = requested.get(key)
            if value:
                sql += f" AND {field} = ?"
                params.append(value)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            return None, "MAPPING_UNAVAILABLE", (
                "no provider mapping for the requested provider/snapshot/region")

        usable = []
        for row in rows:
            regression = _loads(row["regression"], {})
            if row["mapping_status"] == "active" and regression.get("passed"):
                usable.append((row, regression))

        if not usable:
            # F6 与 F5 必须能分辨：§4.5 第 2 条把 `passed=false` 的映射存成
            # `deprecated`，但它的拒绝理由仍是"回归没过"，不是"没登记过"。
            # 若任一匹配行的回归失败，优先报 REGRESSION_FAILED——这是调用方
            # 需要去重跑回归的那一类，`MAPPING_UNAVAILABLE` 会把它误导成
            # "换个供应商就行"。
            if any(not _loads(r["regression"], {}).get("passed") for r in rows):
                return None, "REGRESSION_FAILED", (
                    "the mapping exists but its regression has not passed")
            return None, "MAPPING_UNAVAILABLE", (
                "every mapping for this provider/snapshot is deprecated or "
                "unavailable")

        # E31-F2：数据驻留必须显式。请求没给 region 而可用映射横跨多个区时，
        # 选哪个都是替调用方决定驻留地——拒绝，让它带 region 再来。
        if not requested.get("region"):
            regions = {row["region"] for row, _ in usable}
            if len(regions) > 1:
                return None, "MAPPING_REGION_AMBIGUOUS", (
                    "several regions have usable mappings; specify "
                    "requested_use.region")
        row, regression = sorted(
            usable, key=lambda item: (item[0]["region"], item[0]["provider_id"],
                                      item[0]["channel_id"]))[0]
        return {
            "provider_id": row["provider_id"],
            "channel_id": row["channel_id"],
            "model_snapshot": row["model_snapshot"],
            "region": row["region"],
            "provider_voice_id": row["provider_voice_id"],
            "regression_passed": bool(regression.get("passed")),
            "mapping_status": row["mapping_status"],
        }, None, None

    # ------------------------------------------------------- 授权状态变更

    def revoke(self, tenant_id: str, voice_id: str, command_id: str,
               body: dict, *, principal: str | None = None) -> dict:
        """§4.3：只改状态 + 发事件。不取消 S26 的任务、不删音频、不动 revision。"""
        evidence = body.get("evidence_ref")
        if not isinstance(evidence, dict) or not (
                evidence.get("id") or evidence.get("rights_id")):
            # F12：撤销依据必须可追溯（E39-F2）。
            raise ServiceError(
                422, "REVOCATION_EVIDENCE_REQUIRED",
                "evidence_ref with an id is required to revoke")
        reason_code = self._require_reason(body)
        effective_at = (None if body.get("effective_at") in (None, "")
                        else _norm_time(body["effective_at"]))
        self._authorize(tenant_id, principal=principal,
                        permission=AUTHORIZATION_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        return self._transition(
            tenant_id, voice_id, command_id, body,
            to_state="revoked", allowed_from=("granted", "suspended", "expired"),
            reason_code=reason_code,
            reason_text=body.get("reason_text"),
            evidence=evidence, effective_at=effective_at,
            principal=principal)

    def suspend(self, tenant_id: str, voice_id: str, command_id: str,
                body: dict, *, principal: str | None = None) -> dict:
        reason_code = self._require_reason(body)
        self._authorize(tenant_id, principal=principal,
                        permission=AUTHORIZATION_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        return self._transition(
            tenant_id, voice_id, command_id, body,
            to_state="suspended", allowed_from=("granted",),
            reason_code=reason_code, reason_text=body.get("reason_text"),
            evidence=None, effective_at=None, principal=principal)

    def resume(self, tenant_id: str, voice_id: str, command_id: str,
               body: dict, *, principal: str | None = None) -> dict:
        reason_code = self._require_reason(body)
        self._authorize(tenant_id, principal=principal,
                        permission=AUTHORIZATION_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        return self._transition(
            tenant_id, voice_id, command_id, body,
            to_state="granted", allowed_from=("suspended",),
            reason_code=reason_code, reason_text=body.get("reason_text"),
            evidence=None, effective_at=None, principal=principal)

    def renew(self, tenant_id: str, voice_id: str, command_id: str,
              body: dict, *, principal: str | None = None) -> dict:
        """`expired → granted`。`state_seq` 继续递增，不重置（§8 通用表）。

        到期是**读时**判定的，所以列里可能还是 `granted` 而 `expires_at` 已过；
        那种情况下 renewal 必须可用，否则调用方得等定时任务跑完才能续约。
        """
        expires_raw = body.get("expires_at")
        if expires_raw in (None, ""):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expires_at is required to renew")
        expires_at = _norm_time(expires_raw)
        reason_code = body.get("reason_code") or "RENEWED"
        self._authorize(tenant_id, principal=principal,
                        permission=AUTHORIZATION_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        if expires_at < _now():
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expires_at must be in the future")
        return self._transition(
            tenant_id, voice_id, command_id, body,
            to_state="granted",
            allowed_from=("expired",),
            reason_code=reason_code, reason_text=body.get("reason_text"),
            evidence=None, effective_at=None, expires_at=expires_at,
            principal=principal)

    @staticmethod
    def _require_reason(body: dict) -> str:
        reason_code = body.get("reason_code")
        if not isinstance(reason_code, str) or not reason_code:
            # §3.1 的转移表写"需 reason_code"。没有它，审计只剩时间戳。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "reason_code is required for this transition")
        return reason_code

    def _transition(self, tenant_id, voice_id, command_id, body, *,
                    to_state, allowed_from, reason_code, reason_text,
                    evidence, effective_at, principal,
                    expires_at=_KEEP) -> dict:
        """所有状态转移共用一条写路径。

        `expires_at` 默认 `_KEEP`：**只有续约会改到期时间**。暂停/恢复/撤销
        都不该顺手把期限抹成 NULL——那会让"撤销时本来就已到期"这个事实
        从审计里消失，也会让 resume 之后的音色变成"永久授权"。
        """
        row = self._voice_row(tenant_id, voice_id)
        if row["status"] == "tombstoned":
            raise ServiceError(422, "VOICE_TOMBSTONED",
                               "a tombstoned voice cannot change authorization")
        auth = self._auth_row(tenant_id, voice_id)
        now = _now()
        current = effective_state(auth["state"], auth["expires_at"], now)
        if auth["state"] == "revoked":
            # F13：`revoked` 是终态。重复撤销给 409（幂等意义上的"已达成"），
            # 任何其他转移给 422（语义上不允许）。DB 触发器是最后一道防线，
            # 这两个分支才是给调用方的可行动信息。
            if to_state == "revoked":
                raise ServiceError(409, "ALREADY_REVOKED", "already revoked")
            raise ServiceError(
                422, "REVOCATION_TERMINAL",
                "revoked is terminal; create a new voice_id to re-authorize")
        if current not in allowed_from:
            raise ServiceError(
                409, "STATE_TRANSITION_INVALID",
                f"cannot move from {current!r} to {to_state!r}",
                details={"from_state": current, "to_state": to_state})

        seq = int(auth["state_seq"]) + 1
        uses_before = _loads(auth["authorized_uses"], [])
        expires_value = auth["expires_at"] if expires_at is _KEEP else expires_at
        actor = {"principal_id": principal or "service:S15",
                 "kind": "principal" if principal else "service"}

        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE voice_authorization SET state = ?, state_seq = ?,"
                " state_changed_at = ?, changed_by = ?, reason_code = ?,"
                " reason_text = ?, expires_at = ?,"
                " granted_at = CASE WHEN ? = 'granted' THEN COALESCE(granted_at, ?)"
                " ELSE granted_at END"
                " WHERE tenant_id = ? AND voice_id = ?",
                (to_state, seq, now, _dumps(actor), reason_code, reason_text,
                 expires_value, to_state, now, tenant_id, voice_id))
            self._append_authorization_event(
                tenant_id=tenant_id, voice_id=voice_id, seq=seq,
                from_state=auth["state"], to_state=to_state,
                reason_code=reason_code, reason_text=reason_text,
                evidence_ref=evidence, effective_at=effective_at,
                authorized_uses_before=uses_before,
                expires_at_before=auth["expires_at"], actor=actor)
            self._emit_auth_changed(
                tenant_id=tenant_id, voice_id=voice_id,
                from_state=auth["state"], to_state=to_state, seq=seq,
                reason_code=reason_code,
                evidence_ref=self._event_evidence(evidence),
                effective_at=effective_at,
                authorized_uses_before=uses_before, expires_at=expires_value,
                command_id=command_id,
                home_cell=str(body.get("home_cell") or "unknown"))
        return self.get_authorization(tenant_id, voice_id)

    # ------------------------------------------------------------- 映射（§4.5）

    def add_provider_mapping(self, tenant_id: str, voice_id: str,
                             command_id: str, body: dict, *,
                             principal: str | None = None) -> dict:
        self._voice_row(tenant_id, voice_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        revision_no = body.get("voice_revision")
        if not isinstance(revision_no, int) or isinstance(revision_no, bool):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "voice_revision must be an integer")
        self._revision_row(tenant_id, voice_id, revision_no)
        provider_id = body.get("provider_id")
        channel_id = body.get("channel_id")
        snapshot = body.get("model_snapshot")
        region = body.get("region")
        provider_voice_id = body.get("provider_voice_id")
        for name, value in (("provider_id", provider_id),
                            ("channel_id", channel_id),
                            ("model_snapshot", snapshot),
                            ("region", region),
                            ("provider_voice_id", provider_voice_id)):
            if not isinstance(value, str) or not value:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{name} is required")
        regression = body.get("regression")
        if not isinstance(regression, dict) or "passed" not in regression:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "regression.passed is required")
        # §4.5 校验 2：只有 passed=true 才能建 active 映射；否则存成
        # deprecated（可留档、不可路由）。
        status = "active" if regression.get("passed") else "deprecated"
        with Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO provider_voice_mapping (tenant_id, voice_id,"
                    " voice_revision, provider_id, channel_id, model_snapshot,"
                    " region, provider_voice_id, mapping_status, regression,"
                    " added_at, unavailable_reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, voice_id, revision_no, provider_id, channel_id,
                     snapshot, region, provider_voice_id, status,
                     _dumps(regression), _now(), None))
            except sqlite3.IntegrityError as exc:
                # F14：不允许改 provider_voice_id —— 换了就是新行，同 PK 即冲突。
                raise ServiceError(
                    409, "MAPPING_EXISTS",
                    "a mapping already exists for this provider/channel/"
                    "snapshot/region; mappings are append-only") from exc
        return self._mapping_dict(tenant_id, voice_id, revision_no, provider_id,
                                  channel_id, snapshot, region)

    def _mapping_key(self, tenant_id, voice_id, token: str) -> tuple:
        parts = token.split("~")
        if len(parts) != 5:
            raise ServiceError(404, "NOT_FOUND", "mapping not found")
        revision_no, provider_id, channel_id, snapshot, region = parts
        try:
            revision_no = int(revision_no)
        except ValueError as exc:
            raise ServiceError(404, "NOT_FOUND", "mapping not found") from exc
        row = self.conn.execute(
            "SELECT * FROM provider_voice_mapping WHERE tenant_id = ?"
            " AND voice_id = ? AND voice_revision = ? AND provider_id = ?"
            " AND channel_id = ? AND model_snapshot = ? AND region = ?",
            (tenant_id, voice_id, revision_no, provider_id, channel_id,
             snapshot, region)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "mapping not found")
        return (revision_no, provider_id, channel_id, snapshot, region), row

    def change_mapping_status(self, tenant_id: str, voice_id: str,
                              command_id: str, token: str, action: str,
                              body: dict, *,
                              principal: str | None = None) -> dict:
        self._voice_row(tenant_id, voice_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        key, row = self._mapping_key(tenant_id, voice_id, token)
        current = row["mapping_status"]
        if action == "deprecate":
            if current != "active":
                raise ServiceError(409, "STATE_TRANSITION_INVALID",
                                   f"cannot deprecate a {current} mapping")
            target, reason = "deprecated", row["unavailable_reason"]
        elif action == "unavailable":
            if current == "unavailable":
                raise ServiceError(409, "STATE_TRANSITION_INVALID",
                                   "mapping is already unavailable")
            target = "unavailable"
            reason = body.get("reason") or "provider unavailable"
        elif action == "restore":
            if current == "active":
                raise ServiceError(409, "STATE_TRANSITION_INVALID",
                                   "mapping is already active")
            # §3.2 的 restore 回到 active，但回归不通过的映射不得被路由。
            regression = _loads(row["regression"], {})
            if not regression.get("passed"):
                raise ServiceError(
                    409, "REGRESSION_FAILED",
                    "cannot restore a mapping whose regression has not passed")
            target, reason = "active", None
        else:  # pragma: no cover - 路由层保证穷举
            raise ServiceError(404, "NOT_FOUND", "unknown mapping action")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE provider_voice_mapping SET mapping_status = ?,"
                " unavailable_reason = ? WHERE tenant_id = ? AND voice_id = ?"
                " AND voice_revision = ? AND provider_id = ? AND channel_id = ?"
                " AND model_snapshot = ? AND region = ?",
                (target, reason, tenant_id, voice_id, *key))
        return self._mapping_dict(tenant_id, voice_id, *key)

    def _mapping_dict(self, tenant_id, voice_id, revision_no, provider_id,
                      channel_id, snapshot, region) -> dict:
        row = self.conn.execute(
            "SELECT * FROM provider_voice_mapping WHERE tenant_id = ?"
            " AND voice_id = ? AND voice_revision = ? AND provider_id = ?"
            " AND channel_id = ? AND model_snapshot = ? AND region = ?",
            (tenant_id, voice_id, revision_no, provider_id, channel_id,
             snapshot, region)).fetchone()
        if row is None:  # pragma: no cover - 调用方刚写过
            raise ServiceError(404, "NOT_FOUND", "mapping not found")
        regression = _loads(row["regression"], {})
        return {
            "voice_id": row["voice_id"],
            "voice_revision": row["voice_revision"],
            "provider_id": row["provider_id"],
            "channel_id": row["channel_id"],
            "model_snapshot": row["model_snapshot"],
            "region": row["region"],
            "provider_voice_id": row["provider_voice_id"],
            "mapping_status": row["mapping_status"],
            "regression": regression,
            "added_at": row["added_at"],
            "unavailable_reason": row["unavailable_reason"],
            "mapping_key": "~".join((str(row["voice_revision"]),
                                     row["provider_id"], row["channel_id"],
                                     row["model_snapshot"], row["region"])),
        }

    # --------------------------------------------------- 内容 revision / 候选

    def add_revision(self, tenant_id: str, voice_id: str, command_id: str,
                     body: dict, *, principal: str | None = None) -> dict:
        """`service.yaml` 原定的命令（C15-1 / C15-2 的登记点）。"""
        row = self._voice_row(tenant_id, voice_id)
        if row["status"] == "tombstoned":
            raise ServiceError(422, "VOICE_TOMBSTONED",
                               "a tombstoned voice cannot accept revisions")
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        content = self._content_from(tenant_id, body,
                                     voice_kind=row["voice_kind"])
        digest = revision_content_hash(content)
        revision_no = int(row["current_revision"]) + 1
        accepted_by = body.get("accepted_by") or {
            "principal_id": principal or "service:S15"}
        with Transaction(self.conn):
            self._insert_revision(tenant_id, voice_id, revision_no, digest,
                                  content, parent_revision=int(
                                      row["current_revision"]),
                                  accepted_by=accepted_by)
            self.conn.execute(
                "UPDATE voice SET current_revision = ?"
                " WHERE tenant_id = ? AND voice_id = ?",
                (revision_no, tenant_id, voice_id))
        # C15-2：没有 revision accepted 事件。订阅方只能显式查询。
        return self.get_revision(tenant_id, voice_id, revision_no)

    def _content_from(self, tenant_id: str, body: dict, *,
                      voice_kind: str) -> dict:
        language = body.get("language_profile")
        rate = body.get("rate_profile")
        rights_ref = body.get("rights_ref")
        if not isinstance(language, dict) or not language:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "language_profile is required")
        if not isinstance(rate, dict) or not rate:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "rate_profile is required")
        if not isinstance(rights_ref, dict) or not rights_ref:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "rights_ref is required")
        samples = body.get("voice_sample_refs") or []
        if not isinstance(samples, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "voice_sample_refs must be a list")
        # 注册时验过不代表这条 revision 也该验：每条新 revision 都可能带新
        # 样本，声纹样本必须每次过 S28（E29）。
        self._verify_samples(tenant_id, samples)
        return {
            "language_profile": language,
            "emotion_profile": body.get("emotion_profile") or {},
            "rate_profile": rate,
            "voice_sample_refs": samples,
            "clone_artifact_ref": body.get("clone_artifact_ref"),
            "rights_ref": rights_ref,
            "source_kind": body.get("source_kind") or (
                "cloned" if voice_kind == "cloned" else "preset_registered"),
            "quality_gate_ref": body.get("quality_gate_ref") or {},
            "payload": body.get("payload") or {},
        }

    def add_candidate(self, tenant_id: str, voice_id: str, command_id: str,
                      body: dict, *, principal: str | None = None) -> dict:
        row = self._voice_row(tenant_id, voice_id)
        if row["status"] == "tombstoned":
            raise ServiceError(422, "VOICE_TOMBSTONED",
                               "a tombstoned voice cannot take candidates")
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        content = self._content_from(tenant_id, body,
                                     voice_kind=row["voice_kind"])
        digest = revision_content_hash(content)
        candidate_id = _new_id("candidate")
        now = _now()
        created_by = body.get("created_by") or {
            "principal_id": principal or "service:S15"}
        voice_check = self._voice_check(tenant_id, voice_id, row)
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO voice_candidate (tenant_id, candidate_id, voice_id,"
                " content_hash, payload, voice_check, state, created_by,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, candidate_id, voice_id, digest, _dumps(content),
                 _dumps(voice_check), "PENDING", _dumps(created_by), now,
                 _fmt(_now_dt() + timedelta(days=30))))
        return self.get_candidate(tenant_id, voice_id, candidate_id)

    def _voice_check(self, tenant_id: str, voice_id: str, voice: sqlite3.Row) -> dict:
        """§2.7 的 `voice_check`。**只存结论**，不替调用方判断能不能用。"""
        now = _now()
        auth = self.conn.execute(
            "SELECT * FROM voice_authorization WHERE tenant_id = ?"
            " AND voice_id = ?", (tenant_id, voice_id)).fetchone()
        authorization_present = (
            auth is not None
            and effective_state(auth["state"], auth["expires_at"], now)
            in ("granted", "suspended"))
        rows = self.conn.execute(
            "SELECT * FROM provider_voice_mapping WHERE tenant_id = ?"
            " AND voice_id = ? AND mapping_status = 'active'",
            (tenant_id, voice_id)).fetchall()
        passed = [r for r in rows if _loads(r["regression"], {}).get("passed")]
        return {
            "authorization_present": bool(authorization_present),
            "regression_pass": bool(rows) and len(passed) == len(rows),
            "clone_duration_ok": (
                voice["voice_kind"] not in EXPIRY_REQUIRED_KINDS
                or bool(auth is not None and auth["expires_at"])),
            "provider_coverage": len({r["provider_id"] for r in passed}),
        }

    def get_candidate(self, tenant_id: str, voice_id: str,
                      candidate_id: str) -> dict:
        self._voice_row(tenant_id, voice_id)
        row = self.conn.execute(
            "SELECT * FROM voice_candidate WHERE tenant_id = ?"
            " AND candidate_id = ?", (tenant_id, candidate_id)).fetchone()
        if row is None or row["voice_id"] != voice_id:
            raise ServiceError(404, "NOT_FOUND",
                               f"candidate {candidate_id} not found")
        return {
            "candidate_id": row["candidate_id"],
            "voice_id": row["voice_id"],
            "content_hash": row["content_hash"],
            "state": row["state"],
            "voice_check": _loads(row["voice_check"], {}),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def accept_candidate(self, tenant_id: str, voice_id: str, candidate_id: str,
                         command_id: str, body: dict, *,
                         principal: str | None = None) -> dict:
        row = self._voice_row(tenant_id, voice_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        candidate = self.conn.execute(
            "SELECT * FROM voice_candidate WHERE tenant_id = ?"
            " AND candidate_id = ?", (tenant_id, candidate_id)).fetchone()
        if candidate is None or candidate["voice_id"] != voice_id:
            raise ServiceError(404, "NOT_FOUND",
                               f"candidate {candidate_id} not found")
        if candidate["state"] != "PENDING":
            raise ServiceError(
                409, "CANDIDATE_NOT_PENDING",
                f"candidate is {candidate['state']}, only PENDING can be accepted")
        content = _loads(candidate["payload"], {})
        accepted_by = body.get("accepted_by") or {
            "principal_id": principal or "service:S15"}
        revision_no = int(row["current_revision"]) + 1
        with Transaction(self.conn):
            self._insert_revision(tenant_id, voice_id, revision_no,
                                  candidate["content_hash"], content,
                                  parent_revision=int(row["current_revision"]),
                                  accepted_by=accepted_by)
            self.conn.execute(
                "UPDATE voice_candidate SET state = 'ACCEPTED'"
                " WHERE tenant_id = ? AND candidate_id = ?",
                (tenant_id, candidate_id))
            self.conn.execute(
                "UPDATE voice SET current_revision = ?"
                " WHERE tenant_id = ? AND voice_id = ?",
                (revision_no, tenant_id, voice_id))
        return {"candidate_id": candidate_id, "state": "ACCEPTED",
                "voice_id": voice_id,
                "revision": self.get_revision(tenant_id, voice_id, revision_no)}

    def reject_candidate(self, tenant_id: str, voice_id: str, candidate_id: str,
                         command_id: str, body: dict, *,
                         principal: str | None = None) -> dict:
        self._voice_row(tenant_id, voice_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        candidate = self.conn.execute(
            "SELECT * FROM voice_candidate WHERE tenant_id = ?"
            " AND candidate_id = ?", (tenant_id, candidate_id)).fetchone()
        if candidate is None or candidate["voice_id"] != voice_id:
            raise ServiceError(404, "NOT_FOUND",
                               f"candidate {candidate_id} not found")
        if candidate["state"] != "PENDING":
            raise ServiceError(
                409, "CANDIDATE_NOT_PENDING",
                f"candidate is {candidate['state']}, only PENDING can be rejected")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE voice_candidate SET state = 'REJECTED'"
                " WHERE tenant_id = ? AND candidate_id = ?",
                (tenant_id, candidate_id))
        return {"candidate_id": candidate_id, "state": "REJECTED",
                "voice_id": voice_id}

    def set_canonical(self, tenant_id: str, voice_id: str, command_id: str,
                      body: dict, *, principal: str | None = None) -> dict:
        row = self._voice_row(tenant_id, voice_id)
        if row["status"] == "tombstoned":
            raise ServiceError(422, "VOICE_TOMBSTONED",
                               "a tombstoned voice cannot take a new canonical")
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        revision_no = body.get("revision")
        if not isinstance(revision_no, int) or isinstance(revision_no, bool):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "revision must be an integer")
        self._revision_row(tenant_id, voice_id, revision_no)
        decision_kind = body.get("decision_kind") or "human_approved"
        if decision_kind not in ("human_approved", "policy_auto",
                                 "initial_import"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "invalid decision_kind")
        auth = self._auth_row(tenant_id, voice_id)
        # §3.4：撤销过的音色不应有新内容基准。历史 revision 仍可读。
        if auth["state"] == "revoked":
            raise ServiceError(
                409, "STATE_TRANSITION_INVALID",
                "a revoked voice must not receive a new canonical revision")
        decided_by = body.get("decided_by") or {
            "principal_id": principal or "service:S15"}
        now = _now()
        with Transaction(self.conn):
            prior = self.conn.execute(
                "SELECT revision FROM canonical_pointer WHERE tenant_id = ?"
                " AND voice_id = ? AND superseded_at IS NULL",
                (tenant_id, voice_id)).fetchone()
            self.conn.execute(
                "UPDATE canonical_pointer SET superseded_at = ?"
                " WHERE tenant_id = ? AND voice_id = ? AND superseded_at IS NULL",
                (now, tenant_id, voice_id))
            self.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, voice_id, revision,"
                " decided_at, decided_by, decision_kind, reason_text,"
                " prior_canonical, superseded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, voice_id, revision_no, now, _dumps(decided_by),
                 decision_kind, body.get("reason_text"),
                 None if prior is None else prior["revision"], None))
            self.conn.execute(
                "UPDATE voice SET canonical_revision = ?"
                " WHERE tenant_id = ? AND voice_id = ?",
                (revision_no, tenant_id, voice_id))
        return self.get_canonical(tenant_id, voice_id)

    # ------------------------------------------------------------- 词典（§4.6）

    def create_lexicon(self, tenant_id: str, command_id: str, body: dict, *,
                       principal: str | None = None) -> dict:
        scope_kind = body.get("scope_kind")
        scope_id = body.get("scope_id")
        if scope_kind not in ("project", "series", "voice"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "scope_kind must be project|series|voice")
        if not isinstance(scope_id, str) or not scope_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "scope_id is required")
        if scope_kind == "voice":
            self._voice_row(tenant_id, scope_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind=scope_kind, scope_id=scope_id)
        entries = self._validate_entries(body.get("entries"))
        lexicon_id = body.get("lexicon_id") or _new_id("lexicon")
        digest = _sha256_hex(_jcs(entries))
        created_by = body.get("created_by") or {
            "principal_id": principal or "service:S15"}
        with Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO lexicon (tenant_id, lexicon_id, scope_kind,"
                    " scope_id, language, head_revision) VALUES (?,?,?,?,?,?)",
                    (tenant_id, lexicon_id, scope_kind, scope_id,
                     body.get("language") or "zh-CN", 1))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(409, "NAME_CONFLICT",
                                   "lexicon_id already exists") from exc
            try:
                self.conn.execute(
                    "INSERT INTO lexicon_revision (tenant_id, lexicon_id,"
                    " revision, content_hash, entries, entry_count, created_by,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, lexicon_id, 1, digest, _dumps(entries),
                     len(entries), _dumps(created_by), _now()))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(
                    409, "DUPLICATE_LEXICON_CONTENT",
                    "this lexicon content already exists in the tenant") from exc
        return self.get_lexicon_revision(tenant_id, lexicon_id, 1)

    @staticmethod
    def _validate_entries(entries) -> list:
        """F18。条目格式：`{term, readings:[{pinyin|ipa, context?}], ...}`。"""
        if not isinstance(entries, list) or not entries:
            raise ServiceError(422, "LEXICON_ENTRY_INVALID",
                               "entries must be a non-empty list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ServiceError(422, "LEXICON_ENTRY_INVALID",
                                   "each entry must be an object")
            term = entry.get("term")
            if not isinstance(term, str) or not term:
                raise ServiceError(422, "LEXICON_ENTRY_INVALID",
                                   "entry.term must be a non-empty string")
            readings = entry.get("readings")
            if not isinstance(readings, list) or not readings:
                raise ServiceError(
                    422, "LEXICON_ENTRY_INVALID",
                    f"entry {term!r} needs a non-empty readings list")
            for reading in readings:
                if not isinstance(reading, dict):
                    raise ServiceError(422, "LEXICON_ENTRY_INVALID",
                                       "each reading must be an object")
                if not any(isinstance(reading.get(key), str) and reading.get(key)
                           for key in ("pinyin", "ipa")):
                    raise ServiceError(
                        422, "LEXICON_ENTRY_INVALID",
                        f"entry {term!r} has a reading without pinyin/ipa")
        return entries

    def add_lexicon_revision(self, tenant_id: str, lexicon_id: str,
                             command_id: str, body: dict, *,
                             principal: str | None = None) -> dict:
        row = self._lexicon_row(tenant_id, lexicon_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind=row["scope_kind"], scope_id=row["scope_id"])
        entries = self._validate_entries(body.get("entries"))
        digest = _sha256_hex(_jcs(entries))
        revision_no = int(row["head_revision"]) + 1
        created_by = body.get("created_by") or {
            "principal_id": principal or "service:S15"}
        with Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO lexicon_revision (tenant_id, lexicon_id,"
                    " revision, content_hash, entries, entry_count, created_by,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, lexicon_id, revision_no, digest,
                     _dumps(entries), len(entries), _dumps(created_by), _now()))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(
                    409, "DUPLICATE_LEXICON_CONTENT",
                    "this lexicon content already exists in the tenant") from exc
            self.conn.execute(
                "UPDATE lexicon SET head_revision = ?"
                " WHERE tenant_id = ? AND lexicon_id = ?",
                (revision_no, tenant_id, lexicon_id))
        return self.get_lexicon_revision(tenant_id, lexicon_id, revision_no)

    def _lexicon_row(self, tenant_id: str, lexicon_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM lexicon WHERE tenant_id = ? AND lexicon_id = ?",
            (tenant_id, lexicon_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", f"lexicon {lexicon_id} not found")
        return row

    def bind_lexicon(self, tenant_id: str, voice_id: str, command_id: str,
                     body: dict, *, principal: str | None = None) -> dict:
        self._voice_row(tenant_id, voice_id)
        self._authorize(tenant_id, principal=principal,
                        permission=CONTENT_PERMISSION,
                        scope_kind="voice", scope_id=voice_id)
        lexicon_id = body.get("lexicon_id")
        if not isinstance(lexicon_id, str) or not lexicon_id:
            raise ServiceError(400, "MALFORMED_REQUEST", "lexicon_id is required")
        self._lexicon_row(tenant_id, lexicon_id)
        revision_no = body.get("lexicon_revision")
        if not isinstance(revision_no, int) or isinstance(revision_no, bool):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "lexicon_revision must be an integer")
        self._lexicon_revision_row(tenant_id, lexicon_id, revision_no)
        is_default = 1 if body.get("is_default") else 0
        with Transaction(self.conn):
            if is_default:
                # §2.6：一个音色最多一个默认词典（部分唯一索引兜底）。
                self.conn.execute(
                    "UPDATE voice_lexicon_binding SET is_default = 0"
                    " WHERE tenant_id = ? AND voice_id = ?"
                    " AND lexicon_id <> ?",
                    (tenant_id, voice_id, lexicon_id))
            self.conn.execute(
                "INSERT INTO voice_lexicon_binding (tenant_id, voice_id,"
                " lexicon_id, lexicon_revision, is_default) VALUES (?,?,?,?,?)"
                " ON CONFLICT (tenant_id, voice_id, lexicon_id) DO UPDATE SET"
                " lexicon_revision = excluded.lexicon_revision,"
                " is_default = excluded.is_default",
                (tenant_id, voice_id, lexicon_id, revision_no, is_default))
        return self.get_bindings(tenant_id, voice_id)

    def _lexicon_revision_row(self, tenant_id, lexicon_id,
                              revision_no) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM lexicon_revision WHERE tenant_id = ?"
            " AND lexicon_id = ? AND revision = ?",
            (tenant_id, lexicon_id, revision_no)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"lexicon {lexicon_id} revision {revision_no}"
                               " not found")
        return row

    # ------------------------------------------------------------- 查询（§5.2）

    def get_voice(self, tenant_id: str, voice_id: str) -> dict:
        row = self._voice_row(tenant_id, voice_id)
        auth = self.conn.execute(
            "SELECT * FROM voice_authorization WHERE tenant_id = ?"
            " AND voice_id = ?", (tenant_id, voice_id)).fetchone()
        now = _now()
        authorization = None
        if auth is not None:
            authorization = {
                "state": auth["state"],
                "effective_state": effective_state(auth["state"],
                                                   auth["expires_at"], now),
                "state_seq": auth["state_seq"],
                "authorized_uses": _loads(auth["authorized_uses"], []),
                "scope_notes": auth["scope_notes"],
                "expires_at": auth["expires_at"],
                "state_changed_at": auth["state_changed_at"],
                "reason_code": auth["reason_code"],
            }
        return {
            "voice_id": row["voice_id"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "series_scope": row["series_scope"],
            "display_name": row["display_name"],
            "voice_kind": row["voice_kind"],
            "character_ref": _loads(row["character_ref"], None),
            "status": row["status"],
            "current_revision": row["current_revision"],
            "canonical_revision": row["canonical_revision"],
            "created_at": row["created_at"],
            "authorization": authorization,
            "checked_at": now,
        }

    def get_revision(self, tenant_id: str, voice_id: str,
                     revision_no: int) -> dict:
        row = self._revision_row(tenant_id, voice_id, revision_no)
        return {
            "voice_id": row["voice_id"],
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            "language_profile": _loads(row["language_profile"], {}),
            "emotion_profile": _loads(row["emotion_profile"], {}),
            "rate_profile": _loads(row["rate_profile"], {}),
            "voice_sample_refs": _loads(row["voice_sample_refs"], []),
            "clone_artifact_ref": _loads(row["clone_artifact_ref"], None),
            "rights_ref": _loads(row["rights_ref"], {}),
            "source_kind": row["source_kind"],
            "parent_revision": row["parent_revision"],
            "quality_gate_ref": _loads(row["quality_gate_ref"], {}),
            "payload": _loads(row["payload"], {}),
            "accepted_at": row["accepted_at"],
        }

    def get_bindings(self, tenant_id: str, voice_id: str) -> dict:
        """`service.yaml` 原定的查询：音色与角色/项目/词典的绑定。"""
        row = self._voice_row(tenant_id, voice_id)
        bindings = self.conn.execute(
            "SELECT * FROM voice_lexicon_binding WHERE tenant_id = ?"
            " AND voice_id = ? ORDER BY is_default DESC, lexicon_id",
            (tenant_id, voice_id)).fetchall()
        return {
            "voice_id": voice_id,
            "project_id": row["project_id"],
            "series_scope": row["series_scope"],
            "character_ref": _loads(row["character_ref"], None),
            "current_revision": row["current_revision"],
            "canonical_revision": row["canonical_revision"],
            "lexicon_bindings": [
                {"lexicon_id": b["lexicon_id"],
                 "lexicon_revision": b["lexicon_revision"],
                 "is_default": bool(b["is_default"])}
                for b in bindings],
        }

    def get_authorization(self, tenant_id: str, voice_id: str) -> dict:
        auth = self._auth_row(tenant_id, voice_id)
        now = _now()
        history = self.conn.execute(
            "SELECT * FROM authorization_event WHERE tenant_id = ?"
            " AND voice_id = ? ORDER BY seq",
            (tenant_id, voice_id)).fetchall()
        return {
            "voice_id": voice_id,
            "state": auth["state"],
            "effective_state": effective_state(auth["state"],
                                               auth["expires_at"], now),
            "state_seq": auth["state_seq"],
            "authorized_uses": _loads(auth["authorized_uses"], []),
            "scope_notes": auth["scope_notes"],
            "rights_ref": _loads(auth["rights_ref"], {}),
            "granted_at": auth["granted_at"],
            "expires_at": auth["expires_at"],
            "state_changed_at": auth["state_changed_at"],
            "reason_code": auth["reason_code"],
            "reason_text": auth["reason_text"],
            "history": [
                {"seq": h["seq"], "from_state": h["from_state"],
                 "to_state": h["to_state"], "reason_code": h["reason_code"],
                 "reason_text": h["reason_text"],
                 "evidence_ref": _loads(h["evidence_ref"], None),
                 "effective_at": h["effective_at"],
                 "authorized_uses_before": _loads(
                     h["authorized_uses_before"], []),
                 "expires_at_before": h["expires_at_before"],
                 "actor": _loads(h["actor"], {}),
                 "occurred_at": h["occurred_at"]}
                for h in history],
        }

    def list_mappings(self, tenant_id: str, voice_id: str, *,
                      provider_id: str | None = None,
                      model_snapshot: str | None = None) -> dict:
        self._voice_row(tenant_id, voice_id)
        sql = ("SELECT * FROM provider_voice_mapping WHERE tenant_id = ?"
               " AND voice_id = ?")
        params: list = [tenant_id, voice_id]
        if provider_id:
            sql += " AND provider_id = ?"
            params.append(provider_id)
        if model_snapshot:
            sql += " AND model_snapshot = ?"
            params.append(model_snapshot)
        sql += " ORDER BY voice_revision, provider_id, channel_id, model_snapshot"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return {"voice_id": voice_id, "items": [
            self._mapping_dict(tenant_id, voice_id, r["voice_revision"],
                               r["provider_id"], r["channel_id"],
                               r["model_snapshot"], r["region"])
            for r in rows]}

    def get_canonical(self, tenant_id: str, voice_id: str) -> dict:
        self._voice_row(tenant_id, voice_id)
        row = self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id = ?"
            " AND voice_id = ? AND superseded_at IS NULL",
            (tenant_id, voice_id)).fetchone()
        if row is None:
            raise ServiceError(404, "CANONICAL_NOT_SET",
                               "this voice has no current canonical revision")
        return {
            "voice_id": voice_id,
            "revision": row["revision"],
            "decision_kind": row["decision_kind"],
            "decided_at": row["decided_at"],
            "prior_canonical": row["prior_canonical"],
            "reason_text": row["reason_text"],
        }

    def get_lexicon_revision(self, tenant_id: str, lexicon_id: str,
                             revision_no: int) -> dict:
        lexicon = self._lexicon_row(tenant_id, lexicon_id)
        row = self._lexicon_revision_row(tenant_id, lexicon_id, revision_no)
        return {
            "lexicon_id": lexicon_id,
            "revision": row["revision"],
            "scope_kind": lexicon["scope_kind"],
            "scope_id": lexicon["scope_id"],
            "language": lexicon["language"],
            "content_hash": row["content_hash"],
            "entries": _loads(row["entries"], []),
            "entry_count": row["entry_count"],
            "head_revision": lexicon["head_revision"],
            "created_at": row["created_at"],
        }

    def lookup_voice(self, tenant_id: str, *, series_scope: str,
                     alias: str) -> dict:
        """`GET /voices:lookup?series_scope=&alias=`：按显示名找音色。

        没有 alias 就不查——一个 series_scope 下的全量清单不是"查找"。
        """
        if not series_scope or not alias:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "series_scope and alias are required")
        row = self.conn.execute(
            "SELECT * FROM voice WHERE tenant_id = ? AND series_scope = ?"
            " AND display_name = ?", (tenant_id, series_scope, alias)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no voice named {alias!r} in {series_scope!r}")
        return self.get_voice(tenant_id, row["voice_id"])

    # ------------------------------------------------------------- 过期清理

    def expire_sweep(self, tenant_id: str) -> dict:
        """§7 的 `expiry-sweep`：把 `expires_at < now()` 的 `granted` 置 `expired`。

        这是**可选**的维护动作，不是资格判定的前提——`check_eligibility`
        在读时就会判到期（§4.2 第 6 条）。它失败也不会让已过期音色变得可用。
        """
        now = _now()
        rows = self.conn.execute(
            "SELECT * FROM voice_authorization WHERE tenant_id = ?"
            " AND state = 'granted' AND expires_at IS NOT NULL"
            " AND expires_at < ?", (tenant_id, now)).fetchall()
        expired = []
        for row in rows:
            seq = int(row["state_seq"])
            uses = _loads(row["authorized_uses"], [])
            actor = {"principal_id": "system:expiry-sweep", "kind": "system"}
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE voice_authorization SET state = 'expired',"
                    " state_seq = ?, state_changed_at = ?, reason_code = ?,"
                    " changed_by = ? WHERE tenant_id = ? AND voice_id = ?"
                    " AND state = 'granted'",
                    (seq + 1, now, "RIGHTS_EXPIRED", _dumps(actor), tenant_id,
                     row["voice_id"]))
                self._append_authorization_event(
                    tenant_id=tenant_id, voice_id=row["voice_id"], seq=seq + 1,
                    from_state="granted", to_state="expired",
                    reason_code="RIGHTS_EXPIRED", reason_text=None,
                    evidence_ref=None, effective_at=row["expires_at"],
                    authorized_uses_before=uses,
                    expires_at_before=row["expires_at"], actor=actor)
                self._emit_auth_changed(
                    tenant_id=tenant_id, voice_id=row["voice_id"],
                    from_state="granted", to_state="expired", seq=seq + 1,
                    reason_code="RIGHTS_EXPIRED", evidence_ref=None,
                    effective_at=row["expires_at"],
                    authorized_uses_before=uses, expires_at=row["expires_at"],
                    command_id=f"sweep:{row['voice_id']}:{seq + 1}",
                    home_cell="sweep")
            expired.append({"voice_id": row["voice_id"], "state_seq": seq + 1})
        return {"expired": expired}


__all__ = [
    "AUTHORIZATION_PERMISSION",
    "CONTRACT_GAPS",
    "CONTENT_PERMISSION",
    "DENY_REASONS",
    "ERROR_CODES",
    "EVENT_AUTHORIZATION_CHANGED",
    "ID_PREFIX",
    "REASON_CODES",
    "SCHEMA",
    "SOURCE",
    "TOPIC_VOICE",
    "ServiceError",
    "Transaction",
    "VoiceAssetService",
    "effective_state",
    "revision_content_hash",
    "use_is_authorized",
]
