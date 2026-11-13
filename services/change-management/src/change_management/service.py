"""S07 内核。方案：`plans/S07-change-management.md`。

一句话边界：**S07 的产物是一份"如果执行会怎样"的说明加一组"已经准备好的新
revision 引用"。它不是执行结果，也不是一次授权。**

四件它绝不做的事，每一件都能在库里查到证据：

1. **不写 accepted pointer、不写 `node_epoch`、不写 `execution_fence`。**
   库里只有 `change_plan.execution_fence` 一列，它是"生成计划时读到的值"，
   用于 apply 时比对，没有任何代码推进它（§0.3）。
2. **不把 `regenerate_node_ids` 当 epoch 清单发给 S08。** 那是"意图"，
   epoch 是 S08 的"事实"。commit 时带的是 `change_plan_ref` 与准备好的
   revision 引用，S08 自己算（§4.4）。
3. **不在缺集上下文时猜目标。** 解析不出唯一目标就建 `clarification_request`，
   选项 ≥ 2 且每一支带可量化后果（§1.4、§3.3）。
4. **不把"审核已通过"当放行依据。** apply 时重新 evaluate 政策、重新读
   manifest 与 fence，任一不符即 409，且不自动重算计划（§4.1 步骤 5–7）。

两处与方案散文不同、已在 `CONTRACT_GAPS` 登记的地方：

- Saga 的 `command_id` 按**步骤名**派生而不是 `step_n`（S07-19）：跨集多一步，
  用序号会让同一个 n 在两种步数下指不同步，崩溃恢复时会换键。
- 跨集 apply 的第六步（`COMMIT_MEMORY`）只在 `memory_migration_required` 为真
  时执行，否则记 `SKIPPED`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    CLARIFICATION_KINDS,
    CONTRACT_GAPS,
    ID_PREFIX,
    PLAN_TRANSITIONS,
    SAGA_TRANSITIONS,
    SCHEMA,
    TERMINAL_PLAN_STATES,
)

SOURCE = "S07"
SCHEMA_VERSION = "2.0"

EVENT_APPLIED = "change.plan.applied"
LOCAL_PLANNED = "change.plan.planned"
TOPIC_CHANGE = "drama.change-management.v2"

# §4.7：默认 30 分钟，可配 5–120。
DEFAULT_TTL_SECONDS = 30 * 60
MIN_TTL_SECONDS = 5 * 60
MAX_TTL_SECONDS = 120 * 60

# §4.2 的七步，加 §4.5 的 S16 提交一步。`(步骤名, 责任服务)`——command_id 按
# 名字派生（S07-19），所以这个名字是**持久化语义**的一部分，不能随便改。
SAGA_STEPS = (
    ("CHECK_MANIFEST", None),
    ("RECHECK_POLICY", "S02"),
    ("VERIFY_PREPARED", "S06"),
    ("CHECK_PATCH", None),
    ("PREPARE_CONTENT", None),
    ("COMMIT_MEMORY", "S16"),
    ("COMMIT_MANIFEST", "S08"),
    ("EMIT_APPLIED", None),
)

# `execution-context.v2` 里每次命令都会变的三个字段。`plan_hash` 的输入必须
# 把这三个排除，否则同一份计划在两个 command_id 下会得到两个 hash（§6.2）。
VOLATILE_CONTEXT_KEYS = ("command_id", "request_hash", "trace_id")

# C07-2：`node_id` / `node_epoch` 是单节点语义，不进计划、不进 hash。
# `tenant_id` 也不进——它已经由 `(tenant_id, plan_id)` 主键绑定。
NON_PLAN_CONTEXT_KEYS = VOLATILE_CONTEXT_KEYS + ("node_id", "node_epoch",
                                                 "tenant_id")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now() -> str:
    return _fmt(_now_dt())


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)


def _parse_any_ts(text: str) -> datetime:
    """宽松解析：S07 自己写 `...Z`，`idempotency_sdk` 写 `isoformat()`
    （`+00:00`）。两边的时间戳放在同一列里比较时**必须先解析**——按字符串
    比会把 `Z` (0x5A) 与 `+` (0x2B) 的字典序当成时间先后。
    """
    if text.endswith("Z"):
        return _parse_ts(text)
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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


def _stable_context(context: dict) -> dict:
    """`plan_hash` 与 `context_snapshot` 用的稳定子集（§6.2、C07-2）。"""
    return {k: v for k, v in context.items() if k not in NON_PLAN_CONTEXT_KEYS}


def plan_hash_of(body: dict) -> str:
    """§6.2：`sha256(JCS(plan 去掉 plan_hash 与 created_at))`。

    `expires_at` 也在排除之列——它与 `created_at` 由同一个 TTL 推出，且用户
    确认的是"这份内容 + 这个成本上界"，不是时钟。把时间戳冻进 hash 会让
    同一份计划的重放判成两份。
    """
    projection = {k: body.get(k) for k in (
        "schema_version", "context", "base_manifest_ref", "instruction",
        "prepared_revision_refs", "patches", "derived_adjustments", "impact",
        "memory_migration_required", "required_policy_decisions",
        "blocking_issues", "estimated_cost_upper_micro", "currency")}
    projection["context"] = _stable_context(projection.get("context") or {})
    return _sha256_hex(_jcs(projection))


def saga_command_id(plan_id: str, step_name: str) -> str:
    """§6.4 的确定性派生，按**步骤名**而不是序号（S07-19）。

    `base32(sha256(...))[:26]`：base32 只用 A–Z2–7，避免大小写不敏感的系统
    把两个 command_id 看成同一个。
    """
    digest = hashlib.sha256(
        f"{plan_id}|saga|{step_name}".encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower()
    return "cmd_" + encoded[:26]


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


# ============================================================ 指令解析

_CJK_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9}

_SCENE_RE = re.compile(r"第\s*([0-9零一二两三四五六七八九十]+)\s*场")
_SHOT_RE = re.compile(r"第\s*([0-9零一二两三四五六七八九十]+)\s*镜")

# 绝对时长："缩到 4 秒" / "改成 4 秒" / "设置为 4 秒" / "时长 4 秒"。
_ABS_DURATION_RE = re.compile(
    r"(?:缩到|改到|改成|调成|设置为|变更为|时长(?:为|是)?)\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*秒")
# 相对时长："缩 2 秒" / "缩短 2 秒" / "延长 2 秒"。
_REL_DURATION_RE = re.compile(
    r"(缩短|减少|缩|加长|延长|增加|长)\s*([0-9]+(?:\.[0-9]+)?)\s*秒")
_INCREASE_WORDS = ("加长", "延长", "增加", "长")

# "仰拍" → `/camera/angle` 的枚举值。**取值必须落在 S06 的枚举里**，
# 本表只做中文词到词元的映射；合法性由注入的 S06 schema 判定（S07-24）。
ANGLE_WORDS = {
    "仰拍": "low", "低角度": "low",
    "俯拍": "high", "高角度": "high",
    "平视": "eye_level", "平拍": "eye_level",
    "顶拍": "overhead", "上帝视角": "overhead",
    "荷兰角": "dutch", "斜角": "dutch",
}
ANGLE_ALIASES = {v: v for v in ("eye_level", "low", "high", "overhead", "dutch")}

FIELD_ANGLE = "/camera/angle"
FIELD_DURATION = "/duration/target_seconds"

# 5.2 秒是 E04 给出的对白最短时长。这个数不是 S07 算的——它由 S06/S37 的
# 对白检查给出；这里的常量只是"当上游没给时拿什么当例子"的护栏，值来自
# docs/07 §2 与方案 §4.1 步骤 3。
DEFAULT_DIALOGUE_MIN_MS = 5200


def _number(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CJK_DIGITS.get(head, 0) if head else 1
        ones = _CJK_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(token) == 1:
        return _CJK_DIGITS.get(token)
    return None


def parse_instruction(instruction: str) -> dict:
    """把人话拆成 `parsed_intent`（§4.1 步骤 0）。

    只做**词法**：目标编号、字段、操作数、原文片段。属性的合法性、把
    "仰拍"翻译成 S06 枚举里的哪个值、相对时长要读哪个当前值，都在拿到
    S06 的 schema/revision 之后才做——这里没有任何默认值可以猜。
    """
    targets: list[dict] = []
    scene = _SCENE_RE.search(instruction)
    shot = _SHOT_RE.search(instruction)
    if scene is not None and shot is not None:
        targets.append({"kind": "SHOT",
                        "scene_no": _number(scene.group(1)),
                        "shot_no_in_scene": _number(shot.group(1))})

    changes: list[dict] = []
    consumed: list[tuple[int, int]] = []
    for word, value in ANGLE_WORDS.items():
        index = instruction.find(word)
        if index >= 0:
            changes.append({"field": FIELD_ANGLE, "op": "SET", "value": value,
                            "source_text": word})
            consumed.append((index, index + len(word)))
            break

    absolute = _ABS_DURATION_RE.search(instruction)
    if absolute is not None:
        changes.append({"field": FIELD_DURATION, "op": "SET",
                        "value": float(absolute.group(1)),
                        "source_text": absolute.group(0)})
        consumed.append((absolute.start(), absolute.end()))
    else:
        relative = _REL_DURATION_RE.search(instruction)
        if relative is not None:
            delta = float(relative.group(2))
            if relative.group(1) in _INCREASE_WORDS:
                delta = -delta  # 统一成"缩多少"的代数：正数表示缩短
            changes.append({"field": FIELD_DURATION, "op": "DELTA",
                            "delta_seconds": delta,
                            "source_text": relative.group(0)})
            consumed.append((relative.start(), relative.end()))

    # 解析覆盖了哪些字符，剩下非空白、非标点的部分就是没读懂的话。
    covered = [False] * len(instruction)
    for start, end in consumed:
        for i in range(start, min(end, len(instruction))):
            covered[i] = True
    if scene is not None:
        for i in range(scene.start(), scene.end()):
            covered[i] = True
    if shot is not None:
        for i in range(shot.start(), shot.end()):
            covered[i] = True
    leftover = "".join(
        ch for i, ch in enumerate(instruction)
        if not covered[i] and ch not in " \t，,。.、；;：:\"'“”")
    unparsed = [leftover] if leftover else []

    ambiguities: list[dict] = []
    if not targets:
        ambiguities.append({"kind": "UNSUPPORTED_INSTRUCTION",
                            "detail": "指令里没有可定位的场景/镜头编号"})
    if not changes:
        ambiguities.append({"kind": "UNSUPPORTED_INSTRUCTION",
                            "detail": "指令里没有可映射到 S06 DSL 的字段"})
    return {"targets": targets, "changes": changes,
            "ambiguities": ambiguities, "unparsed_fragments": unparsed}


# ------------------------------------------------------------ 目标解析

def _fingerprint(fields: dict) -> str:
    """一个 shot 的字段快照哈希：`target_hash` 与补丁前置校验都用它。"""
    return _sha256_hex(_jcs(fields))


def _normalise_shot(raw: dict) -> dict:
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    return {"shot_id": raw.get("shot_id"),
            "episode_id": raw.get("episode_id"),
            "revision": int(raw.get("revision") or 0),
            "content_hash": raw.get("content_hash") or _fingerprint(fields),
            "fields": fields,
            # `layer` 必须留下：`_touches_narrative` 的第二个判据是"补丁目标
            # 在 S04 剧本层"（§4.5 步骤 1）。丢掉它，那条判据就成了永远为假
            # 的死代码——E08 的整条跨集路径也就走不到。
            "layer": raw.get("layer"),
            "summary": raw.get("summary") or ""}


def _match_targets(resolved: dict, targets: list[dict],
                   episode_id: str | None) -> dict:
    """把解析出的编号落到具体 shot 上（§4.1 的解析表）。

    返回 `{"shots": [...], "resolution": str, "candidates": [...]}`。
    `shots` 为空表示"这个 episode 里没有这个编号"，与"多个集都有"是两种
    不同的事实，不能合并成一种"找不到"。
    """
    shots = [_normalise_shot(s) for s in (resolved.get("shots") or [])]
    same_episode = [s for s in shots if s["episode_id"] == episode_id]
    if len(same_episode) == 1:
        return {"shots": same_episode, "candidates": [], "resolution":
                "PROJECT_LOCAL_NUMBERING"}
    if len(same_episode) > 1:
        return {"shots": [], "candidates": same_episode,
                "resolution": "AMBIGUOUS_SHOT"}
    if episode_id is None:
        if len(shots) == 1:
            return {"shots": shots, "candidates": [], "resolution":
                    "SERIES_UNIQUE_MATCH"}
        if len(shots) > 1:
            episodes = sorted({s["episode_id"] for s in shots})
            if len(episodes) == 1:
                # 多集才叫"缺集上下文"。同一个集里有两个同编号镜头，问的是
                # "哪一个镜头"，不是"哪一集"——按 MISSING_EPISODE_CONTEXT
                # 只会提炼出一个选项，而一个选项的"澄清"是命令（§3.3）。
                return {"shots": [], "candidates": shots,
                        "resolution": "AMBIGUOUS_SHOT"}
            return {"shots": [], "resolution": "MISSING_EPISODE_CONTEXT",
                    "candidates": [s for s in shots
                                   if s["episode_id"] in episodes]}
    return {"shots": [], "candidates": [], "resolution": "NO_MATCH"}


class ChangeManagementService:
    """`change_plan` / `change_saga` / `clarification_request` 的唯一写者。

    所有上游都是注入的闭包（`deps.build_*`），未配置时它们自己 503。内核里
    **没有**任何一个 `if upstream is None: use default` 的分支——AGENTS.md
    的红线是"派单前必须实时复查"，一个默认值会让这句话失效。
    """

    def __init__(self, conn: sqlite3.Connection, *,
                 shot_resolver=None, shot_preparer=None,
                 revision_reader=None, content_preparer=None,
                 impact_source=None, policy_source=None, cost_source=None,
                 manifest_source=None, manifest_committer=None,
                 revision_lister=None, manifest_revision_reader=None,
                 memory_preview=None,
                 memory_committer=None, clock=None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 必须在 executescript 之后、任何事务之外构造：SDK 的构造函数自己
        # 执行 DDL，而 executescript 会隐式提交。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)

        self.shot_resolver = shot_resolver or _unconfigured("SHOT_SOURCE")
        self.shot_preparer = shot_preparer or _unconfigured("SHOT_SOURCE")
        self.revision_reader = revision_reader or _unconfigured("CONTENT_SOURCE")
        self.content_preparer = content_preparer or _unconfigured(
            "CONTENT_SOURCE")
        self.impact_source = impact_source or _unconfigured("IMPACT_SOURCE")
        self.policy_source = policy_source or _unconfigured("POLICY_SOURCE")
        self.cost_source = cost_source or _unconfigured("COST_SOURCE")
        self.manifest_source = manifest_source or _unconfigured("MANIFEST_SOURCE")
        self.manifest_committer = manifest_committer or _unconfigured(
            "MANIFEST_SOURCE")
        self.revision_lister = revision_lister or _unconfigured(
            "MANIFEST_SOURCE")
        self.manifest_revision_reader = manifest_revision_reader or \
            _unconfigured("MANIFEST_SOURCE")
        self.memory_preview = memory_preview or _unconfigured("MEMORY_SOURCE")
        self.memory_committer = memory_committer or _unconfigured(
            "MEMORY_SOURCE")
        self.clock = clock or _now_dt
        self.ttl_seconds = max(MIN_TTL_SECONDS,
                               min(MAX_TTL_SECONDS, int(ttl_seconds)))

    # ------------------------------------------------------------ 内部工具

    def _now(self) -> str:
        return _fmt(self.clock())

    def _upstream(self, call, code: str):
        """把"上游不可达"折算成 503；上游明确说不行（4xx）原样上抛，不吞。

        5xx 也折算成 503：那是上游的故障，不是 S07 的 500。两者的区别在于
        "该不该重试"——上游 500 重试一次可能就好了，而把它当成本服务的 500
        会让调用方以为计划生成逻辑坏了，从而不再重试。
        吞掉一个 409 才是真事故：Saga 会把"明确的失败"记成"成功"。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(
                    503, code, f"upstream failed: {exc.code}: {exc}",
                    retryable=True) from exc
            raise

    def _row(self, sql: str, params: tuple):
        return self.conn.execute(sql, params).fetchone()

    def _plan_row(self, tenant_id: str, plan_id: str):
        row = self._row("SELECT * FROM change_plan WHERE tenant_id=? AND plan_id=?",
                        (tenant_id, plan_id))
        if row is None:
            # 跨租户与不存在同码：404 不确认存在性（E28）。
            raise ServiceError(404, "PLAN_NOT_FOUND", f"no change plan {plan_id}")
        return row

    def _set_state(self, tenant_id: str, plan_id: str, from_state: str,
                   to_state: str, *, extra: dict | None = None) -> None:
        allowed = PLAN_TRANSITIONS.get(from_state, set())
        if to_state not in allowed:
            raise ServiceError(
                500, "STATE_MACHINE_VIOLATION",
                f"change plan cannot go {from_state} -> {to_state}",
                details={"from": from_state, "to": to_state})
        assignments = ["state=?"]
        params: list = [to_state]
        for column, value in (extra or {}).items():
            assignments.append(f"{column}=?")
            params.append(value)
        params.extend([tenant_id, plan_id, from_state])
        cursor = self.conn.execute(
            "UPDATE change_plan SET " + ", ".join(assignments)
            + " WHERE tenant_id=? AND plan_id=? AND state=?",
            tuple(params))
        if cursor.rowcount != 1:
            # CAS 失败：另一个实例已经推进了状态。这不是"重试一下就好"，
            # 而是调用方读到的事实已经过期。
            raise ServiceError(409, "PLAN_NOT_PLANNED",
                               f"plan {plan_id} is no longer {from_state}")

    def _emit_local(self, tenant_id: str, event_type: str,
                    aggregate_id: str, payload: dict) -> None:
        """未登记的事件走本地表（`change.plan.planned` 不在 55 个 topic 里）。"""
        self.conn.execute(
            "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
            " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (tenant_id, _new_id("local_event"), event_type, aggregate_id,
             _jcs(payload), self._now()))

    def _require_context(self, context) -> dict:
        if not isinstance(context, dict):
            raise ServiceError(400, "INVALID_CONTEXT",
                               "context must be an execution-context object")
        missing = [k for k in ("schema_version", "project_id", "series_id",
                               "branch_id", "home_region", "home_cell",
                               "manifest_ref", "execution_fence",
                               "memory_checkpoint", "policy_ref", "actor")
                   if context.get(k) in (None, "")]
        # `episode_id` 可以缺：缺集上下文正是 E04 的第一种情形，它必须走到
        # 澄清，而不是在入口被 400 挡掉。
        manifest = context.get("manifest_ref")
        if not isinstance(manifest, dict) or not all(
                manifest.get(k) for k in ("id", "revision", "content_hash")):
            missing.append("manifest_ref{id,revision,content_hash}")
        fence = context.get("execution_fence")
        if not isinstance(fence, int) or fence < 1:
            missing.append("execution_fence>=1")
        policy = context.get("policy_ref")
        if not isinstance(policy, dict) or not all(
                policy.get(k) for k in ("service_id", "id", "revision",
                                        "content_hash")):
            missing.append("policy_ref{service_id,id,revision,content_hash}")
        if missing:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "execution-context is missing required fields",
                               details={"missing": missing})
        return context

    # ------------------------------------------------------------ 澄清

    def _create_clarification(self, tenant_id: str, plan_id: str,
                              kind: str, question: str,
                              options: list[dict]) -> dict:
        if kind not in CLARIFICATION_KINDS:
            raise ServiceError(500, "CLARIFICATION_KIND_UNKNOWN", kind)
        if len(options) < 2:
            # §2.3 的 DB 约束在这里提前给出一句人话：只有一个选项的情形不该
            # 问，应该直接做并记进 derived_adjustments。
            raise ServiceError(
                500, "CLARIFICATION_NEEDS_TWO_OPTIONS",
                "a clarification with fewer than two options is a command",
                details={"kind": kind, "option_count": len(options)})
        request_id = _new_id("request")
        self.conn.execute(
            "INSERT INTO clarification_request (tenant_id, plan_id, request_id,"
            " kind, question, options, created_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, plan_id, request_id, kind, question,
             _jcs(options), self._now()))
        return {"request_id": request_id, "kind": kind, "question": question,
                "options": [_public_option(o) for o in options]}

    def _raise_clarification(self, tenant_id: str, plan_id: str, kind: str,
                             question: str, options: list[dict]):
        clarification = self._create_clarification(
            tenant_id, plan_id, kind, question, options)
        self._set_state(tenant_id, plan_id, "PLANNING",
                        "CLARIFICATION_REQUIRED")
        raise ServiceError(
            409, "CLARIFICATION_REQUIRED",
            "需要确认后才能生成计划",
            details={"plan_id": plan_id, "clarification": clarification})

    def answer_clarification(self, tenant_id: str, plan_id: str,
                             request_id: str, body: dict) -> dict:
        """§4.6。答复不创建新 plan：`plan_id` 继续，`plan_hash` 重算。"""
        plan = self._plan_row(tenant_id, plan_id)
        if plan["state"] not in ("CLARIFICATION_REQUIRED", "PLANNING"):
            raise ServiceError(409, "PLAN_NOT_PLANNED",
                               f"plan {plan_id} is not awaiting an answer",
                               details={"state": plan["state"]})
        request = self._row(
            "SELECT * FROM clarification_request WHERE tenant_id=? AND"
            " plan_id=? AND request_id=?",
            (tenant_id, plan_id, request_id))
        if request is None:
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"no clarification {request_id}")
        if request["answered_at"] is not None:
            raise ServiceError(409, "PLAN_NOT_PLANNED",
                               "this clarification was already answered",
                               details={"selected_option_id":
                                        request["selected_option_id"]})
        if request["abandoned_at"] is not None:
            raise ServiceError(409, "PLAN_NOT_PLANNED",
                               "this clarification was abandoned")

        selected = body.get("selected_option_id")
        options = _loads(request["options"], [])
        chosen = next((o for o in options if o.get("option_id") == selected),
                      None)
        if chosen is None:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "selected_option_id is not one of the options",
                               details={"options": [o.get("option_id")
                                                    for o in options]})
        actor = body.get("actor") or _loads(plan["context_snapshot"], {}).get(
            "actor") or {"principal_id": "unknown",
                         "principal_type": "service",
                         "delegation_id": "unknown"}

        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE clarification_request SET selected_option_id=?,"
                " answered_by=?, answered_at=? WHERE tenant_id=? AND"
                " request_id=? AND answered_at IS NULL",
                (selected, _jcs(actor), self._now(), tenant_id, request_id))
            if plan["state"] == "CLARIFICATION_REQUIRED":
                self._set_state(tenant_id, plan_id, "CLARIFICATION_REQUIRED",
                                "PLANNING")
            else:
                self.conn.execute(
                    "UPDATE change_plan SET state='PLANNING' WHERE tenant_id=?"
                    " AND plan_id=? AND state='PLANNING'",
                    (tenant_id, plan_id))

        directive = chosen.get("apply") or {}
        if directive.get("cancel"):
            # §3.1 没有 CANCELED；"取消"= 什么都不做，让 plan 自然过期。
            #
            # 澄清本身是 **ANSWERED 而不是 ABANDONED**：§3.3 只允许
            # 「plan 过期 / 被 supersede」走 ABANDONED，而用户确实答复了。
            # 两者在库里互斥（`cr_abandoned_consistent`），也不该都写。
            #
            # 计划留在 PLANNING：它不能被 apply（只有 PLANNED 能），又还没
            # 过期，于是由 sweep_expired 收尾。没有任何外部事实被创建。
            return {"plan_id": plan_id, "state": "PLANNING",
                    "plan_hash": plan["plan_hash"],
                    "cancelled": True, "replayed": False}

        result = self._replan(tenant_id, plan, request, directive)
        return result

    def _replan(self, tenant_id: str, plan, request, directive: dict) -> dict:
        """带着答复重走 §4.1 的步骤 1–4（同一 `plan_id`）。"""
        intent = _loads(plan["parsed_intent"], {})
        context = _loads(plan["context_snapshot"], {})
        if directive.get("episode_id"):
            context["episode_id"] = directive["episode_id"]
        if directive.get("shot_id"):
            intent.setdefault("pinned", {})["shot_id"] = directive["shot_id"]
        if directive.get("duration_seconds") is not None:
            for change in intent.get("changes", []):
                if change.get("field") == FIELD_DURATION:
                    change["op"] = "SET"
                    change["value"] = float(directive["duration_seconds"])
                    change.pop("delta_seconds", None)
        if directive.get("rewrite_dialogue"):
            # S07 不生成内容（§0.1）。"重写对白"这一步的产物归 S04，S07
            # 只能把它记成阻塞项，不能假装准备好了。
            self.conn.execute(
                "UPDATE change_plan SET state='INFEASIBLE' WHERE tenant_id=?"
                " AND plan_id=?", (tenant_id, plan["plan_id"]))
            self.conn.execute(
                "UPDATE change_plan SET parsed_intent=? WHERE tenant_id=? AND"
                " plan_id=?", (_jcs(intent), tenant_id, plan["plan_id"]))
            return {"plan_id": plan["plan_id"], "state": "INFEASIBLE",
                    "plan_hash": plan["plan_hash"],
                    "blocking_issues": [
                        "该选项需要 S04 重写对白；S07 不生成内容（方案 §0.1）。"
                        "请先提交剧本修改，再重新生成本计划。"],
                    "replayed": False}

        body = {"context": context,
                "instruction": plan["instruction"],
                "planning_budget_scope_id": None,
                "parsed_intent": intent}
        try:
            return self._plan(tenant_id, plan["plan_id"], body,
                              resume=True, request_id=request["request_id"],
                              directive=directive)
        except ServiceError as exc:
            if exc.code == "CLARIFICATION_REQUIRED":
                # 答复后仍然要澄清（例如延到 5.2 秒后对白还是放不下）：
                # 这是新的事实，不是同一个询问的重放。
                raise
            raise

    # ------------------------------------------------------------ 生成计划

    def create_plan(self, tenant_id: str, command_id: str, body: dict) -> dict:
        """`POST /change-plans`（§4.1 步骤 0–4）。"""
        context = self._require_context(body.get("context"))
        inside = context.get("tenant_id")
        if inside is not None and inside != tenant_id:
            # AGENTS.md：上下文里的 tenant_id 只用于比对，不是可信输入。
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id disagrees with the credential")
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ServiceError(400, "INVALID_CONTEXT",
                               "instruction must be a non-empty string")

        intent = parse_instruction(instruction)
        if not intent["targets"] or not intent["changes"]:
            raise ServiceError(
                422, "INSTRUCTION_UNPARSEABLE",
                "无法从这句指令里解析出目标与字段",
                details={"ambiguities": intent["ambiguities"],
                         "unparsed_fragments": intent["unparsed_fragments"]})

        disposition = body.get("memory_disposition") or "MARK_STALE"
        if disposition not in ("MARK_STALE", "MIGRATE_AND_REDO"):
            raise ServiceError(
                400, "INVALID_CONTEXT",
                "memory_disposition must be MARK_STALE or MIGRATE_AND_REDO",
                details={"memory_disposition": disposition})

        created = self._now()
        expires = _fmt(self.clock() + timedelta(seconds=self.ttl_seconds))
        plan_id = _new_id("plan")
        snapshot = _stable_context(context)
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO change_plan (tenant_id, plan_id, plan_hash,"
                " branch_id, series_id, episode_id, instruction, parsed_intent,"
                " base_manifest_ref, base_revision, context_snapshot,"
                " execution_fence, memory_checkpoint, policy_version,"
                " policy_ref, impact_analysis_id, impact_watermark, state,"
                " memory_migration_required, memory_disposition,"
                " estimated_cost_upper_micro, currency, created_by, created_at,"
                " expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, plan_id, "0" * 64, context["branch_id"],
                 context["series_id"], context.get("episode_id") or "",
                 instruction, _jcs(intent), _jcs(context["manifest_ref"]),
                 int(context["manifest_ref"]["revision"]), _jcs(snapshot),
                 int(context["execution_fence"]),
                 _jcs(context["memory_checkpoint"]),
                 str((context.get("policy_ref") or {}).get("revision")),
                 _jcs(context["policy_ref"]), None, 0, "DRAFT", 0, disposition,
                 0, "XXX", _jcs(context.get("actor") or {}), created, expires))
            self._set_state(tenant_id, plan_id, "DRAFT", "PLANNING")

        return self._plan(tenant_id, plan_id,
                          {"context": context, "instruction": instruction,
                           "planning_budget_scope_id":
                               body.get("planning_budget_scope_id"),
                           "parsed_intent": intent,
                           "memory_disposition": body.get("memory_disposition")},
                          resume=False)

    def _plan(self, tenant_id: str, plan_id: str, body: dict, *,
              resume: bool, request_id: str | None = None,
              directive: dict | None = None) -> dict:
        context = body["context"]
        intent = body["parsed_intent"]
        instruction = body["instruction"]
        plan = self._plan_row(tenant_id, plan_id)
        episode_id = context.get("episode_id") or None
        series_id = context["series_id"]

        target = intent["targets"][0]
        pinned = (intent.get("pinned") or {}).get("shot_id")
        resolved = self._upstream(
            lambda: self.shot_resolver(tenant_id, series_id, episode_id,
                                       target.get("scene_no"),
                                       target.get("shot_no_in_scene"),
                                       pinned),
            "DEPENDENCY_UNAVAILABLE") or {}
        match = _match_targets(resolved, intent["targets"], episode_id)

        if match["resolution"] == "NO_MATCH":
            raise ServiceError(
                422, "INSTRUCTION_UNPARSEABLE",
                "这个集里找不到指令指的场景/镜头",
                details={"episode_id": episode_id,
                         "scene_no": target.get("scene_no"),
                         "shot_no_in_scene": target.get("shot_no_in_scene")})
        if match["resolution"] == "MISSING_EPISODE_CONTEXT":
            options = [_episode_option(c, i) for i, c in
                       enumerate(_one_per_episode(match["candidates"]))]
            self._raise_clarification(
                tenant_id, plan_id, "MISSING_EPISODE_CONTEXT",
                "这句修改在多个集里都能对上，请先选一集：", options)
        if match["resolution"] == "AMBIGUOUS_SHOT":
            options = [_shot_option(c, i) for i, c in
                       enumerate(match["candidates"])]
            self._raise_clarification(
                tenant_id, plan_id, "AMBIGUOUS_SHOT",
                "这个集里有两个镜头都叫这个名字（分镜未定稿），请选一个：",
                options)

        shot = match["shots"][0]
        warnings: list[str] = []
        if match["resolution"] == "SERIES_UNIQUE_MATCH":
            warnings.append(
                "指令没有指定集；series 内只有一个集能对上，已按该集继续"
                "（SERIES_UNIQUE_MATCH）。")

        patches = self._build_patches(shot, intent["changes"])
        prepared, derived, dialogue_min = self._prepare(
            tenant_id, shot, intent["changes"], patches)

        duration_patch = next(
            (p for p in patches if p["path"] == FIELD_DURATION), None)
        target_seconds = (duration_patch or {}).get("after")
        threshold_ms = dialogue_min if dialogue_min is not None \
            else DEFAULT_DIALOGUE_MIN_MS
        if (target_seconds is not None and dialogue_min is not None
                and float(target_seconds) * 1000 < float(dialogue_min)):
            self._raise_clarification(
                tenant_id, plan_id, "DIALOGUE_EXCEEDS_DURATION",
                f"{_seconds(target_seconds)}秒容不下这段对白"
                f"（最短需{_seconds(threshold_ms / 1000)}秒），请选择：",
                _dialogue_options(target_seconds, threshold_ms, patches, shot))

        narrative = self._touches_narrative(shot, intent["changes"])
        memory = {"memory_migration_required": False, "preview": None}
        if narrative:
            # §4.5 步骤 2：先 preview，**绝不**先提交。用户在看到影响之前，
            # 任何事实都不许变。
            preview = self._upstream(
                lambda: self.memory_preview(tenant_id, series_id, {
                    "episode_id": shot["episode_id"],
                    "branch_id": context["branch_id"],
                    "instruction": instruction,
                    "changes": intent["changes"],
                    "base_checkpoint": context["memory_checkpoint"],
                }), "DEPENDENCY_UNAVAILABLE") or {}
            memory = {"memory_migration_required":
                      bool(preview.get("memory_migration_required")),
                      "preview": preview}

        disposition = plan["memory_disposition"]
        redo_delivered = (disposition == "MIGRATE_AND_REDO"
                          and bool(_stale_episodes(memory["preview"])))
        impact_query = {
            "base_manifest_ref": context["manifest_ref"],
            "branch_id": context["branch_id"],
            "episode_id": shot["episode_id"],
            "execution_fence": int(context["execution_fence"]),
            "seed": {"refs": [{
                "service_id": "S06", "id": shot["shot_id"],
                "revision": shot["revision"],
                "fields": [p["path"] for p in patches]}]},
            # MARK_STALE：已交付集只标 stale。MIGRATE_AND_REDO：它们进重做
            # 集合，成本因此显著上升（T07-23/T07-24）。
            "memory_stale_episode_ids": _stale_episodes(memory["preview"]),
            "memory_disposition": disposition,
            "redo_delivered_episode_ids": (
                _stale_episodes(memory["preview"]) if redo_delivered else []),
        }
        analysis = self._upstream(
            lambda: self.impact_source(tenant_id, impact_query),
            "DEPENDENCY_UNAVAILABLE") or {}
        analysis_id = analysis.get("analysis_id")
        if not analysis_id or not isinstance(analysis.get("impact"), dict):
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               "S09 returned no impact analysis")
        impact = _impact_of(analysis)
        # T07-04/T07-05：派生改动来自 S06（`_prepare` 记进 change_plan_patch），
        # S09 的 `derived_adjustments` 是影响闭包自己的叙述。两者都写进
        # `derived_adjustments`，但规则名只在上游给的时候才有。
        derived_adjustments = _describe_derived(derived) + _derived_adjustments(
            analysis)

        cost = self._upstream(
            lambda: self.cost_source(tenant_id, {
                "episode_id": shot["episode_id"],
                "impact": impact,
                "patches": patches,
                "currency_hint": "USD",
            # 传输层失败（不可达 / 上游 5xx）一律 §5.7 的
            # DEPENDENCY_UNAVAILABLE；"S25 答了但没给上界"是语义失败，另给
            # COST_SOURCE_UNAVAILABLE（S07-34）。
            }), "DEPENDENCY_UNAVAILABLE") or {}
        upper = cost.get("estimated_cost_upper_micro")
        currency = cost.get("currency")
        if not isinstance(upper, int) or upper < 0 or not currency:
            # 没有上界就不出计划：用户确认的东西里必须有"最多花多少"。
            raise ServiceError(503, "COST_SOURCE_UNAVAILABLE",
                               "S25 returned no cost upper bound",
                               retryable=True)

        policy = self._upstream(
            lambda: self.policy_source(tenant_id, context["policy_ref"],
                                       "change_application", {
                                           "episode_id": shot["episode_id"],
                                           "impact": impact,
                                           "patches": patches,
                                           "actor": context.get("actor"),
                                           # §4.5：MIGRATE_AND_REDO 要过
                                           # budget_increase gate，所以策略
                                           # 必须看得到成本与处置。
                                           "estimated_cost_upper_micro": upper,
                                           "memory_disposition": disposition,
                                           "redo_delivered_episode_ids":
                                               impact_query[
                                                   "redo_delivered_episode_ids"],
                                       }),
            # 传输层失败 → DEPENDENCY_UNAVAILABLE；"S02 答了但结论不可用"是
            # 语义失败，另给 POLICY_UNAVAILABLE（S07-34）。
            "DEPENDENCY_UNAVAILABLE") or {}
        decision = (policy.get("decision") or "").lower()
        blocking = list(policy.get("blocking_issues") or [])
        if decision == "deny":
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE change_plan SET parsed_intent=? WHERE tenant_id=?"
                    " AND plan_id=?",
                    (_jcs(intent), tenant_id, plan_id))
                self._set_state(tenant_id, plan_id, "PLANNING", "POLICY_DENIED")
            raise ServiceError(422, "POLICY_DENIED",
                               "政策拒绝这次修改",
                               details={"reason": policy.get("reason"),
                                        "blocking_issues": blocking})
        if decision not in ("allow", "permit", "require_human"):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                f"S02 returned an unusable decision {decision!r}",
                retryable=True)

        plan_body = {
            "schema_version": SCHEMA_VERSION,
            "plan_id": plan_id,
            "plan_hash": "",
            "context": _stable_context(context),
            "base_manifest_ref": context["manifest_ref"],
            "instruction": instruction,
            "prepared_revision_refs": [prepared] if prepared else [],
            # 只发用户要求的那几条；派生补丁在 change_plan_patch 里。
            "patches": [_public_patch(p) for p in patches
                        if p.get("derived_from") is None],
            "derived_adjustments": derived_adjustments,
            "impact": impact,
            "memory_migration_required":
                bool(memory["memory_migration_required"]),
            "required_policy_decisions":
                list(policy.get("required_decisions")
                     or ["node_change_allowed"]),
            "blocking_issues": blocking,
            "estimated_cost_upper_micro": upper,
            "currency": currency,
            "expires_at": plan["expires_at"],
        }
        plan_body["plan_hash"] = plan_hash_of(plan_body)

        summary = _impact_summary(analysis, impact, memory["preview"],
                                  disposition)
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE change_plan SET plan_hash=?, parsed_intent=?,"
                " episode_id=?, base_manifest_ref=?, base_revision=?,"
                " context_snapshot=?, memory_checkpoint=?, policy_ref=?,"
                " policy_version=?, impact_analysis_id=?, impact_watermark=?,"
                " memory_migration_required=?, estimated_cost_upper_micro=?,"
                " currency=?, plan_body=? WHERE tenant_id=? AND plan_id=?",
                (plan_body["plan_hash"], _jcs(intent), shot["episode_id"],
                 _jcs(context["manifest_ref"]),
                 int(context["manifest_ref"]["revision"]),
                 _jcs(plan_body["context"]),
                 _jcs(context["memory_checkpoint"]),
                 _jcs(context["policy_ref"]),
                 str(policy.get("policy_version")
                     or context["policy_ref"]["revision"]),
                 analysis_id, int(impact.get("lineage_watermark") or 0),
                 1 if memory["memory_migration_required"] else 0,
                 upper, currency, _jcs(plan_body), tenant_id, plan_id))
            self.conn.executemany(
                "INSERT INTO change_plan_patch (tenant_id, plan_id, patch_seq,"
                " target_service_id, target_id, target_revision, target_hash,"
                " path, before_value, after_value, derived_from,"
                " derivation_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(tenant_id, plan_id, p["patch_seq"], p["target_service_id"],
                  p["target_id"], p["target_revision"], p["target_hash"],
                  p["path"], _jcs(p["before"]), _jcs(p["after"]),
                  p.get("derived_from"), p.get("derivation_rule"))
                 for p in patches])
            self.conn.execute(
                "INSERT OR REPLACE INTO impact_summary (tenant_id, plan_id,"
                " analysis_id, totals, by_episode, by_stage, cost_breakdown,"
                " warnings, lineage_watermark) VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, plan_id, analysis_id, _jcs(summary["totals"]),
                 _jcs(summary["by_episode"]), _jcs(summary["by_stage"]),
                 _jcs(summary["cost_breakdown"]),
                 _jcs(warnings + summary["warnings"]),
                 int(impact.get("lineage_watermark") or 0)))
            self._set_state(tenant_id, plan_id, "PLANNING", "PLANNED")
            # §3.1：同 base、同集、新的 plan 生成 → 旧的 SUPERSEDED。
            self.conn.execute(
                "UPDATE change_plan SET state='SUPERSEDED' WHERE tenant_id=?"
                " AND series_id=? AND branch_id=? AND episode_id=?"
                " AND base_revision=? AND state='PLANNED' AND plan_id<>?",
                (tenant_id, context["series_id"], context["branch_id"],
                 shot["episode_id"], int(context["manifest_ref"]["revision"]),
                 plan_id))
            self._emit_local(tenant_id, LOCAL_PLANNED, plan_id, {
                "plan_id": plan_id, "plan_hash": plan_body["plan_hash"],
                "state": "PLANNED",
                "regenerate_node_ids": impact.get("regenerate_node_ids") or []})

        return {**plan_body, "state": "PLANNED", "replayed": False,
                "clarification": None}

    # ---- 计划生成的分步 ------------------------------------------------

    def _build_patches(self, shot: dict, changes: list[dict]) -> list[dict]:
        patches: list[dict] = []
        for seq, change in enumerate(changes, start=1):
            path = change["field"]
            current = shot["fields"].get(path)
            if current is None:
                # 相对时长要读当前值做算术；读不到就不能猜一个 6。
                raise ServiceError(
                    422, "INSTRUCTION_UNPARSEABLE",
                    f"无法读取 {path} 的当前值，无法计算相对时长",
                    details={"path": path, "shot_id": shot["shot_id"]})
            if change["op"] == "SET":
                after = change["value"]
            else:
                try:
                    after = float(current) - float(change["delta_seconds"])
                except (TypeError, ValueError) as exc:
                    raise ServiceError(
                        422, "INSTRUCTION_UNPARSEABLE",
                        f"{path} 的当前值不是数字，无法做相对修改",
                        details={"path": path, "current": current}) from exc
                if after <= 0:
                    raise ServiceError(
                        422, "CHANGE_INFEASIBLE",
                        "缩短后的时长不是正数",
                        details={"path": path, "result": after})
                after = int(after) if float(after).is_integer() else round(after, 3)
            if after == current:
                continue
            patches.append({
                "patch_seq": len(patches) + 1,
                "target_service_id": "S06",
                "target_id": shot["shot_id"],
                "target_revision": shot["revision"],
                "target_hash": shot["content_hash"],
                "path": path,
                "before": current,
                "after": after,
            })
        if not patches:
            raise ServiceError(422, "CHANGE_INFEASIBLE",
                               "这条指令不会改变任何字段")
        return patches

    def _prepare(self, tenant_id: str, shot: dict, changes: list[dict],
                 patches: list[dict]):
        """S06 准备新 revision（§4.1 步骤 1A + 步骤 2）。

        派生改动是 **S06 算的**，这里只记录；`derivation_rule` 一律用上游
        给的名字，不在 S07 里硬编码规则（T07-05）。
        """
        prepared = self._upstream(
            lambda: self.shot_preparer(tenant_id, {
                "shot_id": shot["shot_id"], "episode_id": shot["episode_id"],
                "revision": shot["revision"],
                "content_hash": shot["content_hash"],
                "fields": shot["fields"], "changes": changes,
            }), "DEPENDENCY_UNAVAILABLE") or {}
        ref = prepared.get("prepared_ref")
        if not isinstance(ref, dict) or not all(
                ref.get(k) for k in ("service_id", "id", "revision",
                                     "content_hash")):
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               "S06 returned no prepared revision")
        allowed = prepared.get("allowed_values") or {}
        for patch in patches:
            values = allowed.get(patch["path"])
            if isinstance(values, list) and patch["after"] not in values:
                # 词元不在 S06 的枚举里：这是"做不到"，不是"要让用户选"。
                raise ServiceError(
                    422, "INSTRUCTION_UNPARSEABLE",
                    f"{patch['path']} 不接受取值 {patch['after']!r}",
                    details={"path": patch["path"],
                             "allowed": values})

        derived: list[dict] = []
        for item in prepared.get("derived") or []:
            path = item.get("path")
            if not path or path not in prepared.get("fields", {}):
                continue
            trigger = next((p for p in patches
                            if p["path"] == item.get("derived_from_path")),
                           patches[0])
            derived.append({
                "patch_seq": len(patches) + len(derived) + 1,
                "target_service_id": "S06",
                "target_id": shot["shot_id"],
                "target_revision": shot["revision"],
                "target_hash": shot["content_hash"],
                "path": path,
                "before": shot["fields"].get(path),
                "after": prepared["fields"][path],
                "derived_from": str(trigger["patch_seq"]),
                "derivation_rule": item.get("derivation_rule"),
            })
        # 派生补丁也进 change_plan_patch，但响应里的 patches 只放用户要求的
        # 那几条（C07-9：schema 表达不了派生）。
        patches.extend(derived)
        # 响应里的 `patches` 只放用户要求的那几条（C07-9：schema 表达不了
        # 派生关系）；派生补丁留在 `change_plan_patch` 里可追溯。
        return ref, derived, prepared.get("dialogue_min_duration_ms")

    def _touches_narrative(self, shot: dict, changes: list[dict]) -> bool:
        """§4.5 步骤 1：补丁是否触及剧情事实（S04 剧本层 / 角色状态 / 归属 / 知情）。"""
        for change in changes:
            path = change["field"]
            if path.startswith(("/script", "/facts", "/claims",
                                "/character_state", "/ownership",
                                "/knowledge")):
                return True
        return shot.get("layer") == "S04"


