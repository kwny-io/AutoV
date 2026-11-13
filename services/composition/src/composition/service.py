"""S42 的内核：把 S41 定下的时间线变成一个**候选母版**（`MasterCandidate`）。

本服务是执行面，不是决策面。三条边界先写清楚，否则下面的代码会被误读：

1. **S42 不决定剪成什么样。** 分段边界、片段顺序、`output_plan`、`mix_plan`
   全部来自 S41（方案 §2.3）。这里没有"自己合并 clips 算分段"的分支 ——
   那会让同一条时间线在不同时间渲染出不同分段，缓存全失效。
2. **S42 不存字节、不存接受指针、不存金额。** 产物地址在 S28；当前有效
   母版在 S08；钱在 S23。`master_candidate.state` 里的
   `ACCEPTED_BY_SNAPSHOT` **只是投影**，由消费
   `snapshot.manifest.committed` 得出，权威永远在 S08。
3. **两个版本号是两列。** `execution_fence` 属于 run / branch，
   `node_epoch` 属于单个 `logical_node`。代码里不互相赋值、不比较、
   不换算、不 `max()`。段结果上报**分别**校验：fence 落后 → `STALE_FENCE`，
   epoch 落后 → `STALE_EPOCH`，各有各的理由。

## 内核做到哪一步

真实编码 / 拼接 / 响度测量都不在本进程（方案 §1：这里是控制面 + 数据面）。
内核实现的是**命令、状态机、幂等、事务、事件、判定**：

- 创建：读 S41 → L1 完整性 → 实时复查输入（S28 / S38）→ 解析规格 →
  申请 S22 许可（**先许可后落库**，§4.1 第 6 步）→ 事务写 job / segments / graph。
- 段：worker 通过 `record_segment_uploaded` 回报，**产物先在 S28 确认再写 ref**。
- 收尾：`finish_render_job` 做时长自检 → 整片响度门 → 写候选 → 发事件。
- 对账：`recover_expired_leases()` 把租约过期的 job 转 `ORPHANED`；
  `reconcile_render_job()` **查 S28** 决定 SUCCEEDED / 续跑 / 重排队。
- 混音：`finish_mix_job` 生成新候选，**video ref 逐字段继承**（§4.4）。

## 输入形状（全部由 `deps.py` 注入或构造参数给，测试里是闭包）

```python
timeline_source(tenant, ref) -> {"timeline_id","revision","content_hash",
                                 "duration_ticks","completeness",
                                 "formal_master_eligible","tick_rate","fps",
                                 "clips":[{"clip_id","rec_in_ticks","rec_out_ticks",
                                           "src_in_ticks","src_out_ticks",
                                           "src_artifact_ref"}],
                                 "output_plan":{...},"mix_plan":{...}}
timeline_diff(tenant, timeline_id, from_rev, to_rev)
                              -> {"affected_ranges":[{"start","end"}],"diff_mode"}
artifact_facts(tenant, ref)   -> {"artifact_id","revision","content_hash","state",
                                 "tombstoned","duration_ms","frame_rate",...}
subtitle_source(tenant, ref)  -> {"id","revision","content_hash"}
master_spec_source(tenant, ref) -> {"spec": {"container","video_codec",...}}
permit_source(tenant, request)  -> {"permit_id","pool_id","state",...}
usage_reporter(tenant, request) -> {...}
```

## 与契约的两处字面差异

- `post.render.failed` / `post.master.rendered` 里的 hash 是**裸 64 位 hex**
  （契约 `pattern: ^[0-9a-f]{64}$`），而方案示例写 `sha256:...`。本实现
  统一剥前缀后再写库与发事件（E41-HASH 的同一口径）。
- 事件信封的 SDK 列叫 `sequence`，契约叫 `aggregate_sequence`。只有拼
  payload 时做映射。

## 明确不做

- 不执行 ffmpeg，不测量响度（`finish_*` 接收实测值，没有就按"测不出 = 门失败"）。
- 不重建 `command_receipt`（SDK 的 `idempotency_record` 就是它）。
- 不枚举 S28 上"属于本 job 的对象"：契约没有 list-by-job 接口，对账只复核
  已记录的 ref（S42-C14）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Outbox, new_event

from . import schema

SOURCE = schema.SOURCE
SCHEMA_VERSION = schema.SCHEMA_VERSION
TOPIC = schema.TOPIC

#: `execution-context.v2` 的 18 个 required 字段。
REQUIRED_CONTEXT = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref",
    "actor", "command_id", "request_hash", "trace_id",
)

#: 段租约。方案 §7.2：TTL 90s、心跳 30s。
LEASE_SECONDS = 90

#: 资源秒估算：整片默认按 0.6 秒 GPU / 每秒成片；混音只吃 CPU。
GPU_MS_PER_OUTPUT_SECOND = 600
CPU_MS_PER_OUTPUT_SECOND = 120
MIX_CPU_MS_PER_OUTPUT_SECOND = 40

#: 两遍 loudnorm 的目标 LRA 缺省值 —— **只用于滤镜图占位**，不用于判定。
#: 判定只比 `master_spec` 的容差（§4.5）。
DEFAULT_TARGET_LRA = 11.0


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _after(seconds: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return schema.ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _b32(key_hex: str, length: int) -> str:
    raw = bytes.fromhex(key_hex)
    return base64.b32encode(raw).decode("ascii").lower().rstrip("=")[:length]


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


def stable_request_hash(body: dict) -> str:
    """命令幂等摘要：业务字段全进，trace 与时间戳不进。

    重投时 `trace_id` 变了，裸 `canonical_request_hash` 会算出不同摘要，
    把一次合法重投判成"同键不同 body"。
    """
    from idempotency_sdk import canonical_request_hash

    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    context = candidate.get("context")
    if isinstance(context, dict):
        candidate["context"] = {
            k: v for k, v in context.items()
            if k not in schema.VOLATILE_CONTEXT_KEYS}
    nested = candidate.get("execution_context")
    if isinstance(nested, dict):
        candidate["execution_context"] = {
            k: v for k, v in nested.items()
            if k not in schema.VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


def _hex64(value) -> str | None:
    """裸 64 位小写 hex，或 `sha256:` 前缀的同一摘要。"""
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be a non-empty string",
                           details={"field": field})
    return value


def _require_int(value, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an integer",
                           details={"field": field})
    if minimum is not None and value < minimum:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be >= {minimum}",
                           details={"field": field, "minimum": minimum})
    return value


def _require_object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be an object",
                           details={"field": field})
    return value


def _require_hash(value, field: str) -> str:
    digest = _hex64(value)
    if digest is None:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field} must be a 64-hex digest",
                           details={"field": field})
    return digest


def _require_ref(value, field: str, *, service_id: str | None = "S28") -> dict:
    """钉住一版的 artifact ref：`{service_id, id, revision, content_hash}`。

    **不接受"只给 id"**：没 revision 就没法保证渲染用的是哪一版，
    `stale_inputs` 也就无从判定（§4.6 最后一条）。
    """
    ref = _require_object(value, field)
    if service_id is not None and ref.get("service_id") != service_id:
        raise ServiceError(422, "SCHEMA_INVALID",
                           f"{field}.service_id must be {service_id!r}",
                           details={"field": field})
    return {
        "service_id": ref.get("service_id"),
        "id": _require_text(ref.get("id") or ref.get("artifact_id"),
                            f"{field}.id"),
        "revision": _require_int(ref.get("revision"), f"{field}.revision",
                                 minimum=1),
        "content_hash": _require_hash(ref.get("content_hash"),
                                      f"{field}.content_hash"),
    }


def _context(body: dict, tenant_id: str) -> dict:
    context = body.get("context") or body.get("execution_context")
    context = _require_object(context, "context")
    missing = [key for key in REQUIRED_CONTEXT if key not in context]
    if missing:
        raise ServiceError(422, "SCHEMA_INVALID",
                           "execution context is missing required keys",
                           details={"missing": missing})
    if context.get("tenant_id") != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "context tenant_id disagrees with the signed context")
    context["execution_fence"] = _require_int(
        context.get("execution_fence"), "context.execution_fence", minimum=1)
    context["node_epoch"] = _require_int(
        context.get("node_epoch"), "context.node_epoch", minimum=1)
    return context


# ---------------------------------------------------------------- 版本摘要
#
# 四个版本各有各的算法，混在一起就解释不了"这一版为什么变"（§2.1）。

def render_profile_hash(spec_ref: dict, profile: dict) -> str:
    return _sha256_hex(_jcs({
        "kind": "render_profile.v1",
        "master_spec_ref": spec_ref,
        "container": profile["container"],
        "video_codec": profile["video_codec"],
        "video_params": profile["video_params"],
        "audio_codec": profile["audio_codec"],
        "audio_params": profile["audio_params"],
        "color": profile.get("color"),
    }))


def render_env_hash(env: dict) -> str:
    """编码器 / 驱动 / 镜像版本。**进段缓存键，不进内容语义**（§2.1）。"""
    return _sha256_hex(_jcs({"kind": "render_env.v1", **env}))


def segment_key(*, segment: dict, input_hashes, profile_hash: str,
                env_hash: str, subtitle_hash: str | None) -> str:
    """§7.3 的缓存键。`subtitle_hash` 可以是 None（无字幕的渲染）。

    **不含 tenant**，所以查询必须带 `tenant_id` 谓词（§7.3 最后一条）。
    """
    return _sha256_hex(_jcs({
        "kind": "segment_key.v1",
        "timeline_segment": segment,
        "input_content_hashes": sorted(set(input_hashes)),
        "render_profile_hash": profile_hash,
        "render_env_hash": env_hash,
        "subtitle_artifact_hash": subtitle_hash,
    }))


def compute_graph_hash(graph: dict) -> str:
    """由节点与边确定性序列化得出（§2.2 规则 1）。`graph_hash` 自身不参与。"""
    payload = {k: v for k, v in graph.items() if k != "graph_hash"}
    return _sha256_hex(_jcs(payload))


# ---------------------------------------------------------------- 滤镜图
#
# 滤镜图是**数据**，不是渲染函数里的一段代码（§2.2）。三个诚实之处：
#
# 1. 节点 kind 取自 `schema.NODE_KINDS` 闭集；参数全部来自上游计划。
# 2. `sidechaincompress` 只带 `policy_ref`：`mix_plan` 里没有 threshold /
#    ratio 的数值，本服务不发明它们（S42-C10）。
# 3. `loudnorm` 是两遍（测量 → 应用），一次 loudnorm 的动态增益会让不同
#    片段响度不一致。

def _stem_gains(mix_plan: dict) -> dict:
    gains = mix_plan.get("stem_gains")
    return gains if isinstance(gains, dict) else {}


def build_render_graph(*, timeline: dict, profile: dict, mix_plan: dict,
                       stems: tuple[str, ...],
                       input_refs: list[dict]) -> dict:
    """正式渲染的滤镜图：视频链 + 音频链 + 两遍 loudnorm + 编码 + 封装。"""
    nodes: list[dict] = []
    edges: list[list[str]] = []
    seen: dict[str, str] = {}
    for ref in input_refs:
        node_id = "in_" + _b32(_sha256_hex(_jcs(ref)), 10)
        if node_id in seen:
            continue
        seen[node_id] = ref["id"]
        nodes.append({"id": node_id, "kind": "input", "artifact_ref": ref})

    gains = _stem_gains(mix_plan)
    audio_inputs: list[str] = []
    for role in stems:
        node_id = f"gain_{role}"
        gain = gains.get(role, 0.0)
        nodes.append({"id": node_id, "kind": "volume",
                      "role": role, "gain_db": float(gain)
                      if _finite(gain) else 0.0})
        audio_inputs.append(node_id)
        if role in ("dialogue", "voice") and mix_plan.get("ducking_policy_ref"):
            # C10：只带策略引用，数值由执行器按策略解析。
            duck_id = f"duck_{role}"
            nodes.append({"id": duck_id, "kind": "sidechaincompress",
                          "role": role,
                          "policy_ref": mix_plan["ducking_policy_ref"],
                          "key": "gain_bgm"})
            edges.append([node_id, duck_id])
            audio_inputs[-1] = duck_id
    if audio_inputs:
        nodes.append({"id": "mix_av", "kind": "amix",
                      "inputs": list(audio_inputs), "normalize": False})
        edges.extend([[src, "mix_av"] for src in audio_inputs])

    nodes.append({"id": "loud_pass1", "kind": "loudnorm", "mode": "measure",
                  "target_i": mix_plan.get("target_lufs"),
                  "target_tp": mix_plan.get("true_peak_ceiling"),
                  "target_lra": mix_plan.get("target_lra", DEFAULT_TARGET_LRA)})
    nodes.append({"id": "loud_pass2", "kind": "loudnorm", "mode": "apply",
                  "measured_from": "loud_pass1",
                  "target_i": mix_plan.get("target_lufs"),
                  "target_tp": mix_plan.get("true_peak_ceiling"),
                  "target_lra": mix_plan.get("target_lra", DEFAULT_TARGET_LRA)})
    if audio_inputs:
        edges.append(["mix_av", "loud_pass1"])
    edges.append(["loud_pass1", "loud_pass2"])

    # 视频链：每个 segment 一段 trim；分段边界来自 S41 的 output_plan。
    segments = timeline["output_plan"].get("segments") or []
    video_sources = []
    for index, clip in enumerate(timeline.get("clips") or [], start=1):
        ref = clip.get("src_artifact_ref")
        if not isinstance(ref, dict):
            continue
        node_id = "in_" + _b32(_sha256_hex(_jcs(ref)), 10)
        if node_id not in seen:
            seen[node_id] = ref["id"]
            nodes.append({"id": node_id, "kind": "input", "artifact_ref": ref})
        trim_id = f"trim_s{index}"
        nodes.append({"id": trim_id, "kind": "trim",
                      "src_in": int(clip.get("src_in_ticks") or 0),
                      "src_out": int(clip.get("src_out_ticks") or 0),
                      "rec_in": int(clip.get("rec_in_ticks") or 0)})
        edges.append([node_id, trim_id])
        video_sources.append(trim_id)
    if video_sources:
        nodes.append({"id": "concat_v", "kind": "concat",
                      "inputs": list(video_sources)})
        edges.extend([[src, "concat_v"] for src in video_sources])

    texture = {
        "id": "enc_v", "kind": "encode",
        "codec": profile["video_codec"],
        "params": profile["video_params"],
    }
    nodes.append(texture)
    if video_sources:
        edges.append(["concat_v", "enc_v"])
    nodes.append({"id": "mux", "kind": "mux",
                  "container": profile["container"],
                  "video_codec": profile["video_codec"],
                  "audio_codec": profile["audio_codec"],
                  "faststart": True})
    edges.append(["enc_v", "mux"])
    edges.append(["loud_pass2", "mux"])
    graph = {"graph_version": schema.GRAPH_VERSION,
             "segments": [int(s.get("segment_no") or i)
                          for i, s in enumerate(segments, start=1)],
             "nodes": nodes, "edges": edges}
    graph["graph_hash"] = compute_graph_hash(graph)
    return graph


def build_mix_graph(*, mix_plan: dict, stems: list[dict],
                    audio_codec: str | None) -> dict:
    """remix 的滤镜图：**只有音频**。没有 `encode` 时不补默认 AAC（C9）。"""
    nodes: list[dict] = []
    edges: list[list[str]] = []
    gains = _stem_gains(mix_plan)
    sources: list[str] = []
    for entry in stems:
        role = entry["role"]
        ref = entry["artifact_ref"]
        node_id = "in_" + _b32(_sha256_hex(_jcs(ref)), 10)
        nodes.append({"id": node_id, "kind": "input", "artifact_ref": ref})
        gain_id = f"gain_{role}"
        gain = gains.get(role, 0.0)
        nodes.append({"id": gain_id, "kind": "volume", "role": role,
                      "gain_db": float(gain) if _finite(gain) else 0.0})
        edges.append([node_id, gain_id])
        sources.append(gain_id)
        if role in ("dialogue", "voice") and mix_plan.get("ducking_policy_ref"):
            duck_id = f"duck_{role}"
            nodes.append({"id": duck_id, "kind": "sidechaincompress",
                          "role": role,
                          "policy_ref": mix_plan["ducking_policy_ref"],
                          "key": "gain_bgm"})
            edges.append([gain_id, duck_id])
            sources[-1] = duck_id
    nodes.append({"id": "mix_av", "kind": "amix", "inputs": list(sources),
                  "normalize": False})
    edges.extend([[src, "mix_av"] for src in sources])
    nodes.append({"id": "loud_pass1", "kind": "loudnorm", "mode": "measure",
                  "target_i": mix_plan.get("target_lufs"),
                  "target_tp": mix_plan.get("true_peak_ceiling"),
                  "target_lra": mix_plan.get("target_lra", DEFAULT_TARGET_LRA)})
    nodes.append({"id": "loud_pass2", "kind": "loudnorm", "mode": "apply",
                  "measured_from": "loud_pass1",
                  "target_i": mix_plan.get("target_lufs"),
                  "target_tp": mix_plan.get("true_peak_ceiling"),
                  "target_lra": mix_plan.get("target_lra", DEFAULT_TARGET_LRA)})
    edges.append(["mix_av", "loud_pass1"])
    edges.append(["loud_pass1", "loud_pass2"])
    if audio_codec:
        nodes.append({"id": "enc_a", "kind": "encode", "codec": audio_codec})
        edges.append(["loud_pass2", "enc_a"])
    graph = {"graph_version": schema.GRAPH_VERSION, "nodes": nodes,
             "edges": edges}
    graph["graph_hash"] = compute_graph_hash(graph)
    return graph


# ---------------------------------------------------------------- 响度门

def _usable_measurement(measurement: dict | None,
                        measurement_failed: bool) -> tuple[dict | None, bool]:
    """把"给了测量行但值不是有限数"降级为"测不出"。

    这必须在写库与发事件**之前**做：SQLite 会把 `float('nan')` 绑成
    NULL（`loudness_measurement.integrated_lufs` 是 NOT NULL，直接
    IntegrityError），而 `json.dumps` 会写出非法 JSON 的 `NaN`。两处都
    是"测不出"这件事的不同表现形态，语义上也确实是门失败（§4.5）。
    """
    if measurement is None:
        return None, measurement_failed
    if not _finite(measurement.get("integrated_lufs")) or \
            not _finite(measurement.get("true_peak_dbtp")):
        return None, True
    return measurement, measurement_failed


def loudness_gate(measurement: dict | None, spec: dict,
                  *, measurement_failed: bool = False) -> tuple[str, bool | None]:
    """`(gate, conforms_to_spec)`。§4.5 的四条规则全在这里：

    - `conforms_to_spec` 由**实测值**比较规格容差得出，不是编码器自报。
    - 不达标 → `FAILED`（候选仍生成，`deliverable=false`）。
    - **测不出 = 门失败**（`measurement_failed` 或值非有限）→ `FAILED`。
    - 完全没测（既没有值也没有失败标记）→ `NOT_RUN`，绝不是 `PASSED`。
    """
    loudness = spec.get("loudness") if isinstance(spec.get("loudness"),
                                                  dict) else {}
    target = loudness.get("target_lufs")
    tolerance = loudness.get("tolerance_lu")
    ceiling = loudness.get("true_peak_ceiling_dbtp")
    if measurement_failed:
        return "FAILED", False
    if measurement is None:
        return "NOT_RUN", None
    integrated = measurement.get("integrated_lufs")
    peak = measurement.get("true_peak_dbtp")
    if not _finite(integrated) or not _finite(peak):
        # 给了测量行却没有值：等同测不出。
        return "FAILED", False
    if not _finite(target) or not _finite(tolerance):
        # 规格没有容差 → 无法判定是否达标。拒绝猜一个容差。
        raise ServiceError(
            422, "MASTER_SPEC_REQUIRED",
            "master spec has no loudness tolerance; S42 will not judge "
            "conformance against an invented tolerance (plan §4.5)")
    conforms = abs(float(integrated) - float(target)) <= float(tolerance) \
        and float(peak) <= float(ceiling or target)
    return ("PASSED" if conforms else "FAILED"), conforms


# ---------------------------------------------------------------- 候选 manifest

def build_manifest(*, video_ref: dict, audio_ref: dict,
                   duration_ticks: int, frame_count: int,
                   subtitle_ref: dict | None, stems: list[dict],
                   timeline_id: str, timeline_revision: int,
                   mix_plan_hash: str, graph_hash: str,
                   render_env_hash: str, artifacts: list[dict]) -> dict:
    """候选 manifest。**这是交付证据的根**，`manifest_content_hash` 由它复算
    （C-S42-03：审计要能从 manifest 算出同一个 hash）。"""
    return {
        "manifest_version": "s42.candidate.v1",
        "timeline": {"timeline_id": timeline_id,
                     "revision": int(timeline_revision)},
        "duration_ticks": int(duration_ticks),
        "frame_count": int(frame_count),
        "video_artifact_ref": video_ref,
        "audio_artifact_ref": audio_ref,
        "subtitle_artifact_ref": subtitle_ref,
        "stems": stems,
        "mix_plan_hash": mix_plan_hash,
        "graph_hash": graph_hash,
        "render_env_hash": render_env_hash,
        "artifacts": artifacts,
    }


# ---------------------------------------------------------------- 服务

class CompositionService:
    """S42 的写路径。**没有字节入口，也没有接受指针入口。**

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox.__init__` 都含
    `executescript`，而它会隐式提交。两者必须在任何 `BEGIN IMMEDIATE`
    之前跑完，否则命令事务会被悄悄提交、ROLLBACK 失效。

    平台 SDK 的 `Inbox` 只写三列，而本服务的 inbox 表多三列元数据，同名
    不同形 —— **不用平台类**，自己写 `INSERT OR IGNORE`（与 S38 同一做法）。
    """

    def __init__(self, conn: sqlite3.Connection, *, timeline_source,
                 timeline_diff, artifact_facts, subtitle_source,
                 master_spec_source, permit_source, usage_reporter,
                 render_env: dict | None = None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema.SCHEMA)
        self.outbox = Outbox(self.conn)
        self.timeline_source = timeline_source
        self.timeline_diff = timeline_diff
        self.artifact_facts = artifact_facts
        self.subtitle_source = subtitle_source
        self.master_spec_source = master_spec_source
        self.permit_source = permit_source
        self.usage_reporter = usage_reporter
        #: 编码环境事实。未给出时用显式占位 —— 不假装知道驱动版本。
        self.render_env = dict(render_env or {
            "encoder": "unresolved",
            "encoder_version": "unresolved",
            "ffmpeg_version": "unresolved",
            "gpu_driver": "unresolved",
            "image_digest": "sha256:unresolved",
        })
        self.metrics: dict[str, int] = {}

    # ------------------------------------------------------------ 工具

    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _upstream(self, call, code: str):
        """上游不可达统一 503，**绝不用缓存结论兜底**。"""
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

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        """outbox 的 `sequence` **按 aggregate 单调**，不是全局（AGENTS.md）。

        `BEGIN IMMEDIATE` 期间读 MAX + 1 是安全的：同 aggregate 的两条事件
        不可能同时出现在两个事务里。
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM outbox "
            "WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row[0]) + 1

    def _traceparent(self, trace_id: str) -> str:
        digest = _sha256_hex(trace_id or "trace-unset")
        return "00-" + digest[:32] + "-" + digest[32:48] + "-01"

    def _emit(self, *, tenant_id: str, aggregate_id: str, event_type: str,
              subject: str, data: dict, context: dict) -> dict:
        envelope = new_event(
            source=SOURCE, type=event_type, subject=subject,
            tenant_id=tenant_id,
            home_cell=str(context.get("home_cell") or "cell_unknown"),
            aggregate_id=aggregate_id,
            sequence=self._next_sequence(tenant_id, aggregate_id),
            correlation_id=str(context.get("trace_id") or aggregate_id),
            traceparent=self._traceparent(str(context.get("trace_id") or "")),
            data=data, schema_version=SCHEMA_VERSION)
        self.outbox.append(envelope)
        return envelope.to_dict()

    def _not_found(self, what: str):
        """跨租户与不存在返回**完全相同**的响应（架构红线）。

        两个分支共用这一个构造函数，连 `details` 都是空 dict —— 任何差异
        都会让攻击者区分"这个 id 存在但属于别人"。
        """
        return ServiceError(404, "NOT_FOUND", f"{what} not found")

    # ------------------------------------------------------------ 上游读取

    def _read_timeline(self, tenant_id: str, ref: dict) -> dict:
        result = self._upstream(
            lambda: self.timeline_source(tenant_id, ref),
            "TIMELINE_UNAVAILABLE")
        if not isinstance(result, dict):
            raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                               "S41 returned no timeline revision")
        return result

    def _read_spec(self, tenant_id: str, ref: dict) -> dict:
        result = self._upstream(
            lambda: self.master_spec_source(tenant_id, ref),
            "MASTER_SPEC_UNAVAILABLE")
        spec = result.get("spec") if isinstance(result, dict) else None
        _require_object(spec, "master_spec")
        container = spec.get("container")
        if container not in schema.CONTAINERS:
            raise ServiceError(
                422, "UNSUPPORTED_SPEC",
                f"container {container!r} is not in the closed set",
                details={"container": container,
                         "allowed": list(schema.CONTAINERS)})
        video_codec = spec.get("video_codec")
        if video_codec not in schema.VIDEO_CODECS:
            raise ServiceError(
                422, "UNSUPPORTED_SPEC",
                f"video_codec {video_codec!r} is not in the closed set",
                details={"video_codec": video_codec,
                         "allowed": list(schema.VIDEO_CODECS)})
        audio_codec = spec.get("audio_codec")
        if audio_codec not in schema.AUDIO_CODECS:
            raise ServiceError(
                422, "UNSUPPORTED_SPEC",
                f"audio_codec {audio_codec!r} is not in the closed set",
                details={"audio_codec": audio_codec,
                         "allowed": list(schema.AUDIO_CODECS)})
        return spec

    def _check_artifacts(self, tenant_id: str, refs: list[dict]) -> dict:
        """逐条实时复查（L2）。返回 `{ref_json: facts}`；缺失的进 `missing`。

        404 从 S28 透传过来时转成"缺失"，**不是**"服务不可达"（E28-S42-02）。
        """
        facts: dict[str, dict] = {}
        missing: list[dict] = []
        for ref in refs:
            key = _jcs(ref)
            if key in facts:
                continue
            try:
                fact = self._upstream(
                    lambda ref=ref: self.artifact_facts(tenant_id, ref),
                    "ARCHIVE_UNAVAILABLE")
            except ServiceError as exc:
                if exc.status == 404:
                    facts[key] = {}
                    missing.append({"ref": ref, "reason": "NOT_FOUND"})
                    continue
                raise
            if not isinstance(fact, dict) or not fact:
                facts[key] = {}
                missing.append({"ref": ref, "reason": "NOT_FOUND"})
                continue
            facts[key] = fact
            if fact.get("tombstoned"):
                missing.append({"ref": ref, "reason": "TOMBSTONED"})
            elif fact.get("content_hash") != ref["content_hash"]:
                missing.append({"ref": ref, "reason": "HASH_MISMATCH",
                                "actual": fact.get("content_hash")})
        return {"facts": facts, "missing": missing}

    def _read_subtitle(self, tenant_id: str, ref: dict | None) -> dict | None:
        if ref is None:
            return None
        result = self._upstream(
            lambda: self.subtitle_source(tenant_id, ref),
            "SUBTITLE_SOURCE_UNAVAILABLE")
        if not isinstance(result, dict):
            raise ServiceError(502, "SUBTITLE_BAD_RESPONSE",
                               "S38 returned no subtitle revision")
        return result

    # ------------------------------------------------------------ 创建渲染

    def create_render_job(self, tenant_id: str, command_id: str,
                          body: dict, request_hash: str) -> dict:
        """§4.1 的 1–7 步。顺序不能换：**先申请许可，再落 job**。

        第 6 步在前是硬要求。反过来会造出一堆「排队但永远拿不到许可」的
        僵尸任务，而且它们还会出现在列表里、被重试、被取消。
        """
        context = _context(body, tenant_id)
        purpose = body.get("purpose")
        if purpose not in schema.PURPOSES:
            raise ServiceError(422, "SCHEMA_INVALID",
                               f"purpose must be one of {schema.PURPOSES}",
                               details={"purpose": purpose})
        timeline_ref = _require_object(body.get("timeline_ref"),
                                       "timeline_ref")
        timeline = self._read_timeline(tenant_id, timeline_ref)

        # --- L1：结构完整性（§4.2）。用 S41 **现算**的判定，不读 gaps 快照。
        if purpose == "formal" and not timeline.get("formal_master_eligible"):
            raise ServiceError(
                422, "INCOMPLETE_TIMELINE",
                "timeline revision is not eligible for a formal master",
                details={"completeness": timeline.get("completeness")})

        output_plan = _require_object(timeline.get("output_plan"),
                                      "output_plan")
        mix_plan = _require_object(timeline.get("mix_plan"), "mix_plan")
        if not _finite(mix_plan.get("target_lufs")):
            raise ServiceError(422, "MIX_TARGET_REQUIRED",
                               "mix_plan has no target_lufs freedom")
        segments = output_plan.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ServiceError(502, "TIMELINE_PLAN_UNAVAILABLE",
                               "output_plan.segments is empty")

        # --- 第 5 步：规格。缺 ref 就是 422，不默认 H.264（E37-S42-02）。
        spec_ref_raw = body.get("master_spec_ref")
        if spec_ref_raw is None:
            raise ServiceError(422, "MASTER_SPEC_REQUIRED",
                               "master_spec_ref is required; S42 will not "
                               "default to any encoder or loudness target")
        spec_ref = dict(_require_object(spec_ref_raw, "master_spec_ref"))
        spec_ref["revision"] = _require_int(spec_ref.get("revision"),
                                            "master_spec_ref.revision",
                                            minimum=1)
        spec_ref["id"] = _require_text(spec_ref.get("id"),
                                       "master_spec_ref.id")
        digest = _hex64(spec_ref.get("content_hash"))
        if digest is not None:
            spec_ref["content_hash"] = digest
        spec = self._read_spec(tenant_id, spec_ref)
        profile = {
            "container": spec["container"],
            "video_codec": spec["video_codec"],
            "video_params": spec.get("video_params") or {},
            "audio_codec": spec["audio_codec"],
            "audio_params": spec.get("audio_params") or {},
            "color": {k: spec.get(k) for k in
                      ("color_primaries", "transfer_function",
                       "matrix_coefficients")
                      if spec.get(k) is not None} or spec.get("color"),
        }
        profile_hash = render_profile_hash(spec_ref, profile)
        env_hash = render_env_hash(self.render_env)

        # --- 第 4 步：实时复查输入。**这是渲染前的复查，不是提交时的**。
        input_refs = []
        for clip in timeline.get("clips") or []:
            ref = clip.get("src_artifact_ref")
            if isinstance(ref, dict):
                input_refs.append(_require_ref(ref, "clips[].src_artifact_ref"))
        subtitle_ref = None
        if body.get("subtitle_ref") is not None:
            subtitle_ref = _require_ref(body["subtitle_ref"],
                                        "subtitle_ref", service_id="S38")
        requested_stems = body.get("requested_stems") or []
        if not isinstance(requested_stems, list):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "requested_stems must be an array")
        checked = self._check_artifacts(tenant_id, input_refs)
        if checked["missing"]:
            raise ServiceError(
                422, "INPUT_ARTIFACT_MISSING",
                "one or more render inputs are missing in S28",
                details={"missing": checked["missing"]})
        subtitle = self._read_subtitle(tenant_id, subtitle_ref)
        if subtitle_ref is not None:
            if subtitle.get("content_hash") != subtitle_ref["content_hash"]:
                raise ServiceError(
                    422, "INPUT_ARTIFACT_MISSING",
                    "subtitle revision is no longer the one requested",
                    details={"expected": subtitle_ref["content_hash"],
                             "actual": subtitle.get("content_hash")})

        # --- 分段：边界只来自 S41。segment_key 由 §7.3 的五个因子算出。
        input_hashes = [ref["content_hash"] for ref in input_refs]
        sub_hash = subtitle["content_hash"] if subtitle else None
        segment_rows = []
        for index, segment in enumerate(segments, start=1):
            rec_in = _require_int(int(segment.get("rec_in") or 0),
                                  f"segments[{index}].rec_in", minimum=0)
            rec_out = _require_int(int(segment.get("rec_out") or 0),
                                   f"segments[{index}].rec_out")
            if rec_out <= rec_in:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   "segment rec_out must exceed rec_in",
                                   details={"segment_no": index})
            key = segment_key(
                segment=segment, input_hashes=input_hashes,
                profile_hash=profile_hash, env_hash=env_hash,
                subtitle_hash=sub_hash)
            segment_rows.append({"segment_no": index, "rec_in": rec_in,
                                 "rec_out": rec_out, "segment_key": key})

        # --- 第 3 步的 diff：只用于抑制复用，缺失时全部重渲染。
        affected: list[dict] = []
        diff_mode = "unknown"
        previous = self._latest_segment_plan(tenant_id, timeline.get(
            "timeline_id"))
        if previous is not None and previous["revision"] != int(
                timeline.get("revision") or 0):
            diff = self._upstream(
                lambda: self.timeline_diff(
                    tenant_id, timeline["timeline_id"],
                    previous["revision"], int(timeline.get("revision") or 0)),
                "TIMELINE_DIFF_UNAVAILABLE")
            affected = diff.get("affected_ranges") or []
            diff_mode = diff.get("diff_mode") or "exact"

        graph = build_render_graph(
            timeline={**timeline, "output_plan": output_plan},
            profile=profile, mix_plan=mix_plan,
            stems=tuple(str(s) for s in requested_stems),
            input_refs=list({_jcs(r): r for r in input_refs}.values()))
        graph_hash = graph["graph_hash"]

        duration_ticks = int(timeline.get("duration_ticks") or 0) or max(
            row["rec_out"] for row in segment_rows)
        tick_rate = int(timeline.get("tick_rate") or 90000)
        gpu_ms = int(duration_ticks / tick_rate * 1000
                     * GPU_MS_PER_OUTPUT_SECOND / 1000)
        cpu_ms = int(duration_ticks / tick_rate * 1000
                     * CPU_MS_PER_OUTPUT_SECOND / 1000)
        resource_class = "cpu_media_second" if purpose != "formal" \
            else "gpu_second"

        # --- 第 6 步：许可。拒绝即 REJECTED，**不落 RUNNING**。
        permit = self._acquire_permit(
            tenant_id, context, resource_class=resource_class,
            amount_ms=gpu_ms if resource_class == "gpu_second" else cpu_ms,
            estimated_gpu_ms=gpu_ms, estimated_cpu_ms=cpu_ms)
        if permit.get("state") in ("DENIED", "REJECTED", "EXPIRED"):
            raise ServiceError(
                422, "BUDGET_DENIED",
                "the resource permit was refused; the job was not created",
                details={"permit_state": permit.get("state"),
                         "reason": permit.get("reason")})

        job_id = _new_id("render_job")
        now = _now()
        fps = timeline.get("fps") if isinstance(timeline.get("fps"),
                                                dict) else None
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO render_job (tenant_id, render_job_id, project_id,"
                " series_id, episode_id, branch_id, timeline_id,"
                " timeline_revision, timeline_content_hash, mix_plan_hash,"
                " render_profile_hash, render_env_hash, render_env, purpose,"
                " state, permit_ref, estimated_gpu_ms, consumed_gpu_ms,"
                " consumed_cpu_ms, retry_count, stale_inputs, subtitle_ref,"
                " subtitle_content_hash, master_spec_ref, duration_ticks,"
                " fps_num, fps_den, graph_hash, execution_fence, node_epoch,"
                " node_id, command_id, request_hash, execution_context,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,0,0,0,'[]',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, job_id, context["project_id"],
                 context["series_id"], context["episode_id"],
                 context["branch_id"], timeline["timeline_id"],
                 int(timeline["revision"]), timeline["content_hash"],
                 mix_plan["plan_hash"], profile_hash, env_hash,
                 _jcs(self.render_env), purpose, "QUEUED",
                 _jcs({"permit_id": permit.get("permit_id"),
                       "pool_id": permit.get("pool_id"),
                       "lease_expires_at": permit.get("lease_expires_at")}),
                 gpu_ms, _jcs(subtitle_ref) if subtitle_ref else None,
                 sub_hash, _jcs(spec_ref), duration_ticks,
                 int(fps["num"]) if fps and fps.get("num") else None,
                 int(fps["den"]) if fps and fps.get("den") else None,
                 graph_hash, context["execution_fence"],
                 context["node_epoch"], context["node_id"], command_id,
                 request_hash, _jcs(context), now, now))
            self.conn.execute(
                "INSERT OR REPLACE INTO render_profile (tenant_id,"
                " profile_hash, master_spec_ref, container, video_codec,"
                " video_params, audio_codec, audio_params, color_primaries,"
                " transfer_function, matrix_coefficients, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, profile_hash, _jcs(spec_ref),
                 profile["container"], profile["video_codec"],
                 _jcs(profile["video_params"]), profile["audio_codec"],
                 _jcs(profile["audio_params"]),
                 (profile.get("color") or {}).get("color_primaries"),
                 (profile.get("color") or {}).get("transfer_function"),
                 (profile.get("color") or {}).get("matrix_coefficients"), now))
            self.conn.execute(
                "INSERT OR REPLACE INTO filter_graph (tenant_id, graph_hash,"
                " graph_version, graph, built_from, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, graph_hash, schema.GRAPH_VERSION, _jcs(graph),
                 _jcs({"timeline_revision": int(timeline["revision"]),
                       "mix_plan_hash": mix_plan["plan_hash"],
                       "render_profile_hash": profile_hash,
                       "input_artifact_hashes": sorted(set(input_hashes)),
                       "subtitle_artifact_hash": sub_hash}), now))
            for row in segment_rows:
                state, output_ref = self._reusable_segment(
                    tenant_id, row["segment_key"], affected)
                self.conn.execute(
                    "INSERT INTO render_segment (tenant_id, render_job_id,"
                    " segment_no, rec_in_ticks, rec_out_ticks, segment_key,"
                    " input_refs, state, output_artifact_ref, attempts,"
                    " consumed_gpu_ms) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
                    (tenant_id, job_id, row["segment_no"], row["rec_in"],
                     row["rec_out"], row["segment_key"], _jcs(input_refs),
                     state, _jcs(output_ref) if output_ref else None))
                if state == "REUSED":
                    self._bump("segments_reused")
            self._bump("jobs_created")
        return {
            "render_job_id": job_id,
            "state": "QUEUED",
            "estimated_gpu_ms": gpu_ms,
            "estimated_cpu_ms": cpu_ms,
            "permit_ref": {"permit_id": permit.get("permit_id")},
            "operation_id": job_id,
            # C6：没有段级进度事件，实时面只是命名占位。
            "realtime_ref": {
                "run_id": context.get("trace_id"),
                "sse_topic": f"composition:render_job:{job_id}"},
            "diff_mode": diff_mode,
            "segments": len(segment_rows),
        }

    def _acquire_permit(self, tenant_id: str, context: dict, *,
                        resource_class: str, amount_ms: int,
                        estimated_gpu_ms: int,
                        estimated_cpu_ms: int) -> dict:
        request = {
            "resource_class": resource_class,
            "requested_ms": int(amount_ms),
            "estimated_gpu_ms": int(estimated_gpu_ms),
            "estimated_cpu_ms": int(estimated_cpu_ms),
            # S22 的 acquire 需要这三个坐标；S42 没有预约，于是显式派生：
            # `attempt_id` 由命令派生（重投命中幂等，不会重复申请），
            # `reservation_*` 只做标识，不用它记账（本服务不存金额）。
            "attempt_id": f"{context.get('command_id')}:render",
            "reservation_ref": {
                "service_id": "S42", "id": str(context.get("command_id"))},
            "reservation_amount_micro": 0,
            "route_decision_ref": context.get("policy_ref") or {
                "service_id": "S02", "id": "policy_unresolved"},
            "execution_fence": context.get("execution_fence"),
            "node_id": context.get("node_id"),
            "node_epoch": context.get("node_epoch"),
        }
        result = self._upstream(
            lambda: self.permit_source(tenant_id, request), "PERMIT_UNAVAILABLE")
        return result if isinstance(result, dict) else {}

    def _latest_segment_plan(self, tenant_id: str,
                             timeline_id: str) -> dict | None:
        """上一个已落库的 revision，以及它当时的段边界（用于 diff 的上界）。"""
        row = self.conn.execute(
            "SELECT timeline_revision, timeline_id FROM render_job"
            " WHERE tenant_id=? AND timeline_id=?"
            " ORDER BY timeline_revision DESC, created_at DESC LIMIT 1",
            (tenant_id, timeline_id)).fetchone()
        if row is None:
            return None
        return {"revision": int(row["timeline_revision"])}

    def _reusable_segment(self, tenant_id: str, key: str,
                          affected: list[dict]) -> tuple[str, dict | None]:
        """缓存命中**且**不与受影响区间交叠才复用（§4.3 三条注意）。

        缓存查询必须带 `tenant_id` 谓词：`segment_key` 本身不含租户。
        """
        row = self.conn.execute(
            "SELECT output_artifact_ref, rec_in_ticks, rec_out_ticks"
            " FROM render_segment WHERE tenant_id=? AND segment_key=?"
            " AND state IN ('DONE','REUSED') AND output_artifact_ref IS NOT NULL"
            " LIMIT 1", (tenant_id, key)).fetchone()
        if row is None:
            return "PENDING", None
        candidate = _loads(row["output_artifact_ref"], None)
        if not isinstance(candidate, dict):
            return "PENDING", None
        for span in affected or []:
            start = span.get("start")
            end = span.get("end")
            if not _finite(start) or not _finite(end):
                # 区间形状不认识 → 保守：不复用。
                return "PENDING", None
            if int(start) < int(row["rec_out_ticks"]) \
                    and int(end) > int(row["rec_in_ticks"]):
                return "PENDING", None
        return "REUSED", candidate

    # ------------------------------------------------------------ 创建混音

    def create_mix_job(self, tenant_id: str, command_id: str, body: dict,
                       request_hash: str) -> dict:
        """§4.4 的 remix。三条硬约束都在这里：

        1. **video 流逐字段复用**：`source_video_ref` 原样进候选，不重编码。
        2. **stems 必须可用**：缺一个就 `STEMS_UNAVAILABLE`（不是"猜着分离"）。
        3. **不需要视频许可**：只吃 `cpu_media_second`。
        """
        context = _context(body, tenant_id)
        source_video = _require_ref(body.get("source_video_ref"),
                                    "source_video_ref")
        stems_raw = body.get("stems")
        if not isinstance(stems_raw, list) or not stems_raw:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "stems must be a non-empty array")
        stems: list[dict] = []
        seen_roles: set[str] = set()
        for index, entry in enumerate(stems_raw):
            entry = _require_object(entry, f"stems[{index}]")
            role = entry.get("role")
            if role not in ("dialogue", "sfx", "bgm", "voice"):
                raise ServiceError(
                    422, "SCHEMA_INVALID",
                    f"stems[{index}].role is not a stem role",
                    details={"role": role, "allowed": ["dialogue", "sfx",
                                                       "bgm", "voice"]})
            if role in seen_roles:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"stems has duplicate role {role!r}")
            seen_roles.add(role)
            stems.append({"role": role,
                          "artifact_ref": _require_ref(
                              entry.get("artifact_ref"),
                              f"stems[{index}].artifact_ref")})
        mix_plan_hash = _require_hash(body.get("mix_plan_hash"),
                                      "mix_plan_hash")
        target_lufs = body.get("target_lufs")
        true_peak = body.get("true_peak_ceiling")
        if not _finite(target_lufs):
            raise ServiceError(422, "MIX_TARGET_REQUIRED",
                               "target_lufs is required")
        if not _finite(true_peak):
            raise ServiceError(422, "MIX_TARGET_REQUIRED",
                               "true_peak_ceiling is required")
        reason = body.get("reason")
        if reason not in schema.MIX_REASONS:
            raise ServiceError(422, "SCHEMA_INVALID",
                               f"reason must be one of {schema.MIX_REASONS}")
        repair_ref = body.get("repair_plan_ref")
        if reason == "remix_defect" and repair_ref is None:
            raise ServiceError(
                422, "SCHEMA_INVALID",
                "remix_defect requires repair_plan_ref (plan §5 constraint)")
        if repair_ref is not None:
            repair_ref = _require_ref(repair_ref, "repair_plan_ref",
                                      service_id="S39")

        # --- 实时复查：video + 每个 stem。缺一个就停，不尝试从成品分离。
        checked = self._check_artifacts(
            tenant_id, [source_video] + [s["artifact_ref"] for s in stems])
        if checked["missing"]:
            raise ServiceError(
                422, "STEMS_UNAVAILABLE",
                "one or more stems (or the source video) are not available; "
                "S42 will not separate stems from a finished mix (plan §4.4)",
                details={"missing": checked["missing"]})

        # --- C11：时间线坐标从「持有同一 source_video_ref 的既有候选」继承。
        basis = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=?"
            " AND json_extract(video_artifact_ref, '$.id')=?"
            " AND json_extract(video_artifact_ref, '$.revision')=?"
            " AND json_extract(video_artifact_ref, '$.content_hash')=?"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, source_video["id"], source_video["revision"],
             source_video["content_hash"])).fetchone()
        if basis is None:
            raise ServiceError(
                422, "REMASTER_BASIS_MISSING",
                "no existing candidate holds this video stream; S42 will not "
                "invent a timeline revision or fence for the remix (S42-C11)",
                details={"source_video_ref": source_video})

        spec = self._spec_for_context(tenant_id, basis)
        mix_plan = {
            "target_lufs": float(target_lufs),
            "true_peak_ceiling": float(true_peak),
            "ducking_policy_ref": body.get("ducking_policy_ref"),
            "stem_gains": body.get("stem_gains") or {},
        }
        # C9：body 没给 codec 时不放 encode 节点，绝不默认 AAC。
        graph = build_mix_graph(mix_plan=mix_plan, stems=stems,
                                audio_codec=body.get("audio_codec"))
        graph_hash = graph["graph_hash"]

        duration_ticks = int(basis["duration_ticks"])
        mix_cpu_ms = int(duration_ticks / 90000 * 1000
                         * MIX_CPU_MS_PER_OUTPUT_SECOND / 1000)
        permit = self._acquire_permit(
            tenant_id, context, resource_class="cpu_media_second",
            amount_ms=mix_cpu_ms, estimated_gpu_ms=0,
            estimated_cpu_ms=mix_cpu_ms)
        if permit.get("state") in ("DENIED", "REJECTED", "EXPIRED"):
            raise ServiceError(
                422, "BUDGET_DENIED",
                "the resource permit was refused; the mix job was not created",
                details={"permit_state": permit.get("state")})

        mix_job_id = _new_id("mix_job")
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO mix_job (tenant_id, mix_job_id, episode_id,"
                " branch_id, source_video_ref, timeline_id, timeline_revision,"
                " basis_candidate_id, stems, mix_plan_hash, graph_hash,"
                " target_lufs, true_peak_ceiling, reason, repair_plan_ref,"
                " state, consumed_cpu_ms, execution_fence, node_epoch, node_id,"
                " command_id, request_hash, execution_context, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CREATED',0,?,?,?,?,?,?,?)",
                (tenant_id, mix_job_id, context["episode_id"],
                 context["branch_id"], _jcs(source_video),
                 basis["timeline_id"], int(basis["timeline_revision"]),
                 basis["candidate_id"], _jcs(stems), mix_plan_hash,
                 graph_hash, float(target_lufs), float(true_peak), reason,
                 _jcs(repair_ref) if repair_ref else None,
                 context["execution_fence"], context["node_epoch"],
                 context["node_id"], command_id, request_hash,
                 _jcs(context), now))
            self.conn.execute(
                "INSERT OR REPLACE INTO filter_graph (tenant_id, graph_hash,"
                " graph_version, graph, built_from, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, graph_hash, schema.GRAPH_VERSION, _jcs(graph),
                 _jcs({"mix_plan_hash": mix_plan_hash,
                       "input_artifact_hashes": sorted(
                           [source_video["content_hash"]]
                           + [s["artifact_ref"]["content_hash"]
                              for s in stems]),
                       "basis_candidate_id": basis["candidate_id"]}), now))
            self._bump("mix_jobs_created")
        return {
            "mix_job_id": mix_job_id,
            "state": "CREATED",
            "reason": reason,
            "estimated_cpu_ms": mix_cpu_ms,
            "permit_ref": {"permit_id": permit.get("permit_id")},
            "operation_id": mix_job_id,
            "realtime_ref": {"run_id": context.get("trace_id"),
                             "sse_topic": f"composition:mix_job:{mix_job_id}"},
            "basis_candidate_id": basis["candidate_id"],
            "spec_available": spec is not None,
        }

    def _spec_for_context(self, tenant_id: str, basis_row) -> dict | None:
        """remix 沿用上一版的规格（同一 video 流，同一交付规格）。"""
        manifest = _loads(basis_row["manifest"], {})
        # `build_manifest` 的顶层没有 `master_spec_ref`；规格 ref 是
        # `finish_render_job` 经 `extra_manifest` 放进 `spec_ref` 的。
        ref = manifest.get("spec_ref") if isinstance(manifest, dict) else None
        if not isinstance(ref, dict):
            return None
        try:
            return self._read_spec(tenant_id, ref)
        except ServiceError as exc:
            if exc.status in (404, 422):
                return None
            raise

    # ------------------------------------------------------------ 段回报

    def claim_render_job(self, tenant_id: str, job_id: str,
                         worker_id: str) -> dict:
        """worker 认领。`ADMITTED` 支线在这里落地；`QUEUED` 直接认领也允许
        （单进程执行器没有单独的调度器）。"""
        job = self._job(tenant_id, job_id)
        if job["state"] in schema.TERMINAL_JOB_STATES:
            raise ServiceError(409, "JOB_TERMINAL",
                               f"job is {job['state']}",
                               details={"state": job["state"]})
        if job["state"] == "CANCELLING":
            raise ServiceError(409, "JOB_TERMINAL",
                               "job is cancelling; no new work is accepted",
                               details={"state": job["state"]})
        lease = _after(LEASE_SECONDS)
        with Transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE render_job SET state='RUNNING', worker_id=?,"
                " lease_expires_at=?, updated_at=? WHERE tenant_id=?"
                " AND render_job_id=? AND worker_id IS NULL"
                " AND state IN ('QUEUED','ADMITTED')",
                (worker_id, lease, _now(), tenant_id, job_id))
            if cur.rowcount != 1:
                raise ServiceError(
                    409, "CLAIM_RACE",
                    "another worker holds this job",
                    retryable=True)
        return self.get_render_job(tenant_id, job_id)

    def record_segment_uploaded(self, tenant_id: str, job_id: str, *,
                                segment_no: int, output_ref: dict,
                                consumed_gpu_ms: int = 0,
                                execution_fence: int,
                                node_epoch: int) -> dict:
        """段产物回报。**ref 只在 S28 确认之后才写**（§4.6 不变量）。

        fence / epoch **分别**校验，两个不同的错误码 —— 合并成一个
        `STALE` 会让"哪个作用域旧了"无法判断。旧 epoch 的结果永远不会推进
        任何东西，这里连段状态都不改。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] in schema.TERMINAL_JOB_STATES:
            raise ServiceError(
                409, "JOB_TERMINAL",
                "artifacts from a terminal job are discarded (plan §3.4)",
                details={"state": job["state"]})
        if int(execution_fence) < int(job["execution_fence"]):
            raise ServiceError(
                409, "STALE_FENCE",
                "the reporting run/branch fence is behind the job's",
                details={"job_execution_fence": job["execution_fence"],
                         "reported_execution_fence": int(execution_fence)})
        if int(node_epoch) < int(job["node_epoch"]):
            raise ServiceError(
                409, "STALE_EPOCH",
                "the reporting node epoch is behind the job's; old-epoch "
                "results never advance anything (AGENTS.md)",
                details={"job_node_epoch": job["node_epoch"],
                         "reported_node_epoch": int(node_epoch)})
        if output_ref is None:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "output_ref is required")
        ref = _require_ref(output_ref, "output_ref")
        try:
            fact = self._upstream(
                lambda: self.artifact_facts(tenant_id, ref),
                "ARCHIVE_UNAVAILABLE")
        except ServiceError as exc:
            if exc.status == 404:
                # S28 不认识这个 ref（不存在 / 跨租户 / 已被删）。这不是
                # "job 不存在"，而是"这段产物没有被确认" —— 与 hash 不符
                # 是同一件事的两种形态，给同一个 502。原样透传 404 会让
                # worker 把"产物没落库"读成"job 被删了"。
                fact = None
            else:
                raise
        if not isinstance(fact, dict) or fact.get("content_hash") != \
                ref["content_hash"]:
            raise ServiceError(
                502, "ARCHIVE_MISMATCH",
                "S28 does not confirm the reported segment artifact")
        now = _now()
        with Transaction(self.conn):
            cur = self.conn.execute(
                "UPDATE render_segment SET state='DONE', output_artifact_ref=?,"
                " attempts=attempts+1, consumed_gpu_ms=consumed_gpu_ms,"
                " finished_at=? WHERE tenant_id=? AND render_job_id=?"
                " AND segment_no=? AND state IN ('PENDING','RUNNING','FAILED')",
                (_jcs(ref), now, tenant_id, job_id, int(segment_no)))
            if cur.rowcount != 1:
                raise ServiceError(
                    409, "SEGMENT_TERMINAL",
                    "segment is already terminal or does not exist",
                    details={"segment_no": int(segment_no)})
            self.conn.execute(
                "UPDATE render_job SET consumed_gpu_ms=consumed_gpu_ms+?,"
                " updated_at=? WHERE tenant_id=? AND render_job_id=?",
                (int(consumed_gpu_ms), now, tenant_id, job_id))
            if int(consumed_gpu_ms) > 0:
                self._record_usage(tenant_id, job_id=job_id,
                                   resource_class="gpu_second",
                                   quantity_ms=int(consumed_gpu_ms),
                                   usage_key=f"{job_id}:{int(segment_no)}")
        return {"segment_no": int(segment_no), "state": "DONE",
                "output_artifact_ref": ref}

    def mark_segment_retry(self, tenant_id: str, job_id: str, *,
                           segment_no: int, attempts: int,
                           consumed_gpu_ms: int = 0) -> dict:
        """段失败回报。§4.6 的上限在这里拒绝（S42-C15）。"""
        job = self._job(tenant_id, job_id)
        if job["state"] in schema.TERMINAL_JOB_STATES:
            raise ServiceError(409, "JOB_TERMINAL",
                               f"job is {job['state']}")
        if int(attempts) >= 3:
            self._fail_job(tenant_id, job_id, "SEGMENT_RENDER_FAILED",
                           {"segment_no": int(segment_no),
                            "attempts": int(attempts)})
            return {"segment_no": int(segment_no), "state": "FAILED",
                    "job_state": "FAILED"}
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE render_segment SET state='FAILED', attempts=?,"
                " consumed_gpu_ms=consumed_gpu_ms+?, finished_at=?"
                " WHERE tenant_id=? AND render_job_id=? AND segment_no=?"
                " AND state IN ('PENDING','RUNNING','FAILED')",
                (int(attempts), int(consumed_gpu_ms), now, tenant_id, job_id,
                 int(segment_no)))
            self.conn.execute(
                "UPDATE render_job SET retry_count=retry_count+1,"
                " consumed_gpu_ms=consumed_gpu_ms+?, updated_at=?"
                " WHERE tenant_id=? AND render_job_id=?",
                (int(consumed_gpu_ms), now, tenant_id, job_id))
            if int(consumed_gpu_ms) > 0:
                self._record_usage(tenant_id, job_id=job_id,
                                   resource_class="gpu_second",
                                   quantity_ms=int(consumed_gpu_ms),
                                   usage_key=f"{job_id}:{int(segment_no)}")
        return {"segment_no": int(segment_no), "state": "FAILED",
                "job_state": job["state"]}

    def _record_usage(self, tenant_id: str, *, job_id: str | None = None,
                      mix_job_id: str | None = None,
                      resource_class: str, quantity_ms: int,
                      usage_key: str) -> str:
        """用量事实。**不是账本**：没有余额、没有金额、没有累计应收。"""
        usage_id = "us_" + _b32(_sha256_hex(
            _jcs({"tenant": tenant_id, "key": usage_key})), 26)
        self.conn.execute(
            "INSERT OR REPLACE INTO render_usage (tenant_id, usage_id,"
            " render_job_id, mix_job_id, resource_class, quantity_ms,"
            " occurred_at, reported_to_s23) VALUES (?,?,?,?,?,?,?,0)",
            (tenant_id, usage_id, job_id, mix_job_id, resource_class,
             int(quantity_ms), _now()))
        return usage_id

    # ------------------------------------------------------------ 收尾

    def finish_render_job(self, tenant_id: str, job_id: str, *,
                          video_ref: dict, audio_ref: dict,
                          measurement: dict | None = None,
                          measurement_failed: bool = False,
                          frame_count: int | None = None,
                          small: dict | None = None) -> dict:
        """§4.1 第 10–11 步：自检 → 响度门 → 候选 → 事件。

        `small` 是可选的小产物（thumbnail / subtitle / stems）：
        `{"role": ref, ...}`，逐个进 `master_candidate_artifact`。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] in schema.TERMINAL_JOB_STATES:
            raise ServiceError(409, "JOB_TERMINAL", f"job is {job['state']}",
                               details={"state": job["state"]})
        segments = self.conn.execute(
            "SELECT * FROM render_segment WHERE tenant_id=? AND"
            " render_job_id=? ORDER BY segment_no",
            (tenant_id, job_id)).fetchall()
        pending = [int(s["segment_no"]) for s in segments
                   if s["state"] not in schema.SEGMENT_TERMINAL_STATES]
        if pending:
            raise ServiceError(
                409, "SEGMENTS_INCOMPLETE",
                "not every segment has a confirmed artifact",
                details={"pending_segments": pending})

        video = _require_ref(video_ref, "video_ref")
        audio = _require_ref(audio_ref, "audio_ref")
        checked = self._check_artifacts(tenant_id, [video, audio])
        if checked["missing"]:
            raise ServiceError(
                422, "INPUT_ARTIFACT_MISSING",
                "the rendered output is not confirmed in S28",
                details={"missing": checked["missing"]})

        # --- 时长自检（§4.6：拼接后时长必须等于 EDL 时长）。
        #
        # `duration_ticks` 在创建时就等于 S41 的整片时长，段的分界也来自
        # S41；这里能自检的是「段覆盖率」与「帧数」。**不假设 fps**：
        # fps 只来自 S41 的 payload，读不到就只报 fps_unavailable（C12）。
        duration_ticks = int(job["duration_ticks"])
        covered = max(int(s["rec_out_ticks"]) for s in segments)
        checks = {"duration": "compared",
                  "segment_coverage_ticks": covered}
        if covered != duration_ticks:
            self._fail_job(
                tenant_id, job_id, "DURATION_MISMATCH",
                {"duration_ticks": duration_ticks,
                 "segment_coverage_ticks": covered,
                 "checks_run": ["segment_coverage"]})
            raise ServiceError(
                422, "DURATION_MISMATCH",
                "segments do not cover the timeline duration; no candidate "
                "is produced (plan §4.6)",
                details={"duration_ticks": duration_ticks,
                         "segment_coverage_ticks": covered})
        if frame_count is not None:
            expected_frames = None
            fps_num = job["fps_num"]
            fps_den = job["fps_den"]
            if fps_num and fps_den:
                expected_frames = round(
                    duration_ticks / 90000 * int(fps_num) / int(fps_den))
            if expected_frames is not None and int(frame_count) != expected_frames:
                self._fail_job(
                    tenant_id, job_id, "DURATION_MISMATCH",
                    {"expected_frames": expected_frames,
                     "actual_frames": int(frame_count),
                     "duration_ticks": duration_ticks,
                     "checks_run": ["frame_count"]})
                raise ServiceError(
                    422, "DURATION_MISMATCH",
                    "concatenated frame count does not match the EDL; no "
                    "candidate is produced (plan §4.6)",
                    details={"expected_frames": expected_frames,
                             "actual_frames": int(frame_count)})
            checks["frame_count"] = "compared" if expected_frames is not None \
                else "fps_unavailable"

        # --- 响度门（§4.5，整片测量）。
        spec_ref = _loads(job["master_spec_ref"], {})
        spec = self._read_spec(tenant_id, spec_ref)
        measurement, measurement_failed = _usable_measurement(
            measurement, measurement_failed)
        gate, conforms = loudness_gate(measurement, spec,
                                       measurement_failed=measurement_failed)
        if gate == "FAILED":
            self._bump("loudness_gate_fail")

        # --- 字幕 stale：渲染后字幕变了 → 候选有效但 stale_inputs 非空。
        stale: list[dict] = []
        stored_sub_ref = _loads(job["subtitle_ref"], None)
        if isinstance(stored_sub_ref, dict):
            try:
                current = self._read_subtitle(tenant_id, stored_sub_ref)
            except ServiceError as exc:
                if exc.status == 404:
                    stale.append({"role": "subtitle",
                                  "artifact_ref": stored_sub_ref,
                                  "reason": "SUBTITLE_MISSING"})
                    current = None
                else:
                    raise
            if current is not None and current.get("content_hash") != \
                    stored_sub_ref["content_hash"]:
                stale.append({"role": "subtitle",
                              "artifact_ref": stored_sub_ref,
                              "reason": "SUBTITLE_CHANGED",
                              "current_content_hash":
                                  current.get("content_hash")})
        if measurement_failed:
            stale.append({"role": "loudness", "reason": "MEASUREMENT_FAILED"})

        context = _loads(job["execution_context"], {})
        deliverable = gate == "PASSED" and not stale
        with Transaction(self.conn):
            candidate = self._write_candidate(
                tenant_id=tenant_id, context=context,
                timeline_id=job["timeline_id"],
                timeline_revision=int(job["timeline_revision"]),
                video=video, audio=audio, duration_ticks=duration_ticks,
                frame_count=int(frame_count or 0), measurement=measurement,
                gate=gate, conforms=conforms, stale=stale,
                deliverable=deliverable,
                execution_fence=int(job["execution_fence"]),
                render_env_hash=job["render_env_hash"],
                graph_hash=job["graph_hash"],
                mix_plan_hash=job["mix_plan_hash"],
                subtitle_hash=job["subtitle_content_hash"],
                render_job_id=job_id, mix_job_id=None,
                spec_ref=spec_ref, small=small or {},
                extra_manifest={"purpose": job["purpose"],
                                "spec_ref": spec_ref, "checks": checks})
            self.conn.execute(
                "UPDATE render_job SET state='SUCCEEDED',"
                " output_candidate_id=?, updated_at=? WHERE tenant_id=?"
                " AND render_job_id=? AND state NOT IN"
                " ('CANCELLED','FAILED','REJECTED')",
                (candidate["candidate_id"], _now(), tenant_id, job_id))
            event = self._emit(
                tenant_id=tenant_id, aggregate_id=candidate["candidate_id"],
                event_type=schema.EVENT_MASTER_RENDERED,
                subject=f"episode/{context.get('episode_id')}/candidate/"
                        f"{candidate['candidate_id']}",
                context=context,
                data={
                    "schema_version": SCHEMA_VERSION,
                    "candidate_id": candidate["candidate_id"],
                    "episode_id": context.get("episode_id"),
                    "branch_id": context.get("branch_id"),
                    "timeline_revision": int(job["timeline_revision"]),
                    "video_artifact_ref": video,
                    "audio_artifact_ref": audio,
                    "duration_ticks": duration_ticks,
                    "loudness": {
                        "integrated_lufs": (measurement or {}).get(
                            "integrated_lufs"),
                        "true_peak_dbtp": (measurement or {}).get(
                            "true_peak_dbtp"),
                        "gate": gate},
                    "deliverable": deliverable,
                    "execution_fence": int(job["execution_fence"]),
                    "render_env_hash": job["render_env_hash"],
                    "graph_hash": job["graph_hash"],
                    "stale_inputs": stale,
                    "qc_trigger_requested": True,
                })
            candidate["event"] = event["id"]
        return candidate

    def _fail_job(self, tenant_id: str, job_id: str, code: str,
                  detail: dict | None = None,
                  *, state: str = "FAILED",
                  emit: bool = True) -> dict:
        """终态失败，并发 `post.render.failed`。

        `sequence` 走 `render_job_id` 这个 aggregate —— 与候选事件不共享
        计数（§6.3 的聚合键表）。
        """
        if state not in schema.RENDER_FAILED_STATES:
            raise ValueError(f"{state} cannot be emitted as render.failed")
        row = self.conn.execute(
            "SELECT * FROM render_job WHERE tenant_id=? AND render_job_id=?",
            (tenant_id, job_id)).fetchone()
        if row is None:
            raise self._not_found("render job")
        job = dict(row)
        if job["state"] in schema.TERMINAL_JOB_STATES and \
                job["state"] != state:
            # 先到者赢（§7.2）：已终态就不覆盖。
            return {"render_job_id": job_id, "state": job["state"],
                    "emit": False}
        context = _loads(job["execution_context"], {})
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE render_job SET state=?, failure_code=?,"
                " failure_detail=?, stale_inputs=?, updated_at=?"
                " WHERE tenant_id=? AND render_job_id=?",
                (state, code, _jcs(detail or {}),
                 _jcs((detail or {}).get("missing") or []), _now(),
                 tenant_id, job_id))
            event_id = None
            if emit:
                event = self._emit(
                    tenant_id=tenant_id, aggregate_id=job_id,
                    event_type=schema.EVENT_RENDER_FAILED,
                    subject=f"episode/{job['episode_id']}/render-job/{job_id}",
                    context=context,
                    data={
                        "schema_version": SCHEMA_VERSION,
                        "render_job_id": job_id,
                        "project_id": job["project_id"],
                        "series_id": job["series_id"],
                        "episode_id": job["episode_id"],
                        "branch_id": job["branch_id"],
                        "timeline_id": job["timeline_id"],
                        "timeline_revision": int(job["timeline_revision"]),
                        "purpose": job["purpose"],
                        "state": state,
                        "failure_code": code,
                        "failure_detail": detail or {},
                        "retry_count": int(job["retry_count"]),
                        "stale_inputs": (detail or {}).get("missing") or [],
                        "consumed_gpu_ms": int(job["consumed_gpu_ms"]),
                        "consumed_cpu_ms": int(job["consumed_cpu_ms"]),
                        "render_env_hash": job["render_env_hash"],
                    })
                event_id = event["id"]
        self._bump(f"jobs_{state.lower()}")
        return {"render_job_id": job_id, "state": state, "emit": True,
                "event": event_id}

    # ------------------------------------------------------------ 候选

    def _write_candidate(self, *, tenant_id: str, context: dict,
                         timeline_id: str, timeline_revision: int,
                         video: dict, audio: dict, duration_ticks: int,
                         frame_count: int, measurement: dict | None,
                         gate: str, conforms: bool | None,
                         stale: list[dict], deliverable: bool,
                         execution_fence: int, render_env_hash: str,
                         graph_hash: str, mix_plan_hash: str,
                         subtitle_hash: str | None,
                         render_job_id: str | None,
                         mix_job_id: str | None, spec_ref: dict,
                         small: dict, extra_manifest: dict | None = None) -> dict:
        episode_id = str(context.get("episode_id"))
        branch_id = str(context.get("branch_id"))
        stems = [{"role": role, "artifact_ref": ref}
                 for role, ref in (small or {}).items()
                 if role in ("stem_dialogue", "stem_sfx", "stem_bgm")]
        subtitle_ref = None
        if subtitle_hash is not None:
            # 候选 manifest 必须能回答「基于哪版字幕」（§4.6 最后一条）。
            subtitle_ref = {"content_hash": subtitle_hash}
        manifest = build_manifest(
            video_ref=video, audio_ref=audio, duration_ticks=duration_ticks,
            frame_count=frame_count, subtitle_ref=subtitle_ref, stems=stems,
            timeline_id=timeline_id, timeline_revision=timeline_revision,
            mix_plan_hash=mix_plan_hash, graph_hash=graph_hash,
            render_env_hash=render_env_hash,
            artifacts=[{"role": role, "artifact_ref": ref}
                       for role, ref in (small or {}).items()])
        if extra_manifest:
            manifest.update(extra_manifest)
        # 规格 ref 恒进 manifest：remix 的候选也要能回答"按哪份交付规格出的"，
        # 否则链式 remix 走到第二跳就查不到规格了（`_spec_for_context`）。
        if spec_ref:
            manifest["spec_ref"] = spec_ref
        manifest_content_hash = _sha256_hex(_jcs(manifest))
        # 候选不可变：同一 manifest 已经有候选就复用，不造第二个。
        existing = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=? AND episode_id=?"
            " AND branch_id=? AND manifest_content_hash=?",
            (tenant_id, episode_id, branch_id,
             manifest_content_hash)).fetchone()
        if existing is not None:
            return self._candidate_view(dict(existing))

        candidate_id = _new_id("candidate")
        measurement_id = None
        now = _now()
        # 不在这里开事务：调用方（`finish_render_job` / `finish_mix_job`）
        # 把「候选 + job 终态 + outbox」放进**同一个** BEGIN IMMEDIATE。
        # 否则事件写失败时候选已经提交，job 却还在 RUNNING。
        if measurement is not None and conforms is not None:
            measurement_id = _new_id("measurement")
            self.conn.execute(
                "INSERT INTO loudness_measurement (tenant_id,"
                " measurement_id, render_job_id, mix_job_id, artifact_ref,"
                " integrated_lufs, true_peak_dbtp, lra_lu,"
                " measurement_tool, measurement_version, conforms_to_spec,"
                " spec_ref, measured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, measurement_id, render_job_id, mix_job_id,
                 _jcs(audio), float(measurement.get("integrated_lufs")),
                 float(measurement.get("true_peak_dbtp")),
                 measurement.get("lra_lu"),
                 str(measurement.get("measurement_tool") or "unknown"),
                 str(measurement.get("measurement_version") or "unknown"),
                 1 if conforms else 0, _jcs(spec_ref), now))
        self.conn.execute(
            "INSERT INTO master_candidate (tenant_id, candidate_id,"
            " episode_id, branch_id, timeline_id, timeline_revision,"
            " manifest_content_hash, manifest, render_job_id, mix_job_id,"
            " video_artifact_ref, audio_artifact_ref, duration_ticks,"
            " frame_count, loudness_measurement_id, loudness_gate,"
            " qc_report_refs, stale_inputs, deliverable, state,"
            " execution_fence, render_env_hash, graph_hash, mix_plan_hash,"
            " subtitle_artifact_hash, command_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',?,?,"
            "'CANDIDATE',?,?,?,?,?,?,?)",
            (tenant_id, candidate_id, episode_id, branch_id, timeline_id,
             int(timeline_revision), manifest_content_hash, _jcs(manifest),
             render_job_id, mix_job_id, _jcs(video), _jcs(audio),
             int(duration_ticks), int(frame_count), measurement_id, gate,
             _jcs(stale), 1 if deliverable else 0, int(execution_fence),
             render_env_hash, graph_hash, mix_plan_hash, subtitle_hash,
             str(context.get("command_id") or ""), now))
        ordinal = 0
        for role, ref in [("master_video", video), ("master_audio", audio)]:
            self.conn.execute(
                "INSERT INTO master_candidate_artifact (tenant_id,"
                " candidate_id, role, ordinal, artifact_ref)"
                " VALUES (?,?,?,?,?)",
                (tenant_id, candidate_id, role, ordinal, _jcs(ref)))
            ordinal += 1
        for role, ref in (small or {}).items():
            if role not in schema.ARTIFACT_ROLES or role in (
                    "master_video", "master_audio"):
                continue
            self.conn.execute(
                "INSERT INTO master_candidate_artifact (tenant_id,"
                " candidate_id, role, ordinal, artifact_ref)"
                " VALUES (?,?,?,?,?)",
                (tenant_id, candidate_id, role, 0, _jcs(ref)))
        self._bump("candidates_created")
        row = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=? AND"
            " candidate_id=?", (tenant_id, candidate_id)).fetchone()
        return self._candidate_view(dict(row))

    def finish_mix_job(self, tenant_id: str, mix_job_id: str, *,
                       output_audio_ref: dict,
                       measurement: dict | None = None,
                       measurement_failed: bool = False,
                       consumed_cpu_ms: int = 0) -> dict:
        """§4.4 的收尾：**新候选，同一个 video 流**。

        `video_artifact_ref` 从 `mix_job.source_video_ref` 原样抄过去 ——
        逐字段相同是 E24-S42-01 的判定依据，不是"重编码一遍碰巧一样"。
        """
        row = self.conn.execute(
            "SELECT * FROM mix_job WHERE tenant_id=? AND mix_job_id=?",
            (tenant_id, mix_job_id)).fetchone()
        if row is None:
            raise self._not_found("mix job")
        job = dict(row)
        if job["state"] in schema.TERMINAL_MIX_STATES:
            raise ServiceError(409, "JOB_TERMINAL", f"job is {job['state']}",
                               details={"state": job["state"]})
        audio = _require_ref(output_audio_ref, "output_audio_ref")
        checked = self._check_artifacts(tenant_id, [audio])
        if checked["missing"]:
            raise ServiceError(422, "INPUT_ARTIFACT_MISSING",
                               "the mixed audio is not confirmed in S28",
                               details={"missing": checked["missing"]})
        video = _loads(job["source_video_ref"], {})
        basis = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=? AND"
            " candidate_id=?",
            (tenant_id, job["basis_candidate_id"])).fetchone()
        if basis is None:
            raise self._not_found("basis candidate")
        basis = dict(basis)
        context = _loads(job["execution_context"], {})
        spec_ref = _loads(basis["manifest"], {}).get("spec_ref") or {}
        spec = self._read_spec(tenant_id, spec_ref) if spec_ref else {}
        measurement, measurement_failed = _usable_measurement(
            measurement, measurement_failed)
        gate, conforms = loudness_gate(measurement, spec,
                                       measurement_failed=measurement_failed)
        stale = []
        if measurement_failed:
            stale.append({"role": "loudness", "reason": "MEASUREMENT_FAILED"})
        context_event = {**context, "episode_id": job["episode_id"],
                         "branch_id": job["branch_id"]}
        with Transaction(self.conn):
            candidate = self._write_candidate(
                tenant_id=tenant_id, context=context,
                timeline_id=job["timeline_id"],
                timeline_revision=int(job["timeline_revision"]),
                video=video, audio=audio,
                duration_ticks=int(basis["duration_ticks"]),
                frame_count=int(basis["frame_count"]), measurement=measurement,
                gate=gate, conforms=conforms, stale=stale,
                deliverable=gate == "PASSED" and not stale,
                execution_fence=int(job["execution_fence"]),
                render_env_hash=basis["render_env_hash"],
                graph_hash=job["graph_hash"], mix_plan_hash=job["mix_plan_hash"],
                subtitle_hash=basis["subtitle_artifact_hash"],
                render_job_id=None, mix_job_id=mix_job_id,
                spec_ref=spec_ref, small={},
                extra_manifest={"remix": {
                    "reason": job["reason"],
                    "basis_candidate_id": job["basis_candidate_id"],
                    "repair_plan_ref": _loads(job["repair_plan_ref"], None),
                    "video_stream_reused": True}})
            # 用量只在收尾时报一次：创建时不报（那时还没烧任何 CPU）。
            cpu_ms = int(job["consumed_cpu_ms"]) + int(consumed_cpu_ms or 0)
            self.conn.execute(
                "UPDATE mix_job SET state='SUCCEEDED', output_audio_ref=?,"
                " candidate_id=?, consumed_cpu_ms=?, finished_at=?"
                " WHERE tenant_id=?"
                " AND mix_job_id=? AND state IN ('CREATED','RUNNING')",
                (_jcs(audio), candidate["candidate_id"], cpu_ms, _now(),
                 tenant_id, mix_job_id))
            if cpu_ms:
                self._record_usage(tenant_id, mix_job_id=mix_job_id,
                                   resource_class="cpu_media_second",
                                   quantity_ms=cpu_ms,
                                   usage_key=f"{mix_job_id}:mix")
            event = self._emit(
                tenant_id=tenant_id, aggregate_id=mix_job_id,
                event_type=schema.EVENT_MIX_COMPLETED,
                subject=f"episode/{job['episode_id']}/mix-job/{mix_job_id}",
                context=context_event,
                data={
                    "schema_version": SCHEMA_VERSION,
                    "mix_job_id": mix_job_id,
                    "episode_id": job["episode_id"],
                    "branch_id": job["branch_id"],
                    "source_video_ref": video,
                    "mix_plan_hash": job["mix_plan_hash"],
                    "graph_hash": job["graph_hash"],
                    "target_lufs": float(job["target_lufs"]),
                    "true_peak_ceiling": float(job["true_peak_ceiling"]),
                    "reason": job["reason"],
                    "repair_plan_ref": _loads(job["repair_plan_ref"], None),
                    "state": "SUCCEEDED",
                    "output_audio_ref": audio,
                    "consumed_cpu_ms": cpu_ms,
                })
            candidate["event"] = event["id"]
        candidate["source_video_preserved"] = True
        return candidate

    def cancel_render_job(self, tenant_id: str, job_id: str) -> dict:
        """`CANCELLING`，**不承诺立即停止**，**不退款**（E22）。

        已消耗的用量照常上报：GPU 秒是真烧掉了。终态 job 再取消即
        `JOB_TERMINAL`（幂等重投由 `command()` 兜住，不会走到这里两次）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] in schema.TERMINAL_JOB_STATES:
            raise ServiceError(409, "JOB_TERMINAL", f"job is {job['state']}",
                               details={"state": job["state"]})
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE render_job SET state='CANCELLING', updated_at=?"
                " WHERE tenant_id=? AND render_job_id=? AND state IN"
                " ('QUEUED','ADMITTED','RUNNING','CANCELLING')",
                (_now(), tenant_id, job_id))
        return {"render_job_id": job_id, "state": "CANCELLING",
                "consumed_gpu_ms": int(job["consumed_gpu_ms"]),
                "consumed_cpu_ms": int(job["consumed_cpu_ms"])}

    def cancel_mix_job(self, tenant_id: str, mix_job_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM mix_job WHERE tenant_id=? AND mix_job_id=?",
            (tenant_id, mix_job_id)).fetchone()
        if row is None:
            raise self._not_found("mix job")
        job = dict(row)
        if job["state"] in schema.TERMINAL_MIX_STATES:
            raise ServiceError(409, "JOB_TERMINAL", f"job is {job['state']}",
                               details={"state": job["state"]})
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE mix_job SET state='CANCELLED', finished_at=?"
                " WHERE tenant_id=? AND mix_job_id=? AND state IN"
                " ('CREATED','RUNNING')",
                (_now(), tenant_id, mix_job_id))
        return {"mix_job_id": mix_job_id, "state": "CANCELLED",
                "consumed_cpu_ms": int(job["consumed_cpu_ms"])}

    # ------------------------------------------------------------ 恢复与对账

    def recover_expired_leases(self, tenant_id: str | None = None) -> dict:
        """租约过期的 job → `ORPHANED`。**不猜产物**：对账另走 `reconcile`。"""
        now = _now()
        sql = ("SELECT tenant_id, render_job_id FROM render_job"
               " WHERE state IN ('ADMITTED','RUNNING')"
               " AND lease_expires_at IS NOT NULL AND lease_expires_at < ?")
        args: tuple = (now,)
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            args = (now, tenant_id)
        rows = self.conn.execute(sql, args).fetchall()
        orphaned = []
        for row in rows:
            with Transaction(self.conn):
                cur = self.conn.execute(
                    "UPDATE render_job SET state='ORPHANED', worker_id=NULL,"
                    " lease_expires_at=NULL, updated_at=? WHERE tenant_id=?"
                    " AND render_job_id=? AND state IN ('ADMITTED','RUNNING')",
                    (_now(), row["tenant_id"], row["render_job_id"]))
                if cur.rowcount == 1:
                    orphaned.append(row["render_job_id"])
        if orphaned:
            self._bump("jobs_orphaned", len(orphaned))
        return {"orphaned": orphaned}

    def reconcile_render_job(self, tenant_id: str,
                             job_id: str) -> dict:
        """ORPHANED 对账：**查 S28**，不查本地临时文件（§7.2）。

        三种结局（§3.1 的状态图）：

        - 全部段都有 S28 确认的产物 → 回到 `RUNNING`（可立即收尾）
        - 部分段有 → `RUNNING` 续跑剩余段（已确认的段不重渲染）
        - 全都没有 → `QUEUED` 重新排队

        已记录的段 ref 逐条复核（C14）。**不扫 S28 列表去猜**哪些属于本
        job —— 那需要按 job 查询的接口，契约没有。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] not in schema.RESUMABLE_JOB_STATES:
            raise ServiceError(
                409, "JOB_NOT_RESUMABLE",
                f"job is {job['state']}; reconcile applies to "
                f"{schema.RESUMABLE_JOB_STATES}",
                details={"state": job["state"]})
        rows = self.conn.execute(
            "SELECT * FROM render_segment WHERE tenant_id=? AND"
            " render_job_id=? ORDER BY segment_no",
            (tenant_id, job_id)).fetchall()
        confirmed, lost = [], []
        for row in rows:
            segment = dict(row)
            ref = _loads(segment["output_artifact_ref"], None)
            if not isinstance(ref, dict):
                continue
            try:
                fact = self._upstream(
                    lambda ref=ref: self.artifact_facts(tenant_id, ref),
                    "ARCHIVE_UNAVAILABLE")
            except ServiceError as exc:
                if exc.status == 404:
                    fact = {}
                else:
                    raise
            if isinstance(fact, dict) and fact.get("content_hash") == \
                    ref["content_hash"] and not fact.get("tombstoned"):
                confirmed.append(segment["segment_no"])
            else:
                lost.append(segment["segment_no"])
        if confirmed and not lost:
            target = "RUNNING"
        elif confirmed:
            target = "RUNNING"
        else:
            target = "QUEUED"
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE render_job SET state=?, updated_at=? WHERE tenant_id=?"
                " AND render_job_id=? AND state='ORPHANED'",
                (target, _now(), tenant_id, job_id))
        return {"render_job_id": job_id, "state": target,
                "confirmed_segments": confirmed, "lost_segments": lost,
                "remaining_segments": [int(s["segment_no"]) for s in rows
                                       if s["state"] not in
                                       schema.SEGMENT_TERMINAL_STATES]}

    def retry_render_job(self, tenant_id: str, job_id: str) -> dict:
        """`FAILED`（可重试失败）→ `QUEUED`。终态其他值不允许。"""
        job = self._job(tenant_id, job_id)
        if job["state"] not in ("FAILED", "ORPHANED"):
            raise ServiceError(
                409, "JOB_NOT_RETRYABLE",
                f"job is {job['state']}; only FAILED/ORPHANED can be retried",
                details={"state": job["state"]})
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE render_job SET state='QUEUED', failure_code=NULL,"
                " failure_detail=NULL, worker_id=NULL, lease_expires_at=NULL,"
                " updated_at=? WHERE tenant_id=? AND render_job_id=?",
                (_now(), tenant_id, job_id))
        return self.get_render_job(tenant_id, job_id)

    def report_usage(self, tenant_id: str, *, limit: int = 100) -> dict:
        """把未上报的用量事实交给 S23。**只报事实，不做结算**。

        S23 不可达时保留 `reported_to_s23=0` 并整体 503：丢一条用量事实
        比晚一点报更糟（那是真实的资源消耗）。
        """
        rows = self.conn.execute(
            "SELECT * FROM render_usage WHERE tenant_id=? AND"
            " reported_to_s23=0 ORDER BY occurred_at LIMIT ?",
            (tenant_id, int(limit))).fetchall()
        reported, failed = [], None
        for row in rows:
            usage = dict(row)
            request = {
                "usage_id": usage["usage_id"],
                "resource_class": usage["resource_class"],
                "quantity_ms": int(usage["quantity_ms"]),
                "occurred_at": usage["occurred_at"],
                "render_job_id": usage["render_job_id"],
                "mix_job_id": usage["mix_job_id"],
            }
            try:
                self._upstream(
                    lambda request=request: self.usage_reporter(
                        tenant_id, request),
                    "USAGE_REPORT_UNAVAILABLE")
            except ServiceError as exc:
                failed = {"code": exc.code, "status": exc.status}
                break
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE render_usage SET reported_to_s23=1"
                    " WHERE tenant_id=? AND usage_id=?",
                    (tenant_id, usage["usage_id"]))
            reported.append(usage["usage_id"])
        return {"reported": reported,
                "pending": len(rows) - len(reported),
                "error": failed}

    # ------------------------------------------------------------ 查询

    def _job(self, tenant_id: str, job_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM render_job WHERE tenant_id=? AND render_job_id=?",
            (tenant_id, job_id)).fetchone()
        if row is None:
            raise self._not_found("render job")
        return dict(row)

    def get_render_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        segments = self.conn.execute(
            "SELECT segment_no, rec_in_ticks, rec_out_ticks, state,"
            " output_artifact_ref, attempts, consumed_gpu_ms, segment_key"
            " FROM render_segment WHERE tenant_id=? AND render_job_id=?"
            " ORDER BY segment_no", (tenant_id, job_id)).fetchall()
        candidate = None
        if job["output_candidate_id"]:
            row = self.conn.execute(
                "SELECT * FROM master_candidate WHERE tenant_id=? AND"
                " candidate_id=?",
                (tenant_id, job["output_candidate_id"])).fetchone()
            if row is not None:
                candidate = self._candidate_view(dict(row))
        return {
            "render_job_id": job["render_job_id"],
            "state": job["state"],
            "purpose": job["purpose"],
            "episode_id": job["episode_id"],
            "branch_id": job["branch_id"],
            "timeline_id": job["timeline_id"],
            "timeline_revision": int(job["timeline_revision"]),
            "timeline_content_hash": job["timeline_content_hash"],
            "mix_plan_hash": job["mix_plan_hash"],
            "render_profile_hash": job["render_profile_hash"],
            "render_env_hash": job["render_env_hash"],
            "render_env": _loads(job["render_env"], {}),
            "graph_hash": job["graph_hash"],
            "execution_fence": int(job["execution_fence"]),
            "node_epoch": int(job["node_epoch"]),
            "node_id": job["node_id"],
            "permit_ref": _loads(job["permit_ref"], None),
            "worker_id": job["worker_id"],
            "lease_expires_at": job["lease_expires_at"],
            "estimated_gpu_ms": job["estimated_gpu_ms"],
            "consumed_gpu_ms": int(job["consumed_gpu_ms"]),
            "consumed_cpu_ms": int(job["consumed_cpu_ms"]),
            "retry_count": int(job["retry_count"]),
            "failure_code": job["failure_code"],
            "failure_detail": _loads(job["failure_detail"], None),
            "stale_inputs": _loads(job["stale_inputs"], []),
            "candidate": candidate,
            "segments": [
                {"segment_no": int(s["segment_no"]),
                 "rec_in_ticks": int(s["rec_in_ticks"]),
                 "rec_out_ticks": int(s["rec_out_ticks"]),
                 "state": s["state"],
                 "output_artifact_ref": _loads(s["output_artifact_ref"], None),
                 "attempts": int(s["attempts"]),
                 "consumed_gpu_ms": int(s["consumed_gpu_ms"]),
                 "segment_key": s["segment_key"]}
                for s in segments],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }

    def list_render_jobs(self, tenant_id: str, *, episode_id: str | None = None,
                         branch_id: str | None = None,
                         limit: int = 50) -> dict:
        sql = ("SELECT render_job_id, state, purpose, episode_id, branch_id,"
               " timeline_revision, output_candidate_id, created_at"
               " FROM render_job WHERE tenant_id=?")
        args: list = [tenant_id]
        if episode_id:
            sql += " AND episode_id=?"
            args.append(episode_id)
        if branch_id:
            sql += " AND branch_id=?"
            args.append(branch_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        return {"items": [dict(r) for r in rows]}

    def _candidate_view(self, row: dict) -> dict:
        return {
            "candidate_id": row["candidate_id"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "timeline_id": row["timeline_id"],
            "timeline_revision": int(row["timeline_revision"]),
            "manifest": _loads(row["manifest"], {}),
            "manifest_content_hash": row["manifest_content_hash"],
            "render_job_id": row["render_job_id"],
            "mix_job_id": row["mix_job_id"],
            "video_artifact_ref": _loads(row["video_artifact_ref"], {}),
            "audio_artifact_ref": _loads(row["audio_artifact_ref"], {}),
            "duration_ticks": int(row["duration_ticks"]),
            "frame_count": int(row["frame_count"]),
            "loudness_measurement_id": row["loudness_measurement_id"],
            "loudness_gate": row["loudness_gate"],
            "qc_report_refs": _loads(row["qc_report_refs"], []),
            "stale_inputs": _loads(row["stale_inputs"], []),
            "deliverable": bool(row["deliverable"]),
            # 只是本服务的投影，权威在 S08（§3.3）。
            "state": row["state"],
            "state_is_projection": row["state"] == "ACCEPTED_BY_SNAPSHOT",
            "execution_fence": int(row["execution_fence"]),
            "render_env_hash": row["render_env_hash"],
            "graph_hash": row["graph_hash"],
            "mix_plan_hash": row["mix_plan_hash"],
            "subtitle_artifact_hash": row["subtitle_artifact_hash"],
            "artifact_roles": sorted(
                {r["role"] for r in self.conn.execute(
                    "SELECT role FROM master_candidate_artifact"
                    " WHERE tenant_id=? AND candidate_id=?",
                    (row["tenant_id"], row["candidate_id"])).fetchall()}),
            "created_at": row["created_at"],
        }

    def get_candidate(self, tenant_id: str, candidate_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=? AND"
            " candidate_id=?", (tenant_id, candidate_id)).fetchone()
        if row is None:
            raise self._not_found("master candidate")
        view = self._candidate_view(dict(row))
        artifacts = self.conn.execute(
            "SELECT role, ordinal, artifact_ref FROM master_candidate_artifact"
            " WHERE tenant_id=? AND candidate_id=? ORDER BY role, ordinal",
            (tenant_id, candidate_id)).fetchall()
        view["artifacts"] = [
            {"role": a["role"], "ordinal": int(a["ordinal"]),
             "artifact_ref": _loads(a["artifact_ref"], {})}
            for a in artifacts]
        return view

    def list_candidates(self, tenant_id: str, *, episode_id: str | None = None,
                        branch_id: str | None = None,
                        deliverable: bool | None = None,
                        limit: int = 50) -> dict:
        sql = ("SELECT candidate_id, episode_id, branch_id, timeline_revision,"
               " loudness_gate, deliverable, state, created_at"
               " FROM master_candidate WHERE tenant_id=?")
        args: list = [tenant_id]
        if episode_id:
            sql += " AND episode_id=?"
            args.append(episode_id)
        if branch_id:
            sql += " AND branch_id=?"
            args.append(branch_id)
        if deliverable is not None:
            sql += " AND deliverable=?"
            args.append(1 if deliverable else 0)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["deliverable"] = bool(item["deliverable"])
            items.append(item)
        return {"items": items}

    def candidate_evidence(self, tenant_id: str, candidate_id: str) -> dict:
        """交付证据包（§6.2）：**每个输入是什么 hash、用了什么图、什么环境**。

        S43 / S45 靠它回答"这一版能不能复现"。这里只回本服务**自己的**
        记录，不向 S28 现查 —— 证据是"当时是什么样"，不是"现在是什么样"。
        """
        row = self.conn.execute(
            "SELECT * FROM master_candidate WHERE tenant_id=? AND"
            " candidate_id=?", (tenant_id, candidate_id)).fetchone()
        if row is None:
            raise self._not_found("master candidate")
        candidate = dict(row)
        job = None
        if candidate["render_job_id"]:
            found = self.conn.execute(
                "SELECT * FROM render_job WHERE tenant_id=? AND"
                " render_job_id=?",
                (tenant_id, candidate["render_job_id"])).fetchone()
            job = dict(found) if found is not None else None
        graph = self.conn.execute(
            "SELECT * FROM filter_graph WHERE tenant_id=? AND graph_hash=?",
            (tenant_id, candidate["graph_hash"])).fetchone()
        measurement = None
        if candidate["loudness_measurement_id"]:
            found = self.conn.execute(
                "SELECT * FROM loudness_measurement WHERE tenant_id=? AND"
                " measurement_id=?",
                (tenant_id, candidate["loudness_measurement_id"])).fetchone()
            measurement = dict(found) if found is not None else None
        segments = []
        if job is not None:
            segments = [
                {"segment_no": int(s["segment_no"]),
                 "segment_key": s["segment_key"],
                 "rec_in_ticks": int(s["rec_in_ticks"]),
                 "rec_out_ticks": int(s["rec_out_ticks"]),
                 "output_artifact_ref": _loads(s["output_artifact_ref"], None)}
                for s in self.conn.execute(
                    "SELECT * FROM render_segment WHERE tenant_id=? AND"
                    " render_job_id=? ORDER BY segment_no",
                    (tenant_id, job["render_job_id"])).fetchall()]
        artifact_rows = self.conn.execute(
            "SELECT role, ordinal, artifact_ref FROM master_candidate_artifact"
            " WHERE tenant_id=? AND candidate_id=? ORDER BY role, ordinal",
            (tenant_id, candidate_id)).fetchall()
        return {
            "candidate_id": candidate_id,
            "manifest_content_hash": candidate["manifest_content_hash"],
            "manifest": _loads(candidate["manifest"], {}),
            "graph_hash": candidate["graph_hash"],
            "graph_version": (dict(graph)["graph_version"]
                              if graph is not None else None),
            "graph": _loads(dict(graph)["graph"], None)
            if graph is not None else None,
            "render_env_hash": candidate["render_env_hash"],
            "render_env": _loads(job["render_env"], {}) if job else {},
            "render_profile_hash": job["render_profile_hash"] if job else None,
            "mix_plan_hash": candidate["mix_plan_hash"],
            "subtitle_artifact_hash": candidate["subtitle_artifact_hash"],
            "timeline_content_hash": job["timeline_content_hash"] if job
            else None,
            "loudness": None if measurement is None else {
                "integrated_lufs": measurement["integrated_lufs"],
                "true_peak_dbtp": measurement["true_peak_dbtp"],
                "lra_lu": measurement["lra_lu"],
                "measurement_tool": measurement["measurement_tool"],
                "measurement_version": measurement["measurement_version"],
                "conforms_to_spec": bool(measurement["conforms_to_spec"]),
                "measured_at": measurement["measured_at"]},
            "loudness_gate": candidate["loudness_gate"],
            "qc_report_refs": _loads(candidate["qc_report_refs"], []),
            "stale_inputs": _loads(candidate["stale_inputs"], []),
            "artifacts": [
                {"role": a["role"], "ordinal": int(a["ordinal"]),
                 "artifact_ref": _loads(a["artifact_ref"], {})}
                for a in artifact_rows],
            "segments": segments,
            "execution_fence": int(candidate["execution_fence"]),
            "deliverable": bool(candidate["deliverable"]),
        }

    # ------------------------------------------------------------ 事件消费

    def _consume(self, consumer: str, envelope: dict) -> str:
        """inbox 去重 + **按 aggregate 的水位**。返回三种状态之一：

        - `"applied"`  首次见到且不旧于水位 → 可以投影
        - `"duplicate"` event_id 已处理过 → 不重复投影
        - `"stale"`    首次见到但 sequence 落后于水位 → **不投影，也不报错**

        `"stale"` **不抛 409**：乱序是至少一次投递的正常形态，不是客户端
        错误。抛错会让发布方无限重投那条永远旧的事件。水位回答的是
        「这次要不要覆盖已有投影」，答案是不；调用方在返回值里说出来即可。

        水位不是可选的：一条 `sequence=1` 的快照事件晚到，不能把
        `sequence=2` 已经做的投影改回去。
        """
        tenant_id = str(envelope.get("tenant_id") or "")
        event_id = str(envelope.get("id") or "")
        if not event_id:
            raise ServiceError(422, "SCHEMA_INVALID", "event id is required")
        aggregate_id = str(envelope.get("aggregate_id")
                           or envelope.get("subject") or "")
        sequence = envelope.get("sequence",
                                envelope.get("aggregate_sequence"))
        if sequence is None:
            sequence = 0
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "event sequence must be an integer")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbox (consumer, event_id, tenant_id,"
            " event_type, aggregate_id, sequence, received_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (consumer, event_id, tenant_id or None,
             envelope.get("type"), aggregate_id or None, sequence, _now()))
        if cur.rowcount != 1:
            return "duplicate"
        row = self.conn.execute(
            "SELECT sequence FROM event_watermark WHERE consumer=?"
            " AND aggregate_id=?", (consumer, aggregate_id)).fetchone()
        watermark = int(row["sequence"]) if row is not None else 0
        if sequence and sequence < watermark:
            # inbox 已记（同一事务内，会随投影一起提交），投影跳过。
            return "stale"
        if sequence:
            self.conn.execute(
                "INSERT INTO event_watermark (consumer, aggregate_id,"
                " sequence, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(consumer, aggregate_id) DO UPDATE SET"
                " sequence=excluded.sequence, updated_at=excluded.updated_at",
                (consumer, aggregate_id, sequence, _now()))
        return "applied"

    def consume_timeline_revision(self, tenant_id: str,
                                  envelope: dict) -> dict:
        """S41 的 revision 信号：留一份「S41 说哪几段变了」的可追溯记录。

        **这份记录不是复用的判据。** 真正渲染时仍回读 S41 的 diff（§4.2 L1）：
        事件是通知，不是输入 —— 拿一条可能过期或被伪造的事件去决定"哪一段
        可以复用"，等于让事件成为第二权威。`timeline_signal` 只供审计与
        运维回答"当时 S41 说了什么"。
        """
        data = envelope.get("data") or {}
        timeline_id = data.get("timeline_id")
        revision = data.get("revision")
        if not isinstance(timeline_id, str) or not isinstance(revision, int):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "timeline event needs timeline_id + revision")
        with Transaction(self.conn):
            outcome = self._consume(schema.CONSUMER_TIMELINE, envelope)
            if outcome == "applied":
                self.conn.execute(
                    "INSERT OR REPLACE INTO timeline_signal (tenant_id,"
                    " timeline_id, revision, content_hash, affected_ranges,"
                    " received_at) VALUES (?,?,?,?,?,?)",
                    (tenant_id, timeline_id, revision,
                     str(data.get("content_hash") or ""),
                     _jcs(data.get("affected_ranges") or []), _now()))
        return {"accepted": outcome == "applied", "outcome": outcome,
                "timeline_id": timeline_id, "revision": revision}

    def consume_repair_planned(self, tenant_id: str, envelope: dict) -> dict:
        """S39 的修复计划镜像。**只读镜像**：本服务不执行计划、不改计划。

        `top_action != remix` 时同样记录 —— E24-S42-05 要求"计划是
        regenerate_video 时 S42 走完整渲染"，而走哪条路由由编排（S21）
        决定，不由本服务把 remix 计划降级或升级。
        """
        data = envelope.get("data") or {}
        plan_id = data.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "repair event needs plan_id")
        with Transaction(self.conn):
            outcome = self._consume(schema.CONSUMER_REPAIR, envelope)
            if outcome == "applied":
                self.conn.execute(
                    "INSERT OR REPLACE INTO repair_signal (tenant_id, plan_id,"
                    " sequence, state, top_action, episode_id, artifact_sha256,"
                    " repair_plan_ref, payload, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, plan_id, int(envelope.get("sequence") or 0),
                     str(data.get("state") or ""),
                     str(data.get("top_action") or ""),
                     data.get("episode_id"), data.get("artifact_sha256"),
                     _jcs(envelope.get("subject_ref") or None)
                     if envelope.get("subject_ref") else None,
                     _jcs(data), _now()))
        return {"accepted": outcome == "applied", "outcome": outcome,
                "plan_id": plan_id, "top_action": data.get("top_action")}

    def consume_snapshot_committed(self, tenant_id: str,
                                   envelope: dict) -> dict:
        """S08 的接受快照：**改的是本服务的投影，不是真相**（§3.3）。

        - 只有 `accepted_pointers_delta` 里出现与候选 `video_artifact_ref`
          **逐字段相同**的 S28 四键 ref 才认（C8：元素形状未定义，不猜）。
        - 命中 → 该候选 `ACCEPTED_BY_SNAPSHOT`；同集同分支的其它候选 →
          `SUPERSEDED`（不删除，交付证据可能引用旧候选）。
        - `execution_fence` 低于候选自己的 fence → 不投影：旧 fence 的快照
          不能改写新 fence 的候选（旧结果永远推不动当前接受）。
        """
        data = envelope.get("data") or {}
        delta = data.get("accepted_pointers_delta")
        if not isinstance(delta, list):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "snapshot event needs accepted_pointers_delta")
        snapshot_fence = data.get("execution_fence")
        entries = [d for d in delta if isinstance(d, dict)]
        unmatched = [d for d in entries if not _is_s28_ref(d)]
        accepted = []
        with Transaction(self.conn):
            outcome = self._consume(schema.CONSUMER_SNAPSHOT, envelope)
            for entry in entries if outcome == "applied" else ():
                if not _is_s28_ref(entry):
                    continue
                row = self.conn.execute(
                    "SELECT * FROM master_candidate WHERE tenant_id=?"
                    " AND json_extract(video_artifact_ref, '$.id')=?"
                    " AND json_extract(video_artifact_ref, '$.revision')=?"
                    " AND json_extract(video_artifact_ref, '$.content_hash')=?"
                    " AND state IN ('CANDIDATE','ACCEPTED_BY_SNAPSHOT',"
                    "'SUPERSEDED','QC_REJECTED')"
                    " ORDER BY created_at DESC",
                    (tenant_id, entry["id"], int(entry["revision"]),
                     entry["content_hash"])).fetchone()
                if row is None:
                    unmatched.append(entry)
                    continue
                candidate = dict(row)
                if isinstance(snapshot_fence, int) and \
                        snapshot_fence < int(candidate["execution_fence"]):
                    # 快照的 fence 更旧：不投影，也不报错 —— 记下来即可。
                    self._bump("snapshot_fence_skipped")
                    unmatched.append({**entry,
                                      "reason": "STALE_SNAPSHOT_FENCE"})
                    continue
                self.conn.execute(
                    "UPDATE master_candidate SET state='ACCEPTED_BY_SNAPSHOT'"
                    " WHERE tenant_id=? AND candidate_id=?",
                    (tenant_id, candidate["candidate_id"]))
                self.conn.execute(
                    "UPDATE master_candidate SET state='SUPERSEDED'"
                    " WHERE tenant_id=? AND episode_id=? AND branch_id=?"
                    " AND candidate_id<>? AND state IN"
                    " ('CANDIDATE','ACCEPTED_BY_SNAPSHOT')",
                    (tenant_id, candidate["episode_id"],
                     candidate["branch_id"], candidate["candidate_id"]))
                accepted.append(candidate["candidate_id"])
        return {"accepted": outcome == "applied", "outcome": outcome,
                "projected": accepted, "unmatched": unmatched,
                "accepted_revision": data.get("accepted_revision")}

    def consume_artifact_ingested(self, tenant_id: str,
                                  envelope: dict) -> dict:
        """S28 的入库回执。**只记 inbox**。

        不在这里"顺手"完成段：段完成必须走 `record_segment_uploaded`，
        由 worker 报出它用的是哪个 `segment_key`；事件里没有这个字段，
        靠猜会把两个 job 的段串在一起。
        """
        data = envelope.get("data") or {}
        with Transaction(self.conn):
            outcome = self._consume(schema.CONSUMER_ARTIFACT, envelope)
        return {"accepted": outcome == "applied", "outcome": outcome,
                "artifact_id": data.get("artifact_id")}


def _is_s28_ref(value) -> bool:
    """S28 的四键 ref。**多一个键或类型不对都不认** —— 不猜形状（C8）。

    `accepted_pointers_delta` 的元素形状在契约里没有定义（C8），所以这里
    的判定必须严格：`video_artifact_ref` 的契约是
    `additionalProperties: false`，一个多出来的键就说明它不是同一类 ref，
    不能拿来匹配候选。
    """
    if not isinstance(value, dict):
        return False
    if set(value) != {"service_id", "id", "revision", "content_hash"}:
        return False
    if value.get("service_id") != "S28":
        return False
    identifier = value.get("id")
    revision = value.get("revision")
    digest = value.get("content_hash")
    return isinstance(identifier, str) and bool(identifier) \
        and isinstance(revision, int) and not isinstance(revision, bool) \
        and revision >= 1 and schema.is_hash(digest)


