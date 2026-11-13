"""S31 内核。方案：`plans/S31-speech-production.md`。

一句话：**声音是人格，不是参数。** 全文件围绕这一句的四个可验证后果：

1. **没有换声线的代码路径。** `speech_job.voice_binding_id` /
   `voice_binding_revision` 是 NOT NULL，且本文件没有任何一条 UPDATE 碰它们；
   表上也没有 `fallback_voice_id`。想换声音只能新建一个 job。
2. **授权快照不放行。** `VoiceBindingRef` 只用于事后解释；放行必须由
   `rights_recheck_log` 里一条 `eligible=1` 的记录背书，而那条记录只能来自
   一次 live `check-eligibility`。五个时点一个不少（§4.2），其中
   `pre_dispatch`（提交前最后一刻）最容易漏，`post_ingest`（钱已经花了）
   最容易被当成"太晚了算了"。
3. **`RIGHTS_BLOCKED` 是终态。** 它不在 `RETRYABLE_STATES` 里，重试入口结构上
   看不见它；`:retry` 对它返回 409 而不是 422，好让"不可重试"与"参数错了"
   在客户端可区分。
4. **付费只走 S26。** 本文件里没有任何供应商 URL、没有任何 HTTP；提交是一次
   注入的 `submitter` 调用，测试里是 stub。

三条容易写错、这里显式写死的语义：

- **`require_alignment` 是供应商词级时间戳能力**，不是"S37 要做对齐"。能力
  缺失 → `REJECTED_LOCAL(CAPABILITY_MISSING)`，**不降级为 false**（§2.5）。
- **时长只有一个诚实的答案**：容器实测的 `observed_duration_ms`（台词跨度的
  上界）。`dialogue_span_ms` 永远发 `null` + `pending_s37`，不缓存、不估算。
- **台账键是 `shot_lineage_id`**，与 S39 的 `AttemptCounterKey` 一致，**不从
  `utterance_id` 派生**；复用写 0 行台账（§8.1/§8.2）。

事务与幂等与 S33 同构：平台幂等 SDK 的 `begin/finish` 是裸语句，所以命令事务
内把结果写回同一个 `idempotency_record` 行，"提交"与"记下已提交"原子成立。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    JOB_STATES,
    OUTPUT_FORMATS,
    OWNED_ACTIONS,
    OWNED_DEFECT_TYPES,
    RETRYABLE_STATES,
    RIGHTS_DENY_REASONS,
    SCHEMA,
    TECHNICAL_DENY_REASONS,
    TERMINAL_STATES,
)

SOURCE = "S31"
SCHEMA_VERSION = "2.0"

# 已登记事件（`contracts/event-topics.v2.json`：只有这一个）。
EVENT_SPEECH_READY = "production.speech.ready"

# 方案 §6.4 自己提出、但**没有登记**的三个（缺口 C6）。用本地类型名发出，
# 不冒充登记事件——订阅方按 event-topics 写路由，发了也进不了他们的表。
LOCAL_JOB_STATE = "speech.job.state-changed"
LOCAL_RIGHTS_LAPSED = "speech.voice-authorization-lapsed"
LOCAL_UTTERANCE_REVISION = "speech.utterance-revision.recorded"

# 方案 §4.2 的五个时点，一字不多一字不少（schema 的 CHECK 也是这五个）。
SUBMIT_SYNC = "submit_sync"
WORKER_START = "worker_start"
PRE_DISPATCH = "pre_dispatch"
POST_INGEST = "post_ingest"
EVENT_DRIVEN = "event_driven"

# 运行时产生的、**不属于 S15 词表**的 deny_reason 之一：上游不可达。
# 它不是 S15 的拒绝，但"查不到"必须与"查到了可用"可区分（E30）。
UNREACHABLE = "S15_UNREACHABLE"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(value) -> datetime | None:
    """ISO-8601 → 带时区的 `datetime`。**不能拿字符串比大小。**

    方案 §6.1 的示例 deadline 是 `+08:00` 偏移，而 `_now()` 是 `Z`：
    `"2026-11-13T01:00:00+08:00" <= "2026-11-13T00:00:00.000Z"` 在字节上
    为 False，实际它早了 8 小时。解析不了的返回 None，由调用方决定是拒绝
    还是保守处理。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _deadline_passed(deadline) -> bool:
    """死线是否已过。解析不了按"已过"处理：宁可拒一个合法请求，也不为
    一个说不清时间点的请求去花一次钱。"""
    parsed = _parse_ts(deadline)
    if parsed is None:
        return True
    return parsed <= datetime.now(timezone.utc)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(raw, default=None):
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


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


# ------------------------------------------------------------ 纯函数：指纹

def input_fingerprint(*, text_hash: str, voice_id: str, voice_revision: int,
                      lexicon_id: str | None, lexicon_revision: int | None,
                      lexicon_content_hash: str | None, emotion: str,
                      locale: str, output_format: str,
                      sample_rate_hz: int, require_alignment: bool) -> str:
    """§4.4，逐项照抄，**包括 `locale`**。

    用 `\\x1f` 分隔而不是直接拼接：直接拼会让 `("ab","c")` 与 `("a","bc")`
    撞成同一个指纹。方案写的是 `||`，那是散文里的并置符号，不是 SQL 语义。

    不进来的那些（`variation_nonce` / `seed` / `attempt_id` / `provider` /
    `model_id` / `command_id` / `trace_id` / `deadline` / `budget_scope_id` /
    `node_epoch` / `execution_fence` / `manifest_revision`）都是"怎么做的"，
    不是"做的是什么"。它们一旦进来，重跑整集就会全部重新付费（§4.4 / E38）。

    `lexicon.source_kind` **不在**里面：同一份词典无论是调用方显式给的还是
    音色默认带的，念出来的字一样，不该被算成两次生产。进指纹的是内容
    （`id` + `revision` + `content_hash`），不是来源标签。
    """
    return _sha256_hex("\x1f".join([
        text_hash,
        voice_id,
        str(voice_revision),
        lexicon_id or "",
        "" if lexicon_revision is None else str(lexicon_revision),
        lexicon_content_hash or "",
        emotion,
        locale,
        output_format,
        str(sample_rate_hz),
        "1" if require_alignment else "0",
    ]))


# ------------------------------------------------------------------ 服务

def _row(row) -> dict | None:
    return None if row is None else dict(row)