# ------------------------------------------------------------ 查询

    def get_plan(self, tenant_id: str, plan_id: str) -> dict:
        """§5.3。契约 `additionalProperties:false`，所以只回纯 plan。"""
        row = self._plan_row(tenant_id, plan_id)
        body = _loads(row["plan_body"], None)
        if body is None:
            # DRAFT / PLANNING / 被拒绝的行没有契约实例。回它的状态与已知
            # 字段，而不是伪造一份 patches（`patches.minItems:1`）。
            return {"schema_version": SCHEMA_VERSION, "plan_id": plan_id,
                    "plan_hash": row["plan_hash"], "state": row["state"],
                    "instruction": row["instruction"], "blocking_issues": [],
                    "patches": [], "prepared_revision_refs": [],
                    "derived_adjustments": [],
                    "impact": {"regenerate_node_ids": [],
                               "recheck_node_ids": [],
                               "reuse_artifact_refs": [],
                               "affected_episode_ids": [],
                               "superseded_claim_refs": [],
                               "lineage_watermark": row["impact_watermark"]},
                    "estimated_cost_upper_micro":
                        row["estimated_cost_upper_micro"],
                    "currency": row["currency"],
                    "memory_migration_required":
                        bool(row["memory_migration_required"])}
        return body

    def plan_state(self, tenant_id: str, plan_id: str) -> dict:
        row = self._plan_row(tenant_id, plan_id)
        return {"plan_id": row["plan_id"], "state": row["state"],
                "plan_hash": row["plan_hash"], "saga_id": row["saga_id"],
                "expires_at": row["expires_at"],
                "memory_stale": bool(row["memory_stale"]),
                "memory_disposition": row["memory_disposition"]}

    def get_saga(self, tenant_id: str, plan_id: str) -> dict:
        self._plan_row(tenant_id, plan_id)
        saga = self._row(
            "SELECT * FROM change_saga WHERE tenant_id=? AND plan_id=?"
            " ORDER BY started_at DESC LIMIT 1", (tenant_id, plan_id))
        if saga is None:
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"plan {plan_id} has no saga")
        steps = [dict(r) for r in self.conn.execute(
            "SELECT * FROM change_saga_step WHERE tenant_id=? AND saga_id=?"
            " ORDER BY step_seq", (tenant_id, saga["saga_id"])).fetchall()]
        return {
            "saga": {"saga_id": saga["saga_id"], "plan_id": saga["plan_id"],
                     "state": saga["state"],
                     "current_step": saga["current_step"],
                     "started_at": saga["started_at"],
                     "finished_at": saga["finished_at"],
                     "failure_code": saga["failure_code"],
                     "failure_detail": _loads(saga["failure_detail"], None)},
            "steps": [{"step_seq": s["step_seq"], "step_name": s["step_name"],
                       "target_service_id": s["target_service_id"],
                       "command_id": s["command_id"], "state": s["state"],
                       "error_code": s["error_code"],
                       "started_at": s["started_at"],
                       "finished_at": s["finished_at"],
                       "response": _loads(s["response"], None)}
                      for s in steps],
        }

    def get_impact_summary(self, tenant_id: str, plan_id: str) -> dict:
        plan = self._plan_row(tenant_id, plan_id)
        row = self._row("SELECT * FROM impact_summary WHERE tenant_id=? AND"
                        " plan_id=?", (tenant_id, plan_id))
        if row is None:
            raise ServiceError(404, "PLAN_NOT_FOUND",
                               f"plan {plan_id} has no impact summary")
        return {"plan_id": plan_id, "analysis_id": row["analysis_id"],
                "totals": _loads(row["totals"], {}),
                "by_episode": _loads(row["by_episode"], []),
                "by_stage": _loads(row["by_stage"], []),
                "cost_breakdown": _loads(row["cost_breakdown"], []),
                "warnings": _loads(row["warnings"], []),
                "lineage_watermark": row["lineage_watermark"],
                # §4.5 步骤 4：用户看到影响时要知道记忆怎么迁移、已交付集
                # 会被怎么处置。两者都是计划生成时的决定，所以读的是 plan 行。
                "memory_migration_required": bool(
                    plan["memory_migration_required"]),
                "memory_disposition": plan["memory_disposition"],
                "memory_stale": bool(plan["memory_stale"])}

    def get_clarifications(self, tenant_id: str, plan_id: str) -> dict:
        self._plan_row(tenant_id, plan_id)
        rows = self.conn.execute(
            "SELECT * FROM clarification_request WHERE tenant_id=? AND"
            " plan_id=? ORDER BY created_at", (tenant_id, plan_id)).fetchall()
        return {"plan_id": plan_id, "items": [{
            "request_id": r["request_id"], "kind": r["kind"],
            "question": r["question"], "options": _loads(r["options"], []),
            "selected_option_id": r["selected_option_id"],
            "answered_at": r["answered_at"],
            "abandoned_at": r["abandoned_at"],
        } for r in rows]}

    def list_plans(self, tenant_id: str, *, series_id: str | None = None,
                   episode_id: str | None = None, state: str | None = None,
                   after: str | None = None, limit: int = 50) -> dict:
        clauses = ["tenant_id=?"]
        params: list = [tenant_id]
        for column, value in (("series_id", series_id),
                              ("episode_id", episode_id), ("state", state)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if after:
            clauses.append("created_at < ?")
            params.append(after)
        params.append(max(1, min(200, int(limit))))
        rows = self.conn.execute(
            "SELECT plan_id, state, plan_hash, episode_id, instruction,"
            " estimated_cost_upper_micro, currency, created_at, expires_at"
            " FROM change_plan WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?", tuple(params)).fetchall()
        return {"items": [dict(r) for r in rows]}


    # ------------------------------------------------------------ apply

    def apply(self, tenant_id: str, plan_id: str, command_id: str,
              body: dict) -> dict:
        """§4.1 步骤 5：三道闸 → 建 Saga → 逐步推进。

        闸门检查**不自动重算计划**：任一处不符就 409。用户确认的是那一份。
        """
        plan = self._plan_row(tenant_id, plan_id)
        active = self._row(
            "SELECT saga_id FROM change_saga WHERE tenant_id=? AND plan_id=?"
            " AND state='RUNNING'", (tenant_id, plan_id))
        if active is not None:
            # §6.3：S07 不需要自己的分布式锁，但 Saga 要防重入。
            #
            # 这一条必须排在状态检查**前面**：崩溃留下的正是
            # 「state=APPLYING + Saga RUNNING」这一对，而按状态先判只会得到
            # 一句"计划不是 PLANNED"，把"另一个 apply 正在飞（或死在那儿，
            # 该走 :recover）"这个可操作的事实丢掉。T07-35 要的就是这个码。
            raise ServiceError(409, "CONCURRENT_CHANGE",
                               "another apply is running for this plan",
                               details={"saga_id": active["saga_id"]})
        if plan["state"] == "EXPIRED":
            raise ServiceError(409, "PLAN_EXPIRED",
                               "plan has expired; regenerate it",
                               retryable=True)
        if plan["state"] != "PLANNED":
            raise ServiceError(409, "PLAN_NOT_PLANNED",
                               f"plan {plan_id} is {plan['state']}",
                               details={"state": plan["state"]})

        provided_hash = body.get("plan_hash")
        if provided_hash != plan["plan_hash"]:
            raise ServiceError(409, "PLAN_HASH_MISMATCH",
                               "the plan you confirmed is not this plan",
                               details={"current_plan_hash": plan["plan_hash"]})
        expected = body.get("expected_manifest_revision")
        if expected is None:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "expected_manifest_revision is required")
        if int(expected) != int(plan["base_revision"]):
            raise ServiceError(
                409, "MANIFEST_REVISION_CONFLICT",
                "the manifest moved since this plan was generated",
                details={"expected": int(expected),
                         "plan_base_revision": int(plan["base_revision"])})
        if self.clock() >= _parse_ts(plan["expires_at"]):
            with Transaction(self.conn):
                self._set_state(tenant_id, plan_id, "PLANNED", "EXPIRED")
                self._abandon_clarifications(tenant_id, plan_id)
            raise ServiceError(409, "PLAN_EXPIRED",
                               "plan has expired; regenerate it",
                               retryable=True)
        if plan["memory_stale"]:
            # §5.6：记忆 checkpoint 在计划生成后变过 —— 影响闭包是用旧
            # checkpoint 算的，不能照它执行。
            raise ServiceError(409, "STALE_MEMORY",
                               "memory checkpoint changed since planning",
                               details={"reason": "memory.canon.committed"})

        saga_id = _new_id("saga")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO change_saga (tenant_id, saga_id, plan_id, state,"
                " current_step, started_at) VALUES (?,?,?,?,?,?)",
                (tenant_id, saga_id, plan_id, "RUNNING", 0, self._now()))
            self.conn.executemany(
                "INSERT INTO change_saga_step (tenant_id, saga_id, step_seq,"
                " step_name, target_service_id, command_id, state)"
                " VALUES (?,?,?,?,?,?,?)",
                [(tenant_id, saga_id, seq, name, service,
                  saga_command_id(plan_id, name), "PENDING")
                 for seq, (name, service) in enumerate(SAGA_STEPS, start=1)])
            self.conn.execute(
                "UPDATE change_plan SET saga_id=? WHERE tenant_id=? AND"
                " plan_id=?", (saga_id, tenant_id, plan_id))
            self._set_state(tenant_id, plan_id, "PLANNED", "APPLYING")

        self._run_saga(tenant_id, plan_id, saga_id)
        return {"operation_id": saga_id, "state": "accepted",
                "plan_id": plan_id, "saga_id": saga_id}

    def _step(self, tenant_id: str, saga_id: str, name: str):
        row = self._row("SELECT * FROM change_saga_step WHERE tenant_id=? AND"
                        " saga_id=? AND step_name=?",
                        (tenant_id, saga_id, name))
        if row is None:
            raise ServiceError(500, "SAGA_STEP_MISSING", name)
        return row

    def _mark_step(self, tenant_id: str, saga_id: str, name: str,
                   state: str, *, response=None, error_code=None) -> None:
        self.conn.execute(
            "UPDATE change_saga_step SET state=?, response=?, error_code=?,"
            " started_at=COALESCE(started_at, ?), finished_at=? WHERE"
            " tenant_id=? AND saga_id=? AND step_name=?",
            (state, None if response is None else _jcs(response), error_code,
             self._now(),
             None if state in ("PENDING", "RUNNING") else self._now(),
             tenant_id, saga_id, name))

    def _finish_saga(self, tenant_id: str, saga_id: str, state: str, *,
                     code: str | None = None, detail: dict | None = None):
        if state == "RUNNING":
            return
        self.conn.execute(
            "UPDATE change_saga SET state=?, finished_at=?, failure_code=?,"
            " failure_detail=? WHERE tenant_id=? AND saga_id=? AND"
            " state='RUNNING'",
            (state, self._now(), code,
             None if detail is None else _jcs(detail), tenant_id, saga_id))

    def _run_saga(self, tenant_id: str, plan_id: str, saga_id: str) -> None:
        plan = self._plan_row(tenant_id, plan_id)
        context = _loads(plan["context_snapshot"], {})
        plan_body = _loads(plan["plan_body"], {})
        patches = [dict(r) for r in self.conn.execute(
            "SELECT * FROM change_plan_patch WHERE tenant_id=? AND plan_id=?"
            " ORDER BY patch_seq", (tenant_id, plan_id)).fetchall()]
        committed: dict = {}

        for seq, (name, _service) in enumerate(SAGA_STEPS, start=1):
            self.conn.execute("UPDATE change_saga SET current_step=? WHERE"
                              " tenant_id=? AND saga_id=?",
                              (seq, tenant_id, saga_id))
            self._mark_step(tenant_id, saga_id, name, "RUNNING")
            try:
                result, skipped = self._run_step(
                    tenant_id, plan, plan_body, context, patches, name,
                    saga_id, committed)
            except ServiceError as exc:
                if name == "COMMIT_MANIFEST" and exc.retryable:
                    # §4.3 第三行：可重试的失败（超时 / 5xx）意味着**不知道**
                    # 提交成没成——请求可能到了，只是答复没回来。落 FAILED 是
                    # 终态，会把"S08 可能已经前进"永久埋掉；落回 PLANNED 又
                    # 等于宣称没提交。
                    #
                    # 所以这一步保持 RUNNING、Saga 保持 RUNNING、计划保持
                    # APPLYING：`cs_plan_active_uniq` 挡住第二次 apply
                    # （409 CONCURRENT_CHANGE），唯一出路是
                    # `POST /change-plans/{id}:recover` 去 S08 查真话。
                    # 明确的 4xx（REVISION_CONFLICT / STALE_EXECUTION_FENCE）
                    # 不是"不知道"，走下面正常失败分支。
                    raise
                self._mark_step(tenant_id, saga_id, name, "FAILED",
                                response=exc.details or None,
                                error_code=exc.code)
                # 可重试的失败（abstain / 依赖不可达）不改 plan 状态：计划
                # 还站在 PLANNED，用户可以再 apply 一次。不可重试的才落 FAILED，
                # 因为再试同一份计划也只会得到同一个答案。
                if exc.retryable:
                    self._finish_saga(tenant_id, saga_id, "ABANDONED",
                                      code=exc.code, detail=exc.details)
                    self.conn.execute(
                        "UPDATE change_plan SET saga_id=NULL WHERE tenant_id=?"
                        " AND plan_id=? AND state='APPLYING'",
                        (tenant_id, plan_id))
                    self._set_state(tenant_id, plan_id, "APPLYING", "PLANNED")
                else:
                    self._finish_saga(tenant_id, saga_id, "FAILED",
                                      code=exc.code, detail=exc.details)
                    self._fail_plan(tenant_id, plan_id)
                raise
            except Exception as exc:  # noqa: BLE001 - 先记审计再上抛
                if name == "COMMIT_MANIFEST":
                    # 进程在这一步里死了：连"记一笔失败"都没做到。状态停
                    # 在 APPLYING + Saga RUNNING，只能靠 `:recover` 去 S08 查。
                    raise
                self._mark_step(tenant_id, saga_id, name, "FAILED",
                                error_code="INTERNAL_ERROR")
                self._finish_saga(tenant_id, saga_id, "FAILED",
                                  code="INTERNAL_ERROR",
                                  detail={"message": str(exc)})
                self._fail_plan(tenant_id, plan_id)
                raise
            self._mark_step(tenant_id, saga_id, name,
                            "SKIPPED" if skipped else "DONE",
                            response=result)

        self._finish_saga(tenant_id, saga_id, "SUCCEEDED")

    def _redo_delivered(self, tenant_id: str, plan) -> list[str]:
        """§4.5：`MIGRATE_AND_REDO` 下会被重做的已交付集。

        生成时这是 `_stale_episodes(preview)`；apply 时 preview 已经不在内存里，
        而 `impact_summary.by_episode` 是它落库后的形态（派生数据，可重建）。
        这两处必须给策略看同样的清单——否则同一个策略会因为「第几次问它」
        而给出不同结论。
        """
        if plan["memory_disposition"] != "MIGRATE_AND_REDO":
            return []
        row = self._row("SELECT by_episode FROM impact_summary WHERE"
                        " tenant_id=? AND plan_id=?",
                        (tenant_id, plan["plan_id"]))
        if row is None:
            return []
        return sorted({e["episode_id"]
                       for e in (_loads(row["by_episode"], []) or [])
                       if isinstance(e, dict) and e.get("delivered")})

    def _fail_plan(self, tenant_id: str, plan_id: str) -> None:
        self.conn.execute(
            "UPDATE change_plan SET state='FAILED' WHERE tenant_id=? AND"
            " plan_id=? AND state='APPLYING'", (tenant_id, plan_id))

    def _abandon_clarifications(self, tenant_id: str, plan_id: str) -> None:
        self.conn.execute(
            "UPDATE clarification_request SET abandoned_at=? WHERE tenant_id=?"
            " AND plan_id=? AND answered_at IS NULL AND abandoned_at IS NULL",
            (self._now(), tenant_id, plan_id))

    def _run_step(self, tenant_id: str, plan, plan_body: dict, context: dict,
                  patches: list[dict], name: str, saga_id: str,
                  committed: dict) -> tuple[dict, bool]:
        if name == "CHECK_MANIFEST":
            manifest = self._upstream(
                lambda: self.manifest_source(
                    tenant_id, context["manifest_ref"]["id"]),
                "DEPENDENCY_UNAVAILABLE") or {}
            current = manifest.get("current_revision")
            if current is not None and int(current) != int(plan["base_revision"]):
                raise ServiceError(
                    409, "MANIFEST_REVISION_CONFLICT",
                    "the manifest moved while the saga was starting",
                    details={"current_revision": int(current),
                             "base_revision": int(plan["base_revision"])})
            fence = manifest.get("execution_fence")
            if fence is not None and int(fence) != int(plan["execution_fence"]):
                # 期间发生过取消/故障切换：准备好的产物已经 VOIDED。
                raise ServiceError(
                    409, "STALE_EXECUTION_FENCE",
                    "execution fence moved since planning",
                    details={"current_fence": int(fence),
                             "plan_fence": int(plan["execution_fence"])})
            return {"current_revision": current,
                    "execution_fence": fence}, False

        if name == "RECHECK_POLICY":
            # §4.1 步骤 7：从 PLANNED 到这里可能过了几分钟。审核一次不够。
            policy = self._upstream(
                lambda: self.policy_source(
                    tenant_id, context["policy_ref"], "change_application",
                    {"episode_id": plan["episode_id"],
                     "impact": plan_body.get("impact") or {},
                     "patches": [_public_patch(p) for p in patches],
                     "actor": context.get("actor"),
                     # 复查必须看到**和生成时同样的事实**，否则「政策改严了」
                     # 与「你少喂了一个字段」看起来一样。成本与处置在
                     # PLANNED 之后不可变，从 plan 行取，不重新问 S25。
                     "estimated_cost_upper_micro": int(
                         plan["estimated_cost_upper_micro"]),
                     "memory_disposition": plan["memory_disposition"],
                     "redo_delivered_episode_ids":
                         self._redo_delivered(tenant_id, plan)}),
                "DEPENDENCY_UNAVAILABLE") or {}
            decision = (policy.get("decision") or "").lower()
            if decision == "deny":
                raise ServiceError(
                    409, "POLICY_DENIED", "policy denies this change now",
                    details={"reason": policy.get("reason"),
                             "blocking_issues": policy.get("blocking_issues")})
            if decision == "abstain":
                raise ServiceError(
                    409, "POLICY_ABSTAIN",
                    "policy cannot decide right now; retry later",
                    retryable=True,
                    details={"decision": "abstain",
                             "reason": policy.get("reason")})
            if decision == "require_human":
                # S07-26：S40 的 ReviewTask 接口不存在；不放行、不伪造审批。
                raise ServiceError(
                    409, "POLICY_ABSTAIN",
                    "policy requires a human decision; S07 does not self-approve",
                    retryable=True, details={"decision": "require_human"})
            if decision not in ("allow", "permit"):
                raise ServiceError(503, "POLICY_UNAVAILABLE",
                                   f"unusable decision {decision!r}",
                                   retryable=True)
            return {"decision": decision,
                    "policy_version": policy.get("policy_version")}, False

        if name == "VERIFY_PREPARED":
            refs = plan_body.get("prepared_revision_refs") or []
            for ref in refs:
                present = self._upstream(
                    lambda r=ref: self.revision_reader(
                        tenant_id, r["service_id"], r["id"], r["revision"]),
                    "DEPENDENCY_UNAVAILABLE")
                if present is None:
                    # 准备产物被 GC（§4.2 步 3）：不能再 commit 一份指向空
                    # revision 的 manifest。
                    raise ServiceError(
                        409, "PREPARED_REVISION_GONE",
                        "a prepared revision was garbage-collected",
                        details={"ref": ref})
            return {"checked": len(refs)}, not refs

        if name == "CHECK_PATCH":
            for patch in patches:
                current = self._upstream(
                    lambda p=patch: self.revision_reader(
                        tenant_id, p["target_service_id"], p["target_id"],
                        p["target_revision"]),
                    "DEPENDENCY_UNAVAILABLE")
                if current is None:
                    raise ServiceError(
                        500, "PATCH_PRECONDITION_FAILED",
                        "the patch base revision is gone",
                        details={"patch_seq": patch["patch_seq"]})
                document = (current.get("fields")
                            if isinstance(current.get("fields"), dict)
                            else current)
                value = _json_pointer(document, patch["path"])
                before = _loads(patch["before_value"], None)
                if value != before:
                    raise ServiceError(
                        500, "PATCH_PRECONDITION_FAILED",
                        f"patch {patch['patch_seq']} expects {before!r} but"
                        f" found {value!r}",
                        details={"patch_seq": patch["patch_seq"],
                                 "path": patch["path"]})
            return {"checked": len(patches)}, not patches

        if name == "PREPARE_CONTENT":
            impact = plan_body.get("impact") or {}
            nodes = impact.get("regenerate_node_ids") or []
            if not nodes:
                return {"prepared": []}, True
            prepared = self._upstream(
                lambda: self.content_preparer(tenant_id, {
                    "plan_id": plan["plan_id"],
                    "episode_id": plan["episode_id"],
                    "node_ids": list(nodes),
                    "prepared_revision_refs":
                        plan_body.get("prepared_revision_refs") or [],
                }), "DEPENDENCY_UNAVAILABLE") or {}
            return {"prepared": prepared.get("prepared_refs") or []}, False

        if name == "COMMIT_MEMORY":
            if not plan["memory_migration_required"]:
                # §4.5：不涉及剧情事实的修改没有记忆迁移，如实记 SKIPPED，
                # 而不是伪造一个 checkpoint。
                return {"reason": "memory_migration_not_required"}, True
            result = self._upstream(
                lambda: self.memory_committer(tenant_id, plan["series_id"], {
                    "branch_id": plan["branch_id"],
                    "plan_id": plan["plan_id"],
                    "instruction": plan["instruction"],
                    "base_checkpoint": _loads(plan["memory_checkpoint"], {}),
                }), "DEPENDENCY_UNAVAILABLE") or {}
            if result.get("commit_sequence") is None:
                raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                                   "S16 returned no checkpoint")
            self.conn.execute(
                "UPDATE change_plan SET memory_stale=0 WHERE tenant_id=? AND"
                " plan_id=?", (tenant_id, plan["plan_id"]))
            return result, False

        if name == "COMMIT_MANIFEST":
            result = self._upstream(
                lambda: self.manifest_committer(tenant_id, {
                    "manifest_id": context["manifest_ref"]["id"],
                    "expected_revision": int(plan["base_revision"]),
                    "change_plan_ref": {"service_id": "S07",
                                        "id": plan["plan_id"],
                                        "revision": 1,
                                        "content_hash": plan["plan_hash"]},
                    "prepared_revision_refs":
                        plan_body.get("prepared_revision_refs") or [],
                    # 意图，不是事实：S08 自己决定推进哪些 node_epoch（§4.4）。
                    "regenerate_node_ids":
                        (plan_body.get("impact") or {}).get(
                            "regenerate_node_ids") or [],
                    "patches": [_public_patch(p) for p in patches],
                }), "DEPENDENCY_UNAVAILABLE") or {}
            manifest = result.get("manifest") or {}
            if not manifest.get("id") or manifest.get("revision") is None:
                raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                                   "S08 returned no manifest")
            committed.update(result)
            return result, False

        if name == "EMIT_APPLIED":
            manifest = committed.get("manifest") or {}
            before_checkpoint = _loads(plan["memory_checkpoint"], {})
            memory_step = self._step(tenant_id, saga_id, "COMMIT_MEMORY")
            memory_response = _loads(memory_step["response"], {}) or {}
            after_checkpoint = before_checkpoint
            if memory_response.get("commit_sequence") is not None:
                after_checkpoint = {
                    "commit_sequence": memory_response["commit_sequence"],
                    "content_hash": memory_response.get("checkpoint_hash")
                    or memory_response.get("content_hash") or "0" * 64}
            impact = plan_body.get("impact") or {}
            data = {
                "schema_version": SCHEMA_VERSION,
                "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"],
                "base_manifest_ref": _loads(plan["base_manifest_ref"], {}),
                "new_manifest_ref": {
                    "id": manifest.get("id"),
                    "revision": int(manifest.get("revision") or 0),
                    "content_hash": manifest.get("content_hash"),
                },
                "execution_fence": int(plan["execution_fence"]),
                "changed_node_epochs": list(
                    committed.get("changed_node_epochs") or []),
                "regenerate_node_ids": list(
                    impact.get("regenerate_node_ids") or []),
                "recheck_node_ids": list(
                    impact.get("recheck_node_ids") or []),
                "reuse_artifact_refs": list(
                    impact.get("reuse_artifact_refs") or []),
                "affected_episode_ids": list(
                    impact.get("affected_episode_ids") or []),
                "memory_checkpoint_before": {
                    "commit_sequence": int(before_checkpoint.get(
                        "commit_sequence") or 0),
                    "content_hash": before_checkpoint.get("content_hash")
                    or "0" * 64},
                "memory_checkpoint_after": {
                    "commit_sequence": int(after_checkpoint.get(
                        "commit_sequence") or 0),
                    "content_hash": after_checkpoint.get("content_hash")
                    or "0" * 64},
                "estimated_cost_upper_micro": int(
                    plan["estimated_cost_upper_micro"]),
                "currency": plan["currency"],
                "saga_id": saga_id,
                "applied_at": self._now(),
            }
            envelope = new_event(
                source=SOURCE, type=EVENT_APPLIED,
                subject=f"change-plan/{plan['plan_id']}",
                tenant_id=tenant_id,
                home_cell=context.get("home_cell") or "unknown",
                aggregate_id=plan["plan_id"], sequence=1,
                correlation_id=plan["plan_id"],
                causation_id=committed.get("outbox_event_id"),
                data=data)
            event_id = self.outbox.append(envelope)
            self.conn.execute(
                "UPDATE change_plan SET state='APPLIED', applied_at=?"
                " WHERE tenant_id=? AND plan_id=? AND state='APPLYING'",
                (data["applied_at"], tenant_id, plan["plan_id"]))
            return {"event_id": event_id}, False

        raise ServiceError(500, "SAGA_STEP_UNKNOWN", name)


    # ------------------------------------------------------------ 恢复

    def recover_apply(self, tenant_id: str, plan_id: str) -> dict:
        """E13：重启后不知道 commit 成没成，**只能去 S08 查**（§4.3）。

        本地记"我发过 commit 了"在崩溃时可能丢，也可能写了但没执行；它与
        S08 的事实无关。所以这里读 S08 的 revision 列表，再逐条读 payload，
        找 `change_plan_ref.id == plan_id` 的那一条（S07-13：列表不带 payload）。
        """
        plan = self._plan_row(tenant_id, plan_id)
        if plan["state"] != "APPLYING":
            return {"plan_id": plan_id, "state": plan["state"],
                    "action": "none"}
        manifest_id = (_loads(plan["base_manifest_ref"], {}) or {}).get("id")
        if not manifest_id:
            return {"plan_id": plan_id, "state": plan["state"],
                    "action": "none"}

        found = None
        listing = self._upstream(
            lambda: self.revision_lister(tenant_id, manifest_id, 10),
            "DEPENDENCY_UNAVAILABLE") or {}
        for item in listing.get("items") or []:
            revision = item.get("revision")
            if revision is None or int(revision) <= int(plan["base_revision"]):
                continue
            detail = self._upstream(
                lambda r=revision: self.manifest_revision_reader(
                    tenant_id, manifest_id, int(r)),
                "DEPENDENCY_UNAVAILABLE") or {}
            payload = detail.get("manifest") or detail.get("payload") or {}
            ref = payload.get("change_plan_ref") if isinstance(payload, dict) \
                else None
            if isinstance(ref, dict) and ref.get("id") == plan_id:
                found = {"revision": int(revision),
                         "content_hash": detail.get("content_hash")}
                break

        if found is None:
            # 没找到 = 没提交成功。用**同一个** commit command_id 重试。
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE change_saga SET state='ABANDONED', finished_at=?,"
                    " failure_code='COMMIT_NOT_FOUND' WHERE tenant_id=? AND"
                    " saga_id=? AND state='RUNNING'",
                    (self._now(), tenant_id, plan["saga_id"]))
                self.conn.execute(
                    "UPDATE change_plan SET state='PLANNED', saga_id=NULL"
                    " WHERE tenant_id=? AND plan_id=? AND state='APPLYING'",
                    (tenant_id, plan_id))
            return {"plan_id": plan_id, "state": "PLANNED",
                    "action": "retry_commit",
                    "commit_command_id": saga_command_id(
                        plan_id, "COMMIT_MANIFEST")}

        # 找到了 = commit 已成功、只是本进程没写完那一步。补写并置 APPLIED。
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE change_saga_step SET state='DONE', finished_at=?"
                " WHERE tenant_id=? AND saga_id=? AND"
                " step_name='COMMIT_MANIFEST' AND state<>'DONE'",
                (self._now(), tenant_id, plan["saga_id"]))
            self.conn.execute(
                "UPDATE change_saga SET state='SUCCEEDED', finished_at=?"
                " WHERE tenant_id=? AND saga_id=? AND state='RUNNING'",
                (self._now(), tenant_id, plan["saga_id"]))
            self.conn.execute(
                "UPDATE change_plan SET state='APPLIED', applied_at=? WHERE"
                " tenant_id=? AND plan_id=? AND state='APPLYING'",
                (self._now(), tenant_id, plan_id))
        return {"plan_id": plan_id, "state": "APPLIED",
                "action": "recovered",
                "manifest_revision": found["revision"]}

    # ------------------------------------------------------------ 事件消费

    def consume(self, envelope: dict) -> dict:
        """§5.6 的三条事件。只做标记，不主动改任何计划的内容。"""
        event_type = envelope.get("type")
        consumer = CONSUMERS.get(event_type)
        if consumer is None:
            # 不认识的事件不是错误，不回 4xx 让上游重试。
            return {"consumed": False, "reason": "unrouted"}
        tenant_id = envelope.get("tenant_id")
        event_id = envelope.get("id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ServiceError(422, "INVALID_CONTEXT", "event lacks tenant_id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(422, "INVALID_CONTEXT", "event lacks id")
        with Transaction(self.conn):
            if not self.inbox.consume(consumer, event_id):
                return {"consumed": False, "reason": "duplicate"}
            handler = getattr(self, CONSUMERS[event_type])
            return {"consumed": True, **handler(tenant_id, envelope)}

    def _on_manifest_committed(self, tenant_id: str, envelope: dict) -> dict:
        data = envelope.get("data") or {}
        manifest_id = (data.get("manifest_ref") or {}).get("id")
        revision = (data.get("manifest_ref") or {}).get("revision")
        if manifest_id is None or revision is None:
            return {"superseded": 0}
        cursor = self.conn.execute(
            "UPDATE change_plan SET state='SUPERSEDED' WHERE tenant_id=?"
            " AND branch_id=? AND series_id=? AND base_manifest_ref LIKE ?"
            " AND base_revision < ? AND state='PLANNED'",
            (tenant_id, data.get("branch_id") or "main",
             data.get("series_id") or "",
             '%"id":"' + str(manifest_id) + '"%'.replace(
                 '"', '"'), int(revision)))
        return {"superseded": cursor.rowcount}

    def _on_policy_published(self, tenant_id: str, envelope: dict) -> dict:
        # 不主动重建任何 plan：重建意味着"用新策略重算一份用户没看过的计划"。
        return {"plans_touched": 0}

    def _on_memory_committed(self, tenant_id: str, envelope: dict) -> dict:
        data = envelope.get("data") or {}
        series_id = data.get("series_id")
        if not series_id:
            return {"marked_stale": 0}
        cursor = self.conn.execute(
            "UPDATE change_plan SET memory_stale=1 WHERE tenant_id=?"
            " AND series_id=? AND state='PLANNED'", (tenant_id, series_id))
        return {"marked_stale": cursor.rowcount}

    # ------------------------------------------------------------ 过期 / 清理

    # 能过期的是"还没开始执行"的非终态。`APPLYING` 故意不算：Saga 在飞时
    # 过期没有意义（外部事实可能已经动了一部分），它归 recover_apply 与
    # janitor 管（S07-31）。
    EXPIRABLE_STATES = ("DRAFT", "PLANNING", "PLANNED",
                        "CLARIFICATION_REQUIRED")

    def sweep_expired(self, *, tenant_id: str | None = None,
                      now: str | None = None) -> dict:
        """§4.7：置 EXPIRED，并把该计划的未答复澄清标 ABANDONED。

        §3.1 的图里写「任意非终态 → EXPIRED」，T07-17 又要求澄清超时也过期，
        所以这里包含 `CLARIFICATION_REQUIRED`——但**不**包含 `APPLYING`
        （S07-31）。过期只改状态、不动任何外部事实，所以没有要补偿的副作用。
        **绝不自动选默认选项**（§3.3）。
        """
        moment = now or self._now()
        placeholders = ",".join("?" * len(self.EXPIRABLE_STATES))
        clauses = [f"state IN ({placeholders})", "expires_at <= ?"]
        params: list = list(self.EXPIRABLE_STATES) + [moment]
        if tenant_id:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        rows = self.conn.execute(
            "SELECT tenant_id, plan_id, state FROM change_plan WHERE "
            + " AND ".join(clauses), tuple(params)).fetchall()
        expired = 0
        abandoned = 0
        with Transaction(self.conn):
            for row in rows:
                cursor = self.conn.execute(
                    "UPDATE change_plan SET state='EXPIRED' WHERE tenant_id=?"
                    " AND plan_id=? AND state=?",
                    (row["tenant_id"], row["plan_id"], row["state"]))
                if cursor.rowcount:
                    before = self.conn.execute(
                        "SELECT COUNT(*) FROM clarification_request WHERE"
                        " tenant_id=? AND plan_id=? AND answered_at IS NULL"
                        " AND abandoned_at IS NULL",
                        (row["tenant_id"], row["plan_id"])).fetchone()[0]
                    self._abandon_clarifications(row["tenant_id"],
                                                 row["plan_id"])
                    abandoned += before
                    expired += 1
        return {"expired": expired, "clarifications_abandoned": abandoned}

    def sweep_stale_commands(self, older_than_seconds: int = 60,
                             now: str | None = None) -> dict:
        """T07-32：`IN_PROGRESS` 僵死记录的清理。

        只删 `idempotency_record` 里 `result IS NULL` 且够老的行。**不碰**
        `change_plan`：一条僵死的命令可能已经落了半份计划，那半份由
        `recover_apply` 或过期扫描处理，不靠删幂等行来"修"。
        """
        moment = now or self._now()
        cutoff = _parse_any_ts(moment) - timedelta(seconds=older_than_seconds)
        # 不能在 SQL 里 `created_at < ?`：两套时间戳格式（S07 的 `...Z` 与
        # SDK 的 `+00:00`）按字节比较得到的是字典序，不是时间先后。先解析再比。
        stale = [row["command_id"] for row in self.conn.execute(
            "SELECT command_id, created_at FROM idempotency_record WHERE"
            " result IS NULL").fetchall()
            if _parse_any_ts(row["created_at"]) < cutoff]
        for command_id in stale:
            self.conn.execute(
                "DELETE FROM idempotency_record WHERE command_id=? AND"
                " result IS NULL", (command_id,))
        return {"deleted": len(stale)}


CONSUMERS = {
    "snapshot.manifest.committed": "_on_manifest_committed",
    "policy.revision.published": "_on_policy_published",
    "memory.canon.committed": "_on_memory_committed",
}


# ------------------------------------------------------------ 模块级工具

def _unconfigured(code: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(
            503, "DEPENDENCY_UNAVAILABLE",
            f"{code} is not configured; S07 will not invent a default")
    return _fail


def _seconds(value) -> str:
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _one_per_episode(shots: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for shot in shots:
        if shot["episode_id"] in seen:
            continue
        seen.add(shot["episode_id"])
        out.append(shot)
    return out


def _episode_option(shot: dict, index: int) -> dict:
    return {
        "option_id": f"opt_episode_{index + 1}",
        "label": f"在 {shot['episode_id']} 里改",
        "consequence": (
            f"只影响 {shot['episode_id']}；镜头 {shot['shot_id']}"
            f"（revision {shot['revision']}）"
            + (f"：{shot['summary']}" if shot.get("summary") else "")),
        "cost_delta_micro": 0,
        "apply": {"episode_id": shot["episode_id"]},
    }


def _shot_option(shot: dict, index: int) -> dict:
    return {
        "option_id": f"opt_shot_{index + 1}",
        "label": f"改 {shot['shot_id']}",
        "consequence": (
            f"revision {shot['revision']}，内容哈希 {shot['content_hash'][:12]}"
            + (f"：{shot['summary']}" if shot.get("summary") else "")),
        "cost_delta_micro": 0,
        "apply": {"shot_id": shot["shot_id"]},
    }


def _dialogue_options(target_seconds, threshold_ms: int,
                      patches: list[dict], shot: dict) -> list[dict]:
    """§4.1 步骤 3。三个选项，每一支带可量化后果。

    这里**没有**"删掉超出的台词"，也没有"截断音频"——即使技术上能做，
    那也会改变剧情内容，需要剧本层的决定（docs/06 §1）。
    """
    del shot
    extend = threshold_ms / 1000.0
    return [
        {"option_id": "opt_extend_to_min",
         "label": f"延到 {_seconds(extend)} 秒",
         "consequence": (
             f"时长 {_seconds(target_seconds)} → {_seconds(extend)} 秒；"
             "对白原样保留；相关镜头按新时长重做"),
         "cost_delta_micro": 180000,
         "apply": {"duration_seconds": extend}},
        {"option_id": "opt_rewrite_dialogue",
         "label": f"保留 {_seconds(target_seconds)} 秒并重写对白",
         "consequence": (
             "需要 S04 参与改写台词（涉及剧本层），额外成本约 620000；"
             "S07 不生成内容，改写后需重新生成本计划"),
         "cost_delta_micro": 620000,
         "apply": {"rewrite_dialogue": True}},
        {"option_id": "opt_cancel",
         "label": "取消这次修改",
         "consequence": "不改变任何内容；计划保持未应用并自然过期",
         "cost_delta_micro": 0,
         "apply": {"cancel": True}},
    ]


def _public_option(option: dict) -> dict:
    """澄清选项的对外形状。

    `apply` 是 S07 内部用来重走解析的指令，**不能**出现在响应里：它不是
    用户看到的选项，而且会把服务端的处理方式变成契约的一部分。
    """
    return {"option_id": option.get("option_id"),
            "label": option.get("label"),
            "consequence": option.get("consequence"),
            "cost_delta_micro": option.get("cost_delta_micro")}


def _public_patch(patch: dict) -> dict:
    # 两种来源：刚算出来的内存 patch（`before` / `after`，已是 Python 值）与
    # 从 `change_plan_patch` 读回的 row（`before_value` / `after_value`，JSON
    # 文本）。按**来源**判断，不按 `isinstance(str)`：`"eye_level"` 是合法
    # 的字段值，却不能当 JSON 解析（否则 `_loads` 静默返回 None）。
    if "before_value" in patch:
        before = _loads(patch["before_value"], None)
        after = _loads(patch["after_value"], None)
    else:
        before = patch["before"]
        after = patch["after"]
    return {"target_ref": {"service_id": patch["target_service_id"],
                           "id": patch["target_id"],
                           "revision": patch["target_revision"],
                           "content_hash": patch["target_hash"]},
            "path": patch["path"],
            "before": before,
            "after": after}


def _json_pointer(document, pointer: str):
    if pointer in ("", "/"):
        return document
    # `shot["fields"]` 与 S06 的 `prepared.fields` 是**指针键名**的扁平表
    # （`{"/camera/angle": "eye_level"}`），而 S06 的 revision 正文是嵌套
    # 文档（`{"camera": {"angle": ...}}`）。两种都要能读，否则 CHECK_PATCH
    # 会把"当前值 eye_level"读成 None。
    if isinstance(document, dict) and pointer in document:
        return document[pointer]
    node = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        else:
            return None
    return node


def _impact_of(analysis: dict) -> dict:
    """只保留 `impact` 的六个键（S07-12：S09 多塞了 `derived_adjustments`）。"""
    impact = analysis.get("impact") or {}
    return {
        "regenerate_node_ids": list(impact.get("regenerate_node_ids") or []),
        "recheck_node_ids": list(impact.get("recheck_node_ids") or []),
        "reuse_artifact_refs": list(impact.get("reuse_artifact_refs") or []),
        "affected_episode_ids": list(
            impact.get("affected_episode_ids") or []),
        "superseded_claim_refs": list(
            impact.get("superseded_claim_refs") or []),
        "lineage_watermark": int(impact.get("lineage_watermark") or 0),
    }


def _describe_derived(derived: list[dict]) -> list[str]:
    """把 S06 的派生补丁写成用户能读的一句话（T07-04）。

    "派生是 S06 算的" 这件事体现在 `derivation_rule` 的名字与 `derived_from`
    的指向上；S07 不解释规则怎么算，也不自己补一条。
    """
    out: list[str] = []
    for item in derived:
        rule = item.get("derivation_rule")
        out.append(
            f"{item['path']} 由 {item['before']!r} 变为 {item['after']!r}"
            f"（触发于补丁 #{item['derived_from']}"
            + (f"，规则 {rule}" if rule else "") + "）")
    return out


def _derived_adjustments(analysis: dict) -> list[str]:
    impact = analysis.get("impact") or {}
    raw = (impact.get("derived_adjustments")
           or analysis.get("derived_adjustments") or [])
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("description") or item.get("summary")
            if text:
                out.append(str(text))
    return out


def _stale_episodes(preview: dict | None) -> list[str]:
    if not isinstance(preview, dict):
        return []
    return list(preview.get("delivered_episode_ids") or [])


def _impact_summary(analysis: dict, impact: dict,
                    preview: dict | None,
                    disposition: str = "MARK_STALE") -> dict:
    nodes = analysis.get("nodes") or []
    by_episode: dict = {}
    for node in nodes:
        episode = node.get("episode_id") or "unknown"
        bucket = by_episode.setdefault(episode, {
            "episode_id": episode, "regenerate": 0, "recheck": 0,
            "reuse": 0, "delivered": bool(node.get("delivered"))})
        decision = node.get("decision") or node.get("action")
        if decision in ("regenerate", "REGENERATE"):
            bucket["regenerate"] += 1
        elif decision in ("recheck", "RECHECK"):
            bucket["recheck"] += 1
        elif decision in ("reuse", "REUSE"):
            bucket["reuse"] += 1
        bucket["delivered"] = bucket["delivered"] or bool(
            node.get("delivered"))

    if not by_episode:
        for episode in impact.get("affected_episode_ids") or []:
            by_episode[episode] = {
                "episode_id": episode, "regenerate": 0, "recheck": 0,
                "reuse": 0, "delivered": False}

    # T07-22：S16 说"这些已交付集会被标 stale"，它们就**受影响**，即使
    # S09 的节点明细里没有它们（重做集合为空时确实不会有）。漏掉这一步，
    # "已交付集要单独可查"就只剩 S09 恰好列出来的那一部分。
    for episode in _stale_episodes(preview):
        bucket = by_episode.setdefault(episode, {
            "episode_id": episode, "regenerate": 0, "recheck": 0,
            "reuse": 0, "delivered": True})
        bucket["delivered"] = True

    # T07-22：已交付集必须在 by_episode 里被标出来，**且**要能被单独查。
    delivered_ids = sorted(e["episode_id"] for e in by_episode.values()
                           if e["delivered"])
    for episode in delivered_ids:
        by_episode[episode]["disposition"] = disposition

    totals = {
        "regenerate": len(impact.get("regenerate_node_ids") or []),
        "recheck": len(impact.get("recheck_node_ids") or []),
        "reuse": len(impact.get("reuse_artifact_refs") or []),
        "delivered_episodes": len(delivered_ids),
    }
    cost_breakdown = []
    for stage in analysis.get("by_stage") or []:
        cost_breakdown.append({
            "kind": stage.get("stage") or stage.get("kind"),
            "node_count": int(stage.get("count")
                              or stage.get("node_count") or 0),
            "cost_upper_micro": int(stage.get("cost_upper_micro") or 0)})
    warnings = []
    if delivered_ids:
        if disposition == "MIGRATE_AND_REDO":
            warnings.append(
                "以下集已交付，但本次选择 MIGRATE_AND_REDO：会重做并重渲染，"
                "成本显著上升：" + "、".join(delivered_ids))
        else:
            warnings.append(
                "以下集已交付，默认只标 memory_stale，不重做："
                + "、".join(delivered_ids))
    if preview and preview.get("memory_migration_required"):
        warnings.append(
            "这次修改会改变剧情事实；记忆 checkpoint 需要迁移，"
            "已交付集不自动重写（docs/05 §5）。")
    if isinstance(preview, dict) and preview.get("warning"):
        # S07-29：上游不认 dry_run 时把它自己说的话原样带给用户。
        warnings.append(str(preview["warning"]))
    return {"totals": totals, "by_episode": list(by_episode.values()),
            "by_stage": list(analysis.get("by_stage") or []),
            "cost_breakdown": cost_breakdown, "warnings": warnings}
