"""S32 单测。编号对应 `plans/S32-lipsync.md` §10 的 T32-01…T32-54，
括号里标出它守的验收条目（`docs/16`）。

三组是本文件的存在理由：

- `TestDiff` —— §4.6 的"不许动别人的脸"。它不是 QC，是权利约束：
  `preserve_other_faces` 在表上是 `CHECK (= 1)`，越界的产物**不登记 take**。
- `TestMoney` —— E16 / E14。`SUBMISSION_UNKNOWN` 之后**一个提交都不许发**；
  预算不足不降帧率、不缩窗口。
- `TestValidity` —— §4.2 的三路合取，**现场**算。take 里没有任何
  `valid_until`，因为有效期不是时间函数。

上游全部是 stub，**按真实契约写**：

- S26 的 `generation.job.state-changed` 里**没有** `provider_job_id`，只有
  `attempt_id`（同 S31 C11）。
- S28 的 `artifact.ingested` 里**没有** `frame_count` / `timecode_start` /
  `fps`（缺口 C4）。所以"阻塞在 INGESTING 并告警"是一条**正常**分支，不是
  测试造出来的边角。
- S37 的报告可能根本没有跑过（`ran=False`），那不是错误，是
  `not_yet_available`。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from idempotency_sdk import (
    CommandConflict,
    SqliteIdempotencyStore,
    canonical_request_hash,
)

from lipsync.schema import (
    CONTRACT_GAPS,
    JOB_STATES,
    MAX_FRAMES_PER_JOB,
    SCHEMA,
    TERMINAL_STATES,
)
from lipsync.service import (
    ALLOWED_TRANSITIONS,
    EVENT_LIPSYNC_READY,
    LipsyncService,
    ServiceError,
    Transaction,
    frame_range,
    input_fingerprint,
    visibility_grade,
)

FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"
TENANT = "t_alpha"
OTHER = "t_beta"

VIDEO_HASH = "a" * 64
AUDIO_HASH = "b" * 64
FACE_HASH = "f" * 64
OTHER_HASH = "9" * 64

# 默认窗口：start 820，音频 3280ms，trim_head 180 / trim_tail 60
#   end = 820 + 3280 - 60 = 4040
#   fps 30 → 处理帧 [22, 124]，processed = 103（闭区间）
DEFAULT_START = 820
DEFAULT_DURATION = 3280
DEFAULT_END = 4040
DEFAULT_FRAMES = 103


# ------------------------------------------------------------------ 夹具

def _context(tenant=TENANT, epoch=3, fence=9):
    """平台签名后的 execution-context.v2。18 个必填键一个不少。

    **没有 `run_id`**：`execution-context.v2` 的 18 个键里没有它（缺口 G16）。
    在这里补一个会让"台账写 run_id"看起来成立，而真实签名里根本取不到。
    """
    return {
        "schema_version": "2.0", "tenant_id": tenant,
        "project_id": "proj_1", "series_id": "ser_1",
        "branch_id": "br_main", "episode_id": "ep_3",
        "home_region": "cn-east", "home_cell": "cell_a",
        "manifest_ref": {"id": "man_7", "revision": 12,
                         "content_hash": "b" * 64},
        "node_id": "node_lipsync_ep3", "node_epoch": epoch,
        "execution_fence": fence,
        "memory_checkpoint": None,
        "policy_ref": {"service_id": "S02", "id": "pol_1", "revision": 2,
                       "content_hash": "c" * 64},
        "actor": {"principal_id": "u_1", "principal_type": "human",
                  "delegation_id": None},
        "command_id": "cmd_1", "request_hash": "d" * 64,
        "trace_id": "trace_1",
    }


def _facts(**over):
    """`deps.build_eligibility` 的 `_facts` 形状，逐键对齐。"""
    facts = {
        "video": {"content_hash": VIDEO_HASH, "frame_count": 600,
                  "fps": 30.0, "duration_ms": 20000,
                  "referenceable": True, "tombstoned": False,
                  "origin_service": "S30"},
        "audio": {"content_hash": AUDIO_HASH, "duration_ms": DEFAULT_DURATION},
        "audio_take": {"rights_state": "usable"},
        "s37": {"ran": True, "defects": [],
                "report_ref": {"service_id": "S37", "id": "ar_1",
                               "revision": 1, "content_hash": "c" * 64}},
        "face_authorization_state": "granted",
        "model_family": "lt_live",
    }
    return _merge(facts, over)


def _merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _body(**over):
    body = {
        "command_id": "cmd_1",
        "request_hash": "d" * 64,
        "context": _context(),
        "shot_lineage_id": "lin_ep3_sc3_02",
        "speaker_character_id": "char_linlan",
        "video_ref": {"service_id": "S28", "id": "vid_1", "revision": 2,
                      "content_hash": VIDEO_HASH},
        "audio_ref": {"service_id": "S28", "id": "aud_1", "revision": 1,
                      "content_hash": AUDIO_HASH},
        "audio_take_id": "vt_1",
        # C4：`video_ref` 自己带不了 origin，`artifact.ingested` 也不带
        # `ingesting_service`，所以调用方必须显式说出来（§4.9）。
        "video_origin_service": "S30",
        "face_track": {
            "ref": {"service_id": "S28", "id": "ft_1", "revision": 1,
                    "content_hash": FACE_HASH},
            "track_id": "face_2",
            "frame_range": {"start": 0, "end": 499},
            "visibility": {"visible_frames": DEFAULT_FRAMES,
                           "covered_frames": 0, "min_face_px": 96},
            "detector": {"model": "face_det", "version": "1.0"},
        },
        "audio_window": {"start_ms": DEFAULT_START, "trim_head_ms": 180,
                         "trim_tail_ms": 60, "source": "s37_alignment"},
        "preserve_other_faces": True,
        "fps": 30.0,
        "repair_action_id": None,
        "route_policy_ref": {"service_id": "S25", "id": "routing_demo",
                             "revision": 1, "content_hash": "e" * 64},
        "budget_scope_id": "budget_ep3",
        "deadline": FUTURE,
    }
    return _merge(body, over)


def _s26_event(job_id, state, *, reason=None, attempt_id=None,
               tenant=TENANT, artifact=None, provider_job_id=None):
    """S26 的**真实** payload：只有 `attempt_id`，没有 `provider_job_id`。"""
    data = {
        "schema_version": "2.0",
        "attempt_id": attempt_id or f"{job_id}:1",
        "state": state, "previous_state": "RUNNING",
        "business_attempt": 1, "network_retry": 0, "submit_attempts": 1,
        "provider": "prov_x", "channel": "ch_a",
        "node_id": "node_lipsync_ep3", "node_epoch": 3,
        "execution_fence": 9, "must_reconcile": state == "SUBMISSION_UNKNOWN",
        "reason_code": reason,
    }
    if artifact is not None:
        data["artifact_ref"] = artifact
    if provider_job_id is not None:
        data["provider_job_id"] = provider_job_id
    return {"tenant_id": tenant, "data": data}


class _RacingConn:
    """在"读检查点"之后、写之前插一刀：别人把它推进了。

    `BEGIN IMMEDIATE` 让同进程里没有真正的并发写者，竞态只能这样复现——
    而这正是 CAS 存在的理由：它守的是"读与写之间事实变了"。
    """

    def __init__(self, conn, *, on_sql, action):
        self._conn = conn
        self._on_sql = on_sql
        self._action = action
        self._fired = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, args=()):
        cur = self._conn.execute(sql, args)
        if not self._fired and self._on_sql in sql:
            self._fired = True
            self._action()
        return cur


class Harness:
    """一个内存库 + 按 `api/app.py` 形状复制的幂等外壳。"""

    def __init__(self, conn=None, **overrides):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.calls = {"eligibility": 0, "route": 0, "submit": 0, "cancel": 0,
                      "artifact_facts": 0, "artifact_check": 0, "rights": 0,
                      "diff": 0, "url": 0}
        self.eligibility_result = overrides.get("eligibility", _facts())
        self.route_result = overrides.get("route", {
            "feasible": True,
            "capabilities": {"face_region_localized_edit": None},
            "residency_allowed": None,
            "model_family": "lt_live",
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64},
        })
        self.submit_result = overrides.get("submit", {
            "provider_job_id": "pj_1", "attempt_id": None,
            "quote_upper_bound_micro": 41_000})
        # C4：S28 的 `artifact.ingested` 不带帧数/时码/fps，`GET /artifacts/{id}`
        # 在本 mock 里也不提供 —— 这就是"阻塞并告警"那条正常分支的默认形态。
        self.artifact_facts_result = overrides.get("artifact_facts", None)
        self.artifact_check_result = overrides.get("artifact_check", True)
        self.rights_state = overrides.get("rights", "usable")
        self.diff_result = overrides.get("diff", {
            "max_diff_complement": 1.0, "max_diff_target_region": 30.0,
            "changed_frame_ratio_complement": 0.5, "frames_compared": 103})
        self.url_result = overrides.get("url", {"url": "https://archive.invalid/x"})
        self.generation_state_result = overrides.get("generation_state", None)
        self.service = LipsyncService(
            self.conn,
            eligibility=self._eligibility,
            route_resolver=self._route,
            submitter=self._submit,
            canceler=self._cancel,
            artifact_facts=self._artifact_facts,
            artifact_check=self._artifact_check,
            url_refresher=self._url,
            rights_reader=self._rights,
            diff_measurer=self._diff,
            generation_state=self._generation_state,
        )
        self.idem = SqliteIdempotencyStore(self.conn)

    # --------------------------------------------------------- 注入回调

    def _call(self, name, value, *args):
        self.calls[name] += 1
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(*args)
        return value

    def _eligibility(self, tenant, request):
        return self._call("eligibility", self.eligibility_result, tenant,
                          request)

    def _route(self, tenant, request):
        return self._call("route", self.route_result, tenant, request)

    def _submit(self, tenant, command, decision_ref):
        return self._call("submit", self.submit_result, tenant, command,
                          decision_ref)

    def _cancel(self, tenant, provider_job_id, reason):
        return self._call("cancel", {"canceled": True}, tenant,
                          provider_job_id, reason)

    def _artifact_facts(self, tenant, artifact):
        return self._call("artifact_facts", self.artifact_facts_result,
                          tenant, artifact)

    def _artifact_check(self, tenant, artifact):
        return self._call("artifact_check", self.artifact_check_result,
                          tenant, artifact)

    def _url(self, tenant, artifact, source):
        return self._call("url", self.url_result, tenant, artifact, source)

    def _rights(self, tenant, audio_take_id):
        return self._call("rights", {"rights_state": self.rights_state},
                          tenant, audio_take_id)

    def _diff(self, tenant, request):
        return self._call("diff", self.diff_result, tenant, request)

    def _generation_state(self, tenant, provider_job_id):
        return self._call("generation_state_state", self.generation_state_result,
                          tenant, provider_job_id)

    def script(self, *results, skip=0):
        """让 `eligibility` 依次返回这几份事实，最后一份重复使用。

        `skip` 用来跳过已经发生过的那几次读（建 job 一次、提交前复查一次），
        否则"第三次读变了"会被错位成"第一次读变了"。
        """
        state = {"n": skip}

        def staged(tenant, request):
            index = min(state["n"], len(results) - 1)
            state["n"] += 1
            self.calls["eligibility"] += 1
            value = results[index]
            if isinstance(value, Exception):
                raise value
            return value

        self.service.eligibility = staged
        return state

    # --------------------------------------------------------- 便捷读库

    def create(self, body=None, *, tenant=TENANT, command_id=None):
        """按 `api/app.py` 的顺序：幂等占位 → 内核 → 落结果。"""
        body = body or _body()
        command_id = command_id or body["command_id"]
        request_hash = canonical_request_hash(body)
        record = self.idem.begin(tenant, command_id, request_hash)
        if record.result is not None:
            return json.loads(record.result)
        try:
            status, result = self.service.create_job(
                tenant, command_id, body, request_hash)
        except Exception:
            self.idem.abort(tenant, command_id)
            raise
        self.idem.finish(tenant, command_id,
                         json.dumps(result, ensure_ascii=False))
        # 409 是 `INPUTS_INELIGIBLE` 的**正常返回值**，不是异常：job 已落库，
        # 响应体同时是错误体与 job 引用。走 app.py 的同一套映射。
        self.last_status = status
        if status in (200, 202):
            result = {**result, "_status": status}
        return {**result, "_status": status}

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def state(self, job_id):
        row = self.one("SELECT state FROM lipsync_job WHERE lipsync_job_id=?",
                       (job_id,))
        return row["state"] if row else None

    def job(self, job_id):
        return self.one("SELECT * FROM lipsync_job WHERE lipsync_job_id=?",
                        (job_id,))

    def takes(self, job_id):
        return self.all("SELECT * FROM lipsync_take WHERE lipsync_job_id=?"
                        " ORDER BY created_at", (job_id,))

    def events(self, event_type=None):
        if event_type is None:
            rows = self.all("SELECT * FROM outbox ORDER BY created_at, sequence")
        else:
            rows = self.all("SELECT * FROM outbox WHERE event_type=? "
                            "ORDER BY created_at, sequence", (event_type,))
        for row in rows:
            row["envelope"] = json.loads(row["payload"])
        return rows

    def ready_events(self):
        return self.events(EVENT_LIPSYNC_READY)


def _artifact_for(job_id):
    """每个 job 一个唯一产物：`lt_artifact_uniq` 按 (租户, artifact id, hash)。"""
    return {"service_id": "S28", "id": f"vid_ls_{job_id}", "revision": 1,
            "content_hash": (job_id.encode().hex() * 2)[:64]}


def _archive_event(job_id, artifact, *, tenant=TENANT, **facts):
    """`artifact.ingested`：真实 payload 里**没有**帧事实（C4），
    所以帧事实是测试显式加上的——加与不加本身是两条分支。"""
    data = {"schema_version": "2.0", "lipsync_job_id": job_id,
            "artifact_ref": artifact, "ingested_at": FUTURE,
            "content_class": "generated_video"}
    data.update(facts)
    return {"tenant_id": tenant, "source": {"service_id": "S26",
                                            "provider_job_id": "pj_1"},
            "data": data}


def ingest(h, job_id, *, advance=True, facts=None, artifact=None,
           tenant=TENANT):
    """把 job 推到 READY 所需的两步：PROVIDER_SUCCEEDED → artifact.ingested。

    帧事实默认按 job 的实际窗口给（start 22 / 103 帧 / 30fps），因为事件的
    真实形状里没有它们 —— 不给就会走 C4 的阻塞分支。
    """
    job = h.job(job_id)
    if advance and job["state"] not in ("INGESTING", "WAITING_UNKNOWN"):
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_SUCCEEDED", tenant=tenant))
    artifact = artifact or _artifact_for(job_id)
    if facts is None:
        facts = {"frame_count": DEFAULT_FRAMES,
                 "timecode_start": 22, "fps": 30}
    event = _archive_event(job_id, artifact, tenant=tenant, **facts)
    with Transaction(h.conn):
        return h.service.on_artifact_ingested(event)


def ingest_raw(h, job_id, *, artifact=None, tenant=TENANT, advance=True):
    """不带任何帧事实的归档 —— C4 的正常形态。"""
    job = h.job(job_id)
    if advance and job["state"] not in ("INGESTING", "WAITING_UNKNOWN"):
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_SUCCEEDED", tenant=tenant))
    artifact = artifact or _artifact_for(job_id)
    with Transaction(h.conn):
        return h.service.on_artifact_ingested(
            _archive_event(job_id, artifact, tenant=tenant))


def _raw_job(h, job_id, **over):
    """绕过内核直接写一行 job —— 用来证明表上那道 CHECK 自己站得住。

    内核拒绝是**一道**门；如果只有它，任何一次绕过内核的写入（迁移脚本、
    对账工具、别的语言的实现）都能把契约里 `const: true` 的字段写坏。
    """
    values = {
        "lipsync_job_id": job_id, "tenant_id": TENANT, "project_id": "p",
        "series_id": "s", "episode_id": "e", "branch_id": "b",
        "shot_lineage_id": "shot", "speaker_character_id": "spk",
        "video_ref": '{"service_id":"S28","id":"v","revision":1,'
                     '"content_hash":"' + "a" * 64 + '"}',
        "audio_ref": '{"service_id":"S28","id":"a","revision":1,'
                     '"content_hash":"' + "b" * 64 + '"}',
        "window_start_ms": 0, "window_end_ms": 100,
        "window_source": "explicit", "process_frame_start": 0,
        "process_frame_end": 1, "processed_frames": 2,
        "face_track_binding_id": "ftb_x",
        "eligibility_snapshot_id": "ies_x", "preserve_other_faces": 1,
        "fps": 30.0, "visibility_grade": "full", "node_id": "n",
        "node_epoch_at_create": 1, "execution_fence": 1,
        "input_fingerprint": "c" * 64, "request_hash": "h",
        "idempotency_key": "k", "variation_nonce": "v",
        "execution_context": "{}", "budget_scope_id": "bud", "deadline": "d",
        "state": "CREATED", "created_at": "now", "updated_at": "now",
        "state_entered_at": "now",
    }
    values.update(over)
    columns = ", ".join(values)
    marks = ",".join("?" * len(values))
    h.conn.execute(
        f"INSERT INTO lipsync_job ({columns}) VALUES ({marks})",
        tuple(values.values()))


@pytest.fixture()
def h():
    return Harness()


# =================================================================== T32-01/02
class TestIdempotency:
    def test_t32_01_replay_returns_the_original_response(self, h):
        """T32-01（E12）：同一个 key 同一个 body，重放拿到**原来那份响应**。"""
        first = h.create()
        assert first["state"] == "WAITING_PROVIDER"
        job_id = first["lipsync_job_id"]
        assert h.calls["submit"] == 1

        # 不走内核、不走幂等表以外的任何东西：重放 = 读回存的 result。
        record = h.idem.begin(TENANT, "cmd_1",
                              canonical_request_hash(_body()))
        assert record.result is not None
        replay = json.loads(record.result)
        assert replay["lipsync_job_id"] == job_id
        assert replay["operation_id"] == first["operation_id"]
        # 没有第二条 job，也没有第二次提交。
        assert len(h.all("SELECT * FROM lipsync_job")) == 1
        assert h.calls["submit"] == 1

    def test_t32_02_same_command_different_body_is_409(self, h):
        """T32-02（E12）：同键不同 body —— 409，不是覆盖。"""
        h.create()
        with pytest.raises(CommandConflict) as exc:
            h.create(_body(shot_lineage_id="lin_other"))
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
        assert exc.value.retryable is False
        # 原 job 未被改写
        rows = h.all("SELECT shot_lineage_id FROM lipsync_job")
        assert [r["shot_lineage_id"] for r in rows] == ["lin_ep3_sc3_02"]
        assert h.calls["submit"] == 1


# =================================================================== T32-03..05
class TestWindow:
    def test_frame_range_is_a_closed_interval_with_two_handles(self):
        """§2.4 的算术：820→22 是因为 floor(24.6)=24 再减 2 帧手柄。"""
        assert frame_range(start_ms=820, end_ms=4040, fps=30.0,
                           frame_count=None) == (22, 124, 103)
        # 到片尾时尾部裁到最后一帧，裁掉多少体现在 processed 里
        assert frame_range(start_ms=820, end_ms=4040, fps=30.0,
                           frame_count=100) == (22, 99, 78)

    def test_t32_03_missing_audio_window_points_at_s37(self, h):
        """T32-03（E24）：没有窗口不是"猜一个"，而是告诉它去 S37 拿。"""
        body = _body()
        body.pop("audio_window")
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert exc.value.status == 422
        assert exc.value.code == "AUDIO_WINDOW_REQUIRED"
        how = exc.value.details["how_to_obtain"]
        assert how["service_id"] == "S37"
        assert how["route"] == "GET /alignment-reports/{id}/units"
        # 一个字段都没建
        assert h.all("SELECT * FROM lipsync_job") == []
        assert h.calls["submit"] == 0

    def test_t32_04_end_off_by_more_than_one_frame(self, h):
        """T32-04（E24）：`end_ms` 与音频长度对不上超过一帧 → 422。"""
        body = _body(audio_window={"start_ms": DEFAULT_START,
                                   "end_ms": DEFAULT_END + 200,
                                   "trim_head_ms": 180, "trim_tail_ms": 60,
                                   "source": "s37_alignment"})
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert exc.value.status == 422
        assert exc.value.code == "AUDIO_WINDOW_MISMATCH"
        assert exc.value.details["expected_end_ms"] == DEFAULT_END
        assert exc.value.details["audio_duration_ms"] == DEFAULT_DURATION
        # 一帧是 33.3ms；差 40ms 才算越界
        assert exc.value.details["frame_ms"] == pytest.approx(1000 / 30)

    def test_t32_04b_end_off_by_less_than_one_frame_is_tolerated(self, h):
        """同一帧以内的偏差不该让调用方重算 —— 那只会制造失败重试。"""
        result = h.create(_body(audio_window={
            "start_ms": DEFAULT_START, "end_ms": DEFAULT_END + 30,
            "trim_head_ms": 180, "trim_tail_ms": 60,
            "source": "s37_alignment"}))
        assert result["state"] == "WAITING_PROVIDER"

    def test_t32_05_window_past_video_bounds(self, h):
        """T32-05（E24）：窗口超出视频长度，不裁、不外扩。"""
        h.eligibility_result = _facts(video={"duration_ms": 3000})
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 422
        assert exc.value.code == "AUDIO_WINDOW_OUT_OF_RANGE"

    def test_fps_must_match_the_video(self, h):
        """帧号是视频的坐标系。fps 不同，同一个 ms 窗口指的是不同帧。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(fps=25.0))
        assert exc.value.code == "FPS_MISMATCH"
        assert exc.value.status == 422

    def test_audio_duration_unavailable_does_not_guess(self, h):
        """S28 说不出时长 → 503。拿一个猜的时长去算帧号等于伪造窗口。"""
        facts = _facts()
        facts["audio"]["duration_ms"] = None
        h.eligibility_result = facts
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 503
        assert exc.value.code == "AUDIO_DURATION_UNAVAILABLE"
        assert exc.value.retryable is True


