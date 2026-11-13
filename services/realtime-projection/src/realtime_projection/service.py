"""S47 内核。方案：`plans/S47-realtime-projection.md`。

一句话边界：**S47 只回答"截至某个水位，界面该显示什么"，不回答"现在是什么"。**

两条比别的服务更硬的纪律，代码里每一处都在替它们让路：

1. **不是第二权威。** 这张库里没有余额、没有授权态、没有 accepted pointer、
   没有放行依据（§1.2、§5.6）。`spent_micros` 能存是因为它是**已结算的历史
   累计**，不会倒退；`remaining_micros` 永远不存，只给 `remaining_ref`。
   `manifest_revision` 只用于展示，读它的方法叫
   `read_manifest_revision_for_display`——命名即约束。
2. **事件处理必须幂等且容忍乱序。** 三处去重：inbox 的
   `(consumer, event_id)`、投影行的 `change_seq` CAS、通知的
   `(tenant, principal, event_id, channel)`。缺号不装看不见：A 类 topic 一旦
   序号跳号，就把该行标成 `GAPPED` 并写一条 `PROJECTION_GAP` 警告，**仍然
   应用**该事件（§3.3：丢弃会让投影停在更旧的位置，更糟）。

两个水位，别混：

- `applied_watermark` —— **只**放 S21 的 `projection_watermark`，只用于缺号
  检测与乱序丢弃。B 类事件（ledger/artifact/delivery…）没有这个序，不动它。
- `change_seq` —— 每 run 单调的**展示序号**，SSE 的 `id` 与 `change_log` 的
  主键都用它。§4.3 说"`id` 就是 `projection_watermark`"，这在 A 类事件密
  集时字面成立（两者取同一个值），而在 B 类事件上 `projection_watermark`
  根本没有对应值——所以需要这个序号（C47-23）。

`ABSENT` 不是一个数据库状态，是"查不到行"（PCA-3）。`TERMINAL` 之后只记
诊断、不改展示（§3.2）。

**这里的 CAS 不叫 `execution_fence`**，也不与它有任何关系：那个是 S08 的取消与
故障切换机制。这里的比较只是缓存一致性检查（§3.6）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from . import redaction
from .schema import (
    CLASS_A_EVENTS,
    CLOCK_SKEW_TOLERANCE_MS,
    CONNECTION_LIMITS,
    CONSUMER_NOTIFY,
    CONSUMER_PROJECTION,
    CURSOR_RETENTION_SECONDS,
    DEFAULT_NOTIFY_TYPES,
    ENABLED_CHANNELS,
    EVENT_CLASSES,
    ID_PREFIX,
    KNOWN_CHANNELS,
    NON_PROJECTING_EVENTS,
    RESYNC_MAX_PER_MINUTE,
    SCHEMA,
    URGENT_NOTIFY_TYPES,
)

SOURCE = "S47"
SCHEMA_VERSION = "2.0"
EVENT_CURSOR_ADVANCED = "projection.cursor.advanced"
TOPIC_CURSOR = "drama.realtime-projection.v2"

# §3.2 的终态集合（与 S21 的 TERMINAL_STATES 同义，但不 import 它——跨服务
# import 业务常量就是耦合）。这里只用于"不再更新展示"的判断。
TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "INCOMPLETE", "FAILED", "CANCELED"})

# §3.4 的合并窗口（毫秒）。run 级与错误/终态立即推，所以不在表里。
# 「计数类 500ms」没有独立的 event_class（计数挂在 run.state 上），见 C47-24。
MERGE_WINDOWS_MS = {"node.state": 200}

# 事件类型 → SSE 的粗粒度类别（§4.3）。**不泄漏 Kafka 的 type**。
EVENT_CLASS_OF = {
    "orchestration.run.state-changed": "run.state",
    "generation.job.state-changed": "node.state",
    "artifact.ingested": "artifact",
    "review.decision.recorded": "review",
    "delivery.package.created": "delivery",
    "delivery.package.sealed": "delivery",
    "delivery.package.published": "delivery",
    "delivery.publish.failed": "warning",
    "finance.ledger.entry-posted": "run.state",
    "model.route.selected": "node.state",
    "connector.observation.received": "warning",
    "integration.command.accepted": "node.state",
}

# 通知白名单（§3.9）。**事件 → 人类需要知道的事**，映射是显式白名单。
NOTIFY_OF_RUN_STATE = {
    "FAILED": "run.failed",
    "SUCCEEDED": "run.completed",
    "WAITING_REVIEW": "review.requested",
}

# §2.1 写了一个 `orchestration.node.state-changed`，注册表里没有这个类型
# （C47-12）。这里**故意不进 EVENT_CLASS_OF**：真收到时按"未知类型"忽略并
# 计数。若给它开通道，节点级 sequence 对 run 不是稠密的，每一条都会误报
# GAPPED——比不显示更糟。


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _norm_time(text: str | None) -> str | None:
    """把上游时间统一成毫秒精度的 UTC 字符串。

    上游（如 `event_sdk.new_event`）给的是微秒 `...446476Z`，我们自己的
    `_iso` 给的是毫秒。两种混在同一列里会让字符串比较与 `max()` 在边界上
    出错，也会让快照里的 `state_since` 与 `as_of` 看起来不是同一口径。
    """
    moment = _parse_iso(text)
    return None if moment is None else _iso(moment)


def _now() -> str:
    return _iso(_now_dt())


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。一个命令一个事务，不嵌套。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class RealtimeProjectionService:
    """消费事件 → 折叠成 run 看板投影 → 供快照 / SSE / 通知读取。

    注入的上游全是**只读** callback（§1.4）：S01 判权限，S21 供回源。
    没有写回的口子，连一个 POST 包装函数都不提供（§8.3）。
    """

    def __init__(self, conn: sqlite3.Connection, *, authorization_source=None,
                 run_source=None, redaction_secret: bytes | None = None,
                 clock=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Outbox / Inbox 的构造各自 executescript（隐式提交），必须在 SCHEMA
        # 之后、任何事务之外。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)

        self.authorization_source = authorization_source
        self.run_source = run_source or _resync_unavailable
        self.redaction_secret = redaction_secret
        self._clock = clock or _now_dt

        # §3.8 的回源配额：每 run 每分钟 ≤6 次。没有为它建表——它是**限流器**，
        # 不是事实；重启后从零开始只会让前 60 秒更宽松，不会让任何数据出错。
        self._resync_calls: dict[tuple[str, str], deque] = defaultdict(deque)
        # §4.4 的游标事件节流：每订阅每秒 ≤1。同上，进程内桶。
        self._cursor_emitted_at: dict[str, datetime] = {}

    def _stamp(self) -> str:
        """本服务自己的"现在"。**必须**走注入的时钟，否则可注入的时钟只影响
        TTL/配额，而 `as_of` / `created_at` 仍是真实时间——测试与运维都会看到
        两套时间在同一个响应里打架（T47-12 就是靠它钉住的）。"""
        return _iso(self._clock())

    # ================================================================ 事件入口

    def consume(self, envelope: dict, *, side_effects: bool = True) -> dict:
        """接一条事件。inbox 去重 → 按 topic 选策略 → CAS 应用 → 记 change_log。

        整个动作在**一个事务**里：inbox 行、投影行、change_log、outbox 同生
        共死。应用后 kill 会重投，inbox 挡住；应用前 kill 则什么都没发生（§7.5）。
        """
        if not isinstance(envelope, dict):
            raise ServiceError(422, "MALFORMED_REQUEST",
                               "event must be an object")
        tenant_id = envelope.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ServiceError(422, "MALFORMED_REQUEST", "event lacks tenant_id")
        event_id = envelope.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(422, "MALFORMED_REQUEST", "event lacks id")
        event_type = envelope.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ServiceError(422, "MALFORMED_REQUEST", "event lacks type")

        route = EVENT_CLASS_OF.get(event_type)
        if route is None:
            # §7.1 / T47-07：未知事件类型忽略并计数，不报错。上游加类型时
            # S47 不该挂。
            return {"consumed": False, "reason": "unrouted",
                    "event_type": event_type}

        data = envelope.get("data")
        if not isinstance(data, dict):
            # §7.1 / T47-08：schema 不合法 → 不应用、不崩溃。inbox 仍记下它，
            # 避免同一条坏事件被无限重投（DLQ 的职责在平台侧）。
            return {"consumed": False, "reason": "invalid",
                    "event_type": event_type}

        with Transaction(self.conn):
            if not self.inbox.consume(CONSUMER_PROJECTION, event_id):
                return {"consumed": False, "reason": "duplicate"}

            try:
                result = self._apply(tenant_id, envelope, event_type, route,
                                     data)
            except ServiceError as exc:
                if exc.status != 422:
                    raise
                # §9.1 T47-08：事件 schema 非法 → 不应用、**也不重试**。
                # inbox 行必须一起提交，否则同一条坏事件会被无限重投（commit
                # 前抛异常会连 inbox 一起回滚）。DLQ 本身在平台侧。
                result = {"applied": False, "reason": "invalid",
                          "code": exc.code, "detail": str(exc)}
            if side_effects:
                result["notifications"] = self._notify(
                    tenant_id, envelope, event_type, data)
            return {"consumed": True, **result}

    def _apply(self, tenant_id: str, envelope: dict, event_type: str,
               event_class: str, data: dict) -> dict:
        if event_type in CLASS_A_EVENTS:
            return self._apply_class_a(tenant_id, envelope, event_class, data)
        if event_type in NON_PROJECTING_EVENTS:
            # 这些事件没有 run 坐标，或只用于展示路由/水位：不改 run 行，但
            # 照旧算"消费成功"——通知路径已经看过它们。
            return {"applied": False, "reason": "not_projected"}
        return self._apply_class_b(tenant_id, envelope, event_class, data)

    # ---------------------------------------------------------------- A 类

    def _apply_class_a(self, tenant_id: str, envelope: dict, event_class: str,
                       data: dict) -> dict:
        """A 类：单调无洞的 sequence，做缺号检测与乱序丢弃（§3.3）。"""
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ServiceError(422, "MALFORMED_REQUEST",
                               "run state event lacks data.run_id")
        sequence = envelope.get("sequence")
        if not isinstance(sequence, int) or sequence < 1:
            raise ServiceError(422, "MALFORMED_REQUEST",
                               "run state event needs aggregate_sequence >= 1")

        row = self._row(tenant_id, run_id)
        if row is None or row["projection_state"] == "EVICTED":
            # §7.1 最后一行 / T47-09：行不存在且不是"首现" → 创建 + GAPPED。
            # 被清理过的行等同于没有。
            self._create_row(tenant_id, envelope, data,
                             gapped=sequence > 1, expected=1, got=sequence,
                             replace_evicted=row is not None)
            row = self._row(tenant_id, run_id)
            if row is None:  # 并发建行输给了别人，让赢的那一方持有本事件
                return {"applied": False, "reason": "raced"}

        if row["projection_state"] == "TERMINAL":
            # §3.2 / T47-10：终态后只记诊断，不更新展示。
            self._note_terminal_late(tenant_id, run_id, envelope)
            return {"applied": False, "reason": "terminal"}

        applied = int(row["applied_watermark"])
        if sequence <= applied:
            # 乱序或重复：旧不覆盖新（§3.1 步骤 3、T47-02）。
            return {"applied": False, "reason": "stale", "watermark": applied}

        gapped = sequence > applied + 1
        fields, warnings, artifacts = self._merge_fields(
            row, data, event_type=envelope.get("type"),
            event_time=envelope.get("time"))

        if gapped:
            fields["projection_state"] = "GAPPED"
            fields["gap"] = _dumps({"expected": applied + 1, "got": sequence,
                                    "detected_at": self._stamp()})
            # §3.3：仍应用该事件；UI 至少显示"最新已知的，但中间有洞"。
            warnings = self._union_by(warnings, [{
                "code": "PROJECTION_GAP", "expected": applied + 1,
                "got": sequence, "at": self._stamp()}], key="code")

        if data.get("state") in TERMINAL_RUN_STATES:
            fields["projection_state"] = "TERMINAL"

        sequence_for_display = max(sequence, int(row["change_seq"]) + 1)
        fields["applied_watermark"] = sequence
        fields["warnings"] = _dumps(warnings)
        fields["artifacts"] = _dumps(artifacts)
        changed = self._cas_update(tenant_id, run_id, sequence_for_display,
                                   fields)
        if not changed:
            return {"applied": False, "reason": "stale", "watermark": applied}

        payload = {"watermark": sequence_for_display,
                   "type": envelope.get("type"), "at": envelope.get("time")}
        for key, value in (("run_state", data.get("state")),
                           ("completed_shots", fields.get("completed_shots")),
                           ("current_stage", data.get("current_stage"))):
            if value is not None:
                payload[key] = value
        if gapped:
            payload["warning"] = {"code": "PROJECTION_GAP",
                                  "expected": applied + 1, "got": sequence,
                                  "at": self._stamp()}
        self._write_change_log(tenant_id, run_id, sequence_for_display,
                              event_class, payload)
        return {"applied": True, "reason": "applied",
                "watermark": sequence_for_display,
                "source_watermark": sequence,
                "gapped": gapped, "event_class": event_class}

    # ---------------------------------------------------------------- B 类

    def _apply_class_b(self, tenant_id: str, envelope: dict, event_class: str,
                       data: dict) -> dict:
        """B 类：没有每 run 单调序，只用 time 排序，不做缺号检测（§3.3）。"""
        run_id = self._run_of(envelope.get("type"), data)
        if not run_id:
            # 事件里没有 run 坐标 → 它不属于任何 run 投影。**不猜**（§2.1）。
            return {"applied": False, "reason": "no_run_scope"}
        row = self._row(tenant_id, run_id)
        if row is None:
            if envelope.get("type") != "finance.ledger.entry-posted":
                # 只有金额事件能凭空建行（它带完整归因坐标）。
                return {"applied": False, "reason": "no_projection_row"}
            self._create_row(tenant_id, envelope, data, gapped=False,
                             expected=1, got=1, ledger=True)
            row = self._row(tenant_id, run_id)
            if row is None:
                return {"applied": False, "reason": "raced"}

        if row["projection_state"] == "TERMINAL":
            # 终态行的展示不再变，但金额与制品仍要记（它们说明"花了多少"），
            # 所以只跳过 run_state 类字段。诊断照样记。
            pass

        event_time = _parse_iso(envelope.get("time"))
        last_time = _parse_iso(row["last_event_time"])
        if event_time and last_time and (
                event_time < last_time - timedelta(
                    milliseconds=CLOCK_SKEW_TOLERANCE_MS)):
            return {"applied": False, "reason": "stale", "detail": "clock_skew"}

        fields, warnings, artifacts = self._merge_fields(
            row, data, event_type=envelope.get("type"),
            event_time=envelope.get("time"))
        fields["warnings"] = _dumps(warnings)
        fields["artifacts"] = _dumps(artifacts)
        display = int(row["change_seq"]) + 1
        changed = self._cas_update(tenant_id, run_id, display, fields)
        if not changed:
            return {"applied": False, "reason": "stale",
                    "watermark": int(row["change_seq"])}

        payload = {"watermark": display, "type": envelope.get("type"),
                   "at": envelope.get("time")}
        self._write_change_log(tenant_id, run_id, display, event_class, payload)
        return {"applied": True, "reason": "applied", "watermark": display,
                "event_class": event_class}

    def _run_of(self, event_type: str | None, data: dict) -> str | None:
        """从事件里取 run 坐标。取不到就返回 None——不按 tenant 猜。"""
        if event_type == "finance.ledger.entry-posted":
            attribution = data.get("attribution") or {}
            run_id = attribution.get("run_id")
            return run_id if isinstance(run_id, str) and run_id else None
        value = data.get("run_id")
        return value if isinstance(value, str) and value else None

    # ---------------------------------------------------------------- 字段合并

    def _merge_fields(self, row: sqlite3.Row, data: dict, *, event_type: str | None,
                      event_time: str | None) -> tuple[dict, list, list]:
        """§3.5 的四条字段规则。**事件里没有的字段，投影里就没有。**

        返回 `(标量字段, warnings, artifacts)`；后两者是列表，调用方决定怎么
        落库——这样在 A 类里追加 `PROJECTION_GAP` 时不用去解析 JSON 字符串。
        """
        fields: dict = {}
        warnings = _loads(row["warnings"], [])
        artifacts = _loads(row["artifacts"], [])
        event_time = _norm_time(event_time) or event_time

        if event_type == "orchestration.run.state-changed":
            state = data.get("state")
            if isinstance(state, str) and state:
                if state != row["run_state"]:
                    fields["state_since"] = event_time or self._stamp()
                fields["run_state"] = state
            revision = data.get("manifest_revision")
            if isinstance(revision, int):
                fields["manifest_revision"] = revision
            # 契约里叫 completed_nodes，§4.2 的对外形状叫 completed_shots
            # （C47-16）。单调累加取 max，防回退（T47-05）。
            done = data.get("completed_nodes")
            if isinstance(done, int) and done >= 0:
                fields["completed_shots"] = max(int(row["completed_shots"]), done)
            # `total_shots` 与 `current_node` 在事件里没有来源——保持原值，
            # 不用 blocked_nodes 之类的近义字段凑（C47-16）。
            blocked = data.get("blocked_nodes")
            if isinstance(blocked, int) and blocked > 0:
                warnings = self._union_by(warnings, [{
                    "code": "NODE_BLOCKED", "count": blocked,
                    "at": event_time}], key="code")
            failed = data.get("failed_nodes")
            if isinstance(failed, int) and failed > 0:
                warnings = self._union_by(warnings, [{
                    "code": "NODE_FAILED", "count": failed,
                    "at": event_time}], key="code")

        elif event_type == "finance.ledger.entry-posted":
            # §10.5：只有"已结算"的累计能投影。RESERVE/RELEASE 不是花费。
            budget_after = data.get("budget_after") or {}
            consumed = budget_after.get("consumed_micro")
            if data.get("entry_type") in ("SETTLE", "ADJUST") \
                    and isinstance(consumed, int):
                fields["spent_micros"] = max(int(row["spent_micros"]), consumed)
                fields["spent_as_of"] = event_time or self._stamp()

        elif event_type == "artifact.ingested":
            artifact_ref = data.get("artifact_ref") or {}
            artifact_id = artifact_ref.get("id") or data.get("subject")
            if isinstance(artifact_id, str) and artifact_id:
                artifacts = self._union_by(artifacts, [{
                    "artifact_id": artifact_id,
                    "kind": (data.get("content_class") or "unknown"),
                    "url_ref": f"/artifacts/{artifact_id}/download",
                }], key="artifact_id")

        elif event_type == "generation.job.state-changed":
            # UI 进度条只看"这个节点到哪一步了"。事件里没有 run 级计数，
            # 所以不写 completed_shots（那会变成第二份计数）。
            node_id = data.get("node_id")
            job_state = data.get("state")
            if isinstance(node_id, str) and node_id and job_state:
                fields["current_node"] = _dumps({
                    "node_id": node_id,
                    "node_epoch": data.get("node_epoch"),
                    "state": job_state,
                })

        if event_time:
            # B 类的排序键就是它（§3.3 第二段）；A 类只是顺带记录。
            fields["last_event_time"] = self._max_time(
                row["last_event_time"], event_time)
        return fields, warnings, artifacts

    @staticmethod
    def _max_time(current: str | None, incoming: str | None) -> str | None:
        incoming = _norm_time(incoming) or incoming
        if not incoming:
            return current
        if not current:
            return incoming
        return incoming if incoming > current else current

    @staticmethod
    def _union_by(existing: list, incoming: list, *, key: str) -> list:
        """只增不减集合：按 id 去重（§3.5）。后到的覆盖同 id 的旧值。"""
        merged: dict[str, dict] = {}
        order: list[str] = []
        for item in list(existing) + list(incoming):
            if not isinstance(item, dict):
                continue
            ident = item.get(key)
            if ident is None:
                continue
            if ident not in merged:
                order.append(ident)
            merged[ident] = item
        return [merged[i] for i in order]

    # ---------------------------------------------------------------- CAS

    def _cas_update(self, tenant_id: str, run_id: str, display: int,
                    fields: dict) -> bool:
        """§3.6：条件在 `WHERE` 里，所以这是 CAS 而不是锁。

        0 行返回 False = 有更新的已经应用（分区重平衡时旧 consumer 还在跑，
        或多个 consumer 并发时输的那一方）。
        """
        assignments = ["change_seq = ?", "updated_at = ?"]
        params: list = [display, self._stamp()]
        for column, value in fields.items():
            assignments.append(f"{column} = ?")
            params.append(value)
        params.extend([tenant_id, run_id, display])
        cur = self.conn.execute(
            "UPDATE run_dashboard_projection SET "
            + ", ".join(assignments)
            + " WHERE tenant_id = ? AND run_id = ? AND change_seq < ?", params)
        return cur.rowcount == 1

    # ---------------------------------------------------------------- 建行

    def _create_row(self, tenant_id: str, envelope: dict, data: dict, *,
                    gapped: bool, expected: int, got: int,
                    replace_evicted: bool = False, ledger: bool = False) -> None:
        """建投影行。坐标缺失时**不伪造**（C47-15）：写空串 + 一条警告。"""
        attribution = data.get("attribution") or {}
        project_id = (data.get("project_id") or attribution.get("project_id")
                      or envelope.get("project_id") or "")
        episode_id = (data.get("episode_id") or attribution.get("episode_id")
                      or envelope.get("episode_id") or "")
        branch_id = (data.get("branch_id") or attribution.get("branch_id")
                     or envelope.get("branch_id") or "")
        run_id = data.get("run_id") or attribution.get("run_id")
        now = self._stamp()

        warnings = []
        if not (project_id and episode_id and branch_id):
            missing = [name for name, value in
                       (("project_id", project_id),
                        ("episode_id", episode_id),
                        ("branch_id", branch_id)) if not value]
            warnings.append({"code": "PROJECTION_COORDINATE_MISSING",
                             "missing": missing, "at": now})
        if gapped:
            warnings.append({"code": "PROJECTION_GAP", "expected": expected,
                             "got": got, "at": now})

        # **不在这里判终态**：首条事件就是终态时，若建行即 TERMINAL，调用方
        # 的下一句会把它当"终态后的迟到事件"丢掉，于是这一帧永远进不了
        # change_log。终态由正常的字段合并路径写进同一次 CAS。
        state = "GAPPED" if gapped else "LIVE"
        run_state = None if ledger else data.get("state")

        if replace_evicted:
            self.conn.execute(
                "DELETE FROM run_dashboard_projection WHERE tenant_id = ?"
                " AND run_id = ? AND projection_state = 'EVICTED'",
                (tenant_id, run_id))
        self.conn.execute(
            "INSERT OR IGNORE INTO run_dashboard_projection ("
            "tenant_id, run_id, project_id, episode_id, branch_id,"
            " projection_state, applied_watermark, change_seq, source_watermark,"
            " gap, last_event_time, run_state, state_since, manifest_revision,"
            " current_node, completed_shots, total_shots, spent_micros,"
            " spent_as_of, warnings, artifacts, created_at, updated_at,"
            " evicted_at) VALUES (" + ",".join("?" * 24) + ")",
            (tenant_id, run_id, project_id, episode_id, branch_id, state,
             0, 0, None,
             _dumps({"expected": expected, "got": got, "detected_at": now})
             if gapped else None,
             envelope.get("time"), run_state,
             envelope.get("time") if run_state else None,
             data.get("manifest_revision"),
             _dumps(data["current_node"]) if data.get("current_node") else None,
             int(data.get("completed_nodes") or 0), None, 0, None,
             _dumps(warnings), "[]", now, now, None))

    # ---------------------------------------------------------------- 变更日志

    def _write_change_log(self, tenant_id: str, run_id: str, display: int,
                          event_class: str, payload: dict) -> None:
        """写 change_log。**payload 已过 PROJECTION_SECRET_SCAN**（§5.2）。

        脱敏在这里而不是在推送时：change_log 是 SSE 回放的唯一来源，推送前
        再脱敏就等于同一份明文在库里躺了 15 分钟。
        """
        if event_class not in EVENT_CLASSES:
            event_class = "warning"
        clean, hits = redaction.redact_payload(
            payload, tenant_id, secret=self.redaction_secret)
        if hits:
            clean["redaction_hits"] = hits
        self.conn.execute(
            "INSERT INTO projection_change_log"
            " (tenant_id, run_id, watermark, event_class, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, run_id, watermark) DO UPDATE SET"
            " event_class = excluded.event_class,"
            " payload = excluded.payload, created_at = excluded.created_at",
            (tenant_id, run_id, display, event_class, _dumps(clean), self._stamp()))

    def _note_terminal_late(self, tenant_id: str, run_id: str,
                            envelope: dict) -> None:
        """TERMINAL 之后的迟到事件只记诊断（§3.2 / T47-10）。"""
        row = self._row(tenant_id, run_id)
        if row is None:
            return
        warnings = self._union_by(_loads(row["warnings"], []), [{
            "code": "LATE_EVENT_AFTER_TERMINAL",
            "event_id": envelope.get("id"), "at": self._stamp()}], key="code")
        self.conn.execute(
            "UPDATE run_dashboard_projection SET warnings = ?, updated_at = ?"
            " WHERE tenant_id = ? AND run_id = ?",
            (_dumps(warnings), self._stamp(), tenant_id, run_id))

    # ================================================================ 查询

    def _row(self, tenant_id: str, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM run_dashboard_projection"
            " WHERE tenant_id = ? AND run_id = ?", (tenant_id, run_id)).fetchone()

    def read_manifest_revision_for_display(self, tenant_id: str,
                                          run_id: str) -> int | None:
        """只用于展示。**任何按它做决定**的逻辑都是错的——权威在 S08/S21。"""
        row = self._row(tenant_id, run_id)
        return None if row is None else row["manifest_revision"]

    def snapshot(self, tenant_id: str, run_id: str) -> dict:
        """§4.2。必带 `as_of` / `projection_watermark` / `projection_state`。"""
        row = self._row(tenant_id, run_id)
        if row is None or row["projection_state"] == "EVICTED":
            # 跨租户与不存在同码（E28 / T47-31）：不泄漏存在性。
            raise ServiceError(
                404, "RUN_NOT_FOUND", "no projection for this run",
                details={"authority_hint": f"GET /runs/{run_id} on S21"})
        return {
            "run_id": row["run_id"],
            "projection_state": row["projection_state"],
            # `as_of` 是**投影自己**的看法时刻：落后多少由它与请求时刻的差
            # 表达，不假装实时（T47-12）。
            "as_of": row["updated_at"],
            "projection_watermark": int(row["change_seq"]),
            "source_watermark": row["source_watermark"],
            "run": {
                "state": row["run_state"],
                "state_since": row["state_since"],
                "episode_id": row["episode_id"] or None,
                "branch_id": row["branch_id"] or None,
                "manifest_revision": row["manifest_revision"],
            },
            "progress": {
                "completed_shots": int(row["completed_shots"]),
                "total_shots": row["total_shots"],
                "current_node": _loads(row["current_node"], None),
            },
            "cost": {
                "spent_micros": int(row["spent_micros"]),
                "spent_as_of": row["spent_as_of"],
                # 余额必须回源；这里只有一个指针（§1.2 / §4.2 / T47-13）。
                "remaining_ref": f"/budget-authorizations/{row['run_id']}",
            },
            "warnings": _loads(row["warnings"], []),
            "artifacts": _loads(row["artifacts"], []),
            "__hints": {
                # T47-14：机械提示哪些字段不是权威、要去哪查。
                "authority": {
                    "run_state": "S21",
                    "budget": "S23",
                    "voice_authorization": "S15",
                    "canon": "S16",
                    "manifest": "S08",
                    "review_decision": "S40",
                    "artifact_bytes": "S28",
                },
                "do_not_resubmit_on_resync": True,
                "projection_is_derived": True,
            },
        }

    def list_runs(self, tenant_id: str, *, state: str | None = None,
                  before: str | None = None, limit: int = 50) -> dict:
        clauses = ["tenant_id = ?", "projection_state <> 'EVICTED'"]
        params: list = [tenant_id]
        if state:
            clauses.append("run_state = ?")
            params.append(state)
        if before:
            clauses.append("updated_at < ?")
            params.append(before)
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            "SELECT run_id, projection_state, change_seq, applied_watermark,"
            " run_state, state_since, episode_id, branch_id, completed_shots,"
            " total_shots, spent_micros, updated_at"
            " FROM run_dashboard_projection"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY updated_at DESC, run_id DESC LIMIT ?", params).fetchall()
        return {"items": [dict(row) for row in rows],
                "next_before": rows[-1]["updated_at"] if rows else None}

    def dashboard_summary(self, tenant_id: str) -> dict:
        """租户级汇总。§12 第 4 条允许 30s 缓存；这里现算，只做聚合读。

        `as_of` 一样要给——汇总也会滞后。
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN run_state IN ('QUEUED','RUNNING','WAITING_INPUT',"
            "   'WAITING_REVIEW','WAITING_MEMORY','WAITING_BUDGET','PAUSED')"
            "   THEN 1 ELSE 0 END) AS active,"
            " SUM(CASE WHEN run_state = 'SUCCEEDED' THEN 1 ELSE 0 END)"
            "   AS completed,"
            " SUM(CASE WHEN projection_state = 'GAPPED' THEN 1 ELSE 0 END)"
            "   AS gapped"
            " FROM run_dashboard_projection"
            " WHERE tenant_id = ? AND projection_state <> 'EVICTED'",
            (tenant_id,)).fetchone()
        return {"active_runs": int(row["active"] or 0),
                "completed_runs": int(row["completed"] or 0),
                "gapped_runs": int(row["gapped"] or 0),
                "total_runs": int(row["total"] or 0),
                "as_of": self._stamp()}

    # ================================================================ 回源

    def resync(self, tenant_id: str, run_id: str) -> dict:
        """§3.8：只读回源 S21，用**上游 watermark** 走同一条 CAS。

        频率上限每 run 每分钟 6 次；超了就 429，让调用方退避——回源风暴会把
        权威打挂，而权威没有为 UI 连接数设计过。
        """
        now = self._clock()
        bucket = self._resync_calls[(tenant_id, run_id)]
        while bucket and (now - bucket[0]).total_seconds() > 60:
            bucket.popleft()
        if len(bucket) >= RESYNC_MAX_PER_MINUTE:
            raise ServiceError(
                429, "RESYNC_LIMIT_REACHED",
                "this run has been resynced 6 times in the last minute",
                retryable=True)
        bucket.append(now)

        try:
            upstream = self.run_source(tenant_id, run_id)
        except ConnectionError as exc:
            raise ServiceError(503, "PROJECTION_UNAVAILABLE",
                               "S21 unreachable, projection stays GAPPED",
                               retryable=True) from exc

        watermark = upstream.get("projection_watermark")
        if not isinstance(watermark, int) or watermark < 0:
            raise ServiceError(
                502, "UPSTREAM_CONTRACT_VIOLATION",
                "S21 snapshot has no integer projection_watermark")
        row = self._row(tenant_id, run_id)
        if row is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "no projection for this run")

        fields = {
            "projection_state": "LIVE",
            "gap": None,
            "source_watermark": watermark,
            "run_state": upstream.get("state"),
            "manifest_revision": upstream.get("manifest_revision"),
            "completed_shots": max(int(row["completed_shots"]),
                                   int(upstream.get("completed_nodes") or 0)),
            "applied_watermark": max(int(row["applied_watermark"]), watermark),
            "last_event_time": self._stamp(),
        }
        if upstream.get("state") in TERMINAL_RUN_STATES:
            fields["projection_state"] = "TERMINAL"
        display = max(int(row["change_seq"]) + 1, watermark)
        changed = self._cas_update(tenant_id, run_id, display, fields)
        if not changed:
            return {"resynced": False, "reason": "stale",
                    "upstream_watermark": watermark}
        if fields["projection_state"] == "LIVE":
            # 回源成功后清掉洞标记（§3.8）——但**不**伪造一条 change_log。
            self.conn.execute(
                "UPDATE run_dashboard_projection SET warnings = ?"
                " WHERE tenant_id = ? AND run_id = ?",
                (_dumps([w for w in _loads(row["warnings"], [])
                         if w.get("code") != "PROJECTION_GAP"]),
                 tenant_id, run_id))
        return {"resynced": True, "upstream_watermark": watermark,
                "projection_state": fields["projection_state"]}

    # ================================================================ 订阅

    def create_subscription(self, tenant_id: str, principal_id: str,
                            body: dict) -> dict:
        """§2.3。`since_watermark` 超出保留窗口 → 410（E36）。"""
        channel = body.get("channel", "sse")
        if channel != "sse":
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                f"channel {channel!r} is not available; only 'sse' exists "
                "today (webhook: C47-7 / C46-6)")
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "scope must be an object")
        scope_kind = scope.get("kind")
        scope_id = scope.get("id")
        if scope_kind not in ("run", "tenant"):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "scope.kind must be 'run' or 'tenant'")
        if scope_kind == "run" and not scope_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "scope.id is required for a run subscription")
        filters = body.get("event_filters") or []
        if not isinstance(filters, list) or any(
                not isinstance(f, str) for f in filters):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event_filters must be a list of strings")

        self._check_subscription_limit(tenant_id, principal_id, scope_kind,
                                       scope_id)
        self._authorize(tenant_id, principal_id, scope_kind, scope_id)

        run_id = scope_id if scope_kind == "run" else None
        row = None
        if run_id:
            row = self._row(tenant_id, run_id)
            if row is None or row["projection_state"] == "EVICTED":
                raise ServiceError(404, "RUN_NOT_FOUND",
                                   "no projection for this run")

        since = body.get("since_watermark")
        if since is not None and not isinstance(since, int):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "since_watermark must be an integer")
        snapshot_watermark = int(row["change_seq"]) if row is not None else 0

        if since is not None:
            self._check_cursor_available(tenant_id, run_id, since)

        subscription_id = _new_id("subscription")
        now = self._stamp()
        self.conn.execute(
            "INSERT INTO subscription_registration (subscription_id, tenant_id,"
            " principal_id, scope_kind, scope_id, event_filters, state,"
            " since_watermark, created_at) VALUES (?,?,?,?,?,?, 'ACTIVE', ?,?)",
            (subscription_id, tenant_id, principal_id, scope_kind, scope_id,
             _dumps(filters), since, now))
        cursor = None
        # PCA-5 / C47-20：租户级订阅可登记，但没有 run_id 就无处放 cursor —
        # 不假装能跑 SSE。
        if run_id:
            cursor = self._open_cursor(
                tenant_id, principal_id, run_id, subscription_id,
                since if since is not None else snapshot_watermark)
            self._touch_connection(tenant_id, principal_id, run_id)
        return {
            "subscription_id": subscription_id,
            "stream_url": (f"/runs/{run_id}/events?subscription_id="
                           f"{subscription_id}") if run_id else None,
            "cursor": cursor["subscription_id"] if cursor else None,
            "as_of": now,
            "snapshot_watermark": snapshot_watermark,
            "channel": channel,
        }

    def _authorize(self, tenant_id: str, principal_id: str, scope_kind: str,
                   scope_id: str | None) -> None:
        """S01 兜底检查（可选）。租户隔离本身由每条 SQL 的 tenant 谓词保证。

        S01 的作用域前缀里没有 `run`（只有 tenant/org_unit/project/series/
        episode），所以 run 级订阅只能用 tenant 作用域去问——问不出"这个 run
        归不归他看"。这一条登记为 C47-25。
        """
        if self.authorization_source is None:
            return
        context = self.authorization_source(
            tenant_id, principal_id,
            "tenant" if scope_kind == "run" else scope_kind,
            _tenant_scope_id(tenant_id) if scope_kind == "run" else scope_id)
        denied = context.get("denied") or []
        permissions = context.get("permissions") or []
        if "run.get" in denied:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "S01 denied run.get for this principal")
        if permissions and "run.get" not in permissions:
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "S01 did not grant run.get for this principal")

    def _check_subscription_limit(self, tenant_id: str, principal_id: str,
                                  scope_kind: str, scope_id: str | None) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM subscription_registration"
            " WHERE tenant_id = ? AND principal_id = ? AND state = 'ACTIVE'",
            (tenant_id, principal_id)).fetchone()
        if int(row[0]) >= CONNECTION_LIMITS["principal"]:
            raise ServiceError(422, "SUBSCRIPTION_LIMIT_REACHED",
                               "a principal may hold at most "
                               f"{CONNECTION_LIMITS['principal']} active "
                               "subscriptions")
        if scope_kind == "run" and scope_id:
            per_run = self.conn.execute(
                "SELECT COUNT(*) FROM subscription_registration"
                " WHERE tenant_id = ? AND scope_id = ? AND state = 'ACTIVE'",
                (tenant_id, scope_id)).fetchone()
            if int(per_run[0]) >= CONNECTION_LIMITS["run"]:
                raise ServiceError(422, "SUBSCRIPTION_LIMIT_REACHED",
                                   "a run may have at most "
                                   f"{CONNECTION_LIMITS['run']} subscribers")

    def close_subscription(self, tenant_id: str, subscription_id: str) -> dict:
        cur = self.conn.execute(
            "UPDATE subscription_registration SET state = 'CLOSED', closed_at = ?"
            " WHERE subscription_id = ? AND tenant_id = ? AND state = 'ACTIVE'",
            (self._stamp(), subscription_id, tenant_id))
        if cur.rowcount == 0:
            raise ServiceError(404, "SUBSCRIPTION_NOT_FOUND",
                               "subscription not found")
        self.conn.execute("DELETE FROM sse_cursor WHERE subscription_id = ?",
                          (subscription_id,))
        return {"subscription_id": subscription_id, "state": "CLOSED"}

    # ================================================================ SSE

    def _open_cursor(self, tenant_id: str, principal_id: str, run_id: str,
                     subscription_id: str, last_sent: int) -> dict:
        now = self._clock()
        expires = now + timedelta(seconds=CURSOR_RETENTION_SECONDS)
        self.conn.execute(
            "INSERT INTO sse_cursor (subscription_id, tenant_id, principal_id,"
            " run_id, channel, last_sent_watermark, created_at,"
            " last_activity_at, expires_at) VALUES (?,?,?,?, 'sse', ?,?,?,?)"
            " ON CONFLICT (subscription_id) DO UPDATE SET"
            " last_sent_watermark = excluded.last_sent_watermark,"
            " last_activity_at = excluded.last_activity_at,"
            " expires_at = excluded.expires_at",
            (subscription_id, tenant_id, principal_id, run_id, last_sent,
             _iso(now), _iso(now), _iso(expires)))
        return {"subscription_id": subscription_id,
                "last_sent_watermark": last_sent,
                "expires_at": _iso(expires)}

    def _check_cursor_available(self, tenant_id: str, run_id: str | None,
                                since: int) -> None:
        """since 的位置是否还能回放。不能就 410 + snapshot_url（§3.7）。"""
        if not run_id:
            return
        oldest = self.conn.execute(
            "SELECT MIN(watermark) FROM projection_change_log"
            " WHERE tenant_id = ? AND run_id = ?", (tenant_id, run_id)).fetchone()[0]
        current = self._row(tenant_id, run_id)
        current_seq = int(current["change_seq"]) if current is not None else 0
        if oldest is None:
            # 没有变更日志：只有 since 就是当前位置时才算可用（§7.2 第 4 行：
            # "游标存在但 change_log 里没有对应记录 → 410"）。
            if since < current_seq:
                raise self._cursor_expired(run_id)
            return
        if since < int(oldest) - 1:
            raise self._cursor_expired(run_id)

    @staticmethod
    def _cursor_expired(run_id: str) -> ServiceError:
        return ServiceError(
            410, "CURSOR_EXPIRED",
            "cursor is outside the replay window (15 min); reload the snapshot "
            "and resubscribe. Never resubmit the user's command (E36).",
            details={
                "snapshot_url": f"/runs/{run_id}/snapshot",
                "do_not_resubmit": [
                    f"GET /runs/{run_id}/snapshot",
                    "rebuild the view from that snapshot",
                    f"GET /runs/{run_id}/events without Last-Event-ID",
                    "do not re-issue the command the user was composing; "
                    "generation and payment are idempotent upstream only under "
                    "their own keys",
                ],
            })

    def open_stream(self, tenant_id: str, principal_id: str, run_id: str, *,
                    subscription_id: str | None = None,
                    last_event_id: str | None = None) -> dict:
        """建立/续接一条 SSE 消费。返回 `{cursor, frames, watermark}`。

        这个内核没有 Redis pub/sub（§6.4 的扇出层），所以按方案的降级路径走
        **轮询**：每次调用返回"游标之后的帧"，客户端按 `keepalive_seconds`
        再拉。帧的 `id` / `event` / `data` 与方案 §4.3 完全一致，换成真 SSE
        时客户端不用改。
        """
        row = self._row(tenant_id, run_id)
        if row is None or row["projection_state"] == "EVICTED":
            raise ServiceError(404, "RUN_NOT_FOUND", "no projection for this run")
        self._authorize(tenant_id, principal_id, "run", run_id)

        cursor = None
        if last_event_id:
            try:
                since = int(last_event_id)
            except (TypeError, ValueError):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "Last-Event-ID must be an integer watermark")
            cursor = self._find_cursor(tenant_id, principal_id, run_id,
                                       subscription_id)
            if cursor is None:
                raise self._cursor_expired(run_id)
            expires = _parse_iso(cursor["expires_at"])
            if expires is not None and expires < self._clock():
                # §7.2：游标超 15 分钟 → 410 + snapshot_url。
                self.conn.execute(
                    "DELETE FROM sse_cursor WHERE subscription_id = ?",
                    (cursor["subscription_id"],))
                raise self._cursor_expired(run_id)
            # **不**把客户端的 id 抬到游标位置。客户端说自己只看到 1、游标
            # 却在 3，可能是它也丢了 2/3（服务端先提交了投影、后推进游标，
            # §7.5 / T47-36）。少发一次比丢一帧贵：重复帧客户端能按 id 去重，
            # 漏掉的帧客户端永远不知道自己缺。
            since = min(since, int(cursor["last_sent_watermark"]))
            if since < int(row["change_seq"]):
                self._check_cursor_available(tenant_id, run_id, since)
        else:
            if subscription_id:
                cursor = self._find_cursor(tenant_id, principal_id, run_id,
                                           subscription_id)
            if cursor is None:
                self._touch_connection(tenant_id, principal_id, run_id)
                subscription_id = subscription_id or _new_id("subscription")
                cursor = self._open_cursor(tenant_id, principal_id, run_id,
                                           subscription_id,
                                           int(row["change_seq"]))
            since = int(cursor["last_sent_watermark"])

        frames, watermark = self._frames_since(tenant_id, run_id, since)
        if cursor:
            self._advance_cursor(cursor, run_id, watermark)
        return {"cursor": cursor["subscription_id"] if cursor else None,
                "frames": frames, "watermark": watermark,
                "keepalive_seconds": 15}

    def _find_cursor(self, tenant_id: str, principal_id: str, run_id: str,
                     subscription_id: str | None) -> sqlite3.Row | None:
        if subscription_id:
            # 裸主键的表必须带租户谓词（PCA-6 / T47-34），否则知道 id 就能读。
            return self.conn.execute(
                "SELECT * FROM sse_cursor WHERE subscription_id = ?"
                " AND tenant_id = ? AND principal_id = ?",
                (subscription_id, tenant_id, principal_id)).fetchone()
        return self.conn.execute(
            "SELECT * FROM sse_cursor WHERE tenant_id = ? AND principal_id = ?"
            " AND run_id = ? ORDER BY last_activity_at DESC LIMIT 1",
            (tenant_id, principal_id, run_id)).fetchone()

    def _touch_connection(self, tenant_id: str, principal_id: str,
                          run_id: str) -> None:
        """§6.5 的三级连接上限。计数按现有游标行——它就是"活跃连接"的代理。"""
        def count(where: str, *params) -> int:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM sse_cursor WHERE " + where,
                params).fetchone()[0])
        if count("tenant_id = ?", tenant_id) >= CONNECTION_LIMITS["tenant"]:
            raise ServiceError(429, "CONNECTION_LIMIT_REACHED",
                               "tenant SSE connection limit reached",
                               retryable=True)
        if count("tenant_id = ? AND principal_id = ?", tenant_id,
                 principal_id) >= CONNECTION_LIMITS["principal"]:
            raise ServiceError(429, "CONNECTION_LIMIT_REACHED",
                               "principal SSE connection limit reached",
                               retryable=True)
        if count("tenant_id = ? AND run_id = ?", tenant_id,
                 run_id) >= CONNECTION_LIMITS["run"]:
            raise ServiceError(429, "CONNECTION_LIMIT_REACHED",
                               "this run has too many watchers", retryable=True)

    def _frames_since(self, tenant_id: str, run_id: str,
                      since: int | None) -> tuple[list[dict], int]:
        since = 0 if since is None else int(since)
        rows = self.conn.execute(
            "SELECT watermark, event_class, payload FROM projection_change_log"
            " WHERE tenant_id = ? AND run_id = ? AND watermark > ?"
            " ORDER BY watermark ASC", (tenant_id, run_id, since)).fetchall()
        frames: list[dict] = []
        watermark = since
        for row in self._merge_for_push(rows):
            payload = _loads(row["payload"], {})
            # §4.3 / T47-23：每一个 data 都带 watermark；没有就是 bug。
            payload.setdefault("watermark", int(row["watermark"]))
            frames.append({"id": int(row["watermark"]),
                           "event": row["event_class"],
                           "data": payload})
            watermark = max(watermark, int(row["watermark"]))
        return frames, watermark

    @staticmethod
    def _merge_for_push(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        """§3.4：node 级 200ms、计数类 500ms 内只推最后一条。

        **合并只影响推几条，不改变事实**：被合并掉的中间态仍在 change_log
        里，重连回放时按 `id` 逐条都能取到。`run.state` 与 `warning` 从不合并。
        `id` 取保留下来那条的 watermark，所以客户端看到的 `id` 仍然单调。
        """
        out: list[sqlite3.Row] = []
        last_index: dict[str, int] = {}
        for row in rows:
            cls = row["event_class"]
            window = MERGE_WINDOWS_MS.get(cls, 0)
            if window and cls in last_index:
                prev = out[last_index[cls]]
                prev_at = _parse_iso(_loads(prev["payload"], {}).get("at"))
                at = _parse_iso(_loads(row["payload"], {}).get("at"))
                if prev_at and at and (
                        at - prev_at).total_seconds() * 1000 <= window:
                    out[last_index[cls]] = row
                    continue
            last_index[cls] = len(out)
            out.append(row)
        return out

    def _advance_cursor(self, cursor: sqlite3.Row, run_id: str,
                        watermark: int) -> None:
        """推进游标并（节流后）发 `projection.cursor.advanced`。"""
        if watermark <= int(cursor["last_sent_watermark"]):
            return  # 没前进就不发（T47-25）
        now = self._clock()
        self.conn.execute(
            "UPDATE sse_cursor SET last_sent_watermark = ?, last_activity_at = ?,"
            " expires_at = ? WHERE subscription_id = ?",
            (watermark, _iso(now),
             _iso(now + timedelta(seconds=CURSOR_RETENTION_SECONDS)),
             cursor["subscription_id"]))
        self._emit_cursor(cursor, run_id, watermark, now)

    def _emit_cursor(self, cursor: sqlite3.Row, run_id: str, watermark: int,
                     now: datetime) -> None:
        """§4.4：每条订阅每秒最多 1 条；`aggregate_sequence = watermark`。"""
        subscription_id = cursor["subscription_id"]
        last = self._cursor_emitted_at.get(subscription_id)
        if last is not None and (now - last).total_seconds() < 1:
            return
        if watermark < 1:
            # PCA-4 / C47-19：信封的 aggregate_sequence 最小是 1，水位 0 的
            # 那一帧发不出去；同一事实由 snapshot 的 projection_watermark=0 表达。
            return
        self._cursor_emitted_at[subscription_id] = now
        projection = self._row(cursor["tenant_id"], run_id)
        state = (projection["projection_state"]
                 if projection is not None else "ABSENT")
        # PCA-2 / C47-19：lag_ms 取"消费处理时刻 − 事件 time"。两个读法里
        # 这一种可自证，且在 GAPPED 时不会被回源时间掩盖。
        last_frame = self.conn.execute(
            "SELECT payload FROM projection_change_log WHERE tenant_id = ?"
            " AND run_id = ? AND watermark = ?",
            (cursor["tenant_id"], run_id, watermark)).fetchone()
        event_time = _parse_iso(
            _loads(last_frame["payload"], {}).get("at")) if last_frame else None
        lag_ms = 0 if event_time is None else max(
            0, int((now - event_time).total_seconds() * 1000))
        event = new_event(
            source=SOURCE,
            type=EVENT_CURSOR_ADVANCED,
            subject=f"subscription/{subscription_id}",
            tenant_id=cursor["tenant_id"],
            home_cell="cell_local",
            aggregate_id=subscription_id,
            sequence=int(watermark),
            # PCA-8 / C47-21：示例里的 null 过不了信封；用订阅 id，非空可追溯。
            correlation_id=subscription_id,
            causation_id=subscription_id,
            data={"subscription_id": subscription_id, "run_id": run_id,
                  "watermark": int(watermark), "lag_ms": lag_ms,
                  "state": state},
            schema_version=SCHEMA_VERSION,
        )
        self.outbox.append(event)

    # ================================================================ 通知

    def _notify(self, tenant_id: str, envelope: dict, event_type: str,
                data: dict) -> dict:
        """§3.9：事件 → 人类需要知道的事。白名单之外的**不通知**。"""
        notify_types = self._notify_types(event_type, data)
        if not notify_types:
            return {"deliveries": 0, "types": []}
        event_id = envelope["id"]
        run_id = self._run_of(event_type, data)
        deliveries = 0
        for principal_id, preference in self._recipients(
                tenant_id, run_id).items():
            wanted, channels = self._effective_preference(preference)
            for notify_type in notify_types:
                if notify_type not in wanted:
                    continue
                for channel in channels:
                    kind = channel.get("kind")
                    if kind not in KNOWN_CHANNELS:
                        continue
                    deliveries += self._enqueue_delivery(
                        tenant_id, principal_id, event_id, notify_type, kind,
                        channel, preference, run_id)
        return {"deliveries": deliveries, "types": sorted(notify_types)}

    def _recipients(self, tenant_id: str,
                    run_id: str | None) -> dict[str, sqlite3.Row | None]:
        """收件人 = 显式偏好 ∪ 该 run 的观看者（后者用默认白名单）。

        S47 不持有主体名录（那是 S01 的），所以"所有该收到的人"只能从这两处
        推出。没在这两处出现的人不会收到——登记为 C47-26。
        """
        rows = self.conn.execute(
            "SELECT * FROM notification_preference WHERE tenant_id = ?",
            (tenant_id,)).fetchall()
        recipients: dict[str, sqlite3.Row | None] = {
            row["principal_id"]: row for row in rows}
        if run_id:
            watchers = self.conn.execute(
                "SELECT DISTINCT principal_id FROM sse_cursor"
                " WHERE tenant_id = ? AND run_id = ?", (tenant_id, run_id)
            ).fetchall()
            for watcher in watchers:
                recipients.setdefault(watcher["principal_id"], None)
        return recipients

    def _effective_preference(self, preference: sqlite3.Row | None):
        if preference is None:
            return set(DEFAULT_NOTIFY_TYPES), [{"kind": "in_app"}]
        return (set(_loads(preference["event_types"], [])),
                _loads(preference["channels"], []))

    def _notify_types(self, event_type: str, data: dict) -> set[str]:
        if event_type == "orchestration.run.state-changed":
            found = set()
            mapped = NOTIFY_OF_RUN_STATE.get(data.get("state"))
            if mapped:
                found.add(mapped)
            if int(data.get("blocked_nodes") or 0) > 0 or data.get("blocked_reason"):
                found.add("run.blocked")
            return found
        if event_type == "delivery.package.sealed":
            return {"delivery.sealed"}
        # §3.9：node.state-changed 太频繁，默认不通知（T47-41）。
        return set()

    def _enqueue_delivery(self, tenant_id: str, principal_id: str,
                          event_id: str, notify_type: str, kind: str,
                          channel: dict, preference: sqlite3.Row | None,
                          run_id: str | None) -> int:
        """建一条投递回执。`nd_uniq` 保证同事件同渠道只有一条。"""
        now = self._clock()
        state, deferred_until, error = "PENDING", None, None
        if kind not in ENABLED_CHANNELS:
            # C47-7 / C46-6：webhook 走 S46 的投递，S47 这边还没有那个命令。
            # 记 FAILED + 明确原因，不静默丢弃（§10.4 要求 UI 上说清楚）。
            state, error = "FAILED", "CHANNEL_NOT_AVAILABLE"
        elif notify_type not in URGENT_NOTIFY_TYPES:
            quiet = _loads(preference["quiet_hours"], None) \
                if preference is not None else None
            end = _quiet_hours_end(quiet, now)
            if end is not None:
                state, deferred_until = "DEFERRED", _iso(end)

        delivery_id = _new_id("delivery")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO notification_delivery (delivery_id,"
            " tenant_id, principal_id, event_id, event_type, channel_kind,"
            " channel_ref, state, deferred_until, attempt_count, last_error,"
            " sent_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,NULL,?)",
            (delivery_id, tenant_id, principal_id, event_id, notify_type, kind,
             channel.get("address_ref") or channel.get("subscription_id"),
             state, deferred_until, error, _iso(now)))
        if cur.rowcount == 0:
            return 0  # 去重：同一事件同一渠道只投一次（T47-44）

        if state == "PENDING" and kind == "in_app":
            self._send_in_app(tenant_id, principal_id, event_id, notify_type,
                              delivery_id, run_id)
        # email 保持 PENDING：仓库里没有投递器，S47 只负责记下"该发"。
        return 1

    def _send_in_app(self, tenant_id: str, principal_id: str, event_id: str,
                     notify_type: str, delivery_id: str,
                     run_id: str | None) -> None:
        notification_id = _new_id("notification")
        self.conn.execute(
            "INSERT OR IGNORE INTO in_app_notification (notification_id,"
            " tenant_id, principal_id, event_id, event_type, title, body_ref,"
            " run_id, read_at, created_at) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
            (notification_id, tenant_id, principal_id, event_id, notify_type,
             notify_type.replace(".", " ").upper(),
             f"/notifications/{notification_id}", run_id, self._stamp()))
        self.conn.execute(
            "UPDATE notification_delivery SET state = 'SENT', sent_at = ?,"
            " attempt_count = 1 WHERE delivery_id = ?", (self._stamp(), delivery_id))

    def get_preference(self, tenant_id: str, principal_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM notification_preference"
            " WHERE tenant_id = ? AND principal_id = ?",
            (tenant_id, principal_id)).fetchone()
        if row is None:
            # 默认值也是事实：不返回 404，否则每个前端要自己发明一份默认。
            return {"tenant_id": tenant_id, "principal_id": principal_id,
                    "event_types": sorted(DEFAULT_NOTIFY_TYPES),
                    "channels": [{"kind": "in_app"}], "quiet_hours": None,
                    "updated_at": None, "is_default": True}
        return {"tenant_id": tenant_id, "principal_id": principal_id,
                "event_types": _loads(row["event_types"], []),
                "channels": _loads(row["channels"], []),
                "quiet_hours": _loads(row["quiet_hours"], None),
                "updated_at": row["updated_at"], "is_default": False}

    def put_preference(self, tenant_id: str, principal_id: str,
                       body: dict) -> dict:
        """`POST` / `PATCH` 都是 last-write-wins（§6.3）。不猜词表。"""
        current = self.get_preference(tenant_id, principal_id)
        event_types = body.get("event_types", current["event_types"])
        channels = body.get("channels", current["channels"])
        quiet_hours = body.get("quiet_hours", current["quiet_hours"])
        if not isinstance(event_types, list) or any(
                not isinstance(t, str) for t in event_types):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event_types must be a list of strings")
        if not isinstance(channels, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "channels must be a list")
        for channel in channels:
            if not isinstance(channel, dict) or \
                    channel.get("kind") not in KNOWN_CHANNELS:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "each channel needs a known kind "
                                   f"(one of {sorted(KNOWN_CHANNELS)})")
        if quiet_hours is not None and not isinstance(quiet_hours, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "quiet_hours must be an object or null")
        self.conn.execute(
            "INSERT INTO notification_preference (tenant_id, principal_id,"
            " event_types, channels, quiet_hours, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (tenant_id, principal_id)"
            " DO UPDATE SET event_types = excluded.event_types,"
            " channels = excluded.channels, quiet_hours = excluded.quiet_hours,"
            " updated_at = excluded.updated_at",
            (tenant_id, principal_id, _dumps(event_types), _dumps(channels),
             _dumps(quiet_hours) if quiet_hours is not None else None, self._stamp()))
        return self.get_preference(tenant_id, principal_id)

    def list_notifications(self, tenant_id: str, principal_id: str, *,
                           unread_only: bool = False, limit: int = 50) -> dict:
        clause = " AND read_at IS NULL" if unread_only else ""
        rows = self.conn.execute(
            "SELECT notification_id, event_id, event_type, title, body_ref,"
            " run_id, read_at, created_at FROM in_app_notification"
            " WHERE tenant_id = ? AND principal_id = ?" + clause +
            " ORDER BY created_at DESC LIMIT ?",
            (tenant_id, principal_id, max(1, min(int(limit), 200)))).fetchall()
        return {"items": [dict(row) for row in rows],
                "unread_only": unread_only}

    def mark_read(self, tenant_id: str, principal_id: str,
                  notification_id: str) -> dict:
        cur = self.conn.execute(
            "UPDATE in_app_notification SET read_at = ?"
            " WHERE notification_id = ? AND tenant_id = ? AND principal_id = ?"
            " AND read_at IS NULL", (self._stamp(), notification_id, tenant_id,
                                     principal_id))
        if cur.rowcount == 0:
            exists = self.conn.execute(
                "SELECT 1 FROM in_app_notification WHERE notification_id = ?"
                " AND tenant_id = ? AND principal_id = ?",
                (notification_id, tenant_id, principal_id)).fetchone()
            if exists is None:
                raise ServiceError(404, "NOTIFICATION_NOT_FOUND",
                                   "notification not found")
            return {"notification_id": notification_id, "already_read": True}
        return {"notification_id": notification_id, "already_read": False}

    # ================================================================ 维护

    def prune(self) -> dict:
        """§5.2 / §7.2 / T47-39：清理 15 分钟前的 change_log 与过期游标。

        清理只删"确定早于保留窗口"的行，所以与写入并发也不会删掉正在用的。
        """
        cutoff = _iso(self._clock() - timedelta(seconds=CURSOR_RETENTION_SECONDS))
        changes = self.conn.execute(
            "DELETE FROM projection_change_log WHERE created_at < ?",
            (cutoff,)).rowcount
        cursors = self.conn.execute(
            "DELETE FROM sse_cursor WHERE expires_at < ?",
            (_iso(self._clock()),)).rowcount
        return {"change_log_deleted": changes, "cursors_deleted": cursors}

    def evict(self, tenant_id: str, run_id: str) -> dict:
        """§3.2：无观看者且过保留期时标记 EVICTED。行仍可重建。"""
        cur = self.conn.execute(
            "UPDATE run_dashboard_projection SET projection_state = 'EVICTED',"
            " evicted_at = ?, updated_at = ? WHERE tenant_id = ? AND run_id = ?"
            " AND projection_state <> 'EVICTED'",
            (self._stamp(), self._stamp(), tenant_id, run_id))
        if cur.rowcount == 0:
            raise ServiceError(404, "RUN_NOT_FOUND", "no projection for this run")
        self.conn.execute(
            "DELETE FROM projection_change_log WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id))
        return {"run_id": run_id, "projection_state": "EVICTED"}


def _tenant_scope_id(tenant_id: str) -> str:
    """S01 的 tenant 作用域要求 `tnt_` 前缀（`SCOPE_PREFIX['tenant']`）。

    租户 id 本身可能就是 `tnt_...`；不是就拼一个——**不是**在伪造事实，而是在
    构造一个 S01 认得的查询参数（查不到会 404，不会误放行）。
    """
    return tenant_id if tenant_id.startswith("tnt_") else f"tnt_{tenant_id}"


def _quiet_hours_end(quiet: dict | None, now: datetime) -> datetime | None:
    """落在 quiet hours 内则返回窗口结束时刻（UTC）；否则 None。

    tz 解析不了就**当作没有 quiet hours**：静音失败比误静音安全——误静音的
    后果是"用户完全没被告知"，而拒绝静音只多一条通知。
    """
    if not isinstance(quiet, dict):
        return None
    start_text, end_text = quiet.get("from"), quiet.get("to")
    if not start_text or not end_text:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(quiet.get("tz") or "UTC")
    except Exception:  # noqa: BLE001 - tz 数据缺失也要能跑
        return None
    try:
        start_h, start_m = (int(x) for x in str(start_text).split(":"))
        end_h, end_m = (int(x) for x in str(end_text).split(":"))
    except ValueError:
        return None
    local = now.astimezone(zone)
    minutes = local.hour * 60 + local.minute
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    inside = (start <= minutes < end) if start <= end else (
        minutes >= start or minutes < end)
    if not inside:
        return None
    end_local = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end_local <= local:
        end_local += timedelta(days=1)
    return end_local.astimezone(timezone.utc)


def _resync_unavailable(*_args, **_kwargs):
    """未配置 S21 时：保持 GAPPED，不伪造一份"看起来对齐"的投影。"""
    raise ServiceError(
        503, "PROJECTION_UNAVAILABLE",
        "S21_BASE_URL is not configured; a GAPPED projection stays GAPPED "
        "rather than being filled from a guess (plan §3.8)")
