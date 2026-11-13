"""S13 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库/互相 import）。

两个上游，形状都是**读上游代码核对过的**，不是从方案里抄的：

- **S28** `services/artifact-ingest`：
  `GET /artifacts/{artifact_id}/revisions/{revision}` 返回
  `{artifact_id, revision, content_hash, size_bytes, mime_type, ...}`。
  方案 §4.1 第 5 条要的正是「每个 artifact 存在且 hash 一致」，这两个数都在
  这个响应里。404 与「连不上」必须分开：前者是 `ARTIFACT_NOT_FOUND`，
  后者是 §4.4 F16 的 503 `DEPENDENCY_UNAVAILABLE` 且**不落候选**。
- **S01** `services/tenant-access`：
  `GET /authorization-context?scope_kind=&scope_id=&principal_id=&purpose=`
  返回 `permissions` / `denied` / `revocation_epoch`，**不返回角色**。
  但它没有 scene.* 权限键（注册表里 12 个键一个都不在这个域），所以
  `REGISTERED_PERMISSIONS` 是空集，内核主路径**不调用**它——发明一个写权限门
  只会让每条写命令在生产里 503（CONTRACT_GAPS 的 S13-5）。保留这个函数是
  为了 owner 把权限键注册进 S01 之后，接线只改一个环境变量。

**未配置一律必然失败**（S28）：§4.4 F16 要求 S28 超时就不落候选，没有
「忘了配就跳过 artifact 校验」的路径。S01 的未配置版同样 fail closed，
即使它今天不在主路径上——一个可选的注入点不该有一个放行的默认值。
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
        # 超时/不可达要和「上游说了不行」分开：前者是 503 可重试，后者是 4xx。
        # S13 的 F16 正是靠这个区分：连接失败绝不能变成「artifact 不存在」，
        # 否则一次 S28 抖动会把一个完全合法的候选判成 422。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S28 artifact

def build_artifact_source(base_url: str | None):
    """`(tenant_id, artifact_ref) -> {"content_hash": str}`，或抛错。

    两种错误由调用方翻进行为：

    - `ServiceError(404, "ARTIFACT_NOT_FOUND")` → 服务层 422 `ARTIFACT_NOT_FOUND`
    - `ConnectionError` → 服务层 503 `DEPENDENCY_UNAVAILABLE`，**不落候选**（F16）

    `artifact_ref` 的形状方案没写死。接受本仓常见的三种写法：
    `{"id":…, "revision":…, "content_hash":…}`、`{"artifact_id":…, …}`、
    以及字符串 id（此时 revision 取 `current_revision`——S28 的
    `GET /artifacts/{id}` 返回它）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S28_BASE_URL is not configured; S13 will not accept a revision "
                "whose artifact references it cannot verify (plan §4.1 step 5, "
                "F16). Configure S28 or the whole draft path stays closed.")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _head(tenant_id: str, artifact_ref: dict | str) -> dict:
        artifact_id, revision = _split_ref(artifact_ref)
        if not artifact_id:
            raise ServiceError(422, "ARTIFACT_NOT_FOUND",
                               "artifact_ref has no id")
        if revision is None:
            # 没有 revision 就去取 artifact 头，读它的 current_revision。
            # 不默认成 1：一个不存在的 revision 会变成 404，而那是
            # 「素材没了」的意思，跟「调用方没写 revision」是两回事。
            head = _request(
                "GET", f"{prefix}/artifacts/{urllib.parse.quote(str(artifact_id))}",
                headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
            revision = head.get("current_revision") or head.get("source_revision")
            if revision is None:
                raise ServiceError(422, "ARTIFACT_NOT_FOUND",
                                   f"artifact {artifact_id} has no revision")
        try:
            result = _request(
                "GET", f"{prefix}/artifacts/{urllib.parse.quote(str(artifact_id))}"
                       f"/revisions/{int(revision)}",
                headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(404, "ARTIFACT_NOT_FOUND",
                                   f"artifact {artifact_id}@{revision} does not "
                                   f"exist in S28") from exc
            raise
        return {"artifact_id": result.get("artifact_id") or artifact_id,
                "revision": result.get("revision", revision),
                "content_hash": result.get("content_hash")}

    return _head


def _split_ref(ref) -> tuple[str | None, int | None]:
    if isinstance(ref, str):
        return ref, None
    if isinstance(ref, dict):
        artifact_id = ref.get("artifact_id") or ref.get("id")
        revision = ref.get("revision")
        if isinstance(revision, bool):
            revision = None
        return artifact_id, revision
    return None, None


# ---------------------------------------------------------------- S01 授权

def build_authorizer(base_url: str | None):
    """`(tenant_id, principal_id, permission) -> {"allowed": bool}`。

    **今天不在内核主路径上**（`REGISTERED_PERMISSIONS` 是空集）。它存在的
    意义是：owner 把 `scene.*` 键注册进 S01 之后，接线只改一个环境变量。

    `allowed` 只在 S01 明确把该权限列进 `permissions` 且没列进 `denied`
    时为真——什么都不说就是不许。`denied` 必须一起看：S01 的语义是 deny
    压过 allow，只取 `permissions` 会让一条显式 deny 消失。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AUTHORIZER_UNAVAILABLE",
                "S01_BASE_URL is not configured; S13 will not guess whether a "
                "caller may act on a scene (plan §5.4 has no auth code, so this "
                "gate is off by default — see CONTRACT_GAPS S13-5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _authorize(tenant_id: str, principal_id: str, permission: str) -> dict:
        query = urllib.parse.urlencode({
            "scope_kind": "tenant",
            "scope_id": tenant_id,
            "principal_id": str(principal_id),
            "purpose": permission,
        })
        result = _request(
            "GET", f"{prefix}/authorization-context?{query}",
            headers={"X-Tenant-Id": tenant_id,
                     "X-Principal-Id": str(principal_id)},
            timeout=0.8)
        permissions = set(result.get("permissions") or [])
        denied = set(result.get("denied") or [])
        return {"allowed": permission in (permissions - denied),
                "permissions": sorted(permissions),
                "denied": sorted(denied),
                "revocation_epoch": result.get("revocation_epoch")}
    return _authorize
