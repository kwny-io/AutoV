"""S31 单测。编号对应 `plans/S31-speech-production.md` §10 的 T31-01…T31-52，
括号里标出它守的验收条目（`docs/16`）。

三组是本文件的存在理由：

- `TestNoVoiceSwap` —— §4.3 的禁令。它在结构上就成立（冻结列 NOT NULL +
  没有 UPDATE 路径 + `RIGHTS_BLOCKED` 终态），所以测试查的不是"函数返回了
  什么"，而是"有没有一条路能换声线"。§11.1 要求这一步在任何付费通路接通
  之前完成。
- `TestMoney` —— E16 / E14。`SUBMISSION_UNKNOWN` 之后**一个提交都不许发**。
- `TestLedger` —— E26。键是 `shot_lineage_id`，复用写 0 行。

上游全部是 stub，**按真实契约写**：S15 的 `check-eligibility` 返回
`{eligible, authorization_state, state_seq, ...}`；S26 的
`generation.job.state-changed` 里**没有** `provider_job_id`（只有
`attempt_id`）；S28 的 `artifact.ingested` 用 `duration_ms` 而不是
`observed_duration_ms`，且它**可以为空**。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from idempotency_sdk import canonical_request_hash

from speech_production.schema import SCHEMA
from speech_production.service import (
    SpeechService,
    ServiceError,
    input_fingerprint,
)

FUTURE = "2099-01-01T00:00:00.000Z"
TENANT = "t_alpha"


# ------------------------------------------------------------------ 夹具

def _context(tenant=TENANT, epoch=3, fence=9):
    """平台签名后的 execution-context.v2。18 个必填键一个不少。"""
    return {
        "schema_version": "2.0", "tenant_id": tenant,
        "project_id": "proj_1", "series_id": "ser_1",
        "branch_id": "br_main", "episode_id": "ep_3",
        "home_region": "cn-east", "home_cell": "cell_a",
        "manifest_ref": {"id": "man_7", "revision": 12,
                         "content_hash": "b" * 64},
        "node_id": "node_speech_ep3", "node_epoch": epoch,
        "execution_fence": fence,
        "memory_checkpoint": None,
        "policy_ref": {"service_id": "S02", "id": "pol_1", "revision": 2,
                       "content_hash": "c" * 64},
        "actor": {"principal_id": "u_1", "principal_type": "human",
                  "delegation_id": None},
        "command_id": "cmd_1", "request_hash": "d" * 64,
        "trace_id": "trace_1",
    }


def _body(**over):
    body = {
        "command_id": "cmd_1",
        "request_hash": "d" * 64,
        "context": _context(),
        "utterance": {
            "utterance_id": "utt_3_2_02",
            "shot_lineage_id": "lin_ep3_sc3_02",
            "text": "你终于来了。",
            "text_revision": 2, "locale": "zh-CN",
            "speaker_character_id": "char_linlan",
            "source_ref": {"service_id": "S06", "id": "shot_3_2",
                           "revision": 8},
        },
        "voice_binding": {"id": "vox_linlan", "revision": 2},
        "emotion": "紧张克制",
        "pronunciation_lexicon": None,
        "output_spec": {"format": "wav", "sample_rate_hz": 48000},
        "require_alignment": True,
        "repair_action_id": None,
        "route_policy_ref": {"service_id": "S25", "id": "routing_demo",
                             "revision": 1, "content_hash": "e" * 64},
        "budget_scope_id": "budget_ep3",
        "deadline": FUTURE,
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return body


def _granted(seq=7, **over):
    out = {"voice_id": "vox_linlan", "eligible": True,
           "authorization_state": "granted", "state_seq": seq,
           "expires_at": None,
           "provider_mapping": {"channel": "ch_a", "provider_voice_id": "v1"},
           "deny_reason": None, "checked_at": FUTURE}
    out.update(over)
    return out


def _denied(reason, state="revoked", seq=8):
    return {"voice_id": "vox_linlan", "eligible": False,
            "authorization_state": state, "state_seq": seq,
            "expires_at": None, "provider_mapping": None,
            "deny_reason": reason, "detail": reason, "checked_at": FUTURE}


class Harness:
    """一个内存库 + 按 `api/app.py` 形状复制的幂等外壳。"""

    def __init__(self, **overrides):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.calls = {"eligibility": 0, "submit": 0, "route": 0,
                      "artifact": 0, "cancel": 0, "lexicon": 0, "bindings": 0}
        self.eligibility_result = overrides.get("eligibility", _granted())
        self.route_result = overrides.get("route", {
            "feasible": True,
            "capabilities": {"word_timestamps": True},
            "residency_allowed": None,
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64},
        })
        self.submit_result = overrides.get("submit", {
            "provider_job_id": "pj_1", "attempt_id": None,
            "generation_job_id": "gj_1", "quote_upper_bound_micro": 21_000})
        self.artifact_present = overrides.get("artifact_present", True)
        self.bindings_result = overrides.get("bindings", {
            "voice_id": "vox_linlan", "project_id": "proj_1",
            "series_scope": None, "current_revision": 2,
            "lexicon_bindings": []})
        self.lexicons = overrides.get("lexicons", {
            ("lex_jp", 4): {"lexicon_id": "lex_jp", "revision": 4,
                            "content_hash": "1" * 64,
                            "scope_kind": "project", "scope_id": "proj_1"},
        })
        self.service = SpeechService(
            self.conn,
            eligibility=self._eligibility,
            lexicon_reader=self._lexicon,
            bindings_reader=self._bindings,
            route_resolver=self._route,
            submitter=self._submit,
            canceler=self._cancel,
            artifact_check=self._artifact,
            generation_state=None,
        )

    # ---- stubs ----
    def _eligibility(self, tenant, voice_id, voice_revision, requested_use):
        self.calls["eligibility"] += 1
        if isinstance(self.eligibility_result, Exception):
            raise self.eligibility_result
        return self.eligibility_result

    def script(self, *results):
        """按调用顺序回放 S15 的回答；最后一个重复使用。

        §4.2 的五个时点各自独立查询，所以"撤销发生在 3 分钟后"只能这样
        表达 —— 那正是这个服务存在的理由。
        """
        queue = list(results)
        state = {"n": 0}

        def _call(tenant, voice_id, revision, use):
            self.calls["eligibility"] += 1
            index = min(state["n"], len(queue) - 1)
            state["n"] += 1
            value = queue[index]
            if isinstance(value, Exception):
                raise value
            return value

        self.service.eligibility = _call
        return _call

    def _lexicon(self, tenant, lexicon_id, revision):
        self.calls["lexicon"] += 1
        found = self.lexicons.get((lexicon_id, revision))
        if found is None:
            raise ServiceError(404, "LEXICON_NOT_FOUND", "nope")
        return found

    def _bindings(self, tenant, voice_id):
        self.calls["bindings"] += 1
        if isinstance(self.bindings_result, Exception):
            raise self.bindings_result
        return self.bindings_result

    def _route(self, tenant, request):
        self.calls["route"] += 1
        if isinstance(self.route_result, Exception):
            raise self.route_result
        return self.route_result

    def _submit(self, tenant, command, route_decision_ref):
        self.calls["submit"] += 1
        self.last_command = command
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    def _artifact(self, tenant, artifact_ref):
        self.calls["artifact"] += 1
        return self.artifact_present

    def _cancel(self, tenant, provider_job_id, reason):
        self.calls["cancel"] += 1

    # ---- 幂等外壳 ----
    def create(self, body=None, *, tenant=TENANT, command_id="cmd_1"):
        from idempotency_sdk import SqliteIdempotencyStore
        body = body or _body(command_id=command_id)
        store = SqliteIdempotencyStore(self.conn)
        request_hash = canonical_request_hash(body)
        record = store.begin(tenant, command_id, request_hash)
        if record.result is not None:
            return json.loads(record.result)
        try:
            status, result = self.service.create_job(
                tenant, command_id, body, request_hash)
        except Exception:
            store.abort(tenant, command_id)
            raise
        store.finish(tenant, command_id,
                     json.dumps(result, ensure_ascii=False))
        return result

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return None if row is None else dict(row)

    def all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def state(self, job_id):
        return self.one("SELECT state FROM speech_job WHERE speech_job_id=?",
                        (job_id,))["state"]


def ingest(harness, job_id, *, duration=3280, container="wav",
           sample_rate=48000, content_hash=None, provider_job_id="pj_1",
           artifact_id=None):
    """`WAITING_PROVIDER --PROVIDER_SUCCEEDED--> INGESTING`，再收 S28 事件。

    `artifact.ingested` 只把 `INGESTING` 推到 `READY`；不先走 S26 那一步，
    内核会（正确地）回答 `job_not_ingesting`。测试里必须按真实顺序发。
    """
    current = harness.state(job_id)
    if current not in ("INGESTING", "WAITING_UNKNOWN"):
        harness.service.on_generation_state_changed(
            _s26_event(job_id, "PROVIDER_SUCCEEDED",
                       attempt_id=harness.one(
                           "SELECT attempt_id FROM speech_job WHERE "
                           "speech_job_id=?", (job_id,))["attempt_id"]))
    # 每个 job 一批不同的字节：`vt_artifact_uniq` 是 (tenant, artifact id,
    # content_hash)，全部共用 `audio_1` 会让第二次收尾撞上唯一约束 —— 那
    # 是测试造出来的碰撞，不是真实世界里会发生的事。
    artifact_id = artifact_id or f"audio_{job_id}"
    content_hash = content_hash or (job_id.encode().hex() * 2)[:64]
    data = {
        "schema_version": "2.0",
        # S31 能按 `speech_job_id` 直接关联（`_job_for_artifact` 的第一条
        # 分支）。测试里所有 job 共用同一个 provider id，不显式给出就会关联
        # 到最先那一行 —— 那是测试的歧义，不是内核的。
        "speech_job_id": job_id,
        "artifact_ref": {"service_id": "S28", "id": artifact_id,
                         "revision": 1, "content_hash": content_hash},
        "content_class": "audio", "size_bytes": 128000,
        "source": {"kind": "provider_url", "provider_job_id": provider_job_id,
                   "attempt_id": None},
        "dedup": {"hit": False}, "derivative_hint": None,
        "mime_type": "audio/wav", "container": container,
        "sample_rate_hz": sample_rate, "audio_channels": 1,
    }
    if duration is not None:
        data["duration_ms"] = duration
    return harness.service.on_artifact_ingested(
        {"tenant_id": TENANT, "data": data})


# ================================================================== 幂等

class TestIdempotency:
    def test_same_command_replays_the_original_response(self):
        """T31-01 / E12：同 command 重复 → 原响应，不产生第二个 job。"""
        h = Harness()
        first = h.create()
        second = h.create()
        assert first == second
        assert h.calls["submit"] == 1
        assert len(h.all("SELECT * FROM speech_job")) == 1

    def test_same_command_different_body_is_409(self):
        """T31-02 / E12：同键不同 body 是冲突，**不是覆盖**。"""
        from idempotency_sdk import CommandConflict, SqliteIdempotencyStore
        h = Harness()
        h.create()
        store = SqliteIdempotencyStore(h.conn)
        changed = _body(emotion="狂喜")
        with pytest.raises(CommandConflict):
            store.begin(TENANT, "cmd_1", canonical_request_hash(changed))


# ================================================================== 校验

class TestValidation:
    def test_empty_text_is_rejected_before_any_call(self):
        """T31-03 / E04：空台词不建 job，且不花任何上游调用。"""
        h = Harness()
        body = _body(utterance={"text": ""})
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert (exc.value.status, exc.value.code) == (422, "EMPTY_UTTERANCE")
        assert h.all("SELECT * FROM speech_job") == []
        assert h.calls["eligibility"] == 0

    def test_whitespace_only_text_is_empty(self):
        """T31-04 / E04：纯空白与空同罪。"""
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.create(_body(utterance={"text": "   \n\t "}))
        assert exc.value.code == "EMPTY_UTTERANCE"

    def test_text_changed_without_bumping_revision_conflicts(self):
        """T31-05 / E12：同一个 text_revision 不能对应两份文本。"""
        h = Harness()
        h.create(_body(command_id="cmd_a"))
        changed = _body(command_id="cmd_b")
        changed["utterance"]["text"] = "你终于走了。"
        with pytest.raises(ServiceError) as exc:
            h.create(changed, command_id="cmd_b")
        assert (exc.value.status, exc.value.code) == (422,
                                                      "TEXT_REVISION_CONFLICT")

    def test_deadline_in_the_past_is_422(self):
        """F25：死线优先于任何昂贵调用。"""
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.create(_body(deadline="2000-01-01T00:00:00Z"))
        assert exc.value.code == "DEADLINE_PASSED"
        assert h.calls["eligibility"] == 0

    def test_deadline_with_an_offset_is_compared_as_a_time_not_a_string(self):
        """F25：方案 §6.1 的示例死线带 `+08:00`，不能按字节比较。

        这条测试先证明**字符串比较确实会错**：构造一个已经过去 5 分钟、但
        按 `+08:00` 渲染后字节上大于 `_now()` 的死线。旧的 `str(deadline)
        <= _now()` 会放行它，然后去为一次已经过期的请求花钱。
        """
        from datetime import datetime, timedelta, timezone as tz
        now = datetime.now(tz.utc)
        past = (now - timedelta(minutes=5)).astimezone(
            tz(timedelta(hours=8)))
        deadline = past.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        # 探针自证：字节比较说"还在未来"，真值说"已经过去"。
        assert deadline > now_text
        assert past <= now

        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.create(_body(deadline=deadline))
        assert exc.value.code == "DEADLINE_PASSED"
        assert h.calls["eligibility"] == 0

    def test_output_format_is_a_closed_set(self):
        h = Harness()
        with pytest.raises(ServiceError) as exc:
            h.create(_body(output_spec={"format": "aiff"}))
        assert exc.value.code == "INVALID_OUTPUT_SPEC"

    def test_body_tenant_must_match_the_signed_context(self):
        """E28：租户来自签名上下文，请求体只能比对，不能主张。"""
        h = Harness()
        body = _body()
        body["context"]["tenant_id"] = "t_evil"
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert (exc.value.status, exc.value.code) == (403, "TENANT_MISMATCH")


# ================================================================== 指纹

class TestFingerprint:
    def test_locale_is_in_the_fingerprint(self):
        """T31-41 / E02：换语言是另一次生产，不是同一次。"""
        base = dict(text_hash="a" * 64, voice_id="v", voice_revision=2,
                    lexicon_id=None, lexicon_revision=None,
                    lexicon_content_hash=None, emotion="e",
                    output_format="wav", sample_rate_hz=48000,
                    require_alignment=True)
        zh = input_fingerprint(locale="zh-CN", **base)
        en = input_fingerprint(locale="en-US", **base)
        assert zh != en

    def test_replay_noise_does_not_change_the_fingerprint(self):
        """T31-06 / E38：重跑整集不能因为 trace/attempt 变了就重新付费。

        `variation_nonce` / `attempt_id` / `deadline` 等根本不在签名里，
        所以这里能用"同参数两次得到同一个值"来证明它们进不来。
        """
        kwargs = dict(text_hash="a" * 64, voice_id="v", voice_revision=2,
                      lexicon_id="lex", lexicon_revision=4,
                      lexicon_content_hash="b" * 64, emotion="e",
                      locale="zh-CN", output_format="wav",
                      sample_rate_hz=48000, require_alignment=True)
        assert input_fingerprint(**kwargs) == input_fingerprint(**kwargs)

    def test_lexicon_source_kind_is_not_in_the_fingerprint(self):
        """同一个词典，显式给出与音色默认绑定，念出来是一样的。"""
        # source_kind 不在函数签名里 —— 这就是断言本身。
        import inspect
        params = inspect.signature(input_fingerprint).parameters
        assert "source_kind" not in params
        assert "variation_nonce" not in params
        assert "node_epoch" not in params
        assert "execution_fence" not in params


# ================================================================== 复用

class TestReuse:
    def _ready_take(self, h, *, tenant=TENANT):
        job = h.create()
        ingest(h, job["speech_job_id"])
        return h.one("SELECT * FROM voice_take")

    def test_same_fingerprint_reuses_without_paying(self):
        """T31-06 / E38：同指纹第二次 → 200 + reused，零付费调用。"""
        h = Harness()
        take = self._ready_take(h)
        submits = h.calls["submit"]
        body = _body(command_id="cmd_2")
        result = h.create(body, command_id="cmd_2")
        assert result["reused"] is True
        assert result["speech_job_id"] is None  # §6.1：不给一个会误导的 id
        assert result["voice_take_id"] == take["voice_take_id"]
        assert h.calls["submit"] == submits
        assert len(h.all("SELECT * FROM speech_job")) == 1

    def test_repair_action_never_reuses(self):
        """T31-07 / E24：修复请求必须真的重做，否则缺陷永远修不掉。"""
        h = Harness()
        self._ready_take(h)
        body = _body(command_id="cmd_2")
        body["repair_action"] = {"action_id": "ra_1",
                                 "action_type": "re_synthesize_speech",
                                 "defect_type": "voice",
                                 "shot_lineage_id": "lin_ep3_sc3_02"}
        result = h.create(body, command_id="cmd_2")
        assert result["reused"] is False
        assert result["speech_job_id"] is not None
        assert h.calls["submit"] == 2

    def test_tombstoned_artifact_is_not_reused(self):
        """T31-08 / E23：S28 已不可引用 → 走新 job，不把死链接递下去。"""
        h = Harness()
        self._ready_take(h)
        h.artifact_present = False
        result = h.create(_body(command_id="cmd_2"), command_id="cmd_2")
        assert result["reused"] is False
        assert result["speech_job_id"] is not None

    def test_void_take_with_same_fingerprint_is_409(self):
        """T31-09 / E30：命中一个已撤销的产物 = 请求的正是那个不能用的声音。"""
        h = Harness()
        take = self._ready_take(h)
        h.conn.execute(
            "UPDATE voice_take SET rights_state='void', rights_state_seq=9 "
            "WHERE voice_take_id=?", (take["voice_take_id"],))
        with pytest.raises(ServiceError) as exc:
            h.create(_body(command_id="cmd_2"), command_id="cmd_2")
        assert (exc.value.status, exc.value.code) == (
            409, "VOICE_AUTHORIZATION_REVOKED")

    def test_reuse_does_not_write_a_ledger_row(self):
        """T31-40 / E26：全复用重跑，台账 0 行、累计不变。"""
        h = Harness()
        self._ready_take(h)
        before = len(h.all("SELECT * FROM speech_intent_ledger"))
        h.create(_body(command_id="cmd_2"), command_id="cmd_2")
        assert len(h.all("SELECT * FROM speech_intent_ledger")) == before == 1


# ================================================================== 不换声线

class TestNoVoiceSwap:
    def test_voice_columns_are_not_null_and_never_updated(self):
        """T31-18 / E30：**没有换声线的代码路径**。

        两条结构性证据：列是 NOT NULL 且无默认值；内核源码里没有任何一条
        UPDATE 把这两个列作为被赋值方。
        """
        h = Harness()
        cols = {r["name"]: r for r in h.all("PRAGMA table_info(speech_job)")}
        for column in ("voice_binding_id", "voice_binding_revision"):
            assert cols[column]["notnull"] == 1
            assert cols[column]["dflt_value"] is None
        # 没有 fallback 列
        assert not any("fallback" in name for name in cols)

        from pathlib import Path
        import speech_production.service as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        import re
        for match in re.finditer(r"UPDATE speech_job SET ([^\"']+)", src):
            clause = match.group(1)
            assert "voice_binding_id=" not in clause, clause
            assert "voice_binding_revision=" not in clause, clause

    def test_sync_fast_path_is_a_409_and_creates_no_job(self):
        """T31-10 / E30 **时点 1**：同步快路径给即时否定，一行都不落。

        它省钱的地方不是"少查一次"，而是"不建一个必然走不到的 job"。
        """
        h = Harness(eligibility=_denied("AUTHORIZATION_REVOKED"))
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert (exc.value.status, exc.value.code) == (
            409, "VOICE_AUTHORIZATION_REVOKED")
        assert exc.value.details["deny_reason"] == "AUTHORIZATION_REVOKED"
        assert h.all("SELECT * FROM speech_job") == []
        assert h.calls["submit"] == 0 and h.calls["route"] == 0

    def test_revoked_before_dispatch_blocks_without_spending(self):
        """T31-10 / E30 **时点 2**：快路径通过、worker 开工时已撤销。

        `CHECKING_RIGHTS` 存在的理由就是这一段：请求可能在队列里躺了几分钟。
        """
        h = Harness()
        h.script(_granted(), _denied("AUTHORIZATION_REVOKED"))
        result = h.create()
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"
        assert result["state"] == "RIGHTS_BLOCKED"
        assert h.calls["submit"] == 0
        lapse = h.all("SELECT * FROM outbox WHERE event_type=?",
                      ("speech.voice-authorization-lapsed",))
        assert len(lapse) == 1

    def test_revocation_between_preparing_and_dispatch_is_caught(self):
        """T31-11 / E30：**时点 3**，最容易漏的那个。

        第一次查（同步快路径）通过，第二次（worker_start）通过，第三次
        （pre_dispatch）拒绝 —— 一笔钱都不能花。
        """
        h = Harness()
        calls = {"n": 0}
        original = h._eligibility

        def flaky(tenant, voice_id, revision, use):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise ServiceError(200, "UNREACHABLE_PLACEHOLDER", "x") \
                    if False else None
            return original(tenant, voice_id, revision, use)

        # 更直接的做法：让第 3 次调用返回 denied。
        seq = {"n": 0}
        results = [_granted(), _granted(), _denied("AUTHORIZATION_REVOKED")]

        def scripted(tenant, voice_id, revision, use):
            seq["n"] += 1
            h.calls["eligibility"] += 1
            return results[min(seq["n"], len(results)) - 1]

        h.service.eligibility = scripted
        result = h.create()
        assert seq["n"] == 3, "pre_dispatch recheck must have run"
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"
        assert h.calls["submit"] == 0
        points = [r["check_point"] for r in h.all(
            "SELECT check_point FROM rights_recheck_log ORDER BY checked_at, "
            "rowid")]
        assert points == ["worker_start", "pre_dispatch"]

    def test_revoked_after_dispatch_voids_the_take_and_publishes_nothing(self):
        """T31-12 / E30：钱已花，但 take 必须 void，`ready` 一个都不发。"""
        h = Harness()
        job = h.create()
        h.eligibility_result = _denied("AUTHORIZATION_REVOKED")
        out = ingest(h, job["speech_job_id"])
        assert out["state"] == "RIGHTS_BLOCKED"
        assert out["rights_state"] == "void"
        assert out["ready_published"] is False
        take = h.one("SELECT * FROM voice_take")
        assert take["rights_state"] == "void"
        assert take["rights_state_seq"] == 8
        assert h.all("SELECT * FROM outbox WHERE event_type=?",
                     ("production.speech.ready",)) == []
        # 检查点没被推进
        assert h.all("SELECT * FROM speech_checkpoint") == []

    def test_event_driven_revocation_marks_ready_takes_void(self):
        """T31-13 / E30：已 READY 的 take 收到撤销事件 → void，排除出检查点。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        assert h.one("SELECT * FROM voice_take")["rights_state"] == "usable"
        out = h.service.on_voice_authorization_changed({
            "tenant_id": TENANT,
            "data": {"voice_id": "vox_linlan", "to_state": "revoked",
                     "from_state": "granted", "state_seq": 9}})
        assert out["voided_takes"] == 1
        take = h.one("SELECT * FROM voice_take")
        assert (take["rights_state"], take["rights_state_seq"]) == ("void", 9)

    def test_second_revocation_event_is_a_noop(self):
        """`UPDATE ... WHERE rights_state <> 'void'` —— 第二次事件不覆盖依据。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        for seq in (9, 11):
            h.service.on_voice_authorization_changed({
                "tenant_id": TENANT,
                "data": {"voice_id": "vox_linlan", "to_state": "revoked",
                         "state_seq": seq}})
        assert h.one("SELECT * FROM voice_take")["rights_state_seq"] == 9

    def _assert_blocked_without_side_effects(self, state, reason):
        """两个 `RIGHTS_BLOCKED` 变体共用的三条断言：保留**具体**原因、
        不取路由、不提交、不新开第二个 job（换声线的形式化替身）。"""
        h = Harness()
        h.script(_granted(), _denied(reason, state=state))
        result = h.create()
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"
        assert result["state"] == "RIGHTS_BLOCKED"
        assert result["reason_code"] == reason
        assert h.calls["route"] == 0
        assert h.calls["submit"] == 0
        # 没有换声线的第二个 job
        assert len(h.all("SELECT * FROM speech_job")) == 1
        return h, result

    def test_expired_blocks_with_its_own_reason(self):
        """T31-14 / E30：授权过期 → `RIGHTS_BLOCKED(AUTHORIZATION_EXPIRED)`。

        在 `CHECKING_RIGHTS`（时点 2）到货 —— 快路径已经通过了，所以这里
        也必须验证 `RIGHTS_BLOCKED` 保留的是**具体**原因，而不是一个笼统的
        "没授权"。原因决定人该去找 S15 做什么。
        """
        self._assert_blocked_without_side_effects(
            "expired", "AUTHORIZATION_EXPIRED")

    def test_suspended_blocks_without_switching_voice(self):
        """T31-15 / E30：`suspended` 是**技术**停用，仍然不换声线、不重路由。

        这一条单独列出来，因为"suspended 是技术问题，所以换个渠道试试"是
        最自然的误改：技术性停用看起来像可用性问题，但换渠道 = 换声音实现，
        那是 S24/S25 的决定，不是本服务在失败路径上能悄悄做的。
        """
        h, result = self._assert_blocked_without_side_effects(
            "suspended", "AUTHORIZATION_SUSPENDED")
        assert result["reason_code"] != "AUTHORIZATION_REVOKED"
        assert h.calls["eligibility"] == 2

    def test_use_not_authorized_is_rights_class(self):
        """T31-16 / E30：用途未授权不是"技术故障"，不重试。

        它走的是权利类分支（`RIGHTS_BLOCKED`），不是 `REJECTED_LOCAL` ——
        否则 S39 会把它当成一个可以安排重试的普通失败。
        """
        h = Harness()
        h.script(_granted(), _denied("USE_NOT_AUTHORIZED", state="granted"))
        result = h.create()
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"
        assert result["reason_code"] == "USE_NOT_AUTHORIZED"
        assert h.calls["submit"] == 0

    def test_s15_unreachable_is_never_eligible(self):
        """T31-17 / E30：不可达 ≠ 可用。DB 的 CHECK 也拦着。"""
        h = Harness(eligibility=ConnectionError("connection refused"))
        with pytest.raises(ServiceError) as exc:
            h.create()
        assert exc.value.status == 503
        assert h.calls["submit"] == 0
        # 直接调用复查入口，验证它会写一行 unreachable/eligible=0 并过 CHECK
        out = h.service._recheck(
            tenant_id=TENANT, voice_id="vox_linlan", voice_revision=2,
            requested_use={"modality": "speech"}, check_point="worker_start",
            job_id="sj_x")
        assert (out["observed_state"], out["eligible"]) == ("unreachable",
                                                            False)
        row = h.one("SELECT * FROM rights_recheck_log ORDER BY rowid DESC")
        assert (row["observed_state"], row["eligible"]) == ("unreachable", 0)

    def test_unreachable_cannot_be_recorded_as_eligible(self):
        """DB 层面的证据：`rrl_unreachable_not_eligible` 是真约束。"""
        h = Harness()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO rights_recheck_log (recheck_id, tenant_id, "
                "speech_job_id, voice_id, check_point, observed_state, "
                "observed_state_seq, eligible, deny_reason, checked_at) "
                "VALUES ('rrl_bad','t','sj','v','worker_start','unreachable',"
                "NULL,1,NULL,'now')")

    def test_retry_on_rights_blocked_is_409(self):
        """T31-19 / E30：`RIGHTS_BLOCKED` 不可重试，且用 409 而非 422。

        409 说的是"当前状态不允许"，422 说的是"你的请求有问题"。后者会把
        调用方引去改请求体，而这里唯一正确的动作是去找 S15 恢复授权。
        """
        h = Harness()
        h.script(_granted(), _denied("AUTHORIZATION_REVOKED"))
        result = h.create()
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job(TENANT, result["speech_job_id"])
        assert (exc.value.status, exc.value.code) == (409, "JOB_NOT_RETRYABLE")

    def test_late_granted_event_does_not_revive_a_blocked_job(self):
        """T31-20 / E30：事件只收紧。`granted` 不得复活 `RIGHTS_BLOCKED`。"""
        h = Harness()
        h.script(_granted(), _denied("AUTHORIZATION_REVOKED"))
        result = h.create()
        out = h.service.on_voice_authorization_changed({
            "tenant_id": TENANT,
            "data": {"voice_id": "vox_linlan", "to_state": "granted",
                     "state_seq": 12}})
        assert out["ignored"] is True
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"

    def test_blocked_job_stays_blocked_after_an_event_and_a_restart(self):
        """终态无自动出口：撤销事件 + 重启都不能让它自己动。"""
        h = Harness()
        h.script(_granted(), _denied("AUTHORIZATION_REVOKED"))
        result = h.create()
        h.service.on_voice_authorization_changed({
            "tenant_id": TENANT, "data": {"voice_id": "vox_linlan",
                                          "to_state": "revoked",
                                          "state_seq": 9}})
        out = h.service.recover(TENANT)
        assert result["speech_job_id"] in out["noop"]
        assert h.state(result["speech_job_id"]) == "RIGHTS_BLOCKED"


# ================================================================== 词典

class TestLexicon:
    def test_explicit_lexicon_wins(self):
        """T31-21 / E02。"""
        h = Harness()
        body = _body(pronunciation_lexicon={"id": "lex_jp", "revision": 4})
        result = h.create(body)
        job = h.service.get_job(TENANT, result["speech_job_id"])
        assert job["lexicon"]["source_kind"] == "explicit"
        assert job["lexicon"]["lexicon_id"] == "lex_jp"

    def test_voice_default_is_used_when_request_is_null(self):
        """T31-22 / E02：第 2 级必须存在，否则第一次漏传就念错人名。"""
        h = Harness(bindings={
            "voice_id": "vox_linlan", "project_id": "proj_1",
            "lexicon_bindings": [{"lexicon_id": "lex_jp",
                                  "lexicon_revision": 4,
                                  "is_default": True}]})
        result = h.create()
        job = h.service.get_job(TENANT, result["speech_job_id"])
        assert job["lexicon"]["source_kind"] == "voice_default"
        assert job["lexicon"]["lexicon_id"] == "lex_jp"

    def test_no_binding_means_none_not_empty_lexicon(self):
        """T31-23 / E02：没有就是没有，不编一个空词典。"""
        h = Harness()
        result = h.create()
        job = h.service.get_job(TENANT, result["speech_job_id"])
        assert job["lexicon"]["source_kind"] == "none"
        assert job["lexicon"]["lexicon_id"] is None

    def test_lexicon_from_another_project_is_422(self):
        """T31-24 / E28：拿别的剧的专名表合成是灾难。"""
        h = Harness(lexicons={("lex_other", 1): {
            "lexicon_id": "lex_other", "revision": 1,
            "content_hash": "2" * 64, "scope_kind": "project",
            "scope_id": "proj_OTHER"}})
        body = _body(pronunciation_lexicon={"id": "lex_other", "revision": 1})
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert (exc.value.status, exc.value.code) == (
            422, "LEXICON_SCOPE_MISMATCH")

    def test_lexicon_from_another_tenant_is_404(self):
        """T31-25 / E28：跨租户与不存在在 S15 眼里一样，这里也不区分。"""
        h = Harness(lexicons={})
        body = _body(pronunciation_lexicon={"id": "lex_secret", "revision": 1})
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert exc.value.status == 404

    def test_lexicon_none_event_is_schema_shaped(self):
        """C10：`source_kind='none'` 时 `lexicon_ref` 仍要结构合法。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        event = h.one("SELECT * FROM outbox WHERE event_type=?",
                      ("production.speech.ready",))
        data = json.loads(event["payload"])["data"]
        assert data["lexicon_ref"]["source_kind"] == "none"
        assert data["lexicon_ref"]["id"]
        assert data["lexicon_ref"]["revision"] >= 1


