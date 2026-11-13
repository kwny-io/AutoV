"""S42 的上游，全部是注入式 callback，**不是 import**（AGENTS.md 禁止跨库）。

七条路径，全部在 `build_*` 里闭合。**未配置一律 fail closed**：调用时抛
`ServiceError(503, "<CODE>_UNAVAILABLE")`。这里没有「可选依赖」——渲染的每
一个输入都是确定性的前提，少一个就只能停，不能猜：

| 依赖 | 用来做什么 | 不可达 |
|---|---|---|
| S41 时间线 | 分段、片段排列、`output_plan` / `mix_plan` | 503 `TIMELINE_UNAVAILABLE` |
| S41 diff | 增量渲染的受影响区间 | 503 `TIMELINE_DIFF_UNAVAILABLE` |
| S28 制品 | 输入/产物是否存在、hash、时长、帧率 | 503 `ARCHIVE_UNAVAILABLE` |
| S38 字幕 | 字幕轨当前 hash（stale 判定） | 503 `SUBTITLE_SOURCE_UNAVAILABLE` |
| 项目规格 | 编码/封装/色彩/响度容差 | 503 `MASTER_SPEC_UNAVAILABLE` |
| S22 许可 | `gpu_second` / `cpu_media_second` 资源秒 | 503 `PERMIT_UNAVAILABLE` |
| S23 计量 | 用量事实上报 | 503 `USAGE_REPORT_UNAVAILABLE` |

**没有硬编码 provider URL。** `base_url` 一律来自环境变量，由 `api/app.py`
注入；这里出现的路径是本仓服务之间的内部契约路径。

## 与真实契约的两处落差（见 `schema.CONTRACT_GAPS`）

- `S41` 的 `GET /timelines/{id}/revisions/{rev}` **不返回** `output_plan` 与
  `mix_plan`（S42-C3）。本模块先看响应里有没有，没有就试提案路由
  `/plans`；两条路都拿不到即 502 `TIMELINE_PLAN_UNAVAILABLE`。
  **S42 绝不自行划分 segment、绝不发明响度目标。**
- `S23` 没有「资源用量事实」入口（S42-C17）。上报走提案路由
  `/usage-facts`；未配置 503，配置了也只发这一种形状。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError


def _request(method: str, url: str, *, body: dict | None = None,
             headers: dict | None = None, timeout: float = 15.0) -> dict:
    data = None if body is None else json.dumps(
        body, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=merged,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            pass
        raise ServiceError(exc.code, payload.get("code") or "UPSTREAM_ERROR",
                           payload.get("message") or str(exc),
                           retryable=bool(payload.get("retryable")),
                           details=payload.get("details") or {})
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _unconfigured(code: str, detail: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, code, detail)
    return _fail


def _hash64(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


# ------------------------------------------------------------------ S41 timeline

def build_timeline_source(base_url: str | None):
    """`(tenant_id, ref) -> 时间线快照（含 output_plan / mix_plan）`。

    `ref` 形如 `{"timeline_id": "tl_1", "revision": 10}`。返回值：

        {"timeline_id", "revision", "content_hash", "duration_ticks",
         "completeness", "formal_master_eligible", "tick_rate", "fps",
         "clips": [...], "output_plan": {...}, "mix_plan": {...}}

    **计划必须是 S41 给的。** 读不到 `output_plan.segments` 就 502，而不是
    按 clips 自己合并出一个分段方案——那会让同一条时间线在不同时间渲染出
    不同分段，缓存全失效（方案 §2.3）。
    """
    if not base_url:
        return _unconfigured(
            "TIMELINE_UNAVAILABLE",
            "S41_BASE_URL is not configured; S42 has no segments, no mix plan "
            "and will not invent either (plan §2.3, §4.1)")
    prefix = base_url.rstrip("/")

    def _timeline(tenant_id: str, ref: dict) -> dict:
        raw_id = ref.get("timeline_id") or ref.get("id")
        if not raw_id:
            raise ServiceError(422, "TIMELINE_REF_REQUIRED",
                               "timeline_ref.timeline_id is required")
        try:
            revision = int(ref.get("revision"))
        except (TypeError, ValueError):
            raise ServiceError(422, "TIMELINE_REF_REQUIRED",
                               "timeline_ref.revision must be an integer")
        timeline_id = urllib.parse.quote(str(raw_id))
        headers = {"X-Tenant-Id": tenant_id}
        result = _request(
            "GET", f"{prefix}/timelines/{timeline_id}/revisions/{revision}",
            headers=headers, timeout=20.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                               "timeline revision is not an object")
        output_plan = result.get("output_plan")
        mix_plan = result.get("mix_plan")
        if not isinstance(output_plan, dict) or not isinstance(mix_plan, dict):
            # S42-C3：契约读 API 不含计划，试提案路由；拿不到就停。
            try:
                extra = _request(
                    "GET",
                    f"{prefix}/timelines/{timeline_id}/revisions/{revision}/plans",
                    headers=headers, timeout=20.0)
            except ServiceError as exc:
                if isinstance(output_plan, dict) and isinstance(mix_plan, dict):
                    extra = {}
                else:
                    raise ServiceError(
                        502, "TIMELINE_PLAN_UNAVAILABLE",
                        "S41 revision response carried no output_plan/mix_plan "
                        "and the /plans route is unavailable; S42 will not "
                        "derive segments or loudness targets itself",
                        details={"upstream_status": exc.status,
                                 "upstream_code": exc.code}) from exc
            if not isinstance(extra, dict):
                raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                                   "plans response is not an object")
            output_plan = output_plan or extra.get("output_plan")
            mix_plan = mix_plan or extra.get("mix_plan")
        if not isinstance(output_plan, dict) or not isinstance(mix_plan, dict):
            raise ServiceError(
                502, "TIMELINE_PLAN_UNAVAILABLE",
                "S41 did not provide both output_plan and mix_plan")
        segments = output_plan.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ServiceError(
                502, "TIMELINE_PLAN_UNAVAILABLE",
                "output_plan.segments is empty; S42 does not choose segments",
                details={"timeline_id": raw_id, "revision": revision})
        payload = result.get("payload") if isinstance(
            result.get("payload"), dict) else {}
        content_hash = _hash64(result.get("content_hash")) \
            or _hash64(ref.get("content_hash"))
        if content_hash is None:
            raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                               "timeline revision has no 64-hex content_hash",
                               details={"timeline_id": raw_id,
                                        "revision": revision})
        return {
            "timeline_id": result.get("timeline_id") or raw_id,
            "revision": int(result.get("revision") or revision),
            "content_hash": content_hash,
            "duration_ticks": int(result.get("duration_ticks")
                                  or output_plan.get("duration_ticks") or 0),
            "completeness": result.get("completeness"),
            # §4.4：`gaps` 是提交时的快照，**不能**当放行依据；这里只取
            # S41 现算的 `formal_master_eligible`。
            "formal_master_eligible": bool(
                result.get("formal_master_eligible")),
            "tick_rate": int(result.get("tick_rate")
                             or payload.get("tick_rate") or 90000),
            "fps": payload.get("fps"),
            "clips": result.get("clips") or [],
            "output_plan": output_plan,
            "mix_plan": mix_plan,
        }
    return _timeline


def build_timeline_diff(base_url: str | None):
    """`(tenant_id, timeline_id, from_revision, to_revision) -> 受影响区间`。

    只用来抑制段复用（§4.3：交叠即重渲染）。未配置即 503：没有区间信息时
    S42 会**全部重渲染**（缓存不是正确性依赖），但调用方要显式知道这一点，
    不能让「没配 diff」静默退化成「没有受影响区间」。
    """
    if not base_url:
        return _unconfigured(
            "TIMELINE_DIFF_UNAVAILABLE",
            "S41_BASE_URL is not configured; S42 cannot tell which output "
            "ranges changed and will not reuse segments blindly (plan §4.3)")
    prefix = base_url.rstrip("/")

    def _diff(tenant_id: str, timeline_id: str, from_revision: int,
              to_revision: int) -> dict:
        tid = urllib.parse.quote(str(timeline_id))
        url = (f"{prefix}/timelines/{tid}/diff?from={int(from_revision)}"
               f"&to={int(to_revision)}")
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=20.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                               "timeline diff is not an object")
        ranges = result.get("affected_output_ranges")
        if ranges is None:
            ranges = result.get("affected_ranges") or []
        return {"affected_ranges": ranges,
                "diff_mode": result.get("diff_mode") or "exact"}
    return _diff


# --------------------------------------------------------------- S28 artifact

def build_artifact_facts(base_url: str | None):
    """`(tenant_id, ref) -> 制品事实`。

    返回值：

        {"artifact_id", "revision", "content_hash", "state", "tombstoned",
         "mime_type", "duration_ms", "frame_rate", "width_px", "height_px"}

    读**指定的那一版**（`/artifacts/{id}/revisions/{rev}`），因为 §4.2 的 L2
    复查问的是「我这次渲染用的那一版还在不在」，不是「这个制品现在是什么」。
    跨租户在 S28 侧就是 404，本模块原样透传。
    """
    if not base_url:
        return _unconfigured(
            "ARCHIVE_UNAVAILABLE",
            "S28_BASE_URL is not configured; S42 holds no bytes and cannot "
            "verify any render input or output (plan §1)")
    prefix = base_url.rstrip("/")

    def _facts(tenant_id: str, ref: dict) -> dict:
        raw_id = ref.get("artifact_id") or ref.get("id")
        if not raw_id:
            raise ServiceError(422, "ARTIFACT_REF_REQUIRED",
                               "artifact ref has no id")
        artifact_id = urllib.parse.quote(str(raw_id))
        revision = ref.get("revision")
        headers = {"X-Tenant-Id": tenant_id}
        if revision is None:
            meta = _request("GET", f"{prefix}/artifacts/{artifact_id}",
                            headers=headers, timeout=15.0)
            rev = (meta or {}).get("current_revision")
            if rev is None:
                raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                                   "artifact has no current_revision")
            revision = int(rev)
        state = _request(
            "GET", f"{prefix}/artifacts/{artifact_id}", headers=headers,
            timeout=15.0)
        if not isinstance(state, dict):
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact is not an object")
        row = _request(
            "GET", f"{prefix}/artifacts/{artifact_id}/revisions/{int(revision)}",
            headers=headers, timeout=20.0)
        if not isinstance(row, dict):
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact revision is not an object")
        content_hash = _hash64(row.get("content_hash"))
        if content_hash is None:
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact revision has no 64-hex content_hash",
                               details={"artifact_id": raw_id,
                                        "revision": revision})
        return {
            "artifact_id": str(raw_id),
            "revision": int(revision),
            "content_hash": content_hash,
            "state": state.get("state"),
            "tombstoned": state.get("state") == "TOMBSTONED",
            "content_class": state.get("content_class"),
            "mime_type": row.get("mime_type"),
            "duration_ms": row.get("duration_ms"),
            "frame_rate": row.get("frame_rate"),
            "width_px": row.get("width_px"),
            "height_px": row.get("height_px"),
        }
    return _facts


# ------------------------------------------------------------------ S38 subtitle

def build_subtitle_source(base_url: str | None):
    """`(tenant_id, ref) -> {id, revision, content_hash}`。

    S38 的 `GET /subtitle-tracks/{id}/revisions/{rev}` 只回**签名 URL**，不回
    hash（§3.0）。hash 在轨道响应的 `renditions[]` 里，所以读轨道再按
    revision 取那一行的 `content_hash`；取不到就 502，不把 URL 当 hash。
    """
    if not base_url:
        return _unconfigured(
            "SUBTITLE_SOURCE_UNAVAILABLE",
            "S38_BASE_URL is not configured; S42 cannot tell whether the "
            "subtitle track it rendered against is still current (plan §4.6)")
    prefix = base_url.rstrip("/")

    def _subtitle(tenant_id: str, ref: dict) -> dict:
        raw_id = ref.get("artifact_id") or ref.get("id") or \
            ref.get("track_id")
        if not raw_id:
            raise ServiceError(422, "SUBTITLE_REF_REQUIRED",
                               "subtitle_ref has no id")
        revision = ref.get("revision")
        track = _request(
            "GET", f"{prefix}/subtitle-tracks/{urllib.parse.quote(str(raw_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(track, dict):
            raise ServiceError(502, "SUBTITLE_BAD_RESPONSE",
                               "subtitle track is not an object")
        renditions = track.get("renditions") or []
        candidates = [r for r in renditions
                      if revision is None
                      or int(r.get("subtitle_revision") or 0) == int(revision)]
        if not candidates:
            raise ServiceError(502, "SUBTITLE_BAD_RESPONSE",
                               "subtitle track has no rendition for the "
                               "requested revision",
                               details={"subtitle_id": raw_id,
                                        "revision": revision})
        latest = max(candidates,
                     key=lambda r: int(r.get("subtitle_revision") or 0))
        content_hash = _hash64(latest.get("content_hash"))
        if content_hash is None:
            raise ServiceError(502, "SUBTITLE_BAD_RESPONSE",
                               "rendition has no 64-hex content_hash")
        return {"id": str(raw_id),
                "revision": int(latest.get("subtitle_revision") or revision or 0),
                "content_hash": content_hash}
    return _subtitle


# ------------------------------------------------------------- master spec

def build_master_spec_source(base_url: str | None):
    """`(tenant_id, ref) -> {"spec": {...}}`。

    **项目规格没有权威服务**（方案 §11 C1）。这里只是一个提案读口；未配置
    即 503，S42 不会退回任何默认编码器或响度容差（§1.2）。

    规格形状（本服务用到的部分）：

        {"container": "mp4", "video_codec": "h264",
         "video_params": {...}, "audio_codec": "aac", "audio_params": {...},
         "color": {"primaries", "transfer", "matrix"},
         "loudness": {"target_lufs": -16.0, "tolerance_lu": 1.0,
                      "true_peak_ceiling_dbtp": -1.0}}
    """
    if not base_url:
        return _unconfigured(
            "MASTER_SPEC_UNAVAILABLE",
            "no master-spec authority is configured (plan §11 C1); S42 will "
            "not default to H.264 or any loudness target")
    prefix = base_url.rstrip("/")

    def _spec(tenant_id: str, ref: dict) -> dict:
        raw_id = ref.get("id")
        if not raw_id:
            raise ServiceError(422, "MASTER_SPEC_REQUIRED",
                               "master_spec_ref.id is required")
        revision = ref.get("revision") or 1
        result = _request(
            "GET",
            f"{prefix}/specs/{urllib.parse.quote(str(raw_id))}"
            f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "MASTER_SPEC_BAD_RESPONSE",
                               "master spec is not an object")
        spec = result.get("spec", result)
        if not isinstance(spec, dict) or not spec:
            raise ServiceError(502, "MASTER_SPEC_BAD_RESPONSE",
                               "master spec has no content")
        return {"spec": spec}
    return _spec


# ------------------------------------------------------------------ S22 permit

def build_permit_source(base_url: str | None):
    """`(tenant_id, request) -> {permit_id, pool_id, state, lease_expires_at}`。

    §1.1：渲染**不是**付费模型调用，但**仍要许可**——`resource_class` 是
    `gpu_second` / `cpu_media_second`，与 S26 的供应商并发额度是两条线。
    """
    if not base_url:
        return _unconfigured(
            "PERMIT_UNAVAILABLE",
            "S22_BASE_URL is not configured; S42 will not start GPU/CPU media "
            "work without a resource-second permit (plan §1.1, §4.1 step 6)")
    prefix = base_url.rstrip("/")

    def _permit(tenant_id: str, request: dict) -> dict:
        result = _request("POST", f"{prefix}/permits:acquire", body=request,
                          headers={"X-Tenant-Id": tenant_id,
                                   "X-Requested-With": "S42"},
                          timeout=20.0)
        if not isinstance(result, dict) or not result.get("permit_id"):
            raise ServiceError(502, "PERMIT_BAD_RESPONSE",
                               "permit-acquire did not return a permit_id")
        return result
    return _permit


# ------------------------------------------------------------ S23 usage facts

def build_usage_reporter(base_url: str | None):
    """`(tenant_id, request) -> fact`。**只上报用量，不预留、不扣减**（§1）。"""
    if not base_url:
        return _unconfigured(
            "USAGE_REPORT_UNAVAILABLE",
            "S23_BASE_URL is not configured; render usage facts stay local "
            "with reported_to_s23=0 (plan §1, §4.1 step 12)")
    prefix = base_url.rstrip("/")

    def _report(tenant_id: str, request: dict) -> dict:
        result = _request("POST", f"{prefix}/usage-facts", body=request,
                          headers={"X-Tenant-Id": tenant_id,
                                   "X-Requested-With": "S42"},
                          timeout=20.0)
        return result if isinstance(result, dict) else {}
    return _report


# ------------------------------------------------------------------- assemble

def build_all(env=None) -> dict:
    """从环境变量组装全部回调。全部未配置时服务仍可起（每个回调各自 503）。"""
    import os

    env = env if env is not None else os.environ
    s41 = env.get("S41_BASE_URL")
    s28 = env.get("S28_BASE_URL")
    s38 = env.get("S38_BASE_URL")
    s22 = env.get("S22_BASE_URL")
    s23 = env.get("S23_BASE_URL")
    spec = env.get("MASTER_SPEC_BASE_URL")
    return {
        "timeline_source": build_timeline_source(s41),
        "timeline_diff": build_timeline_diff(s41),
        "artifact_facts": build_artifact_facts(s28),
        "subtitle_source": build_subtitle_source(s38),
        "master_spec_source": build_master_spec_source(spec),
        "permit_source": build_permit_source(s22),
        "usage_reporter": build_usage_reporter(s23),
    }