class SpeechService:
    """所有上游都是注入的 callback（见 `deps.py`）。本文件不 import 任何服务。"""

    def __init__(self, conn: sqlite3.Connection, *, eligibility,
                 lexicon_reader, bindings_reader, route_resolver, submitter,
                 canceler, artifact_check, generation_state=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 会**隐式提交当前事务**。所以只能在事务外建一次，之后复用同一个实例——
        # 否则命令事务会在第一条事件写入时被悄悄提交掉，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.eligibility = eligibility
        self.lexicon_reader = lexicon_reader
        self.bindings_reader = bindings_reader
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.canceler = canceler
        self.artifact_check = artifact_check
        self.generation_state = generation_state

    # -------------------------------------------------------- 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _transition(self, job: dict, to_state: str, *, reason_code=None,
                    detail=None, outbox: Outbox | None = None) -> dict:
        if to_state not in JOB_STATES:
            raise AssertionError(f"unknown state {to_state}")
        seq = int(job["state_seq"]) + 1
        now = _now()
        self.conn.execute(
            "UPDATE speech_job SET state=?, state_seq=?, updated_at=?, "
            "state_entered_at=?, failure_code=COALESCE(?, failure_code), "
            "failure_detail=COALESCE(?, failure_detail) WHERE speech_job_id=?",
            (to_state, seq, now, now, reason_code, detail,
             job["speech_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (speech_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["speech_job_id"], seq, job["state"], to_state, reason_code,
             detail, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_JOB_STATE,
                subject=f"speech-job/{job['speech_job_id']}",
                tenant_id=job["tenant_id"], home_cell="speech",
                aggregate_id=job["speech_job_id"], sequence=seq,
                correlation_id=job["speech_job_id"],
                data={"speech_job_id": job["speech_job_id"],
                      "utterance_id": job["utterance_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "locale": job["locale"],
                      "from": job["state"], "to": to_state,
                      "reason_code": reason_code},
                schema_version=SCHEMA_VERSION))
        return {**job, "state": to_state, "state_seq": seq}

    def _job(self, tenant_id: str, job_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM speech_job WHERE tenant_id=? AND speech_job_id=?",
            (tenant_id, job_id)).fetchone())
        if row is None:
            raise ServiceError(404, "SPEECH_JOB_NOT_FOUND", f"no job {job_id}")
        return row

    # -------------------------------------------------------- 授权复查

    def _recheck(self, *, tenant_id: str, voice_id: str, voice_revision: int,
                 requested_use: dict, check_point: str,
                 job_id: str | None) -> dict:
        """§4.2 的五个时点共用的**唯一**放行入口。

        返回 `{eligible, observed_state, observed_state_seq, deny_reason,
        mapping, expires_at, detail}`，并**无条件**写一行 `rights_recheck_log`
        （调用方必须已开事务，且与状态转移同事务——§3.3）。

        三条硬语义：

        - **不可达 = 不可用。** `observed_state='unreachable'` 且
          `eligible=0`，DB 的 `rrl_unreachable_not_eligible` 也拦着。
        - **快照不参与。** 这里只读 `eligibility` 的返回值；任何来自
          `voice_binding_ref` 的 `authorization_state` 都不进来。
        - **不缓存。** S15 的 check-eligibility 是强一致读，这里每次调用。
        """
        if check_point not in (SUBMIT_SYNC, WORKER_START, PRE_DISPATCH,
                               POST_INGEST, EVENT_DRIVEN):
            raise AssertionError(f"unknown recheck point {check_point}")
        try:
            observed = self.eligibility(tenant_id, voice_id, voice_revision,
                                        requested_use)
            if not isinstance(observed, dict):
                raise ConnectionError("eligibility returned a non-object")
            state = observed.get("authorization_state")
            eligible = observed.get("eligible") is True
            deny = observed.get("deny_reason")
            if state not in ("granted", "suspended", "revoked", "expired"):
                state = "granted" if eligible else "unreachable"
            if eligible and state != "granted":
                # S15 说 eligible 但状态是 revoked —— 两者矛盾时按**更保守**
                # 的那个读。一个自相矛盾的上游不能被当成放行证据。
                eligible = False
                deny = deny or {
                    "revoked": "AUTHORIZATION_REVOKED",
                    "expired": "AUTHORIZATION_EXPIRED",
                    "suspended": "AUTHORIZATION_SUSPENDED",
                }.get(state, "AUTHORIZATION_SUSPENDED")
            observed_state = state
            seq = observed.get("state_seq")
            mapping = observed.get("provider_mapping")
            expires_at = observed.get("expires_at")
            detail = observed.get("detail")
        except (ServiceError, ConnectionError) as exc:
            # 上游明确拒绝（4xx）与不可达分开记：前者带自己的 code，后者记
            # `unreachable`。两者都**不放行**。
            observed_state, eligible = "unreachable", False
            deny, seq, mapping, expires_at = UNREACHABLE, None, None, None
            if isinstance(exc, ServiceError) and exc.status in (400, 404, 422):
                detail = f"S15 rejected the eligibility query: {exc.code}"
            else:
                detail = ("S15 check-eligibility is unreachable; an outage is "
                          "not evidence that the voice may be used")

        # "unreachable" 只允许 denied。DB 里也有 CHECK，但这里先拦一次，
        # 让错误在这行就报出来而不是以 IntegrityError 的形式冒到 500。
        if observed_state == "unreachable":
            eligible = False
        if eligible and observed_state != "granted":
            eligible = False
            deny = deny or "AUTHORIZATION_SUSPENDED"

        now = _now()
        self.conn.execute(
            "INSERT INTO rights_recheck_log (recheck_id, tenant_id, "
            "speech_job_id, voice_id, check_point, observed_state, "
            "observed_state_seq, eligible, deny_reason, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_new_id("recheck"), tenant_id, job_id or "", voice_id,
             check_point, observed_state, seq, 1 if eligible else 0, deny, now))
        return {"eligible": eligible, "observed_state": observed_state,
                "observed_state_seq": seq, "deny_reason": deny,
                "mapping": mapping, "expires_at": expires_at,
                "detail": detail, "checked_at": now}

    @staticmethod
    def _requested_use(locale: str) -> dict:
        """S15 的 `requested_use` 只要求 `modality`，这里再补语言与用途名。

        `use` 走 `"speech"` 而不是 `"audiobook"` —— 本服务生产的是影视配音。
        F6 的 `USE_NOT_AUTHORIZED` 由 S15 的 `authorized_uses` 判定，这里不预判。
        """
        return {"modality": "speech", "use": "speech", "locale": locale}

    # -------------------------------------------------------- 词典解析

    def _resolve_lexicon(self, tenant_id: str, *, project_id: str,
                         series_id: str, voice_id: str,
                         requested: dict | None) -> dict:
        """§2.4 的三级解析，**顺序即语义**。

        1. 请求显式给出 → `explicit`
        2. 请求为 null 且 S15 有该音色的默认绑定 → `voice_default`
        3. 都没有 → `none`（`lexicon_id` 为 null，且不吃任何词表）

        校验：跨租户 → 404（不泄漏存在性）；同租户跨项目/跨剧 → 422
        `LEXICON_SCOPE_MISMATCH`。**不猜、不降级成"没有"**：拿错词典会把
        专名念错，而那正是词典存在的理由。
        """
        explicit = requested or None
        if explicit is not None:
            if not isinstance(explicit, dict) or not explicit.get("id"):
                raise ServiceError(422, "INVALID_PRONUNCIATION_LEXICON",
                                   "pronunciation_lexicon must be {id, revision}")
            revision = explicit.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) \
                    or revision < 1:
                raise ServiceError(422, "INVALID_PRONUNCIATION_LEXICON",
                                   "pronunciation_lexicon.revision must be >= 1")
            resolved = self._read_lexicon(tenant_id, explicit["id"], revision)
            return self._checked_lexicon(resolved, project_id, series_id,
                                         "explicit")

        bindings = self._read_bindings(tenant_id, voice_id)
        default = None
        for entry in (bindings or {}).get("lexicon_bindings") or []:
            if entry.get("is_default"):
                default = entry
                break
        if default is None:
            return {"lexicon_id": None, "revision": None, "content_hash": None,
                    "source_kind": "none", "scope_kind": None, "scope_id": None}
        resolved = self._read_lexicon(tenant_id, default["lexicon_id"],
                                      int(default["lexicon_revision"]))
        return self._checked_lexicon(resolved, project_id, series_id,
                                     "voice_default")

    def _read_lexicon(self, tenant_id: str, lexicon_id: str,
                      revision: int) -> dict:
        try:
            resolved = self.lexicon_reader(tenant_id, lexicon_id, revision)
        except ServiceError as exc:
            if exc.status == 404:
                # 跨租户与不存在在 S15 眼里是同一种情况，这里也不区分。
                raise ServiceError(404, "LEXICON_NOT_FOUND",
                                   f"no lexicon {lexicon_id}") from exc
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "LEXICON_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        if not isinstance(resolved, dict) or not resolved.get("content_hash"):
            raise ServiceError(404, "LEXICON_NOT_FOUND",
                               f"no lexicon {lexicon_id} revision {revision}")
        return resolved

    def _read_bindings(self, tenant_id: str, voice_id: str) -> dict:
        try:
            bindings = self.bindings_reader(tenant_id, voice_id)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "VOICE_BINDINGS_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        return bindings if isinstance(bindings, dict) else {}

    @staticmethod
    def _checked_lexicon(resolved: dict, project_id: str, series_id: str,
                         source_kind: str) -> dict:
        scope_kind = resolved.get("scope_kind")
        scope_id = resolved.get("scope_id")
        if scope_kind == "project" and scope_id != project_id:
            raise ServiceError(
                422, "LEXICON_SCOPE_MISMATCH",
                "lexicon belongs to a different project",
                details={"lexicon_scope_kind": scope_kind})
        if scope_kind == "series" and scope_id not in (series_id, None):
            raise ServiceError(
                422, "LEXICON_SCOPE_MISMATCH",
                "lexicon belongs to a different series",
                details={"lexicon_scope_kind": scope_kind})
        if scope_kind == "voice" and scope_id is None:
            raise ServiceError(422, "LEXICON_SCOPE_MISMATCH",
                               "voice-scoped lexicon has no owner")
        return {"lexicon_id": resolved["lexicon_id"],
                "revision": int(resolved["revision"]),
                "content_hash": resolved["content_hash"],
                "source_kind": source_kind,
                "scope_kind": scope_kind, "scope_id": scope_id}

    # -------------------------------------------------------- 校验（§4.1）
    def _validate(self, body: dict, tenant_id: str) -> dict:
        """a–j，**顺序即方案顺序**：先便宜的、先无副作用的。

        (c) 的跨租户判定要调 S15 的只读接口；它在最前面，因为"这个音色不是
        你的"不该等到算完指纹才发现。
        """
        ctx = body.get("context") or {}
        if not isinstance(ctx, dict):
            raise ServiceError(422, "INVALID_CONTEXT", "context must be object")
        if ctx.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id does not match the credential")
        for key in ("project_id", "series_id", "episode_id", "branch_id",
                    "node_id"):
            if not ctx.get(key):
                raise ServiceError(422, "INVALID_CONTEXT",
                                   f"context.{key} required")
        if ctx.get("node_epoch") is None or ctx.get("execution_fence") is None:
            raise ServiceError(422, "INVALID_CONTEXT",
                               "context.node_epoch and execution_fence required")

        utterance = body.get("utterance") or {}
        if not isinstance(utterance, dict):
            raise ServiceError(422, "INVALID_UTTERANCE",
                               "utterance must be object")

        voice = body.get("voice_binding") or {}
        if not isinstance(voice, dict) or not voice.get("id"):
            raise ServiceError(422, "INVALID_VOICE_BINDING",
                               "voice_binding.id required")
        voice_revision = voice.get("revision")
        if not isinstance(voice_revision, int) or isinstance(voice_revision,
                                                             bool) \
                or voice_revision < 1:
            raise ServiceError(422, "INVALID_VOICE_BINDING",
                               "voice_binding.revision must be >= 1")

        # (d) 文本与 locale。空台词不能合成（F1）；纯空白与空同罪。
        text = utterance.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(422, "EMPTY_UTTERANCE",
                               "utterance.text must be non-blank")
        locale = utterance.get("locale")
        if not isinstance(locale, str) or not locale.strip():
            raise ServiceError(422, "INVALID_LOCALE",
                               "utterance.locale required")

        # (e)(f) 文本版本。`utterance_id + locale` 必须唯一确定一个 revision，
        # 且 revision 与 text 必须互相印证（F2）。两者都不靠"调用方说对就是对"。
        revision = utterance.get("text_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) \
                or revision < 1:
            raise ServiceError(422, "INVALID_TEXT_REVISION",
                               "utterance.text_revision must be >= 1")
        utterance_id = utterance.get("utterance_id")
        if not isinstance(utterance_id, str) or not utterance_id.strip():
            raise ServiceError(422, "INVALID_UTTERANCE",
                               "utterance.utterance_id required")
        text_hash = _sha256_hex(text)
        existing = _row(self.conn.execute(
            "SELECT * FROM utterance_revision WHERE tenant_id=? AND "
            "utterance_id=? AND locale=? AND text_revision=?",
            (tenant_id, utterance_id, locale, revision)).fetchone())
        if existing is not None and existing["text_hash"] != text_hash:
            raise ServiceError(
                422, "TEXT_REVISION_CONFLICT",
                "this text_revision already names a different text; bump "
                "text_revision instead of rewriting it",
                details={"text_revision": revision,
                         "stored_text_hash": existing["text_hash"],
                         "incoming_text_hash": text_hash})
        # 同一份文本不许占用两个 revision 号：否则"第几次改"没有答案。
        same_text = _row(self.conn.execute(
            "SELECT * FROM utterance_revision WHERE tenant_id=? AND "
            "utterance_id=? AND locale=? AND text_hash=?",
            (tenant_id, utterance_id, locale, text_hash)).fetchone())
        if same_text is not None and int(same_text["text_revision"]) != revision:
            raise ServiceError(
                422, "TEXT_REVISION_CONFLICT",
                "this text already has a different text_revision",
                details={"stored_text_revision": int(same_text["text_revision"]),
                         "incoming_text_revision": revision})

        # (g) emotion 非空。闭集不存在（C5），这里不猜词表。
        emotion = body.get("emotion")
        if not isinstance(emotion, str) or not emotion.strip():
            raise ServiceError(422, "EMPTY_EMOTION", "emotion must be non-blank")

        # (h) 输出规格闭集。
        spec = body.get("output_spec") or {}
        if not isinstance(spec, dict):
            raise ServiceError(422, "INVALID_OUTPUT_SPEC",
                               "output_spec must be object")
        output_format = spec.get("format")
        if output_format not in OUTPUT_FORMATS:
            raise ServiceError(422, "INVALID_OUTPUT_SPEC",
                               f"output_spec.format must be one of "
                               f"{list(OUTPUT_FORMATS)}")
        sample_rate = spec.get("sample_rate_hz")
        if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) \
                or sample_rate < 8000:
            raise ServiceError(422, "INVALID_OUTPUT_SPEC",
                               "output_spec.sample_rate_hz must be >= 8000")

        # (j) 死线。**它的比较必须在所有昂贵调用之前**，否则会为一个已经过期
        # 的请求去问 S15 / S25。
        deadline = body.get("deadline")
        if not deadline:
            raise ServiceError(422, "INVALID_BODY", "deadline required")
        if _deadline_passed(deadline):
            raise ServiceError(422, "DEADLINE_PASSED",
                               "deadline is not in the future",
                               details={"deadline": deadline})

        return {"context": ctx, "utterance": utterance, "voice": voice,
                "voice_id": voice["id"], "voice_revision": voice_revision,
                "text": text, "text_hash": text_hash, "locale": locale,
                "text_revision": revision, "utterance_id": utterance_id,
                "emotion": emotion, "output_format": output_format,
                "sample_rate": sample_rate,
                "require_alignment": bool(body.get("require_alignment")),
                "deadline": deadline}

    # -------------------------------------------------------- 修复动作守卫
    def _check_repair(self, body: dict, utterance: dict) -> dict | None:
        """§4.6。**S31 只执行 `re_synthesize_speech` 且只针对 `voice` 缺陷。**

        三个拒绝都发生在创建任何 job 之前（没花钱、没写台账）：
        - `re_synthesize_speech` + 非 `voice` 缺陷 → 422，带 `expected_type=voice`
        - `redo_lipsync` / `regenerate_video` / 其它族 → 422（不是 S31 的入口）
        - lineage 不符 → 422 `LINEAGE_MISMATCH`

        反向升级也不做：`voice` 缺陷**只**产生 `re_synthesize_speech`，绝不
        顺手升级成 `regenerate_video`（那会把"改一句台词"放大成一次视频重生成）。
        """
        action = body.get("repair_action")
        if action is None:
            return None
        if not isinstance(action, dict):
            raise ServiceError(422, "INVALID_REPAIR_ACTION",
                               "repair_action must be object")
        action_type = action.get("action_type")
        if action_type not in OWNED_ACTIONS:
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                f"S31 executes only {list(OWNED_ACTIONS)}",
                details={"received": action_type, "expected_type": "voice"})
        defect_type = action.get("defect_type")
        if defect_type not in OWNED_DEFECT_TYPES:
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                "re_synthesize_speech is only valid for voice defects; "
                "a lipsync defect does not imply the words were wrong",
                details={"received_defect_type": defect_type,
                         "expected_type": "voice"})
        action_lineage = action.get("shot_lineage_id")
        if action_lineage and utterance.get("shot_lineage_id") \
                and action_lineage != utterance["shot_lineage_id"]:
            raise ServiceError(
                422, "LINEAGE_MISMATCH",
                "repair action targets a different shot lineage",
                details={"repair_shot_lineage_id": action_lineage,
                         "utterance_shot_lineage_id":
                             utterance["shot_lineage_id"]})
        if not action.get("action_id"):
            raise ServiceError(422, "INVALID_REPAIR_ACTION",
                               "repair_action.action_id required")
        return action

    # -------------------------------------------------------- 可复用查找
    def _find_reusable(self, *, tenant_id: str, fingerprint: str,
                       repair_action: dict | None) -> dict:
        """§4.4 的四条件。返回 `{"take":…}` / `{"blocked":…}` / `None`。

        - 带 `repair_action_id` → 直接 `None`（修复必须真的重做，E24）
        - `rights_state <> 'usable'` → **不建新 job**，抛 409
          `VOICE_AUTHORIZATION_REVOKED`。命中一个已撤销音色的产物，说明这次
          请求要的正是那个声音，而它已经不能用了。
        - artifact 不再可引用 → `None`（S28 可能已 tombstone，E23）
        - 同 tenant 由 SQL 保证。
        """
        if repair_action is not None:
            return None
        rows = self.conn.execute(
            "SELECT * FROM voice_take WHERE tenant_id=? AND "
            "input_fingerprint=? ORDER BY created_at DESC",
            (tenant_id, fingerprint)).fetchall()
        if not rows:
            return None
        for raw in rows:
            take = dict(raw)
            if take["rights_state"] != "usable":
                raise ServiceError(
                    409, "VOICE_AUTHORIZATION_REVOKED",
                    "a take with this exact input is now void because its "
                    "voice authorization lapsed; S31 does not fall back to "
                    "another voice",
                    details={"voice_take_id": take["voice_take_id"],
                             "rights_state": take["rights_state"]})
            if take["state"] not in ("READY", "ACCEPTED"):
                # SUPERSEDED 的历史产物可以复用（字节还在，内容没变），
                # 但只有在它仍是 usable 时；这里不排除 SUPERSEDED，
                # 因为"剪辑重新采用一段旧音频"是合法的。
                continue
            artifact = _loads(take["artifact_ref"], {})
            try:
                referenceable = self.artifact_check(tenant_id, artifact)
            except (ServiceError, ConnectionError):
                # 查不到 ≠ 不可用，但**复用**要求"确认还在"。查不到就不复用，
                # 走一次新 job；反过来把没校验的字节递出去才不可接受（E23）。
                referenceable = False
            if not referenceable:
                continue
            return {"take": take, "artifact_ref": artifact}
        return None

    # -------------------------------------------------------- 创建
    def create_job(self, tenant_id: str, command_id: str, body: dict,
                   request_hash: str) -> tuple[int, dict]:
        """`POST /speech-jobs`。返回 `(http_status, body)`。

        返回值带状态码，因为"复用"是 200、"新建"是 202 —— 这不是风格问题：
        200 的 body 里 `speech_job_id` 是 null，调用方若按 202 处理会去轮询
        一个不存在的 job。两个语义必须由 HTTP 层本身区分。
        """
        body = body if isinstance(body, dict) else {}
        v = self._validate(body, tenant_id)
        repair = self._check_repair(body, v["utterance"])

        # (c) 跨租户：S15 对别的租户的音色返回 404，这里不区分"不存在"与
        # "不是你的"，避免用 404/403 的差异做存在性探测。
        bindings = self._read_bindings(tenant_id, v["voice_id"])

        deadline = v["deadline"]
        del deadline  # 已在校验里比较过；此处不再读时间，避免第二次 now() 抖动

        # 时点 1：同步快路径。**不写日志**（此刻还没有 job 可挂），只回答
        # 一个即时的是/否。它省一次 worker 唤醒，不承担放行责任——真正的
        # 放行证据是 CHECKING_RIGHTS 那一行。
        observed = self._observe(tenant_id, v)
        if observed["eligible"] is False and observed["deny_reason"] in \
                RIGHTS_DENY_REASONS:
            raise ServiceError(
                409, "VOICE_AUTHORIZATION_REVOKED",
                "the voice is not eligible for speech synthesis right now",
                details={"deny_reason": observed["deny_reason"],
                         "authorization_state": observed["observed_state"],
                         "state_seq": observed["observed_state_seq"],
                         "voice_id": v["voice_id"],
                         "voice_revision": v["voice_revision"]})

        lexicon = self._resolve_lexicon(
            tenant_id, project_id=v["context"]["project_id"],
            series_id=v["context"]["series_id"], voice_id=v["voice_id"],
            requested=body.get("pronunciation_lexicon"))
        fingerprint = input_fingerprint(
            text_hash=v["text_hash"], voice_id=v["voice_id"],
            voice_revision=v["voice_revision"],
            lexicon_id=lexicon["lexicon_id"],
            lexicon_revision=lexicon["revision"],
            lexicon_content_hash=lexicon["content_hash"],
            emotion=v["emotion"], locale=v["locale"],
            output_format=v["output_format"],
            sample_rate_hz=v["sample_rate"],
            require_alignment=v["require_alignment"])

        # 复用：**先于** job 落库。命中时响应里没有 speech_job_id，若先建
        # 一行再退，那个 job 就成了一条永远不前进的孤儿记录。
        reuse = self._find_reusable(tenant_id=tenant_id,
                                    fingerprint=fingerprint,
                                    repair_action=repair)
        if reuse is not None:
            take = reuse["take"]
            return 200, {
                "speech_job_id": None, "reused": True,
                "voice_take_id": take["voice_take_id"],
                "artifact_ref": reuse["artifact_ref"],
                "audio_duration_ms": int(take["observed_duration_ms"]),
                "note": "input_fingerprint matched an existing usable take; "
                        "no paid job was created",
            }

        job_id = _new_id("job")
        effective_hash = request_hash or _sha256_hex(_jcs(
            {k: val for k, val in body.items()
             if k not in ("command_id", "request_hash")}))
        idem_key = _sha256_hex("\x1f".join(
            [tenant_id, command_id, effective_hash]))

        def produce():
            now = _now()
            self._record_utterance(tenant_id, v, now)
            vbr_id = self._record_voice_ref(tenant_id, v, observed, now)
            lbr_id = self._record_lexicon_ref(tenant_id, lexicon, now)
            self.conn.execute(
                "INSERT INTO speech_job (speech_job_id, tenant_id, project_id, "
                "series_id, episode_id, branch_id, utterance_id, "
                "utterance_index, shot_lineage_id, locale, text_revision, "
                "source_revision, voice_binding_ref_id, lexicon_binding_ref_id, "
                "voice_binding_id, voice_binding_revision, emotion, "
                "output_format, sample_rate_hz, require_alignment, node_id, "
                "node_epoch_at_create, execution_fence, input_fingerprint, "
                "request_hash, idempotency_key, repair_action_id, "
                "strategy_class, variation_nonce, route_policy_ref, "
                "route_decision_ref, budget_scope_id, deadline, "
                "execution_context, state, state_seq, created_at, updated_at, "
                "state_entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, tenant_id, v["context"]["project_id"],
                 v["context"]["series_id"], v["context"]["episode_id"],
                 v["context"]["branch_id"], v["utterance_id"],
                 int(v["utterance"].get("utterance_index") or 0),
                 v["utterance"].get("shot_lineage_id"), v["locale"],
                 v["text_revision"], v["utterance"].get("source_revision"),
                 vbr_id, lbr_id, v["voice_id"], v["voice_revision"],
                 v["emotion"], v["output_format"], v["sample_rate"],
                 1 if v["require_alignment"] else 0,
                 v["context"]["node_id"], int(v["context"]["node_epoch"]),
                 int(v["context"]["execution_fence"]), fingerprint,
                 effective_hash, idem_key,
                 repair["action_id"] if repair else None,
                 _strategy_class(repair, v["emotion"]),
                 body.get("variation_nonce") or uuid.uuid4().hex,
                 _jcs(body["route_policy_ref"])
                 if body.get("route_policy_ref") else None,
                 None, str(body.get("budget_scope_id") or ""),
                 str(v["deadline"]), _jcs(v["context"]), "CREATED", 0,
                 now, now, now))
            job = self._job(tenant_id, job_id)
            job = self._transition(job, "CHECKING_RIGHTS",
                                   outbox=self.outbox)
            return self._after_created(tenant_id, job, v, lexicon, observed,
                                       repair, body)

        try:
            return 202, self._command(tenant_id, command_id, produce)
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "speech_job_open_uniq" not in text and \
                    "speech_job.tenant_id, speech_job.utterance_id" not in text:
                raise
            # 同一输入已有在途 job：**不**再开一条，也不重复付费。
            existing = _row(self.conn.execute(
                "SELECT * FROM speech_job WHERE tenant_id=? AND "
                "utterance_id=? AND locale=? AND input_fingerprint=? AND "
                "state NOT IN ('READY','REJECTED_LOCAL','RIGHTS_BLOCKED',"
                "'FAILED','RECONCILED_FAILED','CANCELED') "
                "ORDER BY created_at DESC",
                (tenant_id, v["utterance_id"], v["locale"],
                 fingerprint)).fetchone())
            if existing is None:
                raise
            return 202, {"speech_job_id": existing["speech_job_id"],
                         "state": existing["state"],
                         "operation_id": None,
                         "reused": False, "duplicate_suppressed": True}

    def _observe(self, tenant_id: str, v: dict) -> dict:
        """时点 1 用的只读观测。**不可达直接 503**：创建 job 需要一份真实的
        授权快照（`authorization_state_seq >= 1`），编不出来。"""
        try:
            observed = self.eligibility(
                tenant_id, v["voice_id"], v["voice_revision"],
                self._requested_use(v["locale"]))
        except (ServiceError, ConnectionError) as exc:
            raise ServiceError(
                503, "VOICE_ELIGIBILITY_UNAVAILABLE",
                "cannot read the voice's current authorization; refusing to "
                "create a job with a guessed snapshot",
                retryable=True,
                details={"cause": getattr(exc, "code", type(exc).__name__)}) \
                from exc
        if not isinstance(observed, dict):
            raise ServiceError(503, "VOICE_ELIGIBILITY_UNAVAILABLE",
                               "eligibility returned no object", retryable=True)
        state = observed.get("authorization_state")
        if state not in ("granted", "suspended", "revoked", "expired"):
            raise ServiceError(
                503, "VOICE_ELIGIBILITY_UNAVAILABLE",
                "eligibility response has no usable authorization_state",
                retryable=True)
        seq = observed.get("state_seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise ServiceError(
                503, "VOICE_ELIGIBILITY_UNAVAILABLE",
                "eligibility response has no usable state_seq", retryable=True)
        eligible = observed.get("eligible") is True and state == "granted"
        return {"eligible": eligible, "observed_state": state,
                "observed_state_seq": seq,
                "deny_reason": None if eligible else (
                    observed.get("deny_reason") or "AUTHORIZATION_SUSPENDED"),
                "mapping": observed.get("provider_mapping"),
                "expires_at": observed.get("expires_at"),
                "detail": observed.get("detail")}

    def _record_utterance(self, tenant_id: str, v: dict, now: str) -> None:
        """`UtteranceRevision` 落库。文本与 hash 已在 `_validate` 里核对过；
        这里只补来源。旁白没有镜头（`shot_lineage_id` 可为 NULL），这是
        已知的计数盲点（§8.2），不是错误。"""
        source_ref = v["utterance"].get("source_ref") or {
            "service_id": "S06"}
        self.conn.execute(
            "INSERT INTO utterance_revision (tenant_id, utterance_id, "
            "text_revision, locale, speaker_character_id, text, text_hash, "
            "emotion, shot_lineage_id, source_ref, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (tenant_id, v["utterance_id"], v["text_revision"], v["locale"],
             str(v["utterance"].get("speaker_character_id") or ""),
             v["text"], v["text_hash"], v["emotion"],
             v["utterance"].get("shot_lineage_id"),
             _jcs(source_ref), now))

    def _record_voice_ref(self, tenant_id: str, v: dict, observed: dict,
                          now: str) -> str:
        """快照 = **观测到的那一刻**，不是"已授权"的证明。它只用于
        `GET /speech-jobs/{id}` 事后解释"当时看到的是哪一版"。"""
        ref_id = _new_id("vbr")
        self.conn.execute(
            "INSERT INTO voice_binding_ref (voice_binding_ref_id, tenant_id, "
            "voice_id, voice_revision, authorization_state, "
            "authorization_state_seq, authorization_expires_at, captured_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ref_id, tenant_id, v["voice_id"], v["voice_revision"],
             observed["observed_state"], observed["observed_state_seq"],
             observed["expires_at"], now))
        return ref_id

    def _record_lexicon_ref(self, tenant_id: str, lexicon: dict,
                            now: str) -> str:
        ref_id = _new_id("lbr")
        self.conn.execute(
            "INSERT INTO lexicon_binding_ref (lexicon_binding_ref_id, "
            "tenant_id, lexicon_id, lexicon_revision, content_hash, "
            "source_kind, scope_kind, scope_id, captured_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ref_id, tenant_id, lexicon["lexicon_id"],
             lexicon["revision"], lexicon["content_hash"],
             lexicon["source_kind"], lexicon["scope_kind"],
             lexicon["scope_id"], now))
        return ref_id

    # -------------------------------------------------------- 创建后推进
    def _after_created(self, tenant_id: str, job: dict, v: dict,
                       lexicon: dict, observed: dict, repair: dict | None,
                       body: dict) -> dict:
        """CHECKING_RIGHTS → PREPARING → SUBMITTED。全部在同一个命令事务里，
        因为调用方拿到的 202 必须对应一个真实的、已经提交给 S26 的 job；
        "先返回 202 再慢慢提交"会把 `SUBMISSION_UNKNOWN` 变成常态。"""
        # 时点 2：worker 真正开工前。这是第一个**有日志**的复查。
        recheck = self._recheck(
            tenant_id=tenant_id, voice_id=job["voice_binding_id"],
            voice_revision=int(job["voice_binding_revision"]),
            requested_use=self._requested_use(job["locale"]),
            check_point=WORKER_START, job_id=job["speech_job_id"])
        if not recheck["eligible"]:
            if recheck["deny_reason"] in RIGHTS_DENY_REASONS:
                self._block(job, recheck, "RIGHTS_BLOCKED")
                return self._accepted(job["speech_job_id"])
            return self._reject_local(
                job, recheck["deny_reason"] or "MAPPING_UNAVAILABLE",
                detail="S15 refused at worker_start",
                extra={"deny_reason": recheck["deny_reason"]})

        job = self._transition(job, "PREPARING", outbox=self.outbox)
        route = self._route(tenant_id, job, v, lexicon, body)
        if route.get("_reject"):
            return route["_reject"]

        # 时点 3：提交前最后一刻。**最容易漏的一个**。从取路由到真正提交
        # 之间横着 S23 的 reserve、S22 的 permit、S26 的 SubmissionIntent，
        # 配额紧张时可能是几分钟。
        recheck = self._recheck(
            tenant_id=tenant_id, voice_id=job["voice_binding_id"],
            voice_revision=int(job["voice_binding_revision"]),
            requested_use=self._requested_use(job["locale"]),
            check_point=PRE_DISPATCH, job_id=job["speech_job_id"])
        if not recheck["eligible"]:
            if recheck["deny_reason"] in RIGHTS_DENY_REASONS:
                self._block(job, recheck, "RIGHTS_BLOCKED")
                return self._accepted(job["speech_job_id"])
            return self._reject_local(
                job, recheck["deny_reason"] or "MAPPING_UNAVAILABLE",
                detail="S15 refused immediately before dispatch",
                extra={"deny_reason": recheck["deny_reason"]})

        return self._submit(tenant_id, job, v, lexicon, route, repair)

    def _route(self, tenant_id: str, job: dict, v: dict, lexicon: dict,
               body: dict) -> dict:
        """PREPARING：取路由、判能力、判驻留。**任何失败都在花钱之前。**"""
        request = {"modality": "speech", "locale": job["locale"],
                   "require_alignment": bool(job["require_alignment"]),
                   "output_spec": {"format": job["output_format"],
                                   "sample_rate_hz": int(job["sample_rate_hz"])},
                   "voice_binding": {"id": job["voice_binding_id"],
                                     "revision": int(job["voice_binding_revision"])},
                   "route_policy_ref": _loads(job["route_policy_ref"], None)}
        try:
            route = self.route_resolver(tenant_id, request)
        except ServiceError as exc:
            raise ServiceError(exc.status, exc.code, str(exc),
                               retryable=exc.retryable,
                               details=exc.details) from exc
        except ConnectionError as exc:
            raise ServiceError(503, "ROUTE_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        route = route if isinstance(route, dict) else {}

        capabilities = route.get("capabilities") or {}
        if bool(job["require_alignment"]) and \
                capabilities.get("word_timestamps") is False:
            # F10：**不降级**。把 true 悄悄改成 false，等于把"我需要词级
            # 时间码"这个要求静默丢掉，下游 S37 之后才对不上。
            return {"_reject": self._reject_local(
                job, "CAPABILITY_MISSING",
                detail="require_alignment=true but the selected route has no "
                       "word_timestamps capability; refusing to downgrade",
                extra={"required_capability": "word_timestamps"})}
        if route.get("feasible") is False:
            # F11：透传降级选项，**不自己挑一个**。
            return {"_reject": self._reject_local(
                job, route.get("reason_code") or "ROUTE_INFEASIBLE",
                detail="no feasible route",
                extra={"degradation_options":
                       route.get("degradation_options") or []})}
        if route.get("residency_allowed") is False:
            return {"_reject": self._reject_local(
                job, "RESIDENCY_FORBIDDEN",
                detail="data residency forbids this locale/region pair")}
        policy_ref = body.get("route_policy_ref") or route.get("policy_ref")
        decision_ref = route.get("route_decision_ref") or \
            route.get("route_decision_id")
        if not policy_ref or not decision_ref:
            # 没有决策引用就没法向 S26 交代"这次按哪条规则路由的"。宁可
            # 本地拒绝，也不提交一个无法审计的付费任务。
            return {"_reject": self._reject_local(
                job, "ROUTE_UNAVAILABLE",
                detail="route resolution returned no route_decision_ref")}
        return {"capabilities": capabilities, "policy_ref": policy_ref,
                "decision_ref": decision_ref, "route": route}

    def _submit(self, tenant_id: str, job: dict, v: dict, lexicon: dict,
                route: dict, repair: dict | None) -> dict:
        command = self._generation_command(job, v, lexicon, route)
        try:
            submitted = self.submitter(tenant_id, command,
                                       route["decision_ref"])
        except ServiceError:
            raise
        except ConnectionError as exc:
            # 提交结果不明：**冻结在 WAITING_UNKNOWN**，绝不重提（E16）。
            self._transition(job, "WAITING_UNKNOWN",
                             reason_code="SUBMISSION_UNKNOWN",
                             detail=str(exc), outbox=self.outbox)
            return self._accepted(job["speech_job_id"])
        submitted = submitted if isinstance(submitted, dict) else {}
        provider_job_id = submitted.get("provider_job_id") or \
            submitted.get("generation_job_id") or submitted.get("operation_id")
        if not provider_job_id:
            # 与上一种同罪：S26 没给我们可对账的 id，就不能当它没发生。
            self._transition(job, "WAITING_UNKNOWN",
                             reason_code="SUBMISSION_UNKNOWN",
                             detail="S26 accepted the command without an id",
                             outbox=self.outbox)
            return self._accepted(job["speech_job_id"])
        attempt_id = submitted.get("attempt_id") or command["attempt_id"]
        now = _now()
        self.conn.execute(
            "UPDATE speech_job SET provider_job_id=?, attempt_id=?, "
            "route_decision_ref=?, quote_upper_bound_micro=?, updated_at=? "
            "WHERE speech_job_id=?",
            (provider_job_id, attempt_id, _ref_text(route["decision_ref"]),
             submitted.get("quote_upper_bound_micro"), now,
             job["speech_job_id"]))
        self._write_ledger(job, action=_ledger_action(job, repair), now=now)
        job = self._job(tenant_id, job["speech_job_id"])
        job = self._transition(job, "SUBMITTED", outbox=self.outbox)
        self._transition(job, "WAITING_PROVIDER", outbox=self.outbox)
        return self._accepted(job["speech_job_id"])

    def _generation_command(self, job: dict, v: dict, lexicon: dict,
                            route: dict) -> dict:
        """组 `generation-command.v2`。

        `context` 是**原样重放**平台签名的那一份（`execution_context` 列），
        不是现拼：18 个必填键里的 `actor` / `trace_id` / `home_region` 只能
        来自签名，现场拼出来的每个值都是伪造签名（S29 踩过的同一个坑）。

        `input` 只放 schema 允许的 7 个键；S31 自己的 `scope_kind` /
        `content_hash` / `source_kind` 都不进来（C3），供应商不该看见
        S31 的簿记字段。
        """
        attempt_no = int(job["business_attempt_no"])
        lexicon_input = None
        if lexicon.get("lexicon_id"):
            lexicon_input = {"id": lexicon["lexicon_id"],
                             "revision": int(lexicon["revision"])}
        return {
            "schema_version": SCHEMA_VERSION,
            "context": _loads(job["execution_context"], {}),
            "attempt_id": f"{job['speech_job_id']}:{attempt_no}",
            "variation_nonce": job["variation_nonce"],
            "idempotency_key": f"{job['idempotency_key']}:{attempt_no}",
            "modality": "speech",
            "input": {
                "text": v["text"],
                "locale": job["locale"],
                "voice_binding": {"id": job["voice_binding_id"],
                                  "revision": int(job["voice_binding_revision"])},
                "emotion": job["emotion"],
                "pronunciation_lexicon": lexicon_input,
                "output_spec": {"format": job["output_format"],
                                "sample_rate_hz": int(job["sample_rate_hz"])},
                "require_alignment": bool(job["require_alignment"]),
            },
            "route_policy_ref": route["policy_ref"],
            "template_ref": None,
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
        }

    # -------------------------------------------------------- 终止助手
    def _accepted(self, job_id: str) -> dict:
        """202 的响应体。`state` 取**库里的事实**，不是"CREATED"。

        §6.1 的示例里写的是 `CREATED`，因为那份示例描述的是"刚建出来"的
        那一瞬间；而本服务在同一个命令事务里一路走到 SUBMITTED。如果这里
        永远回 `CREATED`，`POST` 的答案与紧接着的 `GET /speech-jobs/{id}`
        就会互相矛盾，调用方会以为还来得及取消。宁可回真实状态。
        """
        row = self.conn.execute(
            "SELECT state, failure_code FROM speech_job WHERE speech_job_id=?",
            (job_id,)).fetchone()
        body = {"speech_job_id": job_id,
                "state": row["state"] if row is not None else "CREATED",
                "operation_id": _op_id(job_id), "reused": False}
        if row is not None and row["failure_code"]:
            # `RIGHTS_BLOCKED` 的 `reason_code` 决定人该去找谁（S15 恢复授权
            # vs S25 补映射）。把它藏在 GET 里会让人先猜一轮。
            body["reason_code"] = row["failure_code"]
        return body

    def _block(self, job: dict, recheck: dict, state: str) -> None:
        """权利类拒绝 → `RIGHTS_BLOCKED`。终态、无自动出口、不换声线。"""
        self._transition(
            job, state, reason_code=recheck["deny_reason"] or "RIGHTS_BLOCKED",
            detail=f"voice {job['voice_binding_id']} is no longer eligible "
                   f"(observed={recheck['observed_state']}, "
                   f"seq={recheck['observed_state_seq']})",
            outbox=self.outbox)
        self.outbox.append(new_event(
            source=SOURCE, type=LOCAL_RIGHTS_LAPSED,
            subject=f"voice/{job['voice_binding_id']}",
            tenant_id=job["tenant_id"], home_cell="speech",
            aggregate_id=job["voice_binding_id"],
            sequence=int(job["state_seq"]) + 1,
            correlation_id=job["speech_job_id"],
            data={"voice_id": job["voice_binding_id"],
                  "speech_job_id": job["speech_job_id"],
                  "deny_reason": recheck["deny_reason"],
                  "observed_state": recheck["observed_state"],
                  "state_seq": recheck["observed_state_seq"],
                  "affected_take_ids": []},
            schema_version=SCHEMA_VERSION))

    def _reject_local(self, job: dict, code: str, *, detail: str | None,
                      extra: dict | None = None) -> dict:
        """`REJECTED_LOCAL` 的**响应体**（不是内部信号）。调用方按 202 处理：
        job 确实存在，只是还没花钱就结束了。"""
        self._transition(job, "REJECTED_LOCAL", reason_code=code,
                         detail=detail, outbox=self.outbox)
        body = {"speech_job_id": job["speech_job_id"],
                "state": "REJECTED_LOCAL", "reason_code": code,
                "operation_id": _op_id(job["speech_job_id"]),
                "reused": False}
        body.update(extra or {})
        return body

    # -------------------------------------------------------- 台账
    def _write_ledger(self, job: dict, *, action: str, now: str) -> None:
        """写一行意图台账。**键是 `shot_lineage_id`**，不是 `utterance_id`：
        S39 的 `AttemptCounterKey` 按镜头血缘计数，从 `utterance_id` 派生会
        在两句台词被合并成一个镜头时把上限算多。旁白为 NULL —— 已知盲点。"""
        ctx = _loads(job["execution_context"], {})
        manifest = ctx.get("manifest_ref") or {}
        self.conn.execute(
            "INSERT INTO speech_intent_ledger (entry_id, tenant_id, "
            "speech_job_id, utterance_id, shot_lineage_id, locale, action, "
            "strategy_class, repair_action_id, voice_id, run_id, node_epoch, "
            "manifest_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["speech_job_id"],
             job["utterance_id"], job["shot_lineage_id"], job["locale"],
             action, job["strategy_class"], job["repair_action_id"],
             job["voice_binding_id"], ctx.get("run_id"),
             int(job["node_epoch_at_create"]), manifest.get("id"), now))

    # -------------------------------------------------------- 事件消费
    def on_generation_state_changed(self, event: dict) -> dict:
        """S26 `generation.job.state-changed`。

        **关联键是 `attempt_id`**（C11）：S26 的事件里没有 `provider_job_id`
        字段，它只出现在 `aggregate_id`/`subject` 里。先按 `attempt_id` 找，
        再退回到顶层 `data.provider_job_id`（兼容将来的修正）。

        三条铁律：
        - `SUBMISSION_UNKNOWN` → `WAITING_UNKNOWN` 并**冻结**；绝不重提（E16）
        - 终态 job 不再被事件推动（乱序到达的旧事件必须无害）
        - 不认识的供应商状态 → 忽略并留下原因，不猜
        """
        data = event.get("data") or {}
        attempt_id = data.get("attempt_id")
        provider_job_id = data.get("provider_job_id")
        job = self._job_by_attempt(event.get("tenant_id"), attempt_id,
                                   provider_job_id)
        if job is None:
            return {"ignored": True, "reason": "unknown_attempt"}
        if job["state"] in TERMINAL_STATES:
            # 乱序/重复：`READY` 之后再到 `RUNNING` 不能把它拉回去。
            return {"ignored": True, "reason": "job_terminal",
                    "state": job["state"]}

        state = data.get("state")
        reason = data.get("reason_code")
        if state == "SUBMISSION_UNKNOWN":
            if job["state"] != "WAITING_UNKNOWN":
                self._transition(job, "WAITING_UNKNOWN",
                                 reason_code="SUBMISSION_UNKNOWN",
                                 detail=reason, outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"],
                    "state": "WAITING_UNKNOWN", "frozen": True}
        if state in ("QUEUED", "RUNNING", "PROVIDER_SUCCEEDED",
                     "PROVIDER_FAILED", "CANCELED", "ARCHIVED",
                     "REJECTED_BUDGET", "REJECTED_QUOTA"):
            return self._apply_provider_state(job, state, data, reason)
        # CREATED/RESERVING/ADMITTED/DISPATCH_AUTHORIZED/SUBMITTING/INGESTING/
        # RECONCILING：S26 的内部阶段，S31 不跟着切状态。
        return {"ignored": True,
                "reason": f"unmapped_provider_state:{state}"}

    def _job_by_attempt(self, tenant_id, attempt_id,
                        provider_job_id) -> dict | None:
        if not tenant_id:
            return None
        if attempt_id:
            row = self.conn.execute(
                "SELECT * FROM speech_job WHERE tenant_id=? AND attempt_id=?",
                (tenant_id, attempt_id)).fetchone()
            if row is not None:
                return dict(row)
        if provider_job_id:
            row = self.conn.execute(
                "SELECT * FROM speech_job WHERE tenant_id=? AND "
                "provider_job_id=?", (tenant_id, provider_job_id)).fetchone()
            if row is not None:
                return dict(row)
        return None

    def _apply_provider_state(self, job: dict, state: str, data: dict,
                              reason: str | None) -> dict:
        current = job["state"]
        if state in ("QUEUED", "RUNNING"):
            if current == "SUBMITTED":
                self._transition(job, "WAITING_PROVIDER", outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"],
                    "state": "WAITING_PROVIDER"}
        if state == "PROVIDER_SUCCEEDED":
            # 只表示供应商跑完了；字节还没归档，所以**不进 READY**。
            if current in ("SUBMITTED", "WAITING_PROVIDER", "WAITING_UNKNOWN"):
                self._transition(job, "INGESTING", outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "INGESTING"}
        if state == "ARCHIVED":
            return self._on_archived(job, data, reason)
        if state in ("PROVIDER_FAILED", "CANCELED"):
            # F18/F19：透传失败原因，**不换渠道重试**。S31 没有"另一个供应商"
            # 这个概念——换渠道等于换声音实现，属于 S24/S25 的决定。
            code = _map_provider_failure(state, reason)
            self._transition(job, "FAILED", reason_code=code,
                             detail=reason, outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": code}
        if state == "REJECTED_BUDGET":
            self._transition(job, "FAILED", reason_code="BUDGET",
                             detail=reason, outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": "BUDGET"}
        if state == "REJECTED_QUOTA":
            self._transition(job, "FAILED", reason_code="QUOTA",
                             detail=reason, outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": "QUOTA"}
        return {"ignored": True, "reason": f"unmapped_provider_state:{state}"}

    def _on_archived(self, job: dict, data: dict,
                     reason: str | None = None) -> dict:
        """S26 说归档完了。若事件里已带齐归档事实就直接收尾；否则进
        `INGESTING` 等 S28。**不编时长**（F14）。

        `reason_code='INGEST_FAILED'` 是 S26 §8.3 第 5 步：重归档全部失败，
        成片拿不到了。那是 `INGESTING → FAILED`（§3.1 的"重试耗尽"那一行），
        缺口必须可见（E23）—— 既不能假装 READY，也不能无限期挂着，更不能
        为拿不到成片而再付一次钱。只接 `INGESTING`：`WAITING_UNKNOWN` 的
        出口在 §3.1 里只有 `RECONCILED_*` 两条，替它多加一条边等于自造
        状态机。
        """
        artifact = data.get("artifact_ref")
        duration = data.get("duration_ms",
                            data.get("observed_duration_ms"))
        if reason == "INGEST_FAILED" and job["state"] == "INGESTING":
            self._transition(
                job, "FAILED", reason_code="ARTIFACT_UNAVAILABLE",
                detail="S26 reports the archive could not be produced after "
                       "its retries; the gap is recorded rather than "
                       "re-synthesising (E23)", outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": "ARTIFACT_UNAVAILABLE",
                    "resubmitted": False, "gap_visible": True}
        if artifact and isinstance(duration, int) and duration > 0:
            if job["state"] == "WAITING_UNKNOWN":
                self.conn.execute(
                    "UPDATE speech_job SET reconciled=1 WHERE speech_job_id=?",
                    (job["speech_job_id"],))
                job = self._job(job["tenant_id"], job["speech_job_id"])
                job = self._transition(job, "INGESTING",
                                       reason_code="RECONCILED",
                                       outbox=self.outbox)
            return self._finish_ingest(job, artifact, duration,
                                       data.get("container"),
                                       data.get("sample_rate_hz"))
        if job["state"] == "WAITING_UNKNOWN":
            self.conn.execute(
                "UPDATE speech_job SET reconciled=1 WHERE speech_job_id=?",
                (job["speech_job_id"],))
            job = self._job(job["tenant_id"], job["speech_job_id"])
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
        return {"speech_job_id": job["speech_job_id"], "state": job["state"],
                "waiting": "artifact.ingested"}

    def on_artifact_ingested(self, event: dict) -> dict:
        """S28 `artifact.ingested` → `INGESTING → READY`。

        `duration_ms` 是**可选**的（C2）。缺了它：留在 `INGESTING` 并告警，
        不退化成假 `READY`（F14）；为 0 或不可读：`FAILED(INVALID_AUDIO)`
        （F17）。两者都是"钱已经花了"，区别在于有没有可以交付的字节。
        """
        data = event.get("data") or {}
        artifact = data.get("artifact_ref") or {}
        source = data.get("source") or {}
        tenant = event.get("tenant_id")
        job = self._job_for_artifact(tenant, artifact, source, data)
        if job is None:
            return {"ignored": True, "reason": "unknown_artifact"}
        if job["state"] not in ("INGESTING", "WAITING_UNKNOWN"):
            return {"ignored": True, "reason": "job_not_ingesting",
                    "state": job["state"]}
        duration = data.get("duration_ms",
                            data.get("observed_duration_ms"))
        if duration is None:
            # F14：阻塞。返回 waiting 让调用方/告警看得见，不静默变成 READY。
            return {"speech_job_id": job["speech_job_id"],
                    "state": job["state"], "waiting": "duration_ms",
                    "warning": "artifact.ingested carried no duration_ms"}
        if not isinstance(duration, int) or isinstance(duration, bool) or \
                duration <= 0:
            self._transition(job, "FAILED", reason_code="INVALID_AUDIO",
                             detail=f"duration_ms={duration!r} is not a "
                                    f"positive integer",
                             outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": "INVALID_AUDIO"}
        if source.get("content_hash") and artifact.get("content_hash") and \
                source["content_hash"] != artifact["content_hash"]:
            self._transition(job, "FAILED", reason_code="HASH_MISMATCH",
                             detail="provider-reported hash differs from the "
                                    "archived artifact hash",
                             outbox=self.outbox)
            return {"speech_job_id": job["speech_job_id"], "state": "FAILED",
                    "reason_code": "HASH_MISMATCH"}
        if job["state"] == "WAITING_UNKNOWN":
            self.conn.execute(
                "UPDATE speech_job SET reconciled=1 WHERE speech_job_id=?",
                (job["speech_job_id"],))
            job = self._job(job["tenant_id"], job["speech_job_id"])
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
        return self._finish_ingest(job, artifact, duration,
                                   data.get("container"),
                                   data.get("sample_rate_hz"))

    def _job_for_artifact(self, tenant, artifact: dict, source: dict,
                          data: dict) -> dict | None:
        if not tenant:
            return None
        job_id = data.get("speech_job_id")
        if job_id:
            row = self.conn.execute(
                "SELECT * FROM speech_job WHERE tenant_id=? AND "
                "speech_job_id=?", (tenant, job_id)).fetchone()
            return None if row is None else dict(row)
        for pid in (source.get("provider_job_id"), source.get("generation_job_id")):
            if pid:
                row = self.conn.execute(
                    "SELECT * FROM speech_job WHERE tenant_id=? AND "
                    "provider_job_id=?", (tenant, pid)).fetchone()
                if row is not None:
                    return dict(row)
        attempt = source.get("attempt_id")
        if attempt:
            row = self.conn.execute(
                "SELECT * FROM speech_job WHERE tenant_id=? AND attempt_id=?",
                (tenant, attempt)).fetchone()
            if row is not None:
                return dict(row)
        return None

    def _finish_ingest(self, job: dict, artifact: dict, duration_ms: int,
                       container, sample_rate) -> dict:
        """`INGESTING → READY` 的全部副作用，**一个事务**：

        1. 时点 4 的归档后复查（钱已花，但产物能不能用还没定）
        2. 写 `VoiceTake`（void 或 usable）
        3. 只有 usable 才 CAS 推进检查点并发 `ready`

        顺序不能换。先发 `ready` 再复查，就是给下游一条"可用"的假消息。
        """
        tenant = job["tenant_id"]
        now = _now()
        recheck = self._recheck(
            tenant_id=tenant, voice_id=job["voice_binding_id"],
            voice_revision=int(job["voice_binding_revision"]),
            requested_use=self._requested_use(job["locale"]),
            check_point=POST_INGEST, job_id=job["speech_job_id"])

        usable = bool(recheck["eligible"])
        if usable:
            # 事务内再读一次：同一事务里刚写的那行就是权威，不用内存里的
            # 局部变量代替它（E39：能自证的才写进产物）。
            usable = self._recheck_row_eligible(recheck)

        take_id = _new_id("take")
        self.conn.execute(
            "INSERT INTO voice_take (voice_take_id, tenant_id, speech_job_id, "
            "utterance_id, locale, shot_lineage_id, artifact_ref, "
            "observed_duration_ms, container_format, sample_rate_hz, "
            "voice_binding_id, voice_binding_revision, lex, "
            "input_fingerprint, provider_job_id, attempt_id, "
            "alignment_report_ref, rights_state, rights_state_seq, "
            "repair_action_id, strategy_class, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (take_id, tenant, job["speech_job_id"], job["utterance_id"],
             job["locale"], job["shot_lineage_id"], _jcs(artifact),
             int(duration_ms),
             str(container or _format_from(job["output_format"])),
             int(sample_rate or job["sample_rate_hz"]),
             job["voice_binding_id"], int(job["voice_binding_revision"]),
             _jcs(self._lexicon_snapshot(job)),
             job["input_fingerprint"], job["provider_job_id"] or "",
             job["attempt_id"] or "", None,
             "usable" if usable else "void",
             None if usable else recheck["observed_state_seq"],
             job["repair_action_id"], job["strategy_class"], "READY", now))

        if not usable:
            self._transition(
                job, "RIGHTS_BLOCKED",
                reason_code=recheck["deny_reason"] or "AUTHORIZATION_REVOKED",
                detail="authorization lapsed between dispatch and ingest; the "
                       "take is registered but marked void and will never "
                       "advance a checkpoint",
                outbox=self.outbox)
            self.outbox.append(new_event(
                source=SOURCE, type=LOCAL_RIGHTS_LAPSED,
                subject=f"voice/{job['voice_binding_id']}",
                tenant_id=tenant, home_cell="speech",
                aggregate_id=job["voice_binding_id"],
                sequence=int(job["state_seq"]) + 1,
                correlation_id=job["speech_job_id"],
                data={"voice_id": job["voice_binding_id"],
                      "speech_job_id": job["speech_job_id"],
                      "deny_reason": recheck["deny_reason"],
                      "observed_state": recheck["observed_state"],
                      "state_seq": recheck["observed_state_seq"],
                      "affected_take_ids": [take_id]},
                schema_version=SCHEMA_VERSION))
            return {"speech_job_id": job["speech_job_id"],
                    "state": "RIGHTS_BLOCKED", "voice_take_id": take_id,
                    "rights_state": "void", "ready_published": False}

        final_state = "RECONCILED_READY" if job["reconciled"] else "READY"
        checkpoint = self._advance_checkpoint(job, take_id, now)
        job2 = self._job(tenant, job["speech_job_id"])
        job2 = self._transition(job2, final_state, outbox=self.outbox)
        self._publish_ready(job2, take_id, artifact, duration_ms, checkpoint,
                            now)
        return {"speech_job_id": job["speech_job_id"], "state": final_state,
                "voice_take_id": take_id, "rights_state": "usable",
                "checkpoint_seq": checkpoint["checkpoint_seq"],
                "ready_published": True}

    @staticmethod
    def _recheck_row_eligible(recheck: dict) -> bool:
        return bool(recheck.get("eligible")) and \
            recheck.get("observed_state") == "granted"

    def _lexicon_snapshot(self, job: dict) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM lexicon_binding_ref WHERE lexicon_binding_ref_id=?",
            (job["lexicon_binding_ref_id"],)).fetchone()) or {}
        return {"lexicon_id": row.get("lexicon_id"),
                "revision": row.get("lexicon_revision"),
                "content_hash": row.get("content_hash"),
                "source_kind": row.get("source_kind"),
                "scope_kind": row.get("scope_kind")}

    def _advance_checkpoint(self, job: dict, take_id: str, now: str) -> dict:
        """CAS 推进检查点。

        `shot_lineage_id IS NULL`（旁白）不落检查点：主键里它是 NOT NULL，
        硬塞一个占位值会让旁白与镜头共用一行。旁白由 `utterance_id` 定位，
        这是 §8.2 明说的已知盲点。
        """
        lineage = job["shot_lineage_id"]
        if lineage is None:
            # 旁白没有镜头。仍要返回一个**结构合法**的检查点（seq >= 1），
            # 否则 `production.speech.ready` 过不了 schema；但这个 take 不进
            # `speech_checkpoint` 表，所以它不是任何镜头的基线。这是 §8.2
            # 明写的盲点，不是把它藏起来的理由。
            return {"shot_lineage_id": None, "utterance_id": job["utterance_id"],
                    "locale": job["locale"], "voice_take_id": take_id,
                    "checkpoint_seq": 1, "from": None,
                    "skipped": True}
        row = _row(self.conn.execute(
            "SELECT * FROM speech_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND utterance_id=? AND locale=?",
            (job["tenant_id"], lineage, job["utterance_id"],
             job["locale"])).fetchone())
        previous_take = row["voice_take_id"] if row else None
        if row is None:
            self.conn.execute(
                "INSERT INTO speech_checkpoint (tenant_id, shot_lineage_id, "
                "utterance_id, locale, voice_take_id, checkpoint_seq, "
                "expected_revision, updated_by_job, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job["tenant_id"], lineage, job["utterance_id"], job["locale"],
                 take_id, 1, 0, job["speech_job_id"], now))
            seq = 1
        else:
            old_seq = int(row["checkpoint_seq"])
            cur = self.conn.execute(
                "UPDATE speech_checkpoint SET voice_take_id=?, "
                "checkpoint_seq=?, expected_revision=?, updated_by_job=?, "
                "updated_at=? WHERE tenant_id=? AND shot_lineage_id=? AND "
                "utterance_id=? AND locale=? AND checkpoint_seq=?",
                (take_id, old_seq + 1, old_seq, job["speech_job_id"], now,
                 job["tenant_id"], lineage, job["utterance_id"], job["locale"],
                 old_seq))
            if cur.rowcount != 1:
                # 别人抢先推进了。整笔回滚，让调用方重放这条事件——覆盖会
                # 悄悄丢掉另一个 take 的推进。
                raise ServiceError(409, "CHECKPOINT_CAS_FAILED",
                                   "checkpoint advanced concurrently",
                                   retryable=True)
            seq = old_seq + 1
        return {"shot_lineage_id": lineage, "utterance_id": job["utterance_id"],
                "locale": job["locale"], "voice_take_id": take_id,
                "checkpoint_seq": seq, "from": previous_take, "skipped": False}

    def _publish_ready(self, job: dict, take_id: str, artifact: dict,
                       duration_ms: int, checkpoint: dict, now: str) -> None:
        """`production.speech.ready`，17 个必填字段一个不少。

        `dialogue_span_ms` 恒为 `null` + `dialogue_span_source='pending_s37'`：
        S31 只有容器实测时长，没有台词起止。不猜（§4.5）。

        `lexicon_ref` 在 `source_kind='none'` 时必须结构化合法（C10）：用
        `LexiconBindingRef` 的 id 配 revision 1，`source_kind='none'` ——它是
        一个**合格的占位**，不是编造出来的词典。
        """
        lex = self._lexicon_snapshot(job)
        lbr_id = job["lexicon_binding_ref_id"]
        lexicon_ref = {
            "id": lex["lexicon_id"] or lbr_id,
            "revision": int(lex["revision"]) if lex["revision"] else 1,
            "source_kind": lex["source_kind"] or "none",
        }
        aggregate = job["shot_lineage_id"] or job["utterance_id"]
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_SPEECH_READY,
            subject=f"speech/{job['speech_job_id']}",
            tenant_id=job["tenant_id"], home_cell="speech",
            aggregate_id=aggregate, sequence=int(job["state_seq"]),
            correlation_id=job["speech_job_id"],
            data={
                "schema_version": SCHEMA_VERSION,
                "speech_job_id": job["speech_job_id"],
                "voice_take_id": take_id,
                "utterance_id": job["utterance_id"],
                "utterance_index": int(job["utterance_index"]),
                "shot_lineage_id": job["shot_lineage_id"],
                "locale": job["locale"],
                "text_hash": _text_hash_of(self, job),
                "artifact_ref": artifact,
                "audio_duration_ms": int(duration_ms),
                "dialogue_span_ms": None,
                "dialogue_span_source": "pending_s37",
                "voice_binding": {"id": job["voice_binding_id"],
                                  "revision": int(job["voice_binding_revision"])},
                "lexicon_ref": lexicon_ref,
                "checkpoint": {"from": checkpoint["from"], "to": take_id,
                               "checkpoint_seq": int(checkpoint["checkpoint_seq"])},
                "require_alignment": bool(job["require_alignment"]),
                "rights_state": "usable",
            },
            schema_version=SCHEMA_VERSION))

    # -------------------------------------------------------- 其他消费
    def on_voice_authorization_changed(self, event: dict) -> dict:
        """S15 授权变更。**只收紧，不放行。**

        - `revoked` / `expired` / `suspended`：该音色所有 usable take 标 void，
          在途 job 冻到 `RIGHTS_BLOCKED` 并请 S26 取消（不承诺退款）
        - `granted`：**什么都不做**。把 `RIGHTS_BLOCKED` 拉回来需要一次新的、
          带新 `state_seq` 的复查，而那只由一次新请求触发（§4.2 时点 5）。

        一个 take 被标 void 之后不会被"恢复"：`rights_state_seq` 只增不减，
        而 `UPDATE ... WHERE rights_state <> 'void'` 让第二次事件成为 no-op。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        voice_id = data.get("voice_id")
        to_state = data.get("to_state")
        seq = data.get("state_seq")
        if not tenant or not voice_id:
            return {"ignored": True, "reason": "missing_voice_id"}
        if to_state == "granted":
            return {"ignored": True, "reason": "grants_never_clear_a_block"}
        if to_state not in ("revoked", "expired", "suspended"):
            return {"ignored": True, "reason": f"unmapped_state:{to_state}"}

        now = _now()
        voided = self.conn.execute(
            "UPDATE voice_take SET rights_state='void', rights_state_seq=? "
            "WHERE tenant_id=? AND voice_binding_id=? AND rights_state<>'void'",
            (seq, tenant, voice_id)).rowcount
        frozen = []
        rows = self.conn.execute(
            "SELECT * FROM speech_job WHERE tenant_id=? AND "
            "voice_binding_id=? AND state NOT IN ('READY','REJECTED_LOCAL',"
            "'RIGHTS_BLOCKED','FAILED','RECONCILED_FAILED','CANCELED',"
            "'RECONCILED_READY')", (tenant, voice_id)).fetchall()
        for raw in rows:
            job = dict(raw)
            if job["state"] in TERMINAL_STATES:
                continue
            if job["provider_job_id"]:
                # 在途：先请 S26 取消。取消不了也只是"结果丢弃"，不删字节。
                try:
                    self.canceler(tenant, job["provider_job_id"],
                                  "VOICE_AUTHORIZATION_REVOKED")
                    detail = "S26 cancel requested"
                except (ServiceError, ConnectionError) as exc:
                    detail = f"S26 cancel failed: {getattr(exc, 'code', exc)}"
            else:
                detail = "not yet dispatched"
            self._transition(
                job, "RIGHTS_BLOCKED",
                reason_code=_deny_for(to_state),
                detail=f"authorization changed to {to_state} "
                       f"(seq={seq}); {detail}",
                outbox=self.outbox)
            frozen.append(job["speech_job_id"])
        if voided or frozen:
            self.outbox.append(new_event(
                source=SOURCE, type=LOCAL_RIGHTS_LAPSED,
                subject=f"voice/{voice_id}", tenant_id=tenant,
                home_cell="speech", aggregate_id=voice_id,
                sequence=int(seq or 1),
                correlation_id=str(event.get("event_id") or voice_id),
                data={"voice_id": voice_id, "to_state": to_state,
                      "state_seq": seq, "affected_take_count": voided,
                      "affected_job_ids": frozen},
                schema_version=SCHEMA_VERSION))
        return {"voice_id": voice_id, "voided_takes": voided,
                "frozen_jobs": frozen}

    def on_alignment_completed(self, event: dict) -> dict:
        """S37 对齐完成：**只挂引用，不改状态**。

        `alignment-completed.v2` 里没有 `report_ref` 字段，只有 `report_id` +
        `report_generation`，所以引用是拼出来的 `{service_id:"S37", id,
        revision}`。数值一律不复制：复制就是第二个权威，且 S37 换
        `asr_model_snapshot` 重跑后会静默过期（§4.5）。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        take_id = data.get("voice_take_id")
        report_id = data.get("report_id")
        generation = data.get("report_generation")
        if not tenant or not report_id:
            return {"ignored": True, "reason": "missing_report_id"}
        ref = _jcs({"service_id": "S37", "id": report_id,
                    "revision": int(generation or 1)})
        if take_id:
            rowcount = self.conn.execute(
                "UPDATE voice_take SET alignment_report_ref=? WHERE tenant_id=? "
                "AND voice_take_id=?", (ref, tenant, take_id)).rowcount
            return {"voice_take_id": take_id, "updated": bool(rowcount)}
        rowcount = self.conn.execute(
            "UPDATE voice_take SET alignment_report_ref=? WHERE tenant_id=? AND "
            "provider_job_id=? AND artifact_ref LIKE ?",
            (ref, tenant, str(data.get("provider_job_id") or ""),
             "%" + str(data.get("artifact_sha256") or "") + "%")).rowcount
        return {"updated": bool(rowcount),
                "ignored": False if rowcount else True,
                "reason": None if rowcount else "no_matching_take"}

    def on_timeline_revision(self, event: dict) -> dict:
        """S41 时间线：镜头被删 → take 变 `SUPERSEDED`（**不是 void**）。

        区别是要紧的：void 意味着"声音不能用"（法律问题），SUPERSEDED 意味着
        "这段剪辑不用了"（创作问题）。混成一个词，审计就分不清哪次是撤销。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        lineage = data.get("shot_lineage_id")
        if not tenant or not lineage:
            return {"ignored": True, "reason": "missing_shot_lineage_id"}
        if data.get("event_kind") not in ("shot_removed", "shot_deleted") and \
                data.get("change") not in ("removed", "deleted"):
            return {"ignored": True, "reason": "not_a_removal"}
        rowcount = self.conn.execute(
            "UPDATE voice_take SET state='SUPERSEDED' WHERE tenant_id=? AND "
            "shot_lineage_id=? AND state='READY'",
            (tenant, lineage)).rowcount
        return {"shot_lineage_id": lineage, "superseded": rowcount}

    def on_repair_planned(self, event: dict) -> dict:
        """S39 修复计划：**只读**。记录"有 `re_synthesize_speech` 在路上"，
        绝不据此自动建 job —— 那会让 S39 变成 S31 的第二权威。"""
        data = event.get("data") or {}
        action = data.get("top_action")
        return {"observed": True, "top_action": action,
                "would_execute": action in OWNED_ACTIONS,
                "note": "S31 never creates a job from a repair plan; the "
                        "caller must POST /speech-jobs with the repair_action"}

    def on_manifest_committed(self, event: dict) -> dict:
        """S08 清单提交：只读投影。`node_epoch` 变化**不改本地状态**——
        那是 run/branch 级别的事，与单个 job 的 `node_epoch_at_create` 无关。"""
        return {"observed": True, "ignored": True,
                "reason": "read_only_projection"}

    # -------------------------------------------------------- 查询
    def get_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        take = _row(self.conn.execute(
            "SELECT * FROM voice_take WHERE tenant_id=? AND speech_job_id=? "
            "ORDER BY created_at DESC", (tenant_id, job_id)).fetchone())
        vbr = _row(self.conn.execute(
            "SELECT * FROM voice_binding_ref WHERE voice_binding_ref_id=?",
            (job["voice_binding_ref_id"],)).fetchone()) or {}
        lex = self._lexicon_snapshot(job)
        checkpoint = None
        if job["shot_lineage_id"]:
            checkpoint = _row(self.conn.execute(
                "SELECT * FROM speech_checkpoint WHERE tenant_id=? AND "
                "shot_lineage_id=? AND utterance_id=? AND locale=?",
                (tenant_id, job["shot_lineage_id"], job["utterance_id"],
                 job["locale"])).fetchone())
        rechecks = [dict(r) for r in self.conn.execute(
            "SELECT check_point, observed_state, observed_state_seq, eligible, "
            "deny_reason, checked_at FROM rights_recheck_log WHERE tenant_id=? "
            "AND speech_job_id=? ORDER BY checked_at", (tenant_id, job_id))]
        history = [dict(r) for r in self.conn.execute(
            "SELECT seq, from_state, to_state, reason_code, detail, at FROM "
            "job_transition WHERE speech_job_id=? ORDER BY seq", (job_id,))]
        return {
            "speech_job_id": job["speech_job_id"],
            "source_revision": job["source_revision"],
            "state": job["state"],
            "state_seq": int(job["state_seq"]),
            "business_attempt_no": int(job["business_attempt_no"]),
            "provider_job_id": job["provider_job_id"],
            "failure_code": job["failure_code"],
            "utterance": {"utterance_id": job["utterance_id"],
                          "text_revision": int(job["text_revision"]),
                          "locale": job["locale"],
                          "text_hash": _text_hash_of(self, job)},
            "voice_binding": {
                "id": job["voice_binding_id"],
                "revision": int(job["voice_binding_revision"]),
                "authorization_state_at_submit": vbr.get("authorization_state"),
                "authorization_state_seq_at_submit":
                    vbr.get("authorization_state_seq"),
            },
            "lexicon": {"lexicon_id": lex["lexicon_id"],
                        "revision": lex["revision"],
                        "content_hash": lex["content_hash"],
                        "source_kind": lex["source_kind"],
                        "scope_kind": lex["scope_kind"]},
            "input_fingerprint": job["input_fingerprint"],
            "take": None if take is None else {
                "voice_take_id": take["voice_take_id"],
                "artifact_ref": _loads(take["artifact_ref"], None),
                "observed_duration_ms": int(take["observed_duration_ms"]),
                "rights_state": take["rights_state"],
                "state": take["state"],
                "alignment_report_ref": _loads(
                    take["alignment_report_ref"], None),
            },
            "checkpoint": None if checkpoint is None else {
                "shot_lineage_id": checkpoint["shot_lineage_id"],
                "utterance_id": checkpoint["utterance_id"],
                "locale": checkpoint["locale"],
                "voice_take_id": checkpoint["voice_take_id"],
                "checkpoint_seq": int(checkpoint["checkpoint_seq"]),
            },
            "frozen": job["state"] in ("WAITING_UNKNOWN", "RIGHTS_BLOCKED"),
            "must_reconcile_before_resubmit": job["state"] == "WAITING_UNKNOWN",
            "rights_rechecks": rechecks,
            "history": history,
        }

    def list_jobs(self, tenant_id: str, *, shot_lineage_id=None,
                  utterance_id=None, locale=None, state=None) -> dict:
        sql = "SELECT * FROM speech_job WHERE tenant_id=?"
        args: list = [tenant_id]
        for column, value in (("shot_lineage_id", shot_lineage_id),
                              ("utterance_id", utterance_id),
                              ("locale", locale), ("state", state)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        sql += " ORDER BY created_at DESC LIMIT 200"
        return {"items": [dict(r) for r in self.conn.execute(sql, args)]}

    def get_shot_checkpoint(self, tenant_id: str, shot_lineage_id: str,
                            locale: str | None = None) -> dict:
        """S30 / S41 / S32 的主读口：该镜全部台词当前的基线 take。"""
        sql = ("SELECT * FROM speech_checkpoint WHERE tenant_id=? AND "
               "shot_lineage_id=?")
        args: list = [tenant_id, shot_lineage_id]
        if locale:
            sql += " AND locale=?"
            args.append(locale)
        rows = [dict(r) for r in self.conn.execute(
            sql + " ORDER BY utterance_id", args)]
        for row in rows:
            take = _row(self.conn.execute(
                "SELECT * FROM voice_take WHERE voice_take_id=?",
                (row["voice_take_id"],)).fetchone())
            # 检查点里的 take 若已 void，**仍然显示**但标出来。把它藏起来会让
            # 下游以为"这句没有音频"，而实际是"有但不可用"，两者处置不同。
            row["take_rights_state"] = take["rights_state"] if take else None
        return {"shot_lineage_id": shot_lineage_id, "locale": locale,
                "items": rows}

    def get_take(self, tenant_id: str, take_id: str) -> dict:
        take = _row(self.conn.execute(
            "SELECT * FROM voice_take WHERE tenant_id=? AND voice_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "VOICE_TAKE_NOT_FOUND", f"no take {take_id}")
        rechecks = [dict(r) for r in self.conn.execute(
            "SELECT check_point, observed_state, observed_state_seq, eligible, "
            "deny_reason, checked_at FROM rights_recheck_log WHERE tenant_id=? "
            "AND speech_job_id=? ORDER BY checked_at",
            (tenant_id, take["speech_job_id"]))]
        return {"voice_take_id": take["voice_take_id"],
                "speech_job_id": take["speech_job_id"],
                "utterance_id": take["utterance_id"],
                "locale": take["locale"],
                "shot_lineage_id": take["shot_lineage_id"],
                "artifact_ref": _loads(take["artifact_ref"], None),
                "observed_duration_ms": int(take["observed_duration_ms"]),
                "container_format": take["container_format"],
                "sample_rate_hz": int(take["sample_rate_hz"]),
                "voice_binding": {"id": take["voice_binding_id"],
                                  "revision": int(take["voice_binding_revision"])},
                "lexicon": _loads(take["lex"], None),
                "input_fingerprint": take["input_fingerprint"],
                "rights_state": take["rights_state"],
                "rights_state_seq": take["rights_state_seq"],
                "state": take["state"],
                "alignment_report_ref": _loads(take["alignment_report_ref"], None),
                "rights_rechecks": rechecks}

    def reuse_eligibility(self, tenant_id: str, take_id: str) -> dict:
        """§6.3：能否被指纹复用。四个条件逐条给结论，不只给一个布尔。"""
        take = _row(self.conn.execute(
            "SELECT * FROM voice_take WHERE tenant_id=? AND voice_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "VOICE_TAKE_NOT_FOUND", f"no take {take_id}")
        usable = take["rights_state"] == "usable"
        state_ok = take["state"] in ("READY", "ACCEPTED")
        not_repair = take["repair_action_id"] is None
        artifact = _loads(take["artifact_ref"], {})
        try:
            referenceable = bool(self.artifact_check(tenant_id, artifact))
        except (ServiceError, ConnectionError):
            referenceable = False
        return {"voice_take_id": take_id,
                "reusable": bool(usable and state_ok and not_repair and
                                 referenceable),
                "checks": {"rights_state_usable": usable,
                           "take_state_ready_or_accepted": state_ok,
                           "not_a_repair_take": not_repair,
                           "artifact_referenceable": referenceable}}

    # -------------------------------------------------------- 命令
    def cancel_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        if job["state"] in TERMINAL_STATES:
            raise ServiceError(409, "JOB_NOT_CANCELABLE",
                               f"job is {job['state']}",
                               details={"state": job["state"]})
        detail = None
        if job["provider_job_id"]:
            try:
                self.canceler(tenant_id, job["provider_job_id"], "USER_CANCEL")
            except (ServiceError, ConnectionError) as exc:
                # 取消没确认：转 `WAITING_UNKNOWN` 而不是假装取消了。用户看到的
                # 必须与真实在途状态一致，否则下一次轮询会对不上。
                self._transition(job, "WAITING_UNKNOWN",
                                 reason_code="CANCEL_UNCONFIRMED",
                                 detail=str(exc), outbox=self.outbox)
                return {"speech_job_id": job_id, "state": "WAITING_UNKNOWN",
                        "canceled": False,
                        "must_reconcile_before_resubmit": True}
            detail = "S26 accepted the cancel"
        self._transition(job, "CANCELED", reason_code="USER_CANCEL",
                         detail=detail, outbox=self.outbox)
        return {"speech_job_id": job_id, "state": "CANCELED", "canceled": True}

    def retry_job(self, tenant_id: str, job_id: str) -> dict:
        """只有 `{FAILED, RECONCILED_FAILED}` 能重试。

        `WAITING_UNKNOWN` 与 `RIGHTS_BLOCKED` 返回 **409**：前者要先对账
        （E16），后者要人改授权（E30）。用 409 而不是 422，客户端才能把
        "不可重试"与"请求写错了"分开。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] == "RIGHTS_BLOCKED":
            raise ServiceError(
                409, "JOB_NOT_RETRYABLE",
                "this job is blocked on voice authorization; retrying would "
                "spend money on a voice that may not be used. A human must "
                "resolve the authorization, or a new voice_id must be used",
                details={"state": "RIGHTS_BLOCKED"})
        if job["state"] == "WAITING_UNKNOWN":
            raise ServiceError(
                409, "MUST_RECONCILE_FIRST",
                "the previous submission's outcome is unknown; resubmitting "
                "blind would double-charge. Reconcile with S26 first",
                details={"state": "WAITING_UNKNOWN"})
        if job["state"] not in RETRYABLE_STATES:
            raise ServiceError(409, "JOB_NOT_RETRYABLE",
                               f"job is {job['state']}",
                               details={"state": job["state"]})
        # 新的业务尝试号：台账与 S26 的幂等键都随之前进，这样"重试"不会
        # 被 S26 当成重复提交吞掉。
        nxt = int(job["business_attempt_no"]) + 1
        self.conn.execute(
            "UPDATE speech_job SET business_attempt_no=?, state='PREPARING', "
            "state_seq=state_seq+1, failure_code=NULL, failure_detail=NULL, "
            "updated_at=? WHERE speech_job_id=?",
            (nxt, _now(), job_id))
        self.conn.execute(
            "INSERT INTO job_transition (speech_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job_id, int(job["state_seq"]) + 1, job["state"], "PREPARING",
             "RETRY", f"business_attempt_no={nxt}", _now()))
        return {"speech_job_id": job_id, "state": "PREPARING",
                "business_attempt_no": nxt}

    def reconcile_unknown(self, tenant_id: str, job_id: str,
                          provider_outcome: dict | None) -> dict:
        """对账。**`provider_outcome is None` = 还查不到 → 什么都不做。**

        这个函数里**没有任何**提交调用。E16 的整个要点就是：不确定的时候
        不动钱。只有当 S26 明确说"没收到"（`accepted=False`）才允许
        `RECONCILED_FAILED`，之后由 S39/人决定要不要新 attempt。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "WAITING_UNKNOWN":
            raise ServiceError(409, "JOB_NOT_UNKNOWN", f"job is {job['state']}",
                               details={"state": job["state"]})
        if provider_outcome is None:
            return {"speech_job_id": job_id, "state": "WAITING_UNKNOWN",
                    "reconciled": False, "resubmitted": False,
                    "note": "S26 still has no answer; freezing costs nothing "
                            "and double-charging costs a lot"}
        accepted = provider_outcome.get("accepted")
        self.conn.execute(
            "UPDATE speech_job SET reconciled=1 WHERE speech_job_id=?",
            (job_id,))
        job = self._job(tenant_id, job_id)
        if accepted is True:
            # 收单了：等它的归档。**不重提**。
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
            return {"speech_job_id": job_id, "state": "INGESTING",
                    "reconciled": True, "resubmitted": False}
        job = self._transition(job, "RECONCILED_FAILED",
                               reason_code=provider_outcome.get("reason_code")
                               or "NOT_ACCEPTED",
                               detail="S26 confirms the command was not "
                                      "accepted; no charge stands",
                               outbox=self.outbox)
        return {"speech_job_id": job_id, "state": "RECONCILED_FAILED",
                "reconciled": True, "resubmitted": False}

    # -------------------------------------------------------- 巡检
    def sweep_deadlines(self, tenant_id: str | None = None) -> dict:
        """T31-30 / E16：`WAITING_UNKNOWN` 熬到死线 → `FAILED(DEADLINE)`。

        **这个方法里没有提交调用，也不应该有。** 它的全部理由是：一个结果
        不明的付费提交，既不能重提（可能已经收单，重提就是双倍扣费），也
        不能永远挂着（调用方会一直等一个不会来的结果）。死线是唯一一个
        "什么都不用知道就能结束"的出口 —— 到点就按超时失败，让 S39 决定
        要不要开新 attempt。

        只扫 `WAITING_UNKNOWN`。其他状态各有自己的出口：`INGESTING` 等
        S28，`SUBMITTED`/`WAITING_PROVIDER` 等 S26 事件，终态不动。
        """
        sql = "SELECT * FROM speech_job WHERE state='WAITING_UNKNOWN'"
        args: list = []
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(tenant_id)
        failed: list[str] = []
        for raw in self.conn.execute(sql, args).fetchall():
            job = dict(raw)
            # 在 Python 里比，不在 SQL 里比：deadline 是带偏移的 ISO-8601，
            # 字符串比较会把 `+08:00` 的早死线看成未到（见 `_parse_ts`）。
            if not _deadline_passed(job["deadline"]):
                continue
            self._transition(
                job, "FAILED", reason_code="DEADLINE",
                detail=f"deadline {job['deadline']} passed while the "
                       f"submission outcome was still unknown; not "
                       f"resubmitted (E16)",
                outbox=self.outbox)
            failed.append(job["speech_job_id"])
        return {"expired": failed, "resubmitted": False,
                "state_for_expired": "FAILED", "reason_code": "DEADLINE"}

    # -------------------------------------------------------- 重启恢复
    def recover(self, tenant_id: str | None = None) -> dict:
        """§7.3 的重启矩阵。**按状态分派，没有"统一重试"。**

        - `CHECKING_RIGHTS`：重做复查（崩溃前的内存结论一律作废）
        - `PREPARING`：不自动继续。报价可能已过期，重新取路由需要有事务语义
          的上下文；标记为需要重新准备，由调用方重放。
        - `SUBMITTED`/`WAITING_PROVIDER`：**只读**，等 S26 事件，不自造状态
        - `INGESTING`：什么都不做，等 S28 重新探测时长
        - `WAITING_UNKNOWN`：**什么都不做**（E16）
        - `RIGHTS_BLOCKED`：**什么都不做**（终态）
        """
        sql = "SELECT * FROM speech_job WHERE 1=1"
        args: list = []
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(tenant_id)
        actions = {"rechecked": [], "waiting": [], "noop": [],
                   "needs_reprepare": []}
        for raw in self.conn.execute(sql, args).fetchall():
            job = dict(raw)
            state = job["state"]
            if state == "CHECKING_RIGHTS":
                recheck = self._recheck(
                    tenant_id=job["tenant_id"],
                    voice_id=job["voice_binding_id"],
                    voice_revision=int(job["voice_binding_revision"]),
                    requested_use=self._requested_use(job["locale"]),
                    check_point=WORKER_START,
                    job_id=job["speech_job_id"])
                if not recheck["eligible"] and \
                        recheck["deny_reason"] in RIGHTS_DENY_REASONS:
                    self._block(job, recheck, "RIGHTS_BLOCKED")
                actions["rechecked"].append(job["speech_job_id"])
            elif state == "PREPARING":
                actions["needs_reprepare"].append(job["speech_job_id"])
            elif state in ("SUBMITTED", "WAITING_PROVIDER", "INGESTING"):
                actions["waiting"].append(job["speech_job_id"])
            elif state in ("WAITING_UNKNOWN", "RIGHTS_BLOCKED"):
                actions["noop"].append(job["speech_job_id"])
            elif state in TERMINAL_STATES:
                # READY / REJECTED_LOCAL / FAILED / RECONCILED_* / CANCELED：
                # 重启时也归 noop。把它们列出来而不是静默略过，是因为运维
                # 需要一份"我扫了 137 个 job，每个各归哪一类"的完整清单；
                # 不在任何桶里等于"未知"，会被人当成漏扫。
                actions["noop"].append(job["speech_job_id"])
            else:
                # 新增状态忘了进矩阵：宁可报出来，也不要悄悄不动。
                actions.setdefault("unhandled", []).append(job["speech_job_id"])
        return actions


# ------------------------------------------------------------------ 小工具

def _text_hash_of(svc: "SpeechService", job: dict) -> str:
    row = _row(svc.conn.execute(
        "SELECT text_hash FROM utterance_revision WHERE tenant_id=? AND "
        "utterance_id=? AND locale=? AND text_revision=?",
        (job["tenant_id"], job["utterance_id"], job["locale"],
         job["text_revision"])).fetchone())
    return (row or {}).get("text_hash") or ""


def _op_id(job_id: str) -> str:
    return ID_PREFIX["operation"] + _sha256_hex(job_id)[:20]


def _strategy_class(repair: dict | None, emotion: str) -> str:
    if repair is not None:
        return "re_synthesize_speech"
    return f"generate:{emotion}"


def _ledger_action(job: dict, repair: dict | None) -> str:
    """台账动作。**locale 变化不是新动作**——它是同一句的另一种语言，
    但仍然要写台账（共享同一个 `shot_lineage_id`，§8.1）。"""
    if repair is not None:
        return "re_synthesize_speech"
    return "generate"


def _ref_text(ref) -> str:
    if ref is None:
        return ""
    if isinstance(ref, str):
        return ref
    return _jcs(ref)


def _format_from(output_format: str) -> str:
    return output_format


def _deny_for(to_state: str) -> str:
    return {"revoked": "AUTHORIZATION_REVOKED",
            "expired": "AUTHORIZATION_EXPIRED",
            "suspended": "AUTHORIZATION_SUSPENDED"}.get(
                to_state, "AUTHORIZATION_REVOKED")


def _map_provider_failure(state: str, reason: str | None) -> str:
    """供应商失败 → 本地 `failure_code`。**透传，不换渠道，不重试。**

    `CANCELED` 在 S26 侧可能是用户取消或平台撤单；在 S31 侧一律记为
    `CANCELED_BY_PROVIDER`，因为这一条路径只在 S26 主动取消时到达。
    """
    if state == "CANCELED":
        return "CANCELED_BY_PROVIDER"
    mapping = {"POLICY_REJECTED": "POLICY_REJECTED",
               "ACCOUNT_UNAUTHORIZED": "ACCOUNT_UNAUTHORIZED",
               "BUDGET": "BUDGET", "QUOTA": "QUOTA",
               "INVALID_AUDIO": "INVALID_AUDIO"}
    if reason in mapping:
        return mapping[reason]
    return "PROVIDER_FAILED"
