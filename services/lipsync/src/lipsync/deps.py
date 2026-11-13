"""上游回调。**本服务不直连任何供应商，也不付任何钱。**

六个上游，六份职责，不能混：

- **S28**（artifact-ingest）：视频/音频是否仍可引用、时长、帧数、讨论 fps。
  同时承担跨租户判定：别的租户的引用在这里是 404。
- **S31**（speech-production）：音频 take 的 `rights_state`，以及它是不是
  已经被新的合成取代。**这是"音频能不能用"的唯一权威**——本服务不自己
  记一份授权状态。
- **S37**（audio-alignment）：`AUD.TEXT.*` 缺陷与对齐报告引用。没有报告 =
  `ran=False`，服务据此放行并打标 `not_yet_available`（§4.3）。
- **S10**（character-assets）：人脸授权状态。`lapsed` 时拒绝做口型（F4）。
- **S25**（model-routing）：路由决策。只读，不产生费用。
- **S26**（generation-jobs）：**唯一**提交付费任务的地方。本服务不给供应商
  发 HTTP，也不知道供应商是谁。

未配置的上游返回 503 且带自己的 code，绝不静默降级成"可以用"。这不是
保守过度：本服务每一个 503 都对应一个"如果猜错就要么违反授权、要么花错
钱"的判断点。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .service import ServiceError

DEFAULT_TIMEOUT = 15.0


def _request(method: str, url: str, *, body=None, headers=None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    payload = None
    hdrs = {"accept": "application/json"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["content-type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=payload, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise ServiceError(
            exc.code, payload.get("code") or "UPSTREAM_ERROR",
            payload.get("message") or f"upstream returned {exc.code}",
            retryable=bool(payload.get("retryable")),
            details=payload.get("details") or {}) from exc
    except (urllib.error.URLError, OSError) as exc:
        # 连接层失败 → ConnectionError。service.py 把它读成"不可达"，而
        # "不可达"永远不等于"可用"（E30 / E23）。
        raise ConnectionError(f"{method} {url}: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConnectionError(f"{method} {url}: non-JSON response") from exc


def _unconfigured(code: str, message: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, code, message, retryable=True)
    return _fail


# ------------------------------------------------------------------ S28

def build_artifact_facts(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """`GET /artifacts/{id}`：取归档事实。

    `frame_rate_num` / `frame_rate_den` 是 `artifact.ingested.v2` 申明的
    字段，而 `frame_count` / `timecode_start` **不是**（缺口 C4）。S28 的
    详情接口是这三样事实的唯一来源，所以本回调返回 `None` 而不是编一个值。
    """
    if not base_url:
        return _unconfigured("ARTIFACT_FACTS_UNAVAILABLE",
                             "S28 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, artifact_ref: dict) -> dict | None:
        if not isinstance(artifact_ref, dict) or not artifact_ref.get("id"):
            return None
        raw = _request("GET", f"{root}/artifacts/{artifact_ref['id']}",
                       headers={tenant_header: tenant_id})
        return {
            "frame_count": raw.get("frame_count"),
            "timecode_start": raw.get("timecode_start"),
            "fps": raw.get("fps"),
            "frame_rate_num": raw.get("frame_rate_num"),
            "frame_rate_den": raw.get("frame_rate_den"),
            "duration_ms": raw.get("duration_ms"),
        }

    return _read


def build_artifact_check(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """归档产物是否仍 `REFERENCEABLE`。

    三态刻意保留：`None` = 不知道。本服务的复用与 `/validity` 把 `None`
    当**不可用**处理——宁可多花一次口型的钱，也不把一个自己没校验过的
    对象递给下游（E23）。
    """
    if not base_url:
        return _unconfigured("ARTIFACT_CHECK_UNAVAILABLE",
                             "S28 is not configured")

    root = base_url.rstrip("/")

    def _check(tenant_id: str, artifact_ref: dict) -> bool | None:
        if not isinstance(artifact_ref, dict) or not artifact_ref.get("id"):
            return False
        raw = _request("GET", f"{root}/artifacts/{artifact_ref['id']}",
                       headers={tenant_header: tenant_id})
        state = str(raw.get("state") or "").upper()
        if not state:
            return None
        return state in ("AVAILABLE", "REFERENCEABLE")

    return _check


def build_refresher(base_url: str | None, *, tenant_header="x-tenant-id"):
    """`POST /artifacts/{id}:refresh-url`：归档 URL 过期时换一个（F22）。

    这条路径的全部理由是**省钱**：字节已经在 S28 里，只是取不出来。重跑
    一次生成也能"解决"，但那是拿一次新的付费调用换一个 HTTP 超时。
    """
    if not base_url:
        return _unconfigured("ARTIFACT_URL_REFRESH_UNAVAILABLE",
                             "S28 is not configured")

    root = base_url.rstrip("/")

    def _refresh(tenant_id: str, artifact_ref: dict, source: dict) -> dict:
        return _request(
            "POST", f"{root}/artifacts/{artifact_ref.get('id')}:refresh-url",
            body={"reason_code": "URL_EXPIRED"},
            headers={tenant_header: tenant_id})

    return _refresh


# ------------------------------------------------------------------ S31

def build_rights_reader(base_url: str | None, *,
                        tenant_header="x-tenant-id"):
    """`GET /voice-takes/{id}`：读音频 take 的 `rights_state`。

    **必须 live 读。** 撤销发生在归档之后，所以任何被拷进 take 的值在撤销
    当晚就变成了历史。`/validity` 与复用判定都走这一条（§4.2）。

    别的租户的 take 在这里是 404 → 服务转成"引用不可达"（E28）。
    """
    if not base_url:
        return _unconfigured("VOICE_RIGHTS_UNAVAILABLE",
                             "S31 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, audio_take_id: str) -> dict:
        raw = _request("GET", f"{root}/voice-takes/{audio_take_id}",
                       headers={tenant_header: tenant_id})
        return {"rights_state": raw.get("rights_state"),
                "state": raw.get("state"),
                "artifact_ref": raw.get("artifact_ref"),
                "observed_duration_ms": raw.get("observed_duration_ms"),
                "alignment_report_ref": raw.get("alignment_report_ref")}

    return _read


# ------------------------------------------------------------------ S37

def build_s37_reader(base_url: str | None, *, tenant_header="x-tenant-id"):
    """`GET /alignment-reports/{id}` → 归一成 `{ran, defects, report_ref}`。

    两件事刻意分开：
    - 没有报告（404）→ `ran=False`。**不是错误**：首轮生产本来就可能跑在
      QC 之前（§4.3），服务据此放行并打标。
    - 有报告 → 原样给出缺陷列表，由内核按 `AUD.TEXT.*` + `major/blocker`
      过滤。**deps 不做业务判断**：把"哪些缺陷算阻断"写在两处，就一定会
      有两处不一致。

    `AUD.TEXT.SCRIPT_CONFLICT` 不需要在这里剔除——内核的
    `NON_BLOCKING_TEXT_CODES` 负责，而且那个过滤器有测试。
    """
    if not base_url:
        return _unconfigured("ALIGNMENT_REPORT_UNAVAILABLE",
                             "S37 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, audio_take_id: str | None,
              artifact_ref: dict) -> dict:
        params = []
        if audio_take_id:
            params.append(f"voice_take_id={audio_take_id}")
        if artifact_ref.get("id"):
            params.append(f"artifact_id={artifact_ref['id']}")
        if not params:
            return {"ran": False, "defects": [], "report_ref": None}
        try:
            raw = _request(
                "GET", f"{root}/alignment-reports?{'&'.join(params)}",
                headers={tenant_header: tenant_id})
        except ServiceError as exc:
            if exc.status == 404:
                return {"ran": False, "defects": [], "report_ref": None}
            raise
        report_id = raw.get("report_id")
        if not report_id:
            return {"ran": False, "defects": [], "report_ref": None}
        return {
            "ran": True,
            "defects": raw.get("defects") or [],
            "report_ref": {"service_id": "S37", "id": report_id,
                           "revision": int(raw.get("report_generation") or 1)},
        }

    return _read


# ------------------------------------------------------------------ S10

def build_face_authorization(base_url: str | None, *,
                             tenant_header="x-tenant-id"):
    """`GET /characters/{id}/face-authorization` → `granted/lapsed/unknown`。

    三态而不是布尔：`unknown`（没查过 / 没有这条记录）与 `lapsed`（明确
    失效）处置不同。前者不阻断建 job（本服务不是授权权威），后者阻断
    （F4 / T32-12）。
    """
    if not base_url:
        return _unconfigured("FACE_AUTHORIZATION_UNAVAILABLE",
                             "S10 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, speaker_character_id: str) -> str:
        try:
            raw = _request(
                "GET",
                f"{root}/characters/{speaker_character_id}"
                f"/face-authorization",
                headers={tenant_header: tenant_id})
        except ServiceError as exc:
            if exc.status == 404:
                return "unknown"
            raise
        state = str(raw.get("state") or "").lower()
        if state in ("granted", "lapsed"):
            return state
        return "unknown"

    return _read


# ------------------------------------------------------------------ S25

def build_route_resolver(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """`POST /routes:resolve` → 归一化成内核读得懂的形状。

    归一化的三件事：

    - `capabilities` **只从显式布尔取**。`face_region_localized_edit` 缺失
      （S24 目前没有登记它，缺口 C7）读成 `None` 而不是 `False`：`False`
      会拒绝掉所有路由，`True` 会把没有的能力当成有。只有显式 `False` 才
      触发 `CAPABILITY_MISSING`。
    - `feasible`：任何一项显式为 False 就是 False。
    - `residency_allowed`：从 rejected 里的 RESIDENCY/REGION 码推断。
    """
    if not base_url:
        return _unconfigured("ROUTE_UNAVAILABLE", "S25 is not configured")

    root = base_url.rstrip("/")

    def _resolve(tenant_id: str, request: dict) -> dict:
        raw = _request("POST", f"{root}/routes:resolve", body=request,
                       headers={tenant_header: tenant_id})
        eligible = raw.get("eligible") or []
        first = eligible[0] if eligible else {}
        profile = first.get("execution_profile") or {}
        capabilities = {
            key: profile[key] for key in
            ("face_region_localized_edit", "editable_region",
             "temporal_consistency", "multi_speaker")
            if isinstance(profile.get(key), bool)}
        rejected = raw.get("rejected") or []
        rejected_codes = {str(item.get("code") or "").upper()
                          for item in rejected if isinstance(item, dict)}
        residency_allowed = None
        if any("RESIDENCY" in code or "REGION" in code
               for code in rejected_codes):
            residency_allowed = False
        flags = [first.get("feasible")] + [
            item.get("feasible") for item in rejected
            if isinstance(item, dict)]
        feasible = None
        if any(value is False for value in flags):
            feasible = False
        elif any(value is True for value in flags):
            feasible = True
        route = first.get("route") or {}
        return {
            "feasible": feasible,
            "degradation_options": raw.get("degradation_options") or [],
            "capabilities": capabilities,
            "residency_allowed": residency_allowed,
            "region": raw.get("region"),
            "model_family": profile.get("model_family")
            or route.get("model_family"),
            "route_decision_ref": raw.get("route_decision_id")
            or raw.get("decision_id") or route.get("route_id"),
            "policy_ref": raw.get("route_policy_ref")
            or request.get("route_policy_ref"),
            "selected": first or None,
        }

    return _resolve


# ------------------------------------------------------------------ S26

def build_submitter(base_url: str | None, *, tenant_header="x-tenant-id"):
    """`POST /generation-jobs`。**付费任务只从这里出去。**

    幂等键由 service.py 生成（`attempt_id` / `idempotency_key` 都带业务
    尝试号），所以同一次业务重试不会撞上 S26 的去重；而**同一次**业务
    尝试重放会被 S26 吞掉——这正是要的。

    返回必须带一个可对账的 id。没有 → 503：S26 说收下了却给不出 id，等同
    提交结果不明，绝不能当成功。
    """
    if not base_url:
        return _unconfigured("GENERATION_JOBS_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _submit(tenant_id: str, command: dict, route_decision_ref) -> dict:
        body = {"command_id": command.get("idempotency_key"),
                "generation_command": command,
                "route_decision_ref": route_decision_ref}
        raw = _request("POST", f"{root}/generation-jobs", body=body,
                       headers={tenant_header: tenant_id})
        if not (raw.get("provider_job_id") or raw.get("generation_job_id")):
            raise ServiceError(503, "GENERATION_JOB_ID_MISSING",
                               "S26 accepted the command but returned no "
                               "job id", retryable=True)
        return raw

    return _submit


def build_canceler(base_url: str | None, *, tenant_header="x-tenant-id"):
    if not base_url:
        return _unconfigured("GENERATION_CANCEL_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _cancel(tenant_id: str, provider_job_id: str, reason: str) -> dict:
        return _request(
            "POST", f"{root}/generation-jobs/{provider_job_id}:cancel",
            body={"reason_code": reason},
            headers={tenant_header: tenant_id})

    return _cancel


def build_generation_state(base_url: str | None, *,
                           tenant_header="x-tenant-id"):
    """`GET /generation-jobs/{id}`：对账用。**只读**。"""
    if not base_url:
        return _unconfigured("GENERATION_STATUS_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, provider_job_id: str) -> dict | None:
        try:
            return _request(
                "GET", f"{root}/generation-jobs/{provider_job_id}",
                headers={tenant_header: tenant_id})
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    return _read


# ------------------------------------------------------------------ 差异度量

def build_diff_measurer(base_url: str | None, *,
                        tenant_header="x-tenant-id"):
    """§4.6 的逐像素补集最大差。由媒体计算服务执行，S32 只下判断。

    **不能在本进程里算**：S32 不持有媒体字节（§2.1：它只持有引用）。而
    "算不出来"与"没越界"是两件事——所以这里是 503 + `retryable`，内核把
    它变成整笔回滚，job 留在 `INGESTING`。
    """
    if not base_url:
        return _unconfigured("DIFF_MEASUREMENT_UNAVAILABLE",
                             "the media diff service is not configured")

    root = base_url.rstrip("/")

    def _measure(tenant_id: str, request: dict) -> dict:
        raw = _request("POST", f"{root}/still-frames:diff",
                       body=request, headers={tenant_header: tenant_id})
        return {
            "max_diff_complement": raw.get("max_diff_complement"),
            "max_diff_target_region": raw.get("max_diff_target_region"),
            "changed_frame_ratio_complement":
                raw.get("changed_frame_ratio_complement"),
            "frames_compared": raw.get("frames_compared"),
        }

    return _measure


# ------------------------------------------------------------------ 组合

def build_eligibility(s28, s31, s37, s10):
    """把四份只读事实合成一份合格性快照。

    这是本服务唯一一个"跨服务扇出"的地方，所以它的失败语义必须写死：

    - **任何一个上游 404** → 原样抛 404。跨租户与不存在在调用方眼里是同一
      件事（E28），S32 不做区分。
    - **任何一个上游不可达** → 503。编不出一份合格性快照，也就不该建 job。
    - **S37 没有报告** 不是错误（`ran=False`），**S10 查不到记录**也不是
      错误（`unknown`）。这两条是"允许但打标"的分支，不能和 503 混。

    `audio.duration_ms` 缺失也在这里被判 503：没有时长就算不出 `end_ms`，
    而 `end_ms` 是报价与帧范围的输入（§4.5）。
    """
    def _facts(tenant_id: str, request: dict) -> dict:
        video_ref = request.get("video_ref") or {}
        audio_ref = request.get("audio_ref") or {}
        audio_take_id = request.get("audio_take_id")
        speaker = request.get("speaker_character_id")

        video = s28(tenant_id, video_ref)
        if not isinstance(video, dict):
            video = {}
        audio = s28(tenant_id, audio_ref)
        if not isinstance(audio, dict):
            audio = {}

        take = {"rights_state": "unknown"}
        if audio_take_id:
            take = s31(tenant_id, audio_take_id)
            if not isinstance(take, dict):
                take = {"rights_state": "unknown"}

        report = s37(tenant_id, audio_take_id, audio_ref)
        if not isinstance(report, dict):
            report = {"ran": False, "defects": [], "report_ref": None}

        face_state = s10(tenant_id, speaker) if speaker else "unknown"

        duration = audio.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or \
                duration <= 0:
            # 时长是窗口与帧范围的输入，缺了就没法报价。S31 的 take 可能
            # 记得更准（容器实测），所以再看一眼它。
            observed = take.get("observed_duration_ms")
            if isinstance(observed, int) and not isinstance(observed, bool) \
                    and observed > 0:
                duration = observed
        return {
            "video": {
                "content_hash": video.get("content_hash")
                or video_ref.get("content_hash"),
                "frame_count": video.get("frame_count"),
                "fps": video.get("fps"),
                "duration_ms": video.get("duration_ms"),
                "referenceable": video.get("state") in (None, "AVAILABLE",
                                                        "REFERENCEABLE"),
                "tombstoned": str(video.get("state") or "").upper()
                in ("TOMBSTONED", "DELETED", "PURGED"),
                "origin_service": video.get("origin_service")
                or video.get("ingesting_service"),
            },
            "audio": {
                "content_hash": audio.get("content_hash")
                or audio_ref.get("content_hash"),
                "duration_ms": duration,
            },
            "audio_take": {
                "rights_state": take.get("rights_state") or "unknown",
            },
            "s37": report,
            "face_authorization_state": face_state,
            "model_family": request.get("model_family"),
        }

    return _facts


def build_all(env=None) -> dict:
    """从环境变量组装全部回调。全部未配置时服务仍可起（每个回调各自 503）。"""
    env = env if env is not None else os.environ
    s10 = env.get("S10_BASE_URL")
    s25 = env.get("S25_BASE_URL")
    s26 = env.get("S26_BASE_URL")
    s28 = env.get("S28_BASE_URL")
    s31 = env.get("S31_BASE_URL")
    s37 = env.get("S37_BASE_URL")
    media = env.get("MEDIA_DIFF_BASE_URL")
    return {
        "eligibility": build_eligibility(
            build_artifact_facts(s28), build_rights_reader(s31),
            build_s37_reader(s37), build_face_authorization(s10)),
        "route_resolver": build_route_resolver(s25),
        "submitter": build_submitter(s26),
        "canceler": build_canceler(s26),
        "artifact_facts": build_artifact_facts(s28),
        "artifact_check": build_artifact_check(s28),
        "url_refresher": build_refresher(s28),
        "rights_reader": build_rights_reader(s31),
        "diff_measurer": build_diff_measurer(media),
        "generation_state": build_generation_state(s26),
    }
