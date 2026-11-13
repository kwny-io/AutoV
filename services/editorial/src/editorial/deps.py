"""S41 的上游适配。全是 HTTP 闭包，没有任何跨服务数据库访问。

三个原则：

1. **未配置就是 503。** 不返回空列表、不返回 `{}`、不当作「没有素材」。
   `None` 在提交路径上等于「这个请求不成立」，会把一个可重试的运维故障
   变成 422，直接教坏调用方的重试逻辑（方案 §3.3 末段）。
2. **超时是 500ms 不是 15s。** §4.5 的失败矩阵要求 S28 读超时立刻 503
   且不落库；一个 15 秒的阻塞读会把时间线的写锁按住 15 秒。
3. **404 原样往上传。** 跨租户时 S28 也是 404，服务层统一转 422
   `SOURCE_ARTIFACT_MISSING`，不泄漏「这个 id 存在但不属于你」（E28-S41-02）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .schema import ARTIFACT_DEADLINE_SECONDS
from .service import ServiceError, bare_hash


def _request(method: str, url: str, *, body=None, headers=None,
             timeout: float = ARTIFACT_DEADLINE_SECONDS) -> dict:
    """最小的 JSON HTTP 调用。

    `HTTPError` 的 body 里有上游的 `code` / `retryable`，丢掉它就只能给调用
    方一个笼统的 502，等于让上游的错误分类信息在边界上蒸发。
    """
    payload = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, method=method,
                                     headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - 读不到 body 不该盖住状态码
            raw = ""
        detail = {}
        try:
            detail = json.loads(raw) if raw else {}
        except ValueError:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        raise ServiceError(
            exc.code,
            detail.get("code") or "UPSTREAM_ERROR",
            detail.get("message") or f"upstream returned {exc.code}",
            retryable=bool(detail.get("retryable")),
            details=detail.get("details") or {}) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 上层按 ConnectionError 映射成 503。这里不吞掉原因，运维要看得出
        # 是 DNS、连接还是超时。
        raise ConnectionError(f"{method} {url} failed: {exc}") from exc
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ServiceError(
            502, "UPSTREAM_INVALID",
            f"{method} {url} returned a non-JSON body") from exc
    if not isinstance(parsed, dict):
        raise ServiceError(502, "UPSTREAM_INVALID",
                           f"{method} {url} returned a non-object body")
    return parsed


def _unconfigured(code: str):
    def _raise(*_args, **_kwargs):
        raise ServiceError(503, code, "dependency is not configured",
                           retryable=True)
    return _raise


def build_artifact_source(base_url: str | None):
    """S28 `GET /artifacts/{id}[/revisions/{revision}]`。

    revision 是**钉住**的：方案 §2.1 与 §5 都要求 `src_artifact_ref` 带
    revision。没有 revision 的读会拿到「最新」，而那可能在两分钟前刚被替换。
    """
    if not base_url:
        return _unconfigured("DEPENDENCY_UNAVAILABLE")
    prefix = base_url.rstrip("/")

    def _fetch(tenant_id: str, artifact_id: str, revision: int) -> dict:
        if not artifact_id:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "artifact id is required")
        quoted = urllib.parse.quote(str(artifact_id), safe="")
        if revision is None:
            url = f"{prefix}/artifacts/{quoted}"
        else:
            url = f"{prefix}/artifacts/{quoted}/revisions/{int(revision)}"
        artifact = _request("GET", url,
                            headers={"X-Tenant-Id": tenant_id})
        # 兼容两种上游形状：有的把 artifact 包在 `artifact` 里，有的平铺。
        inner = artifact.get("artifact")
        if isinstance(inner, dict):
            artifact = inner
        digest = bare_hash(artifact.get("content_hash"))
        if digest is not None:
            artifact = {**artifact, "content_hash": digest}
        return artifact

    return _fetch


def build_authorization_source(base_url: str | None):
    """S01 `GET /authorization-context`（可选）。

    权限注册表里没有 editorial/timeline 的动作键（CONTRACT_GAPS
    E41-AUTHZ），所以本服务**不自己解释权限**。传进来的 `scope_kind` 由调用
    方决定；未配置时返回 None，让服务层如实说明「没有授权源」，而不是
    默认放行。
    """
    if not base_url:
        return None
    prefix = base_url.rstrip("/")

    def _fetch(tenant_id: str, scope_kind: str, scope_id: str,
               principal_id: str, purpose: str) -> dict | None:
        query = urllib.parse.urlencode({"scope_kind": scope_kind,
                                        "scope_id": scope_id,
                                        "principal_id": principal_id,
                                        "purpose": purpose})
        return _request("GET", f"{prefix}/authorization-context?{query}",
                        headers={"X-Tenant-Id": tenant_id},
                        timeout=0.8)

    return _fetch