# ================================================================== 路由能力

class TestRoute:
    def test_missing_word_timestamps_rejects_instead_of_downgrading(self):
        """T31-26 / E18：**不降级**。降级会让 S38 的断句悄悄失去输入。"""
        h = Harness(route={
            "feasible": True, "capabilities": {"word_timestamps": False},
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64}})
        result = h.create()
        assert h.state(result["speech_job_id"]) == "REJECTED_LOCAL"
        assert result["reason_code"] == "CAPABILITY_MISSING"
        assert h.calls["submit"] == 0

    def test_require_alignment_false_tolerates_a_route_without_the_capability(self):
        """能力缺失只在被要求时才拒。"""
        h = Harness(route={
            "feasible": True, "capabilities": {"word_timestamps": False},
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64}})
        result = h.create(_body(require_alignment=False))
        assert h.state(result["speech_job_id"]) == "WAITING_PROVIDER"

    def test_infeasible_route_passes_degradation_options_through(self):
        """T31-11 F11：透传降级选项，不自作主张挑一个。"""
        h = Harness(route={
            "feasible": False, "degradation_options": ["lower_rate"],
            "reason_code": "NO_CAPACITY",
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64}})
        result = h.create()
        assert h.state(result["speech_job_id"]) == "REJECTED_LOCAL"
        assert result["degradation_options"] == ["lower_rate"]

    def test_residency_forbidden_is_rejected(self):
        """T31-50 / E31。"""
        h = Harness(route={
            "feasible": True, "capabilities": {"word_timestamps": True},
            "residency_allowed": False,
            "route_decision_ref": {"service_id": "S25", "id": "rd_1",
                                   "revision": 1},
            "policy_ref": {"service_id": "S25", "id": "routing_demo",
                           "revision": 1, "content_hash": "e" * 64}})
        result = h.create()
        assert result["reason_code"] == "RESIDENCY_FORBIDDEN"
        assert h.calls["submit"] == 0


