"""S44 评测与分析测试。方案 §9 的 C / E12 / E13 / E25 / E26 / E28 / E36 / E38
八个分组，加上通用验收（表清单、跨服务写、CONTRACT_GAPS、事件 schema、
未配置依赖的 503 桩、重启恢复、HTTP 层）。

三条主线，与 §0 / §2.4 的措辞一一对应：

1. **没有数据不是 0。** 上游不可达 → `503 DEPENDENCY_UNAVAILABLE`；冻结
   cohort 的成员在源里没有事实 → `503 ... INCOMPLETE_READ`（不许缩小分母）；
   `n = 0` → `value = null`（DDL CHECK 与事件契约 `allOf` 是同一条规则）。
   每条失败路径都同时断言「库里没有半截快照」。
2. **S44 是读数，不是账本。** 静态断言：写语句只落在本服务 19 张表上；
   `DELETE` 一条都没有；`UPDATE` 只出现在三个状态机表上；不 import 别的
   服务、不自己拨号（`deps.py` 里没有任何 URL scheme 字面量）。
3. **整数 micro 守恒。** 分摊在 10000 组随机金额/权重上逐组核对
   `sum(allocations) == original`；真实快照上再核对
   `sum(cost_allocation) == sum(cost_entry)`。

上游全部是闭包 stub：**不联网、不调模型、不付费**。注入的
`evaluation_execution_enabled=True` 只在这份测试的 stub 评测器上成立；
`test_http_evaluation_execution_is_off_by_default` 证明 API 层默认关闭。

每个用例的 docstring 第一行是它在方案里的编号。方案 §9 **没有 `T44-*`
编号**；`test_plan_ids_are_covered_both_ways` 用 AST 与方案原文双向比对，
并断言集合大小恰好 28。
"""

from __future__ import annotations

