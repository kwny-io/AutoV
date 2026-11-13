"""S38 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

六条路径，全部在 `build_*` 里闭合；**未配置时的行为分三种**，不是一律 503：

| 依赖 | 不可达时 | 为什么 |
|---|---|---|
| S37 对齐 | 503 `ALIGNMENT_UNAVAILABLE` | 时间码的唯一来源；没有它排不了版 |
| S41 剪辑 | 503 `TIMELINE_UNAVAILABLE` | cue 的 clip 参数与时长都来自它 |
| S28 归档 | 503 `ARCHIVE_UNAVAILABLE` | 渲染可以完成，但字节无处可放 |
| S06 台词 | 503 `DIALOGUE_UNAVAILABLE` | 源文本是要交付的东西，不能猜 |
| S15 词典 | `None`（缺席） | 只影响读音判定，报告走 abstain（C2） |
| S26 翻译 | 503 `TRANSLATION_UNAVAILABLE` | 付费任务；本服务**从不**调用它 |

最后一条要特别说明：`build_translation_source` 只存在于"未来的重译入口"，
S38 的内核没有任何代码路径会调它。方案 §9.2 明确要求 stale 译文**不自动
重译**（那是付费 S26 任务且需人验收），所以它不是可选优化，而是被禁用的
动作。把它做成一个能用的 callback 会诱使后来者接上线。

**没有硬编码 provider URL。** `base_url` 一律来自环境变量，由 `api/app.py`
注入；`deps.py` 里出现的路径是本仓服务之间的内部契约路径。
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


def _unconfigured(code: str, detail: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, code, detail)
    return _fail


# ---------------------------------------------------------------- S37 alignment

def build_alignment_source(base_url: str | None):
    """`(tenant_id, ref, generation) -> {report, units}`。

    S37 的 `GET /alignment-reports/{id}` 给报告，`/units` 给词级区间
    （`time_base="presentation_pts"`）。**两者都要**：报告说明这一版对齐
    是否 pass（abstain 时 S38 不能装成有时间码），units 才是 cue 的边界
    依据（§4.1 明说用 CI 边界而不是点估计）。

    未配置必然 503：没有 S37 就没有时间码，S38 既不能渲染也不能判。
    """
    if not base_url:
        return _unconfigured(
            "ALIGNMENT_UNAVAILABLE",
            "S37_BASE_URL is not configured; S38 has no word-level timecodes "
            "and will not invent them (plan §4.1)")
    prefix = base_url.rstrip("/")

    def _alignment(tenant_id: str, ref: dict, generation: str = "latest") -> dict:
        report_id = urllib.parse.quote(str(ref["id"]))
        query = "" if generation in (None, "", "latest") \
            else f"?generation={urllib.parse.quote(str(generation))}"
        headers = {"X-Tenant-Id": tenant_id}
        report = _request("GET", f"{prefix}/alignment-reports/{report_id}{query}",
                          headers=headers, timeout=15.0)
        units = _request("GET", f"{prefix}/alignment-reports/{report_id}/units{query}",
                         headers=headers, timeout=30.0)
        return {"report": report, "units": units}
    return _alignment


# ------------------------------------------------------------------ S41 timeline

def build_timeline_source(base_url: str | None):
    """`(tenant_id, ref) -> {tick_rate, duration_ticks, clips[], completeness}`。

    S41 是时间线的权威。S38 读它做三件事：cue 的 clip 归属、`OUT_OF_RANGE`
    的上界、以及 §3.1 增量重算时的新旧 clip 参数。

    `tick_rate` 不在 `post.timeline.revision.created` 里（PTR-2），只能从这里
    读；读不到就不做增量（返回 503 让上游重投），绝不假定 90000。
    """
    if not base_url:
        return _unconfigured(
            "TIMELINE_UNAVAILABLE",
            "S41_BASE_URL is not configured; S38 cannot place cues on a "
            "timeline it cannot read (plan §3.1, PTR-2)")
    prefix = base_url.rstrip("/")

    def _timeline(tenant_id: str, ref: dict) -> dict:
        timeline_id = urllib.parse.quote(str(ref["id"]))
        revision = int(ref.get("revision") or 1)
        url = f"{prefix}/timelines/{timeline_id}/revisions/{revision}"
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=20.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "TIMELINE_BAD_RESPONSE",
                               "timeline revision is not an object")
        # 只取本服务用得到的字段；不把整份时间线抄进本地。
        return {
            "timeline_id": result.get("timeline_id") or ref["id"],
            "revision": result.get("revision") or revision,
            "tick_rate": result.get("tick_rate"),
            "duration_ticks": result.get("duration_ticks"),
            "completeness": result.get("completeness"),
            "clips": result.get("clips") or [],
        }
    return _timeline


# --------------------------------------------------------------- S06 dialogue

def build_dialogue_source(base_url: str | None):
    """`(tenant_id, ref) -> {lines: [{line_id, text, ...}], ...}`。

    源台词是 S06 的。S38 只用它来：把 `alignment_unit → script_line_id`
    对上、算覆盖率的分母、以及为 `SUB.TEXT.SOURCE_STALE` 提供基准 revision。

    未配置 503：没有源文本就没有字幕可排，用空台词"通过"是最坏的假绿。
    """
    if not base_url:
        return _unconfigured(
            "DIALOGUE_UNAVAILABLE",
            "S06_BASE_URL is not configured; S38 will not render subtitles "
            "from an empty dialogue (plan §7)")
    prefix = base_url.rstrip("/")

    def _dialogue(tenant_id: str, ref: dict) -> dict:
        script_id = urllib.parse.quote(str(ref["id"]))
        revision = int(ref.get("revision") or 1)
        url = f"{prefix}/scripts/{script_id}/revisions/{revision}"
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=20.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "DIALOGUE_BAD_RESPONSE",
                               "dialogue revision is not an object")
        return {"script_id": result.get("script_id") or ref["id"],
                "revision": result.get("revision") or revision,
                "lines": result.get("lines") or []}
    return _dialogue


# ------------------------------------------------------------------- S15 lexicon

def build_lexicon_source(base_url: str | None):
    """`(tenant_id, ref) -> {revision, lexicon{...}} | None`。

    与 S37 同一路径（`GET /voices/{id}/revisions/{rev}`）。未配置返回
    `None`，不是 503：词典是**查询级**依赖，它缺席只让
    `SUB.TEXT.LEXICON_UNUSED` 落 `not_evaluated(dependency_unavailable)`，
    整条命令不该因此失败。缺了这一项的报告会 abstain（C2）。
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