# ================================================================== 动作族

class TestActionFamily:
    def test_lipsync_defect_cannot_request_speech(self):
        """T31-36 / E24：口型缺陷没有"台词念错了"的证据基础。"""
        h = Harness()
        body = _body()
        body["repair_action"] = {"action_id": "ra_1",
                                 "action_type": "re_synthesize_speech",
                                 "defect_type": "lipsync"}
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert (exc.value.status, exc.value.code) == (422,
                                                      "ACTION_TYPE_MISMATCH")
        assert exc.value.details["expected_type"] == "voice"

    def test_redo_lipsync_is_not_our_entry_point(self):
        """T31-37 / E24：那是 S32 的入口。"""
        h = Harness()
        body = _body()
        body["repair_action"] = {"action_id": "ra_2",
                                 "action_type": "redo_lipsync",
                                 "defect_type": "lipsync"}
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert exc.value.code == "ACTION_TYPE_MISMATCH"

    def test_lineage_mismatch_is_422(self):
        """T31-38 / E26。"""
        h = Harness()
        body = _body()
        body["repair_action"] = {"action_id": "ra_3",
                                 "action_type": "re_synthesize_speech",
                                 "defect_type": "voice",
                                 "shot_lineage_id": "lin_OTHER"}
        with pytest.raises(ServiceError) as exc:
            h.create(body)
        assert (exc.value.status, exc.value.code) == (422, "LINEAGE_MISMATCH")

    def test_voice_defect_is_accepted(self):
        h = Harness()
        body = _body()
        body["repair_action"] = {"action_id": "ra_4",
                                 "action_type": "re_synthesize_speech",
                                 "defect_type": "voice",
                                 "shot_lineage_id": "lin_ep3_sc3_02"}
        result = h.create(body)
        assert h.state(result["speech_job_id"]) == "WAITING_PROVIDER"


