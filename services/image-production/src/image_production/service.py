"""S29 内核。方案：`plans/S29-image-production.md`。

一句话边界：**S29 决定"要哪张图"，不决定"花多少钱、走哪条路、能不能派单"。**

把三层分开说清楚，因为这是本文件最容易被写坏的地方：

- **意图层**（要哪张图：参考组合、prompt、尺寸、node_kind）由 S29 拥有。
  `input_fingerprint` 冻结它，业务幂等键建在它上面。
- **花费层**（余额、报价、reservation）归 S23，**配额与派单**归 S22/S27，
  **选型**归 S25。S29 只保存 `route_decision_ref` 的 `{id, revision,
  content_hash}`，不复制 `eligible[]`、不缓存"预算已批"。任何一层被抄进本库
  都会变成第二个权威（AGENTS.md）。
- **事实层**（这张图 ready 了、检查点指向它）由 S29 发布为事实，
  `production.image.ready` 带 `checkpoint_seq`。谁作废、怎么传播归 S09/S30/S08。

八条不能违反的语义：

1. **付费只走 S26。** 本模块没有任何供应商 URL / base url；唯一的付费出口是
   注入的 `submitter`，它 POST `/generation-jobs`。直连模型是缺陷，不是优化。
2. **`SUBMISSION_UNKNOWN` 下绝不重提。** 状态机没有 `SUBMISSION_UNKNOWN`
   → 可推进的边；业务幂等键在 UNKNOWN 期间仍占位（`image_job_open_uniq`
   把 UNKNOWN 算未终结），所以"再建一个 job 试试"会撞唯一约束。
3. **预算/配额在派单时由 S26 的准入 Saga 现查。** S29 库里没有"预算已批"
   这一列，每次提交都是一次新的 S26 调用，F6/F7 的拒绝由 S26 当场回，S29
   只透传成 `FAILED(BUDGET|QUOTA)`，且**不 +1**（没花钱）。
4. **检查点不是 S08 的接受指针。** 它只回答"下游该拿哪张图"。推进它不改
   任何 accepted pointer；两者用各自的 CAS。
5. **候选不可变。** `keyframe_edit` 产出新候选，旧候选保留为历史证据；
   不出现"同一候选的新 revision"。
6. **`shot_lineage_id` 只能来自上游。** 生成它 = 跨 Run 计数清零 = 已消耗
   费用被绕过（§8.2）。同一 `shot_ref.id` 换个 lineage 再出现 → 拒绝。
7. **`strategy_class` 原样透传。** 计算规则归 S39；S29 算一套必然与它不一致。
8. **不判"作废"。** 只发事实；影响闭包是 S09 的。

事务与幂等与 S33/S34 同构：命令事务内把结果写回同一个 `idempotency_record`
行，提交与"记下已提交"原子成立。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    CONTRACT_GAPS,
    ID_PREFIX,
    JOB_STATES,
    MAX_EDIT_WINDOW_FRAMES,
    NODE_KINDS,
    REFERENCE_ROLES,
    RETRYABLE_STATES,
    ROLES_NOT_APPLICABLE_FOR_IMAGE,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
)

SOURCE = "S29"
SCHEMA_VERSION = "2.0"

# 已登记事件（`contracts/event-topics.v2.json` 里 S29 只声明了这一个）。
EVENT_IMAGE_READY = "production.image.ready"
TOPIC_IMAGE = "drama.image-production.v2"

# 未登记事件（§6.4 自己列的另外三个，缺口 C2）。
LOCAL_JOB_STATE = "image.job.state-changed"
LOCAL_CANDIDATE_REJECTED = "image.candidate.rejected"
LOCAL_KEYFRAME_REFRESH_FAILED = "image.keyframe-refresh-failed"

# `execution-context.v2` 的 18 个必填键，逐字来自契约。少一个 S26 就 400，
# 而 S29 无权替调用方补——补出来的每个值都是伪造签名。
EXECUTION_CONTEXT_REQUIRED = (
    "schema_version", "tenant_id", "project_id", "series_id", "episode_id",
    "branch_id", "node_id", "node_epoch", "execution_fence", "manifest_ref",
    "memory_checkpoint", "policy_ref", "actor", "home_cell", "home_region",
    "trace_id", "command_id", "request_hash",
)

# §2.4：`char_linlan@3` —— 角色 ID + 版本。契约没给 grammar，这里取最紧的一种
# 可解析形式；解析不了就 422，不猜。
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}@[1-9][0-9]*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# 提交 S26 时的状态映射（S26 的 16 态 → 本服务的动作）。
_S26_SUCCESS_STATES = frozenset({"PROVIDER_SUCCEEDED", "INGESTING", "ARCHIVED"})
_S26_UNKNOWN_STATES = frozenset({"SUBMISSION_UNKNOWN", "RECONCILING"})


# ------------------------------------------------------------ 基础设施助手

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return _jcs(value if value is not None else {})


def _parse_ts(value: str) -> datetime:
    """接受 `Z` 与 `+08:00`。字符串比较会把"还有一小时"读成"已过期"。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_prompt(text: str) -> str:
    """空白归一。**不**做同义改写：改写 prompt 是 S20 的事，改一个字就是另一张图。"""
    return " ".join(str(text).split())


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。**可重入**，只有最外层真正 BEGIN/COMMIT。

    为什么必须可重入：事件消费者要把"记下这个 `event_id` 已经消费过"和"改 job
    状态"放进**同一个**事务。如果去重行先独立提交、状态改到一半进程挂了，这条
    事件就永久丢了（`inbox` 里已经有它，重投会被当成 DUPLICATE）。可重入让内层
    的 `_transition` / `_finalize` 自动挂在外层上，异常冒到最外层才 ROLLBACK——
    去重行跟着回滚，事件会被重投。

    SQLite 不支持嵌套 `BEGIN`，所以内层只记账、不发起事务。这与 S26 的写法一致
    （`services/generation-jobs` 的 `Transaction` 也是深度计数）。
    """

    def __init__(self, conn: sqlite3.Connection, depth: list[int] | None = None):
        self.conn = conn
        self.depth = depth if depth is not None else [0]
        self.outermost = False

    def __enter__(self):
        self.outermost = self.depth[0] == 0
        if self.outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self.depth[0] += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth[0] -= 1
        if not self.outermost:
            return False
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：寻址

def content_hash(references: list, identity_constraints: list) -> str:
    """`ReferenceCombination` 的内容地址（§2.4）。

    **顺序有语义**：`[A,B]` 与 `[B,A]` 必须是两个 hash（T29-05）——首帧与风格图
    调换位置会生成完全不同的构图。而 `identity_constraints` 是集合，排序后参与
    hash（T29-06），否则同一组约束的两种写法会买到两张图。

    `ordinal` 不参与：它是给人读的序号，数组位置已经表达了顺序；把序号也编进
    去会让"同一组参考换个编号"变成新 hash。
    """
    ordered = [{"artifact_id": r["artifact_id"], "sha256": r["sha256"],
                "role": r["role"]} for r in references]
    payload = {"references": ordered,
               "identity_constraints": sorted(identity_constraints)}
    return _sha256_hex(_jcs(payload))


def input_fingerprint(*, reference_content_hash: str, prompt_intent: str,
                      mode: str, width: int, height: int,
                      identity_constraints: list) -> str:
    """`(tenant, shot_lineage_id, node_kind, input_fingerprint)` 的那一段。

    含参考组合 hash、归一化 prompt、mode、尺寸、排序后的约束。**不含**
    `command_id` / `trace_id` / `deadline` / `budget_scope_id`：那些是这一次
    调用怎么走，不是"要哪张图"。含进去会让同一次重试变成新意图，绕开业务
    幂等键去付第二次钱。
    """
    payload = {
        "reference_combination": reference_content_hash,
        "prompt_intent": _normalize_prompt(prompt_intent),
        "mode": mode,
        "width": int(width),
        "height": int(height),
        "identity_constraints": sorted(identity_constraints),
    }
    return _sha256_hex(_jcs(payload))


def canonical_transition(from_state: str | None, to_state: str,
                         *, allow_retry: bool = False) -> bool:
    """表驱动状态机（§3.1）。未知状态一律不合法——不是"默认放行"。"""
    if from_state is not None and from_state not in JOB_STATES:
        return False
    if to_state not in JOB_STATES:
        return False
    if to_state in TRANSITIONS.get(from_state, frozenset()):
        return True
    # 重试是唯一允许的"终态回退"：它不推进旧事实，而是把同一 intent 再试一次
    # （§6.3）。没有 `allow_retry` 的调用方走不到这里。
    if allow_retry and from_state in RETRYABLE_STATES and to_state == "PREPARING":
        return True
    return False


# ---------------------------------------------------------------- 服务

def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _provider_state_of(local_state: str) -> str:
    """本地状态 → S26 侧状态的**确定映射**（C29-11）。

    只是投影，不是权威：真正的权威值只有 S26 的 `GET /generation-jobs/{id}`
    能给。GET 不额外发请求（那会把一次读变成一次对上游的依赖），所以响应里
    同时给 `provider_state_source="local_map"`，让读者知道这是推导值。
    """
    return {
        "CREATED": "CREATED",
        "PREPARING": "RESERVING",
        "SUBMITTED": "SUBMITTING",
        "WAITING_PROVIDER": "RUNNING",
        "WAITING_UNKNOWN": "SUBMISSION_UNKNOWN",
        "INGESTING": "INGESTING",
        "READY": "ARCHIVED",
        "RECONCILED_READY": "ARCHIVED",
        "REJECTED_LOCAL": "REJECTED",
        "FAILED": "PROVIDER_FAILED",
        "RECONCILED_FAILED": "REJECTED",
        "CANCELED": "CANCELED",
    }.get(local_state, "UNKNOWN")


class ImageProductionService:
    def __init__(self, conn: sqlite3.Connection, *, route_resolver, submitter,
                 artifact_check, artifact_importer, generation_state, canceler):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._tx_depth = [0]
        # `Outbox.__init__` / `Inbox.__init__` 内部都会 executescript，而
        # sqlite3 的 executescript 会**隐式提交当前事务**。所以只能在任何事务
        # 之外建一次；否则命令事务会在第一条事件写入时被悄悄提交，
        # 后面的 ROLLBACK 就失效了。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.artifact_check = artifact_check
        self.artifact_importer = artifact_importer
        self.generation_state = generation_state
        self.canceler = canceler

    # -------------------------------------------------------- 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        """一个命令一个事务；结果**在同一个事务里**写回幂等记录。

        平台幂等 SDK 的 `begin/finish` 是裸语句，不在我们的 `BEGIN IMMEDIATE`
        里。若把 `finish` 留到事务外，会存在"业务已提交、幂等未记录"的窗口，
        重试就会再付一次钱。这里直接把 `result` 覆盖进同一行。
        """
        with Transaction(self.conn, self._tx_depth):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """上游不可达 → 503 retryable；4xx 原样上抛（它是"明确说了不行"）。"""
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    def _transition(self, job: dict, to_state: str, *, reason_code=None,
                    detail=None, outbox: Outbox | None = None,
                    allow_retry: bool = False) -> dict:
        if not canonical_transition(job["state"], to_state,
                                    allow_retry=allow_retry):
            # 状态机违例是 bug，不是用户错误：让它响亮地失败，而不是写下一个
            # 谁都解释不了的状态。
            raise AssertionError(
                f"illegal image job transition {job['state']} -> {to_state}")
        seq = int(job["state_seq"]) + 1
        now = _now()
        # 这里**不写** failure_code / failure_detail：`reason_code` 是"为什么走
        # 这一步"（RETRY、ARTIFACT_INGESTED、PROVIDER_SUCCEEDED…），不是失败。
        # 早先写成 COALESCE 会把成功路径的原因码刻进失败字段，`retry_job` 清完
        # 又立刻被 RETRY 重新写上，读口就永远报一个不存在的失败。
        # 真正的失败码由 `_fail` / `_reject_local` / 对账分支各自显式写入。
        self.conn.execute(
            "UPDATE image_job SET state=?, state_seq=?, updated_at=?, "
            "state_entered_at=? WHERE image_job_id=?",
            (to_state, seq, now, now, job["image_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (image_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["image_job_id"], seq, job["state"], to_state, reason_code,
             detail, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_JOB_STATE,
                subject=f"image-job/{job['image_job_id']}",
                tenant_id=job["tenant_id"], home_cell="image-production",
                aggregate_id=job["image_job_id"], sequence=seq,
                correlation_id=job["image_job_id"],
                data={"image_job_id": job["image_job_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "node_kind": job["node_kind"],
                      "from": job["state"], "to": to_state,
                      "reason_code": reason_code},
                schema_version=SCHEMA_VERSION))
        return {**job, "state": to_state, "state_seq": seq}

    def _job(self, tenant_id: str, job_id: str) -> dict:
        job = _row(self.conn.execute(
            "SELECT * FROM image_job WHERE tenant_id=? AND image_job_id=?",
            (tenant_id, job_id)).fetchone())
        if job is None:
            raise ServiceError(404, "IMAGE_JOB_NOT_FOUND", f"no job {job_id}")
        return job

    # ---------------------------------------------------------- 校验（§6.1）
    def _validate(self, body: dict, tenant_id: str) -> dict:
        """§6.1 的九步。**顺序即方案顺序**：先便宜、先无副作用、先本地。

        两处刻意与"友好"相反：

        - 跨租户参考返回 **404 且响应体与"不存在"完全一致**（T29-03）。403
          会告诉调用方"这个 id 存在，只是不是你的"——那本身就是泄漏。
        - `deadline` 用解析后的时刻比较，不用字符串序（带偏移的串会错位）。
        """
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be an object")

        # ---- 1/2：context 完整性 + 签名绑定
        ctx = body.get("context")
        if not isinstance(ctx, dict):
            raise ServiceError(422, "INVALID_CONTEXT", "context must be an object")
        if ctx.get("tenant_id") != tenant_id:
            # AGENTS.md：body / LLM 提供的 tenant_id 不是可信输入。
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id does not match the credential")
        missing = [k for k in EXECUTION_CONTEXT_REQUIRED if ctx.get(k) is None]
        if missing:
            raise ServiceError(
                422, "INVALID_CONTEXT",
                f"context is missing: {', '.join(missing)}",
                details={"missing": missing})

        # `built_for.manifest_ref` 在事件里必须是三字段（PIR-3）。这三项只能来自
        # 已签名的 context；缺一个就无法回答"这张图照着哪一版 manifest 造的"，
        # 而那不是 S29 能事后补的信息。
        manifest_ref = ctx.get("manifest_ref")
        if not isinstance(manifest_ref, dict) \
                or not {"id", "revision", "content_hash"} <= set(manifest_ref) \
                or not _HEX64_RE.match(str(manifest_ref.get("content_hash") or "")):
            raise ServiceError(
                422, "INVALID_CONTEXT",
                "context.manifest_ref must carry id, revision and a 64-hex "
                "content_hash (production.image.ready.built_for requires them)")

        # ---- shot_lineage_id（U1 / C4）：必须显式传入，S29 不生成
        shot_lineage_id = body.get("shot_lineage_id")
        if not isinstance(shot_lineage_id, str) or not shot_lineage_id.strip():
            raise ServiceError(
                422, "SHOT_LINEAGE_REQUIRED",
                "shot_lineage_id is required and must come from upstream; S29 "
                "will not generate one (cross-Run attempt counting keys on it)")

        # ---- shot_ref 形状（存在性校验需要 S06，未声明在 depends_on，C29-19）
        shot_ref = body.get("shot_ref")
        if not isinstance(shot_ref, dict):
            raise ServiceError(422, "INVALID_SHOT_REF", "shot_ref must be an object")
        for key in ("service_id", "id", "revision"):
            if shot_ref.get(key) in (None, ""):
                raise ServiceError(422, "INVALID_SHOT_REF",
                                   f"shot_ref.{key} required")
        try:
            int(shot_ref["revision"])
        except (TypeError, ValueError):
            raise ServiceError(422, "INVALID_SHOT_REF",
                               "shot_ref.revision must be an integer")

        # ---- 5：node_kind 闭集（时间结构一致性需要 S06/S35，C29-20）
        node_kind = body.get("node_kind")
        if node_kind not in NODE_KINDS:
            raise ServiceError(422, "INVALID_NODE_KIND",
                               f"node_kind must be one of {sorted(NODE_KINDS)}",
                               details={"node_kind": node_kind})

        # ---- §8.2 断言：同一 shot_ref.id 不能换 lineage
        drift = self.conn.execute(
            "SELECT shot_lineage_id FROM image_job WHERE tenant_id=? AND "
            "json_extract(shot_ref,'$.id')=? AND shot_lineage_id<>? LIMIT 1",
            (tenant_id, str(shot_ref["id"]), shot_lineage_id)).fetchone()
        if drift is not None:
            raise ServiceError(
                409, "SHOT_LINEAGE_DRIFT",
                "this shot_ref.id is already bound to a different "
                "shot_lineage_id; changing lineage resets cross-Run attempt "
                "counting (docs/11 §3)",
                details={"existing_shot_lineage_id": drift["shot_lineage_id"]})

        mode = body.get("mode")
        if mode not in ("generate", "edit"):
            raise ServiceError(422, "INVALID_MODE",
                               "mode must be 'generate' or 'edit'")

        prompt_intent = body.get("prompt_intent")
        if not isinstance(prompt_intent, str) or not _normalize_prompt(prompt_intent):
            raise ServiceError(422, "EMPTY_PROMPT_INTENT",
                               "prompt_intent must be non-blank")

        try:
            width = int(body.get("width"))
            height = int(body.get("height"))
        except (TypeError, ValueError):
            raise ServiceError(422, "INVALID_DIMENSIONS",
                               "width and height must be integers")
        if width <= 0 or height <= 0:
            raise ServiceError(422, "INVALID_DIMENSIONS",
                               "width and height must be > 0")

        # ---- 8：identity_constraints 可解析（(character_id, revision)）
        constraints = body.get("identity_constraints")
        if constraints is None:
            constraints = []
        if not isinstance(constraints, list):
            raise ServiceError(422, "INVALID_IDENTITY_CONSTRAINTS",
                               "identity_constraints must be an array")
        for item in constraints:
            if not isinstance(item, str) or not _IDENTITY_RE.match(item):
                raise ServiceError(
                    422, "INVALID_IDENTITY_CONSTRAINTS",
                    f"identity constraint {item!r} is not '<character_id>@<revision>'")

        # ---- 9：deadline > now（解析后比较，不比字符串）
        deadline = body.get("deadline")
        if not isinstance(deadline, str):
            raise ServiceError(422, "INVALID_DEADLINE", "deadline is required")
        try:
            deadline_at = _parse_ts(deadline)
        except ValueError:
            raise ServiceError(422, "INVALID_DEADLINE",
                               "deadline must be an ISO-8601 timestamp")
        if deadline_at <= datetime.now(timezone.utc):
            raise ServiceError(422, "DEADLINE_IN_PAST",
                               "deadline must be in the future")

        # ---- budget_scope_id：S23 的记账键，**不能默认或现编**
        #
        # 以前这里写 `body.get("budget_scope_id")` 然后落库时 `or "default"`。
        # 那等于给没传 scope 的调用方凭空造一个预算桶：S23 收到 "default" 会
        # 按那个桶记账/预留，于是所有漏传 scope 的租户共用一格预算——既不
        # 隔离也不可审计。S29 不做预算决策（那是 S23 的），所以它只能"要"：
        # 缺了就 422，不替调用方挑一个。
        budget_scope_id = body.get("budget_scope_id")
        if not isinstance(budget_scope_id, str) or not budget_scope_id.strip():
            raise ServiceError(
                422, "INVALID_BUDGET_SCOPE",
                "budget_scope_id is required; S29 does not invent a budget "
                "scope (cost accounting belongs to S23)")

        # ---- route_policy_ref：只做形状校验；选型归 S25
        #
        # 形状必须**完整**校验成 `generation-command.v2` 要求的四键
        # （service_id/id/revision/content_hash，`additionalProperties: false`）。
        # 这里放行一个缺 content_hash 的引用，S26 就会在付费前 400；先本地 422
        # 比让一次已被批准的意图在 S26 门口失败要便宜得多。
        route_policy_ref = body.get("route_policy_ref")
        if not isinstance(route_policy_ref, dict) \
                or route_policy_ref.get("service_id") != "S25":
            raise ServiceError(
                422, "INVALID_ROUTE_POLICY_REF",
                "route_policy_ref must be an S25 reference "
                "(S29 does not choose models; S25 does)")
        if set(route_policy_ref) - {"service_id", "id", "revision",
                                    "content_hash"}:
            raise ServiceError(
                422, "INVALID_ROUTE_POLICY_REF",
                "route_policy_ref accepts only service_id/id/revision/"
                "content_hash; S29 will not forward extra keys into a "
                "signed generation command")
        if not isinstance(route_policy_ref.get("id"), str) \
                or not route_policy_ref["id"]:
            raise ServiceError(422, "INVALID_ROUTE_POLICY_REF",
                               "route_policy_ref.id is required")
        revision = route_policy_ref.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) \
                or revision < 1:
            raise ServiceError(422, "INVALID_ROUTE_POLICY_REF",
                               "route_policy_ref.revision must be an integer "
                               ">= 1")
        content_hash_ref = route_policy_ref.get("content_hash")
        if not isinstance(content_hash_ref, str) \
                or not _HEX64_RE.match(content_hash_ref):
            # **不替调用方算一个 hash**：那份内容 S29 拿不到，算出来的就是
            # 一个没有出处的常量。
            raise ServiceError(
                422, "INVALID_ROUTE_POLICY_REF",
                "route_policy_ref.content_hash must be 64 lowercase hex")

        # ---- references：形状 + 角色闭集 + 跨租户/哈希（S28 只读）
        references = body.get("references")
        if not isinstance(references, list) or not references:
            # §5.2 的 `rc_refs_nonempty` 与 C5 的"无图"问题：纯文生图没有
            # 参考表达，S29 不发明一张占位图。缺参考就拒，登记为 C29-12。
            raise ServiceError(
                422, "REFERENCES_REQUIRED",
                "references must be a non-empty array; S29 will not invent a "
                "placeholder reference for text-only generation")
        normalized_refs = []
        seen_ordinals = set()
        for index, ref in enumerate(references):
            if not isinstance(ref, dict):
                raise ServiceError(422, "INVALID_REFERENCE",
                                   "each reference must be an object")
            artifact_id = ref.get("artifact_id")
            sha256 = ref.get("sha256")
            role = ref.get("role")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ServiceError(422, "INVALID_REFERENCE",
                                   "reference.artifact_id required")
            if not isinstance(sha256, str) or not _HEX64_RE.match(sha256):
                raise ServiceError(422, "INVALID_REFERENCE",
                                   "reference.sha256 must be 64 lowercase hex")
            if role not in REFERENCE_ROLES:
                raise ServiceError(422, "INVALID_REFERENCE_ROLE",
                                   f"reference.role must be one of "
                                   f"{sorted(REFERENCE_ROLES)}")
            if role in ROLES_NOT_APPLICABLE_FOR_IMAGE:
                # C3：图像模型不吃音频/视频参考。本地拒，不替契约选边。
                raise ServiceError(
                    422, "ROLE_NOT_APPLICABLE_FOR_IMAGE",
                    f"role {role!r} is not applicable to the image modality")
            if role == "mask" and mode == "generate":
                raise ServiceError(
                    422, "MASK_ROLE_NOT_ALLOWED_IN_GENERATE",
                    "a mask reference is only meaningful for mode=edit")
            ordinal = ref.get("ordinal", index)
            try:
                ordinal = int(ordinal)
            except (TypeError, ValueError):
                raise ServiceError(422, "INVALID_REFERENCE",
                                   "reference.ordinal must be an integer")
            if ordinal in seen_ordinals:
                raise ServiceError(422, "INVALID_REFERENCE",
                                   f"duplicate reference ordinal {ordinal}")
            seen_ordinals.add(ordinal)
            normalized_refs.append({"ordinal": ordinal,
                                    "artifact_id": artifact_id,
                                    "sha256": sha256, "role": role})
        normalized_refs.sort(key=lambda r: r["ordinal"])

        # ---- edit 分支：window ≤ 6、source 归属、mask 绑定
        edit = None
        if mode == "edit":
            edit = self._validate_edit(body, tenant_id, shot_lineage_id,
                                       normalized_refs)

        return {
            "context": ctx,
            "shot_ref": {"service_id": shot_ref["service_id"],
                         "id": shot_ref["id"],
                         "revision": int(shot_ref["revision"])},
            "shot_lineage_id": shot_lineage_id,
            "node_kind": node_kind,
            "mode": mode,
            "prompt_intent": prompt_intent,
            "width": width,
            "height": height,
            "identity_constraints": list(constraints),
            "references": normalized_refs,
            "route_policy_ref": route_policy_ref,
            "budget_scope_id": budget_scope_id,
            "deadline": deadline,
            "edit": edit,
            "strategy_class": body.get("strategy_class"),
        }

    def _validate_edit(self, body: dict, tenant_id: str,
                       shot_lineage_id: str, references: list) -> dict:
        """§1.4 / §4.2。**6 帧上限在应用层和数据库各判一次**（C6）。"""
        repair_action_id = body.get("repair_action_id")
        if not isinstance(repair_action_id, str) or not repair_action_id:
            # C1：`keyframe_edit` 的执行者是 S39 的 RepairAction。没有它就不是
            # 一次受治理的修复，S29 不接受。
            raise ServiceError(
                422, "KEYFRAME_EDIT_NOT_OWNED",
                "mode=edit requires repair_action_id from an S39 RepairAction")

        source_candidate_id = body.get("source_candidate_id")
        if not isinstance(source_candidate_id, str) or not source_candidate_id:
            raise ServiceError(422, "INVALID_EDIT_SOURCE",
                               "mode=edit requires source_candidate_id")
        source = _row(self.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND candidate_id=?",
            (tenant_id, source_candidate_id)).fetchone())
        if source is None:
            # 别人的候选与不存在同样处理：404，不泄漏。
            raise ServiceError(404, "CANDIDATE_NOT_FOUND",
                               f"no candidate {source_candidate_id}")
        if source["shot_lineage_id"] != shot_lineage_id:
            raise ServiceError(
                422, "EDIT_SOURCE_LINEAGE_MISMATCH",
                "source_candidate belongs to a different shot_lineage_id")
        if source["state"] not in ("READY", "ACCEPTED"):
            raise ServiceError(
                409, "EDIT_SOURCE_NOT_READY",
                f"source candidate is {source['state']}; only READY/ACCEPTED "
                "candidates can be edited")

        mask = body.get("edit_mask")
        if not isinstance(mask, dict):
            raise ServiceError(422, "INVALID_EDIT_MASK",
                               "mode=edit requires edit_mask")
        mask_artifact_id = mask.get("artifact_id")
        mask_sha256 = mask.get("sha256")
        if not isinstance(mask_artifact_id, str) or not mask_artifact_id:
            raise ServiceError(422, "INVALID_EDIT_MASK",
                               "edit_mask.artifact_id required")
        if not isinstance(mask_sha256, str) or not _HEX64_RE.match(mask_sha256):
            raise ServiceError(422, "INVALID_EDIT_MASK",
                               "edit_mask.sha256 must be 64 lowercase hex")
        geometry = mask.get("geometry")
        if not isinstance(geometry, dict) or not isinstance(
                geometry.get("bbox"), list) or len(geometry["bbox"]) != 4:
            raise ServiceError(422, "INVALID_EDIT_MASK",
                               "edit_mask.geometry.bbox must be [x,y,w,h]")

        # `instruction` 可选（C29-22）：`generation-command.v2` 的 image input 是
        # `additionalProperties: false`，没有放编辑指令的位置，所以真正会到达供应
        # 商的文字是 `prompt_intent`。这里存的 instruction 是本地证据，缺省取
        # prompt_intent，不另造一份可能不一致的文本。
        instruction = body.get("instruction") or body.get("prompt_intent")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ServiceError(422, "INVALID_EDIT_INSTRUCTION",
                               "instruction must be non-blank")

        window = body.get("window")
        if not isinstance(window, dict):
            raise ServiceError(422, "KEYFRAME_EDIT_OUT_OF_WINDOW",
                               "mode=edit requires window")
        try:
            start = int(window.get("start_frame"))
            end = int(window.get("end_frame"))
        except (TypeError, ValueError):
            raise ServiceError(422, "KEYFRAME_EDIT_OUT_OF_WINDOW",
                               "window.start_frame / end_frame must be integers")
        fps = window.get("fps")
        if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
            # C29-7：没有帧率就无法解释"6 帧"这个窗口。
            raise ServiceError(422, "KEYFRAME_EDIT_FPS_UNKNOWN",
                               "window.fps is required to interpret the frame window")
        if start < 0 or end < start:
            raise ServiceError(422, "KEYFRAME_EDIT_OUT_OF_WINDOW",
                               "window must satisfy 0 <= start <= end")
        span = end - start + 1
        if span > MAX_EDIT_WINDOW_FRAMES:
            # F13 / T29-16：**校验期就拒，不建 job、不 +1**。
            raise ServiceError(
                422, "KEYFRAME_EDIT_OUT_OF_WINDOW",
                f"window spans {span} frames; the ceiling is "
                f"{MAX_EDIT_WINDOW_FRAMES}",
                details={"span_frames": span,
                         "max_frames": MAX_EDIT_WINDOW_FRAMES})

        # 重复执行的 repair action。唯一索引是最后一道，这里是"返回首次的 job"
        # 的那一道（§7.1）。放在校验期，才能在**写入前**返回 409。
        existing = self.conn.execute(
            "SELECT image_job_id FROM image_job WHERE tenant_id=? AND "
            "repair_action_id=?", (tenant_id, repair_action_id)).fetchone()
        if existing is not None:
            raise ServiceError(
                409, "REPAIR_ACTION_ALREADY_EXECUTED",
                "this repair_action_id has already produced an image job",
                details={"image_job_id": existing["image_job_id"]})

        return {
            "source_candidate_id": source_candidate_id,
            "source_artifact_id": (source["artifact_ref"] or {}).get("id")
            if isinstance(source["artifact_ref"], dict)
            else _loads(source["artifact_ref"], {}).get("id"),
            "mask_artifact_id": mask_artifact_id,
            "mask_sha256": mask_sha256,
            "mask_geometry": geometry,
            "instruction": instruction,
            "repair_action_id": repair_action_id,
            "window_start_frame": start,
            "window_end_frame": end,
            "window_fps": fps,
        }

    # ---------------------------------------------- 参考组合与本地只读校验
    def _check_references(self, tenant_id: str, refs: list) -> None:
        """§6.1 第 3/6 条：跨租户 → 404；同租户 hash 不符/不可读 → 拒绝。

        `artifact_check` 的三态必须分开用（C29-11 的同类理由）：

        - 抛 404 → 不存在或不属于本租户 → 404（T29-03）
        - `False` → artifact 在，但字节不是这一份 → 本地拒绝（T29-04 / F2）
        - `None` → 没查成 → **本地拒绝**（F1）。不知道参考是不是这张图时花钱，
          是把钱押在一个没验过的输入上。
        """
        for ref in refs:
            artifact_ref = {"service_id": "S28", "id": ref["artifact_id"],
                            "revision": 1, "content_hash": ref["sha256"]}
            verdict = self._upstream(
                lambda: self.artifact_check(tenant_id, artifact_ref),
                "ARTIFACT_CHECK_UNAVAILABLE")
            if verdict is True:
                continue
            if verdict is False:
                raise _LocalReject(
                    "REFERENCE_HASH_MISMATCH",
                    f"artifact {ref['artifact_id']} does not match sha256 "
                    f"{ref['sha256']}")
            raise _LocalReject(
                "REFERENCE_UNREADABLE",
                f"could not confirm artifact {ref['artifact_id']}; refusing to "
                "spend on an unverified reference")

    def _upsert_combination(self, tenant_id: str, refs: list,
                            constraints: list) -> dict:
        """内容寻址：同 hash 复用一行，**不建第二行**（§2.4）。"""
        digest = content_hash(refs, constraints)
        existing = _row(self.conn.execute(
            "SELECT * FROM reference_combination WHERE tenant_id=? AND "
            "content_hash=?", (tenant_id, digest)).fetchone())
        if existing is not None:
            return existing
        combo_id = _new_id("combo")
        self.conn.execute(
            "INSERT INTO reference_combination (reference_combination_id, "
            "tenant_id, content_hash, \"references\", identity_constraints, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (combo_id, tenant_id, digest,
             _jcs([{k: r[k] for k in ("ordinal", "artifact_id", "sha256", "role")}
                   for r in refs]),
             _jcs(sorted(constraints)), _now()))
        return _row(self.conn.execute(
            "SELECT * FROM reference_combination WHERE "
            "reference_combination_id=?", (combo_id,)).fetchone())

    # ------------------------------------------------------ POST /image-jobs
    def create_job(self, body: dict, tenant_id: str, command_id: str) -> dict:
        checked = self._validate(body, tenant_id)

        refs = list(checked["references"])
        edit = checked["edit"]
        if edit is not None:
            # §4.2 第 3 步：edit 的参考组合包含被编辑的源候选与 mask。
            base = [r for r in refs
                    if r["artifact_id"] not in (edit["mask_artifact_id"],)]
            refs = [{"ordinal": 0, "artifact_id": edit["source_artifact_id"],
                     "sha256": self._artifact_sha_of(edit["source_candidate_id"],
                                                     tenant_id),
                     "role": "first_frame"},
                    {"ordinal": 1, "artifact_id": edit["mask_artifact_id"],
                     "sha256": edit["mask_sha256"], "role": "mask"}]
            offset = 2
            for r in base:
                if r["artifact_id"] in (edit["source_artifact_id"],
                                        edit["mask_artifact_id"]):
                    continue
                refs.append({"ordinal": offset, **{k: r[k] for k in
                                                   ("artifact_id", "sha256",
                                                    "role")}})
                offset += 1

        # 校验 3/6：只读、无副作用。放在建 job 之前——但拒绝时要留下一个
        # REJECTED_LOCAL 的 job（C29-1），所以在 `produce` 里不抛。
        combo = None
        reject_code = reject_detail = None
        try:
            self._check_references(tenant_id, refs)
        except _LocalReject as exc:
            reject_code, reject_detail = exc.code, str(exc)

        digest = content_hash(refs, checked["identity_constraints"])
        fingerprint = input_fingerprint(
            reference_content_hash=digest,
            prompt_intent=checked["prompt_intent"], mode=checked["mode"],
            width=checked["width"], height=checked["height"],
            identity_constraints=checked["identity_constraints"])

        def produce() -> dict:
            nonlocal combo
            # 业务幂等：同 (tenant, lineage, node_kind, fingerprint) 未终结 →
            # 返回已有 job（T29-11）。**不新建第二个 job**——那会让同一意图
            # 出两份钱、两份 ready、两个检查点竞争。UNKNOWN 期间这条同样成立。
            open_job = _row(self.conn.execute(
                "SELECT * FROM image_job WHERE tenant_id=? AND "
                "shot_lineage_id=? AND node_kind=? AND input_fingerprint=? AND "
                "state NOT IN ('READY','RECONCILED_READY','REJECTED_LOCAL',"
                "'FAILED','RECONCILED_FAILED','CANCELED')",
                (tenant_id, checked["shot_lineage_id"], checked["node_kind"],
                 fingerprint)).fetchone())
            if open_job is not None:
                return self._job_response(open_job,
                                          extra={"duplicate_suppressed": True},
                                          combo_hash=digest)

            combo = self._upsert_combination(tenant_id, refs,
                                             checked["identity_constraints"])
            # mask 必须先于 job 写：`ij_edit_needs_mask` 是**表级 CHECK**，
            # 它在 INSERT 那一刻就要看到非空 edit_mask_id。先建 job 再补 mask
            # 会直接撞 CHECK（不是等到提交才发现）。
            mask_id = self._write_mask(tenant_id, edit) if edit else None
            job_id = _new_id("job")
            now = _now()
            ctx = checked["context"]
            try:
                self.conn.execute(
                    "INSERT INTO image_job (image_job_id, tenant_id, project_id, "
                    "series_id, episode_id, branch_id, shot_ref, "
                    "shot_lineage_id, node_id, node_epoch_at_create, "
                    "execution_fence, node_kind, mode, prompt_intent, width, "
                    "height, identity_constraints, reference_combination_id, "
                    "source_candidate_id, edit_mask_id, repair_action_id, "
                    "strategy_class, input_fingerprint, request_hash, "
                    "idempotency_key, route_decision_ref, route_policy_ref, "
                    "variation_nonce, "
                    "execution_context, budget_scope_id, deadline, "
                    "business_attempt_no, reconciled, state, state_seq, "
                    "created_at, updated_at, state_entered_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "NULL,?,?,?,?,?,?,1,'CREATED',0,?,?,?)",
                    (job_id, tenant_id, ctx["project_id"], ctx["series_id"],
                     ctx["episode_id"], ctx["branch_id"],
                     _jcs(checked["shot_ref"]), checked["shot_lineage_id"],
                     ctx["node_id"], int(ctx["node_epoch"]),
                     int(ctx["execution_fence"]), checked["node_kind"],
                     checked["mode"], checked["prompt_intent"],
                     checked["width"], checked["height"],
                     _jcs(sorted(checked["identity_constraints"])),
                     combo["reference_combination_id"],
                     edit["source_candidate_id"] if edit else None,
                     mask_id,
                     edit["repair_action_id"] if edit else None,
                     checked["strategy_class"],
                     fingerprint, str(ctx.get("request_hash") or ""),
                     f"{tenant_id}\x1f{command_id}",
                     # route_decision_ref 此刻还是 NULL（路由尚未发生），
                     # route_policy_ref 是调用方下发的那份，二者不是一回事。
                     _jcs(checked["route_policy_ref"]),
                     uuid.uuid4().hex, _jcs(ctx),
                     checked["budget_scope_id"],
                     checked["deadline"], 1, now, now, now))
            except sqlite3.IntegrityError as exc:
                # SQLite 报列名，不报索引名。并发双提交撞 `image_job_open_uniq`
                # 会得到 `UNIQUE constraint failed: image_job.tenant_id, ...`。
                # 按索引名匹配永远不命中，会把并发变成 500。
                text = str(exc)
                if "UNIQUE constraint failed: image_job.tenant_id" in text:
                    existing = _row(self.conn.execute(
                        "SELECT * FROM image_job WHERE tenant_id=? AND "
                        "shot_lineage_id=? AND node_kind=? AND "
                        "input_fingerprint=? AND state NOT IN "
                        "('READY','RECONCILED_READY','REJECTED_LOCAL','FAILED',"
                        "'RECONCILED_FAILED','CANCELED')",
                        (tenant_id, checked["shot_lineage_id"],
                         checked["node_kind"], fingerprint)).fetchone())
                    if existing is not None:
                        return self._job_response(
                            existing, extra={"duplicate_suppressed": True})
                raise

            job = self._job(tenant_id, job_id)
            outbox = self.outbox

            # `CREATED → PREPARING` 是方案的"prepare worker 领取"。我们内联做，
            # 但**先走这条边再判拒绝**：§3.1 的表里没有 `CREATED →
            # REJECTED_LOCAL`，拒绝一律从 PREPARING 出发。先前顺序反了，导致
            # 每个本地拒绝都撞状态机断言（变 500），T29-04 根本走不到。
            job = self._transition(job, "PREPARING", outbox=outbox)

            # F1/F2：本地拒绝。job 留下，状态 REJECTED_LOCAL，**不调 S26**。
            if reject_code is not None:
                return self._reject_local(job, reject_code, outbox,
                                          detail=reject_detail or "")

            return self._dispatch(job, outbox)

        return self._command(tenant_id, command_id, produce)

    def _artifact_sha_of(self, candidate_id: str, tenant_id: str) -> str:
        row = self.conn.execute(
            "SELECT artifact_ref FROM keyframe_candidate WHERE tenant_id=? AND "
            "candidate_id=?", (tenant_id, candidate_id)).fetchone()
        ref = _loads(row["artifact_ref"], {}) if row is not None else {}
        sha = ref.get("content_hash")
        if not isinstance(sha, str) or not _HEX64_RE.match(sha):
            raise ServiceError(409, "EDIT_SOURCE_ARTIFACT_UNREADABLE",
                               "source candidate carries no usable content_hash")
        return sha

    def _write_mask(self, tenant_id: str, edit: dict) -> str:
        """写 mask 行。`em_mask_single_binding` 挡住跨候选复用（T29-19 / C29-5）。"""
        mask_id = _new_id("mask")
        geometry_digest = _sha256_hex(_jcs(edit["mask_geometry"]))
        try:
            self.conn.execute(
                "INSERT INTO edit_mask (edit_mask_id, tenant_id, "
                "source_candidate_id, artifact_ref, mask_artifact_id, "
                "mask_sha256, geometry_digest, instruction, window_start_frame, "
                "window_end_frame, window_fps, constraint_6frames, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (mask_id, tenant_id, edit["source_candidate_id"],
                 _jcs({"service_id": "S28", "id": edit["mask_artifact_id"],
                       "revision": 1, "content_hash": edit["mask_sha256"]}),
                 edit["mask_artifact_id"], edit["mask_sha256"], geometry_digest,
                 edit["instruction"], edit["window_start_frame"],
                 edit["window_end_frame"], edit["window_fps"], _now()))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: edit_mask.tenant_id" in str(exc) or \
                    "edit_mask.tenant_id, edit_mask.mask_artifact_id" in str(exc):
                raise ServiceError(
                    409, "MASK_ALREADY_BOUND",
                    "this mask artifact is already bound to another candidate; "
                    "a mask must not be reused across candidates (§2.5)") from exc
            raise
        return mask_id

    def _reject_local(self, job: dict, code: str, outbox: Outbox, *,
                      detail: str) -> dict:
        """本地拒绝：没花钱、没留台账。终态但**不是失败**——换参考/改策略就能好，
        所以业务幂等键不占位，之后同输入还能重开。"""
        self.conn.execute(
            "UPDATE image_job SET failure_code=?, updated_at=? "
            "WHERE image_job_id=?", (code, _now(), job["image_job_id"]))
        job = self._job(job["tenant_id"], job["image_job_id"])
        job = self._transition(job, "REJECTED_LOCAL", reason_code=code,
                               detail=detail, outbox=outbox)
        outbox.append(new_event(
            source=SOURCE, type=LOCAL_CANDIDATE_REJECTED,
            subject=f"image-job/{job['image_job_id']}",
            tenant_id=job["tenant_id"], home_cell="image-production",
            aggregate_id=job["image_job_id"], sequence=job["state_seq"],
            correlation_id=job["image_job_id"],
            data={"image_job_id": job["image_job_id"],
                  "shot_lineage_id": job["shot_lineage_id"],
                  "node_kind": job["node_kind"], "reason_code": code,
                  "detail": detail},
            schema_version=SCHEMA_VERSION))
        return self._job_response(job, extra={"reason_code": code})

    # ------------------------------------------------------------- 提交 S26
    def _generation_command(self, job: dict, *, attempt_id: str,
                            idempotency_key: str, refs: list,
                            instructions: dict | None) -> dict:
        """`generation-command.v2` 的 image 分支。

        11 个必填键一个都不能少（S26 逐键查 `COMMAND_REQUIRED`），而 image 的
        `input` 是 `additionalProperties: false`，只接受 prompt_intent / mode /
        width / height / references / identity_constraints（C29-18：本地的
        `ordinal` 在这里剥掉，顺序靠数组本身表达）。

        `context` **原样复用**请求里那份已签名上下文。18 键里的 `actor` /
        `trace_id` / `home_region` 只能来自平台签名；现拼出来的每个值都是伪造签名。
        """
        ctx = _loads(job["execution_context"], {})
        input_refs = [{"artifact_id": r["artifact_id"], "sha256": r["sha256"],
                       "role": r["role"]} for r in refs]
        return {
            "schema_version": SCHEMA_VERSION,
            "context": ctx,
            # S26 的 attempt 主键是 (tenant, attempt_id)，同 job 重试要换一个新的
            # attempt_id；idempotency_key 同理。
            "attempt_id": attempt_id,
            "variation_nonce": job["variation_nonce"],
            "idempotency_key": idempotency_key,
            "modality": "image",
            "input": {
                "prompt_intent": job["prompt_intent"],
                "mode": job["mode"],
                "width": int(job["width"]),
                "height": int(job["height"]),
                "references": input_refs,
                "identity_constraints": _loads(job["identity_constraints"], []),
            },
            "template_ref": None,
            "route_policy_ref": _loads(job["route_policy_ref"], None),
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
        }

    def _dispatch(self, job: dict, outbox: Outbox) -> dict:
        """PREPARING → 取路由 → 提交 S26。

        **每次派单都是一次新的 S26 调用**：本服务没有"预算已批"这一列，也
        没有缓存的路由报价（F5 的报价会过期）。所以"派单时重新检查"在这里是
        结构性的，不是一次记得要做的动作。
        """
        refs = self._refs_of(job)
        instructions = self._edit_instructions(job)

        for _attempt in range(2):
            attempt_base = int(job["business_attempt_no"])
            attempt_id = f"{job['image_job_id']}:{attempt_base}"
            idempotency_key = f"{job['idempotency_key']}:{attempt_base}"
            command = self._generation_command(
                job, attempt_id=attempt_id, idempotency_key=idempotency_key,
                refs=refs, instructions=instructions)

            route = self._upstream(
                lambda: self.route_resolver(job["tenant_id"], job, command),
                "ROUTE_DECISION_UNAVAILABLE")

            if isinstance(route, dict) and route.get("feasible") is False:
                # F4：透传原因，**不返回默认模型**（T29-07）。
                return self._reject_local(
                    job, "ROUTE_INFEASIBLE", outbox,
                    detail=str(route.get("infeasible_reason")
                               or "S25 reported no feasible route"))
            if isinstance(route, dict) and route.get("residency_allowed") is False:
                # T29-32：数据驻留，必须排在第一次付费提交之前。
                return self._reject_local(
                    job, "RESIDENCY_FORBIDDEN", outbox,
                    detail="no feasible route satisfies the residency requirement")

            quote_valid_to = (route or {}).get("quote_valid_to")
            if quote_valid_to:
                try:
                    if _parse_ts(quote_valid_to) <= datetime.now(timezone.utc):
                        # F5：报价过期。**不重用旧报价**；重取一次。
                        #
                        # 这里**不写状态转移**：方案的 F5 明写"停在 PREPARING"，
                        # 而 `PREPARING -> PREPARING` 不在 §3.1 的表里，硬造一条
                        # 自环会让"每次状态变化都推高 state_seq"这条不变量失效，
                        # 下游按 state_seq 判断新旧时会看到一次假变化。
                        self._note_quote_expired(job, outbox)
                        continue
                except ValueError:
                    raise ServiceError(503, "ROUTE_DECISION_UNAVAILABLE",
                                       "route quote_valid_to is not a timestamp",
                                       retryable=True)

            if not (route.get("route_id") or route.get("route_decision_id")):
                # 拿不到路由 id 时**不能**编一个：S26 会拿它去校验预算与配额。
                raise ServiceError(
                    503, "ROUTE_DECISION_UNAVAILABLE",
                    "S25 returned no route id; S29 will not invent one",
                    retryable=True)

            self.conn.execute(
                "UPDATE image_job SET route_decision_ref=?, updated_at=? "
                "WHERE image_job_id=?",
                (_jcs(self._route_ref(route)), _now(), job["image_job_id"]))
            job = self._job(job["tenant_id"], job["image_job_id"])

            submitted = self._upstream(
                lambda: self.submitter(job, command, route, 0),
                "GENERATION_JOBS_UNAVAILABLE")
            if not isinstance(submitted, dict) \
                    or not submitted.get("provider_job_id"):
                raise ServiceError(
                    503, "GENERATION_JOBS_UNAVAILABLE",
                    "S26 accepted the request but returned no provider_job_id",
                    retryable=True)

            self.conn.execute(
                "UPDATE image_job SET provider_job_id=?, attempt_id=?, "
                "updated_at=? WHERE image_job_id=?",
                (submitted["provider_job_id"],
                 submitted.get("attempt_id") or attempt_id, _now(),
                 job["image_job_id"]))
            self._write_ledger(job, action="keyframe_edit"
                               if job["mode"] == "edit" else "generate")
            job = self._job(job["tenant_id"], job["image_job_id"])
            job = self._transition(job, "SUBMITTED", outbox=outbox)
            job = self._transition(job, "WAITING_PROVIDER", outbox=outbox)
            return self._job_response(
                job, extra={"operation_id": submitted.get("operation_id")})

        raise ServiceError(
            503, "ROUTE_DECISION_UNAVAILABLE",
            "route quotes kept expiring; refusing to submit on a stale price",
            retryable=True)

    def _note_quote_expired(self, job: dict, outbox: Outbox) -> None:
        """F5：记一笔"报价过期、重新取"，**状态仍是 PREPARING**。

        为什么不是一次状态转移：`TRANSITIONS["PREPARING"]` 里没有它自己，方案
        §3.1 也对不上（F5 只写"停在 PREPARING"）。自环还会让 `state_seq` 涨，
        而 `state_seq` 是下游判"这条事件是不是比我手上的新"的依据——一个没有
        任何状态变化的 +1 就是噪声。

        所以这里**只发 outbox 事件**，`from == to` + `reason_code` 足以在审计里
        还原"这一次派单用的是重取的报价"，而且不碰 `state_seq`、不造主键冲突
        （`job_transition` 的主键是 `(image_job_id, seq)`，拿同一个 seq 再插一行
        会直接撞键）。
        """
        seq = int(job["state_seq"])
        outbox.append(new_event(
            source=SOURCE, type=LOCAL_JOB_STATE,
            subject=f"image-job/{job['image_job_id']}",
            tenant_id=job["tenant_id"], home_cell="image-production",
            aggregate_id=job["image_job_id"], sequence=seq,
            correlation_id=job["image_job_id"],
            data={"image_job_id": job["image_job_id"],
                  "shot_lineage_id": job["shot_lineage_id"],
                  "node_kind": job["node_kind"],
                  "from": job["state"], "to": job["state"],
                  "reason_code": "QUOTE_EXPIRED"},
            schema_version=SCHEMA_VERSION))

    def _route_ref(self, route: dict) -> dict:
        """只存引用，不复制 `eligible[]`。**拿不到 content_hash 就不编**。"""
        route_id = (route or {}).get("route_id") \
            or (route or {}).get("route_decision_id")
        ref = {"service_id": "S25", "id": route_id,
               "revision": int((route or {}).get("revision") or 1)}
        if (route or {}).get("content_hash"):
            ref["content_hash"] = route["content_hash"]
        return ref

    # ------------------------------------------------------- 台账（只追加）
    def _write_ledger(self, job: dict, *, action: str) -> None:
        """**只有真的把钱花出去的意图**才进台账（§8.1）。

        `run_id` 仅审计归因，**不进任何上限键**；execution-context 没有该字段
        时存空串，不拿 trace_id / manifest_id 冒充（C29-8）。
        """
        ctx = _loads(job["execution_context"], {})
        manifest = ctx.get("manifest_ref") or {}
        self.conn.execute(
            "INSERT INTO image_intent_ledger (entry_id, tenant_id, image_job_id, "
            "attempt_id, provider_job_id, shot_lineage_id, node_kind, action, "
            "strategy_class, repair_action_id, run_id, node_epoch, manifest_id, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["image_job_id"],
             job["attempt_id"], job["provider_job_id"],
             job["shot_lineage_id"], job["node_kind"], action,
             job["strategy_class"], job["repair_action_id"],
             str(ctx.get("run_id") or ""), int(job["node_epoch_at_create"]),
             str(manifest.get("id") or ""), _now()))

    # ------------------------------------------------- 查询（§6.2 / §6.3）
    def get_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        body = self._job_response(job)
        candidates = [self._candidate_body(c) for c in self.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND "
            "image_job_id=? ORDER BY created_at", (tenant_id, job_id)).fetchall()]
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM shot_image_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND node_kind=?",
            (tenant_id, job["shot_lineage_id"], job["node_kind"])).fetchone())
        history = [
            {"seq": int(r["seq"]), "from": r["from_state"], "to": r["to_state"],
             "at": r["at"], "reason_code": r["reason_code"]}
            for r in self.conn.execute(
                "SELECT * FROM job_transition WHERE image_job_id=? ORDER BY seq",
                (job_id,)).fetchall()]
        frozen = job["state"] == "WAITING_UNKNOWN"
        body.update({
            "source_revision": int(_loads(job["shot_ref"], {}).get("revision") or 0),
            "business_attempt_no": int(job["business_attempt_no"]),
            "provider_job_state": _provider_state_of(job["state"]),
            "provider_state_source": "local_map",
            "frozen": frozen,
            # 恒等于 provider_job_state == 'SUBMISSION_UNKNOWN'（§6.2）。它和
            # `frozen` 现在是同一个判定；分成两个字段是因为语义不同：frozen 说
            # "别等"，must_reconcile 说"别重提"。
            "must_reconcile_before_resubmit": frozen,
            "candidates": candidates,
            "checkpoint": None if checkpoint is None else {
                "shot_lineage_id": checkpoint["shot_lineage_id"],
                "node_kind": checkpoint["node_kind"],
                "candidate_id": checkpoint["candidate_id"],
                "checkpoint_seq": int(checkpoint["checkpoint_seq"]),
            },
            "failure": None if job["failure_code"] is None else {
                "code": job["failure_code"], "detail": job["failure_detail"]},
            "history": history,
        })
        return body

    def list_jobs(self, tenant_id: str, *, shot_lineage_id: str | None = None,
                  node_kind: str | None = None, state: str | None = None,
                  limit: int = 100) -> dict:
        sql = "SELECT * FROM image_job WHERE tenant_id=?"
        params: list = [tenant_id]
        if shot_lineage_id:
            sql += " AND shot_lineage_id=?"
            params.append(shot_lineage_id)
        if node_kind:
            sql += " AND node_kind=?"
            params.append(node_kind)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        ids = [r["image_job_id"] for r in rows]
        watermark = self.conn.execute(
            "SELECT MAX(state_seq) AS m FROM image_job WHERE tenant_id=?",
            (tenant_id,)).fetchone()["m"]
        return {"items": [self._job_response(_row(r)) for r in rows],
                "projection_watermark": int(watermark or 0),
                "count": len(ids)}

    def get_checkpoint(self, tenant_id: str, shot_lineage_id: str) -> dict:
        """S30 的主读口。**不含任何 accepted 语义**——它只说"该拿哪张图"。"""
        rows = self.conn.execute(
            "SELECT * FROM shot_image_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? ORDER BY node_kind",
            (tenant_id, shot_lineage_id)).fetchall()
        return {"shot_lineage_id": shot_lineage_id,
                "checkpoints": [
                    {"node_kind": r["node_kind"],
                     "candidate_id": r["candidate_id"],
                     "checkpoint_seq": int(r["checkpoint_seq"]),
                     "updated_by_job": r["updated_by_job"],
                     "updated_at": r["updated_at"]}
                    for r in rows]}

    def get_candidate(self, tenant_id: str, candidate_id: str) -> dict:
        """可追溯验收的读口：候选 + 完整参考链（§6.3）。"""
        row = _row(self.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND "
            "candidate_id=?", (tenant_id, candidate_id)).fetchone())
        if row is None:
            raise ServiceError(404, "CANDIDATE_NOT_FOUND",
                               f"no candidate {candidate_id}")
        body = self._candidate_body(row)
        combo = _row(self.conn.execute(
            "SELECT * FROM reference_combination WHERE "
            "reference_combination_id=?",
            (row["reference_combination_id"],)).fetchone())
        body["reference_chain"] = {
            "reference_combination_hash": combo["content_hash"] if combo else None,
            "references": _loads(combo["references"], []) if combo else [],
            "identity_constraints": _loads(combo["identity_constraints"], [])
            if combo else [],
        }
        job = self._job(tenant_id, row["image_job_id"])
        body["built_for"] = self._built_for(job)
        body["strategy_class"] = row["strategy_class"]
        return body

    # ---------------------------------------------------------- 取消 / 重试
    def cancel_job(self, tenant_id: str, job_id: str, body: dict,
                   command_id: str) -> dict:
        """§6.3。只调 S26 cancel，**不承诺退款**（T29-34）。"""
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] == "WAITING_UNKNOWN":
                # §4.3：UNKNOWN 期间不发起 cancel——未收单的取消没意义，已收单
                # 的可能仍在计费。与 retry 用同一个拒绝码，因为它们都是"先去
                # 对账"。
                raise ServiceError(
                    409, "MUST_RECONCILE_FIRST",
                    "job is WAITING_UNKNOWN; reconcile with S26 before "
                    "cancelling or resubmitting")
            if job["state"] in TERMINAL_STATES:
                raise ServiceError(409, "JOB_ALREADY_TERMINAL",
                                   f"job is {job['state']}")
            outbox = self.outbox
            if job["provider_job_id"]:
                try:
                    self.canceler(job["provider_job_id"],
                                  body.get("reason") or "image_canceled")
                except (ServiceError, ConnectionError) as exc:
                    # 取消没被确认。**不能标 CANCELED**——那等于断言"它确定没在
                    # 做"，而它可能正在做并照样收费。保持原状态，把"没确认"说
                    # 出来，等 S26 的事件收敛。
                    return {"image_job_id": job_id, "state": job["state"],
                            "cancel_state": "CANCEL_PENDING",
                            "refund_promised": False,
                            "note": f"cancellation unconfirmed: {exc}"}
            job = self._transition(
                job, "CANCELED",
                reason_code=body.get("reason") or "REQUESTED", outbox=outbox)
            return {"image_job_id": job_id, "state": job["state"],
                    "cancel_state": "CANCELED", "refund_promised": False}

        return self._command(tenant_id, command_id, produce)

    def retry_job(self, tenant_id: str, job_id: str, body: dict,
                  command_id: str) -> dict:
        """§6.3。只有 `FAILED` / `RECONCILED_FAILED` 能重试。

        `WAITING_UNKNOWN` 必须先对账——盲目重提付费任务正是 AGENTS.md 的红线，
        这条拒绝是 E16 在 API 层的落点（T29-12）。

        C29-23：§6.3 说"新建 job"，但 §7.2 的 `image_job_repair_uniq` 不允许
        同一 `repair_action_id` 建第二个 job。这里**原地重试**：bump
        `business_attempt_no`、换 `variation_nonce`、清失败码，再走一次 S26。
        这样修复幂等键与业务幂等键都保持成立。
        """
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] == "WAITING_UNKNOWN":
                raise ServiceError(
                    409, "MUST_RECONCILE_FIRST",
                    "job is WAITING_UNKNOWN; resubmitting could pay twice")
            if job["state"] not in RETRYABLE_STATES:
                raise ServiceError(
                    409, "JOB_NOT_RETRYABLE",
                    f"job is {job['state']}; only {sorted(RETRYABLE_STATES)} "
                    "can be retried", details={"state": job["state"]})
            self.conn.execute(
                "UPDATE image_job SET business_attempt_no=business_attempt_no+1, "
                "variation_nonce=?, failure_code=NULL, failure_detail=NULL, "
                "reconciled=0, updated_at=? WHERE image_job_id=?",
                (uuid.uuid4().hex, _now(), job_id))
            job = self._job(tenant_id, job_id)
            outbox = self.outbox
            job = self._transition(job, "PREPARING", reason_code="RETRY",
                                   allow_retry=True, outbox=outbox)
            return self._dispatch(job, outbox)

        return self._command(tenant_id, command_id, produce)

    # ================================================ 事件消费者
    def on_generation_state_changed(self, envelope: dict) -> dict:
        """S26 `generation.job.state-changed`。

        **防御式映射**：S26 的 `state` 在契约里是普通 string，这里不假设自己
        认识每一个值。不认识的一律不动 job，返回 `ignored`——把未知状态当成
        "成功"是这条链路最贵的一种错。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            # 没有 id 就无法去重。重投时会被再处理一遍，不能当成功。
            raise ServiceError(400, "INVALID_BODY",
                               "generation state event carries no id")
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        provider_job_id = envelope.get("aggregate_id") or data.get("provider_job_id")
        provider_state = data.get("state")
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_a_generation_job_event"}

        # **去重行与状态变化同一个事务**。若去重先独立提交、状态改到一半崩了，
        # 重投会命中 `inbox` 被判 DUPLICATE，这条事件就永久丢了——付费任务的
        # 状态就永远停在 SUBMITTED。这里让内层 `_transition` 挂在同一事务上，
        # 异常冒到最外层才 ROLLBACK，去重行跟着回滚。
        with Transaction(self.conn, self._tx_depth):
            if not self.inbox.consume("S29", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            result = self._apply_provider_state(tenant_id, provider_job_id,
                                                provider_state, data)
            # 事务里**不做 HTTP**：`BEGIN IMMEDIATE` 已经拿了写锁，在锁里等
            # S28 的超时会把整个租户的写入堵住。需要归档的 job 带出来，提交后
            # 再驱动（重驱动是幂等的，失败也只是"还没触发"）。
            pending_import = result.pop("_request_import", None)

        if pending_import is not None:
            return self._request_import(pending_import, result)
        return result

    def _apply_provider_state(self, tenant_id: str, provider_job_id: str,
                              provider_state: str, data: dict) -> dict:
        job = _row(self.conn.execute(
            "SELECT * FROM image_job WHERE tenant_id=? AND provider_job_id=?",
            (tenant_id, provider_job_id)).fetchone())
        if job is None:
            return {"ignored": True, "reason": "no_matching_image_job"}

        if job["state"] == "WAITING_UNKNOWN":
            # §3.1：UNKNOWN 只有两个出口，都经对账。**不允许它走到 INGESTING /
            # FAILED**——那两条边不在表里，也不该在：一次"不确定是否付过款"的
            # 任务，只有 S26 的对账结论能给它定性。
            return self._reconcile_from_unknown(job, provider_state, data)
        if job["state"] in TERMINAL_STATES:
            # 终态没有出边（§3.1）。这类事件是**旧事实**：可能与某次重试交错、
            # 也可能是同一 attempt 的重复投递（换了 event id 所以去重没拦住）。
            # 让 `_transition` 抛 AssertionError 会把一条无害的过期事件变成
            # 消费者崩溃 + 无限重投；这里明确忽略，且不改任何一行。
            return {"image_job_id": job["image_job_id"],
                    "state": job["state"], "ignored": True,
                    "reason": f"job_already_terminal:{job['state']}"}
        if provider_state in _S26_UNKNOWN_STATES:
            return self._enter_unknown(job)
        if provider_state in _S26_SUCCESS_STATES:
            return self._enter_ingesting(job, provider_state=provider_state)
        if provider_state == "REJECTED_BUDGET":
            # F6：透传，不降规格；**计数不 +1**（没花钱）。
            return self._fail(job, "BUDGET", provider_state)
        if provider_state == "REJECTED_QUOTA":
            # F7：透传，不换池；**计数不 +1**。
            return self._fail(job, "QUOTA", provider_state)
        if provider_state == "PROVIDER_FAILED":
            # F8/F10：真花了钱、真试过 → +1（台账在提交时已写）。
            return self._fail(job, data.get("reason_code") or "PROVIDER_FAILED",
                              provider_state)
        if provider_state == "CANCELED":
            if job["state"] in TERMINAL_STATES:
                return {"image_job_id": job["image_job_id"],
                        "state": job["state"], "ignored": True}
            new = self._transition(job, "CANCELED",
                                   reason_code="PROVIDER_CANCELED",
                                   outbox=self.outbox)
            return {"image_job_id": new["image_job_id"], "state": new["state"]}
        return {"ignored": True,
                "reason": f"unmapped_provider_state:{provider_state}"}

    def _reconcile_from_unknown(self, job: dict, provider_state: str,
                                data: dict) -> dict:
        """`WAITING_UNKNOWN` 的两个出口（§3.2 / §4.6 T29-13）。

        - `ARCHIVED`：S26 对账证明供应商已收单并产出 → `RECONCILED_READY`。
          候选要落，但**不重新生成**；`reconciled=1`。
        - `REJECTED_BUDGET` / `REJECTED_QUOTA` / `PROVIDER_FAILED` / `CANCELED`：
          对账证明没成 → `RECONCILED_FAILED`（都是 S26 §3.1 的 16 态之一）。

        其余一律 `ignored`：`QUEUED` / `RUNNING` 这类"还在做"的状态不是对账
        结论，把它当成结论就是把"可能已付费"读成"一定没好"。
        """
        if provider_state == "ARCHIVED":
            with Transaction(self.conn, self._tx_depth):
                self.conn.execute(
                    "UPDATE image_job SET reconciled=1, updated_at=? "
                    "WHERE image_job_id=?", (_now(), job["image_job_id"]))
                job = self._job(job["tenant_id"], job["image_job_id"])
                new = self._transition(
                    job, "RECONCILED_READY", reason_code="RECONCILED_ARCHIVED",
                    outbox=self.outbox)
            return {"image_job_id": new["image_job_id"], "state": new["state"],
                    "reconciled": True}
        if provider_state in ("REJECTED_BUDGET", "REJECTED_QUOTA",
                              "PROVIDER_FAILED", "CANCELED"):
            with Transaction(self.conn, self._tx_depth):
                self.conn.execute(
                    "UPDATE image_job SET reconciled=1, failure_code=?, "
                    "failure_detail=?, updated_at=? WHERE image_job_id=?",
                    (provider_state, "reconciled_from_unknown", _now(),
                     job["image_job_id"]))
                job = self._job(job["tenant_id"], job["image_job_id"])
                new = self._transition(
                    job, "RECONCILED_FAILED", reason_code=provider_state,
                    outbox=self.outbox)
            return {"image_job_id": new["image_job_id"], "state": new["state"],
                    "reconciled": True, "failure_code": provider_state}
        return {"image_job_id": job["image_job_id"], "state": job["state"],
                "ignored": True, "frozen": True,
                "reason": f"not_a_reconciliation_conclusion:{provider_state}"}

    def _enter_unknown(self, job: dict) -> dict:
        if job["state"] == "WAITING_UNKNOWN":
            return {"image_job_id": job["image_job_id"], "state": job["state"],
                    "ignored": True}
        with Transaction(self.conn, self._tx_depth):
            outbox = self.outbox
            new = self._transition(
                job, "WAITING_UNKNOWN", reason_code="SUBMISSION_UNKNOWN",
                detail="provider outcome unknown; must reconcile before resubmit",
                outbox=outbox)
        return {"image_job_id": new["image_job_id"], "state": new["state"],
                "frozen": True, "must_reconcile_before_resubmit": True}

    def _enter_ingesting(self, job: dict, *, provider_state: str) -> dict:
        if job["state"] in ("INGESTING", "READY"):
            return {"image_job_id": job["image_job_id"], "state": job["state"],
                    "ignored": True}
        new = self._transition(job, "INGESTING", reason_code=provider_state,
                               outbox=self.outbox)
        # 归档是异步的：这里**只**推进状态并把"该去催一次导入"带出去。真正的
        # 候选由 `artifact.ingested` 落定；导入失败不能让 job 假装成功。
        # HTTP 放在事务外发（见 `on_generation_state_changed`）。
        return {"image_job_id": new["image_job_id"], "state": new["state"],
                "_request_import": new}

    def _fail(self, job: dict, code: str, provider_state: str) -> dict:
        if job["state"] in TERMINAL_STATES:
            return {"image_job_id": job["image_job_id"], "state": job["state"],
                    "ignored": True}
        with Transaction(self.conn, self._tx_depth):
            outbox = self.outbox
            self.conn.execute(
                "UPDATE image_job SET failure_code=?, failure_detail=?, "
                "updated_at=? WHERE image_job_id=?",
                (code, provider_state, _now(), job["image_job_id"]))
            job = self._job(job["tenant_id"], job["image_job_id"])
            new = self._transition(job, "FAILED", reason_code=code, outbox=outbox)
        return {"image_job_id": new["image_job_id"], "state": new["state"],
                "failure_code": code}

    def _request_import(self, job: dict, result: dict) -> dict:
        """尽力触发一次 S28 导入（幂等键固定，重放安全）。

        正常路径上 S26 也会建导入；这里是对 F11/F16 的重驱动，不拥有字节。

        **失败不改状态**：job 已经在 INGESTING 且已提交，导入没被受理只说明
        "字节还没进 S28"，不是"生成失败"。URL 过期（F11）尤其不能重生成。

        唯一的例外是 F12 / T29-15 的 `HASH_MISMATCH`：那是 S28 **已经取到字节、
        算完之后明确说"这不是你要的那份"**，不是"还没取到"。S28 在这条路径上
        只更新自己的 import 行并返回 422，**不发事件**（`artifact-ingest` 只在
        发布成功时发 `artifact.ingested`），所以这里是 S29 唯一能知道它的地方。
        按方案打终态 `FAILED(ARTIFACT_HASH_MISMATCH)`，**不重试**：内容不一致
        通常意味着供应商返回了别的东西，重试只会掩盖它。
        """
        try:
            self.artifact_importer(job["tenant_id"], job)
        except (ServiceError, ConnectionError) as exc:
            code = getattr(exc, "code", None) or "ARTIFACT_INGEST_UNAVAILABLE"
            if code == "HASH_MISMATCH":
                # 状态变化要事务；此刻已在外层事务之外（HTTP 不在写锁里）。
                with Transaction(self.conn, self._tx_depth):
                    fresh = self._job(job["tenant_id"], job["image_job_id"])
                    if fresh["state"] not in TERMINAL_STATES:
                        self.conn.execute(
                            "UPDATE image_job SET failure_code=?, "
                            "failure_detail=?, updated_at=? "
                            "WHERE image_job_id=?",
                            ("ARTIFACT_HASH_MISMATCH", "s28_hash_mismatch",
                             _now(), fresh["image_job_id"]))
                        fresh = self._job(job["tenant_id"],
                                          job["image_job_id"])
                        self._transition(fresh, "FAILED",
                                         reason_code="ARTIFACT_HASH_MISMATCH",
                                         outbox=self.outbox)
                result["failure_code"] = "ARTIFACT_HASH_MISMATCH"
                result["note"] = "import rejected: HASH_MISMATCH"
                return result
            result["awaiting_fresh_url"] = code == "URL_EXPIRED"
            result["note"] = f"import not accepted: {code}"
        return result

    def on_artifact_ingested(self, envelope: dict) -> dict:
        """S28 `artifact.ingested` → 写候选、推进检查点、发 ready。

        C29-11 同类教训：这个事件没有顶层 `provider_job_id`，只有
        `source.provider_job_id`。两级都试。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            raise ServiceError(400, "INVALID_BODY",
                               "artifact ingested event carries no id")
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        artifact_ref = data.get("artifact_ref") or {}
        provider_job_id = ((data.get("source") or {}).get("provider_job_id")
                           or data.get("provider_job_id"))
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_shaped_for_image"}

        # 同 `on_generation_state_changed`：去重行与候选/检查点/ready 同事务。
        with Transaction(self.conn, self._tx_depth):
            if not self.inbox.consume("S29", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            if artifact_ref.get("service_id") != "S28":
                # §5.3：候选的 artifact_ref 只能指向 S28。供应商直链进来就是
                # 第二权威。**已消费再拒**：这是一条给我们的毒消息，不消费就会
                # 被无限重投。
                return {"ignored": True, "reason": "artifact_not_owned_by_s28"}
            if not artifact_ref.get("id") or not _HEX64_RE.match(
                    str(artifact_ref.get("content_hash") or "")):
                return {"ignored": True, "reason": "artifact_ref_incomplete"}
            job = _row(self.conn.execute(
                "SELECT * FROM image_job WHERE tenant_id=? AND provider_job_id=?",
                (tenant_id, provider_job_id)).fetchone())
            if job is None:
                return {"ignored": True, "reason": "no_matching_image_job"}
            return self._finalize(job, artifact_ref, observed={
                "width": data.get("width_px"), "height": data.get("height_px"),
                "format": data.get("mime_type") or data.get("container"),
                "bytes": data.get("size_bytes")})

    def _finalize(self, job: dict, artifact_ref: dict, *, observed: dict) -> dict:
        """候选落定 → 检查点 → ready 事件。**在同一事务里**。"""
        if job["state"] in ("WAITING_UNKNOWN", "RECONCILED_FAILED", "CANCELED",
                            "FAILED", "REJECTED_LOCAL"):
            # 这些状态的出口只有对账那条边（§3.2）。一条 `artifact.ingested`
            # 不是对账结论：UNKNOWN 期间它可能来自上一次已经付过款的 attempt，
            # 拿它推进 READY 就是让"不确定是否付过款"悄悄变成"成功"。
            # `RECONCILED_READY` **不**在此列——对账已经证明这张图存在，缺的
            # 只是候选行，`artifact.ingested` 正是补它的那一步（T29-13）。
            return {"image_job_id": job["image_job_id"],
                    "state": job["state"], "ignored": True,
                    "reason": f"artifact_ingested_while_{job['state'].lower()}"}
        with Transaction(self.conn, self._tx_depth):
            outbox = self.outbox
            candidate = self._insert_candidate(job, artifact_ref, observed)
            if job["state"] not in ("READY", "RECONCILED_READY"):
                job = self._transition(job, "READY",
                                       reason_code="ARTIFACT_INGESTED",
                                       outbox=outbox)
            advanced = None
            try:
                advanced = self._advance_checkpoint(job, candidate["candidate_id"])
            except ServiceError as exc:
                # F15：CAS 冲突不吞掉事实——job 已是 READY，检查点未推进，
                # 由 S08 裁决。事件里不带 checkpoint_seq 以外的猜测。
                return {"image_job_id": job["image_job_id"],
                        "candidate_id": candidate["candidate_id"],
                        "state": job["state"], "checkpoint_advanced": False,
                        "checkpoint_error": exc.code}
            if advanced is None:
                return {"image_job_id": job["image_job_id"],
                        "candidate_id": candidate["candidate_id"],
                        "state": job["state"], "checkpoint_advanced": False,
                        "checkpoint_error": "CHECKPOINT_NOT_ADVANCED"}
            self._publish_ready(job, candidate, outbox, advanced["row"],
                                from_candidate_id=advanced["from_candidate_id"])
        return {"image_job_id": job["image_job_id"],
                "candidate_id": candidate["candidate_id"],
                "state": job["state"], "checkpoint_advanced": True,
                "checkpoint_seq": int(advanced["row"]["checkpoint_seq"])}

    def _insert_candidate(self, job: dict, artifact_ref: dict,
                          observed: dict) -> dict:
        """候选不可变；同 artifact 被两个 job 产出 → 复用已有候选（T29-20）。"""
        existing = _row(self.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND "
            "json_extract(artifact_ref,'$.id')=? AND "
            "json_extract(artifact_ref,'$.content_hash')=?",
            (job["tenant_id"], artifact_ref["id"],
             artifact_ref["content_hash"])).fetchone())
        if existing is not None:
            return existing

        requires_recheck = 1 if (job["mode"] == "edit"
                                 or self._dims_differ(job, observed)) else 0
        candidate_id = _new_id("candidate")
        try:
            self.conn.execute(
                "INSERT INTO keyframe_candidate (candidate_id, tenant_id, "
                "image_job_id, shot_lineage_id, node_kind, artifact_ref, "
                "reference_combination_id, source_candidate_id, edit_mask_id, "
                "provider_job_id, attempt_id, observed_output, strategy_class, "
                "repair_action_id, requires_recheck, state, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'READY',?)",
                (candidate_id, job["tenant_id"], job["image_job_id"],
                 job["shot_lineage_id"], job["node_kind"], _jcs(artifact_ref),
                 job["reference_combination_id"], job["source_candidate_id"],
                 job["edit_mask_id"], job["provider_job_id"],
                 job["attempt_id"] or "", _jcs(observed),
                 job["strategy_class"], job["repair_action_id"],
                 requires_recheck, _now()))
        except sqlite3.IntegrityError as exc:
            if "kc_artifact_uniq" in str(exc) or "keyframe_candidate" in str(exc):
                again = _row(self.conn.execute(
                    "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND "
                    "json_extract(artifact_ref,'$.id')=? AND "
                    "json_extract(artifact_ref,'$.content_hash')=?",
                    (job["tenant_id"], artifact_ref["id"],
                     artifact_ref["content_hash"])).fetchone())
                if again is not None:
                    return again
            raise
        return self._job_candidate(job["tenant_id"], candidate_id)

    def _job_candidate(self, tenant_id: str, candidate_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM keyframe_candidate WHERE tenant_id=? AND "
            "candidate_id=?", (tenant_id, candidate_id)).fetchone())
        if row is None:
            raise AssertionError(f"candidate {candidate_id} vanished")
        return row

    def _dims_differ(self, job: dict, observed: dict) -> bool:
        """U7：观测尺寸与请求不一致 → READY 但 `requires_recheck=true`。

        **不自行缩放**：缩放是 S42/交付层的事，S29 改了就是替下游做决定。
        """
        for key, requested in (("width", int(job["width"])),
                               ("height", int(job["height"]))):
            value = observed.get(key)
            if isinstance(value, int) and value != requested:
                return True
        return False

    def _advance_checkpoint(self, job: dict, candidate_id: str) -> dict | None:
        """CAS 推进（§7.2）。返回 `{row, from_candidate_id}`；不该推进时返回 None。

        "不该推进"只有一种情况：新候选比当前指向的更旧（U3 用 created_at 当
        优先级）。**检查点不回退**——回退是人工动作，需要 S40 权限（U5）。

        `from_candidate_id` 必须在这里、在 UPDATE 之前读出：推进之后就再也查不
        到了（表里只有当前值）。事后再"推断"上一张是谁是编数据。
        """
        current = _row(self.conn.execute(
            "SELECT * FROM shot_image_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND node_kind=?",
            (job["tenant_id"], job["shot_lineage_id"],
             job["node_kind"])).fetchone())
        candidate = self._job_candidate(job["tenant_id"], candidate_id)
        if current is not None:
            holder = _row(self.conn.execute(
                "SELECT created_at FROM keyframe_candidate WHERE candidate_id=?",
                (current["candidate_id"],)).fetchone())
            if holder is not None and holder["created_at"] > candidate["created_at"]:
                return None

        now = _now()
        if current is None:
            self.conn.execute(
                "INSERT INTO shot_image_checkpoint (tenant_id, shot_lineage_id, "
                "node_kind, candidate_id, checkpoint_seq, expected_revision, "
                "updated_by_job, updated_at) VALUES (?,?,?,?,1,0,?,?)",
                (job["tenant_id"], job["shot_lineage_id"], job["node_kind"],
                 candidate_id, job["image_job_id"], now))
            return {"row": self._checkpoint_row(job), "from_candidate_id": None}

        for _attempt in range(2):
            prior_id = current["candidate_id"]
            seq = int(current["checkpoint_seq"]) + 1
            cur = self.conn.execute(
                "UPDATE shot_image_checkpoint SET candidate_id=?, "
                "checkpoint_seq=?, expected_revision=expected_revision+1, "
                "updated_by_job=?, updated_at=? WHERE tenant_id=? AND "
                "shot_lineage_id=? AND node_kind=? AND checkpoint_seq=?",
                (candidate_id, seq, job["image_job_id"], now, job["tenant_id"],
                 job["shot_lineage_id"], job["node_kind"],
                 int(current["checkpoint_seq"])))
            if cur.rowcount == 1:
                return {"row": self._checkpoint_row(job),
                        "from_candidate_id": prior_id}
            current = _row(self.conn.execute(
                "SELECT * FROM shot_image_checkpoint WHERE tenant_id=? AND "
                "shot_lineage_id=? AND node_kind=?",
                (job["tenant_id"], job["shot_lineage_id"],
                 job["node_kind"])).fetchone())
            if current is None:
                return None
        raise ServiceError(409, "CHECKPOINT_CAS_FAILED",
                           "concurrent checkpoint advance lost the race twice; "
                           "leaving the decision to S08", retryable=True)

    def _checkpoint_row(self, job: dict) -> dict:
        return _row(self.conn.execute(
            "SELECT * FROM shot_image_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND node_kind=?",
            (job["tenant_id"], job["shot_lineage_id"],
             job["node_kind"])).fetchone())

    def _publish_ready(self, job: dict, candidate: dict, outbox: Outbox,
                       checkpoint: dict, *, from_candidate_id: str | None) -> None:
        """`production.image.ready`：**10 个必填键一个不少**，且
        `additionalProperties: false`（所以不塞 `requires_recheck`，C29-10）。

        PIR-2：`candidate_id` 与 `checkpoint.to_candidate_id` 由同一变量写入，
        并在这里断言相等——契约表达不了这条约束，生产方必须自己保证。
        """
        candidate_id = candidate["candidate_id"]
        seq = int(checkpoint["checkpoint_seq"])
        data = {
            "schema_version": SCHEMA_VERSION,
            "image_job_id": job["image_job_id"],
            "candidate_id": candidate_id,
            "shot_lineage_id": job["shot_lineage_id"],
            "node_kind": job["node_kind"],
            "artifact_ref": _loads(candidate["artifact_ref"], {}),
            "reference_combination_hash": self._combo_hash_of(job),
            "checkpoint": {"from_candidate_id": from_candidate_id,
                           "to_candidate_id": candidate_id,
                           "checkpoint_seq": seq},
            "built_for": self._built_for(job),
            "repaired_from": None if job["repair_action_id"] is None else {
                "repair_action_id": job["repair_action_id"],
                "source_candidate_id": job["source_candidate_id"],
            },
        }
        assert data["candidate_id"] == data["checkpoint"]["to_candidate_id"]
        outbox.append(new_event(
            source=SOURCE, type=EVENT_IMAGE_READY,
            subject=f"keyframe-candidate/{candidate_id}",
            tenant_id=job["tenant_id"], home_cell="image-production",
            aggregate_id=job["shot_lineage_id"], sequence=seq,
            correlation_id=job["image_job_id"], data=data,
            schema_version=SCHEMA_VERSION))

    def _combo_hash_of(self, job: dict) -> str:
        row = self.conn.execute(
            "SELECT content_hash FROM reference_combination WHERE "
            "reference_combination_id=?",
            (job["reference_combination_id"],)).fetchone()
        if row is None:
            raise AssertionError("image job lost its reference combination")
        return row["content_hash"]

    def _built_for(self, job: dict) -> dict:
        """`built_for`：这张图照着哪一版输入造的（供 S08 决定要不要纳入）。

        `manifest_ref` 需要 `{id, revision, content_hash}`（PIR-3 说 §5.2 DDL
        没有这几列）——它们来自**已签名的 context**，所以不另存列也不丢信息。
        """
        ctx = _loads(job["execution_context"], {})
        return {"manifest_ref": ctx.get("manifest_ref") or {},
                "node_epoch_input": int(job["node_epoch_at_create"]),
                "execution_fence": int(job["execution_fence"])}

    # ------------------------------------------------------------ 内部读
    def _refs_of(self, job: dict) -> list:
        combo = self.conn.execute(
            "SELECT \"references\" FROM reference_combination WHERE "
            "reference_combination_id=?",
            (job["reference_combination_id"],)).fetchone()
        return _loads(combo["references"], []) if combo is not None else []

    def _edit_instructions(self, job: dict) -> dict | None:
        if job["edit_mask_id"] is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM edit_mask WHERE edit_mask_id=?",
            (job["edit_mask_id"],)).fetchone()
        if row is None:
            return None
        return {"instruction": row["instruction"],
                "window": {"start_frame": int(row["window_start_frame"]),
                           "end_frame": int(row["window_end_frame"]),
                           "fps": int(row["window_fps"])}}

    def _candidate_body(self, row: dict) -> dict:
        ref = _loads(row["artifact_ref"], {})
        return {"candidate_id": row["candidate_id"],
                "image_job_id": row["image_job_id"],
                "node_kind": row["node_kind"],
                "artifact_ref": ref,
                "state": row["state"],
                "source_candidate_id": row["source_candidate_id"],
                "repair_action_id": row["repair_action_id"],
                "requires_recheck": bool(int(row["requires_recheck"])),
                "observed_output": _loads(row["observed_output"], {}),
                "created_at": row["created_at"]}

    def _job_response(self, job: dict, *, extra: dict | None = None,
                      combo_hash: str | None = None) -> dict:
        body = {"image_job_id": job["image_job_id"],
                "state": job["state"],
                "state_seq": int(job["state_seq"]),
                "shot_lineage_id": job["shot_lineage_id"],
                "node_kind": job["node_kind"],
                "mode": job["mode"],
                "input_fingerprint": job["input_fingerprint"],
                "provider_job_id": job["provider_job_id"],
                "reference_combination_hash": (combo_hash
                                               or self._combo_hash_of(job))
                if job["reference_combination_id"] else None}
        body.update(extra or {})
        return body


class _LocalReject(Exception):
    """内部信号：一次**不花钱**的本地拒绝。不是 API 错误——调用方要留下一个
    `REJECTED_LOCAL` 的 job，所以不能用 ServiceError 穿过事务。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