# =================================================================== T32-06..12
class TestEligibility:
    def test_t32_06_open_major_voice_defect_blocks_with_a_suggestion(self, h):
        """T32-06（E24）：409 必须指向 S31，否则自动修复会重试口型。"""
        h.eligibility_result = _facts(s37={"defects": [
            {"defect_id": "def_1", "code": "AUD.TEXT.MISPRONOUNCE",
             "severity": "major", "status": "open"}]})
        result = h.create()
        assert h.last_status == 409
        assert result["_status"] == 409
        assert result["code"] == "AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC"
        d = result["details"]
        assert d["open_defect_ids"] == ["def_1"]
        assert d["suggested_action"] == "re_synthesize_speech"
        assert d["suggested_service"] == "S31"
        # job 已落库、终态，且一分钱没花
        job = h.job(result["lipsync_job_id"])
        assert job["state"] == "INPUTS_INELIGIBLE"
        assert job["failure_code"] == "AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC"
        assert h.calls["route"] == 0 and h.calls["submit"] == 0

    def test_t32_07_closed_defect_is_allowed(self, h):
        """T32-07（E24）：关掉的缺陷不再阻断 —— 否则修好的音频永远进不来。"""
        h.eligibility_result = _facts(s37={"defects": [
            {"defect_id": "def_1", "code": "AUD.TEXT.MISPRONOUNCE",
             "severity": "major", "status": "closed"}]})
        assert h.create()["state"] == "WAITING_PROVIDER"

    def test_t32_08_s37_never_ran_is_not_yet_available(self, h):
        """T32-08（E24）：没跑过 S37 不是错误，快照记 `not_yet_available`。"""
        h.eligibility_result = _facts(s37={"ran": False, "defects": [],
                                          "report_ref": None})
        result = h.create()
        assert result["state"] == "WAITING_PROVIDER"
        snap = h.one("SELECT voice_defect_check FROM input_eligibility_snapshot")
        assert snap["voice_defect_check"] == "not_yet_available"
        # 这个标记要一路带到 ready 事件里
        out = ingest(h, result["lipsync_job_id"])
        assert out["state"] == "READY"
        ready = h.ready_events()[0]["envelope"]["data"]
        assert ready["voice_defect_check_at_build"] == "not_yet_available"

    def test_t32_09_script_conflict_is_not_a_defect(self, h):
        """T32-09（E24）：`AUD.TEXT.SCRIPT_CONFLICT` 明文"不产生缺陷"。"""
        h.eligibility_result = _facts(s37={"defects": [
            {"defect_id": "def_2", "code": "AUD.TEXT.SCRIPT_CONFLICT",
             "severity": "blocker", "status": "open"}]})
        assert h.create()["state"] == "WAITING_PROVIDER"

    def test_non_text_defect_does_not_block(self, h):
        """口型只管"嘴和词对不对"，别的类别的缺陷不归它判。"""
        h.eligibility_result = _facts(s37={"defects": [
            {"defect_id": "def_3", "code": "AUD.LOUD.LUFS",
             "severity": "blocker", "status": "open"}]})
        assert h.create()["state"] == "WAITING_PROVIDER"

    def test_t32_10_void_rights_blocks_before_spending(self, h):
        """T32-10（E30）：权利已撤 —— 字节没变，但不许用。"""
        h.eligibility_result = _facts(audio_take={"rights_state": "void"})
        result = h.create()
        assert h.last_status == 409
        assert result["code"] == "AUDIO_RIGHTS_VOID"
        assert result["details"]["suggested_service"] == "S31"
        assert h.calls["submit"] == 0

    def test_t32_11_tombstoned_video_blocks(self, h):
        """T32-11（E23）：视频被删除，口型就没有可编辑的帧。"""
        h.eligibility_result = _facts(video={"tombstoned": True})
        result = h.create()
        assert h.last_status == 409
        assert result["code"] == "VIDEO_TOMBSTONED"
        assert result["details"]["suggested_action"] == "regenerate_video"
        assert result["details"]["suggested_service"] == "S30"

    def test_t32_12_lapsed_face_authorization_blocks(self, h):
        """T32-12（E30）：脸授权过期 —— 动那张嘴需要新的授权。"""
        h.eligibility_result = _facts(face_authorization_state="lapsed")
        result = h.create()
        assert h.last_status == 409
        assert result["code"] == "FACE_AUTHORIZATION_LAPSED"
        assert result["details"]["suggested_service"] == "S10"
        assert h.calls["submit"] == 0

    def test_cross_tenant_reference_is_404_same_as_missing(self, h):
        """E28：跨租户与不存在**同一种答案**，否则 404/403 就成了探测器。"""
        h.eligibility_result = ServiceError(404, "INPUT_REF_NOT_FOUND",
                                            "not found")
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 404
        assert exc.value.code == "INPUT_REF_NOT_FOUND"
        h.eligibility_result = ServiceError(404, "VIDEO_NOT_FOUND",
                                            "not found")
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 404
        # 上游的 code 原样透出，但状态码不因租户而不同

    def test_eligibility_unreachable_does_not_create_a_job(self, h):
        """问不到合格性就不能建 job —— 建了就成了一份猜测。"""
        h.eligibility_result = ConnectionError("S37 down")
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 503
        assert exc.value.code == "INPUT_ELIGIBILITY_UNAVAILABLE"
        assert exc.value.retryable is True
        assert h.all("SELECT * FROM lipsync_job") == []