# ================================================================== 钱

class TestMoney:
    def test_submission_unknown_freezes_and_never_resubmits(self):
        """T31-28 / E16：**整个服务最重要的一个测试**。

        S26 的连接在提交时断掉 → `WAITING_UNKNOWN`。之后：重试被拒、
        对账查不到时不动、重启不做任何事。提交次数必须停在 1。
        """
        h = Harness()
        h.submit_result = ConnectionError("connection reset mid-POST")
        result = h.create()
        job_id = result["speech_job_id"]
        assert h.state(job_id) == "WAITING_UNKNOWN"
        assert h.calls["submit"] == 1

        # 重试 ⇒ 409，且提醒先对账
        with pytest.raises(ServiceError) as exc:
            h.service.retry_job(TENANT, job_id)
        assert (exc.value.status, exc.value.code) == (409,
                                                      "MUST_RECONCILE_FIRST")
        # 对账查不到 ⇒ 什么都不做
        out = h.service.reconcile_unknown(TENANT, job_id, None)
        assert out["resubmitted"] is False
        # 重启 ⇒ WAITING_UNKNOWN 是 noop 组
        assert job_id in h.service.recover(TENANT)["noop"]
        assert h.calls["submit"] == 1

    def test_unknown_while_same_fingerprint_arrives_does_not_double_submit(self):
        """T31-29 / E16：UNKNOWN 期间同指纹再来 → 不新开 job。"""
        h = Harness()
        h.submit_result = ConnectionError("timeout")
        first = h.create()
        h.submit_result = {"provider_job_id": "pj_2", "generation_job_id": "g2"}
        second = h.create(_body(command_id="cmd_2"), command_id="cmd_2")
        assert second.get("duplicate_suppressed") is True
        assert second["speech_job_id"] == first["speech_job_id"]
        assert h.calls["submit"] == 1

    def test_unknown_past_deadline_fails_without_resubmitting(self):
        """T31-30 / E16：UNKNOWN 熬到死线 → `FAILED(DEADLINE)`，**不重提**。

        这是 E16 里最容易写反的一条：结果不明时"到点放弃"看起来像失败，
        但重提才是真正的失败 —— 它可能已经收过单，重提就是双倍扣费。
        所以断言两件事：状态确实收了，提交次数一个都没涨。
        """
        h = Harness()
        h.submit_result = ConnectionError("connection reset mid-POST")
        result = h.create()
        job_id = result["speech_job_id"]
        assert h.state(job_id) == "WAITING_UNKNOWN"
        submits = h.calls["submit"]

        # 死线还没到：什么都不该动。
        quiet = h.service.sweep_deadlines(TENANT)
        assert quiet["expired"] == []
        assert h.state(job_id) == "WAITING_UNKNOWN"

        # 把死线拨到过去，模拟"UNKNOWN 挂了很久"。
        h.conn.execute("UPDATE speech_job SET deadline=? WHERE speech_job_id=?",
                       ("2000-01-01T00:00:00.000Z", job_id))
        out = h.service.sweep_deadlines(TENANT)
        assert out["expired"] == [job_id]
        assert out["resubmitted"] is False
        assert h.state(job_id) == "FAILED"
        row = h.one("SELECT failure_code FROM speech_job WHERE speech_job_id=?",
                    (job_id,))
        assert row["failure_code"] == "DEADLINE"
        assert h.calls["submit"] == submits == 1

        # 再扫一次：它已是终态，不会进第二遍，也不会被"顺手重提"。
        assert h.service.sweep_deadlines(TENANT)["expired"] == []
        assert h.calls["submit"] == 1

    def test_deadline_sweep_touches_only_waiting_unknown(self):
        """T31-30 的边界：其他状态各有出口，死线扫描不许动它们。"""
        h = Harness()
        ready = h.create()
        ingest(h, ready["speech_job_id"])
        assert h.state(ready["speech_job_id"]) == "READY"

        other = _body(command_id="c2")
        other["emotion"] = "冷静"  # 换情绪 → 新指纹，否则会命中已有 take 的复用
        waiting = h.create(other, command_id="c2")
        h.conn.execute("UPDATE speech_job SET state='WAITING_PROVIDER', "
                       "deadline=? WHERE speech_job_id=?",
                       ("2000-01-01T00:00:00.000Z", waiting["speech_job_id"]))
        out = h.service.sweep_deadlines(TENANT)
        assert out["expired"] == []
        assert h.state(ready["speech_job_id"]) == "READY"
        assert h.state(waiting["speech_job_id"]) == "WAITING_PROVIDER"

    def test_reconcile_confirmed_not_accepted_ends_in_reconciled_failed(self):
        """S26 明确说"没收到" → 允许失败，但**仍然不重提**。"""
        h = Harness()
        h.submit_result = ConnectionError("timeout")
        result = h.create()
        out = h.service.reconcile_unknown(
            TENANT, result["speech_job_id"],
            {"accepted": False, "reason_code": "NOT_ACCEPTED"})
        assert out["state"] == "RECONCILED_FAILED"
        assert out["resubmitted"] is False
        assert h.calls["submit"] == 1

    def test_budget_failure_is_terminal_and_not_downgraded(self):
        """T31-47 / E14：预算不足不能靠降采样率或缩短文本绕过。"""
        h = Harness()
        result = h.create()
        out = h.service.on_generation_state_changed(_s26_event(
            result["speech_job_id"], "REJECTED_BUDGET"))
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "BUDGET"
        # 输出规格一个字节没变
        job = h.one("SELECT * FROM speech_job WHERE speech_job_id=?",
                    (result["speech_job_id"],))
        assert (job["sample_rate_hz"], job["output_format"]) == (48000, "wav")

    def test_quota_failure_is_terminal(self):
        """T31-48 / E14。"""
        h = Harness()
        result = h.create()
        out = h.service.on_generation_state_changed(_s26_event(
            result["speech_job_id"], "REJECTED_QUOTA"))
        assert out["reason_code"] == "QUOTA"

    def test_account_unauthorized_does_not_switch_channel(self):
        """T31-49 / E18：不换渠道重试 —— 换渠道等于换声音实现。"""
        h = Harness()
        result = h.create()
        out = h.service.on_generation_state_changed(_s26_event(
            result["speech_job_id"], "PROVIDER_FAILED",
            reason="ACCOUNT_UNAUTHORIZED"))
        assert out["reason_code"] == "ACCOUNT_UNAUTHORIZED"
        assert h.calls["submit"] == 1
        assert h.calls["route"] == 1

    def test_cancel_that_cannot_be_confirmed_stays_unknown(self):
        """取消未确认不能假装取消。"""
        h = Harness()
        result = h.create()

        def boom(tenant, pid, reason):
            raise ConnectionError("S26 down")
        h.service.canceler = boom
        out = h.service.cancel_job(TENANT, result["speech_job_id"])
        assert out["state"] == "WAITING_UNKNOWN"
        assert out["canceled"] is False