import ast
import csv
import importlib.util
import io
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from idempotency_sdk import (  # noqa: F401  (CommandInProgress 供参考)
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from evaluation_analytics import deps
from evaluation_analytics.deps import (
    REQUIRED_CONTEXT_KEYS,
    build_accepted_source,
    build_cost_source,
    build_delivery_source,
    build_evaluator_source,
    build_job_source,
    build_qc_source,
    build_review_source,
    validate_execution_context,
)
from evaluation_analytics.schema import (
    CONTRACT_GAPS,
    EVENTS,
    EVENT_EVALUATION,
    EVENT_PROJECTION,
    EVENT_SNAPSHOT,
    METRIC_KEYS,
    SCHEMA_VERSION,
    SEQUENCE_ONE,
    SOURCE,
    TOPIC,
)
from evaluation_analytics.service import (
    SNAPSHOT_CSV_COLUMNS,
    AllocationEngine,
    EvaluationAnalyticsService,
    ServiceError,
    bootstrap_interval,
    krippendorff_alpha,
    percentile,
    split_micro,
    stable_request_hash,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
PKG_SRC = Path(__file__).resolve().parents[1] / "src" / "evaluation_analytics"
APP_PATH = Path(__file__).resolve().parents[1] / "api" / "app.py"
PLAN_PATH = ROOT / "plans" / "S44-evaluation-analytics.md"

TENANT = "t1"
OTHER = "t2"

CASE_ID = re.compile(r"(?:E\d+-S44-\d{2}|C-S44-\d{2})")

# 21 张自有表 = 19 张 DDL 表 + outbox + inbox。
# `idempotency_record` 由平台 idempotency SDK 建，不算本服务自有。
OWNED_TABLES = {
    "accepted_projection",
    "allocation_policy",
    "benchmark_dataset",
    "benchmark_item",
    "blind_review",
    "blind_review_item",
    "blind_review_vote",
    "cohort",
    "cohort_member",
    "cost_allocation",
    "cost_projection",
    "customer_roi",
    "evaluation_run",
    "experiment",
    "experiment_assignment",
    "inbox",
    "metric_definition",
    "metric_snapshot",
    "outbox",
    "project_scope",
    "source_watermark",
}

# 三张允许 UPDATE 的状态机表（其余表只能 INSERT，快照与分摊永不被改写）。
UPDATABLE_TABLES = {"evaluation_run", "blind_review", "experiment"}

# 两个合法的无 tenant_id 语句：内置口径（tenant_id=''）与动态 WHERE。
TENANTLESS_SQL = (
    "select * from metric_definition where metric_key=?",
    "select * from metric_snapshot where",
)

V1_RULES = [
    {"match": {"cost_type": "evaluation"}, "basis": "excluded",
     "dimension": "tenant"},
    {"match": {"cost_type": "retry"}, "basis": "to_parent_episode",
     "dimension": "episode"},
    {"match": {"cost_type": "platform_fixed"}, "basis": "estimated_by_policy",
     "dimension": "episode"},
    {"match": {"cost_type": "generation"}, "basis": "direct",
     "dimension": "episode"},
]

# v2 把评测成本改成直接分摊到集。K3 仍必须排除它 —— 排除的依据是
# `cost_type`，不是 `basis`（E38-S44-09 的强形式）。
V2_RULES = [
    {"match": {"cost_type": "evaluation"}, "basis": "direct",
     "dimension": "episode"},
] + V1_RULES[1:]


# --------------------------------------------------------------------- 输入


def cohort_body(command_id="c1", **over):
    """一个能冻结的 cohort 定义。时间窗必须显式给两端（§2.2）。"""
    payload = {
        "command_id": command_id,
        "definition": {
            "project_ids": ["p1"],
            "time_window": {"from": "2026-01-01T00:00:00Z",
                            "to": "2026-02-01T00:00:00Z"},
            "include_failed_runs": True,
        },
    }
    payload.update(over)
    return payload


def snapshot_body(metric_key, cohort_id, command_id="k1", **over):
    payload = {"command_id": command_id, "cohort_id": cohort_id,
               "metric_key": metric_key}
    payload.update(over)
    return payload


def dataset_body(command_id="d1", items=6, purpose="qc_calibration"):
    return {
        "command_id": command_id,
        "dataset_id": "ds1",
        "version": 1,
        "name": "bench",
        "purpose": purpose,
        "items": [
            {"item_id": f"i{index}",
             "artifact_refs": [{"id": f"a{index}"}],
             "ground_truth": {"label": "ok"},
             "difficulty": "medium"}
            for index in range(1, items + 1)
        ],
    }


def experiment_body(command_id="x1"):
    return {
        "command_id": command_id,
        "name": "seed-prompt-ab",
        "arms": [{"name": "a", "config_ref": {"id": "cfg-a"}},
                 {"name": "b", "config_ref": {"id": "cfg-b"}}],
        "planned_metrics": ["usable_rate", "first_pass_shot_rate"],
    }


def valid_context(**over):
    """一份**真的能过** `contracts/execution-context.v2.schema.json` 的上下文。

    四个嵌套对象各自 `additionalProperties: false`，`id: m1` 这种简写会被
    判 422 —— 这正是要测的行为，所以正样本必须按契约写全。
    """
    context = {
        "schema_version": "2.0",
        "tenant_id": TENANT,
        "project_id": "p1",
        "series_id": "s1",
        "branch_id": "b1",
        "episode_id": "e1",
        "home_region": "cn-north",
        "home_cell": "cell-1",
        "manifest_ref": {"id": "m1", "revision": 1, "content_hash": "b" * 64},
        "node_id": "n1",
        "node_epoch": 1,
        "execution_fence": 1,
        "memory_checkpoint": {"branch_id": "b1", "commit_sequence": 1,
                              "content_hash": "c" * 64},
        "policy_ref": {"service_id": "S02", "id": "pol1", "revision": 1,
                       "content_hash": "d" * 64},
        "actor": {"principal_id": "u1", "principal_type": "human",
                  "delegation_id": "d1"},
        "command_id": "c1",
        "request_hash": "a" * 64,
        "trace_id": "trace-1",
    }
    context.update(over)
    return context


# ------------------------------------------------------------------- 测试台


class Harness:
    """内存库 + 全闭包 stub。构造顺序与 `api/app.py` 一致。

    `SqliteIdempotencyStore` **必须在 service 之后构造**：service 构造时会
    `executescript(SCHEMA)`，其中没有 `idempotency_record`；先建 store 不会
    出错，但保持与生产一致的顺序更不容易走偏。
    """

    SOURCE_STUBS = {
        "cost_source": "st_cost",
        "qc_source": "st_qc",
        "review_source": "st_review",
        "delivery_source": "st_delivery",
        "job_source": "st_job",
        "accepted_source": "st_accepted",
        "evaluator_source": "st_evaluator",
    }

    def __init__(self, conn=None, *, enabled=True, empty_episodes=False, **over):
        # `check_same_thread=False` 与 `api/app.py` 一致：FastAPI 把同步路由
        # 丢到线程池，连接必须允许跨线程使用。
        self.conn = (conn if conn is not None
                     else sqlite3.connect(":memory:", check_same_thread=False,
                                          isolation_level=None))
        self.conn.row_factory = sqlite3.Row
        self.calls = Counter()
        # 上游水位由测试直接改；stub 每次都读当前值。
        self.watermarks = {"cost": 5, "job": 7, "accepted": 4,
                           "delivery": 6, "review": 3}
        self.episodes_data = self._default_episodes()
        self.empty_episodes = empty_episodes
        stubs = {}
        for name, attr in self.SOURCE_STUBS.items():
            # 覆盖也要计数：否则 `harness.calls[...]` 对覆盖掉的依赖永远是 0，
            # "没被再调一次"这类断言会假绿。
            stubs[name] = self._counted(
                name, over.pop(name) if name in over else getattr(self, attr))
        if over:
            raise TypeError(f"unknown source overrides: {sorted(over)}")
        self.service = EvaluationAnalyticsService(
            self.conn, evaluation_execution_enabled=enabled, **stubs)
        # 等水位时不真的睡。
        self.service.sleeper = lambda _seconds: None
        self._idem = SqliteIdempotencyStore(self.conn)

    # -- 默认上游 -----------------------------------------------------
    @staticmethod
    def _default_episodes():
        return {
            "e1": {
                "episode_id": "e1", "project_id": "p1", "abandoned": False,
                "entered_formal_render": True, "failed": False,
                "first_run_created_at": "2026-01-01T00:00:00Z",
                "shots": [
                    {"shot_id": "sh1", "entered_first_qc": True,
                     "first_qc_decision": "pass", "rework_decisions": 0},
                    {"shot_id": "sh2", "entered_first_qc": True,
                     "first_qc_decision": "abstain", "rework_decisions": 2},
                ],
                "nodes": [
                    {"node_id": "nd1", "human_decision": True},
                    {"node_id": "nd2", "human_decision": False},
                ],
            },
            "e2": {
                "episode_id": "e2", "project_id": "p1", "abandoned": False,
                "entered_formal_render": True, "failed": True,
                "first_run_created_at": "2026-01-02T00:00:00Z",
                "shots": [
                    {"shot_id": "sh3", "entered_first_qc": True,
                     "first_qc_decision": "fail", "rework_decisions": 3},
                ],
                "nodes": [{"node_id": "nd3", "human_decision": True}],
            },
            "e3": {
                "episode_id": "e3", "project_id": "p1", "abandoned": False,
                "entered_formal_render": True, "failed": False,
                "first_run_created_at": "2026-01-03T00:00:00Z",
                "shots": [], "nodes": [],
            },
        }

    @staticmethod
    def _select(mapping, query):
        raw = (query or {}).get("episode_ids")
        if isinstance(raw, (list, tuple)):
            wanted = [str(item) for item in raw]
        elif isinstance(raw, str) and raw:
            wanted = [item for item in raw.split(",") if item]
        else:
            wanted = None
        keys = sorted(mapping) if wanted is None else [
            key for key in wanted if key in mapping]
        return [mapping[key] for key in keys]

    def st_job(self, tenant, query):
        rows = {} if self.empty_episodes else {
            key: dict(value) for key, value in self.episodes_data.items()}
        return {"episodes": self._select(rows, query),
                "watermark": self.watermarks["job"]}

    def st_accepted(self, tenant, query):
        rows = {
            "e1": {"episode_id": "e1", "entered_formal_render": True,
                   "accepted": True, "duration_seconds": 600},
            "e2": {"episode_id": "e2", "entered_formal_render": True,
                   "accepted": False, "duration_seconds": 300},
            "e3": {"episode_id": "e3", "entered_formal_render": True,
                   "accepted": False, "duration_seconds": 120},
        }
        return {"episodes": self._select(rows, query),
                "watermark": self.watermarks["accepted"]}

    def cost_entries(self):
        now = datetime.now(timezone.utc)

        def day(offset):
            return (now - timedelta(days=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

        return [
            {"cost_entry_id": "ce_gen_e1", "amount_micro": 6_000_000,
             "cost_type": "generation", "occurred_at": day(1),
             "attribution": {"episode_id": "e1"}},
            {"cost_entry_id": "ce_gen_e2", "amount_micro": 4_000_000,
             "cost_type": "generation", "occurred_at": day(2),
             "attribution": {"episode_id": "e2"}},
            # 评测成本也带集归属：这样 v2 策略把它改成 direct/episode 后
            # 仍然能分摊成功，而 K3 依旧要按 cost_type 把它排除（E38-S44-09
            # 的强形式：排除依据是类别，不是 basis）。
            {"cost_entry_id": "ce_eval", "amount_micro": 1_000_000,
             "cost_type": "evaluation", "occurred_at": day(3),
             "attribution": {"episode_id": "e1"}},
            {"cost_entry_id": "ce_fixed", "amount_micro": 500_000,
             "cost_type": "platform_fixed", "occurred_at": day(2),
             "attribution": {"episode_id": "e1"}},
            {"cost_entry_id": "ce_retry_a", "amount_micro": 100_000,
             "cost_type": "retry", "occurred_at": day(1),
             "attribution": {"episode_id": "e1", "shot_id": "sh1",
                             "run_id": "r1"}},
            {"cost_entry_id": "ce_retry_b", "amount_micro": 50_000,
             "cost_type": "retry", "occurred_at": day(3),
             "attribution": {"episode_id": "e1", "shot_id": "sh1",
                             "run_id": "r2"}},
        ]

    def st_cost(self, tenant, query):
        return {"entries": [dict(entry) for entry in self.cost_entries()],
                "watermark": self.watermarks["cost"]}

    def st_review(self, tenant, query):
        return {
            "referenced_fact_ids": ["f1", "f2"],
            "decisions": [
                {"kind": "memory_conflict", "fact_id": "f2",
                 "confirmed": True},
                {"kind": "memory_conflict", "fact_id": "f1",
                 "confirmed": False},
            ],
            "watermark": self.watermarks["review"],
        }

    def st_delivery(self, tenant, query):
        return {"packages": [{"episode_id": "e1",
                              "published_at": "2026-01-05T00:00:00Z"}],
                "watermark": self.watermarks["delivery"]}

    def st_qc(self, tenant, request):
        return {"rows": [], "watermark": 2}

    def st_evaluator(self, tenant, request):
        return {"item_succeeded": len(request.get("items") or []),
                "item_failed": 0,
                "result_ref": {"id": "res1"},
                "budget_ref": {"reservation_id": "br1"}}

    # -- 工具 ---------------------------------------------------------
    def _counted(self, name, fn):
        def _inner(*args, **kwargs):
            self.calls[name] += 1
            return fn(*args, **kwargs)
        return _inner

    def command(self, tenant, payload, execute):
        """与 `api/app.py` 的 `command()` 等价的幂等外壳。"""
        request_hash = stable_request_hash(payload)
        record = self._idem.begin(tenant, payload["command_id"], request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(request_hash)
        except Exception:
            try:
                self._idem.abort(tenant, payload["command_id"])
            except sqlite3.OperationalError:
                pass
            raise
        self._idem.finish(tenant, payload["command_id"],
                          json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    # -- 业务快捷方式 --------------------------------------------------
    def seed_scope(self, project_id="p1", tenant=TENANT, event_id="ev-scope",
                   sequence=1):
        """喂一条 S08 的 `snapshot.manifest.committed`，建立项目归属。"""
        return self.service.consume_event(tenant, {
            "id": event_id, "type": "snapshot.manifest.committed",
            "tenant_id": tenant, "sequence": sequence,
            "data": {"project_id": project_id, "manifest_id": "man1",
                     "node_id": "n1", "episode_id": "e1",
                     "accepted_revision": 1}})

    def freeze(self, tenant=TENANT, **over):
        payload = cohort_body(**over)
        return self.command(
            tenant, payload,
            lambda h: self.service.freeze_cohort(
                tenant, payload["command_id"], payload, h))

    def cohort(self, **over):
        """归属 + 冻结，返回 cohort_id。"""
        self.seed_scope()
        return self.freeze(**over)["cohort_id"]

    def put_policy(self, version="v1", rules=None, tenant=TENANT):
        return self.service.put_allocation_policy(
            tenant, {"policy_version": version, "rules": rules or V1_RULES})

    def ready(self, **over):
        """能算快照的最小世界：归属 + cohort + 分摊策略 v1。"""
        cohort_id = self.cohort(**over)
        self.put_policy()
        return cohort_id

    def snapshot(self, metric_key, cohort_id, command_id="k1", tenant=TENANT,
                 **over):
        payload = snapshot_body(metric_key, cohort_id, command_id, **over)
        return self.command(
            tenant, payload,
            lambda h: self.service.compute_snapshot(
                tenant, command_id, payload, h))

    def events(self):
        rows = self.conn.execute(
            "SELECT * FROM outbox ORDER BY created_at, event_id").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(item["payload"])
            out.append(item)
        return out

    def event_types(self):
        return [event["event_type"] for event in self.events()]

    def event_data(self, event_type):
        return [event["envelope"]["data"] for event in self.events()
                if event["event_type"] == event_type]

    def count(self, table):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def tables(self):
        return {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def h():
    return Harness()


# ============================================================ 指标取数 · 快照

def test_k1_keeps_failed_runs_in_the_denominator(h):
    """E38-S44-04 cohort 含失败 run，K1 分母包含它们（不被悄悄剔除）"""
    cohort_id = h.ready()
    snap = h.snapshot("usable_rate", cohort_id, command_id="k1")

    # e1 被接受、e2 失败、e3 未接受：分母是 3（三集都进了 formal 渲染），
    # 分子是 1。失败集若被剔掉，率会从 0.333 变成 0.5 —— 那正是「只报成功
    # 样本均值」。
    assert snap["n"] == 3
    assert snap["numerator_value"] == 1.0
    assert snap["denominator_value"] == 3.0
    assert snap["value"] == pytest.approx(1 / 3, abs=1e-6)
    # 失败集没有被隐藏：它在 excluded 里被计数，但仍算进分母。
    assert snap["excluded"]["failed_runs"] == 1
    assert snap["excluded"]["insufficient_data"] == 0


def test_excluded_is_always_present_and_counts_match(h):
    """E38-S44-05 excluded 字段在所有快照都出现，且计数与实际剔除一致"""
    cohort_id = h.ready()
    for key in set(METRIC_KEYS) - {"customer_roi"}:
        snap = h.snapshot(key, cohort_id, command_id=f"x-{key}")
        # 四个基础键在任何快照里都在（即使全 0）；缺字段才是问题。
        for field in ("failed_runs", "abandoned", "insufficient_data",
                      "failed_items"):
            assert field in snap["excluded"], (key, snap["excluded"])

    # 计数必须能与源对上。K4 只对 e1 有发布记录：e2/e3 是「没有事实」，
    # 计进 insufficient_data，而不是被当成 0 秒交付。
    lead = h.snapshot("delivery_lead_time", cohort_id, command_id="k4")
    assert lead["n"] == 1
    assert lead["excluded"]["insufficient_data"] == 2
    assert lead["excluded"]["failed_runs"] == 0

    # K1 的 failed_runs 计数 = S26 里 failed=True 的成员数（恰好 1 个）。
    k1 = h.snapshot("usable_rate", cohort_id, command_id="k1b")
    failed_in_source = sum(
        1 for row in h.episodes_data.values() if row.get("failed") is True)
    assert k1["excluded"]["failed_runs"] == failed_in_source == 1


def test_n_zero_means_null_not_zero(h):
    """E38-S44-06 n=0 → value=null，不返回 0"""
    # 空 cohort：成员集合冻结为空。没有样本不是「可用率 0%」。
    h.seed_scope()
    cohort_id = h.freeze(definition={
        "project_ids": ["p1"],
        "time_window": {"from": "2026-01-01T00:00:00Z",
                        "to": "2026-02-01T00:00:00Z"},
        "episode_ids": ["does-not-exist"],
    })["cohort_id"]
    assert h.service.get_cohort(TENANT, cohort_id)["member_count"] == 0
    h.put_policy()

    snap = h.snapshot("usable_rate", cohort_id, command_id="zero")
    assert snap["n"] == 0
    assert snap["value"] is None          # 不是 0.0
    assert snap["ci95"] is None
    assert snap["distribution"] == {}

    # DDL 层再挡一次：伪造一条 n=0 且 value=0 的行必须失败。
    with pytest.raises(sqlite3.IntegrityError):
        h.conn.execute(
            "INSERT INTO metric_snapshot (tenant_id, snapshot_id, metric_key,"
            " metric_version, cohort_id, value, unit, n, distribution, excluded,"
            " mixed_versions, source_watermarks, watermarks_hash, computed_at)"
            " VALUES (?, ?, 'usable_rate', 1, ?, 0.0, 'ratio', 0, '{}', '{}', 0,"
            " '{}', 'h', '2026-01-01T00:00:00Z')",
            (TENANT, "snap_forged", cohort_id))

    # 事件契约也写同一条规则：value=null 的 data 能过，value=0 的过不了。
    jsonschema = pytest.importorskip("jsonschema")  # 无 jsonschema 时跳过校验
    contract = json.loads((CONTRACTS / "analytics.metric-snapshot-created.v2"
                                        ".schema.json").read_text("utf-8"))
    event = h.event_data(EVENT_SNAPSHOT)[0]
    base = {key: (0 if key == "value" else value)
            for key, value in event.items()}
    base["n"] = 0
    base["value"] = None
    jsonschema.validate(base, contract)
    bad = dict(base, value=0)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, contract)


def test_cost_metrics_carry_distribution_and_ci(h):
    """E38-S44-07 成本类指标同时给 p50/p95/max 与 CI；n<30 标 low_confidence"""
    cohort_id = h.ready()
    cost = h.snapshot("cost_per_usable_minute", cohort_id, command_id="k3ci")
    assert set(cost["distribution"]) >= {"p50", "p95", "max"}
    assert cost["ci95"] is not None
    assert cost["ci95"]["low"] <= cost["value"] <= cost["ci95"]["high"]
    # e1 一集可用 = 10 分钟，n=10 < 30 → 低置信。样本少时 CI 很宽，不能当成
    # 「成本就是 0.615」对外报。
    assert cost["n"] == 10
    assert cost["low_confidence"] is True

    # 30 个可用分钟：同一口径下不再标低置信。
    wide = Harness()
    wide.episodes_data = {
        f"e{index}": {
            "episode_id": f"e{index}", "project_id": "p1", "abandoned": False,
            "entered_formal_render": True, "failed": False,
            "first_run_created_at": "2026-01-01T00:00:00Z",
            "shots": [], "nodes": [],
        } for index in (1, 2, 3)}
    wide.service.accepted_source = wide._counted(
        "accepted_source",
        lambda tenant, query: {
            "episodes": wide._select(
                {key: {"episode_id": key, "entered_formal_render": True,
                       "accepted": True, "duration_seconds": 600}
                 for key in wide.episodes_data}, query),
            "watermark": 4})
    wide.episodes_data["e1"]["shots"] = [{"shot_id": "s", "rework_decisions": 0}]
    wide.episodes_data["e2"]["shots"] = [{"shot_id": "s", "rework_decisions": 0}]
    wide.episodes_data["e3"]["shots"] = [{"shot_id": "s", "rework_decisions": 0}]
    wide_cohort = wide.cohort()
    wide.put_policy()
    wide_cost = wide.snapshot("cost_per_usable_minute", wide_cohort,
                              command_id="k3wide")
    assert wide_cost["n"] == 30
    assert wide_cost["low_confidence"] is False
    # 一秒都不长在分母外：10 + 10 + 10 = 30。
    assert wide_cost["denominator_value"] == 30.0


def test_estimated_and_precise_allocations_are_not_mixed(h):
    """E38-S44-08 估算分摊与精确分摊在同一 KPI 中被标记区分，不混成一个数"""
    cohort_id = h.ready()
    snap = h.snapshot("cost_per_usable_minute", cohort_id, command_id="k3est")

    # platform_fixed（500,000 micro）是 estimated_by_policy；它单列在
    # excluded 里，**没有**进分子。
    assert snap["excluded"]["estimated_allocation_micro"] == 500_000
    assert snap["excluded"]["estimated_allocation_counted_separately"] is True

    # 分子只含精确分摊：6,000,000 + 100,000 + 50,000 = 6,150,000。
    precise = 6_000_000 + 100_000 + 50_000
    assert snap["numerator_value"] == precise
    # 若把估算混进去会是 6,650,000 —— 同一集金额两种口径，必须可区分。
    assert snap["numerator_value"] != precise + 500_000

    rows = h.conn.execute(
        "SELECT basis, amount_micro FROM cost_allocation WHERE tenant_id=?"
        " AND dimension='episode' AND dimension_value='e1'", (TENANT,)).fetchall()
    by_basis = {}
    for row in rows:
        by_basis[row["basis"]] = by_basis.get(row["basis"], 0) + row["amount_micro"]
    assert by_basis["estimated_by_policy"] == 500_000
    # e1 的精确分摊 = gen_e1 6,000,000 + retry_a 100,000 + retry_b 50,000。
    # e2 的 4,000,000 属于另一集，不在这个 K3 分母单位里。
    assert by_basis["direct"] + by_basis["to_parent_episode"] == precise


def test_evaluation_cost_never_enters_k3(h):
    """E38-S44-09 评测成本类别不计入 K3 的生产成本"""
    cohort_id = h.ready()
    snap = h.snapshot("cost_per_usable_minute", cohort_id, command_id="k3eval")
    assert snap["excluded"]["cost_type_evaluation"] == "excluded_from_production"

    # 源条目里确实有 1,000,000 的评测成本 —— 它没被丢掉，只是按 cost_type
    # 排除在分子之外（basis=excluded，仍在分摊表里可查）。
    source_eval = sum(entry["amount_micro"] for entry in h.cost_entries()
                      if entry["cost_type"] == "evaluation")
    assert source_eval == 1_000_000
    row = h.conn.execute(
        "SELECT basis, amount_micro FROM cost_allocation WHERE tenant_id=?"
        " AND cost_entry_id='ce_eval'", (TENANT,)).fetchone()
    assert dict(row) == {"basis": "excluded", "amount_micro": 1_000_000}
    assert snap["numerator_value"] == 6_150_000

    # 换成 v2 也不变：v2 把评测成本改成 direct/episode，排除依据只能是
    # cost_type。分子仍是 6,150,000，不是 7,150,000。
    h.put_policy(version="v2", rules=V2_RULES)
    again = h.snapshot("cost_per_usable_minute", cohort_id, command_id="k3evalv2",
                       allocation_policy_version="v2")
    assert again["numerator_value"] == 6_150_000
    assert again["allocation_policy_version"] == "v2"
    assert again["excluded"]["cost_type_evaluation"] == "excluded_from_production"


def test_allocations_conserve_the_original_micro(h):
    """E38-S44-02 sum(cost_allocation) == sum(cost_entry)（整数 micro 精确）"""
    cohort_id = h.ready()
    h.snapshot("cost_per_usable_minute", cohort_id, command_id="cons")

    source_total = sum(entry["amount_micro"] for entry in h.cost_entries())
    rows = h.conn.execute(
        "SELECT cost_entry_id, amount_micro FROM cost_allocation"
        " WHERE tenant_id=?", (TENANT,)).fetchall()
    assert sum(row["amount_micro"] for row in rows) == source_total == 11_650_000

    # 逐条守恒，不只是总额相等（总额相等可能掩盖两条一多一少）。
    per_entry = {}
    for row in rows:
        per_entry[row["cost_entry_id"]] = \
            per_entry.get(row["cost_entry_id"], 0) + row["amount_micro"]
    for entry in h.cost_entries():
        assert per_entry[entry["cost_entry_id"]] == entry["amount_micro"]

    # 重跑同一快照不会重复记账：主键 (entry, policy, dimension, value)。
    h.snapshot("cost_per_usable_minute", cohort_id, command_id="cons2")
    again = h.conn.execute(
        "SELECT COUNT(*) FROM cost_allocation WHERE tenant_id=?",
        (TENANT,)).fetchone()[0]
    assert again == len(rows)


def test_policy_change_makes_a_new_snapshot_and_keeps_the_old(h):
    """E38-S44-01 S23 原始条目不变，改变 AllocationPolicy 版本 → 新快照，
    旧快照保留，两者可对比"""
    cohort_id = h.ready()
    v1 = h.snapshot("cost_per_usable_minute", cohort_id, command_id="v1snap")
    entries_before = h.cost_entries()

    h.put_policy(version="v2", rules=V2_RULES)
    v2 = h.snapshot("cost_per_usable_minute", cohort_id, command_id="v2snap",
                    allocation_policy_version="v2")

    # 两份快照都在，各自记着自己的策略版本 —— 旧的那份没有被覆盖。
    assert v1["snapshot_id"] != v2["snapshot_id"]
    assert h.service.get_snapshot(TENANT, v1["snapshot_id"])[
        "allocation_policy_version"] == "v1"
    assert h.service.get_snapshot(TENANT, v2["snapshot_id"])[
        "allocation_policy_version"] == "v2"
    # S23 的原始条目一个字节没变（S44 不写账本）。
    assert h.cost_entries() == entries_before

    # 两版策略下 K3 的分子相同（评测成本按 cost_type 排除），但分摊表里
    # ce_eval 的 basis 变了 —— 差异是可见、可解释的，不是黑箱。
    assert v1["numerator_value"] == v2["numerator_value"] == 6_150_000
    v1_basis = h.conn.execute(
        "SELECT basis FROM cost_allocation WHERE tenant_id=?"
        " AND policy_version='v1' AND cost_entry_id='ce_eval'",
        (TENANT,)).fetchone()[0]
    v2_basis = h.conn.execute(
        "SELECT basis FROM cost_allocation WHERE tenant_id=?"
        " AND policy_version='v2' AND cost_entry_id='ce_eval'",
        (TENANT,)).fetchone()[0]
    assert (v1_basis, v2_basis) == ("excluded", "direct")

    # 同版本换规则 → 409，不能悄悄改写让旧快照无法解释。
    with pytest.raises(ServiceError) as caught:
        h.put_policy(version="v2", rules=V1_RULES)
    assert (caught.value.status, caught.value.code) == (
        409, "ALLOCATION_POLICY_IMMUTABLE")


def test_k2_counts_abstain_in_denominator_but_not_numerator(h):
    """E25-S44-01 K2 的分母包含 abstain 的镜头，分子不计入 abstain"""
    cohort_id = h.ready()
    snap = h.snapshot("first_pass_shot_rate", cohort_id, command_id="k2")

    # 三个镜头：sh1 pass、sh2 abstain、sh3 fail。分母 3；分子只有 sh1。
    assert snap["n"] == 3
    assert snap["numerator_value"] == 1.0
    assert snap["denominator_value"] == 3.0
    assert snap["excluded"]["abstain_count"] == 1
    # 若 abstain 被当作 pass，分子会是 2；若被剔出分母，率会是 1/2。
    assert snap["value"] != pytest.approx(2 / 3, abs=1e-6)
    assert snap["value"] != pytest.approx(1 / 2, abs=1e-6)


def test_abstain_treatment_is_explicit_in_the_snapshot(h):
    """E25-S44-02 快照里 abstain_treated_as: "not_pass" 显式可见"""
    cohort_id = h.ready()
    snap = h.snapshot("first_pass_shot_rate", cohort_id, command_id="k2b")
    assert snap["excluded"]["abstain_treated_as"] == "not_pass"
    # 这个标记要一路进事件与导出，否则看板读者只能猜口径。
    event = [data for data in h.event_data(EVENT_SNAPSHOT)
             if data["metric_key"] == "first_pass_shot_rate"][0]
    assert event["excluded"]["abstain_treated_as"] == "not_pass"
    exported = h.service.export_snapshot(TENANT, snap["snapshot_id"])
    assert '"abstain_treated_as":"not_pass"' in exported["csv"]


def test_blind_review_below_five_items_gives_no_mean(h):
    """E25-S44-03 盲评 n<5 的维度标 insufficient_data，不给均值"""
    h.service.put_dataset(TENANT, dataset_body())
    review = h.service.create_blind_review(
        TENANT, "b1",
        {"dataset_id": "ds1", "dataset_version": 1,
         "reviewers": ["r1", "r2"], "vote_dimensions": ["visual", "audio"]},
        "h" * 64)
    # 4 个条目 × 2 位评审，只有一个维度有人投票。分数刻意各不相同：
    # 全打同一个分时 Krippendorff α 在数学上无定义（期望分歧为 0），那会
    # 让「样本不够」和「无法算一致性」混在一起，测不出边界。
    scores = {1: 5, 2: 4, 3: 3, 4: 2, 5: 1}
    h.service.vote(TENANT, "v1", review["blind_review_id"], {"votes": [
        {"reviewer_id": reviewer, "item_id": f"i{index}",
         "dimension": "visual", "score": scores[index]}
        for index in range(1, 5) for reviewer in ("r1", "r2")]}, "h" * 64)
    closed = h.service.close_blind_review(
        TENANT, "v2", review["blind_review_id"], {}, "h" * 64)

    visual = closed["dimensions"]["visual"]
    assert visual["status"] == "insufficient_data"
    assert visual["rated_items"] == 4
    assert visual["mean"] is None          # 4 条不给均值，不是 4.0
    assert closed["dimensions"]["audio"]["rated_items"] == 0
    assert closed["dimensions"]["audio"]["mean"] is None

    # 5 个条目时才有均值 —— 边界正是 5。
    five = h.service.create_blind_review(
        TENANT, "b2",
        {"dataset_id": "ds1", "dataset_version": 1,
         "reviewers": ["r1", "r2"], "vote_dimensions": ["visual"]},
        "h" * 64)
    h.service.vote(TENANT, "v3", five["blind_review_id"], {"votes": [
        {"reviewer_id": reviewer, "item_id": f"i{index}",
         "dimension": "visual", "score": scores[index]}
        for index in range(1, 6) for reviewer in ("r1", "r2")]}, "h" * 64)
    ok = h.service.close_blind_review(
        TENANT, "v4", five["blind_review_id"], {}, "h" * 64)
    assert ok["dimensions"]["visual"]["status"] == "ok"
    assert ok["dimensions"]["visual"]["mean"] == 3.0
    # 盲评结论永不写回 QC（§4.3）。
    assert ok["writes_back_to_qc"] is False


def test_rework_cost_lands_on_the_shot_across_runs(h):
    """E26-S44-01 跨 run 返工成本正确归集到同一镜头，不因 run 不同而分账"""
    cohort_id = h.ready()
    h.snapshot("cost_per_usable_minute", cohort_id, command_id="k5run")

    # ce_retry_a(run r1) 与 ce_retry_b(run r2) 都属 episode e1 / shot sh1。
    # 按 to_parent_episode 归到 e1，两笔相加，而不是按 run 拆成两行。
    rows = h.conn.execute(
        "SELECT dimension, dimension_value, amount_micro FROM cost_allocation"
        " WHERE tenant_id=? AND cost_entry_id IN ('ce_retry_a','ce_retry_b')",
        (TENANT,)).fetchall()
    assert len(rows) == 2
    assert {row["dimension"] for row in rows} == {"episode"}
    assert {row["dimension_value"] for row in rows} == {"e1"}
    assert sum(row["amount_micro"] for row in rows) == 150_000

    # 分摊维度里没有 run：以 run 分账会让「这镜头的返工成本」变成两笔。
    assert not any(row["dimension"] == "run" for row in h.conn.execute(
        "SELECT dimension FROM cost_allocation WHERE tenant_id=?", (TENANT,)))

    # 该集的全部精确分摊在一个地方：6,000,000 + 100,000 + 50,000。
    episode_total = h.conn.execute(
        "SELECT SUM(amount_micro) FROM cost_allocation WHERE tenant_id=?"
        " AND dimension='episode' AND dimension_value='e1'"
        " AND basis IN ('direct','to_parent_episode')", (TENANT,)).fetchone()[0]
    assert episode_total == 6_150_000


def test_rework_rate_counts_shots_not_occurrences(h):
    """E26-S44-02 返工率 K5 按镜头计，不按次计"""
    cohort_id = h.ready()
    snap = h.snapshot("rework_rate", cohort_id, command_id="k5")

    # 镜头：sh1(0 次返工)、sh2(2 次)、sh3(3 次) → 3 个镜头里 2 个返工过。
    shots = [shot for episode in h.episodes_data.values()
             for shot in episode["shots"]]
    assert len(shots) == 3
    assert sum(int(shot["rework_decisions"]) for shot in shots) == 5

    assert snap["n"] == 3                     # 按镜头
    assert snap["numerator_value"] == 2.0     # 返工过的镜头数，不是 5
    assert snap["value"] == pytest.approx(2 / 3, abs=1e-6)
    # 按次会得到 5/3 > 1，显然不是率。
    assert snap["value"] <= 1.0


def test_split_micro_conserves_on_ten_thousand_random_cases():
    """C-S44-01 分摊整数不变量在 10000 笔随机数据下成立"""
    rng = random.Random(44)
    for _ in range(10_000):
        total = rng.randrange(0, 10 ** 12)
        count = rng.randrange(1, 9)
        weights = [rng.random() for _ in range(count)]
        parts = split_micro(total, weights)
        assert len(parts) == count
        assert all(isinstance(part, int) for part in parts)
        # 这一条是全部意义：整数拆分不能漂移一分钱。
        assert sum(parts) == total

    # 全零权重不是「不发」，是按等份分（没有目标权重时的确定性兜底）。
    assert split_micro(1000, [0.0, 0.0]) == [500, 500]
    # 单项拿到全部余数。
    assert split_micro(7, [1.0]) == [7]


def test_unknown_basis_is_refused_not_zeroed():
    """C-S44-03 回滚：旧版本不认识新 basis → 拒绝分摊，不静默归零"""
    engine = AllocationEngine("v9", [{
        "match": {"cost_type": "generation"},
        "basis": "pro_rata_by_future_dimension",
        "dimension": "episode"}])
    with pytest.raises(ServiceError) as caught:
        engine.allocate({"cost_entry_id": "ce1", "amount_micro": 1000,
                         "cost_type": "generation",
                         "attribution": {"episode_id": "e1"}})
    assert (caught.value.status, caught.value.code) == (
        409, "ALLOCATION_BASIS_UNSUPPORTED")
    # 静默归零会让这个月的成本凭空消失 1000 micro —— 那是账本错误，不是降级。
    assert "pro_rata_by_future_dimension" in str(caught.value)

    # 写策略时也不接受未知 basis（409 只留给「已存策略里的旧规则」，入口是 422）。
    with pytest.raises(ServiceError) as refused:
        EvaluationAnalyticsService(
            sqlite3.connect(":memory:", isolation_level=None)
        ).put_allocation_policy(TENANT, {
            "policy_version": "v9", "rules": [{
                "match": {"cost_type": "generation"},
                "basis": "pro_rata_by_future_dimension",
                "dimension": "episode"}]})
    assert refused.value.status == 422


# ================================================= 幂等 · 事务 · 水位 · 租户

def test_same_command_returns_the_same_snapshot_id(h):
    """E12-S44-01 同 command 重投快照请求 → 返回同一 snapshot_id"""
    cohort_id = h.ready()
    payload = snapshot_body("rework_rate", cohort_id, "cmd-1")

    first = h.command(TENANT, payload,
                      lambda request_hash: h.service.compute_snapshot(
                          TENANT, "cmd-1", payload, request_hash))
    second = h.command(TENANT, payload,
                       lambda request_hash: h.service.compute_snapshot(
                           TENANT, "cmd-1", payload, request_hash))

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["replayed"] is False
    assert second["replayed"] is True        # 重放，没有重新计算
    assert h.count("metric_snapshot") == 1
    assert h.count("outbox") == 1             # 也没有再发一条事件

    # 同键不同 body：409，不是覆盖。两个 hash 都给调用方，它才能看出
    # 自己其实换了请求（AGENTS.md 的幂等键纪律）。
    with pytest.raises(CommandConflict) as caught:
        h.command(TENANT, snapshot_body("usable_rate", cohort_id, "cmd-1"),
                  lambda request_hash: pytest.fail("must not execute"))
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.stored_hash != caught.value.incoming_hash

    # 内核的天然键重放：换了 command_id、同一个 cohort/水位/策略，
    # 仍然返回同一个快照 —— 第二次没有产生第二个「同一份报告」。
    third_payload = snapshot_body("rework_rate", cohort_id, "cmd-2")
    third = h.service.compute_snapshot(TENANT, "cmd-2", third_payload, "e" * 64)
    assert third["snapshot_id"] == first["snapshot_id"]
    assert third["replayed"] is True
    assert h.count("metric_snapshot") == 1


def test_crash_mid_computation_leaves_no_half_written_snapshot(h):
    """E13-S44-01 计算中崩溃 → 无半写快照（事务）"""
    cohort_id = h.ready()
    real_emit = h.service._emit

    def explode(*_args, **_kwargs):
        raise RuntimeError("outbox is down")

    h.service._emit = explode
    try:
        with pytest.raises(RuntimeError):
            h.snapshot("rework_rate", cohort_id, command_id="crash")
    finally:
        h.service._emit = real_emit

    # 快照行与事件在同一事务里：事件写不进去，快照也不能留下。
    assert h.count("metric_snapshot") == 0
    assert h.count("outbox") == 0
    # 分摊也是事务的一部分（K3 会写 cost_allocation），同样没有半截。
    h.put_policy()
    h.service._emit = explode
    try:
        with pytest.raises(RuntimeError):
            h.snapshot("cost_per_usable_minute", cohort_id, command_id="crash2")
    finally:
        h.service._emit = real_emit
    assert h.count("metric_snapshot") == 0
    # 分摊是另一组投影：它在自己的一组事务里整体写入，崩溃只会留下
    # **完整**的一套行（可复用、可解释），不会留下半个金额。
    rows = h.conn.execute(
        "SELECT cost_entry_id, amount_micro FROM cost_allocation"
        " WHERE tenant_id=?", (TENANT,)).fetchall()
    totals = {}
    for row in rows:
        totals[row["cost_entry_id"]] = \
            totals.get(row["cost_entry_id"], 0) + row["amount_micro"]
    for entry in h.cost_entries():
        assert totals[entry["cost_entry_id"]] == entry["amount_micro"]

    # 崩溃后重投同一条命令能成功：占位被 abort 掉了，不会卡成 IN_FLIGHT。
    ok = h.snapshot("rework_rate", cohort_id, command_id="crash")
    assert ok["value"] is not None
    assert h.count("metric_snapshot") == 1


def test_outbox_redelivery_is_deduplicated_by_the_inbox(h):
    """E13-S44-02 outbox 重投 → 下游去重"""
    event = {"id": "led-1", "type": "finance.ledger.entry-posted",
             "tenant_id": TENANT, "sequence": 5, "data": {}}
    first = h.service.consume_event(TENANT, event)
    second = h.service.consume_event(TENANT, event)
    assert first == {"consumed": True, "duplicate": False, "event_id": "led-1",
                     "source": "s23", "watermark": 5}
    assert second["consumed"] is False and second["duplicate"] is True
    # 至少一次投递下，水位只被推进一次，也没有第二条 inbox 记录。
    assert h.service._observed_watermark(TENANT, "s23") == 5
    assert h.count("inbox") == 1

    # 乱序：小的 sequence 迟到，不许把水位往回拨。
    late = dict(event, id="led-2", sequence=2)
    h.service.consume_event(TENANT, late)
    assert h.service._observed_watermark(TENANT, "s23") == 5

    # 事件只推进水位与归属投影：S44 不写 S08/S23 的任何事实。
    h.service.consume_event(TENANT, {
        "id": "man-1", "type": "snapshot.manifest.committed",
        "tenant_id": TENANT, "sequence": 6,
        "data": {"project_id": "p1", "manifest_id": "m1", "node_id": "n1",
                 "episode_id": "e1", "accepted_revision": 2}})
    assert h.count("project_scope") == 1
    assert h.count("accepted_projection") == 1
    # 没有任何「接受指针」或「余额」表：这些仍归 S08/S23。
    assert not h.tables() & {"accepted_master", "ledger_entry", "balance",
                             "budget_reservation"}


def test_staleness_names_the_source_and_its_observed_watermark():
    """E36-S44-01 源 watermark 落后 → 响应带 staleness，工作台可显示"截至"时间"""
    harness = Harness()
    harness.seed_scope()
    # 冻结前 S23 已经到 9。这份 cohort 的成员集合冻结在「S23=9」的时刻。
    harness.service.consume_event(TENANT, {
        "id": "led-9", "type": "finance.ledger.entry-posted",
        "tenant_id": TENANT, "sequence": 9, "data": {}})
    cohort_id = harness.freeze()["cohort_id"]
    harness.put_policy()
    assert harness.service.get_cohort(TENANT, cohort_id)[
        "source_watermarks"]["s23"] == 9

    # 读面回到 s23=5：不是「没有问题」，是「比冻结时更旧」。
    snap = harness.snapshot("cost_per_usable_minute", cohort_id,
                            command_id="stale1")
    assert snap["stale_sources"] == {"s23": 5}
    assert snap["source_watermarks"]["s23"] == 5
    # 数字照算，但标了「截至」；不能把旧水位渲染成当前。
    assert snap["value"] is not None

    metrics = harness.service.get_metrics(TENANT, cohort_id=cohort_id)
    assert metrics["data_as_of"] == snap["computed_at"]
    assert set(metrics["staleness"]) >= {"s08", "s23"}
    assert metrics["staleness"]["s23"].endswith("s")
    # projection_watermark 是各源水位的最小值，不是最近一次事件。
    assert metrics["projection_watermark"] == min(
        metrics["source_watermarks"].values())

    # wait_seconds 会重读一次，但水位由上游决定：仍然是 5，不做乐观刷新。
    waited = harness.snapshot("cost_per_usable_minute", cohort_id,
                              command_id="stale2", wait_seconds=5)
    assert waited["stale_sources"] == {"s23": 5}
    assert harness.calls["cost_source"] >= 3

    # 上游回到冻结水位以上就不再标 stale。
    harness.watermarks["cost"] = 10
    caught_up = harness.snapshot("cost_per_usable_minute", cohort_id,
                                 command_id="stale3")
    assert caught_up["stale_sources"] == {}
    # 追赶后是新天然键：与 stale 的那份不是同一行，旧的仍然在。
    assert caught_up["snapshot_id"] != snap["snapshot_id"]


def test_older_watermarks_do_not_overwrite_newer_snapshots(h):
    """E36-S44-02 快照 attempt 带旧 watermark → 不覆盖新 watermark 的快照"""
    cohort_id = h.ready()
    fresh = h.snapshot("rework_rate", cohort_id, command_id="fresh")
    assert fresh["source_watermarks"]["s26"] == 7

    h.watermarks["job"] = 3
    with pytest.raises(ServiceError) as caught:
        h.snapshot("rework_rate", cohort_id, command_id="old")
    assert (caught.value.status, caught.value.code) == (409, "STALE_WATERMARK")
    assert caught.value.details["incoming"] == {"s26": 3}
    assert caught.value.details["stored"]["s26"] == 7

    # 快照没被改写：库里仍然只有 7 的那一份。
    assert h.count("metric_snapshot") == 1
    assert h.service.get_snapshot(TENANT, fresh["snapshot_id"])[
        "source_watermarks"]["s26"] == 7

    # 水位追平后是新快照（新天然键），旧的仍在 —— 不是覆盖。
    h.watermarks["job"] = 8
    newer = h.snapshot("rework_rate", cohort_id, command_id="newer")
    assert newer["snapshot_id"] != fresh["snapshot_id"]
    assert h.count("metric_snapshot") == 2


def test_cross_tenant_snapshots_and_cohorts_are_404():
    """E28-S44-01 跨租户查询快照/cohort → 404"""
    harness = Harness()
    own = harness.ready()
    harness.service.consume_event(OTHER, {
        "id": "p2-scope", "type": "snapshot.manifest.committed",
        "tenant_id": OTHER, "sequence": 1,
        "data": {"project_id": "p2", "manifest_id": "m2", "node_id": "n2",
                 "episode_id": "e9", "accepted_revision": 1}})
    other_cohort = harness.freeze(
        tenant=OTHER, command_id="oc",
        definition={"project_ids": ["p2"],
                    "time_window": {"from": "2026-01-01T00:00:00Z",
                                    "to": "2026-02-01T00:00:00Z"}})["cohort_id"]
    harness.put_policy(tenant=OTHER)
    other_snap = harness.snapshot("usable_rate", other_cohort,
                                  command_id="os", tenant=OTHER)

    # 两个租户互不可见；跨租户与不存在返回同一个 404（不泄漏存在性）。
    for call in (lambda: harness.service.get_snapshot(
                     TENANT, other_snap["snapshot_id"]),
                 lambda: harness.service.get_cohort(TENANT, other_cohort)):
        with pytest.raises(ServiceError) as caught:
            call()
        assert caught.value.status == 404
        assert caught.value.code in ("SNAPSHOT_NOT_FOUND", "COHORT_NOT_FOUND")

    own_snap = harness.snapshot("usable_rate", own, command_id="own")
    listed = harness.service.list_snapshots(TENANT)["snapshots"]
    assert [item["snapshot_id"] for item in listed] == [own_snap["snapshot_id"]]
    assert listed[0]["snapshot_id"] != other_snap["snapshot_id"]
    assert len(harness.service.list_snapshots(OTHER)["snapshots"]) == 1

    # 用别的租户的 cohort_id 查指标：空列表，不是别人的指标。
    leaked = harness.service.get_metrics(TENANT, cohort_id=other_cohort)
    assert leaked["metrics"] == []
    assert leaked["cohort_id"] == other_cohort


def test_frozen_member_missing_from_its_source_refuses_to_shrink_denominator(h):
    """§2.4：冻结后的成员在源里没有事实 → 503 INCOMPLETE_READ。

    「这一集没有事实」与「这一集不存在」看起来一样，但缩掉一个分母单位会
    让率偏高、成本偏低 —— 那正是「只报成功样本均值」。所以拒绝，不是跳过。
    """
    cohort_id = h.ready()
    kept = dict(h.episodes_data)
    h.episodes_data = {"e1": kept["e1"], "e3": kept["e3"]}   # e2 突然消失

    with pytest.raises(ServiceError) as caught:
        h.snapshot("usable_rate", cohort_id, command_id="missing")
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.details["upstream_code"] == "INCOMPLETE_READ"
    assert caught.value.details["missing_members"] == ["e2"]
    assert caught.value.retryable is True
    # 关键：没有写出任何快照，也没有发出事件。缩分母的版本一个数都不产生。
    assert h.count("metric_snapshot") == 0
    assert h.count("outbox") == 0
    assert not any(name.startswith("analytics_snapshot")
                   for name in h.service.metrics)

    # 源恢复后同一个 command_id 仍然能成功（失败不污染幂等记录）。
    h.episodes_data = kept
    snap = h.snapshot("usable_rate", cohort_id, command_id="missing")
    assert snap["n"] == 3
    assert h.count("metric_snapshot") == 1


def test_cohort_projects_must_belong_to_the_same_tenant(h):
    """E28-S44-02 cohort 定义中的 project_ids 必须属于同租户，否则拒绝"""
    # 归属证明只来自本租户消费过的 S08 事件。t2 的 p2 对 t1 不存在。
    h.service.consume_event(OTHER, {
        "id": "p2-scope", "type": "snapshot.manifest.committed",
        "tenant_id": OTHER, "sequence": 1,
        "data": {"project_id": "p2", "manifest_id": "m2", "node_id": "n2",
                 "episode_id": "e9", "accepted_revision": 1}})

    with pytest.raises(ServiceError) as caught:
        h.freeze(command_id="cross", definition={
            "project_ids": ["p1", "p2"],
            "time_window": {"from": "2026-01-01T00:00:00Z",
                            "to": "2026-02-01T00:00:00Z"}})
    assert (caught.value.status, caught.value.code) == (
        403, "PROJECT_NOT_IN_TENANT")
    # p1 也没有被「先证明过」：本租户没有消费过任何 S08 事件，两个
    # project_id 都不可信。拒绝的是**第一个**未经证明的归属，不是只挑 p2。
    assert caught.value.details["project_id"] == "p1"
    assert h.count("cohort") == 0

    # 归属是自己那条 S08 事件建立的，所以 p1 能过；p2 仍不行。
    h.seed_scope()
    own = h.freeze(command_id="own2")
    assert own["member_count"] == 3
    with pytest.raises(ServiceError):
        h.freeze(command_id="cross2", definition={
            "project_ids": ["p2"],
            "time_window": {"from": "2026-01-01T00:00:00Z",
                            "to": "2026-02-01T00:00:00Z"}})


def test_no_cross_tenant_aggregate_is_exposed(h):
    """E28-S44-03 聚合结果不带可识别他租户的信息（如"行业平均"若含跨租户数据
    需聚合到 k≥10）"""
    cohort_id = h.ready()
    h.snapshot("usable_rate", cohort_id, command_id="agg")

    # 结构上的答案：本服务**没有**跨租户聚合面。每条查询都带 tenant_id，
    # 返回的每个指标都能追到本租户的 cohort 与快照。
    metrics = h.service.get_metrics(TENANT, cohort_id=cohort_id)["metrics"]
    assert [item["cohort_id"] for item in metrics] == [cohort_id]
    for item in metrics:
        stored = h.conn.execute(
            "SELECT tenant_id FROM metric_snapshot WHERE snapshot_id=?",
            (item["snapshot_id"],)).fetchone()
        assert stored["tenant_id"] == TENANT

    # 没有「行业平均 / benchmark 对比 / 匿名基线」这类跨租户口径的代码路径，
    # 也没有 k 匿名阈值可以调错 —— 不存在比设错更好。
    source = (Path(__file__).resolve().parents[1] / "src" / "evaluation_analytics"
              / "service.py").read_text("utf-8")
    for forbidden in ("industry_average", "industry", "cross_tenant",
                      "peer_benchmark", "anonymized_baseline"):
        assert forbidden not in source
    # cohort 定义只能列 project/series/episode，不能列 tenant。
    definition = h.service.get_cohort(TENANT, cohort_id)["definition"]
    assert not {"tenant_ids", "tenants", "tenant_id"} & set(definition)
    assert {row[0] for row in h.conn.execute(
        "SELECT DISTINCT tenant_id FROM cohort")} == {TENANT}


def test_recompute_at_same_watermarks_is_field_identical(h):
    """E38-S44-03 同 cohort + 同 watermark 重算 → 结果逐字段相同"""
    cohort_id = h.ready()
    first = h.snapshot("cost_per_usable_minute", cohort_id, command_id="det1")
    second = h.snapshot("cost_per_usable_minute", cohort_id, command_id="det2")

    # 同天然键 → 同一个 snapshot_id，不是第二份「差不多」的报告。
    assert second["snapshot_id"] == first["snapshot_id"]
    assert {key: value for key, value in second.items()
            if key != "replayed"} == {
        key: value for key, value in first.items() if key != "replayed"}

    # 直接再调一次内核（不走幂等外壳）也躲不过 NONDETERMINISTIC 检查：
    # 天然键相同就必须给出同样的数，否则那是确定性 bug。
    payload = snapshot_body("cost_per_usable_minute", cohort_id, "det3")
    again = h.service.compute_snapshot(TENANT, "det3", payload, "f" * 64)
    assert again["replayed"] is True
    assert again["value"] == first["value"]

    # 不同水位是本服务唯一允许的「重算」理由：新快照、旧快照保留。
    h.watermarks["job"] = 8
    shifted = h.snapshot("cost_per_usable_minute", cohort_id, command_id="det4")
    assert shifted["snapshot_id"] != first["snapshot_id"]
    assert h.count("metric_snapshot") == 2


def test_metric_version_upgrade_keeps_old_snapshots_queryable(h):
    """C-S44-02 升级：新指标版本不破坏旧快照查询"""
    cohort_id = h.ready()
    old = h.snapshot("usable_rate", cohort_id, command_id="v1s", metric_version=1)
    assert old["superseded_definition"] is False

    h.service.register_metric_definition(TENANT, {
        "metric_key": "usable_rate", "version": 2,
        "ci_method": "wilson", "unit": "ratio",
        "numerator_spec": "v2 numerator", "denominator_spec": "v2 denominator"})
    new = h.snapshot("usable_rate", cohort_id, command_id="v2s", metric_version=2)

    # 旧快照照常可查，值与单位不变，只是标了「定义已被新版本取代」。
    still = h.service.get_snapshot(TENANT, old["snapshot_id"])
    assert still["metric_version"] == 1
    assert still["value"] == old["value"]
    assert still["superseded_definition"] is True
    assert h.service.get_snapshot(TENANT, new["snapshot_id"])[
        "superseded_definition"] is False

    # /metrics 每个指标只给最新一条，不把两个版本混成两条同名指标。
    metrics = h.service.get_metrics(TENANT, cohort_id=cohort_id)["metrics"]
    usable = [item for item in metrics if item["metric_key"] == "usable_rate"]
    assert len(usable) == 1
    assert usable[0]["metric_version"] == 2

    # 已登记的版本不可改写：口径变了就是新版本号。
    with pytest.raises(ServiceError) as caught:
        h.service.register_metric_definition(TENANT, {
            "metric_key": "usable_rate", "version": 2,
            "ci_method": "bootstrap_percentile", "unit": "ratio"})
    assert (caught.value.status, caught.value.code) == (
        409, "METRIC_DEFINITION_IMMUTABLE")


def test_csv_export_carries_enough_to_recompute(h):
    """C-S44-04 导出 CSV 口径头完整，可被第三方复算"""
    cohort_id = h.ready()
    snap = h.snapshot("cost_per_usable_minute", cohort_id, command_id="exp")
    exported = h.service.export_snapshot(TENANT, snap["snapshot_id"])

    lines = exported["csv"].strip().split("\n")
    header = lines[:len(lines) - 2]
    # 口径头：口径、窗口、冻结时刻、水位、策略版本、CI 方法与分子分母规格。
    joined = "\n".join(header)
    for required in ("metric_key=", "metric_version=", "unit=", "cohort_id=",
                     "cohort_frozen_at=", "time_window_from=",
                     "time_window_to=", "include_failed_runs=",
                     "source_watermarks=", "allocation_policy_version=",
                     "ci_method=", "numerator_spec=", "denominator_spec=",
                     "excluded=", "computed_at="):
        assert required in joined, required

    rows = list(csv.reader(io.StringIO("\n".join(lines[len(header):]))))
    assert rows[0] == list(SNAPSHOT_CSV_COLUMNS)
    assert len(rows) == 2
    assert len(rows[1]) == len(SNAPSHOT_CSV_COLUMNS) == 18
    cells = dict(zip(rows[0], rows[1]))
    # 复算者拿到了分子与分母，不只是 value。
    assert float(cells["numerator_value"]) == 6_150_000.0
    assert float(cells["denominator_value"]) == 10.0
    assert float(cells["value"]) == pytest.approx(
        6_150_000.0 / 1_000_000.0 / 10.0)
    assert cells["allocation_policy_version"] == "v1"

    # 空数据导出成空单元格 + note，不是 0。
    h.seed_scope()
    empty_cohort = h.freeze(command_id="empty", definition={
        "project_ids": ["p1"],
        "time_window": {"from": "2026-01-01T00:00:00Z",
                        "to": "2026-02-01T00:00:00Z"},
        "episode_ids": ["nope"]})["cohort_id"]
    empty = h.snapshot("usable_rate", empty_cohort, command_id="exp0")
    empty_csv = h.service.export_snapshot(TENANT, empty["snapshot_id"])["csv"]
    empty_cells = dict(zip(*list(csv.reader(
        io.StringIO("\n".join(empty_csv.strip().split("\n")[-2:]))))))
    assert empty_cells["value"] == ""
    assert empty_cells["note"] == "no data: value is null, not 0"

    # 其他格式不假装支持（不把 CSV 换个后缀当 xlsx）。
    with pytest.raises(ServiceError) as caught:
        h.service.export_snapshot(TENANT, snap["snapshot_id"], "xlsx")
    assert (caught.value.status, caught.value.code) == (
        422, "EXPORT_FORMAT_UNSUPPORTED")


# ======================================================== 评测运行器 · 数据集

def test_evaluation_run_fsm_marks_partial_and_failed_without_a_snapshot():
    """§4.6：PARTIAL 是「跑完了、部分条目失败」；全失败是 FAILED，不产快照。"""
    def partial(_tenant, _request):
        return {"item_succeeded": 4, "item_failed": 2,
                "result_ref": {"id": "res1"},
                "budget_ref": {"reservation_id": "br1"}}

    harness = Harness(evaluator_source=partial)
    harness.service.put_dataset(TENANT, dataset_body())
    run = harness.service.create_evaluation_run(TENANT, "p1", {
        "command_id": "p1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)

    assert run["state"] == "PARTIAL"
    assert run["partial"] is True          # 调用方必须知道圈了多少条
    assert (run["item_total"], run["item_succeeded"], run["item_failed"]) == (
        6, 4, 2)
    assert harness.service.metrics["analytics_eval_run_partial_total"] == 1
    assert harness.count("metric_snapshot") == 0
    assert [event["envelope"]["data"]["state"] for event in harness.events()
            if event["event_type"] == "analytics.evaluation.completed"] == [
        "PARTIAL"]

    def all_failed(_tenant, _request):
        return {"item_succeeded": 0, "item_failed": 6}

    failed = Harness(evaluator_source=all_failed)
    failed.service.put_dataset(TENANT, dataset_body())
    run = failed.service.create_evaluation_run(TENANT, "f1", {
        "command_id": "f1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    # 全部失败是 FAILED，不是 PARTIAL，也不产快照：半截结果不叫「部分成功」。
    assert run["state"] == "FAILED"
    assert run["partial"] is False
    assert failed.count("metric_snapshot") == 0

    # 评测器没报条数 → 500。缺一个计数不是 0（读模型最危险的换算）。
    def no_counts(_tenant, _request):
        return {"result_ref": {"id": "res1"}}

    broken = Harness(evaluator_source=no_counts)
    broken.service.put_dataset(TENANT, dataset_body())
    with pytest.raises(ServiceError) as caught:
        broken.service.create_evaluation_run(TENANT, "n1", {
            "command_id": "n1", "dataset_id": "ds1", "dataset_version": 1,
            "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (500, "SOURCE_INCONSISTENT")
    row = broken.conn.execute(
        "SELECT state FROM evaluation_run").fetchone()
    # 既没有假装成功，也没有发终态事件。
    assert row["state"] == "RUNNING"
    assert broken.count("outbox") == 0

    # 报的条数比数据集还多 → 500，并把三个数一起给出来。
    too_many = Harness(evaluator_source=lambda *_a: {
        "item_succeeded": 5, "item_failed": 4})
    too_many.service.put_dataset(TENANT, dataset_body())
    with pytest.raises(ServiceError) as caught:
        too_many.service.create_evaluation_run(TENANT, "t1", {
            "command_id": "t1", "dataset_id": "ds1", "dataset_version": 1,
            "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert caught.value.code == "SOURCE_INCONSISTENT"
    assert caught.value.details == {"item_total": 6, "succeeded": 5,
                                    "failed": 4}


def test_evaluation_start_is_deduplicated_to_one_paid_execution():
    """E12-S44-02 重复评测启动 → 不产生第二次付费（经 S26 幂等）

    三重防线的后两道在这里被逐条验证：
    1. 同 `(tenant, command_id)` 重投 → 同一条 run，评测器调用次数不增；
    2. 换 command_id 但同一 `(dataset, version, evaluator_ref)` 已有未终结的
       run（比如进程崩溃留下的 RUNNING）→ 返回那一条并标 `deduplicated`，
       **一次评测器都不调**。这是「第二次付费」真正会发生的那条路。
    """
    harness = Harness()
    harness.service.put_dataset(TENANT, dataset_body())
    # 前一次进程在 RUNNING 处崩掉：库里只剩这条占位，没有面试图重跑。
    harness.conn.execute(
        "INSERT INTO evaluation_run (tenant_id, evaluation_run_id, dataset_id,"
        " dataset_version, evaluator_ref, state, item_total, item_succeeded,"
        " item_failed, result_ref, budget_ref, command_id, request_hash,"
        " started_at, finished_at) VALUES (?, ?, ?, ?, ?, 'RUNNING', 6, 0, 0,"
        " NULL, NULL, ?, ?, ?, NULL)",
        (TENANT, "evr_old", "ds1", 1, '{"id":"ev"}', "old-command", "h" * 64,
         "2026-01-01T00:00:00Z"))

    out = harness.service.create_evaluation_run(TENANT, "retry-1", {
        "command_id": "retry-1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert out["evaluation_run_id"] == "evr_old"
    assert out["deduplicated"] is True
    assert out["replayed"] is True
    assert harness.calls["evaluator_source"] == 0     # 没有第二次付费执行
    assert harness.count("evaluation_run") == 1
    assert harness.count("outbox") == 0               # 也没有第二条完成事件

    # 同一 command_id 再投：同一条 run，仍然零次调用。
    again = harness.service.create_evaluation_run(TENANT, "retry-1", {
        "command_id": "retry-1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert again["evaluation_run_id"] == "evr_old"
    assert again["replayed"] is True
    assert harness.calls["evaluator_source"] == 0

    # 反向对照：**终结**的 run 不挡新命令，否则评测永远只能跑一次。
    done = Harness(evaluator_source=lambda *_a: {
        "item_succeeded": 6, "item_failed": 0})
    done.service.put_dataset(TENANT, dataset_body())
    first = done.service.create_evaluation_run(TENANT, "c1", {
        "command_id": "c1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    second = done.service.create_evaluation_run(TENANT, "c2", {
        "command_id": "c2", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert first["state"] == second["state"] == "SUCCEEDED"
    assert first["evaluation_run_id"] != second["evaluation_run_id"]
    assert done.calls["evaluator_source"] == 2


def test_evaluation_with_an_unreachable_evaluator_fails_and_invents_nothing():
    """§4.6：执行失败就是 FAILED，不产快照，也不把失败说成 PARTIAL。"""
    def unreachable(_tenant, _request):
        raise ConnectionError("gateway down")

    harness = Harness(evaluator_source=unreachable)
    harness.service.put_dataset(TENANT, dataset_body())
    with pytest.raises(ServiceError) as caught:
        harness.service.create_evaluation_run(TENANT, "b1", {
            "command_id": "b1", "dataset_id": "ds1", "dataset_version": 1,
            "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.details["gate"] == "evaluation.execute"
    assert caught.value.details["upstream_code"] == "UNREACHABLE"

    row = harness.conn.execute(
        "SELECT state, finished_at FROM evaluation_run").fetchone()
    assert row["state"] == "FAILED"
    assert row["finished_at"] is not None
    assert harness.count("metric_snapshot") == 0
    assert [event["envelope"]["data"]["state"] for event in harness.events()] == [
        "FAILED"]

    # 开关默认关：命令 503，且连一条占位 run 都不留（不是「排了队但没跑」）。
    off = Harness(enabled=False)
    off.service.put_dataset(TENANT, dataset_body())
    with pytest.raises(ServiceError) as caught:
        off.service.create_evaluation_run(TENANT, "off", {
            "command_id": "off", "dataset_id": "ds1", "dataset_version": 1,
            "evaluator_ref": {"id": "ev"}}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        503, "EVALUATION_EXECUTION_DISABLED")
    assert caught.value.retryable is False
    assert off.count("evaluation_run") == 0
    assert off.calls["evaluator_source"] == 0


def test_dataset_versions_are_immutable_and_items_must_reference_artifacts(h):
    """§1：评测条目必须指真实产物；同一版本一旦写入不可改写（可复现）。"""
    first = h.service.put_dataset(TENANT, dataset_body())
    assert first["version"] == 1 and first["item_count"] == 6
    assert [item["item_id"] for item in first["items"]] == [
        f"i{n}" for n in range(1, 7)]

    with pytest.raises(ServiceError) as caught:
        h.service.put_dataset(TENANT, dataset_body(command_id="d2"))
    assert (caught.value.status, caught.value.code) == (409, "DATASET_IMMUTABLE")

    def fresh(**over):
        body = {"command_id": "z", "dataset_id": "dsX", "version": 2,
                "name": "b", "purpose": "qc_calibration",
                "items": [{"item_id": "i1", "artifact_refs": [{"id": "a"}]}]}
        body.update(over)
        return body

    # 负数情形都必须落在**新版本**上，否则先撞 DATASET_IMMUTABLE。
    for over, expected in (
            ({"purpose": "marketing"}, "DATASET_PURPOSE_UNSUPPORTED"),
            ({"items": []}, "INVALID_BODY"),
            ({"items": [{"item_id": "i1", "artifact_refs": []}]}, "INVALID_BODY"),
            ({"items": [{"item_id": "i1", "artifact_refs": [{"id": "a"}],
                         "difficulty": "impossible"}]}, "INVALID_BODY"),
            ({"version": 0}, "INVALID_BODY")):
        with pytest.raises(ServiceError) as caught:
            h.service.put_dataset(TENANT, fresh(**over))
        assert (caught.value.status, caught.value.code) == (422, expected), over

    # 空引用被点名：调用方能知道是哪一条缺产物。
    with pytest.raises(ServiceError) as caught:
        h.service.put_dataset(TENANT, fresh(
            items=[{"item_id": "i9", "artifact_refs": []}]))
    assert caught.value.details == {"item_id": "i9"}
    assert h.count("benchmark_dataset") == 1     # 失败版本一条都没落库

    # 新版本并存，默认查询给最新一版，旧版仍可按版本号精确取。
    h.service.put_dataset(TENANT, {
        "command_id": "v2", "dataset_id": "ds1", "version": 2, "name": "b2",
        "purpose": "regression",
        "items": [{"item_id": "j1", "artifact_refs": [{"id": "a"}]}]})
    assert h.service.get_dataset(TENANT, "ds1")["version"] == 2
    assert h.service.get_dataset(TENANT, "ds1", 1)["item_count"] == 6

    with pytest.raises(ServiceError) as caught:
        h.service.get_dataset(TENANT, "ds1", 9)
    assert (caught.value.status, caught.value.code) == (404, "DATASET_NOT_FOUND")


# ============================================== 盲评 · 实验 · 投影 · ROI

def test_blind_review_is_blind_and_votes_are_scoped(h):
    """§4.3 第 1/2 条：响应真盲；抽样可复现；投票人/条目/维度都要校验。"""
    h.service.put_dataset(TENANT, dataset_body(items=6))
    review = h.service.create_blind_review(TENANT, "br1", {
        "command_id": "br1", "dataset_id": "ds1", "dataset_version": 1,
        "reviewers": ["r1", "r2"]}, "h" * 64)

    # 真盲：没有模型、prompt 版本、第几次尝试、是否返工。
    assert review["blind"] is True
    for forbidden in ("model_id", "prompt_revision", "attempt_no", "is_retry"):
        assert forbidden not in json.dumps(review, ensure_ascii=False), forbidden
    assert review["item_count"] == len(review["items"]) == 6
    assert set(review["items"][0]) == {"item_id", "artifact_refs"}

    # 抽样是输入的函数：同一个 blind_review_id 必然抽同一批（可复现），
    # 不同 review 的抽样集合不同——但都是原集合的子集。
    same = h.service.create_blind_review(TENANT, "br2", {
        "command_id": "br2", "dataset_id": "ds1", "dataset_version": 1,
        "reviewers": ["r1", "r2"]}, "h" * 64)
    assert [item["item_id"] for item in same["items"]] == [
        item["item_id"] for item in review["items"]]
    subset = h.service.create_blind_review(TENANT, "br3", {
        "command_id": "br3", "dataset_id": "ds1", "dataset_version": 1,
        "reviewers": ["r1", "r2"], "item_count": 3}, "h" * 64)
    assert len(subset["items"]) == 3
    assert {item["item_id"] for item in subset["items"]} <= {
        item["item_id"] for item in review["items"]}

    # 少于两位评审不叫盲评。
    with pytest.raises(ServiceError) as caught:
        h.service.create_blind_review(TENANT, "br4", {
            "command_id": "br4", "dataset_id": "ds1", "dataset_version": 1,
            "reviewers": ["r1"]}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (422, "INVALID_BODY")
    with pytest.raises(ServiceError) as caught:
        h.service.create_blind_review(TENANT, "br5", {
            "command_id": "br5", "dataset_id": "ds1", "dataset_version": 1,
            "reviewers": ["r1", "r2"], "vote_dimensions": ["smell"]}, "h" * 64)
    assert caught.value.code == "VOTE_DIMENSION_UNSUPPORTED"
    with pytest.raises(ServiceError) as caught:
        h.service.create_blind_review(TENANT, "br6", {
            "command_id": "br6", "dataset_id": "ds1", "dataset_version": 1,
            "reviewers": ["r1", "r2"], "item_count": 99}, "h" * 64)
    assert caught.value.details == {"available": 6}

    # 投票：不在会话里的评审 403；未抽中的条目 422；分数必须是 1..5 的整数
    # （bool 不是整数）。
    item = review["items"][0]["item_id"]
    open_review = h.service.create_blind_review(TENANT, "br7", {
        "command_id": "br7", "dataset_id": "ds1", "dataset_version": 1,
        "reviewers": ["a", "b"]}, "h" * 64)
    item = open_review["items"][0]["item_id"]
    for votes, status, code in (
            ([{"reviewer_id": "zz", "item_id": item, "dimension": "visual",
               "score": 3}], 403, "REVIEWER_NOT_IN_SESSION"),
            ([{"reviewer_id": "a", "item_id": "nope", "dimension": "visual",
               "score": 3}], 422, "ITEM_NOT_IN_SESSION"),
            ([{"reviewer_id": "a", "item_id": item, "dimension": "smell",
               "score": 3}], 422, "VOTE_DIMENSION_UNSUPPORTED"),
            ([{"reviewer_id": "a", "item_id": item, "dimension": "visual",
               "score": 0}], 422, "INVALID_BODY"),
            ([{"reviewer_id": "a", "item_id": item, "dimension": "visual",
               "score": True}], 422, "INVALID_BODY")):
        with pytest.raises(ServiceError) as caught:
            h.service.vote(TENANT, "v", open_review["blind_review_id"],
                           {"command_id": "v", "votes": votes}, "h" * 64)
        assert (caught.value.status, caught.value.code) == (status, code), votes

    # 同一 (reviewer,item,dimension) 覆盖而不新增行（§7 的 upsert）。
    for score in (3, 4):
        h.service.vote(TENANT, f"v{score}", open_review["blind_review_id"], {
            "command_id": f"v{score}",
            "votes": [{"reviewer_id": "a", "item_id": item,
                       "dimension": "visual", "score": score}]}, "h" * 64)
    rows = h.conn.execute(
        "SELECT score FROM blind_review_vote WHERE blind_review_id=?"
        " AND reviewer_id='a' AND item_id=? AND dimension='visual'",
        (open_review["blind_review_id"], item)).fetchall()
    assert [row["score"] for row in rows] == [4]


def test_blind_review_low_alpha_is_reported_and_never_writes_back(h):
    """§4.3 第 2/4 条：alpha 低于阈值要标出来；S44 不据此改 QC。"""
    h.service.put_dataset(TENANT, dataset_body(items=6))
    review = h.service.create_blind_review(TENANT, "br", {
        "command_id": "br", "dataset_id": "ds1", "dataset_version": 1,
        "reviewers": ["a", "b"]}, "h" * 64)
    votes = []
    for index, item in enumerate(review["items"]):
        # 两位评审在每一件上都打反：alpha = -3.0（低于 0.667）。
        votes.append({"reviewer_id": "a", "item_id": item["item_id"],
                      "dimension": "visual", "score": 5 if index % 2 else 1})
        votes.append({"reviewer_id": "b", "item_id": item["item_id"],
                      "dimension": "visual", "score": 1 if index % 2 else 5})
    h.service.vote(TENANT, "v1", review["blind_review_id"], {
        "command_id": "v1", "votes": votes}, "h" * 64)
    closed = h.service.close_blind_review(
        TENANT, "cl1", review["blind_review_id"], {}, "h" * 64)

    assert closed["state"] == "CLOSED"
    assert closed["agreement_alpha"] == pytest.approx(-3.0)
    assert closed["low_agreement"] is True
    assert closed["writes_back_to_qc"] is False
    assert closed["dimensions"]["visual"]["status"] == "low_agreement"
    assert closed["dimensions"]["visual"]["mean"] == pytest.approx(3.0)
    # 没投过的维度是 insufficient_data 且 mean 为 None —— 不是 0。
    for dimension in ("audio", "lipsync", "story"):
        assert closed["dimensions"][dimension]["status"] == "insufficient_data"
        assert closed["dimensions"][dimension]["mean"] is None
    assert h.service.metrics["analytics_blind_review_low_alpha_total"] == 1

    # 关评后投票 409；重复关评是重放，不重算、不再计数。
    with pytest.raises(ServiceError) as caught:
        h.service.vote(TENANT, "v2", review["blind_review_id"], {
            "command_id": "v2", "votes": votes[:1]}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        409, "BLIND_REVIEW_CLOSED")
    replay = h.service.close_blind_review(
        TENANT, "cl2", review["blind_review_id"], {}, "h" * 64)
    assert replay["replayed"] is True
    assert replay["agreement_alpha"] == closed["agreement_alpha"]
    assert h.service.metrics["analytics_blind_review_low_alpha_total"] == 1


def test_experiment_assignment_is_deterministic_and_stop_is_idempotent(h):
    """§4.4：同 subject 永远同臂；止损保留数据；开始/停止都可重入。"""
    experiment = h.service.create_experiment(
        TENANT, "x1", experiment_body(), "h" * 64)
    experiment_id = experiment["experiment_id"]
    assert experiment["state"] == "DRAFT"
    assert experiment["assignment_rule"] == {
        "kind": "hash_modulo", "field": "subject_id", "salt": "seed-prompt-ab"}
    # 计划指标必须显式列出并排序（可解释）。
    assert experiment["planned_metrics"] == ["usable_rate",
                                             "first_pass_shot_rate"]

    # DRAFT 期间没有分配（分配只存在于 RUNNING 的实验）。
    with pytest.raises(ServiceError) as caught:
        h.service.assign(TENANT, experiment_id, "ep-1")
    assert (caught.value.status, caught.value.code) == (
        409, "EXPERIMENT_NOT_RUNNING")

    started = h.service.start_experiment(TENANT, "s1", experiment_id, {},
                                         "h" * 64)
    assert started["state"] == "RUNNING"
    restarted = h.service.start_experiment(TENANT, "s2", experiment_id, {},
                                          "h" * 64)
    assert restarted["started_at"] == started["started_at"]   # 不重写时间戳

    # 确定性：同一 subject 两次给同一臂、第二次标 replayed、只落一行。
    first = h.service.assign(TENANT, experiment_id, "ep-1")
    second = h.service.assign(TENANT, experiment_id, "ep-1")
    assert first["arm"] == second["arm"]
    assert (first["replayed"], second["replayed"]) == (False, True)
    assert h.count("experiment_assignment") == 1

    # 60 个不同 subject 会落进两个臂（不是全挤在一个臂里），且总数守恒。
    for index in range(60):
        h.service.assign(TENANT, experiment_id, f"sub-{index}")
    results = h.service.experiment_results(TENANT, experiment_id)
    assert set(results["arm_counts"]) <= {"a", "b"}
    assert sum(results["arm_counts"].values()) == 61
    assert min(results["arm_counts"].values()) >= 20
    assert results["planned_metrics"] == ["first_pass_shot_rate",
                                         "usable_rate"]
    # 未计划的指标没有快照可挂：显式空 + 说明，不是编一个数。
    assert results["exploratory"] == {}
    assert "cohort-scoped" in results["note"]

    stopped = h.service.stop_experiment(TENANT, "sp1", experiment_id,
                                        {"state": "COMPLETED"}, "h" * 64)
    assert stopped["state"] == "COMPLETED"
    again = h.service.stop_experiment(TENANT, "sp2", experiment_id,
                                      {"state": "STOPPED"}, "h" * 64)
    assert again["state"] == "COMPLETED"          # 已停过不改成 STOPPED
    assert again["stopped_at"] == stopped["stopped_at"]
    assert h.count("experiment_assignment") == 61  # 止损不清数据

    with pytest.raises(ServiceError) as caught:
        h.service.start_experiment(TENANT, "s3", experiment_id, {}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        409, "EXPERIMENT_NOT_DRAFT")

    # 非法定义一律 422，且一条实验都不落库。
    for body, code in (
            ({"name": "x", "arms": [{"name": "a", "config_ref": {}}],
              "planned_metrics": ["usable_rate"]}, "INVALID_BODY"),
            ({"name": "x",
              "arms": [{"name": "a", "config_ref": {}},
                       {"name": "a", "config_ref": {}}],
              "planned_metrics": ["usable_rate"]}, "INVALID_BODY"),
            ({"name": "x",
              "arms": [{"name": "a", "config_ref": {}},
                       {"name": "b", "config_ref": {}}],
              "planned_metrics": []}, "INVALID_BODY"),
            ({"name": "x",
              "arms": [{"name": "a", "config_ref": {}},
                       {"name": "b", "config_ref": {}}],
              "planned_metrics": ["nps"]}, "METRIC_KEY_UNSUPPORTED"),
            ({"name": "x",
              "arms": [{"name": "a", "config_ref": {}},
                       {"name": "b", "config_ref": {}}],
              "planned_metrics": ["usable_rate"],
              "assignment_rule": {"kind": "random"}},
             "ASSIGNMENT_RULE_UNSUPPORTED")):
        with pytest.raises(ServiceError) as caught:
            h.service.create_experiment(TENANT, "bad",
                                        {"command_id": "bad", **body}, "h" * 64)
        assert (caught.value.status, caught.value.code) == (422, code), body
    assert h.count("experiment") == 1


def test_projection_has_a_ci_and_is_never_used_for_admission_hint(h):
    """§4.5：投影 = 实际 + 剩余天数 × p50 ± CI；窗口必须标；**不用于放行**。"""
    cohort_id = h.ready()
    projection = h.service.compute_projection(TENANT, "pj1", {
        "command_id": "pj1", "window": "30d", "scope": {"project_id": "p1"},
        "cohort_id": cohort_id}, "h" * 64)

    # 默认成本源 11,650,000 micro，3 天日额 {6.1M, 4.5M, 1.05M}：
    # p50 = 4.5M，remaining = 30 - 3 = 27。
    assert projection["actual_to_date_micro"] == 11_650_000
    assert projection["based_on_rate"] == pytest.approx(4_500_000.0)
    assert projection["projected_total_micro"] == 11_650_000 + 27 * 4_500_000
    assert projection["ci_low_micro"] == 40_000_000
    assert projection["ci_high_micro"] == 176_350_000
    assert projection["ci_low_micro"] <= projection["projected_total_micro"] \
        <= projection["ci_high_micro"]
    assert projection["used_for_admission"] is False
    assert projection["window"] == "30d"

    event = h.event_data("analytics.cost.projection.updated")[-1]
    assert event["projection_id"] == projection["projection_id"]
    # 事件里没有 `used_for_admission` 这个键：放行提示只存在于响应里，
    # 下游不该从一个事件字段里读出「可以按这个数放行」。
    assert "used_for_admission" not in event

    # 投影不是幂等对象：每次计算是新投影（与快照的天然键不同）。
    second = h.service.compute_projection(TENANT, "pj2", {
        "command_id": "pj2", "window": "30d", "scope": {"project_id": "p1"},
        "cohort_id": cohort_id}, "h" * 64)
    assert second["projection_id"] != projection["projection_id"]
    assert second["projected_total_micro"] == projection["projected_total_micro"]

    # 窗口必须显式给，且只接受 7d/30d —— 7d 与 30d 用不同的日额集合。
    explicit = Harness(cost_source=lambda _t, _q: {
        "entries": [
            {"cost_entry_id": "a", "amount_micro": 3_000_000,
             "cost_type": "generation",
             "occurred_at": _days_ago(1),
             "attribution": {"episode_id": "e1"}},
            {"cost_entry_id": "b", "amount_micro": 6_000_000,
             "cost_type": "generation",
             "occurred_at": _days_ago(20),
             "attribution": {"episode_id": "e1"}}],
        "watermark": 5})
    cohort2 = explicit.ready()
    short = explicit.service.compute_projection(TENANT, "w7", {
        "command_id": "w7", "window": "7d", "scope": {"project_id": "p1"},
        "cohort_id": cohort2}, "h" * 64)
    long = explicit.service.compute_projection(TENANT, "w30", {
        "command_id": "w30", "window": "30d", "scope": {"project_id": "p1"},
        "cohort_id": cohort2}, "h" * 64)
    # 同一批账，7d 只看见第 1 天，30d 看见两天：率 3M vs 4.5M。
    assert short["based_on_rate"] == pytest.approx(3_000_000.0)
    assert long["based_on_rate"] == pytest.approx(4_500_000.0)
    assert short["projected_total_micro"] == 9_000_000 + 29 * 3_000_000
    assert long["projected_total_micro"] == 9_000_000 + 28 * 4_500_000

    for body in (
            {"window": "90d", "scope": {"project_id": "p1"},
             "cohort_id": cohort_id},
            {"window": "7d", "scope": {}, "cohort_id": cohort_id},
            {"window": "7d", "scope": {"project_id": "p1"}},
            {"window": "7d", "scope": {"project_id": "p1"},
             "cohort_id": "nope"}):
        with pytest.raises(ServiceError) as caught:
            h.service.compute_projection(TENANT, "bad",
                                         {"command_id": "bad", **body},
                                         "h" * 64)
        assert caught.value.status in (404, 422), body
    with pytest.raises(ServiceError) as caught:
        h.service.compute_projection(TENANT, "bad", {
            "command_id": "bad", "window": "90d",
            "scope": {"project_id": "p1"}, "cohort_id": cohort_id}, "h" * 64)
    assert caught.value.code == "PROJECTION_WINDOW_UNSUPPORTED"
    assert caught.value.details["supported"] == ["7d", "30d"]


def test_projection_refuses_to_extrapolate_from_an_empty_rate():
    """§2.4：窗口里没有带日期的账 → 503，不是「按 0 元/天推到底」。"""
    # 有金额、但没有日期：金额能加总，日额却无从谈起。
    undated = Harness(cost_source=lambda _t, _q: {
        "entries": [{"cost_entry_id": "a", "amount_micro": 9_000_000,
                     "cost_type": "generation",
                     "attribution": {"episode_id": "e1"}}],
        "watermark": 5})
    cohort_id = undated.ready()
    with pytest.raises(ServiceError) as caught:
        undated.service.compute_projection(TENANT, "undated", {
            "command_id": "undated", "window": "7d",
            "scope": {"project_id": "p1"}, "cohort_id": cohort_id}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.details["upstream_code"] == "NO_DATED_ENTRIES"
    assert caught.value.retryable is True
    # 没有投影行、没有事件：不编一个「截至今天花了 0」的结论。
    assert undated.count("cost_projection") == 0
    assert undated.count("outbox") == 0

    # 上游完全没有条目走同一条路（空列表不是「0 元/天」）。
    blank = Harness(cost_source=lambda _t, _q: {"entries": [], "watermark": 5})
    cohort2 = blank.ready()
    with pytest.raises(ServiceError) as caught:
        blank.service.compute_projection(TENANT, "blank", {
            "command_id": "blank", "window": "30d",
            "scope": {"tenant": TENANT}, "cohort_id": cohort2}, "h" * 64)
    assert caught.value.details["upstream_code"] == "NO_DATED_ENTRIES"
    assert blank.count("cost_projection") == 0


def test_roi_requires_external_revenue_and_a_real_allocation(h):
    """K8：收入是外部输入（必须显式给），成本必须来自已分摊的 K3 快照。"""
    cohort_id = h.ready()

    # 没有 K3 分摊 → 503 NO_ALLOCATION，不是「成本 0 → 毛利 100%」。
    with pytest.raises(ServiceError) as caught:
        h.service.compute_roi(TENANT, "r0", {
            "command_id": "r0", "cohort_id": cohort_id,
            "revenue_micro": 20_000_000,
            "contract_ref": {"id": "ct1"}}, "h" * 64)
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.details["upstream_code"] == "NO_ALLOCATION"
    assert h.count("customer_roi") == 0

    # 收入/合同引用缺一不可：没有它们就没有 ROI 的分子。
    for body in ({"cohort_id": cohort_id, "revenue_micro": 0,
                  "contract_ref": {"id": "ct1"}},
                 {"cohort_id": cohort_id, "contract_ref": {"id": "ct1"}},
                 {"cohort_id": cohort_id, "revenue_micro": 100}):
        with pytest.raises(ServiceError) as caught:
            h.service.compute_roi(TENANT, "bad",
                                  {"command_id": "bad", **body}, "h" * 64)
        assert (caught.value.status, caught.value.code) == (
            422, "REVENUE_REQUIRED"), body

    h.snapshot("cost_per_usable_minute", cohort_id, command_id="k3")
    roi = h.service.compute_roi(TENANT, "r1", {
        "command_id": "r1", "cohort_id": cohort_id,
        "revenue_micro": 20_000_000, "contract_ref": {"id": "ct1"}}, "h" * 64)
    assert roi["revenue_source"] == "external_input"
    assert roi["allocated_cost_micro"] == 10_150_000
    assert roi["gross_margin"] == pytest.approx(0.4925)
    # ROI 是 K8 快照的一种：分母口径与成本分摊一起被记下，而不是只留一个比率。
    assert roi["snapshot"]["excluded"]["allocated_cost_micro"] == 10_150_000
    assert roi["snapshot"]["excluded"]["contract_revenue_is_external_input"] is True
    stored = h.conn.execute("SELECT * FROM customer_roi").fetchone()
    assert (stored["revenue_micro"], stored["allocated_cost_micro"],
            stored["gross_margin"]) == (20_000_000, 10_150_000, 0.4925)


# ============================================================= HTTP 层

def _days_ago(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(tmp_path, monkeypatch, *, enabled=False):
    """按 S43/S39 的做法加载 `api/app.py`：**不** `import app`。

    模块级的 `_conn` / `service` / `_idem` 在 import 时已经建好（读的是真实
    环境变量、走的是真实 `build_*(None)` 桩）。这里把三个都换成 Harness 的
    闭包实现，才能在不联网的前提下走完整 HTTP 路径。
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("S44_DB", str(tmp_path / "s44.sqlite"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EVALUATION_EXECUTION_ENABLED", "false")
    for name in ("S23_BASE_URL", "S35_BASE_URL", "S36_BASE_URL",
                 "S37_BASE_URL", "S40_BASE_URL", "S43_BASE_URL",
                 "S26_BASE_URL", "S08_BASE_URL", "EVALUATOR_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location("s44_app_under_test",
                                                  APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    harness = Harness(enabled=enabled)
    module.service = harness.service
    module._idem = harness._idem
    module._conn = harness.conn
    return TestClient(module.app), module, harness


HTTP = {"Idempotency-Key": "c1", "X-Tenant-Id": TENANT}


def test_http_command_surface_replays_and_reports_conflicts(tmp_path,
                                                            monkeypatch):
    """命令面：201 + 重放 + 同键异 body 409；租户/命令头只用于比对。"""
    client, module, harness = _client(tmp_path, monkeypatch)
    harness.seed_scope()

    created = client.post("/cohorts", json=cohort_body("c1"), headers=HTTP)
    assert created.status_code == 201, created.text
    cohort_id = created.json()["cohort_id"]
    assert created.json()["replayed"] is False
    again = client.post("/cohorts", json=cohort_body("c1"), headers=HTTP)
    assert again.status_code == 201
    assert again.json()["replayed"] is True
    assert again.json()["cohort_id"] == cohort_id

    # 同一 Idempotency-Key、不同 body → 409，两个 hash 都给，绝不覆盖。
    conflict = client.post("/cohorts", json=cohort_body("c1", definition={
        "project_ids": ["p1"],
        "time_window": {"from": "2026-01-01T00:00:00Z",
                        "to": "2026-03-01T00:00:00Z"}}), headers=HTTP)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    details = conflict.json()["details"]
    assert details["stored_request_hash"] != details["incoming_request_hash"]
    assert len(details["stored_request_hash"]) == 64

    # 请求体的 tenant_id 只是被比对的输入：不一致 403，不是「以 body 为准」。
    mismatch = client.post("/cohorts", json={**cohort_body("c2"),
                                             "tenant_id": OTHER},
                           headers={"Idempotency-Key": "c2",
                                    "X-Tenant-Id": TENANT})
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "TENANT_MISMATCH"
    nested = client.post("/cohorts", json={**cohort_body("c3"),
                                           "context": {"tenant_id": OTHER}},
                         headers={"Idempotency-Key": "c3",
                                  "X-Tenant-Id": TENANT})
    assert (nested.status_code, nested.json()["code"]) == (
        403, "TENANT_MISMATCH")

    # 头与 body 不一致、缺 key、缺租户都是 400（不是「以 header 为准」）。
    for payload, headers, label in (
            ({**cohort_body("c4"), "command_id": "other"},
             {"Idempotency-Key": "c4", "X-Tenant-Id": TENANT}, "key mismatch"),
            (cohort_body("c5"), {"X-Tenant-Id": TENANT}, "no key"),
            (cohort_body("c6"), {"Idempotency-Key": "c6"}, "no tenant")):
        response = client.post("/cohorts", json=payload, headers=headers)
        assert (response.status_code, response.json()["code"]) == (
            400, "INVALID_BODY"), label

    # 快照：200 + /metrics 扩展形状 + 导出 18 列 + 不支持格式 422。
    harness.put_policy()
    snapshot = client.post("/metric-snapshots",
                           json=snapshot_body("usable_rate", cohort_id, "k1"),
                           headers={"Idempotency-Key": "k1",
                                    "X-Tenant-Id": TENANT})
    assert snapshot.status_code == 200, snapshot.text
    snapshot_id = snapshot.json()["snapshot_id"]

    metrics = client.get(f"/metrics?cohort_id={cohort_id}",
                         headers={"X-Tenant-Id": TENANT})
    assert metrics.status_code == 200
    body = metrics.json()
    assert set(body) == {"series_id", "project_id", "cohort_id",
                         "projection_watermark", "data_as_of", "staleness",
                         "metrics", "mixed_versions",
                         "allocation_policy_version", "source_watermarks"}
    assert body["data_as_of"] == snapshot.json()["computed_at"]
    assert body["metrics"][0]["metric_key"] == "usable_rate"

    # 没有匹配的 cohort 时 404，而不是一条「空指标」。
    missing = client.get("/metrics?project_id=zzz",
                         headers={"X-Tenant-Id": TENANT})
    assert (missing.status_code, missing.json()["code"]) == (
        404, "COHORT_NOT_FOUND")

    exported = client.get(f"/metric-snapshots/{snapshot_id}/export",
                          headers={"X-Tenant-Id": TENANT})
    assert exported.status_code == 200
    assert exported.json()["format"] == "csv"
    assert len(exported.json()["columns"]) == len(SNAPSHOT_CSV_COLUMNS) == 18
    assert exported.json()["csv"].split("\n")[-2].startswith(snapshot_id)
    bad = client.get(f"/metric-snapshots/{snapshot_id}/export?format=xlsx",
                     headers={"X-Tenant-Id": TENANT})
    assert (bad.status_code, bad.json()["code"]) == (
        422, "EXPORT_FORMAT_UNSUPPORTED")

    listed = client.get("/metric-snapshots",
                        headers={"X-Tenant-Id": TENANT})
    assert listed.status_code == 200
    assert [item["snapshot_id"] for item in listed.json()["snapshots"]] == [
        snapshot_id]

    # 跨租户与不存在同为 404（不泄漏存在性）。
    foreign = client.get(f"/metric-snapshots/{snapshot_id}",
                         headers={"X-Tenant-Id": OTHER})
    assert foreign.status_code == 404


def test_http_evaluation_execution_is_off_by_default(tmp_path, monkeypatch):
    """AGENTS.md 的停止条件：付费评测默认关闭，HTTP 里没有「假装跑过」的退路。

    这是模块文档里点名的用例：注入 `enabled=True` 只发生在测试桩上；
    生产默认（env 缺失）必须是 503，且**没有**任何降级实现。
    """
    client, module, harness = _client(tmp_path, monkeypatch, enabled=False)
    assert module.service.evaluation_execution_enabled is False
    assert os.environ.get("EVALUATION_EXECUTION_ENABLED") == "false"

    harness.service.put_dataset(TENANT, dataset_body())
    response = client.post("/evaluation-runs", json={
        "command_id": "ev1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}},
        headers={"Idempotency-Key": "ev1", "X-Tenant-Id": TENANT})
    assert response.status_code == 503
    assert response.json()["code"] == "EVALUATION_EXECUTION_DISABLED"
    assert response.json()["retryable"] is False
    assert harness.count("evaluation_run") == 0
    assert harness.calls["evaluator_source"] == 0

    # 巡检如实说这个开关是关的（别让编排层以为「评测可用」）。
    readiness = client.get("/health/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["evaluation_execution_enabled"] is False

    # 开关打开时（这里显式注入桩）201 且真的调了评测器一次。
    enabled_client, _module, enabled_harness = _client(
        tmp_path / "enabled", monkeypatch, enabled=True)
    enabled_harness.service.put_dataset(TENANT, dataset_body())
    allowed = enabled_client.post("/evaluation-runs", json={
        "command_id": "ev2", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}},
        headers={"Idempotency-Key": "ev2", "X-Tenant-Id": TENANT})
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["state"] == "SUCCEEDED"
    assert enabled_harness.calls["evaluator_source"] == 1


def test_http_context_validation_and_event_consumption(tmp_path, monkeypatch):
    """执行上下文按契约校验；事件入口只推水位，租户不一致 403。"""
    client, module, harness = _client(tmp_path, monkeypatch)
    harness.seed_scope()

    # 缺 node_epoch 的上下文 → 422，并点出缺了哪个键（不是笼统的 400）。
    broken = valid_context()
    broken.pop("node_epoch")
    rejected = client.post("/cohorts", json={
        **cohort_body("c9"), "execution_context": broken},
        headers={"Idempotency-Key": "c9", "X-Tenant-Id": TENANT})
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_EXECUTION_CONTEXT"
    assert rejected.json()["details"]["missing"] == ["node_epoch"]

    # 完整上下文 → 201。
    accepted = client.post("/cohorts", json={
        **cohort_body("c10"), "execution_context": valid_context()},
        headers={"Idempotency-Key": "c10", "X-Tenant-Id": TENANT})
    assert accepted.status_code == 201, accepted.text

    # 事件：首次消费推进水位，重投去重，未知类型 422，跨租户 403。
    envelope = {"id": "led-1", "type": "finance.ledger.entry-posted",
                "tenant_id": TENANT, "sequence": 4, "data": {}}
    first = client.post("/events:consume", json={"event": envelope},
                        headers={"X-Tenant-Id": TENANT})
    assert first.status_code == 200
    assert first.json() == {"consumed": True, "duplicate": False,
                            "event_id": "led-1", "source": "s23",
                            "watermark": 4}
    duplicate = client.post("/events:consume", json={"event": envelope},
                            headers={"X-Tenant-Id": TENANT})
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    # 两条不同事件（seed_scope 的归属事件 + 这条账本事件）各一行，重投不增。
    assert harness.count("inbox") == 2
    unknown = client.post("/events:consume", json={"event": {
        **envelope, "id": "x1", "type": "nope.event"}},
        headers={"X-Tenant-Id": TENANT})
    assert (unknown.status_code, unknown.json()["code"]) == (
        422, "UNKNOWN_EVENT")
    foreign = client.post("/events:consume", json={"event": {
        **envelope, "id": "x2", "tenant_id": OTHER}},
        headers={"X-Tenant-Id": TENANT})
    assert (foreign.status_code, foreign.json()["code"]) == (
        403, "TENANT_MISMATCH")
    # 事件只推进水位与归属投影：没有落任何生产事实。
    assert harness.count("project_scope") == 1
    assert not harness.tables() & {"accepted_master", "ledger_entry",
                                   "budget_reservation"}


# ==================================================== 结构断言 · 跨服务边界

def _source_texts():
    """本服务四个手写模块的全文（用于结构断言）。"""
    files = sorted(PKG_SRC.glob("*.py")) + [APP_PATH]
    return {path: path.read_text("utf-8") for path in files}


def _docstring_nodes(tree):
    """模块/类/函数的 docstring 表达式节点（要按「不是代码」排除）。"""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _code_tokens(text):
    """代码里出现的标识符与字符串（docstring 不算）。"""
    tree = ast.parse(text)
    skip = _docstring_nodes(tree)
    tokens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                tokens.append(node.value)
    return tokens, tree


def test_owned_tables_are_exactly_the_declared_set(h):
    """§5：DDL 只建本服务自己的表；多一张就是「第二个权威」的苗头。"""
    actual = h.tables() - {"idempotency_record", "sqlite_sequence"}
    assert actual == OWNED_TABLES
    assert len(OWNED_TABLES) == 21

    # 明确禁止的东西：账本、余额、预留、QC 结论、接受母版都不在本库。
    for forbidden in ("ledger", "ledger_entry", "balance", "reservation",
                      "budget_reservation", "qc_result", "accepted_master"):
        assert forbidden not in actual, forbidden


def test_sql_literals_stay_inside_this_service():
    """E38-S44-10 S44 无任何写入 S23 的路径（静态断言 + 网络策略）

    前半：AST 扫出所有字符串常量，凡写语句都只落在自有表上；`DELETE` 一条
    都没有（本服务从不删上游事实，也不删自己的快照）；`UPDATE` 只出现在三张
    状态机表上。后半：网络策略 —— 源码里没有任何 URL scheme 字面量，出站
    只能经 `deps.build_*(os.environ.get(...))`，而 `deps.py` 里连 host 都
    没有。所以「向 S23 写一条账」在本服务里既没有 SQL 落点，也没有出口。
    """
    inserts, updates, deletes, upserts = Counter(), Counter(), [], Counter()
    for path, text in _source_texts().items():
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or \
                    not isinstance(node.value, str):
                continue
            sql = " ".join(node.value.split())
            upper = sql.upper()
            # 只认**以写动词开头**的字符串：模块 docstring 里出现「UPDATE 只出现
            # 在三张状态机表上」这样的散文不是语句，不能按 SQL 审。
            if not upper.startswith(("INSERT INTO", "UPDATE ", "DELETE FROM")):
                continue

            def target(pattern):
                match = re.search(pattern, sql, re.IGNORECASE)
                return match.group(1) if match else None

            if upper.startswith("INSERT INTO"):
                table = target(r"INSERT INTO\s+([a-z_]+)")
                assert table in OWNED_TABLES, (path.name, sql[:70])
                inserts[table] += 1
                if "ON CONFLICT" in upper:
                    upserts[table] += 1
            elif upper.startswith("UPDATE"):
                table = target(r"UPDATE\s+([a-z_]+)")
                assert table in UPDATABLE_TABLES, (path.name, sql[:70])
                updates[table] += 1
            elif upper.startswith("DELETE"):
                deletes.append((path.name, sql[:70]))

            # 每个写语句都必须带 tenant_id 作用域，除了两个白名单字面量。
            if "tenant_id" not in sql:
                assert any(sql.lower().startswith(prefix)
                           for prefix in TENANTLESS_SQL), (path.name, sql[:70])

    assert deletes == []                      # 一条 DELETE 都没有
    assert set(updates) == UPDATABLE_TABLES   # 三个状态机表
    assert sum(inserts.values()) == 18        # 18 条 INSERT 字面量
    assert sum(upserts.values()) == 4         # project_scope / accepted_projection
    assert set(upserts) == {"project_scope", "accepted_projection",
                            "blind_review_vote", "source_watermark"}
    # 写语句的表名必须是裸表名（`INSERT INTO s23.ledger_entry` 这种跨库写法
    # 在目标正则那一步就会被 `in OWNED_TABLES` 拒掉）；这里再正向确认一遍。
    for path, text in _source_texts().items():
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                upper = node.value.upper()
                if upper.startswith(("INSERT INTO", "UPDATE ", "DELETE FROM")):
                    assert not re.search(r"\b(?:s\d{2}|main|temp)\.", node.value), \
                        (path.name, node.value[:70])

    # 网络策略：没有 URL scheme，也就没有「顺手 POST 一条给 S23」的可能。
    for path, text in _source_texts().items():
        assert "://" not in text, path.name


def test_no_cross_service_imports_or_direct_database_access():
    """AGENTS.md 第一条：跨服务只走注入回调，不 import 业务 ORM。"""
    # 标准库随便用；第三方只允许这三个平台包 + HTTP 框架 + 契约校验器。
    # 关键是被禁止的集合：任何 `sNN` / 别的服务的包名都不行。
    allowed = set(sys.stdlib_module_names) | {
        "evaluation_analytics", "event_sdk", "idempotency_sdk",
        "fastapi", "pydantic", "starlette", "jsonschema"}
    for path, text in _source_texts().items():
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else \
                    [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert not re.fullmatch(r"s\d{2}(_[a-z_]+)?", name), \
                    (path.name, name)
                assert name in allowed, (path.name, name)

    # 只有内核与 app 拿到连接；deps 不许自己拨号，schema 不许连库。
    app_text = APP_PATH.read_text("utf-8")
    service_text = (PKG_SRC / "service.py").read_text("utf-8")
    deps_text = (PKG_SRC / "deps.py").read_text("utf-8")
    schema_text = (PKG_SRC / "schema.py").read_text("utf-8")
    assert app_text.count("sqlite3.connect") == 1
    assert "sqlite3.connect" not in service_text
    assert "sqlite3.connect" not in deps_text
    assert "sqlite3.connect" not in schema_text
    for name in ("s23.db", "S23.db", "s43.db", "evaluation.db"):
        assert name not in app_text + service_text


def test_unconfigured_dependencies_are_503_stubs_not_defaults():
    """§8：`build_*(None)` 必须是 503 闭包，不是「默认连 localhost」。"""
    builders = {
        "COST_SOURCE_UNAVAILABLE": build_cost_source,
        "QC_SOURCE_UNAVAILABLE": build_qc_source,
        "REVIEW_SOURCE_UNAVAILABLE": build_review_source,
        "DELIVERY_SOURCE_UNAVAILABLE": build_delivery_source,
        "JOB_SOURCE_UNAVAILABLE": build_job_source,
        "ACCEPTED_SOURCE_UNAVAILABLE": build_accepted_source,
        "EVALUATOR_UNAVAILABLE": build_evaluator_source,
    }
    assert all(True for _ in builders)
    for code, builder in builders.items():
        stub = builder(None)
        with pytest.raises(ServiceError) as caught:
            stub(TENANT, {})
        assert (caught.value.status, caught.value.code) == (503, code), code
        assert caught.value.retryable is False
        assert "configured" in str(caught.value), code

    # 巡检从同一个属性得到同一份清单：桩和报告不会各说一套。
    # 这里必须用**没有注入任何源**的实例，才是「生产启动、env 全空」的样子。
    bare = EvaluationAnalyticsService(
        sqlite3.connect(":memory:", isolation_level=None))
    assert bare.unavailable_dependencies() == sorted(builders)
    assert len(bare.unavailable_dependencies()) == 7
    assert bare.evaluation_execution_enabled is False


def test_contract_gaps_are_registered_without_inventing_owner_decisions():
    """§11 的开放问题要登记，不能自己选边；也不能一条都没有。"""
    assert len(CONTRACT_GAPS) == 17
    ids = [gap["id"] for gap in CONTRACT_GAPS]
    assert len(set(ids)) == len(ids)
    assert ids == [f"S44-{index}" for index in range(1, 18)]
    kinds = {gap["kind"] for gap in CONTRACT_GAPS}
    assert kinds <= {"needs_owner", "conflict", "gap"}
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["summary"].strip() and gap["handling"].strip()
    # 有需要所有者裁决的项，说明我们没有把冲突当成「已解决」。
    assert "needs_owner" in kinds


def test_events_match_the_registered_topic_and_contract_schemas(h):
    """§6.3：三个事件的 data 必须过契约；负对照证明校验器真的在看。"""
    jsonschema = pytest.importorskip("jsonschema")
    assert EVENTS == (EVENT_SNAPSHOT, EVENT_EVALUATION, EVENT_PROJECTION)

    cohort_id = h.ready()
    h.snapshot("usable_rate", cohort_id, command_id="e1")
    h.snapshot("cost_per_usable_minute", cohort_id, command_id="e3")
    h.service.put_dataset(TENANT, dataset_body())
    h.service.create_evaluation_run(TENANT, "ev1", {
        "command_id": "ev1", "dataset_id": "ds1", "dataset_version": 1,
        "evaluator_ref": {"id": "ev"}}, "h" * 64)
    h.service.compute_projection(TENANT, "pj", {
        "command_id": "pj", "window": "30d", "scope": {"project_id": "p1"},
        "cohort_id": cohort_id}, "h" * 64)

    observed = set(h.event_types())
    assert observed == set(EVENTS)
    # 主题名不是自封的：从 event-topics.v2.json 读回每个事件登记的 topic/producer。
    registry = json.loads((CONTRACTS / "event-topics.v2.json").read_text("utf-8"))
    topics = {entry["event_type"]: entry for entry in registry["topics"]}
    for event_type in EVENTS:
        assert topics[event_type]["topic"] == TOPIC
        assert topics[event_type]["producer"] == SOURCE
        assert topics[event_type]["partition_key"] == "tenant_id:aggregate_id"
    documents = {
        EVENT_SNAPSHOT: "analytics.metric-snapshot-created.v2.schema.json",
        EVENT_EVALUATION: "analytics.evaluation-completed.v2.schema.json",
        EVENT_PROJECTION: "analytics.cost-projection-updated.v2.schema.json",
    }
    for event in h.events():
        event_type = event["event_type"]
        assert event["source"] == SOURCE
        assert event["envelope"]["tenant_id"] == TENANT
        assert event["envelope"]["schema_version"] == SCHEMA_VERSION
        contract = json.loads(
            (CONTRACTS / documents[event_type]).read_text("utf-8"))
        jsonschema.validate(event["envelope"]["data"], contract)
        # 负对照：少一个必填键必须被拒 —— 否则上面的 validate 什么都没做。
        broken = {key: value for key, value
                  in list(event["envelope"]["data"].items())[1:]}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(broken, contract)

    # 快照事件的 aggregate 序号恒为 1（快照不可变，§3.2）。
    snapshots = [event for event in h.events()
                 if event["event_type"] == EVENT_SNAPSHOT]
    assert len(snapshots) == 2
    assert all(event["sequence"] == SEQUENCE_ONE for event in snapshots)
    # 评测事件按 run 递增：同一个 run 只会终态一次。
    for event in h.events():
        if event["event_type"] == EVENT_EVALUATION:
            assert event["sequence"] >= 1


def test_restart_recovery_rebuilds_the_same_view(tmp_path):
    """E13-S44-01 的邻域：重开库后快照、指标与水位都在，且一字不差。"""
    path = tmp_path / "s44.sqlite"
    first = Harness(conn=sqlite3.connect(path, isolation_level=None,
                                         check_same_thread=False))
    first.conn.row_factory = sqlite3.Row
    first.service = EvaluationAnalyticsService(
        first.conn, evaluation_execution_enabled=True,
        **{name: first._counted(name, getattr(first, attr))
           for name, attr in first.SOURCE_STUBS.items()})
    cohort_id = first.ready()
    snapshot = first.snapshot("cost_per_usable_minute", cohort_id,
                              command_id="persist")
    before = first.service.get_metrics(TENANT, cohort_id=cohort_id)
    first.conn.close()

    second = Harness(conn=sqlite3.connect(path, isolation_level=None,
                                          check_same_thread=False))
    second.conn.row_factory = sqlite3.Row
    second.service = EvaluationAnalyticsService(
        second.conn, evaluation_execution_enabled=True,
        **{name: second._counted(name, getattr(second, attr))
           for name, attr in second.SOURCE_STUBS.items()})
    restored = second.service.get_snapshot(TENANT, snapshot["snapshot_id"])
    assert restored["value"] == snapshot["value"]
    assert restored["n"] == snapshot["n"]
    assert restored["source_watermarks"] == snapshot["source_watermarks"]
    after = second.service.get_metrics(TENANT, cohort_id=cohort_id)
    assert after["metrics"] == before["metrics"]
    assert after["data_as_of"] == before["data_as_of"]
    # 事件也没丢：重启后仍能重发（outbox 是持久化的）。
    assert [event["event_type"] for event in second.events()] == [
        EVENT_SNAPSHOT]
    second.conn.close()


def test_version_coordinates_are_not_used_as_columns():
    """AGENTS.md：`execution_fence`（run/branch）与 `node_epoch`（单节点）
    是同名不同义的坐标，本服务不按它们出数，也就不该有这两列 —— 有列就迟早
    会有人拿来比较或互相赋值。
    """
    import evaluation_analytics.schema as schema_module

    ddl = schema_module.SCHEMA
    # schema 的模块 docstring 会提到这两个名字（说明为什么不用），但 DDL 里
    # 不许有列。这里只看 DDL 文本本身。
    for column in ("execution_fence", "node_epoch"):
        assert not re.search(rf"\b{column}\s+(INTEGER|TEXT|BIGINT)", ddl), column
        assert not re.search(rf"^\s*{column}\b", ddl, re.MULTILINE), column
    assert "execution_fence" not in ddl and "node_epoch" not in ddl

    # 内核与 HTTP 层的**代码**里（不含 docstring）不出现这两个名字：没有
    # 读取、比较、赋值。上下文形状校验只在 deps 里做，就在下一条。
    for path in sorted(PKG_SRC.glob("service*.py")) + [APP_PATH]:
        tokens, _tree = _code_tokens(path.read_text("utf-8"))
        for column in ("execution_fence", "node_epoch"):
            assert not [token for token in tokens
                        if isinstance(token, str) and column in token], \
                (path.name, column)
    assert "execution_fence" in REQUIRED_CONTEXT_KEYS
    assert "node_epoch" in REQUIRED_CONTEXT_KEYS


def test_plan_ids_are_covered_both_ways():
    """方案 §9 的 28 个编号：测试里一个不缺、一个不多，也没有 `T44-*`。"""
    text = PLAN_PATH.read_text("utf-8")
    planned = set(CASE_ID.findall(text))
    assert len(planned) == 28, sorted(planned)
    assert not re.search(r"T44-\d+", text)

    tree = ast.parse(Path(__file__).read_text("utf-8"), filename=__file__)
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Module, ast.ClassDef)):
            doc = ast.get_docstring(node) or ""
            covered.update(CASE_ID.findall(doc))
    assert covered - planned == set(), sorted(covered - planned)
    assert planned - covered == set(), sorted(planned - covered)
    assert len(covered) == 28

    # 每个编号都必须落在一个以 `test_` 开头的函数上，不是一个空壳类。
    per_function = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for case_id in CASE_ID.findall(ast.get_docstring(node) or ""):
                per_function[case_id] += 1
    assert all(count >= 1 for count in per_function.values())
    assert set(per_function) == planned

    # 每个编号都要在测试函数体里被真的用上（不是只写在 docstring）。
    # 反向检查：任何 `E\d+-S44` / `C-S44` 字面量都来自方案那一组。
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and CASE_ID.fullmatch(node.value)}
    assert literals <= planned, sorted(literals - planned)
