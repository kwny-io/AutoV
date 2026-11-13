"""S35 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

三条读路径，各自的形状都是**读上游代码核对过的**（不是从方案里抄的）：

- **S28** `services/artifact-ingest`：
  - `GET /artifacts/{id}` → `{artifact_id, revision, content_hash, size_bytes,
    mime_type, duration_ms, width_px, height_px, frame_rate{num,den}, ...}`。
    注意字段名是 `width_px` / `height_px` / `duration_ms`，不是场景里的
    `width` / `duration_seconds`——`service._video_from_artifact` 负责换算。
  - `GET /artifacts/{id}/revisions/{revision}` 同形状。
  - `POST /media-access-tickets` 换一个短签名 URL（`purpose="qc"`）。
  - **没有**探测接口。§3.2 第 3 步的媒体沙箱（ffprobe/ffmpeg/ebur128）
    在本仓还不存在，所以 `build_probe_source` 的协议是本服务自己定的，
    并且在未配置时**必然失败**，不伪造探针结果。
- **S06** `services/storyboard` 是 `design_only`（没有 `src/`、没有 `api/`）。
  ShotSpec 的读路径按 §6.1 的 `shot_spec_ref` 形状**假设**为
  `GET {prefix}/shots/{id}?revision=&series_id=&branch_id=`，与
  `services/continuity/src/continuity/deps.py` 的既有约定一致。
  未配置 = 返回 `None` → `TECH.DURATION.MISMATCH` / `TECH.AUDIO.MISSING`
  落 `not_evaluated`，整份报告 abstain（S35-7）。**不猜"大概没对白"。**
- **S02** `GET /policies/{id}/revisions/{revision}` → `{policy_ref, policy}`。

**未配置一律必然失败**（S28 / S02）。S35 的产物是"合格/不合格"的判定，
用一个默认阈值兜底等于替租户决定交付标准——那比 503 危险得多。
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
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S28 artifact

def build_artifact_source(base_url: str | None):
    """`(tenant_id, artifact_ref) -> S28 的 artifact 摘要`。

    `artifact_ref` 是命令体的 `{service_id, id, revision, content_hash}`。
    带 `revision` 时读指定修订——报告要钉住被测量的那一份，不能读"当前"。
    404 / 409 原样抛出：那是"这个 artifact 取不到"，命令层转 422（§6.1）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28_BASE_URL is not configured; S35 will not report on an "
                "artifact it cannot read (plan §3.2)")
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


def build_probe_source(base_url: str | None):
    """`(tenant_id, artifact_ref, artifact_kind) -> 探针文档`。

    §3.2 第 2–3 步（短签名 URL + 媒体沙箱）。本仓还没有沙箱服务，所以这个
    协议是本服务自己定的；未配置时**必然失败**（503），由 service 降级成
    `abstain`，绝不用"空探针"假装测过（S35-18）。

    探针文档的形状见 `technical_qc.evaluation` 的模块 docstring。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "PROBE_UNAVAILABLE",
                "no media probe sandbox is configured; S35 will abstain "
                "rather than report on an unmeasured artifact (plan §3.2)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _probe(tenant_id: str, ref: dict, artifact_kind: str) -> dict:
        url = (f"{prefix}/probes?" + urllib.parse.urlencode({
            "artifact_id": ref.get("id"),
            "revision": ref.get("revision"),
            "content_hash": ref.get("content_hash"),
            "artifact_kind": artifact_kind,
        }))
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=60.0)
    return _probe


# ---------------------------------------------------------------- S06 ShotSpec

def build_shot_source(base_url: str | None):
    """`(tenant_id, shot_ref, execution_context) -> ShotSpec`。

    只读两项：`duration_seconds`（§4 `TECH.DURATION.MISMATCH`）与
    `dialogue`（§4 `TECH.AUDIO.MISSING`，**不来自 ASR**）。返回 `None` 时
    调用方把这两项落 `not_evaluated`；服务层不区分"shot 不存在"与"读不到"，
    因为两者对判定的后果相同：这一条没查成。

    S06 目前是 `design_only`，所以这里的路径是**假设的**（与 continuity 的
    `build_shot_source` 同形）。未配置返回 `None`，不是 503：ShotSpec 是
    查询级依赖（C3），不该让整条命令失败。
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
        return {"shot_id": result.get("shot_id") or ref.get("id"),
                "shot_revision": result.get("revision") or ref.get("revision"),
                "duration_seconds": result.get("duration_seconds"),
                # 声明缺失必须是"缺键"，而不是 `None` 或 `[]`：§4 末段要求
                # 声明缺失 → not_applicable(no_dialogue)，而不是猜"没有对白"。
                **({"dialogue": result["dialogue"]}
                   if "dialogue" in result else {})}
    return _shot


# ---------------------------------------------------------------- S02 profile

def build_policy_source(base_url: str | None):
    """`(tenant_id, policy_ref, profile_ref) -> technical_qc_profile`。

    读 `policy.technical_qc` 段：`thresholds` / `target` / `allowed_actions` /
    `subtitle_policy` / `policy_version` / `evaluator_*`。**返回体里必须有
    `policy_version`**：报告要记"这一份判定用的是哪版阈值"，缺了就无法复核。
    没有 `technical_qc` 段时 503——"策略没说"不能等于"用内置阈值"。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                "S02_BASE_URL is not configured; S35 will not fall back to "
                "built-in QC thresholds, because a default threshold may be "
                "looser than the tenant's delivery contract (plan §4)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _resolve(tenant_id: str, ref: dict, profile_ref: dict | None) -> dict:
        policy_id = ref.get("id")
        revision = ref.get("revision")
        if not policy_id or revision is None:
            raise ServiceError(422, "INVALID_TECHNICAL_CHECK",
                               "execution_context.policy_ref needs id and "
                               "revision")
        result = _request(
            "GET", f"{prefix}/policies/{urllib.parse.quote(str(policy_id))}"
                   f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        document = result.get("policy")
        if not isinstance(document, dict):
            raise ServiceError(503, "POLICY_UNAVAILABLE",
                               "S02 returned a policy revision with no document")
        section = document.get("technical_qc")
        if not isinstance(section, dict):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                f"policy {policy_id}@{revision} has no technical_qc section; "
                f"S35 will not measure against built-in thresholds")
        profile = dict(section)
        # 命令体的 `profile_ref` 只覆盖目标（画幅/帧率/响度），不覆盖阈值：
        # 目标是"这次要交付成什么样"，阈值是合规线，后者只能来自策略。
        requested = (profile_ref or {}).get("target")
        if isinstance(requested, dict):
            profile["target"] = {**(profile.get("target") or {}), **requested}
        profile.setdefault("policy_id", policy_id)
        profile.setdefault("revision", revision)
        profile.setdefault("policy_version",
                           str(result.get("policy_ref", {}).get("revision")
                               or revision))
        return profile
    return _resolve