def _s26_event(job_id, state, *, reason=None, attempt_id=None, tenant=TENANT):
    """S26 的**真实** payload：没有 `provider_job_id`，只有 `attempt_id`（C11）。"""
    return {
        "tenant_id": tenant,
        "data": {
            "schema_version": "2.0",
            "attempt_id": attempt_id or f"{job_id}:1",
            "state": state, "previous_state": "RUNNING",
            "business_attempt": 1, "network_retry": 0, "submit_attempts": 1,
            "provider": "prov_x", "channel": "ch_a",
            "node_id": "node_speech_ep3", "node_epoch": 3,
            "execution_fence": 9, "must_reconcile": state == "SUBMISSION_UNKNOWN",
            "reason_code": reason,
        },
    }


# ================================================================== 归档

class TestIngest:
    def test_absent_duration_blocks_in_ingesting(self):
        """T31-32 / E23：没有时长就阻塞，**不退化成假 READY**。"""
        h = Harness()
        job = h.create()
        out = ingest(h, job["speech_job_id"], duration=None)
        assert out["state"] == "INGESTING"
        assert out["waiting"] == "duration_ms"
        assert h.state(job["speech_job_id"]) == "INGESTING"
        assert h.all("SELECT * FROM outbox WHERE event_type=?",
                     ("production.speech.ready",)) == []

    def test_zero_duration_is_invalid_audio(self):
        """T31-33 / E23。"""
        h = Harness()
        job = h.create()
        out = ingest(h, job["speech_job_id"], duration=0)
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "INVALID_AUDIO"

    def test_hash_mismatch_fails(self):
        """T31-34 / E23：供应商报的 hash 与归档的不一致 → 不能交付。

        `FAILED(HASH_MISMATCH)` 而不是重归档：字节已经到了，只是不是我们
        要的那份。信任"先收下再说"会把一次传输损坏变成一条被剪辑的镜头。
        """
        h = Harness()
        job = h.create()
        h.service.on_generation_state_changed(
            _s26_event(job["speech_job_id"], "PROVIDER_SUCCEEDED",
                       attempt_id=f"{job['speech_job_id']}:1"))
        out = h.service.on_artifact_ingested({"tenant_id": TENANT, "data": {
            "speech_job_id": job["speech_job_id"],
            "artifact_ref": {"service_id": "S28", "id": "a", "revision": 1,
                             "content_hash": "9" * 64},
            "source": {"provider_job_id": "pj_1",
                       "content_hash": "8" * 64},
            "duration_ms": 3000}})
        assert out["reason_code"] == "HASH_MISMATCH"
        assert h.all("SELECT * FROM voice_take") == []

    def test_artifact_ref_must_name_s28(self):
        """T31-35 / E01：源服务是结构约束，不是约定。"""
        h = Harness()
        job = h.create()
        with pytest.raises(sqlite3.IntegrityError):
            h.conn.execute(
                "INSERT INTO voice_take (voice_take_id, tenant_id, "
                "speech_job_id, utterance_id, locale, artifact_ref, "
                "observed_duration_ms, container_format, sample_rate_hz, "
                "voice_binding_id, voice_binding_revision, lex, "
                "input_fingerprint, provider_job_id, attempt_id, rights_state,"
                " state, created_at) VALUES ('vt_bad','t',?, 'utt','zh-CN',"
                "'{\"service_id\":\"openai\",\"id\":\"x\",\"content_hash\":"
                "\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                "aaaaaaaa\"}', 3000, 'wav', 48000, 'v', 2, '{}', ?, 'pj', 'a',"
                "'usable','READY','now')",
                (job["speech_job_id"], "f" * 64))

    def test_ready_event_carries_measured_duration_and_null_span(self):
        """T31-27 / §4.5 / E24：只有容器实测时长；台词跨度等 S37。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"], duration=3280)
        event = h.one("SELECT * FROM outbox WHERE event_type=?",
                      ("production.speech.ready",))
        data = json.loads(event["payload"])["data"]
        assert data["audio_duration_ms"] == 3280
        assert data["dialogue_span_ms"] is None
        assert data["dialogue_span_source"] == "pending_s37"
        assert data["checkpoint"]["checkpoint_seq"] == 1
        assert data["rights_state"] == "usable"
        assert set(data) == {
            "schema_version", "speech_job_id", "voice_take_id",
            "utterance_id", "utterance_index", "shot_lineage_id", "locale",
            "text_hash", "artifact_ref", "audio_duration_ms",
            "dialogue_span_ms", "dialogue_span_source", "voice_binding",
            "lexicon_ref", "checkpoint", "require_alignment", "rights_state"}

    def test_provider_timestamps_never_enter_the_take(self):
        """T31-27 / E24：provider 时间戳不成第二个对齐权威。

        即使供应商的响应里真有 `word_timestamps`（很多 TTS 都能给），它
        也不许落库：S37 是 `AUD.TEXT.*` / `AUD.VOICE.*` 的唯一权威，而这里
        存下来的时间码没人会再校准，S37 换 ASR 模型重跑后它就静默过期了。
        """
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        h.service.on_artifact_ingested({"tenant_id": TENANT, "data": {
            "speech_job_id": job["speech_job_id"],
            "artifact_ref": {"service_id": "S28", "id": "audio_1",
                             "revision": 1, "content_hash": "9" * 64},
            "source": {"provider_job_id": "pj_1"},
            "duration_ms": 3280,
            "word_timestamps": [{"word": "你", "start_ms": 0, "end_ms": 90}]}})
        take = h.one("SELECT * FROM voice_take")
        assert take is not None
        assert "timestamps" not in take
        columns = {r["name"] for r in h.all("PRAGMA table_info(voice_take)")}
        assert not any("word" in c or "span" in c for c in columns)

    def test_ingest_gap_fails_without_resynthesizing(self):
        """T31-31 / E23：归档彻底失败 → 缺口可见，**不重新合成**。

        S26 §8.3 的第 5 步：重归档全试过还是拿不到成片，就 `INGEST_FAILED`。
        对本服务来说这意味着"钱花了、字节没有"，只能如实记 `FAILED`；
        重新提交才是灾难 —— 第二笔钱，而且同一 attempt 会出现两个成片。

        方案 T31-31 原话是"向 S26 要新 URL，不重新合成"。S26 只有
        in-process 的 `start_ingest(rearchive=True)`、没有 HTTP 路由，
        所以本服务改不了那一步；它保证的是**自己的**那一半：一次新的
        合成请求都不发（C12）。
        """
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"], duration=None)  # 停在 INGESTING
        assert h.state(job["speech_job_id"]) == "INGESTING"
        submits = h.calls["submit"]

        out = h.service.on_generation_state_changed(_s26_event(
            job["speech_job_id"], "ARCHIVED", reason="INGEST_FAILED"))
        assert out["state"] == "FAILED"
        assert out["reason_code"] == "ARTIFACT_UNAVAILABLE"
        assert out["resubmitted"] is False
        assert out["gap_visible"] is True
        assert h.state(job["speech_job_id"]) == "FAILED"
        assert h.calls["submit"] == submits == 1
        # 没有字节就没有 take，也没有 ready。
        assert h.all("SELECT * FROM voice_take") == []
        assert h.all("SELECT * FROM outbox WHERE event_type=?",
                     ("production.speech.ready",)) == []

    def test_ingest_gap_on_a_ready_job_is_ignored(self):
        """迟到的 `INGEST_FAILED` 不能把一个已 `READY` 的 take 作废。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        assert h.state(job["speech_job_id"]) == "READY"
        out = h.service.on_generation_state_changed(_s26_event(
            job["speech_job_id"], "ARCHIVED", reason="INGEST_FAILED"))
        assert out.get("ignored") is True
        assert h.state(job["speech_job_id"]) == "READY"
        assert h.one("SELECT rights_state FROM voice_take")["rights_state"] == \
            "usable"

    def test_alignment_completed_attaches_a_reference_only(self):
        """S37：只挂引用，不改状态，不复制数值。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        take = h.one("SELECT * FROM voice_take")
        before = h.state(job["speech_job_id"])
        out = h.service.on_alignment_completed({"tenant_id": TENANT, "data": {
            "voice_take_id": take["voice_take_id"],
            "report_id": "rep_1", "report_generation": 2,
            "coverage_ratio": 0.98}})
        assert out["updated"] is True
        refreshed = h.one("SELECT * FROM voice_take")
        ref = json.loads(refreshed["alignment_report_ref"])
        assert ref == {"service_id": "S37", "id": "rep_1", "revision": 2}
        assert h.state(job["speech_job_id"]) == before


# ================================================================== 台账

class TestLedger:
    def test_three_lines_plus_one_redone_makes_four_rows(self):
        """T31-39 / E26：一镜 3 句 + 1 次重做 = 4 行，`shot_lineage_id` 全同。

        重做那一次必须能被识别出来（`action='re_synthesize_speech'` + 非空
        `repair_action_id`），否则 S39 的跨 Run 累计会把修复也当成首轮。
        """
        h = Harness()
        h.artifact_present = False
        for index, utt in enumerate(("utt_a", "utt_b", "utt_c")):
            body = _body(command_id=f"c{index}")
            body["utterance"]["utterance_id"] = utt
            h.create(body, command_id=f"c{index}")
        # 把 utt_b 的 job 推到 FAILED，重做才不会撞上 open-uniq
        h.conn.execute(
            "UPDATE speech_job SET state='FAILED' WHERE utterance_id='utt_b' "
            "AND tenant_id=?", (TENANT,))
        body = _body(command_id="c_fix")
        body["utterance"]["utterance_id"] = "utt_b"
        body["repair_action"] = {"action_id": "ra_9",
                                 "action_type": "re_synthesize_speech",
                                 "defect_type": "voice"}
        h.create(body, command_id="c_fix")
        rows = h.all("SELECT * FROM speech_intent_ledger")
        assert len(rows) == 4
        assert {r["shot_lineage_id"] for r in rows} == {"lin_ep3_sc3_02"}
        fixes = [r for r in rows if r["action"] == "re_synthesize_speech"]
        assert len(fixes) == 1
        assert fixes[0]["repair_action_id"] == "ra_9"

    def test_ledger_key_is_shot_lineage_not_utterance(self):
        """T31-39 / E26：键是镜头血缘。从 utterance 派生会在合镜时算错上限。"""
        h = Harness()
        h.create()
        row = h.one("SELECT * FROM speech_intent_ledger")
        assert row["shot_lineage_id"] == "lin_ep3_sc3_02"
        assert row["utterance_id"] == "utt_3_2_02"

    def test_narration_has_a_null_lineage_and_is_a_known_blind_spot(self):
        """U4 / §8.2：旁白不进镜头级计数，但台账仍然写。"""
        h = Harness()
        body = _body()
        body["utterance"]["shot_lineage_id"] = None
        result = h.create(body)
        row = h.one("SELECT * FROM speech_intent_ledger")
        assert row["shot_lineage_id"] is None
        # 旁白不落检查点表（主键里它是 NOT NULL），但事件仍结构合法
        assert h.all("SELECT * FROM speech_checkpoint") == []
        ingest(h, result["speech_job_id"])
        event = h.one("SELECT * FROM outbox WHERE event_type=?",
                      ("production.speech.ready",))
        data = json.loads(event["payload"])["data"]
        assert data["shot_lineage_id"] is None
        assert data["checkpoint"]["checkpoint_seq"] >= 1

    def test_locale_change_makes_a_new_take_and_a_new_ledger_row(self):
        """T31-41 / E02：翻译配音是另一次生产，共享同一个镜头血缘。"""
        h = Harness()
        h.artifact_present = False
        h.create(_body(command_id="c_zh"))
        en = _body(command_id="c_en")
        en["utterance"]["locale"] = "en-US"
        h.create(en, command_id="c_en")
        rows = h.all("SELECT * FROM speech_intent_ledger")
        assert len(rows) == 2
        assert {r["locale"] for r in rows} == {"zh-CN", "en-US"}
        assert len({r["speech_job_id"] for r in rows}) == 2


# ================================================================== 并发

class _RacingConn:
    """在"读检查点"之后、写之前插一刀：别人把它推进了。

    SQLite 的 `BEGIN IMMEDIATE` 让同进程里没有真正的并发写者，所以竞态
    只能这样复现 —— 而这恰好是这道 CAS 存在的理由：它守的是"读与写之间
    事实变了"，不是"两个线程抢锁"。
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