# =================================================================== T32-13..17
class TestVisibility:
    """§4.7。分母**始终是整个窗口**（103 帧），不是人脸轨覆盖的那段。"""

    def test_visibility_grade_math(self):
        assert visibility_grade(0, 103, 96) == ("absent", "NO_FACE_IN_WINDOW")
        assert visibility_grade(30, 103, 96) == ("sparse", "LOW_FACE_VISIBILITY")
        assert visibility_grade(72, 103, 96) == ("partial", None)
        assert visibility_grade(93, 103, 96) == ("full", None)
        # 脸太小是能力问题，先于"稀疏"
        assert visibility_grade(30, 103, 30) == ("too_small", "FACE_TOO_SMALL")
        # 0 帧时像素尺寸不构成独立理由
        assert visibility_grade(0, 103, 30) == ("absent", "NO_FACE_IN_WINDOW")

    def _with_visibility(self, **vis):
        return _body(face_track={"visibility": {"visible_frames": 103,
                                                "covered_frames": 0,
                                                "min_face_px": 96, **vis}})

    def test_t32_13_zero_visible_frames_rejected_without_spending(self, h):
        """T32-13（E24）：没有脸就没有可编辑的嘴。202，不花钱。"""
        result = h.create(self._with_visibility(visible_frames=0))
        assert h.last_status == 202
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "NO_FACE_IN_WINDOW"
        assert h.calls["route"] == 0 and h.calls["submit"] == 0
        # 被拒的分级也要留下来 —— "为什么被拒"不能只活在那次响应里
        assert h.job(result["lipsync_job_id"])["visibility_grade"] == "absent"

    def test_t32_14_thirty_percent_is_low_visibility(self, h):
        """T32-14（E24）：低可见度**不"尽力而为"**。"""
        result = h.create(self._with_visibility(visible_frames=30))
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "LOW_FACE_VISIBILITY"
        assert result["visibility_grade"] == "sparse"
        assert result["processed_frames"] == DEFAULT_FRAMES
        assert h.calls["submit"] == 0

    def test_t32_15_seventy_percent_is_allowed_and_marked_partial(self, h):
        """T32-15（E24）：允许，但 take 与 ready 事件都必须标 `partial`。

        这就是为什么分级要在建 job 时**存下来**：归档事件里没有人脸事实，
        收尾那一刻算不出来。
        """
        result = h.create(self._with_visibility(visible_frames=72))
        assert result["state"] == "WAITING_PROVIDER"
        job_id = result["lipsync_job_id"]
        assert h.job(job_id)["visibility_grade"] == "partial"

        out = ingest(h, job_id)
        assert out["state"] == "READY"
        take = h.takes(job_id)[0]
        assert take["visibility_grade"] == "partial"
        ready = h.ready_events()[0]["envelope"]["data"]
        assert ready["visibility_grade"] == "partial"

    def test_t32_16_tiny_face_is_rejected_even_when_visible(self, h):
        """T32-16（E24）：看得见但只有 30px —— 重绘那张脸是猜。"""
        result = h.create(self._with_visibility(min_face_px=30))
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "FACE_TOO_SMALL"
        assert h.calls["submit"] == 0

    def test_t32_17_over_budget_is_rejected_and_never_auto_split(self, h):
        """T32-17（E35）：1200+ 帧 → 拒绝，并告诉它自己拆；S32 不拆。"""
        # 片长要真的够长：`frame_range` 会把尾部裁到 `frame_count-1`
        facts = _facts(audio={"duration_ms": 45000},
                       video={"duration_ms": 60000, "frame_count": 1800})
        h.eligibility_result = facts
        body = _body(
            audio_window={"start_ms": DEFAULT_START, "trim_head_ms": 180,
                          "trim_tail_ms": 60, "source": "s37_alignment"},
            face_track={"frame_range": {"start": 0, "end": 1999},
                        "visibility": {"visible_frames": 1300,
                                       "covered_frames": 0,
                                       "min_face_px": 96}})
        result = h.create(body)
        assert h.last_status == 202
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "FRAME_BUDGET_EXCEEDED"
        assert result["processed_frames"] > MAX_FRAMES_PER_JOB
        assert "auto-split" in result["suggestion"]
        assert h.calls["route"] == 0 and h.calls["submit"] == 0
        # 没有第二条 job：拆分是调用方的决定
        assert len(h.all("SELECT * FROM lipsync_job")) == 1

    def test_absent_wins_over_budget(self, h):
        """既超预算又没脸：先报"没脸"——拆窗口也长不出脸。"""
        # 片长要真的够长：`frame_range` 会把尾部裁到 `frame_count-1`
        facts = _facts(audio={"duration_ms": 45000},
                       video={"duration_ms": 60000, "frame_count": 1800})
        h.eligibility_result = facts
        body = _body(
            audio_window={"start_ms": DEFAULT_START, "trim_head_ms": 180,
                          "trim_tail_ms": 60, "source": "s37_alignment"},
            face_track={"frame_range": {"start": 0, "end": 1999},
                        "visibility": {"visible_frames": 0,
                                       "covered_frames": 0,
                                       "min_face_px": 96}})
        assert h.create(body)["reason_code"] == "NO_FACE_IN_WINDOW"


