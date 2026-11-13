"""S10 的上游：五个资产服务 + S08，全部是注入式 callback（AGENTS.md 禁止跨库）。

**这一层只做形状归一，不解释语义。** 它把五个服务各不相同的 revision 响应压成
同一枚指针 `{service_id, id, revision, content_hash}`，把上游的几种"这类东西
不存在"翻译成 S10 的 `OWNER_REF_INVALID`，把"上游不可达"留给
`service._upstream` 统一转 503。它**不从上游读任何内容字段**——不读
`identity_features`、`artifacts`、`topology`、`appearance`、`voice_sample_refs`。
那些是 owner 的内容，S10 抄一份就变成内容副本（方案 §2.9）。

逐条核对过上游的实际路由，不是从方案抄的：

- S11 `GET /characters/{id}/revisions/{rev}` → `character_ref{...}`
- S12 `GET /costumes/{id}/revisions/{rev}?variant=` → `costume_ref{...}`
- S13 `GET /scene-assets/{id}/revisions/{rev}` → `scene_ref{...}`
- S14 `GET /props/{id}/revisions/{rev}?variant=` → `prop_ref{...}`
- S15 `GET /voices/{id}/revisions/{rev}` → **没有 `voice_ref` 包层**，
  `voice_id` / `revision` / `content_hash` 在顶层。

资产级（用于读 `status`，因为多数服务在资产 tombstoned 时对 revision 读直接
422）：S11/S12/S14 返回 `{<type>: {...status...}, canonical, ...}`，
S13 是平铺的，S15 是平铺的。

S48（adaptation）没有可读的 revision 形状，见 `CONTRACT_GAPS` 的 `S10-18`：
它不在 `_PATHS` 里，配置了 base_url 也会以 503 结束，而不是凭一个猜的路径
去拿一个可能不是 content_hash 的字段。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError

# owner_service -> 资源路径段。只放有 revision 读契约的五个。
_PATHS = {
    "S11": "characters",
    "S12": "costumes",
    "S13": "scene-assets",
    "S14": "props",
    "S15": "voices",
}

# 有 `?variant=` 查询参数的两个。
_VARIANT_AWARE = ("S12", "S14")

# revision 响应里包裹指针的键名。S15 没有，单独处理。
_REF_KEY = {
    "S11": "character_ref",
    "S12": "costume_ref",
    "S13": "scene_ref",
    "S14": "prop_ref",
}

# 资产级响应里包裹资产对象的键名。S13/S15 平铺。
_ASSET_KEY = {"S11": "character", "S12": "costume", "S14": "prop"}

# 各服务的资产 id 在平铺响应里的键名。
_ASSET_ID_KEY = {"S13": "scene_asset_id", "S15": "voice_id"}


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


def _base_map(base_urls) -> dict[str, str]:
    """`{service_id: base_url}`，值为空的条目直接丢掉。

    丢掉而不是留一个空串：留空串会让 `_request` 拼出 `/characters/...` 这种
    相对 URL，urllib 会以"unknown url type"报错，看起来像配置错误而不是
    "这个上游没配"。丢掉之后 `_unconfigured` 分支给出的 503 才是准的。
    """
    if not base_urls:
        return {}
    return {key: str(value).rstrip("/") for key, value in base_urls.items()
            if value}


def _unconfigured(service_id: str, what: str):
    def _raise(*_args, **_kwargs):
        raise ServiceError(
            503, "DEPENDENCY_UNAVAILABLE",
            f"{service_id} is not configured; S10 will not seal a binding "
            f"whose {what} it cannot verify (plan §4.3 / §4.6 F1)")
    return _raise


# ------------------------------------------------------------ revision 指针

def build_revision_source(base_urls):
    """`(tenant_id, service_id, asset_id, revision, variant) -> 指针 dict`。

    指针是 `{service_id, id, revision, content_hash, status?}`。**只回这四个
    规范字段加一个可选的 status**，上游的其他内容字段被丢弃——调用方拿不到
    它们，也就不可能把它们写进 S10 的表。

    三种失败分开，因为调用方要给出不同的 reason（F2 / F3 / F4）：

    - 上游 404 → 422 `OWNER_REF_INVALID`，`details.reason = REVISION_NOT_FOUND`
    - 上游 422 `*_TOMBSTONED` → 422 `OWNER_REF_INVALID`，`reason = TOMBSTONED`
    - 上游返回体里没有 `content_hash` → 422 `OWNER_REF_INVALID`，
      `reason = REVISION_MALFORMED`（拿不到 hash 就没法证明"我指的是哪个版本"，
      这不是 hash 不一致）
    """
    bases = _base_map(base_urls)

    def _source(tenant_id: str, service_id: str, asset_id: str, revision: int,
                variant: str | None = None) -> dict:
        if service_id not in _PATHS or service_id not in bases:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{service_id} has no readable revision contract configured; "
                f"S10 refuses to invent one (CONTRACT_GAPS S10-18)")
        path = _PATHS[service_id]
        query = ""
        if variant and service_id in _VARIANT_AWARE:
            query = "?" + urllib.parse.urlencode({"variant": variant})
        url = (f"{bases[service_id]}/{path}/"
               f"{urllib.parse.quote(str(asset_id))}/revisions/{int(revision)}"
               f"{query}")
        try:
            result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                              timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "OWNER_REF_INVALID",
                    f"{service_id} has no {asset_id} revision {revision}",
                    details={"service_id": service_id, "id": asset_id,
                             "revision": int(revision),
                             "reason": "REVISION_NOT_FOUND"}) from exc
            if exc.status == 422 and str(exc.code).endswith("TOMBSTONED"):
                raise ServiceError(
                    422, "OWNER_REF_INVALID",
                    f"{service_id} {asset_id} is tombstoned",
                    details={"service_id": service_id, "id": asset_id,
                             "revision": int(revision),
                             "reason": "TOMBSTONED"}) from exc
            raise
        if not isinstance(result, dict):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{service_id} returned a non-object revision body")
        ref = result.get(_REF_KEY.get(service_id, "")) if service_id in _REF_KEY \
            else result
        if not isinstance(ref, dict):
            raise ServiceError(
                422, "OWNER_REF_INVALID",
                f"{service_id} revision body has no pointer object",
                details={"service_id": service_id, "id": asset_id,
                         "revision": int(revision),
                         "reason": "REVISION_MALFORMED"})
        digest = ref.get("content_hash")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ServiceError(
                422, "OWNER_REF_INVALID",
                f"{service_id} revision body has no usable content_hash",
                details={"service_id": service_id, "id": asset_id,
                         "revision": int(revision),
                         "reason": "REVISION_MALFORMED"})
        pointer = {
            "service_id": service_id,
            "id": str(ref.get("id") or asset_id),
            "revision": int(ref.get("revision") or revision),
            "content_hash": digest,
        }
        # 状态只在资产级读得到；revision 读成功本身已经证明"存在且不是
        # tombstoned"（上游对 tombstoned 的 revision 读会 422）。这里保留
        # 上游若显式给出 status 的原样值，不做推断。
        status = result.get("status") or ref.get("status")
        if isinstance(status, str) and status:
            pointer["status"] = status
        return pointer

    if not bases:
        return _unconfigured("S11-S15", "owner revisions")
    return _source


# ------------------------------------------------------------ 资产级状态

def build_asset_status_source(base_urls):
    """`(tenant_id, service_id, asset_id) -> {"status": str, ...}`。

    S10 存了自己的 `status`（`asset_identity.status`），但那是**生命周期镜像**，
    不是权威。封存前要确认 owner 不处于撤销/墓碑状态，所以读一次资产级。

    404 → 422 `OWNER_REF_INVALID`，`reason = ASSET_NOT_FOUND`。
    """
    bases = _base_map(base_urls)

    def _status(tenant_id: str, service_id: str, asset_id: str) -> dict:
        if service_id not in _PATHS or service_id not in bases:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{service_id} has no readable asset contract configured")
        url = (f"{bases[service_id]}/{_PATHS[service_id]}/"
               f"{urllib.parse.quote(str(asset_id))}")
        try:
            result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                              timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    422, "OWNER_REF_INVALID",
                    f"{service_id} has no asset {asset_id}",
                    details={"service_id": service_id, "id": asset_id,
                             "reason": "ASSET_NOT_FOUND"}) from exc
            raise
        if not isinstance(result, dict):
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               f"{service_id} returned a non-object asset body")
        body = result.get(_ASSET_KEY.get(service_id, ""), result) \
            if service_id in _ASSET_KEY else result
        if not isinstance(body, dict):
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               f"{service_id} asset body has no asset object")
        status = body.get("status")
        if not isinstance(status, str) or not status:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"{service_id} asset body has no status; S10 will not treat a "
                f"missing status as active")
        out = {"status": status,
               "asset_id": str(body.get(_ASSET_ID_KEY.get(service_id, ""),
                                        asset_id))}
        for key in ("current_revision", "canonical_revision", "merged_into"):
            if key in body:
                out[key] = body[key]
        return out

    if not bases:
        return _unconfigured("S11-S15", "owner asset status")
    return _status


# ------------------------------------------------------------ S08 manifest

def build_manifest_source(base_url: str | None):
    """`(tenant_id, manifest_id, revision) -> {"manifest": {...}}`。

    只有一条用途：`snapshot.manifest.committed` 的事件体里**没有**
    `binding_set_ref`（`CONTRACT_GAPS` 的 `S10-11`），usage 投影需要回源。
    未配置时 usage 投影只记缺口、不阻塞封存与 resolve（C10-7：S08 不是启动
    依赖）。
    """
    if not base_url:
        def _unconfigured_manifest(*_args, **_kwargs):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S08 is not configured; binding usage cannot be projected from "
                "an event that carries no binding_set_ref "
                "(CONTRACT_GAPS S10-11)")
        return _unconfigured_manifest

    prefix = base_url.rstrip("/")

    def _manifest(tenant_id: str, manifest_id: str, revision: int) -> dict:
        return _request(
            "GET", f"{prefix}/manifests/{urllib.parse.quote(str(manifest_id))}"
                   f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _manifest