class TestConcurrency:
    def test_checkpoint_cas_loses_cleanly(self):
        """T31-42 / E20：读完之后检查点被别人推进 → 409，**不覆盖**。

        覆盖的后果不是"少一个 take"，而是另一个 take 的推进被静默抹掉，
        而它的 `ready` 事件已经发出去了 —— 时间线里会指向一个检查点不再
        承认的 take。
        """
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        # 再做一个同镜同句、不同指纹的 job（换情绪），让 CAS 有得争
        other = _body(command_id="c2")
        other["emotion"] = "狂喜"
        second = h.create(other, command_id="c2")
        h.service.conn = _RacingConn(
            h.conn, on_sql="FROM speech_checkpoint",
            action=lambda: h.conn.execute(
                "UPDATE speech_checkpoint SET checkpoint_seq=99"))
        try:
            with pytest.raises(ServiceError) as exc:
                ingest(h, second["speech_job_id"])
            assert (exc.value.status, exc.value.code) == (
                409, "CHECKPOINT_CAS_FAILED")
            assert exc.value.retryable is True
        finally:
            h.service.conn = h.conn

    def test_void_wins_over_ready_in_the_same_transaction(self):
        """T31-43 / E30：撤销与 READY 并发 → void 优先，且只有一个结果。"""
        h = Harness()
        job = h.create()
        h.eligibility_result = _denied("AUTHORIZATION_REVOKED")
        out = ingest(h, job["speech_job_id"])
        assert out["rights_state"] == "void"
        assert out["ready_published"] is False
        # 且台账仍然记了这一次生产（钱花了就要记）
        assert len(h.all("SELECT * FROM speech_intent_ledger")) == 1