# =================================================================== T32-18..21
class TestDiff:
    """§4.6 的"不许动别人的脸"。它不是 QC，是权利约束。"""

    def _ready_job(self, h):
        result = h.create()
        return result["lipsync_job_id"]

    def test_classify_other_region_three_bands(self):
        from lipsync.service import classify_other_region
        assert classify_other_region(0.5) == ("none", None)
        assert classify_other_region(6.0) == ("minor", None)
        assert classify_other_region(2.0) == ("none", None)
        assert classify_other_region(40.0) == ("none", "OTHER_FACE_ALTERED")

    def test_t32_18_six_of_255_passes_and_is_marked_minor(self, h):
        """T32-18（E01）：>2 且 <=8 是"轻微漂移"，登记 take 但标 minor。"""
        job_id = self._ready_job(h)
        h.diff_result = {"max_diff_complement": 6.0,
                         "max_diff_target_region": 30.0,
                         "changed_frame_ratio_complement": 0.5,
                         "frames_compared": DEFAULT_FRAMES}
        out = ingest(h, job_id)
        assert out["state"] == "READY"
        take = h.takes(job_id)[0]
        assert take["other_face_drift"] == "minor"
        assert take["other_region_max_diff"] == 6.0
        assert h.ready_events()[0]["envelope"]["data"]["other_face_drift"] == \
            "minor"

    def test_t32_19_forty_of_255_fails_without_registering_a_take(self, h):
        """T32-19（E01）：越界 → FAILED，**没有 take、没有度量行**。

        这不是"质量不合格"，是改了一张没有被授权的脸。
        """
        job_id = self._ready_job(h)
        h.diff_result = {"max_diff_complement": 40.0,
                         "max_diff_target_region": 30.0,
                         "changed_frame_ratio_complement": 0.9,
                         "frames_compared": DEFAULT_FRAMES}
        out = ingest(h, job_id)
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "OTHER_FACE_ALTERED"
        assert out["take_registered"] is False
        assert out["other_region_max_diff"] == 40.0
        assert h.takes(job_id) == []
        assert h.all("SELECT * FROM output_diff_measurement") == []
        assert h.ready_events() == []
        # job 上留下了原因，审计能查
        job = h.job(job_id)
        assert job["failure_code"] == "OTHER_FACE_ALTERED"
        assert "40.0/255" in job["failure_detail"]

    def test_t32_20_preserve_other_faces_false_is_rejected_twice(self, h):
        """T32-20（E01）：Python 层 422，**表上还有一道** `CHECK (= 1)`。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(preserve_other_faces=False))
        assert exc.value.status == 422
        assert exc.value.code == "PRESERVE_OTHER_FACES_REQUIRED"
        assert exc.value.details == {"received": False, "required": True}
        # 第二道门：绕过内核直接写库也进不去
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO lipsync_job (lipsync_job_id, tenant_id, "
                "project_id, series_id, episode_id, branch_id, "
                "shot_lineage_id, speaker_character_id, video_ref, audio_ref, "
                "window_start_ms, window_end_ms, window_source, "
                "process_frame_start, process_frame_end, processed_frames, "
                "face_track_binding_id, eligibility_snapshot_id, "
                "preserve_other_faces, fps, visibility_grade, node_id, "
                "node_epoch_at_create, execution_fence, input_fingerprint, "
                "request_hash, idempotency_key, variation_nonce, "
                "execution_context, budget_scope_id, deadline, state, "
                "created_at, updated_at, state_entered_at) VALUES "
                "('lj_raw','t_alpha','p','s','e','b','shot','spk','{}','{}',"
                "0,100,'explicit',0,1,2,'ftb_x','ies_x',0,30.0,'full','n',1,1,"
                "'" + "a" * 64 + "','h','k','v','{}','bud','d','CREATED',"
                "'now','now','now')")

    def test_t32_21_short_output_is_a_frame_count_mismatch(self, h):
        """T32-21（E23）：产物帧数少于处理的帧数 —— 花了钱但东西不对。"""
        job_id = self._ready_job(h)
        out = ingest(h, job_id, facts={"frame_count": 50,
                                       "timecode_start": 22, "fps": 30})
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "FRAME_COUNT_MISMATCH"
        assert h.takes(job_id) == []

    def test_t32_22_timecode_mismatch(self, h):
        """T32-22（E23）：产出从别的帧开始 —— 贴回原片会整体错位。"""
        job_id = self._ready_job(h)
        out = ingest(h, job_id, facts={"frame_count": DEFAULT_FRAMES,
                                       "timecode_start": 0, "fps": 30})
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "TIMECODE_MISMATCH"
        assert h.takes(job_id) == []

    def test_fps_mismatch_in_output(self, h):
        job_id = self._ready_job(h)
        out = ingest(h, job_id, facts={"frame_count": DEFAULT_FRAMES,
                                       "timecode_start": 22, "fps": 25})
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "FPS_MISMATCH"

    def test_diff_measurement_unreachable_leaves_the_job_ingesting(self, h):
        """量不出来 ≠ 没越界。503 让整笔回滚，job 留在 INGESTING（T32-46）。"""
        job_id = self._ready_job(h)
        h.diff_result = ConnectionError("measure service down")
        with pytest.raises(ServiceError) as exc:
            ingest(h, job_id)
        assert exc.value.status == 503
        assert exc.value.code == "DIFF_MEASUREMENT_UNAVAILABLE"
        assert h.state(job_id) == "INGESTING"
        assert h.takes(job_id) == []
        assert h.ready_events() == []


# =================================================================== T32-23/24/36
class TestStale:
    """"过期不是删除"：take 仍在，检查点不动，依据只写一次。"""

    def _ready(self, h):
        job_id = h.create()["lipsync_job_id"]
        # 第一次读（建 job）干净；第二次读（submit 前）干净；第三次读
        # （READY 事务内的复查）是我们要的"变了"。
        return job_id

    def test_t32_23_audio_changed_before_archive_makes_the_take_stale(self, h):
        """T32-23（E13）：音频换了 —— take 登记、标 stale、不推检查点。"""
        job_id = h.create()["lipsync_job_id"]
        changed = _facts(audio={"content_hash": OTHER_HASH})
        h.script(changed, skip=2)
        out = ingest(h, job_id)
        assert out["state"] == "READY"
        assert out["stale_basis"] == "audio_changed"
        assert out["checkpoint_advanced"] is False
        assert out["ready_published"] is False
        take = h.takes(job_id)[0]
        assert take["stale_basis"] == "audio_changed"
        assert take["stale_detected_at"] is not None
        # 检查点行根本不存在 —— 不是"存在但没动"
        assert h.all("SELECT * FROM lipsync_checkpoint") == []
        assert h.ready_events() == []
        # stale 事件要发，否则下游不知道这份 take 不能当基线
        stale = h.events("lipsync.take.stale")
        assert len(stale) == 1
        assert stale[0]["envelope"]["data"]["stale_basis"] == "audio_changed"

    def test_t32_24_video_changed_before_archive(self, h):
        """T32-24（E13）：视频换了，同一个道理。"""
        job_id = h.create()["lipsync_job_id"]
        h.script(_facts(video={"content_hash": OTHER_HASH}), skip=2)
        out = ingest(h, job_id)
        assert out["stale_basis"] == "video_changed"
        assert out["checkpoint_advanced"] is False
        assert h.all("SELECT * FROM lipsync_checkpoint") == []

    def test_t32_36_reconciled_result_with_changed_inputs_is_stale(self, h):
        """T32-36（E16）：对账成功拿到产物，但输入对已经变了。

        "对账成功 ≠ 结果可用"——这笔钱花了，产物留着审计，但检查点不能动。
        """
        # 先让提交结果不明
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        assert h.state(job_id) == "WAITING_UNKNOWN"

        # S26 说其实成功了
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_SUCCEEDED"))
        assert h.state(job_id) == "INGESTING"

        # 归档时输入对变了
        h.script(_facts(audio={"content_hash": OTHER_HASH}), skip=2)
        out = ingest(h, job_id, advance=False)
        assert out["state"] == "READY"
        assert out["stale_basis"] == "audio_changed"
        assert out["checkpoint_advanced"] is False
        assert h.job(job_id)["reconciled"] == 1
        assert h.all("SELECT * FROM lipsync_checkpoint") == []

    def test_eligibility_unreachable_during_recheck_is_treated_as_changed(self, h):
        """复查不可达不能读成"没变"：一个错标可用的 take 会被当基线。"""
        job_id = h.create()["lipsync_job_id"]
        h.script(ServiceError(503, "INPUT_ELIGIBILITY_UNAVAILABLE", "down"),
                 skip=2)
        out = ingest(h, job_id)
        assert out["stale_basis"] == "video_changed"
        assert out["checkpoint_advanced"] is False

    def test_tombstone_at_recheck_is_video_tombstoned(self, h):
        job_id = h.create()["lipsync_job_id"]
        h.script(_facts(video={"tombstoned": True}), skip=2)
        out = ingest(h, job_id)
        assert out["stale_basis"] == "video_tombstoned"
        assert h.all("SELECT * FROM lipsync_checkpoint") == []


# =================================================================== T32-25..32
def _repair(action_id="ra_1", *, action_type="redo_lipsync",
            defect_type="lip_sync", shot="lin_ep3_sc3_02",
            window_hint=None):
    action = {"action_id": action_id, "action_type": action_type,
              "defect_type": defect_type, "shot_lineage_id": shot,
              "speaker_character_id": "char_linlan"}
    if window_hint:
        action["window_hint"] = window_hint
    return action


class TestRepair:
    def test_t32_25_redo_after_window_change_gets_a_new_fingerprint(self, h):
        """T32-25（E26）：窗口变了 → 指纹不同 + 一笔新 job。"""
        first = h.create()
        base = h.job(first["lipsync_job_id"])
        assert first["strategy_class"].startswith("generate")

        second = h.create(_body(command_id="cmd_2",
                                audio_window={
                                    "start_ms": 900,
                                    "trim_head_ms": 180, "trim_tail_ms": 60,
                                    "source": "s37_alignment"}),
                          command_id="cmd_2")
        other = h.job(second["lipsync_job_id"])
        assert other["input_fingerprint"] != base["input_fingerprint"]
        assert second["state"] == "WAITING_PROVIDER"
        # `window_changed` 自身不区分是不是 redo —— 它只说"窗口和上一笔
        # 同镜同人的 job 比变了没有"（§8.2）；真正决定策略前缀的是 repair
        # 动作本身。
        assert second["window_changed"] is True

    def test_t32_25b_repair_reports_window_changed_and_a_redo_strategy(self, h):
        """修复动作的 strategy_class 必须带 `redo_lipsync:` 前缀与窗口标记。"""
        h.create()   # 先有一份基线窗口
        result = h.create(
            _body(command_id="cmd_2", repair_action_id="ra_1",
                  repair_action=_repair(window_hint={
                      "start_ms": 900, "end_ms": 4120}),
                  audio_window={"start_ms": 900, "trim_head_ms": 180,
                                "trim_tail_ms": 60,
                                "source": "s37_alignment"}),
            command_id="cmd_2")
        assert result["window_changed"] is True
        assert result["strategy_class"].startswith("redo_lipsync:")
        assert "window_changed=true" in result["strategy_class"]
        assert h.job(result["lipsync_job_id"])["repair_action_id"] == "ra_1"

    def test_t32_26_idle_redo_is_flagged_as_repeat_without_change(self, h):
        """T32-26（E26）：输入一个字节没变的重做 —— 记下来，但不拦。

        "没变"要用**别的** job 做基线：同镜同人的同指纹未终结 job 会被
        `lipsync_job_open_uniq` 挡住（§8.3），所以先让第一笔终结。
        """
        first = h.create()["lipsync_job_id"]
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(first, "PROVIDER_FAILED", reason="PROVIDER_ERROR"))
        assert h.state(first) == "FAILED"
        result = h.create(
            _body(command_id="cmd_2", repair_action_id="ra_1",
                  repair_action=_repair()), command_id="cmd_2")
        assert result["window_changed"] is False
        assert "window_changed=false" in result["strategy_class"]
        assert result["repeat_without_input_change"] is True
        ledger = h.all("SELECT * FROM lipsync_intent_ledger")
        assert [row["repeat_without_input_change"] for row in ledger] == [0, 1]
        assert h.events("lipsync.repeat-without-change")

    def test_t32_27_same_repair_action_twice_is_409(self, h):
        """T32-27（E12）：一个 RepairAction 只许应用一次，否则花两次钱。"""
        h.create(_body(command_id="cmd_1", repair_action_id="ra_1",
                       repair_action=_repair()), command_id="cmd_1")
        with pytest.raises(ServiceError) as exc:
            h.create(_body(command_id="cmd_2", repair_action_id="ra_1",
                           repair_action=_repair()), command_id="cmd_2")
        assert exc.value.status == 409
        assert exc.value.code == "REPAIR_ALREADY_APPLIED"
        assert exc.value.details["state"] == "WAITING_PROVIDER"
        assert h.calls["submit"] == 1

    def test_repair_action_id_must_match_the_action_body(self, h):
        with pytest.raises(ServiceError) as exc:
            h.create(_body(repair_action_id="ra_9",
                           repair_action=_repair("ra_1")))
        assert exc.value.code == "INVALID_REPAIR_ACTION"

    def test_t32_40_voice_defect_cannot_be_fixed_by_lipsync(self, h):
        """T32-40（E24）：缺陷类型是"声音"，重做口型修不了它。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(repair_action_id="ra_1",
                           repair_action=_repair(defect_type="voice")))
        assert exc.value.status == 422
        assert exc.value.code == "ACTION_TYPE_MISMATCH"
        assert exc.value.details["suggested_service"] == "S31"
        assert exc.value.details["suggested_action"] == "re_synthesize_speech"
        assert h.calls["submit"] == 0

    def test_t32_41_re_synthesize_is_not_s32s_action(self, h):
        """T32-41（E24）：S32 不参与语音重合成 —— 422，而不是悄悄做掉。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(repair_action_id="ra_1",
                           repair_action=_repair(
                               action_type="re_synthesize_speech")))
        assert exc.value.status == 422
        assert exc.value.code == "ACTION_TYPE_MISMATCH"
        assert exc.value.details["expected_type"] == "lipsync"
        assert h.calls["submit"] == 0


class TestChain:
    def test_t32_28_s32_output_without_a_repair_action_is_forbidden(self, h):
        """T32-28（E24）：拿自己的产物当输入却不说是修什么 = 无限套娃。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(video_origin_service="S32"))
        assert exc.value.status == 422
        assert exc.value.code == "SELF_CHAIN_FORBIDDEN"
        assert exc.value.details["origin_service"] == "S32"
        assert h.calls["submit"] == 0

    def test_t32_29_s32_output_with_a_repair_action_is_allowed(self, h):
        """T32-29（E24）：带着 RepairAction 就是一次明确的"再来一遍"。"""
        result = h.create(_body(video_origin_service="S32",
                                repair_action_id="ra_1",
                                repair_action=_repair()))
        assert result["state"] == "WAITING_PROVIDER"
        assert h.job(result["lipsync_job_id"])["repair_action_id"] == "ra_1"

    def test_t32_30_direct_provider_link_is_not_a_valid_origin(self, h):
        """T32-30（E01）：绕过 S28 直连供应商 = 没有审计链，也没有引用校验。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(video_origin_service="provider_direct"))
        assert exc.value.status == 422
        assert exc.value.code == "UNEXPECTED_VIDEO_ORIGIN"
        assert exc.value.details["allowed"] == ["S30", "S32"]

    def test_missing_origin_cannot_be_assumed(self, h):
        """说不出 origin 就不能建 job —— 链式判断没有默认值。"""
        facts = _facts()
        facts["video"]["origin_service"] = None
        h.eligibility_result = facts
        body = _body()
        body.pop("video_origin_service")
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert exc.value.code == "UNEXPECTED_VIDEO_ORIGIN"

    def test_t32_31_video_ref_must_be_s28(self, h):
        """T32-31（E01）：`service_id: openai` 的 ref 不是可审计的引用。"""
        with pytest.raises(ServiceError) as exc:
            h.create(_body(video_ref={"service_id": "openai", "id": "v",
                                      "revision": 1,
                                      "content_hash": VIDEO_HASH}))
        assert exc.value.status == 422
        assert exc.value.code == "UNEXPECTED_MEDIA_SERVICE"
        assert exc.value.details == {"field": "video_ref",
                                     "service_id": "openai"}
        # 表上还有第二道（`lj_video_is_s28`）
        with pytest.raises(sqlite3.IntegrityError):
            _raw_job(h, "lj_raw2", video_ref='{"service_id":"openai"}')

    def test_t32_32_defects_open_without_ids_is_rejected_by_the_table(self, h):
        """T32-32（E24）：表上不许"我觉得有问题"而没有证据。"""
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO input_eligibility_snapshot (snapshot_id, "
                "tenant_id, video_ref, audio_ref, voice_defect_check, "
                "open_defect_ids, audio_rights_state, video_referenceable, "
                "face_authorization_state, captured_at) VALUES "
                "('ies_raw','t_alpha','{}','{}','defects_open','[]',"
                "'usable',1,'granted','now')")


# =================================================================== T32-33..35/49/50
class TestMoney:
    """E16 / E14。这组断言的是**钱**：提交次数、有没有降级。

    每一个"结果不明"的出口后面都跟着同一句断言：`calls["submit"]` 没变。
    把它写成一组而不是散在各处，是因为漏掉一个出口就是一次重复付费。
    """

    def test_t32_33_capability_missing_is_rejected_locally(self, h):
        """T32-33（E18）：不支持局部重绘就不做 —— **不降级为整帧重绘**。

        整帧重绘会改掉没有被授权的脸（`preserve_other_faces` 是权利属性），
        所以这里既不是 409「输入不合格」，也不是尽力而为。
        """
        h.route_result = {
            **h.route_result,
            "capabilities": {"face_region_localized_edit": False},
            "degradation_options": ["whole_frame_redraw"],
        }
        result = h.create()
        assert result["_status"] == 202
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "CAPABILITY_MISSING"
        assert result["required_capability"] == "face_region_localized_edit"
        # 降级选项原样回给调用方，但 S32 自己一条都不用
        assert result["degradation_options"] == ["whole_frame_redraw"]
        assert h.calls["submit"] == 0
        assert len(h.all("SELECT * FROM lipsync_job")) == 1

    def test_t32_33b_unknown_capability_is_not_a_no(self, h):
        """C7：`None` 是"不知道"，只有显式 `False` 才拒绝。

        把 `None` 当成不支持会让整条能力位缺失的上游全部拦下。
        """
        assert h.route_result["capabilities"]["face_region_localized_edit"] is None
        assert h.create()["state"] == "WAITING_PROVIDER"
        assert h.calls["submit"] == 1

    def test_t32_34_submission_unknown_freezes_and_never_resubmits(self, h):
        """T32-34（E16）：连接断了 = 结果不明，冻结在 `WAITING_UNKNOWN`。

        `attempt_id` 必须在**调 submitter 之前**就落库，否则 S26 随后发来的
        `state-changed` 会被当成 `unknown_attempt` 丢掉，这一笔就永远对不上账。
        """
        h.submit_result = ConnectionError("timeout")
        result = h.create()
        job_id = result["lipsync_job_id"]
        assert result["_status"] == 202
        assert result["state"] == "WAITING_UNKNOWN"
        assert h.job(job_id)["attempt_id"] == f"{job_id}:1"
        assert h.job(job_id)["failure_code"] == "SUBMISSION_UNKNOWN"
        assert h.calls["submit"] == 1

    def test_t32_34b_accepted_without_an_id_is_also_unknown(self, h):
        """S26 说"收下了"却没给可对账的 id —— 与连接中断同罪。"""
        h.submit_result = {}
        result = h.create()
        job_id = result["lipsync_job_id"]
        assert result["state"] == "WAITING_UNKNOWN"
        assert "without an id" in h.job(job_id)["failure_detail"]
        assert h.calls["submit"] == 1

    def test_t32_35_same_fingerprint_during_unknown_is_refused(self, h):
        """T32-35（E16）：结果不明时同指纹再来一次 → 指向对账中的那一笔。"""
        h.submit_result = ConnectionError("timeout")
        frozen = h.create()["lipsync_job_id"]
        with pytest.raises(ServiceError) as exc:
            h.create(_body(command_id="cmd_2"), command_id="cmd_2")
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert exc.value.details == {
            "lipsync_job_id": frozen, "state": "WAITING_UNKNOWN",
            "must_reconcile_before_resubmit": True}
        assert h.calls["submit"] == 1
        assert len(h.all("SELECT * FROM lipsync_job")) == 1

    def test_sweep_after_deadline_fails_without_resubmitting(self, h):
        """E16：死线是唯一"什么都不用知道就能结束"的出口 —— 它不是重试。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        h.conn.execute("UPDATE lipsync_job SET deadline=? WHERE "
                       "lipsync_job_id=?", (PAST, job_id))
        out = h.service.sweep_deadlines()
        assert out["expired"] == [job_id]
        assert out["resubmitted"] is False
        assert h.state(job_id) == "FAILED"
        assert h.job(job_id)["failure_code"] == "DEADLINE"
        assert h.calls["submit"] == 1

    def test_sweep_leaves_a_live_deadline_alone(self, h):
        """死线没到就 sweep —— 不能提前判死，那会把一笔在途付费变成孤儿。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        assert h.service.sweep_deadlines()["expired"] == []
        assert h.state(job_id) == "WAITING_UNKNOWN"

    def test_reconcile_without_an_answer_does_nothing(self, h):
        """E16：S26 还没答案 → 原样冻结，不猜、不重提。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        out = h.service.reconcile_unknown(TENANT, job_id, None)
        assert out["state"] == "WAITING_UNKNOWN"
        assert out["reconciled"] is False
        assert out["resubmitted"] is False
        assert h.calls["submit"] == 1

    def test_reconcile_accepted_moves_to_ingesting_without_resubmitting(self, h):
        """E16：对账成功 ≠ 结果可用 —— 只是允许去取字节，不再发一次请求。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        out = h.service.reconcile_unknown(TENANT, job_id, {"accepted": True})
        assert out["state"] == "INGESTING"
        assert out["resubmitted"] is False
        assert h.state(job_id) == "INGESTING"
        assert h.calls["submit"] == 1

    def test_reconcile_not_accepted_is_its_own_terminal_state(self, h):
        """没被收单 → `RECONCILED_FAILED`。它可重试，`FAILED` 也可重试，
        但两者不能合并：一个"没花钱"，一个"花过钱失败了"。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        out = h.service.reconcile_unknown(
            TENANT, job_id, {"accepted": False, "reason_code": "QUOTA"})
        assert out["state"] == "RECONCILED_FAILED"
        assert h.job(job_id)["failure_code"] == "QUOTA"
        assert h.calls["submit"] == 1

    def test_retry_before_reconciling_is_refused(self, h):
        """E16：`retry` 在 UNKNOWN 上是 409 —— 它必须先去对账。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job(TENANT, job_id)
        assert exc.value.status == 409
        assert exc.value.code == "MUST_RECONCILE_FIRST"
        assert h.calls["submit"] == 1

    def test_recover_never_resubmits_an_unknown_job(self, h):
        """E16：重启矩阵里 `WAITING_UNKNOWN` 是 `noop`，不是"再来一次"。"""
        h.submit_result = ConnectionError("timeout")
        job_id = h.create()["lipsync_job_id"]
        out = h.service.recover()
        assert job_id in out["actions"]["noop"]
        assert out["resubmitted"] is False
        assert h.state(job_id) == "WAITING_UNKNOWN"
        assert h.calls["submit"] == 1

    def test_t32_49_budget_refusal_degrades_nothing(self, h):
        """T32-49（E14）：预算不足 → 直接失败。**不降帧率、不缩窗口。**

        降帧率或缩窗口是"用更差的输入换一次通过"，而口型的输入是被授权的
        那一对；改了就超出授权范围，也等于让报价重新成立。
        """
        job_id = h.create()["lipsync_job_id"]
        before = h.job(job_id)
        with Transaction(h.conn):
            out = h.service.on_generation_state_changed(
                _s26_event(job_id, "REJECTED_BUDGET"))
        after = h.job(job_id)
        assert out == {"lipsync_job_id": job_id, "state": "FAILED",
                       "reason_code": "BUDGET", "degraded": False}
        assert after["state"] == "FAILED"
        assert after["fps"] == before["fps"]
        assert after["processed_frames"] == before["processed_frames"]
        assert after["window_start_ms"] == before["window_start_ms"]
        assert after["window_end_ms"] == before["window_end_ms"]
        assert h.calls["submit"] == 1

    def test_t32_50_account_revoked_does_not_switch_channels(self, h):
        """T32-50（E18）：账户被撤销 → `FAILED(ACCOUNT_UNAUTHORIZED)`。

        换渠道是"绕过被撤销的授权"，不是容错（§2.6）。
        """
        job_id = h.create()["lipsync_job_id"]
        with Transaction(h.conn):
            out = h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_FAILED",
                           reason="ACCOUNT_UNAUTHORIZED"))
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "ACCOUNT_UNAUTHORIZED"
        assert out["channel_switched"] is False
        assert h.job(job_id)["failure_code"] == "ACCOUNT_UNAUTHORIZED"


# =================================================================== 审计可追溯
class TestAudit:
    """E39：每一步都能回答"谁在什么时候把它推到这里、为什么"。

    一条记录被删掉之后，它的历史也无法审计；所以这里断言的核心是**什么都没
    被跳过**，而不是某个字段长什么样。
    """

    def test_every_transition_is_written_once_and_in_order(self, h):
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        out = h.service.get_job(TENANT, job_id)
        history = out["history"]
        assert [e["seq"] for e in history] == list(range(1, len(history) + 1))
        assert [e["from_state"] for e in history][0] == "CREATED"
        assert history[-1]["to_state"] == "READY"
        # 每一段都是上一段的终点接下一段的起点：没有空洞
        for prev, nxt in zip(history, history[1:]):
            assert prev["to_state"] == nxt["from_state"]
        # 表里的行数与响应里的条数一致（响应不是拼出来的摘要）
        assert h.one("SELECT count(*) c FROM job_transition "
                     "WHERE lipsync_job_id=?", (job_id,))["c"] == len(history)
        assert out["state_seq"] == len(history)
        assert all(e["at"] for e in history)

    def test_the_take_keeps_its_provenance(self, h):
        """take 上留得下"用哪份对齐报告、哪个人脸授权、哪次尝试"的证据。"""
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        take = h.service.get_take(
            TENANT, h.takes(job_id)[0]["lipsync_take_id"])
        assert take["lipsync_job_id"] == job_id
        assert take["input_fingerprint"] == h.job(job_id)["input_fingerprint"]
        assert take["s37_report_ref"]["service_id"] == "S37"
        assert take["voice_defect_check_at_build"] == "clean"
        assert take["measurement"]["frames_compared"] == DEFAULT_FRAMES
        assert take["measurement"]["measured_at"]

    def test_a_refusal_records_its_reason_code_and_detail(self, h):
        """被拒也要留痕：`FAILED` 不是"没发生"，是"发生了并且有原因"。"""
        h.route_result = {**h.route_result, "residency_allowed": False,
                          "region": "eu-west"}
        job_id = h.create()["lipsync_job_id"]
        history = h.service.get_job(TENANT, job_id)["history"]
        last = history[-1]
        assert last["to_state"] == "REJECTED_LOCAL"
        assert last["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert h.job(job_id)["failure_code"] == "RESIDENCY_FORBIDDEN"

    def test_the_outbox_sequence_is_monotonic_per_aggregate(self, h):
        """事件序号按 **aggregate** 严格递增，不是全局递增。

        `lipsync.job.state-changed` 挂在 job 上，`production.lipsync.ready` 挂在
        `shot_lineage_id` 上；两条流各自的序号都要能独立发现缺口。写成全局
        唯一会把一条合法的"两个聚合同号"判成丢事件。
        """
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        rows = h.all("SELECT sequence, event_type, aggregate_id FROM outbox "
                     "WHERE tenant_id=? ORDER BY rowid", (TENANT,))
        by_aggregate = {}
        for row in rows:
            by_aggregate.setdefault(row["aggregate_id"], []).append(
                row["sequence"])
        assert set(by_aggregate) == {job_id, "lin_ep3_sc3_02"}
        for aggregate, seqs in by_aggregate.items():
            assert seqs == list(range(1, len(seqs) + 1)), aggregate

    def test_two_speakers_do_not_reuse_a_shot_sequence(self, h):
        """同镜两个说话人各自 READY → 镜头流上必须是两条不同的序号。

        曾经两条都用各自 job 的 `state_seq`（都是 6），于是镜头流里出现
        `6, 6`。按聚合序号发现缺口的消费者（docs/04）会把第二条当成重复
        投递丢掉——第二个说话人的 ready 就永远收不到了。
        """
        def ready(speaker, command_id, face_hash):
            job_id = h.create(_body(
                command_id=command_id, speaker_character_id=speaker,
                face_track={"ref": {"service_id": "S28",
                                    "id": f"ft_{speaker}", "revision": 1,
                                    "content_hash": face_hash}}),
                command_id=command_id)["lipsync_job_id"]
            ingest(h, job_id)
            return job_id

        ready("char_linlan", "cmd_1", FACE_HASH)
        ready("char_other", "cmd_2", OTHER_HASH)

        shot = [r["sequence"] for r in h.all(
            "SELECT sequence FROM outbox WHERE aggregate_id=? "
            "ORDER BY rowid", ("lin_ep3_sc3_02",))]
        assert len(shot) == 2
        assert shot == sorted(shot)
        assert len(set(shot)) == 2, shot
        # 两条 ready 都在，且指向不同的说话人
        speakers = [r["envelope"]["data"]["speaker_character_id"]
                    for r in h.ready_events()]
        assert sorted(speakers) == ["char_linlan", "char_other"]

    def test_the_same_speaker_advancing_twice_continues_the_stream(self, h):
        """同人第二份 take：序号接在镜头上一条之后，不回退、不重号。"""
        first = h.create()["lipsync_job_id"]
        ingest(h, first)
        second = h.create(_body(command_id="cmd_2", audio_window={
            "start_ms": 900, "trim_head_ms": 180, "trim_tail_ms": 60,
            "source": "s37_alignment"}), command_id="cmd_2")["lipsync_job_id"]
        ingest(h, second, facts={"frame_count": 102, "timecode_start": 25,
                                 "fps": 30})
        shot = [r["sequence"] for r in h.all(
            "SELECT sequence FROM outbox WHERE aggregate_id=? "
            "ORDER BY rowid", ("lin_ep3_sc3_02",))]
        assert shot == [1, 2]


# =================================================================== T32-37..39
class TestArchive:
    """E23：字节在 S28 里，取不出来时**再生成一次**不是修复。"""

    def _ingesting(self, h):
        job_id = h.create()["lipsync_job_id"]
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_SUCCEEDED"))
        assert h.state(job_id) == "INGESTING"
        return job_id

    def test_t32_37_expired_url_refreshes_instead_of_regenerating(self, h):
        """T32-37（E23）：URL 过期 → 向 S26 要新的，`regenerated` 恒为 False。"""
        job_id = self._ingesting(h)
        event = _archive_event(job_id, _artifact_for(job_id))
        event["data"]["error"] = "URL_EXPIRED"
        with Transaction(h.conn):
            out = h.service.on_artifact_ingested(event)
        assert out == {"lipsync_job_id": job_id, "state": "INGESTING",
                       "waiting": "artifact.ingested", "url_refreshed": True,
                       "regenerated": False}
        assert h.calls["url"] == 1
        assert h.calls["submit"] == 1

    def test_t32_37b_refresh_failure_keeps_waiting(self, h):
        """刷不出新 URL 就继续等 —— 一个取不出的产物既不能交付也不能判死。"""
        job_id = self._ingesting(h)
        event = _archive_event(job_id, _artifact_for(job_id))
        event["data"]["error"] = "URL_EXPIRED"
        h.url_result = ServiceError(503, "ARCHIVE_UNAVAILABLE", "down")
        with Transaction(h.conn):
            out = h.service.on_artifact_ingested(event)
        assert out["waiting"] == "fresh_url"
        assert out["warning"].startswith(
            "could not refresh archive URL: ARCHIVE_UNAVAILABLE")
        assert out["regenerated"] is False
        assert h.state(job_id) == "INGESTING"
        assert h.calls["submit"] == 1

    def test_t32_37c_empty_url_is_not_a_refresh(self, h):
        """返回了字段但是空串 —— 与"没返回"同一种事实，不能当成成功。"""
        job_id = self._ingesting(h)
        event = _archive_event(job_id, _artifact_for(job_id))
        event["data"]["reason_code"] = "URL_EXPIRED"
        h.url_result = {"url": ""}
        with Transaction(h.conn):
            out = h.service.on_artifact_ingested(event)
        assert out["waiting"] == "fresh_url"
        assert "warning" not in out
        assert out["regenerated"] is False

    def test_t32_38_missing_frame_facts_block_with_an_alert(self, h):
        """T32-38（E23）：S28 不带帧数/时码/fps → 阻塞 + 告警，**不假 READY**。

        这不是测试造出来的边角：`artifact.ingested.v2` 本来就没有这三个字段
        （C4）。缺哪个说哪个，不猜。
        """
        job_id = h.create()["lipsync_job_id"]
        out = ingest_raw(h, job_id)
        assert out["alert"] is True
        assert out["waiting"] == "frame_count,timecode_start,fps"
        assert out["state"] == "INGESTING"
        assert h.state(job_id) == "INGESTING"
        assert h.takes(job_id) == []
        assert h.ready_events() == []

    def test_t32_38b_a_partial_artifact_fetch_fills_only_the_gap(self, h):
        """S28 能补上缺失字段 → 正常收尾；能补上的那一份不是猜的。"""
        job_id = h.create()["lipsync_job_id"]
        h.artifact_facts_result = {"frame_count": DEFAULT_FRAMES,
                                   "timecode_start": 22, "fps": 30}
        out = ingest_raw(h, job_id)
        assert out["state"] == "READY"
        assert out["checkpoint_advanced"] is True

    def test_t32_39_hash_mismatch_fails_and_registers_no_take(self, h):
        """T32-39（E23）：供应商报的 hash 与归档的 hash 不同 = 字节不是那份。"""
        job_id = self._ingesting(h)
        event = _archive_event(job_id, _artifact_for(job_id),
                               frame_count=DEFAULT_FRAMES,
                               timecode_start=22, fps=30)
        # 供应商声明的 hash 挂在 `data.source` 上（S28 把它从 S26 抄下来）。
        event["data"]["source"] = {"service_id": "S26",
                                   "provider_job_id": "pj_1",
                                   "content_hash": OTHER_HASH}
        with Transaction(h.conn):
            out = h.service.on_artifact_ingested(event)
        assert out == {"lipsync_job_id": job_id, "state": "FAILED",
                       "reason_code": "HASH_MISMATCH"}
        assert h.takes(job_id) == []
        assert h.all("SELECT * FROM lipsync_checkpoint") == []


# =================================================================== T32-42..44
class TestConcurrency:
    """E20 / E30。检查点按说话人分行；同人两笔用 CAS；作废优先。"""

    def _ready(self, h, *, speaker="char_linlan", command_id="cmd_1",
               face_hash=FACE_HASH):
        job_id = h.create(_body(command_id=command_id,
                                speaker_character_id=speaker,
                                face_track={"ref": {"service_id": "S28",
                                                    "id": f"ft_{speaker}",
                                                    "revision": 1,
                                                    "content_hash": face_hash}}),
                          command_id=command_id)["lipsync_job_id"]
        assert ingest(h, job_id)["state"] == "READY"
        return job_id

    def test_t32_42_two_speakers_do_not_share_a_checkpoint(self, h):
        """T32-42（E20）：同一镜的两个说话人各占一行，两个都 `checkpoint_seq=1`。

        C6 说清了两者会改同一批帧里的不同嘴；检查点分行的意义是"谁都没覆盖
        谁"，重叠的鬼影由 S36 事后发现。
        """
        a = self._ready(h)
        b = self._ready(h, speaker="char_other", command_id="cmd_2",
                        face_hash=OTHER_HASH)
        assert h.state(a) == "READY" and h.state(b) == "READY"
        rows = h.all("SELECT speaker_character_id, checkpoint_seq FROM "
                     "lipsync_checkpoint ORDER BY speaker_character_id")
        assert [r["speaker_character_id"] for r in rows] == [
            "char_linlan", "char_other"]
        assert [r["checkpoint_seq"] for r in rows] == [1, 1]

        ck = h.service.get_shot_checkpoint(TENANT, "lin_ep3_sc3_02")
        assert ck["shot_lineage_id"] == "lin_ep3_sc3_02"
        assert [i["speaker_character_id"] for i in ck["items"]] == [
            "char_linlan", "char_other"]
        for item in ck["items"]:
            assert item["take_state"] == "READY"
            assert item["stale_basis"] is None

    def test_t32_43_same_speaker_race_loses_on_cas(self, h):
        """T32-43（E20）：同镜同人两笔同时 READY → 一笔成功，一笔 `409` 可重试。

        竞态只能这样复现：`BEGIN IMMEDIATE` 让同进程里没有真正的并发写者，
        而 CAS 守的正是"读与写之间事实变了"。
        """
        a = self._ready(h)
        first_take = h.takes(a)[0]["lipsync_take_id"]

        # 第二笔：同人、同窗口，只有人脸轨 hash 不同（指纹因此不同）
        b = h.create(_body(command_id="cmd_2",
                           face_track={"ref": {"service_id": "S28",
                                               "id": "ft_2", "revision": 1,
                                               "content_hash": OTHER_HASH}}),
                     command_id="cmd_2")["lipsync_job_id"]
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(b, "PROVIDER_SUCCEEDED"))

        real = h.service.conn
        h.service.conn = _RacingConn(
            real, on_sql="FROM lipsync_checkpoint",
            action=lambda: real.execute(
                "UPDATE lipsync_checkpoint SET checkpoint_seq=checkpoint_seq+100"))
        try:
            with pytest.raises(ServiceError) as exc:
                ingest(h, b, advance=False)
        finally:
            h.service.conn = real

        assert exc.value.status == 409
        assert exc.value.code == "CHECKPOINT_CAS_FAILED"
        assert exc.value.retryable is True
        # 整笔回滚：没有 take、没有 ready、检查点仍在第一笔手里
        assert h.takes(b) == []
        assert h.state(b) == "INGESTING"
        assert len(h.ready_events()) == 1
        ck = h.all("SELECT speaker_character_id, checkpoint_seq, "
                   "lipsync_take_id FROM lipsync_checkpoint")
        assert len(ck) == 1
        assert ck[0]["lipsync_take_id"] == first_take

    def test_t32_44_void_found_at_ingest_beats_ready(self, h):
        """T32-44（E30）方向一：归档那一刻权利已撤 → 标 stale，不推检查点。

        钱已经花了，产物留着审计，但它不能成为基线。
        """
        job_id = h.create()["lipsync_job_id"]
        h.script(_facts(audio_take={"rights_state": "void"}), skip=2)
        out = ingest(h, job_id)
        assert out["state"] == "READY"
        assert out["stale_basis"] == "audio_rights_void"
        assert out["checkpoint_advanced"] is False
        assert out["ready_published"] is False
        assert h.takes(job_id)[0]["stale_basis"] == "audio_rights_void"
        assert h.all("SELECT * FROM lipsync_checkpoint") == []
        assert h.ready_events() == []

    def test_t32_44b_void_after_ready_marks_the_take_without_deleting_it(self, h):
        """T32-44（E30）方向二：撤销事件晚到。

        take 标 `audio_rights_void`，**不删除**，检查点行也留着：检查点说的是
        "这一镜曾经用这份 take"，撤掉它等于篡改历史。可用性由 `/validity`
        现场回答（T32-54）。
        """
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        take_id = h.takes(job_id)[0]["lipsync_take_id"]

        out = h.service.on_voice_authorization_lapsed(
            {"tenant_id": TENANT, "data": {}})
        assert out == {"stale_takes": [take_id], "voided": 1}
        take = h.takes(job_id)[0]
        assert take["stale_basis"] == "audio_rights_void"
        assert take["stale_detected_at"] is not None
        # 检查点没被抹掉，历史仍可读
        ck = h.all("SELECT * FROM lipsync_checkpoint")
        assert len(ck) == 1 and ck[0]["lipsync_take_id"] == take_id
        assert h.service.validity(TENANT, take_id)["basis"] == "audio_rights_void"

    def test_second_void_is_a_noop(self, h):
        """`stale_basis` 只写一次：重复投递不该把因果改写成第二条依据。"""
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        first = h.service.on_voice_authorization_lapsed(
            {"tenant_id": TENANT, "data": {}})
        assert first["voided"] == 1
        second = h.service.on_voice_authorization_lapsed(
            {"tenant_id": TENANT, "data": {}})
        assert second == {"stale_takes": [], "voided": 0}
        assert len(h.events("lipsync.take.stale")) == 1


# =================================================================== T32-45/46
class TestRestart:
    """E13。崩溃后的内存结论一律作废：重问、重算，不续用。"""

    def _park_at(self, h, state):
        job_id = h.create()["lipsync_job_id"]
        h.conn.execute(
            "UPDATE lipsync_job SET state=?, failure_code=NULL "
            "WHERE lipsync_job_id=?", (state, job_id))
        h.calls["submit"] = 0
        return job_id

    def test_t32_45_rechecks_and_hands_back_without_submitting(self, h):
        """T32-45（E13）：`CHECKING_INPUTS` 崩溃后重做全部检查。

        重做**绝不**调用 submitter —— 崩溃前是否已经花过钱不确定（可能正卡在
        第二次资格读取与提交之间），再发一次就是重复付费。
        """
        job_id = self._park_at(h, "CHECKING_INPUTS")
        out = h.service.recover()
        entry = out["actions"]["rechecked"][0]
        assert entry["lipsync_job_id"] == job_id
        assert entry["outcome"] == "needs_reprepare"
        assert entry["resubmitted"] is False
        assert out["resubmitted"] is False
        assert h.state(job_id) == "CHECKING_INPUTS"
        assert h.calls["submit"] == 0

    def test_t32_45b_recheck_can_still_block(self, h):
        """重做不是"再放行一次"：上游可能在这中间撤了权利。"""
        job_id = self._park_at(h, "CHECKING_INPUTS")
        h.eligibility_result = _facts(audio_take={"rights_state": "void"})
        entry = h.service.recover()["actions"]["rechecked"][0]
        assert entry["outcome"] == "blocked"
        assert entry["status"] == 409
        assert entry["reason_code"] == "AUDIO_RIGHTS_VOID"
        assert h.state(job_id) == "INPUTS_INELIGIBLE"
        assert h.calls["submit"] == 0

    def test_t32_45c_a_vanished_reference_is_the_same_terminal_state(self, h):
        """引用在崩溃后消失 —— 与"上游说它不可用"同一个终态、同一个码。"""
        job_id = self._park_at(h, "CHECKING_INPUTS")
        h.eligibility_result = ServiceError(404, "VIDEO_NOT_FOUND", "gone")
        entry = h.service.recover()["actions"]["rechecked"][0]
        assert entry["outcome"] == "blocked"
        assert entry["reason_code"] == "INPUT_REF_NOT_FOUND"
        assert h.state(job_id) == "INPUTS_INELIGIBLE"

    def test_t32_45d_unreachable_recheck_stays_put(self, h):
        """复查不可达 ≠ 未通过。留在原地，下一轮再试。"""
        job_id = self._park_at(h, "CHECKING_INPUTS")
        h.eligibility_result = ConnectionError("S37 down")
        entry = h.service.recover()["actions"]["rechecked"][0]
        assert entry["outcome"] == "waiting"
        assert entry["reason_code"] == "INPUT_ELIGIBILITY_UNAVAILABLE"
        assert h.state(job_id) == "CHECKING_INPUTS"
        assert h.calls["submit"] == 0

    def test_t32_46_remeasures_instead_of_reusing_a_lost_measurement(self, h):
        """T32-46（E13）：`INGESTING` 崩溃后内存里的差异值已经没了。

        沿用一个不存在的度量就是伪造证据；重算才是唯一诚实的做法。
        """
        job_id = h.create()["lipsync_job_id"]
        with Transaction(h.conn):
            h.service.on_generation_state_changed(
                _s26_event(job_id, "PROVIDER_SUCCEEDED"))

        h.diff_result = ConnectionError("media diff down")
        with pytest.raises(ServiceError) as exc:
            ingest(h, job_id, advance=False)
        assert exc.value.status == 503
        assert exc.value.code == "DIFF_MEASUREMENT_UNAVAILABLE"
        assert exc.value.retryable is True
        # 整笔回滚：既没有 take，也没有半条 ready
        assert h.state(job_id) == "INGESTING"
        assert h.takes(job_id) == []
        assert h.ready_events() == []

        out = h.service.recover()
        assert job_id in out["actions"]["needs_remeasure"]
        assert out["resubmitted"] is False
        assert h.calls["submit"] == 1

        # 度量恢复后重投同一件事：重算、登记、推进
        h.diff_result = {"max_diff_complement": 1.0,
                         "max_diff_target_region": 30.0,
                         "changed_frame_ratio_complement": 0.5,
                         "frames_compared": DEFAULT_FRAMES}
        again = ingest(h, job_id, advance=False)
        assert again["state"] == "READY"
        assert again["checkpoint_seq"] == 1
        assert again["checkpoint_advanced"] is True
        assert len(h.ready_events()) == 1
        assert h.calls["submit"] == 1


# =================================================================== T32-47/48/51
class TestIsolation:
    """E28 / E31。跨租户与不存在同一种答案；驻留禁止在花钱之前。"""

    @pytest.mark.parametrize("code", [
        "VIDEO_NOT_FOUND", "AUDIO_NOT_FOUND", "VOICE_TAKE_NOT_FOUND",
        "INPUT_REF_NOT_FOUND"])
    def test_t32_47_cross_tenant_media_is_404(self, h, code):
        """T32-47（E28）：上游说"不是你的"与"不存在"都只能是 404。

        403 会让这个端点变成租户探测器；而 job 一行都不该建。
        """
        h.eligibility_result = ServiceError(404, code, "not found")
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 404
        assert exc.value.code == code
        assert h.all("SELECT * FROM lipsync_job") == []
        assert h.calls["submit"] == 0

    def test_t32_48_cross_tenant_face_track_is_404(self, h):
        """T32-48（E28）：人脸轨是生物特征派生物，更不能靠 403 泄漏存在性。"""
        h.eligibility_result = ServiceError(404, "FACE_TRACK_NOT_FOUND",
                                            "not found")
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 404
        assert exc.value.code == "FACE_TRACK_NOT_FOUND"
        assert h.all("SELECT * FROM lipsync_job") == []

    def test_a_foreign_job_is_404_not_403(self, h):
        """查询、取消也一样：别人的 job 与不存在的 job 是同一个答案。"""
        job_id = h.create()["lipsync_job_id"]
        for call in (lambda: h.service.get_job(OTHER, job_id),
                     lambda: h.service.cancel_job(OTHER, job_id),
                     lambda: h.service.retry_job(OTHER, job_id)):
            with pytest.raises(ServiceError) as exc:
                call()
            assert exc.value.status == 404
            assert exc.value.code == "LIPSYNC_JOB_NOT_FOUND"
        with pytest.raises(ServiceError) as exc:
            h.service.get_take(OTHER, "lt_missing")
        assert exc.value.status == 404
        assert exc.value.code == "LIPSYNC_TAKE_NOT_FOUND"
        assert h.calls["cancel"] == 0

    def test_t32_51_residency_refusal_happens_before_spending(self, h):
        """T32-51（E31）：数据驻留不允许 → 本地拒绝，一个字节都不发出去。"""
        h.route_result = {**h.route_result, "residency_allowed": False,
                          "region": "eu-west"}
        result = h.create()
        assert result["_status"] == 202
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert result["region"] == "eu-west"
        assert h.calls["submit"] == 0
        assert h.calls["cancel"] == 0

    def test_route_without_a_decision_ref_is_not_submitted(self, h):
        """没有决策引用就无法向 S26 交代"按哪条规则路由的" —— 本地拒绝。"""
        h.route_result = {"feasible": True,
                          "capabilities": {"face_region_localized_edit": None},
                          "residency_allowed": None}
        result = h.create()
        assert result["state"] == "REJECTED_LOCAL"
        assert result["reason_code"] == "ROUTE_UNAVAILABLE"
        assert h.calls["submit"] == 0


# =================================================================== T32-52
class TestReplay:
    """E32。至少一次投递：重复的 ready 不能触发第二次生成。"""

    def test_t32_52_redelivered_ready_is_inert(self, h):
        """T32-52（E32）：Inbox 去重 → 首次 True；绕过 Inbox 再投一次也安全。"""
        from event_sdk import Inbox

        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        env = h.ready_events()[0]["envelope"]
        take_id = h.takes(job_id)[0]["lipsync_take_id"]

        inbox = Inbox(h.conn)
        assert inbox.consume("S32", env["id"]) is True
        assert inbox.consume("S32", env["id"]) is False

        # 就算去重表没拦住（Kafka 重放、换消费者名），消费本身也必须无害：
        # 终态 job 不再被推动。
        redelivered = {"tenant_id": TENANT, "data": env["data"]}
        with Transaction(h.conn):
            out = h.service.on_artifact_ingested(redelivered)
        assert out == {"ignored": True, "reason": "job_not_ingesting",
                       "state": "READY"}

        # 没有第二次提交、没有第二条 take、没有第二条 ready
        assert h.calls["submit"] == 1
        assert len(h.all("SELECT * FROM lipsync_job")) == 1
        assert len(h.takes(job_id)) == 1
        assert h.takes(job_id)[0]["lipsync_take_id"] == take_id
        assert len(h.ready_events()) == 1
        assert h.one("SELECT checkpoint_seq FROM lipsync_checkpoint")[
            "checkpoint_seq"] == 1

    def test_unmapped_provider_state_is_ignored_with_a_reason(self, h):
        """不认识的状态既不猜也不吞：留一个能检索的原因。"""
        job_id = h.create()["lipsync_job_id"]
        with Transaction(h.conn):
            out = h.service.on_generation_state_changed(
                _s26_event(job_id, "QUANTUM_SUPERPOSITION"))
        assert out == {"ignored": True,
                       "reason": "unmapped_provider_state:"
                                 "QUANTUM_SUPERPOSITION"}
        assert h.state(job_id) == "WAITING_PROVIDER"

    def test_an_out_of_order_terminal_event_cannot_pull_a_job_back(self, h):
        """乱序：终态之后的迟到事件被忽略，不把 `READY` 拉回 `WAITING_PROVIDER`。"""
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        with Transaction(h.conn):
            out = h.service.on_generation_state_changed(
                _s26_event(job_id, "RUNNING"))
        assert out == {"ignored": True, "reason": "job_terminal",
                       "state": "READY"}
        assert h.state(job_id) == "READY"


# =================================================================== T32-53
class TestStateMachine:
    """E01。表驱动：新增状态忘了进表，第一次走那条边就报出来。"""

    def test_t32_53_the_table_and_the_state_list_agree(self):
        """T32-53（E01）：`ALLOWED_TRANSITIONS` 的域恰好是 `JOB_STATES`。"""
        assert set(ALLOWED_TRANSITIONS) == set(JOB_STATES)
        for source, targets in ALLOWED_TRANSITIONS.items():
            assert isinstance(source, str)
            assert isinstance(targets, frozenset), source
            assert targets <= set(JOB_STATES), source
            assert source not in targets, source

    def test_t32_53b_terminal_states_are_terminal(self):
        """终态真的出不去，只有两个可重试的失败态回到 `PREPARING`。"""
        retryable = {"FAILED", "RECONCILED_FAILED"}
        for state in TERMINAL_STATES - retryable:
            assert ALLOWED_TRANSITIONS[state] == frozenset(), state
        for state in retryable:
            assert ALLOWED_TRANSITIONS[state] == frozenset({"PREPARING"})
        # 非终态都必须能走出去，否则 job 会永远停在半路
        for state in set(JOB_STATES) - TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state], state
        # READY 永远不能回 INGESTING：同一份产物不许被收尾两次
        assert "INGESTING" not in ALLOWED_TRANSITIONS["READY"]

    def test_a_sink_state_must_be_declared_terminal(self):
        """没有出边的状态必须同时是终态 —— 否则它是个"半死不活"的位置。

        `RECONCILED_READY` 曾经是这种状态（计划的终态清单漏列它）。三条可
        观测后果：取消它撞 `_transition` 的断言（500 而非 409）、迟到的
        `RUNNING` 事件返回该 job 并不处于的 `WAITING_PROVIDER`、`recover`
        把一笔健康 job 归入 `unhandled`。
        """
        sinks = {s for s in JOB_STATES if not ALLOWED_TRANSITIONS[s]}
        assert sinks <= TERMINAL_STATES

    def test_reconciled_ready_is_treated_as_finished(self, h):
        """对账成功的 job 与 `READY` 一样是终点：不能取消、不被事件拉回。"""
        job_id = h.create()["lipsync_job_id"]
        h.conn.execute("UPDATE lipsync_job SET state='RECONCILED_READY', "
                       "failure_code=NULL, reconciled=1 "
                       "WHERE lipsync_job_id=?", (job_id,))

        with pytest.raises(ServiceError) as exc:
            h.service.cancel_job(TENANT, job_id)
        assert exc.value.status == 409
        assert exc.value.code == "JOB_NOT_CANCELABLE"
        assert h.calls["cancel"] == 0

        with Transaction(h.conn):
            out = h.service.on_generation_state_changed(
                _s26_event(job_id, "RUNNING"))
        assert out == {"ignored": True, "reason": "job_terminal",
                       "state": "RECONCILED_READY"}
        assert h.state(job_id) == "RECONCILED_READY"

        assert job_id in h.service.recover()["actions"]["noop"]

    def test_t32_53c_an_illegal_edge_raises_instead_of_writing(self, h):
        """独立升级回滚：旧版本试着走一条表里没有的边 → 断言，不是写入。"""
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        job = h.job(job_id)
        assert job["state"] == "READY"
        with pytest.raises(AssertionError) as exc:
            h.service._transition(job, "INGESTING")
        assert "illegal transition READY -> INGESTING" in str(exc.value)
        assert h.state(job_id) == "READY"

        with pytest.raises(AssertionError) as exc:
            h.service._transition(job, "TELEPORTED")
        assert "unknown state TELEPORTED" in str(exc.value)


# =================================================================== T32-54
class TestValidity:
    """§4.2 的三路合取，**现场**算。take 里没有 `valid_until`。

    有效期不是时间函数：它只能被"输入变了"驱动。所以这里既断言四条检查
    各自能单独把 `valid` 打成 false，也断言 take 表里根本没有一列可以存
    一个到期时刻。
    """

    def _ready(self, h):
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        return job_id, h.takes(job_id)[0]["lipsync_take_id"]

    def test_no_column_could_hold_an_expiry(self, h):
        """§4.2：不许有 `valid_until` —— 定时炸弹不是有效性。"""
        columns = {r["name"] for r in
                   h.all("PRAGMA table_info(lipsync_take)")}
        assert "valid_until" not in columns
        assert "expires_at" not in columns
        assert "validity" not in columns

    def test_a_live_take_passes_all_four_checks(self, h):
        _, take_id = self._ready(h)
        h.calls["rights"] = 0
        out = h.service.validity(TENANT, take_id,
                                 video_hash=VIDEO_HASH, audio_hash=AUDIO_HASH)
        assert out["valid"] is True
        assert out["basis"] is None
        assert out["checks"] == {"video_hash_matches": True,
                                 "audio_hash_matches": True,
                                 "audio_rights_usable": True,
                                 "artifact_referenceable": True}
        # S31 只在需要时读一次：这里恰好需要
        assert h.calls["rights"] == 1
        assert "valid_until" not in out

    def test_t32_54_void_rights_invalidates_a_hash_identical_take(self, h):
        """T32-54（E30）：hash 一个字节没变，权利撤了 → 不可用。

        这正是"客户端自己比 hash 不够"的那一行：撤销不改 hash。
        """
        _, take_id = self._ready(h)
        h.rights_state = "void"
        out = h.service.validity(TENANT, take_id,
                                 video_hash=VIDEO_HASH, audio_hash=AUDIO_HASH)
        assert out["valid"] is False
        assert out["basis"] == "audio_rights_void"
        assert out["checks"]["audio_rights_usable"] is False
        assert out["checks"]["video_hash_matches"] is True
        assert out["checks"]["audio_hash_matches"] is True

    def test_video_hash_mismatch_short_circuits_before_asking_rights(self, h):
        """指纹这一路假了就不必再问上游 —— 但 basis 必须说清是哪一路。"""
        _, take_id = self._ready(h)
        h.calls["rights"] = 0
        out = h.service.validity(TENANT, take_id, video_hash=OTHER_HASH)
        assert out["valid"] is False
        assert out["basis"] == "video_changed"
        assert out["checks"]["video_hash_matches"] is False
        assert "artifact_referenceable" not in out["checks"]
        assert h.calls["rights"] == 0

    def test_audio_hash_mismatch_is_a_different_basis(self, h):
        _, take_id = self._ready(h)
        out = h.service.validity(TENANT, take_id, audio_hash=OTHER_HASH)
        assert out["basis"] == "audio_changed"

    def test_tombstoned_artifact_is_artifact_unavailable(self, h):
        """第三路：tombstone 之后 hash 不变但字节没了。"""
        _, take_id = self._ready(h)
        h.artifact_check_result = False
        out = h.service.validity(TENANT, take_id)
        assert out["valid"] is False
        assert out["basis"] == "artifact_unavailable"
        assert out["checks"]["artifact_referenceable"] is False

    def test_unreachable_artifact_check_is_also_unavailable(self, h):
        """查不到 ≠ 还在。有效期判定上宁可保守。"""
        _, take_id = self._ready(h)
        h.artifact_check_result = ConnectionError("S28 down")
        out = h.service.validity(TENANT, take_id)
        assert out["basis"] == "artifact_unavailable"

    def test_an_unreachable_rights_reader_is_not_usable(self, h):
        """同一道理：`_rights_state` 把异常折成 `unknown`，不是 `usable`。

        `unknown` 与 `void` 给两个不同的 `basis`，但两者都不是"有效"：§4.2
        的合取写的是 `== 'usable'`，读不到就不满足。
        """
        _, take_id = self._ready(h)
        h.service.rights_reader = lambda tenant, audio: (_ for _ in ()).throw(
            ConnectionError("S31 down"))
        out = h.service.validity(TENANT, take_id)
        assert out["valid"] is False
        assert out["basis"] == "audio_rights_unknown"
        assert out["checks"]["audio_rights_usable"] is False
        assert out["checks"]["video_hash_matches"] is True
        assert out["checks"]["audio_hash_matches"] is True

    def test_a_stale_take_reports_its_recorded_basis(self, h):
        """已标 stale 的 take：依据来自记录，不再去问上游。"""
        job_id, take_id = self._ready(h)
        h.service.on_voice_authorization_lapsed(
            {"tenant_id": TENANT, "data": {}})
        h.calls["rights"] = 0
        out = h.service.validity(TENANT, take_id)
        assert out["valid"] is False
        assert out["basis"] == "audio_rights_void"
        assert out["checks"] == {}
        assert h.calls["rights"] == 0

    def test_a_superseded_take_is_not_valid(self, h):
        """S41 删镜 → `SUPERSEDED`：不是 stale，也不是删除，但不可用。"""
        job_id, take_id = self._ready(h)
        h.conn.execute("UPDATE lipsync_take SET state='SUPERSEDED' "
                       "WHERE lipsync_take_id=?", (take_id,))
        out = h.service.validity(TENANT, take_id)
        assert out["valid"] is False
        assert out["basis"] == "state_superseded"

    def test_cross_tenant_validity_is_404(self, h):
        _, take_id = self._ready(h)
        with pytest.raises(ServiceError) as exc:
            h.service.validity(OTHER, take_id)
        assert exc.value.status == 404
        assert exc.value.code == "LIPSYNC_TAKE_NOT_FOUND"


# =================================================================== 契约
class TestContract:
    """事件按契约最窄的形状发；缺口一个不少地挂在 `/healthz` 上。"""

    def test_every_gap_is_registered_and_named(self):
        """C1–C9 与 G10–G18 一个都不能丢：缺口消失和缺口被解决是两件事。"""
        ids = [gap.split()[0] for gap in CONTRACT_GAPS]
        assert ids == [f"C{i}" for i in range(1, 10)] + \
                      [f"G{i}" for i in range(10, 19)]
        assert all(len(gap) > 20 for gap in CONTRACT_GAPS)
        # 有争议的缺口要显式写"需要 owner"：S32 不替 owner 选边
        assert any("owner" in gap for gap in CONTRACT_GAPS)

    def test_ready_event_matches_the_contract_schema(self, h):
        jsonschema = pytest.importorskip("jsonschema")
        schema_path = (Path(__file__).resolve().parents[3] / "contracts"
                       / "production.lipsync-ready.v2.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        data = h.ready_events()[0]["envelope"]["data"]

        assert set(data) == set(schema["required"])
        jsonschema.validate(data, schema)
        # G10：事件里只有最窄形状，原始值留给查询接口
        assert data["frames"] == {"process_start": 22, "process_end": 124,
                                  "fps": 30.0}
        assert data["audio_window"] == {"start_ms": DEFAULT_START,
                                        "end_ms": DEFAULT_END}
        assert set(data["source_video_ref"]) == {"id", "content_hash"}
        assert data["checkpoint"]["from"] is None
        assert data["checkpoint"]["to"] == data["lipsync_take_id"]
        # 不发媒体 URL、人脸轨引用、供应商信息、差异掩膜
        blob = json.dumps(data)
        for forbidden in ("http://", "https://", "face_track", "provider",
                          "mask"):
            assert forbidden not in blob

    def test_ready_event_rejects_a_wide_shape(self, h):
        """反向证明：契约是 `additionalProperties: false`，多一个键就该红。"""
        jsonschema = pytest.importorskip("jsonschema")
        schema_path = (Path(__file__).resolve().parents[3] / "contracts"
                       / "production.lipsync-ready.v2.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        data = dict(h.ready_events()[0]["envelope"]["data"])
        data["artifact_url"] = "https://archive.invalid/x"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, schema)

    def test_the_checkpoint_from_points_at_the_previous_take(self, h):
        """第二次推进时 `checkpoint.from` 是上一份 take —— 审计链的要求。"""
        first = h.create()["lipsync_job_id"]
        ingest(h, first)
        first_take = h.takes(first)[0]["lipsync_take_id"]

        # 同镜同人、新窗口（指纹不同所以不撞 `lipsync_job_open_uniq`）
        second = h.create(_body(command_id="cmd_2", audio_window={
            "start_ms": 900, "trim_head_ms": 180, "trim_tail_ms": 60,
            "source": "s37_alignment"}), command_id="cmd_2")["lipsync_job_id"]
        # 新窗口的处理帧范围是 25..126（102 帧），产物必须对上才会被登记
        out = ingest(h, second, facts={"frame_count": 102,
                                       "timecode_start": 25, "fps": 30})
        assert out["state"] == "READY"
        assert out["checkpoint_seq"] == 2
        data = h.ready_events()[-1]["envelope"]["data"]
        assert data["checkpoint"]["from"] == first_take
        assert data["checkpoint"]["checkpoint_seq"] == 2


# =================================================================== 上游回调
class TestDeps:
    """`deps.build_all` 的硬约定：**未配置 ≠ 可用**。

    每一个回调都是"猜错就要么违反授权、要么花错钱"的判断点，所以未配置时
    必须 503，不能返回一个看起来合理的默认值。
    """

    def _deps(self):
        from lipsync.deps import build_all
        return build_all(env={})

    @pytest.mark.parametrize("name,code", [
        ("submitter", "GENERATION_JOBS_UNAVAILABLE"),
        ("canceler", "GENERATION_CANCEL_UNAVAILABLE"),
        ("generation_state", "GENERATION_STATUS_UNAVAILABLE"),
        ("route_resolver", "ROUTE_UNAVAILABLE"),
        ("artifact_facts", "ARTIFACT_FACTS_UNAVAILABLE"),
        ("artifact_check", "ARTIFACT_CHECK_UNAVAILABLE"),
        ("rights_reader", "VOICE_RIGHTS_UNAVAILABLE"),
        ("diff_measurer", "DIFF_MEASUREMENT_UNAVAILABLE"),
        ("url_refresher", "ARTIFACT_URL_REFRESH_UNAVAILABLE"),
    ])
    def test_an_unconfigured_upstream_refuses(self, name, code):
        with pytest.raises(ServiceError) as exc:
            self._deps()[name](TENANT, "x")
        assert exc.value.status == 503
        assert exc.value.code == code
        assert exc.value.retryable is True

    def test_the_unconfigured_fanout_refuses_too(self):
        """第 10 个回调 `eligibility` 是 S28+S31+S37+S10 的扇出合成。

        它不在上面那张表里，因为它要一个 dict 形状的请求而不是一个 id。
        未被直接断言过时，这里曾经只剩"建不了 job"这条间接证据 —— 而
        间接证据无法区分"它 503 了"和"它在别处先失败、根本没走到它"。
        """
        with pytest.raises(ServiceError) as exc:
            self._deps()["eligibility"](TENANT, {
                "video_ref": {"id": "v"}, "audio_ref": {"id": "a"},
                "audio_take_id": "at_1", "speaker_character_id": "ch_1"})
        assert exc.value.status == 503
        assert exc.value.code.endswith("_UNAVAILABLE")
        assert exc.value.retryable is True

    def test_every_factory_callback_is_accounted_for(self):
        """`build_all` 返回几个回调，测试就得逐个点过几个 —— 加一个漏一个。"""
        exercised = {
            "submitter", "canceler", "generation_state", "route_resolver",
            "artifact_facts", "artifact_check", "rights_reader",
            "diff_measurer", "url_refresher", "eligibility",
        }
        assert set(self._deps()) == exercised

    def test_an_unconfigured_service_cannot_create_a_job(self):
        """整条链未配置时：一个 job 都建不出来，更不会走到付费。"""
        conn = sqlite3.connect(":memory:", check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        service = LipsyncService(conn, **self._deps())
        with pytest.raises(ServiceError) as exc:
            service.create_job(TENANT, "cmd_1", _body(), "d" * 64)
        assert exc.value.status == 503
        assert conn.execute("SELECT count(*) c FROM lipsync_job"
                            ).fetchone()["c"] == 0

    def test_no_provider_url_is_hardcoded(self):
        """供应商地址只能来自环境变量：本包不出现任何 http(s) 字面量。"""
        package = Path(__file__).resolve().parents[1] / "src" / "lipsync"
        for path in package.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "http://" not in text, path.name
            assert "https://" not in text, path.name

    def test_the_paid_path_has_exactly_one_door(self):
        """付费提交只有 `submitter` 一个回调；`deps` 不给供应商发 HTTP。"""
        package = Path(__file__).resolve().parents[1] / "src" / "lipsync"
        source = (package / "deps.py").read_text(encoding="utf-8")
        # 所有上游都从 `*_BASE_URL` 取地址，且没有供应商名
        assert source.count("BASE_URL") >= 7
        lowered = source.lower()
        for provider in ("runway", "heygen", "sync.so", "synthesia",
                         "did.com", "hedra"):
            assert provider not in lowered


# =================================================================== HTTP 层
def _http_client(monkeypatch, tmp_path, **stub_overrides):
    """`api/app.py` 的 TestClient，上游全部换成 Harness 的 stub。

    `app.py` 在 import 时自己建连接、自己拼十个上游，所以：
    - `S32_DB` 必须指向独立文件库（import 那一刻就建表）；
    - `module.service` / `module._idem` 必须重绑 —— 路由函数在定义时就捕获了
      模块全局，只换 Harness 不管用；
    - 重绑时用 `module._conn`，否则断言查的是另一个库。
    """
    monkeypatch.setenv("S32_DB", str(tmp_path / "s32.db"))
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    # 不能 `import app`：别的服务也有 `api/app.py` 且可能先进 sys.modules。
    spec = importlib.util.spec_from_file_location("s32_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s32_api_app"] = module

    h = Harness(conn=module._conn, **stub_overrides)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h


def _post(client, body, *, command_id="cmd_1", tenant=TENANT,
          path="/lipsync-jobs"):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant})


class TestHttpLayer:
    """`api/app.py` 的映射表：202 / 200 / 409 / 400 / 403 / 422 各有语义。"""

    def test_new_job_is_202_with_replayed_false(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, _body())
        assert r.status_code == 202, r.text
        assert r.json()["state"] == "WAITING_PROVIDER"
        assert r.json()["replayed"] is False
        assert "_status" not in r.json()  # 内部标记不外泄
        assert h.calls["submit"] == 1

    def test_same_key_same_body_replays(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        first = _post(client, _body())
        again = _post(client, _body())
        assert again.status_code == 202
        assert again.json()["replayed"] is True
        assert again.json()["lipsync_job_id"] == first.json()["lipsync_job_id"]
        # 重放不能再花一笔钱
        assert h.calls["submit"] == 1

    def test_same_key_different_body_is_409(self, monkeypatch, tmp_path):
        """同键不同 body —— 409，**不是覆盖**（E25）。"""
        client, h = _http_client(monkeypatch, tmp_path)
        _post(client, _body())
        r = _post(client, _body(shot_lineage_id="lin_other"))
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "IDEMPOTENCY_KEY_REUSED"
        assert body["details"]["also_known_as"] == "IDEMPOTENCY_CONFLICT"
        assert h.calls["submit"] == 1

    def test_missing_idempotency_key_is_422(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/lipsync-jobs", json=_body(),
                        headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 422

    def test_empty_key_is_400(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, _body(), command_id="")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"
        assert h.all("SELECT * FROM lipsync_job") == []

    def test_key_must_match_the_body(self, monkeypatch, tmp_path):
        """请求头与 body 的 `command_id` 不一致 = 400，不猜哪个是真的。"""
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, _body(), command_id="cmd_9")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_BODY"
        assert h.all("SELECT * FROM lipsync_job") == []

    def test_body_tenant_mismatch_is_403(self, monkeypatch, tmp_path):
        """AGENTS.md：body 里的 tenant_id 只用来比对，绝不赋值。"""
        client, h = _http_client(monkeypatch, tmp_path)
        r = _post(client, _body(tenant_id="t_other"))
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_MISMATCH"
        assert h.all("SELECT * FROM lipsync_job") == []

    def test_inputs_ineligible_keeps_its_409_and_the_job(self, monkeypatch,
                                                         tmp_path):
        """409 的 job 已经落库：重放拿到的仍是这一条，不再问 S37。"""
        client, h = _http_client(
            monkeypatch, tmp_path,
            eligibility=_facts(audio_take={"rights_state": "void"}))
        r = _post(client, _body())
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "AUDIO_RIGHTS_VOID"
        assert body["state"] == "INPUTS_INELIGIBLE"
        job_id = body["lipsync_job_id"]
        calls = h.calls["eligibility"]

        again = _post(client, _body())
        assert again.status_code == 409
        assert again.json()["lipsync_job_id"] == job_id
        assert again.json()["replayed"] is True
        assert h.calls["eligibility"] == calls
        assert h.calls["submit"] == 0

    def test_reused_take_is_200_without_a_paid_job(self, monkeypatch,
                                                   tmp_path):
        """200 = 命中已有 take，**没有创建付费任务**（§6.1）。"""
        client, h = _http_client(monkeypatch, tmp_path)
        job_id = h.create()["lipsync_job_id"]
        ingest(h, job_id)
        r = _post(client, _body(command_id="cmd_2"), command_id="cmd_2")
        assert r.status_code == 200
        body = r.json()
        assert body["reused"] is True
        assert body["lipsync_job_id"] is None
        assert body["processed_frames"] == DEFAULT_FRAMES
        assert h.calls["submit"] == 1

    def test_get_job_is_tenant_scoped(self, monkeypatch, tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        job_id = _post(client, _body()).json()["lipsync_job_id"]
        mine = client.get(f"/lipsync-jobs/{job_id}",
                          headers={"X-Tenant-Id": TENANT})
        assert mine.status_code == 200
        assert mine.json()["lipsync_job_id"] == job_id
        theirs = client.get(f"/lipsync-jobs/{job_id}",
                            headers={"X-Tenant-Id": OTHER})
        missing = client.get("/lipsync-jobs/lj_does_not_exist",
                             headers={"X-Tenant-Id": OTHER})
        assert theirs.status_code == missing.status_code == 404
        assert theirs.json()["code"] == missing.json()["code"]

    def test_validity_endpoint_reads_query_hashes(self, monkeypatch,
                                                  tmp_path):
        client, h = _http_client(monkeypatch, tmp_path)
        job_id = _post(client, _body()).json()["lipsync_job_id"]
        ingest(h, job_id)
        take_id = h.takes(job_id)[0]["lipsync_take_id"]
        r = client.get(f"/lipsync-takes/{take_id}/validity",
                       params={"video_hash": OTHER_HASH},
                       headers={"X-Tenant-Id": TENANT})
        assert r.status_code == 200
        assert r.json()["valid"] is False
        assert r.json()["basis"] == "video_changed"

    def test_unknown_event_type_is_declared_unsubscribed(self, monkeypatch,
                                                         tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.post("/events/unknown.thing",
                        json={"tenant_id": TENANT, "data": {}})
        assert r.status_code == 200
        assert r.json() == {"ignored": True,
                            "reason": "unsubscribed:unknown.thing"}

    def test_healthz_exposes_the_gaps(self, monkeypatch, tmp_path):
        client, _ = _http_client(monkeypatch, tmp_path)
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["service_id"] == "S32"
        assert body["contract_gaps"] == CONTRACT_GAPS
