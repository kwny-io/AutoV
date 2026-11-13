"""S37 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

四条读路径：

- **S28** `services/artifact-ingest`：`GET /artifacts/{id}` 或
  `/artifacts/{id}/revisions/{revision}`。S37 只读元数据（`content_hash`、
  `audio_track_hash`、`duration_ms` 等），**不下载媒体**。未配置必然 503：
  报告必须钉在"被测量的那一份"上，读不到元数据就不能报。
- **S26** `services/semantic-qc`… 不，S26 是 `speech-production` 的编排上游，
  本仓没有它的分析产物契约（S37-5）。所以 `build_analysis_source` 的协议是
  **本服务自己定的**：`POST {prefix}/understanding-jobs:analyze` 提交一份带
  `analysis_version: "s37-analysis.v1"` 的请求，同步拿回分析文档。未配置时
  必然失败（503），绝不用"空分析"假装测过。
- **S15** `services/voice-assets`：
  - `GET /voices/{id}/revisions/{revision}` → `{voice_id, revision,
    lexicon_revision, lexicon{...}}`（发音词典）。
  - `GET /voices/{id}/authorization?revision=` → `{effective_state, state,
    state_seq, ...}`（音色授权）。**授权只问 S15，不看本服务的投影**：
    投影只用于发现"读到的比事件旧"。
- **S06** `services/storyboard` 是 `design_only`，ShotSpec 读路径与
  `services/technical-qc/src/technical_qc/deps.py` 同形（`GET /shots/{id}`）。
  未配置返回 `None`，不是 503：ShotSpec 是查询级依赖。

**未配置一律必然失败或明确缺席**，没有"默认值兜底"：S37 的产物是判定，
用一个默认阈值兜底等于替租户决定交付标准。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError


def _request(method: str, url: str, *, body: dict | None = None,
             headers: dict | None = None, timeout: float = 15.0) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
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


# ---------------------------------------------------------------- S28 artifact

def build_artifact_source(base_url: str | None):
    """`(tenant_id, artifact_ref) -> S28 的 artifact 元数据`。

    带 `revision` 时读指定修订——报告要钉住被测量的那一份，不能读"当前"。
    404 / 409 原样抛出，由 `service._artifact` 决定语义（404 → 无法测量）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28_BASE_URL is not configured; S37 will not report on an "
                "artifact it cannot read (plan §2.6)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _artifact(tenant_id: str, ref: dict) -> dict:
        artifact_id = urllib.parse.quote(str(ref["id"]))
        revision = ref.get("revision")
        if revision is None:
            url = f"{prefix}/artifacts/{artifact_id}"
        else:
            url = f"{prefix}/artifacts/{artifact_id}/revisions/{int(revision)}"
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _artifact


# ------------------------------------------------------------- S26 analysis

def build_analysis_source(base_url: str | None):
    """`(tenant_id, request) -> s37-analysis.v1 文档`。

    这是**本服务与 S26 之间尚未成文的私有协议**（S37-5）。待定的恰好是
    "谁来做 ASR/forced alignment"——S37 不做模型推理，也不该假装 S26 已有
    产物契约。未配置时命令 503，**不**降级成 `abstain`：abstain 的意思是
    "查了但不确定"，而这里根本没有可查的证据。

    请求体形状见 `service._analysis`；响应必须是 `analysis_version ==
    "s37-analysis.v1"` 的对象。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ANALYSIS_SOURCE_UNAVAILABLE",
                "S26_BASE_URL is not configured; S37 has no analysis document "
                "to report on and will not fabricate one (plan §2, S37-5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _analysis(tenant_id: str, request: dict) -> dict:
        return _request("POST", f"{prefix}/understanding-jobs:analyze",
                        body=request,
                        headers={"X-Tenant-Id": tenant_id,
                                 "X-Requested-With": "S37"},
                        timeout=120.0)
    return _analysis


# ---------------------------------------------------------------- S06 ShotSpec

def build_shot_source(base_url: str | None):
    """`(tenant_id, shot_ref, execution_context) -> ShotSpec | None`。

    只读两项：`dialogue`（**不来自 ASR**，用于"ShotSpec 有对白但命令没给
    文本"这条 422 门）与 `dialogue_duration_ms`（`AUD.ALIGN.DURATION_VS_SPEC`
    的兜底基准）。

    未配置返回 `None`：ShotSpec 是查询级依赖，不该让整条命令失败；依赖
    它的项落 `not_evaluated`。404 / 410 也返回 `None`，理由同上。
    """
    if not base_url:
        def _absent(*_args, **_kwargs):
            return None
        return _absent

    prefix = base_url.rstrip("/")

    def _shot(tenant_id: str, ref: dict, context: dict) -> dict | None:
        query = {}
        for key in ("series_id", "branch_id"):
            if context.get(key):
                query[key] = context[key]
        if ref.get("revision") is not None:
            query["revision"] = ref["revision"]
        url = (f"{prefix}/shots/{urllib.parse.quote(str(ref['id']))}"
               + (f"?{urllib.parse.urlencode(query)}" if query else ""))
        try:
            result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                              timeout=10.0)
        except ServiceError as exc:
            if exc.status in (404, 410):
                return None
            raise
        if not isinstance(result, dict):
            return None
        return {"id": result.get("shot_id") or ref.get("id"),
                "revision": result.get("revision") or ref.get("revision"),
                **({"dialogue": result["dialogue"]}
                   if "dialogue" in result else {}),
                **({"dialogue_duration_ms": result["dialogue_duration_ms"]}
                   if "dialogue_duration_ms" in result else {})}
    return _shot


# ------------------------------------------------------------------- S15 voice

def build_lexicon_source(base_url: str | None):
    """`(tenant_id, ref) -> {revision, lexicon{...}}`（发音词典）。

    404 抛 404；调用方（`service._lexicon`）把它折成 `None` →
    `AUD.TEXT.MISPRONOUNCE` = `not_evaluated(dependency_unavailable)`。
    """
    if not base_url:
        def _absent(*_args, **_kwargs):
            return None
        return _absent

    prefix = base_url.rstrip("/")

    def _lexicon(tenant_id: str, ref: dict) -> dict | None:
        voice_id = urllib.parse.quote(str(ref["id"]))
        revision = ref.get("revision")
        if revision is None:
            url = f"{prefix}/voices/{voice_id}"
        else:
            url = f"{prefix}/voices/{voice_id}/revisions/{int(revision)}"
        try:
            return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                            timeout=10.0)
        except ServiceError as exc:
            if exc.status in (404, 410):
                return None
            raise
    return _lexicon


def build_voice_source(base_url: str | None):
    """`(tenant_id, ref) -> {state, effective_state, state_seq, ...}`。

    **实时回查**，不读本服务的 `voice_authorization_projection`：投影可能
    比事件旧，而"授权已撤销"晚一小步就是一次越权合成（AGENTS.md：
    不得预填 active）。
    """
    if not base_url:
        def _absent(*_args, **_kwargs):
            return None
        return _absent

    prefix = base_url.rstrip("/")

    def _authorization(tenant_id: str, ref: dict) -> dict | None:
        voice_id = urllib.parse.quote(str(ref["id"]))
        query = {}
        if ref.get("revision") is not None:
            query["revision"] = int(ref["revision"])
        url = (f"{prefix}/voices/{voice_id}/authorization"
               + (f"?{urllib.parse.urlencode(query)}" if query else ""))
        try:
            return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                            timeout=10.0)
        except ServiceError as exc:
            if exc.status in (404, 410):
                return None
            raise
    return _authorization