# ================================================================== 重启

class TestRestart:
    def test_checking_rights_is_rechecked_on_restart(self):
        """T31-44 / E13：崩溃前"我查过"不算数，重做。"""
        h = Harness()
        job = h.create()
        h.conn.execute(
            "UPDATE speech_job SET state='CHECKING_RIGHTS' WHERE "
            "speech_job_id=?", (job["speech_job_id"],))
        before = h.calls["eligibility"]
        h.eligibility_result = _denied("AUTHORIZATION_REVOKED")
        out = h.service.recover(TENANT)
        assert job["speech_job_id"] in out["rechecked"]
        assert h.calls["eligibility"] == before + 1
        assert h.state(job["speech_job_id"]) == "RIGHTS_BLOCKED"

    def test_ingesting_waits_for_a_fresh_duration_probe(self):
        """T31-45 / E13：不沿用崩溃前的时长，等 S28 重新报。"""
        h = Harness()
        job = h.create()
        h.conn.execute(
            "UPDATE speech_job SET state='INGESTING' WHERE speech_job_id=?",
            (job["speech_job_id"],))
        out = h.service.recover(TENANT)
        assert job["speech_job_id"] in out["waiting"]
        assert h.state(job["speech_job_id"]) == "INGESTING"

    def test_recheck_log_cannot_outlive_a_rolled_back_transition(self):
        """E39：`rights_recheck_log` 与状态转移同一事务（方案 §3.3）。

        这条要求在一个方向上才有可观测后果：日志不能比它背书的那次转移
        活得更久。让 `PREPARING` 那一步炸掉，日志必须跟着一起消失 —— 否则
        重启后审计读到"我复查过并放行了"，而实际什么都没发生。反方向
        （转移成功、日志丢了）由"没有日志行就没有 eligible 结论"的读法兜住。
        """
        h = Harness()
        real = h.service._transition

        def explode(job, to_state, **kwargs):
            if to_state == "PREPARING":
                raise RuntimeError("simulated crash after the log write")
            return real(job, to_state, **kwargs)

        h.service._transition = explode
        with pytest.raises(RuntimeError):
            h.create()
        assert h.all("SELECT * FROM rights_recheck_log") == []
        # job 行本身也一起回滚：没有"半个 job"可供后来的巡检捡起来。
        assert h.all("SELECT * FROM speech_job") == []
        assert h.calls["submit"] == 0

    def test_terminal_states_are_untouched_by_recovery(self):
        """T31-44b：`RIGHTS_BLOCKED` / `REJECTED_LOCAL` / `READY` 都属 noop 组。

        重启时对终态做任何复查都是浪费，且危险：一个"顺手再查一次然后
        解冻"的实现就是自动复活。
        """
        h = Harness()
        blocked = h.create()  # 先建好，稍后手动打到终态
        # 换情绪 → 新指纹，才是第二个 job（同指纹会被 open-uniq 合并成一个）
        other = _body(command_id="c2")
        other["emotion"] = "狂喜"
        ready = h.create(other, command_id="c2")
        assert ready["speech_job_id"] != blocked["speech_job_id"]
        ingest(h, ready["speech_job_id"])
        h.conn.execute(
            "UPDATE speech_job SET state='RIGHTS_BLOCKED' WHERE speech_job_id=?",
            (blocked["speech_job_id"],))
        calls = h.calls["eligibility"]
        out = h.service.recover(TENANT)
        assert blocked["speech_job_id"] in out["noop"]
        assert ready["speech_job_id"] in out["noop"]
        assert h.calls["eligibility"] == calls
        assert h.state(blocked["speech_job_id"]) == "RIGHTS_BLOCKED"
        assert h.state(ready["speech_job_id"]) == "READY"


