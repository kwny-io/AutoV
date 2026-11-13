"""S33 内核。方案：`plans/S33-sound-effects.md`。

核心原则一句话：**音效跟着剪辑走，但重定位不是重生成。**

把它拆成两层的后果说清楚，因为这是全文件最容易被写坏的地方：

- **内容层**（什么声音、多长）花钱，走 S26，结果是 `SfxTake`，`content_fingerprint`
  冻结它。剪辑把镜头挪了 300ms，内容层一个字都不用变。
- **位置层**（在第几毫秒）是 `place()` 这个纯函数，免费、可任意重算、可以缓存
  也可以丢掉缓存。把位置写进权威表再当事实读出来，就等于让 S33 成为一个
  剪辑相关事实的第二权威。

所以 `content_fingerprint` **不含时长**（§4.4）。时长一旦进指纹，剪辑缩短一个
镜头就会产出一个新指纹，整集音效重生成——正是本服务要防的那笔钱。

四条不能违反的语义：

1. `cue_placement_cache` 可 TRUNCATE，任何对外读取都现算（§4.2）。
2. 复用要求指纹相同 + 许可可用 + 素材状态可用 + artifact 仍可引用 + 位置可放
   （§4.4）。少一条就重生成。
3. 替代必须同时带 `substitute_take_id` 与 `substitution_authorized_by`
   （§4.6）——语义相似性机器判不了，所以不提供"自动找一个差不多的"。
4. `POLICY_BLOCKED` 与 `GAP_RECORDED` 是两件事：前者没钱花出去、法务改策略就
   能好；后者是"这条 cue 现在缺了"的事实。`SfxGap` 与产生它的状态转移必须
   同事务写入（§3.3）。

事务与幂等与 S03 同构：平台幂等 SDK 的 `begin/finish` 是裸语句，所以命令事务
内把结果写回同一个 `idempotency_record` 行，提交与"记下已提交"原子成立。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    ANCHORS,
    CONTRACT_GAPS,
    GAP_KINDS,
    ID_PREFIX,
    JOB_STATES,
    MAX_DURATION_SECONDS,
    MAX_STRETCH,
    RETRYABLE_STATES,
    SCHEMA,
    SFX_FAMILIES,
    TERMINAL_STATES,
    TRIM_ALLOWED,
)

SOURCE = "S33"
SCHEMA_VERSION = "2.0"

# 已登记事件（event-topics.v2.json）。
EVENT_SFX_READY = "production.sfx.ready"
TOPIC_SFX = "drama.sound-effects.v2"

# 未登记事件（方案 §6.4 自己提出的四个，缺口见 CONTRACT_GAPS）。
LOCAL_JOB_STATE = "sfx.job.state-changed"
LOCAL_GAP_RECORDED = "sfx.gap.recorded"
LOCAL_GAP_CLOSED = "sfx.gap.closed"
LOCAL_RIGHTS_LAPSED = "sfx.take.rights-lapsed"

# 方案 §4.2 的"±1 帧"。方案没写帧率；短剧交付按 24fps 取一帧 ≈ 41.7ms，
# 向上取整成 42ms。帧率变更时需要连同 T33-19 一起重看。
FRAME_TOLERANCE_MS = 42


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


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

def content_fingerprint(cue_intent: str, sfx_family: str,
                        loopable_required: bool) -> str:
    """§4.4。时长**不在**里面——这是整个服务省钱的那一刀。

    用 `\\x1f` 分隔而不是直接拼接：直接拼会让 `("ab","c")` 和 `("a","bc")`
    撞成同一个指纹。方案写的是 `||`，但那是散文里的并置符号，不是 SQL 的
    字符串连接语义；这里按"三段可解析"实现。
    """
    return _sha256_hex("\x1f".join([
        cue_intent, sfx_family, "1" if loopable_required else "0",
    ]))


# ------------------------------------------------------------ 纯函数：place

def place(*, natural_duration_ms: int, requested_duration_ms: int,
          sfx_family: str, loopable: bool) -> dict:
    """§4.2 判定表。**纯函数**：不看库、不看时间、不产生副作用。

    输入只有"素材多长、要放多长、什么族、能不能循环"，输出位置模式与取源区间
    （相对素材自身的 0 点）。把绝对时间算进来的是上层，因为它们依赖 EDL。

    行序即方案的行序，有两个容易写错的地方：

    - **`ambience` 的 `max_stretch` 是 1.0**，而 stretch 只在 `natural <
      requested` 时才有意义，此时 `requested/natural > 1` 恒成立。所以 ambience
      永远走不到 stretch 那一行，T33-23（5s 素材要填 8s 的空 → `loop`）自然成立，
      **不需要把 loop 提到 stretch 前面**。反过来把 loop 提前会改变
      `hard_effect` + loopable + 比值 1.1 的结果（本该 stretch），所以别动。
    - **`ui` 的 `TRIM_ALLOWED` 是 False**（见 schema.py 的说明），所以
      "素材比槽长"的 ui 音效会落到 `unplaceable`。这是有意的：界面音效短一截
      就是换了一个音，属于内容层的决定，不是位置层能自作主张的裁切。
    """
    if natural_duration_ms <= 0 or requested_duration_ms <= 0:
        raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                           "durations must be positive")

    delta = abs(natural_duration_ms - requested_duration_ms)

    # 1. 差在一帧以内：不动。
    if delta <= FRAME_TOLERANCE_MS:
        return {
            "placement_mode": "fixed",
            "source_in_ms": 0,
            "source_out_ms": natural_duration_ms,
            "stretch_ratio": 1.0,
            "reason": None,
        }

    # 2. 素材比槽长且族允许裁切。
    if natural_duration_ms > requested_duration_ms and TRIM_ALLOWED[sfx_family]:
        return {
            "placement_mode": "trim",
            "source_in_ms": 0,
            "source_out_ms": requested_duration_ms,
            "stretch_ratio": 1.0,
            "reason": None,
        }

    # 3. 素材比槽短，族允许拉伸且比值在上限内。
    if natural_duration_ms < requested_duration_ms:
        ratio = requested_duration_ms / natural_duration_ms
        limit = MAX_STRETCH[sfx_family]
        if limit > 1.0 and ratio <= limit:
            return {
                "placement_mode": "stretch",
                "source_in_ms": 0,
                "source_out_ms": natural_duration_ms,
                "stretch_ratio": round(ratio, 4),
                "reason": None,
            }

    # 4. 底噪且可循环：循环铺满，不改素材。
    if sfx_family == "ambience" and loopable:
        return {
            "placement_mode": "loop",
            "source_in_ms": 0,
            "source_out_ms": natural_duration_ms,
            "stretch_ratio": 1.0,
            "reason": None,
        }

    # 5. 放不进去。**这一条不触发重生成**——它只说明这个素材不适合这个槽，
    #    上层记录缺口给人看（§4.2 末段 / §4.6）。
    return {
        "placement_mode": "unplaceable",
        "source_in_ms": None,
        "source_out_ms": None,
        "stretch_ratio": None,
        "reason": "duration_mismatch",
    }


def compute_placement(*, natural_duration_ms: int, requested_duration_ms: int,
                      sfx_family: str, loopable: bool, anchor: str,
                      anchor_offset_ms: int, edl_revision: int | None,
                      edl_shot: dict | None, beat_ref: str | None) -> dict:
    """完整位置 = 纯函数部分 + 锚点。两者分开，前者的单测不依赖 EDL。"""
    base = place(natural_duration_ms=natural_duration_ms,
                 requested_duration_ms=requested_duration_ms,
                 sfx_family=sfx_family, loopable=loopable)

    if base["placement_mode"] == "unplaceable":
        return {**base, "output_start_ms": None, "output_end_ms": None,
                "edl_revision": edl_revision, "anchor": anchor}

    start = _anchor_abs_ms(anchor=anchor, anchor_offset_ms=anchor_offset_ms,
                           edl_shot=edl_shot,
                           requested_duration_ms=requested_duration_ms,
                           beat_ref=beat_ref)
    if start is None:
        # 位置算不出来（EDL 没给）。**不填 0**——0ms 是一个真实的合法位置。
        return {**base, "placement_mode": base["placement_mode"],
                "output_start_ms": None,
                "output_end_ms": None,
                "edl_revision": edl_revision,
                "anchor": anchor,
                "placement_unavailable_reason": "edl_unavailable"}
    return {**base, "output_start_ms": start,
            "output_end_ms": start + requested_duration_ms,
            "edl_revision": edl_revision, "anchor": anchor}


def _anchor_abs_ms(*, anchor: str, anchor_offset_ms: int, edl_shot: dict | None,
                   requested_duration_ms: int, beat_ref: str | None) -> int | None:
    if edl_shot is None:
        return None
    if anchor == "absolute":
        return anchor_offset_ms
    if anchor == "shot_start":
        return int(edl_shot["start_ms"]) + anchor_offset_ms
    if anchor == "shot_end":
        return int(edl_shot["end_ms"]) - requested_duration_ms + anchor_offset_ms
    beats = edl_shot.get("beats") or {}
    if beat_ref is None:
        return None
    return beats.get(beat_ref)


# ------------------------------------------------------------------ 服务

def _row(row) -> dict | None:
    return None if row is None else dict(row)


class SfxService:
    def __init__(self, conn: sqlite3.Connection, *, rights_resolver,
                 route_resolver, submitter, artifact_check, edl_provider,
                 loop_probe, canceler):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 会**隐式提交当前事务**。所以只能在事务外建一次，之后复用同一个实例——
        # 否则命令事务会在第一条事件写入时被悄悄提交掉，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.rights_resolver = rights_resolver
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.artifact_check = artifact_check
        self.edl_provider = edl_provider
        self.loop_probe = loop_probe
        self.canceler = canceler

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
            "UPDATE sfx_job SET state=?, state_seq=?, updated_at=?, "
            "state_entered_at=?, failure_code=COALESCE(?, failure_code), "
            "failure_detail=COALESCE(?, failure_detail) WHERE sfx_job_id=?",
            (to_state, seq, now, now, reason_code, detail, job["sfx_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (sfx_job_id, seq, from_state, to_state, "
            "reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["sfx_job_id"], seq, job["state"], to_state, reason_code,
             detail, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_JOB_STATE,
                subject=f"sfx-job/{job['sfx_job_id']}",
                tenant_id=job["tenant_id"], home_cell="sfx",
                aggregate_id=job["sfx_job_id"], sequence=seq,
                correlation_id=job["sfx_job_id"],
                data={"sfx_job_id": job["sfx_job_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "cue_index": int(job["cue_index"]),
                      "from": job["state"], "to": to_state,
                      "reason_code": reason_code},
                schema_version=SCHEMA_VERSION))
        return {**job, "state": to_state, "state_seq": seq}

    def _job(self, tenant_id: str, job_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM sfx_job WHERE tenant_id=? AND sfx_job_id=?",
            (tenant_id, job_id)).fetchone())
        if row is None:
            raise ServiceError(404, "SFX_JOB_NOT_FOUND", f"no job {job_id}")
        return row

    def _record_gap(self, *, tenant_id: str, shot_lineage_id: str,
                    cue_index: int, cue_id: str, sfx_family: str, kind: str,
                    detail: str, source_job_id: str | None,
                    outbox: Outbox | None) -> dict:
        """**必须与产生它的状态转移同事务调用**（§3.3）。调用方负责事务边界。"""
        if kind not in GAP_KINDS:
            raise AssertionError(f"unknown gap kind {kind}")
        gap_id = _new_id("gap")
        now = _now()
        self.conn.execute(
            "INSERT INTO sfx_gap (gap_id, tenant_id, shot_lineage_id, cue_index, "
            "cue_id, sfx_family, kind, detail, source_job_id, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)",
            (gap_id, tenant_id, shot_lineage_id, cue_index, cue_id, sfx_family,
             kind, detail, source_job_id, now, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_GAP_RECORDED,
                subject=f"sfx-gap/{gap_id}", tenant_id=tenant_id,
                home_cell="sfx", aggregate_id=shot_lineage_id,
                # S33 不持有 shot_lineage 的序列知识：缺口的顺序由 `created_at`
                # 与 `gap_id` 给，envelope 这里只知道"这是关于该镜头的第 1 条
                # 缺口事件"。填一个猜出来的计数比填 1 更糟。
                sequence=1,
                correlation_id=gap_id,
                data={"gap_id": gap_id, "shot_lineage_id": shot_lineage_id,
                      "cue_index": cue_index, "cue_id": cue_id,
                      "kind": kind, "source_job_id": source_job_id},
                schema_version=SCHEMA_VERSION))
        return {"gap_id": gap_id, "kind": kind, "state": "open",
                "detail": detail}

    # -------------------------------------------------------- 校验（§4.1）
    def _validate(self, body: dict, tenant_id: str) -> dict:
        """a–h，**顺序即方案顺序**：先便宜的、先无副作用的。

        注意 (f) 跨租户策略返回 404 而不是 403：告诉调用方"这个 id 存在但不
        让你看"本身就是跨租户信息泄漏。S02 对别的租户的策略，在 S33 眼里和
        不存在是同一种情况。
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
                raise ServiceError(422, "INVALID_CONTEXT", f"context.{key} required")
        if ctx.get("node_epoch") is None or ctx.get("execution_fence") is None:
            raise ServiceError(422, "INVALID_CONTEXT",
                               "context.node_epoch and execution_fence required")

        cue = body.get("cue") or {}
        if not isinstance(cue, dict):
            raise ServiceError(422, "INVALID_CUE", "cue must be object")

        # (c) 族闭集。**不从 cue_intent 猜**——猜错族的代价是生成一个错的音
        #     再花钱重做一次（§2.3）。
        family = cue.get("sfx_family")
        if family not in SFX_FAMILIES:
            raise ServiceError(
                422, "INVALID_SFX_FAMILY",
                f"sfx_family must be one of {list(SFX_FAMILIES)}",
                details={"sfx_family": family})

        # (d) 时长。
        try:
            duration = float(body.get("duration_seconds"))
        except (TypeError, ValueError):
            raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                               "duration_seconds must be a number")
        if not (0 < duration <= MAX_DURATION_SECONDS):
            raise ServiceError(
                422, "DURATION_OUT_OF_RANGE",
                f"duration_seconds must be in (0, {MAX_DURATION_SECONDS}]",
                details={"duration_seconds": duration})

        # (e) 意图不能空白。空白意图的指纹会与"随便什么都行"共用一个值，
        #     于是两条本来不同的 cue 会互相复用（F1）。
        intent = cue.get("cue_intent")
        if not isinstance(intent, str) or not intent.strip():
            raise ServiceError(422, "EMPTY_CUE_INTENT",
                               "cue.cue_intent must be non-blank")

        # (f) 许可策略必须同租户。
        ref = body.get("rights_policy_ref") or {}
        if not isinstance(ref, dict) or ref.get("service_id") != "S02":
            raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                               "rights_policy_ref must be an S02 reference")
        for key in ("id", "revision", "content_hash"):
            if ref.get(key) in (None, ""):
                raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                                   f"rights_policy_ref.{key} required")
        if len(str(ref.get("content_hash"))) != 64:
            raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                               "rights_policy_ref.content_hash must be sha256 hex")

        # (g) shot / beat 归属。归属只有 S06/S07 知道，靠注入的校验器。
        if not cue.get("shot_lineage_id"):
            raise ServiceError(422, "INVALID_CUE", "cue.shot_lineage_id required")
        anchor = cue.get("anchor")
        if anchor not in ANCHORS:
            raise ServiceError(422, "INVALID_ANCHOR",
                               f"anchor must be one of {list(ANCHORS)}")
        if anchor == "beat" and not cue.get("beat_ref"):
            raise ServiceError(422, "INVALID_ANCHOR",
                               "anchor='beat' requires cue.beat_ref")

        # (h) 截止时间。
        deadline = body.get("deadline")
        if not deadline:
            raise ServiceError(422, "INVALID_BODY", "deadline required")
        if str(deadline) <= _now():
            raise ServiceError(422, "DEADLINE_PASSED",
                               "deadline is not in the future",
                               details={"deadline": deadline})

        return {"context": ctx, "cue": cue, "family": family,
                "duration": duration, "intent": intent,
                "anchor": anchor, "rights_ref": ref, "deadline": deadline}

    # ---------------------------------------------------------- 许可解析
    def _bind_rights(self, tenant_id: str, ref: dict, family: str) -> dict:
        """把 `rights_policy_ref` 解析成一份冻结快照（§4.5）。

        C5：S02 没有公开的解析读接口，所以这是注入 callback。没配 → 503，
        **不是"直接透传给 S26 让供应商去判断合规"**。
        """
        try:
            resolved = self.rights_resolver(ref, tenant_id)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE",
                               str(exc), retryable=True) from exc
        if not isinstance(resolved, dict):
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE",
                               "rights resolver returned no snapshot",
                               retryable=True)

        kinds = resolved.get("allowed_license_kinds")
        if not isinstance(kinds, list):
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE",
                               "rights resolver omitted allowed_license_kinds",
                               retryable=True)
        blocked = resolved.get("blocked_families") or []
        # §3.1 / §4.1 第 3 步：**空集合或族冲突都走 POLICY_BLOCKED + 缺口**，
        # 不是 4xx。两者都不是"这次请求写错了"，而是"当前策略下这条 cue 生不
        # 出来"——换策略就能好，所以要留下一件法务能看见的事，而不是一个
        # 只存在于调用方日志里的错误码。
        if not kinds or family in blocked:
            # T33-08：DB 的 rpb_kinds_present 会拒绝空集合，所以**根本不构造
            # binding**。让 IntegrityError 飞出去会变成 500，而方案要的是状态。
            return {"rights_policy_binding_id": None,
                    "allowed_license_kinds": kinds,
                    "blocked_families": blocked,
                    "has_licensed_route": False,
                    "blocked_reason": ("no_licensed_kinds" if not kinds
                                       else "family_blocked")}

        binding_id = _new_id("binding")
        self.conn.execute(
            "INSERT INTO rights_policy_binding (rights_policy_binding_id, "
            "tenant_id, policy_ref, allowed_license_kinds, attribution_required, "
            "territory_restrictions, blocked_families, expires_at, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (binding_id, tenant_id, _jcs(ref),
             _jcs(kinds), 1 if resolved.get("attribution_required") else 0,
             _jcs(resolved.get("territory_restrictions") or []),
             _jcs(blocked), resolved.get("expires_at"), _now()))
        return {"rights_policy_binding_id": binding_id,
                "allowed_license_kinds": kinds,
                "has_licensed_route": bool(kinds)}

    # ------------------------------------------------------- 位置读取
    def _edl(self, tenant_id: str, shot_lineage_id: str,
             edl_revision: int | None) -> dict | None:
        """S41 的 EDL。没配 → None（不知道），不是"没有剪辑"。

        `None` 与 `{"edl_revision": 0}` 必须分开：前者是"问不到"，后者是
        "问到了，是第一版"。
        """
        try:
            return self.edl_provider(tenant_id, shot_lineage_id, edl_revision)
        except (ServiceError, ConnectionError):
            return None

    def _placement_of(self, *, tenant_id: str, take: dict, cue: dict,
                      edl_revision: int | None) -> dict:
        edl = self._edl(tenant_id, cue["shot_lineage_id"], edl_revision)
        shot = None
        resolved_rev = edl_revision
        if edl:
            resolved_rev = edl.get("edl_revision", edl_revision)
            shot = (edl.get("shots") or {}).get(cue["shot_lineage_id"])
        # §4.5 时点 2：每一次给位置都要带当前 rights_state。不可用就返回
        # unplaceable + 缺口，而不是返回一个位置——S42 拿到位置就会混进去。
        if take.get("rights_state") != "usable":
            return {"placement_mode": "unplaceable",
                    "source_in_ms": None, "source_out_ms": None,
                    "output_start_ms": None, "output_end_ms": None,
                    "stretch_ratio": None, "reason": "rights_lapsed",
                    "edl_revision": edl_revision, "anchor": cue["anchor"],
                    "rights_state": take.get("rights_state")}
        return compute_placement(
            natural_duration_ms=int(take["natural_duration_ms"]),
            requested_duration_ms=int(take["requested_duration_ms"]),
            sfx_family=cue["sfx_family"],
            loopable=bool(take["loopable"]),
            anchor=cue["anchor"],
            anchor_offset_ms=int(cue["anchor_offset_ms"]),
            edl_revision=resolved_rev, edl_shot=shot,
            beat_ref=cue.get("beat_ref"))

    # ------------------------------------------------------- 复用（§4.4）
    def _find_reusable(self, *, tenant_id: str, shot_lineage_id: str,
                       cue_index: int, fingerprint: str, family: str,
                       requested_duration_ms: int, cue: dict,
                       edl_revision: int | None) -> dict | None:
        rows = self.conn.execute(
            "SELECT * FROM sfx_take WHERE tenant_id=? AND content_fingerprint=? "
            "AND sfx_family=? AND rights_state='usable' "
            "AND state IN ('READY','ACCEPTED') ORDER BY created_at DESC",
            (tenant_id, fingerprint, family)).fetchall()
        for row in rows:
            take = dict(row)
            # 前缀要求：尽量在同一个镜头内复用，避免全剧共用一个"关门声"后
            # 保真度在剪辑里出现跨镜头的音量断层。这不是硬条件，只是顺序。
            artifact = json.loads(take["artifact_ref"])
            # 条件 4：artifact 仍可引用（S28 是权威）。
            try:
                still_there = self.artifact_check(artifact, tenant_id)
            except (ServiceError, ConnectionError):
                still_there = None
            if still_there is False:
                continue
            # 条件 5：位置可放。EDL 未知时不拦——重定位是免费且可随时重算的，
            # 为一个"以后必然能算出来"的答案去买一次生成，方向是反的。
            if still_there is None and edl_revision is None:
                placement = {"placement_mode": "unknown",
                             "placement_unavailable_reason": "edl_unavailable"}
            else:
                placement = self._placement_of(
                    tenant_id=tenant_id,
                    take={**take, "requested_duration_ms": requested_duration_ms},
                    cue=cue, edl_revision=edl_revision)
                if placement["placement_mode"] == "unplaceable":
                    continue
            return {"take": take, "artifact_ref": artifact,
                    "placement": placement}
        return None

    # ---------------------------------------------------- POST /sfx-jobs
    def create_job(self, body: dict, tenant_id: str, command_id: str) -> dict:
        checked = self._validate(body, tenant_id)
        ctx, cue, family = checked["context"], checked["cue"], checked["family"]
        requested_ms = int(round(checked["duration"] * 1000))
        if requested_ms <= 0:
            raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                               "duration rounds to 0ms")
        loopable_required = bool(cue.get("loopable_required"))

        # 替代配对（§4.6）。DB 也有 CHECK，但在这里先报一句人话。
        sub_take = body.get("substitute_take_id")
        sub_by = body.get("substitution_authorized_by")
        if bool(sub_take) != bool(sub_by):
            raise ServiceError(
                422, "SUBSTITUTION_NOT_AUTHORIZED",
                "substitute_take_id and substitution_authorized_by must both be "
                "present or both be absent")

        fingerprint = content_fingerprint(checked["intent"], family,
                                          loopable_required)
        # 两个不同的键，各管一件事（§7.1）：
        # - `command_key` 是**命令级**的（`sfj_tenant_idem` 的取值）。一条命令
        #   最多落一个 job；同命令重放由 idempotency_record 拦掉，到不了这里。
        # - `business_key` 是**意图级**的，只在未终结时成立（`sfx_job_open_uniq`
        #   这个部分唯一索引）。job 终结后同一意图再提，本来就该能开新 job。
        # 方案 §5.2 把 `sfj_tenant_idem` 写成普通唯一约束，又用 §7.1 说"未终结
        # 才命中"——两处只能靠这个分工同时成立。
        command_key = _sha256_hex("\x1f".join([
            tenant_id, command_id, str(body.get("request_hash") or "")]))
        business_key = _sha256_hex("\x1f".join([
            tenant_id, cue["shot_lineage_id"], str(cue["cue_index"]),
            fingerprint, str(requested_ms)]))

        def produce() -> dict:
            cue_rev = self._upsert_cue(tenant_id, cue, family, checked["intent"])
            cue_full = {**cue_rev, "sfx_family": family,
                        "anchor": cue["anchor"],
                        "anchor_offset_ms": int(cue.get("anchor_offset_ms") or 0),
                        "shot_lineage_id": cue["shot_lineage_id"]}

            # 复用先于生成。复用的响应没有 job（§6.1 的 200 形状）。
            reuse = self._find_reusable(
                tenant_id=tenant_id,
                shot_lineage_id=cue["shot_lineage_id"],
                cue_index=int(cue["cue_index"]),
                fingerprint=fingerprint, family=family,
                requested_duration_ms=requested_ms, cue=cue_full,
                edl_revision=body.get("edl_revision"))
            if reuse is not None:
                return self._reuse_response(
                    tenant_id=tenant_id, take=reuse["take"],
                    artifact=reuse["artifact_ref"],
                    placement=reuse["placement"], cue_full=cue_full,
                    requested_ms=requested_ms)

            rights = self._bind_rights(tenant_id, checked["rights_ref"], family)

            if sub_take:
                # §4.6：替代的四条件里，1/3/4 由 S33 检验，2 由人负责。
                return self._substitute(
                    tenant_id=tenant_id, cue=cue_full, family=family,
                    substitute_take_id=sub_take,
                    authorized_by=sub_by, rights=rights,
                    requested_ms=requested_ms, fingerprint=fingerprint,
                    deadline=checked["deadline"])

            job_id = _new_id("job")
            now = _now()
            try:
                self.conn.execute(
                    "INSERT INTO sfx_job (sfx_job_id, tenant_id, project_id, "
                    "series_id, episode_id, branch_id, cue_id, cue_revision, "
                    "shot_lineage_id, cue_index, sfx_family, "
                    "requested_duration_ms, loopable_required, "
                    "rights_policy_binding_id, content_fingerprint, "
                    "substitute_take_id, substitution_authorized_by, node_id, "
                    "node_epoch_at_create, execution_fence, request_hash, "
                    "idempotency_key, business_key, variation_nonce, "
                    "budget_scope_id, deadline, state, state_seq, created_at, "
                    "updated_at, state_entered_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "'CREATED',0,?,?,?)",
                    (job_id, tenant_id, ctx["project_id"], ctx["series_id"],
                     ctx["episode_id"], ctx["branch_id"], cue_full["cue_id"],
                     int(cue_full["cue_revision"]), cue["shot_lineage_id"],
                     int(cue["cue_index"]), family, requested_ms,
                     1 if loopable_required else 0,
                     rights["rights_policy_binding_id"], fingerprint,
                     sub_take, sub_by, ctx["node_id"], int(ctx["node_epoch"]),
                     int(ctx["execution_fence"]),
                     body.get("request_hash") or "", command_key, business_key,
                     uuid.uuid4().hex, body.get("budget_scope_id") or "default",
                     checked["deadline"], now, now, now))
            except sqlite3.IntegrityError as exc:
                if "sfx_job_open_uniq" in str(exc):
                    # 同一意图已有未终结的 job（真正的并发双提交）。不是错误，
                    # 是"这次生产已经在路上了"，返回那个 job（§7.1 业务幂等）。
                    existing = _row(self.conn.execute(
                        "SELECT * FROM sfx_job WHERE tenant_id=? AND "
                        "business_key=?", (tenant_id, business_key)).fetchone())
                    return self._job_response(
                        existing, fingerprint, requested_ms,
                        extra={"duplicate_suppressed": True})
                raise
            job = self._job(tenant_id, job_id)
            outbox = self.outbox

            job = self._transition(job, "CHECKING_POLICY", outbox=outbox)
            if not rights["has_licensed_route"]:
                # 空的许可集合 = 什么都不能用（§5.3），族被策略禁掉同理。没有钱
                # 花出去，记录的是 POLICY_BLOCKED 而不是缺口：法务改策略就能好。
                # F5 / T33-07：POLICY_BLOCKED **同时**记缺口。缺口不是失败，
                # 是"这条 cue 现在缺了"的事实，法务改策略后要能看到它。
                blocked_by_family = rights.get("blocked_reason") == "family_blocked"
                reason = ("FAMILY_BLOCKED" if blocked_by_family
                          else "NO_LICENSED_ROUTE")
                self._record_gap(
                    tenant_id=tenant_id,
                    shot_lineage_id=cue["shot_lineage_id"],
                    cue_index=int(cue["cue_index"]), cue_id=cue_full["cue_id"],
                    sfx_family=family, kind="no_licensed_route",
                    detail=("family is blocked by the policy and no other "
                            "licensed route exists" if blocked_by_family
                            else "allowed_license_kinds is empty for this policy"),
                    source_job_id=job["sfx_job_id"], outbox=outbox)
                job = self._transition(
                    job, "POLICY_BLOCKED", reason_code=reason,
                    detail=("family blocked by policy" if blocked_by_family
                            else "allowed_license_kinds is empty"), outbox=outbox)
                return self._job_response(job, fingerprint, requested_ms,
                                          extra={"reason_code": reason})

            job = self._transition(job, "PREPARING", outbox=outbox)
            return self._submit(job, cue_full, outbox,
                                fingerprint=fingerprint,
                                requested_ms=requested_ms)

        return self._command(tenant_id, command_id, produce)

    # ------------------------------------------------------------ 替代
    def _substitute(self, *, tenant_id: str, cue: dict, family: str,
                    substitute_take_id: str, authorized_by: str, rights: dict,
                    requested_ms: int, fingerprint: str,
                    deadline: str) -> dict:
        """§4.6 的替代路径。**只有显式请求 + 显式授权才走到这里。**

        条件 2（`cue_intent` 语义相近）机器判不了，所以它体现在"有人签了字"上，
        不是体现在任何比较函数里。这里只检验条件 1/3/4。
        """
        source = _row(self.conn.execute(
            "SELECT * FROM sfx_take WHERE tenant_id=? AND sfx_take_id=?",
            (tenant_id, substitute_take_id)).fetchone())
        if source is None:
            raise ServiceError(404, "SFX_TAKE_NOT_FOUND",
                               f"no take {substitute_take_id}")

        # 条件 1：同族。
        if source["sfx_family"] != family:
            raise ServiceError(
                422, "FAMILY_MISMATCH",
                f"substitute take is {source['sfx_family']!r}, cue needs "
                f"{family!r}", details={"take_family": source["sfx_family"],
                                        "cue_family": family})

        # 条件 3：许可仍可用。
        source = self.recheck_take_rights(tenant_id, substitute_take_id)
        if source.get("rights_state") != "usable":
            raise ServiceError(409, "SUBSTITUTE_NOT_USABLE",
                               "substitute take's rights are not usable")

        # 条件 4：位置可行。
        placement = compute_placement(
            natural_duration_ms=int(source["natural_duration_ms"]),
            requested_duration_ms=requested_ms, sfx_family=family,
            loopable=bool(source["loopable"]), anchor=cue["anchor"],
            anchor_offset_ms=int(cue["anchor_offset_ms"]),
            edl_revision=None, edl_shot=None, beat_ref=cue.get("beat_ref"))
        if placement["placement_mode"] == "unplaceable":
            raise ServiceError(
                409, "SUBSTITUTE_UNPLACEABLE",
                "substitute take cannot be placed into the requested duration",
                details={"natural_duration_ms": int(source["natural_duration_ms"]),
                         "requested_duration_ms": requested_ms})

        take_id = _new_id("take")
        now = _now()
        # 新 take 行：内容来自替代件，指纹记**本 cue 的**意图指纹（它是给这次
        # 需求产生的可用素材），source_kind='substituted'。
        self.conn.execute(
            "INSERT INTO sfx_take (sfx_take_id, tenant_id, sfx_job_id, cue_id, "
            "shot_lineage_id, cue_index, sfx_family, artifact_ref, "
            "natural_duration_ms, sample_rate_hz, channel_count, "
            "container_format, content_fingerprint, loopable, loopable_verified, "
            "rights_policy_binding_id, rights_state, rights_checked_at, "
            "provider_job_id, attempt_id, source_kind, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'READY',?)",
            (take_id, tenant_id, source["sfx_job_id"], cue["cue_id"],
             cue["shot_lineage_id"], int(cue["cue_index"]), family,
             source["artifact_ref"], int(source["natural_duration_ms"]),
             int(source["sample_rate_hz"]), int(source["channel_count"]),
             source["container_format"], fingerprint,
             1 if source["loopable"] else 0, int(source["loopable_verified"]),
             rights["rights_policy_binding_id"], "usable", now,
             source["provider_job_id"], source["attempt_id"], "substituted",
             now))

        # 替代填上了一个缺口 → 关闭它（显式动作，不是自动的）。
        self.conn.execute(
            "UPDATE sfx_gap SET state='closed', closed_by_take_id=?, updated_at=? "
            "WHERE tenant_id=? AND cue_id=? AND state='open'",
            (take_id, now, tenant_id, cue["cue_id"]))

        outbox = self.outbox
        outbox.append(new_event(
            source=SOURCE, type=LOCAL_GAP_CLOSED,
            subject=f"sfx-take/{take_id}", tenant_id=tenant_id,
            home_cell="sfx", aggregate_id=cue["shot_lineage_id"],
            # 同 `_record_gap`：S33 不持有 shot_lineage 的序列知识。
            sequence=1,
            correlation_id=take_id,
            data={"cue_id": cue["cue_id"], "closed_by_take_id": take_id,
                  "closed_as": "substituted",
                  "substitution_authorized_by": authorized_by},
            schema_version=SCHEMA_VERSION))
        return {
            "sfx_job_id": None, "reused": False, "substituted": True,
            "sfx_take_id": take_id,
            "source_kind": "substituted",
            "artifact_ref": json.loads(source["artifact_ref"]),
            "natural_duration_ms": int(source["natural_duration_ms"]),
            "requested_duration_ms": requested_ms,
            "content_fingerprint": fingerprint,
            "substitution_authorized_by": authorized_by,
            "placement": {"placement_mode": placement["placement_mode"],
                          "source_in_ms": placement["source_in_ms"],
                          "source_out_ms": placement["source_out_ms"],
                          "stretch_ratio": placement["stretch_ratio"]},
            "note": "substituted; no paid generation was submitted",
        }

    # ---------------------------------------------------------- 提交 S26
    def _reject_local(self, job: dict, code: str, outbox: Outbox, *,
                      detail: str, extra: dict | None = None) -> dict:
        """本地拒绝：没花钱、没留台账。状态是终态但**不是失败**——F6/F7/F25
        都是"在当前渠道下生不出来"，换渠道/改策略就能好。"""
        self.conn.execute(
            "UPDATE sfx_job SET failure_code=?, updated_at=? WHERE sfx_job_id=?",
            (code, _now(), job["sfx_job_id"]))
        job = self._job(job["tenant_id"], job["sfx_job_id"])
        job = self._transition(job, "REJECTED_LOCAL", reason_code=code,
                               detail=detail, outbox=outbox)
        body = {"sfx_job_id": job["sfx_job_id"], "state": job["state"],
                "reason_code": code}
        body.update(extra or {})
        return body

    def _submit(self, job: dict, cue: dict, outbox: Outbox,
                *, fingerprint: str, requested_ms: int) -> dict:
        """组装 GenerationCommand 并提交。

        **C1/C3 的落地处**：`sfx_family`、`anchor`、`loopable_required` 在这里
        被丢掉——`generation-command.v2` 的 `sound_effect.input` 只有
        `cue_intent` / `duration_seconds` / `rights_policy_ref`，而且
        `additionalProperties: false`。上层需要它们，供应商不需要；传不进去
        就不能假装传了。
        """
        generation_command = {
            "modality": "sound_effect",
            "input": {
                "cue_intent": cue["cue_intent"],
                "duration_seconds": round(requested_ms / 1000.0, 3),
                "rights_policy_ref": json.loads(
                    self.conn.execute(
                        "SELECT policy_ref FROM rights_policy_binding WHERE "
                        "rights_policy_binding_id=?",
                        (job["rights_policy_binding_id"],)).fetchone()["policy_ref"]),
            },
        }

        try:
            route = self.route_resolver(job["tenant_id"], job, generation_command)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "ROUTE_DECISION_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        if isinstance(route, dict) and route.get("feasible") is False:
            # F7：路由不可行。透传 degradation_options，**不自己降级**——
            # 缩短时长或换族都是内容层的决定。
            return self._reject_local(
                job, "ROUTE_INFEASIBLE", outbox,
                detail="no feasible route",
                extra={"degradation_options": route.get("degradation_options") or []})

        # F25：数据驻留。这条必须排在提交之前，否则钱已经花出去了。
        if isinstance(route, dict) and route.get("residency_allowed") is False:
            return self._reject_local(
                job, "RESIDENCY_FORBIDDEN", outbox,
                detail="route cannot satisfy the data residency requirement")

        # F6：渠道不支持可循环但要求可循环 —— **不降级为"生成一个长的"**。
        # 30 秒环境音与 3 秒循环素材差 10 倍成本，而且剪辑一变就不能复用。
        if job["loopable_required"]:
            caps = (route or {}).get("capabilities") or {}
            if caps.get("loopable") is False:
                return self._reject_local(
                    job, "CAPABILITY_MISSING", outbox,
                    detail="requested loopable output but the route cannot "
                           "produce it; refusing to silently degrade")

        if not isinstance(route, dict) or not route.get("route_decision_id"):
            # S25 目前是 design_only，拿不到可行路由时**不能**自己编一个。
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "no feasible route decision; S25 is not available",
                retryable=True)

        try:
            submitted = self.submitter(job, generation_command, route)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "GENERATION_JOBS_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        if not isinstance(submitted, dict) or not submitted.get("provider_job_id"):
            raise ServiceError(503, "GENERATION_JOBS_UNAVAILABLE",
                               "S26 did not return a provider_job_id",
                               retryable=True)

        now = _now()
        self.conn.execute(
            "UPDATE sfx_job SET provider_job_id=?, attempt_id=?, "
            "route_decision_ref=?, quote_upper_bound_micro=?, updated_at=? "
            "WHERE sfx_job_id=?",
            (submitted["provider_job_id"], submitted.get("attempt_id"),
             _jcs({"service_id": "S25",
                   "id": route["route_decision_id"],
                   "revision": route.get("revision", 1)}),
             route.get("upper_bound_micro"), now, job["sfx_job_id"]))
        # 只有真的花出钱去的意图才进台账（§8.1）。复用/裁切/重算/记缺口不写，
        # 所以剪辑改 20 遍不会把 E38 的数字放大 20 倍。
        self.conn.execute(
            "INSERT INTO sfx_intent_ledger (entry_id, tenant_id, sfx_job_id, "
            "shot_lineage_id, cue_index, action, sfx_family, run_id, node_epoch, "
            "manifest_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["sfx_job_id"],
             job["shot_lineage_id"], int(job["cue_index"]), "generate",
             job["sfx_family"], None, int(job["node_epoch_at_create"]),
             None, now))

        job = self._job(job["tenant_id"], job["sfx_job_id"])
        job = self._transition(job, "SUBMITTED", outbox=outbox)
        job = self._transition(job, "WAITING_PROVIDER", outbox=outbox)
        return self._job_response(job, fingerprint, requested_ms,
                                  extra={"operation_id": submitted.get("operation_id")})

    # ------------------------------------------------------------ 响应体
    @staticmethod
    def _job_response(job: dict, fingerprint: str, requested_ms: int,
                      *, extra: dict | None = None) -> dict:
        body = {
            "sfx_job_id": job["sfx_job_id"],
            "state": job["state"],
            "content_fingerprint": fingerprint,
            "requested_duration_ms": requested_ms,
            "reused": False,
        }
        body.update(extra or {})
        return body

    def _reuse_response(self, *, tenant_id: str, take: dict, artifact: dict,
                        placement: dict, cue_full: dict,
                        requested_ms: int) -> dict:
        note = "content reused; position recomputed or deferred"
        if placement.get("placement_unavailable_reason") == "edl_unavailable":
            note = ("content reused; placement deferred because no EDL revision "
                    "is known yet (no cost, recomputable at any time)")
        return {
            "sfx_job_id": None,
            "reused": True,
            "sfx_take_id": take["sfx_take_id"],
            "artifact_ref": artifact,
            "natural_duration_ms": int(take["natural_duration_ms"]),
            "requested_duration_ms": requested_ms,
            "content_fingerprint": take["content_fingerprint"],
            "placement": {
                "placement_mode": placement["placement_mode"],
                "source_in_ms": placement.get("source_in_ms"),
                "source_out_ms": placement.get("source_out_ms"),
                "output_start_ms": placement.get("output_start_ms"),
                "output_end_ms": placement.get("output_end_ms"),
                "edl_revision": placement.get("edl_revision"),
                "anchor": placement.get("anchor"),
                "stretch_ratio": placement.get("stretch_ratio"),
                "reason": placement.get("reason"),
                "placement_unavailable_reason":
                    placement.get("placement_unavailable_reason"),
            },
            "note": note,
        }

    # -------------------------------------------------------- cue 版本记录
    def _upsert_cue(self, tenant_id: str, cue: dict, family: str,
                    intent: str) -> dict:
        intent_hash = _sha256_hex(_jcs({
            "cue_intent": intent, "sfx_family": family,
            "anchor": cue["anchor"],
            "anchor_offset_ms": int(cue.get("anchor_offset_ms") or 0),
            "loopable_required": bool(cue.get("loopable_required")),
            "beat_ref": cue.get("beat_ref"),
        }))
        existing = self.conn.execute(
            "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND intent_hash=?", (tenant_id, cue["cue_id"], intent_hash)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        revision = 1 + (self.conn.execute(
            "SELECT COALESCE(MAX(cue_revision),0) AS m FROM sfx_cue_revision "
            "WHERE tenant_id=? AND cue_id=?",
            (tenant_id, cue["cue_id"])).fetchone()["m"])
        self.conn.execute(
            "INSERT INTO sfx_cue_revision (tenant_id, cue_id, cue_revision, "
            "shot_lineage_id, cue_index, beat_ref, sfx_family, cue_intent, "
            "intent_hash, anchor, anchor_offset_ms, loopable_required, "
            "source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, cue["cue_id"], revision, cue["shot_lineage_id"],
             int(cue["cue_index"]), cue.get("beat_ref"), family, intent,
             intent_hash, cue["anchor"], int(cue.get("anchor_offset_ms") or 0),
             1 if cue.get("loopable_required") else 0,
             _jcs(cue.get("source_ref") or {"service_id": "S07", "id": "unknown"}),
             _now()))
        return dict(self.conn.execute(
            "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND cue_revision=?", (tenant_id, cue["cue_id"], revision)
        ).fetchone())

    # ------------------------------------------------ GET /sfx-cues/{id}
    def get_cue(self, tenant_id: str, cue_id: str, *,
                cue_revision: int | None = None,
                edl_revision: int | None = None) -> dict:
        """§6.2。返回 cue + 意图 + checkpoint + take + placement + gap + history。

        `placement` **每次现算**，不读 `cue_placement_cache` 当事实。缓存存在的
        唯一理由是省 CPU，读路径可以绕过它。
        """
        if cue_revision is None:
            cue = _row(self.conn.execute(
                "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
                "ORDER BY cue_revision DESC LIMIT 1",
                (tenant_id, cue_id)).fetchone())
        else:
            cue = _row(self.conn.execute(
                "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
                "AND cue_revision=?",
                (tenant_id, cue_id, int(cue_revision))).fetchone())
        if cue is None:
            raise ServiceError(404, "SFX_CUE_NOT_FOUND", f"no cue {cue_id}")

        take = _row(self.conn.execute(
            "SELECT t.*, j.requested_duration_ms AS requested_duration_ms, "
            "j.state AS job_state FROM sfx_take t "
            "JOIN sfx_job j ON j.sfx_job_id = t.sfx_job_id "
            "WHERE t.tenant_id=? AND t.cue_id=? "
            "AND t.state IN ('READY','ACCEPTED') "
            "ORDER BY t.created_at DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())

        checkpoint = _row(self.conn.execute(
            "SELECT * FROM sfx_checkpoint WHERE tenant_id=? AND shot_lineage_id=? "
            "AND cue_index=?",
            (tenant_id, cue["shot_lineage_id"], int(cue["cue_index"]))).fetchone())

        gap = _row(self.conn.execute(
            "SELECT * FROM sfx_gap WHERE tenant_id=? AND cue_id=? "
            "AND state='open' ORDER BY created_at DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())

        history = [dict(r) for r in self.conn.execute(
            "SELECT j.sfx_job_id, j.state, j.state_seq, j.sfx_family, "
            "j.requested_duration_ms, j.created_at, j.updated_at "
            "FROM sfx_job j WHERE j.tenant_id=? AND j.cue_id=? "
            "ORDER BY j.created_at DESC", (tenant_id, cue_id)).fetchall()]

        placement = None
        placement_note = None
        if take is not None:
            # 这次 GET 是**知情的副作用**：它只可能把 usable 变 void（§4.5）。
            # 反过来"查到许可恢复了就把 void 改回 usable"是不允许的——
            # 那需要一次新的生成意图，不是一次读取。
            checked_take = self.recheck_take_rights(tenant_id,
                                                    take["sfx_take_id"])
            # 复查回的是 `sfx_take` 一行，`requested_duration_ms` 来自 job 的
            # 连接，要补回去——placement 的"要放多长"不能因为一次许可复查丢掉。
            take = {**checked_take,
                    "requested_duration_ms": take["requested_duration_ms"]}
            if take.get("rights_state") != "usable":
                gap = gap or _row(self.conn.execute(
                    "SELECT * FROM sfx_gap WHERE tenant_id=? AND cue_id=? "
                    "AND state='open' AND kind='rights_lapsed' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tenant_id, cue_id)).fetchone())
            placement = self._placement_of(tenant_id=tenant_id, take=take,
                                           cue=cue, edl_revision=edl_revision)
            placement_note = placement.pop("placement_unavailable_reason", None)

        return {
            "cue": cue,
            "checkpoint": checkpoint,
            "take": take,
            "placement": placement,
            "placement_unavailable_reason": placement_note,
            "gap": gap,
            "jobs": history,
            # 方案 §6.2 要求"回答里点名用的哪一版 EDL"。
            "edl_revision": (placement or {}).get("edl_revision"),
        }

    # ------------------------------------------------- GET /sfx-cues?...
    def list_cues(self, tenant_id: str, *, shot_lineage_id: str | None = None,
                  episode_id: str | None = None, family: str | None = None,
                  has_gap: bool | None = None,
                  edl_revision: int | None = None) -> dict:
        """§6.3。两条查询形状共用一处实现：

        - `?shot_lineage_id=&family=&has_gap=`
        - `?episode_id=&has_gap=true`（C6：缺口从**这里**取，不映射进 S41 的
          `gaps[].kind` 闭集）

        返回的是"每条 cue 的最新意图"加上它当前有无开放缺口，不是所有版本。
        """
        where = ["c.tenant_id=?"]
        args: list = [tenant_id]
        if shot_lineage_id:
            where.append("c.shot_lineage_id=?")
            args.append(shot_lineage_id)
        if episode_id:
            # episode 归属在 job 上（cue 行不重复存），所以靠 job 反查。
            where.append(
                "c.cue_id IN (SELECT j.cue_id FROM sfx_job j WHERE j.tenant_id=? "
                "AND j.episode_id=?)")
            args.extend([tenant_id, episode_id])
        if family:
            where.append("c.sfx_family=?")
            args.append(family)

        rows = self.conn.execute(
            "SELECT c.* FROM sfx_cue_revision c WHERE " + " AND ".join(where) +
            " AND c.cue_revision = (SELECT MAX(x.cue_revision) FROM "
            "sfx_cue_revision x WHERE x.tenant_id=c.tenant_id "
            "AND x.cue_id=c.cue_id) ORDER BY c.shot_lineage_id, c.cue_index",
            args).fetchall()

        items = []
        for row in rows:
            cue = dict(row)
            gap = _row(self.conn.execute(
                "SELECT gap_id, kind, detail FROM sfx_gap WHERE tenant_id=? "
                "AND cue_id=? AND state='open' ORDER BY created_at DESC LIMIT 1",
                (tenant_id, cue["cue_id"])).fetchone())
            if has_gap is True and gap is None:
                continue
            if has_gap is False and gap is not None:
                continue
            take = _row(self.conn.execute(
                "SELECT t.sfx_take_id, t.natural_duration_ms, t.loopable, "
                "t.rights_state, t.source_kind, t.artifact_ref, "
                "j.requested_duration_ms AS requested_duration_ms "
                "FROM sfx_take t JOIN sfx_job j ON j.sfx_job_id=t.sfx_job_id "
                "WHERE t.tenant_id=? AND t.cue_id=? "
                "AND t.state IN ('READY','ACCEPTED') "
                "ORDER BY t.created_at DESC LIMIT 1",
                (tenant_id, cue["cue_id"])).fetchone())
            item = {**cue, "gap": gap,
                    "has_gap": gap is not None, "take": take}
            if take is not None:
                item["placement"] = self._placement_of(
                    tenant_id=tenant_id, take=take, cue=cue,
                    edl_revision=edl_revision)
            items.append(item)

        return {"items": items, "total": len(items),
                "has_gap_filter": has_gap}

    # --------------------------------- GET /shots/{id}/sfx-checkpoint
    def get_shot_checkpoint(self, tenant_id: str, shot_lineage_id: str) -> dict:
        """§6.3。检查点是"这个镜头播到第几条 cue 了"，不是权威音频。

        `expected_cue_count` 来自 S06/S07 的镜头规格——本服务不持有它，所以
        没配时返回 None 而不是 0（0 会被读成"这个镜头本来就没有音效"）。
        """
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM sfx_checkpoint WHERE tenant_id=? AND shot_lineage_id=? "
            "ORDER BY cue_index", (tenant_id, shot_lineage_id)).fetchall()]
        gaps = [dict(r) for r in self.conn.execute(
            "SELECT gap_id, cue_index, kind FROM sfx_gap WHERE tenant_id=? "
            "AND shot_lineage_id=? AND state='open' ORDER BY cue_index",
            (tenant_id, shot_lineage_id)).fetchall()]
        return {
            "shot_lineage_id": shot_lineage_id,
            "checkpoints": rows,
            "next_cue_index": max([r["cue_index"] for r in rows], default=None),
            "open_gaps": gaps,
            "checkpoint_seq": max([r["checkpoint_seq"] for r in rows], default=None),
        }

    # ------------------------------------------- GET /sfx-takes/{id}
    def get_take(self, tenant_id: str, take_id: str) -> dict:
        take = _row(self.conn.execute(
            "SELECT * FROM sfx_take WHERE tenant_id=? AND sfx_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "SFX_TAKE_NOT_FOUND", f"no take {take_id}")
        return take

    # ------------------------------ POST /sfx-cues/{id}/placement:recompute
    def recompute_placement(self, tenant_id: str, cue_id: str,
                            edl_revision: int) -> dict:
        """§6.3 / §4.3。**纯计算，不花钱、不碰 S26。**

        这就是"跟着剪辑走"的那一半：剪辑发新版 EDL，重算一次就完了。
        """
        cue = _row(self.conn.execute(
            "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
            "ORDER BY cue_revision DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())
        if cue is None:
            raise ServiceError(404, "SFX_CUE_NOT_FOUND", f"no cue {cue_id}")

        takes = self.conn.execute(
            "SELECT t.*, j.requested_duration_ms FROM sfx_take t "
            "JOIN sfx_job j ON j.sfx_job_id=t.sfx_job_id WHERE t.tenant_id=? "
            "AND t.cue_id=? AND t.state IN ('READY','ACCEPTED')",
            (tenant_id, cue_id)).fetchall()
        if not takes:
            raise ServiceError(409, "NO_TAKE_TO_PLACE",
                               "cue has no READY take to place")

        results = []
        now = _now()
        for take_row in takes:
            take = dict(take_row)
            placement = self._placement_of(tenant_id=tenant_id, take=take,
                                           cue=cue, edl_revision=edl_revision)
            # 缓存只是省算力的副本，写失败也不影响正确性。
            self.conn.execute(
                "INSERT OR REPLACE INTO cue_placement_cache (tenant_id, "
                "sfx_take_id, edl_revision, placement_mode, output_start_ms, "
                "output_end_ms, source_in_ms, source_out_ms, stretch_ratio, "
                "reason, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, take["sfx_take_id"], int(edl_revision),
                 placement["placement_mode"], placement.get("output_start_ms"),
                 placement.get("output_end_ms"), placement.get("source_in_ms"),
                 placement.get("source_out_ms"), placement.get("stretch_ratio"),
                 placement.get("reason"), now))
            results.append({"sfx_take_id": take["sfx_take_id"], **placement})

        return {"cue_id": cue_id, "edl_revision": int(edl_revision),
                "placements": results}

    # ------------------------------------------------ POST /sfx-gaps/{id}:waive
    def waive_gap(self, tenant_id: str, gap_id: str, body: dict,
                  command_id: str) -> dict:
        """§6.3。**S33 不自己豁免自己**：必须带 S40 的裁决引用。"""
        decision = body.get("decision_ref") or {}
        if not isinstance(decision, dict) or decision.get("service_id") != "S40" \
                or not decision.get("id"):
            raise ServiceError(
                422, "DECISION_REF_REQUIRED",
                "waiving a gap requires an S40 decision_ref; S33 never waives "
                "its own gap")

        def produce() -> dict:
            gap = _row(self.conn.execute(
                "SELECT * FROM sfx_gap WHERE tenant_id=? AND gap_id=?",
                (tenant_id, gap_id)).fetchone())
            if gap is None:
                raise ServiceError(404, "SFX_GAP_NOT_FOUND", f"no gap {gap_id}")
            if gap["state"] != "open":
                raise ServiceError(409, "GAP_NOT_OPEN",
                                   f"gap is {gap['state']}, cannot waive")
            now = _now()
            self.conn.execute(
                "UPDATE sfx_gap SET state='waived', waived_decision_id=?, "
                "updated_at=? WHERE gap_id=?", (_jcs(decision), now, gap_id))
            outbox = self.outbox
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_GAP_CLOSED,
                subject=f"sfx-gap/{gap_id}", tenant_id=tenant_id,
                home_cell="sfx", aggregate_id=gap["shot_lineage_id"],
                sequence=1,
                correlation_id=gap_id,
                data={"gap_id": gap_id, "closed_as": "waived",
                      "decision_ref": decision},
                schema_version=SCHEMA_VERSION))
            return {"gap_id": gap_id, "state": "waived",
                    "waived_decision_id": decision["id"]}

        return self._command(tenant_id, command_id, produce)

    # ----------------------------------------------- POST /sfx-jobs/{id}:cancel
    def cancel_job(self, tenant_id: str, job_id: str, body: dict,
                   command_id: str) -> dict:
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] in TERMINAL_STATES:
                raise ServiceError(409, "JOB_ALREADY_TERMINAL",
                                   f"job is {job['state']}")
            outbox = self.outbox
            if job["provider_job_id"]:
                try:
                    self.canceler(job["provider_job_id"], body.get("reason"))
                except (ServiceError, ConnectionError):
                    # 供应商没确认取消 → 进 WAITING_UNKNOWN，**不能**直接标
                    # CANCELED。标了就等于说"确定没在做"，而它可能正在做。
                    job = self._transition(
                        job, "WAITING_UNKNOWN", reason_code="CANCEL_UNCONFIRMED",
                        detail="provider did not confirm cancellation",
                        outbox=outbox)
                    return {"sfx_job_id": job_id, "state": job["state"],
                            "reconciled": False,
                            "note": "cancellation unconfirmed; must reconcile"}
            job = self._transition(job, "CANCELED",
                                   reason_code=body.get("reason") or "REQUESTED",
                                   outbox=outbox)
            return {"sfx_job_id": job_id, "state": "CANCELED"}

        return self._command(tenant_id, command_id, produce)

    # ------------------------------------------------ POST /sfx-jobs/{id}:retry
    def retry_job(self, tenant_id: str, job_id: str, body: dict,
                  command_id: str) -> dict:
        """§6.3。只有 `FAILED` / `RECONCILED_FAILED` 能重试。

        `POLICY_BLOCKED` 要等策略改，`WAITING_UNKNOWN` 必须先对账（盲目重提
        付费任务正是 AGENTS.md 的红线），`GAP_RECORDED` 是缺口不是失败。
        """
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] not in RETRYABLE_STATES:
                raise ServiceError(
                    409, "JOB_NOT_RETRYABLE",
                    f"job is {job['state']}; only {sorted(RETRYABLE_STATES)} "
                    "can be retried",
                    details={"state": job["state"]})
            self.conn.execute(
                "UPDATE sfx_job SET business_attempt_no=business_attempt_no+1, "
                "variation_nonce=?, failure_code=NULL, failure_detail=NULL, "
                "updated_at=? WHERE sfx_job_id=?",
                (uuid.uuid4().hex, _now(), job_id))
            job = self._job(tenant_id, job_id)
            outbox = self.outbox
            job = self._transition(job, "PREPARING",
                                   reason_code="RETRY", outbox=outbox)
            cue = _row(self.conn.execute(
                "SELECT * FROM sfx_cue_revision WHERE tenant_id=? AND cue_id=? "
                "AND cue_revision=?", (tenant_id, job["cue_id"],
                                       int(job["cue_revision"]))).fetchone())
            return self._submit(job, {**cue}, outbox,
                                fingerprint=job["content_fingerprint"],
                                requested_ms=int(job["requested_duration_ms"]))

        return self._command(tenant_id, command_id, produce)

    # ================================================ 事件消费者
    def on_generation_state_changed(self, envelope: dict) -> dict:
        """S26 `generation.job.state-changed`。

        **防御式映射**：`state` 在契约里是普通 string（16 个值只写在描述里，
        不是闭集），所以这里不假设自己认识每一个值。不认识的一律不动 job，
        返回 `ignored`——把未知状态当成"成功"是这条链路最贵的一种错。
        """
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        provider_job_id = data.get("provider_job_id") or data.get("job_id")
        provider_state = data.get("state")
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_a_generation_job_event"}

        job = _row(self.conn.execute(
            "SELECT * FROM sfx_job WHERE tenant_id=? AND provider_job_id=?",
            (tenant_id, provider_job_id)).fetchone())
        if job is None:
            return {"ignored": True, "reason": "no_matching_sfx_job"}

        if provider_state == "SUBMISSION_UNKNOWN":
            with Transaction(self.conn):
                outbox = self.outbox
                new = self._transition(job, "WAITING_UNKNOWN",
                                       reason_code="SUBMISSION_UNKNOWN",
                                       detail="provider outcome unknown; "
                                              "must reconcile before resubmit",
                                       outbox=outbox)
            return {"sfx_job_id": new["sfx_job_id"], "state": new["state"]}

        if provider_state in ("PROVIDER_SUCCEEDED", "INGESTING"):
            with Transaction(self.conn):
                outbox = self.outbox
                new = self._transition(job, "INGESTING",
                                       reason_code="PROVIDER_SUCCEEDED",
                                       outbox=outbox)
            return {"sfx_job_id": new["sfx_job_id"], "state": new["state"]}

        if provider_state in ("PROVIDER_FAILED", "REJECTED_BUDGET",
                              "REJECTED_QUOTA", "CANCELED"):
            failure = data.get("reason_code") or provider_state
            with Transaction(self.conn):
                outbox = self.outbox
                self.conn.execute(
                    "UPDATE sfx_job SET failure_code=?, updated_at=? "
                    "WHERE sfx_job_id=?", (failure, _now(), job["sfx_job_id"]))
                new = self._transition(job, "FAILED", reason_code=failure,
                                       outbox=outbox)
            return {"sfx_job_id": new["sfx_job_id"], "state": new["state"]}

        return {"ignored": True, "reason": f"unmapped_provider_state:"
                                           f"{provider_state}"}

    def on_artifact_ingested(self, envelope: dict) -> dict:
        """S28 `artifact.ingested` → 落 `SfxTake`。

        **C4**：这个事件里没有 `natural_duration_ms`，`duration_ms` 还是可选
        的。拿不到时长就是 F14：`duration_ms` 缺失时**不能**当 0、也不能当
        "未知但先放着"——0ms 的素材会被 place() 判成 unplaceable，成为一个
        看起来正常的缺口。
        """
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        artifact_ref = data.get("artifact_ref") or {}
        provider_job_id = (data.get("source") or {}).get("provider_job_id") \
            or data.get("provider_job_id")
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_shaped_for_sfx"}

        job = _row(self.conn.execute(
            "SELECT * FROM sfx_job WHERE tenant_id=? AND provider_job_id=?",
            (tenant_id, provider_job_id)).fetchone())
        if job is None:
            return {"ignored": True, "reason": "no_matching_sfx_job"}

        duration_ms = data.get("duration_ms")
        with Transaction(self.conn):
            outbox = self.outbox
            if not isinstance(duration_ms, int) or duration_ms <= 0:
                self._record_gap(
                    tenant_id=tenant_id,
                    shot_lineage_id=job["shot_lineage_id"],
                    cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                    sfx_family=job["sfx_family"],
                    kind="provider_failed",
                    detail="artifact.ingested carried no usable duration_ms "
                           "(C4); cannot size the take",
                    source_job_id=job["sfx_job_id"], outbox=outbox)
                new = self._transition(job, "GAP_RECORDED",
                                       reason_code="DURATION_UNKNOWN",
                                       outbox=outbox)
                return {"sfx_job_id": new["sfx_job_id"], "state": new["state"]}

            loopable = bool(job["loopable_required"])
            verified = False
            if loopable:
                try:
                    probe = self.loop_probe(artifact_ref)
                except (ServiceError, ConnectionError):
                    probe = None
                if probe is True:
                    verified = True
                else:
                    # 声明要循环但验不了 → 缺口，不是"先当能用"。
                    take_id = self._insert_take(
                        job, artifact_ref, duration_ms, loopable=False,
                        verified=False, rights_state="usable",
                        source_kind="generated")
                    self.conn.execute(
                        "UPDATE sfx_take SET rights_state='void' "
                        "WHERE sfx_take_id=?", (take_id,))
                    self._record_gap(
                        tenant_id=tenant_id,
                        shot_lineage_id=job["shot_lineage_id"],
                        cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                        sfx_family=job["sfx_family"],
                        kind="loopability_unverified",
                        detail="take declared loopable but loopability could "
                               "not be verified",
                        source_job_id=job["sfx_job_id"], outbox=outbox)
                    new = self._transition(job, "GAP_RECORDED",
                                           reason_code="LOOPABILITY_UNVERIFIED",
                                           outbox=outbox)
                    return {"sfx_job_id": new["sfx_job_id"],
                            "state": new["state"], "sfx_take_id": take_id}

            take_id = self._insert_take(job, artifact_ref, duration_ms,
                                        loopable=loopable, verified=verified,
                                        rights_state="usable",
                                        source_kind="generated")
            # T33-40：检查点是 CAS。并发两个 job 抢同一个 (shot, cue_index)
            # 时只有一个能推进序号，另一个重读后重试——不是两边都写。
            self._advance_checkpoint(job, take_id)
            # 新 take 来了 → 显式关闭这条 cue 的开放缺口（§4.6 末段）。
            self.conn.execute(
                "UPDATE sfx_gap SET state='closed', closed_by_take_id=?, "
                "updated_at=? WHERE tenant_id=? AND cue_id=? AND state='open'",
                (take_id, _now(), tenant_id, job["cue_id"]))
            new = self._transition(job, "READY", outbox=outbox)
            self._publish_ready(new, take_id, artifact_ref, duration_ms,
                                loopable=loopable, outbox=outbox)
            return {"sfx_job_id": new["sfx_job_id"], "state": new["state"],
                    "sfx_take_id": take_id}

    def _insert_take(self, job: dict, artifact_ref: dict, duration_ms: int,
                     *, loopable: bool, verified: bool, rights_state: str,
                     source_kind: str) -> str:
        take_id = _new_id("take")
        self.conn.execute(
            "INSERT INTO sfx_take (sfx_take_id, tenant_id, sfx_job_id, cue_id, "
            "shot_lineage_id, cue_index, sfx_family, artifact_ref, "
            "natural_duration_ms, sample_rate_hz, channel_count, "
            "container_format, content_fingerprint, loopable, loopable_verified, "
            "rights_policy_binding_id, rights_state, rights_checked_at, "
            "provider_job_id, attempt_id, source_kind, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'READY',?)",
            (take_id, job["tenant_id"], job["sfx_job_id"], job["cue_id"],
             job["shot_lineage_id"], int(job["cue_index"]), job["sfx_family"],
             _jcs(artifact_ref), int(duration_ms),
             int(artifact_ref.get("sample_rate_hz") or 48000),
             int(artifact_ref.get("channel_count") or 1),
             artifact_ref.get("container") or "audio/wav",
             job["content_fingerprint"], 1 if loopable else 0,
             1 if verified else 0, job["rights_policy_binding_id"],
             rights_state, _now(), job["provider_job_id"],
             job["attempt_id"] or "", source_kind, _now()))
        return take_id

    def _advance_checkpoint(self, job: dict, take_id: str) -> int:
        """CAS 推进检查点。`expected_revision` 是乐观锁那一位。"""
        current = _row(self.conn.execute(
            "SELECT * FROM sfx_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND cue_index=?",
            (job["tenant_id"], job["shot_lineage_id"],
             int(job["cue_index"]))).fetchone())
        now = _now()
        if current is None:
            self.conn.execute(
                "INSERT INTO sfx_checkpoint (tenant_id, shot_lineage_id, "
                "cue_index, sfx_take_id, checkpoint_seq, expected_revision, "
                "updated_by_job, updated_at) VALUES (?,?,?,?,1,0,?,?)",
                (job["tenant_id"], job["shot_lineage_id"],
                 int(job["cue_index"]), take_id, job["sfx_job_id"], now))
            return 1
        seq = int(current["checkpoint_seq"]) + 1
        cur = self.conn.execute(
            "UPDATE sfx_checkpoint SET sfx_take_id=?, checkpoint_seq=?, "
            "expected_revision=expected_revision+1, updated_by_job=?, "
            "updated_at=? WHERE tenant_id=? AND shot_lineage_id=? AND "
            "cue_index=? AND checkpoint_seq=?",
            (take_id, seq, job["sfx_job_id"], now, job["tenant_id"],
             job["shot_lineage_id"], int(job["cue_index"]),
             int(current["checkpoint_seq"])))
        if cur.rowcount != 1:
            raise ServiceError(409, "CHECKPOINT_CAS_FAILED",
                               "concurrent checkpoint advance lost the race; "
                               "retry the ingest", retryable=True)
        return seq

    def _publish_ready(self, job: dict, take_id: str, artifact_ref: dict,
                       natural_duration_ms: int, *, loopable: bool,
                       outbox: Outbox) -> None:
        """`production.sfx.ready`，**16 个必填字段一个不少**（schema
        `required` 全列且 `additionalProperties: false`）。"""
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM sfx_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND cue_index=?",
            (job["tenant_id"], job["shot_lineage_id"],
             int(job["cue_index"]))).fetchone())
        prev_seq = int(checkpoint["checkpoint_seq"]) if checkpoint else 0
        new_seq = prev_seq + 1

        outbox.append(new_event(
            source=SOURCE, type=EVENT_SFX_READY,
            subject=f"sfx-take/{take_id}", tenant_id=job["tenant_id"],
            home_cell="sfx", aggregate_id=job["shot_lineage_id"],
            sequence=new_seq,
            correlation_id=job["sfx_job_id"],
            data={
                "schema_version": SCHEMA_VERSION,
                "sfx_job_id": job["sfx_job_id"],
                "sfx_take_id": take_id,
                "cue_id": job["cue_id"],
                "cue_revision": int(job["cue_revision"]),
                "shot_lineage_id": job["shot_lineage_id"],
                "cue_index": int(job["cue_index"]),
                "sfx_family": job["sfx_family"],
                "artifact_ref": artifact_ref,
                "natural_duration_ms": int(natural_duration_ms),
                "loopable": bool(loopable),
                "rights_state": "usable",
                "source_kind": "generated",
                "checkpoint": {
                    "from": checkpoint["sfx_take_id"] if checkpoint else None,
                    "to": take_id,
                    "checkpoint_seq": new_seq,
                },
                "content_fingerprint": job["content_fingerprint"],
                "gap_state": "none",
            },
            schema_version=SCHEMA_VERSION))

    # ================================================ 许可复查
    def recheck_take_rights(self, tenant_id: str, take_id: str) -> dict:
        """**每一次使用都要复查许可**（§4.5）。

        这个 `GET` 是"知情副作用"：它只可能把 usable 变 void，永远不反向。
        许可失效后素材不能再用，缺口的产生与 take 的失效必须同事务。
        """
        take = _row(self.conn.execute(
            "SELECT * FROM sfx_take WHERE tenant_id=? AND sfx_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "SFX_TAKE_NOT_FOUND", f"no take {take_id}")

        binding = _row(self.conn.execute(
            "SELECT * FROM rights_policy_binding WHERE "
            "rights_policy_binding_id=?",
            (take["rights_policy_binding_id"],)).fetchone())
        try:
            resolved = self.rights_resolver(json.loads(binding["policy_ref"]),
                                            tenant_id)
        except (ServiceError, ConnectionError):
            # 查不到许可 ≠ 许可失效。保持现状并如实报告没查到。
            return {**take, "rights_state": take["rights_state"],
                    "rights_checked": False}
        if resolved and not (resolved.get("allowed_license_kinds") or []):
            with Transaction(self.conn):
                outbox = self.outbox
                self.conn.execute(
                    "UPDATE sfx_take SET rights_state='void', rights_checked_at=? "
                    "WHERE sfx_take_id=?", (_now(), take_id))
                self._record_gap(
                    tenant_id=tenant_id,
                    shot_lineage_id=take["shot_lineage_id"],
                    cue_index=int(take["cue_index"]), cue_id=take["cue_id"],
                    sfx_family=take["sfx_family"], kind="rights_lapsed",
                    detail="licensed route disappeared; take is void",
                    source_job_id=take["sfx_job_id"], outbox=outbox)
            return {**self.get_take(tenant_id, take_id), "rights_checked": True}
        return {**take, "rights_checked": True}

    # ================================================ 对账
    def reconcile_unknown(self, tenant_id: str, job_id: str,
                          provider_outcome: dict | None) -> dict:
        """`WAITING_UNKNOWN` 的出口。**不重提付费任务**，只收敛成已知状态。

        这就是 AGENTS.md 那条红线的落点：`SUBMISSION_UNKNOWN` 下重提会花两次钱，
        而"到底提交成没成"只有供应商能答。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "WAITING_UNKNOWN":
            raise ServiceError(409, "JOB_NOT_IN_UNKNOWN",
                               f"job is {job['state']}")
        with Transaction(self.conn):
            outbox = self.outbox
            if provider_outcome is None:
                # 还是不知道。**保持原状**，不改 state，不重提。
                return {"sfx_job_id": job_id, "state": job["state"],
                        "reconciled": False,
                        "note": "provider outcome still unknown; not resubmitted"}
            if provider_outcome.get("succeeded"):
                self.conn.execute(
                    "UPDATE sfx_job SET reconciled=1, updated_at=? "
                    "WHERE sfx_job_id=?", (_now(), job_id))
                job = self._job(tenant_id, job_id)
                new = self._transition(job, "RECONCILED_READY",
                                       reason_code="RECONCILED_SUCCESS",
                                       outbox=outbox)
                return {"sfx_job_id": job_id, "state": new["state"],
                        "reconciled": True}
            self.conn.execute(
                "UPDATE sfx_job SET reconciled=1, updated_at=? WHERE sfx_job_id=?",
                (_now(), job_id))
            job = self._job(tenant_id, job_id)
            # T33-34 / F13：对账确认没成 → 缺口 + GAP_RECORDED。它**不是**
            # FAILED：FAILED 可以 :retry 重提付费任务，而对账失败意味着
            # "不知道供应商侧有没有残留"，重提是错的。
            self._record_gap(
                tenant_id=tenant_id,
                shot_lineage_id=job["shot_lineage_id"],
                cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                sfx_family=job["sfx_family"],
                kind="submission_unrecoverable",
                detail="reconciliation confirmed the submission did not "
                       "succeed; manual decision required before any resubmit",
                source_job_id=job_id, outbox=outbox)
            new = self._transition(job, "GAP_RECORDED",
                                   reason_code="RECONCILED_FAILURE",
                                   outbox=outbox)
            return {"sfx_job_id": job_id, "state": new["state"],
                    "reconciled": True}