# --------------------------------------------------------------- S28 artifact ingest

def build_archive_source(base_url: str | None):
    """`(tenant_id, request) -> {artifact_id, content_hash, byte_size}`。

    把渲染好的字节交给 S28，换回一个不可变的 `art_<ulid>`。**S38 不落字节**
    （§3.0）；这个 callback 是字节走出本服务的唯一出口。

    请求体形状：

        {"media_type": "application/x-subrip",
         "content_b64": "<base64>",
         "content_hash": "<sha256 hex>",
         "metadata": {"subtitle_track_id": ..., "subtitle_revision": ...,
                      "format": "srt", "target_locale": ...}}

    未配置 503：渲染可以完成，但轨道停在 `rendered_pending_archive` 且
    **不发** `post.subtitle.ready`（§3.0）。
    """
    if not base_url:
        return _unconfigured(
            "ARCHIVE_UNAVAILABLE",
            "S28_BASE_URL is not configured; S38 holds no subtitle bytes and "
            "cannot publish a track it could not archive (plan §3.0)")
    prefix = base_url.rstrip("/")

    def _archive(tenant_id: str, request: dict) -> dict:
        result = _request("POST", f"{prefix}/artifacts:ingest", body=request,
                          headers={"X-Tenant-Id": tenant_id,
                                   "X-Requested-With": "S38"},
                          timeout=120.0)
        if not isinstance(result, dict):
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact-ingest did not return an object")
        artifact_id = result.get("artifact_id")
        # 契约（C9）不强制 ID 空间，但 §3.0 要求字节由 S28 持有。一个不以
        # art_ 开头的 id 不是 S28 指针，当成功处理会让 rendition 指向虚无。
        if not isinstance(artifact_id, str) or not artifact_id.startswith("art_"):
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact_id is not in the S28 ID space",
                               details={"artifact_id": artifact_id})
        content_hash = result.get("content_hash")
        byte_size = result.get("byte_size")
        if not isinstance(content_hash, str) or len(content_hash) != 64 \
                or not isinstance(byte_size, int) or byte_size <= 0:
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact-ingest did not return content_hash/"
                               "byte_size",
                               details={"content_hash": content_hash,
                                        "byte_size": byte_size})
        return {"artifact_id": artifact_id, "content_hash": content_hash,
                "byte_size": byte_size}
    return _archive


def build_artifact_url_source(base_url: str | None):
    """`(tenant_id, artifact_id) -> {url, expires_at}`（S28 的签名下载）。

    查询面 `GET /subtitle-tracks/{id}/revisions/{rev}?format=` 只回签名 URL，
    不回字节（§3.0）。未配置 503：S38 没有别的办法给出下载地址。
    """
    if not base_url:
        return _unconfigured(
            "ARTIFACT_SOURCE_UNAVAILABLE",
            "S28_BASE_URL is not configured; S38 cannot sign a download URL "
            "for bytes it does not hold (plan §3.0)")
    prefix = base_url.rstrip("/")

    def _url(tenant_id: str, artifact_id: str) -> dict:
        result = _request(
            "GET",
            f"{prefix}/artifacts/{urllib.parse.quote(artifact_id)}/content-url",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(result, dict) or not result.get("url"):
            raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                               "artifact-ingest did not return a signed url")
        return {"url": result["url"],
                "expires_at": result.get("expires_at")}
    return _url


# ------------------------------------------------------------- S26 translation

def build_translation_source(base_url: str | None):
    """`(tenant_id, request) -> 译文任务`。**本内核从不调用。**

    方案 §9.2：stale 译文只能人工触发重译，且那是付费 S26 任务。把它做成
    可配置的 callback 只是为了将来接入口时形状明确；未配置时 503，配置了
    也**没有调用点**。测试断言翻译任务计数恒为 0（E25-V5）。
    """
    if not base_url:
        return _unconfigured(
            "TRANSLATION_UNAVAILABLE",
            "S26_BASE_URL is not configured; S38 never auto-retranslates "
            "(paid task, needs human acceptance; plan §9.2)")
    prefix = base_url.rstrip("/")

    def _translate(tenant_id: str, request: dict) -> dict:
        return _request("POST", f"{prefix}/translation-jobs", body=request,
                        headers={"X-Tenant-Id": tenant_id,
                                 "X-Requested-With": "S38"},
                        timeout=120.0)
    return _translate