# ================================================================== 租户

class TestTenant:
    def test_take_and_job_from_another_tenant_are_404(self):
        """T31-46 / E28。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        take = h.one("SELECT * FROM voice_take")
        with pytest.raises(ServiceError) as exc:
            h.service.get_job("t_beta", job["speech_job_id"])
        assert exc.value.status == 404
        with pytest.raises(ServiceError) as exc:
            h.service.get_take("t_beta", take["voice_take_id"])
        assert exc.value.status == 404

    def test_checkpoint_read_is_tenant_scoped(self):
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        mine = h.service.get_shot_checkpoint(TENANT, "lin_ep3_sc3_02")
        theirs = h.service.get_shot_checkpoint("t_beta", "lin_ep3_sc3_02")
        assert len(mine["items"]) == 1
        assert theirs["items"] == []


# ================================================================== 重放

class TestReplay:
    def test_replaying_ready_does_not_trigger_a_second_synthesis(self):
        """T31-51 / E32：事件重放不产生第二次付费。"""
        h = Harness()
        job = h.create()
        first = ingest(h, job["speech_job_id"])
        submits = h.calls["submit"]
        second = ingest(h, job["speech_job_id"])
        assert first["ready_published"] is True
        assert second.get("ignored") is True
        assert h.calls["submit"] == submits
        assert len(h.all("SELECT * FROM outbox WHERE event_type=?",
                         ("production.speech.ready",))) == 1
        assert len(h.all("SELECT * FROM voice_take")) == 1

    def test_out_of_order_provider_event_cannot_move_a_terminal_job(self):
        """乱序：READY 之后再到 RUNNING 必须无害。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        out = h.service.on_generation_state_changed(
            _s26_event(job["speech_job_id"], "RUNNING"))
        assert out["ignored"] is True
        assert h.state(job["speech_job_id"]) == "READY"

    def test_unmapped_provider_state_is_ignored_with_a_reason(self):
        h = Harness()
        job = h.create()
        out = h.service.on_generation_state_changed(
            _s26_event(job["speech_job_id"], "DISPATCH_AUTHORIZED"))
        assert out == {"ignored": True,
                       "reason": "unmapped_provider_state:DISPATCH_AUTHORIZED"}

    def test_repair_plan_never_creates_a_job(self):
        """§6.4：S39 的计划是只读输入，不是命令。"""
        h = Harness()
        out = h.service.on_repair_planned({"tenant_id": TENANT, "data": {
            "top_action": "re_synthesize_speech"}})
        assert out["would_execute"] is True
        assert h.all("SELECT * FROM speech_job") == []

    def test_manifest_commit_is_a_read_only_projection(self):
        """S08 的 `node_epoch` 变化不改本地状态。"""
        h = Harness()
        out = h.service.on_manifest_committed({"tenant_id": TENANT, "data": {
            "manifest_ref": {"id": "man_8", "revision": 13},
            "node_epoch": 99}})
        assert out["ignored"] is True

    def test_timeline_removal_supersedes_rather_than_voids(self):
        """S41：镜头删了是 SUPERSEDED（创作），不是 void（法律）。"""
        h = Harness()
        job = h.create()
        ingest(h, job["speech_job_id"])
        out = h.service.on_timeline_revision({"tenant_id": TENANT, "data": {
            "shot_lineage_id": "lin_ep3_sc3_02",
            "event_kind": "shot_removed"}})
        assert out["superseded"] == 1
        take = h.one("SELECT * FROM voice_take")
        assert (take["state"], take["rights_state"]) == ("SUPERSEDED", "usable")


# ================================================================== 契约

class TestContract:
    def test_generation_command_matches_v2_speech_input(self):
        """C3：`speech.input` 恰好 7 个键，多一个都会被 S26 的 schema 拒。"""
        h = Harness()
        h.create()
        command = h.last_command
        assert set(command["input"]) == {
            "text", "locale", "voice_binding", "emotion",
            "pronunciation_lexicon", "output_spec", "require_alignment"}
        assert set(command) >= {
            "schema_version", "context", "attempt_id", "variation_nonce",
            "idempotency_key", "modality", "input", "route_policy_ref",
            "template_ref", "budget_scope_id", "deadline"}
        assert command["modality"] == "speech"

    def test_signed_context_is_replayed_not_rebuilt(self):
        """`actor` / `trace_id` / `home_region` 只能来自签名。"""
        h = Harness()
        h.create()
        ctx = h.last_command["context"]
        assert ctx["trace_id"] == "trace_1"
        assert ctx["actor"]["principal_id"] == "u_1"
        assert ctx["home_region"] == "cn-east"
        assert len(ctx) == 18

    def test_attempt_id_and_idempotency_key_carry_the_business_attempt(self):
        h = Harness()
        result = h.create()
        command = h.last_command
        assert command["attempt_id"] == f"{result['speech_job_id']}:1"
        assert command["idempotency_key"].endswith(":1")

    def test_readiness_reports_the_contract_gaps(self):
        from speech_production.schema import CONTRACT_GAPS
        assert len(CONTRACT_GAPS) >= 10
        assert all(gap.startswith("S31 C") for gap in CONTRACT_GAPS)
