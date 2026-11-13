"""S15 的两个上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

只有两个依赖，形状都是**读上游代码核对过的**：

- **S28** `services/artifact-ingest`：
  - `GET /artifacts/{id}/revisions/{rev}` 返回 `{artifact_id, revision,
    content_hash, size_bytes, mime_type, ...}`——**没有 `probe_ok`，也没有
    `source_kind`**（`artifact_revision.probe_ok` 列存在但读取路径不返回它；
    `artifact_import.source_kind` 只在 import 实体上）。这正是 S15-2 登记的
    缺口：E29 要求"样本来自隔离导入器且过了恶意媒体扫描"，而 S28 的读接口
    不提供这两个事实。
  - 所以 `build_sample_scanner` 的返回值有三种，**调用方必须区分**：
    `False` = 确定不可用（404 / 未过检），`None` = **查不出来**，
    `True`/dict = 已核实。缺字段按 `None` 处理 → 内核 503。
    这一条不能松：声纹样本是生物特征，把"问不到"当成"没问题"等于用一个
    未经扫描的克隆源生成新音色。
- **S01** `services/tenant-access`：`GET /authorization-context?...` 返回
  `{permissions[], denied[], ...}`，语义与 `project_catalog.deps` 一致。
  `permission` 不在 permission registry 里时 S01 永远答"不许"，所以这里
  照抄 S03 的做法：先查注册表，未注册直接 503，而不是报 403 让人以为是
  权限问题（S15-8）。

**未配置 base url 时返回必然失败的 callback。** 没有"忘了配就放行"的路径。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError

# 抄自 `contracts/identity/permission-registry.v2.json`（40 个键）。与音色相关的
# **确实只有一个**：`asset.voice.authorize`（risk=high，plans/S01-tenant-access.md:46
# 指派给 S11–S15：S01 只判"这个主体有没有这个权限"，"这个声音现在还能不能用"
# 是 S15 的事实）。
#
# 本服务全部写命令都问这一个键。注册表里没有更细的写键，自己发明一个只会让每条
# 写命令在 S01 侧 503——那是把一个粒度问题变成"服务不可用"。粒度是否要拆
# （内容写 vs 授权写）是 owner 的决定，登记在 CONTRACT_GAPS S15-8。
#
# 分开命名是为了让"要不要拆"这件事在代码里可见：owner 注册新键时只改
# service.py 里的两个常量。
REGISTERED_PERMISSIONS = ("asset.voice.authorize",)


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


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> dict:
    return _request("GET", url, headers=headers, timeout=timeout)


# ---------------------------------------------------------------- S28 样本

def build_sample_scanner(base_url: str | None, *, deadline_seconds: float = 2.0):
    """`(sample_ref, tenant_id) -> dict | None`。

    成功时返回 `{found, content_hash, probe_ok, source_kind}`；返回 `None`
    表示"上游答了，但答不出这两个事实"——内核据此 503，而不是放行。

    `sample_ref` 形如 `{artifact_id, revision, content_hash?}`。`content_hash`
    是本服务登记的期望值，用来和 S28 的读数对拍（F17）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S28_BASE_URL is not configured; S15 will not register a voice "
                "on a voiceprint sample whose scan state is unknown")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _scan(sample_ref, tenant_id: str):
        artifact_id = sample_ref.get("artifact_id") or sample_ref.get("id")
        revision = sample_ref.get("revision")
        if not artifact_id or revision is None:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "voice_sample_refs entries need artifact_id and revision")
        url = (f"{prefix}/artifacts/{urllib.parse.quote(str(artifact_id))}"
               f"/revisions/{int(revision)}")
        try:
            result = _get(url, headers={"X-Tenant-Id": tenant_id},
                          timeout=deadline_seconds)
        except ServiceError as exc:
            # 跨租户/不存在的样本在 S15 眼里都是"没有这个样本"。
            if exc.status == 404:
                return {"found": False, "content_hash": None,
                        "probe_ok": None, "source_kind": None}
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE", str(exc),
                               retryable=True) from exc

        if not result:
            return {"found": False, "content_hash": None,
                    "probe_ok": None, "source_kind": None}
        if "probe_ok" not in result or "source_kind" not in result:
            # S15-2：S28 这套读接口不发布这两个事实。**不猜**。
            return None
        return {
            "found": True,
            "content_hash": result.get("content_hash"),
            "probe_ok": bool(result.get("probe_ok")),
            "source_kind": result.get("source_kind"),
        }
    return _scan


# ---------------------------------------------------------------- S01 授权

def build_authorizer(base_url: str | None, *, deadline_seconds: float = 0.8):
    """`(tenant_id, principal_id, scope_kind, scope_id, permission) -> dict`。

    800ms 预算抄自 S03 的 §4.1 第 3 步（**不是** S15 方案里的数——S15 只要求
    资格检查 P99 < 50ms，对写命令这条上游没有给预算）。超时即 503，且事务还
    没开始。差别只在权限键：本服务只有一个已经注册的 voice 键。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AUTH_CONTEXT_UNAVAILABLE",
                "S01_BASE_URL is not configured; refusing to write without an "
                "authorization context")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _authorize(tenant_id: str, principal_id: str, scope_kind: str,
                   scope_id: str, permission: str) -> dict:
        if permission not in REGISTERED_PERMISSIONS:
            # S15-8：S01 对没注册的键永远答"不许"，报 403 会让人以为是权限
            # 配置问题。503 + 说清是"键没注册"才是可行动的事实。
            raise ServiceError(
                503, "PERMISSION_KEY_UNREGISTERED",
                f"permission {permission!r} is not in "
                "contracts/identity/permission-registry.v2.json; S15 has no way "
                "to authorize this action yet (S15-8)")
        query = urllib.parse.urlencode({
            "scope_kind": scope_kind, "scope_id": scope_id,
            "principal_id": principal_id, "purpose": permission,
        })
        try:
            result = _get(f"{prefix}/authorization-context?{query}",
                          headers={"X-Tenant-Id": tenant_id,
                                   "X-Principal-Id": principal_id},
                          timeout=deadline_seconds)
        except ConnectionError as exc:
            raise ServiceError(503, "AUTH_CONTEXT_UNAVAILABLE",
                               str(exc)) from exc
        granted = permission in (result.get("permissions") or [])
        denied = permission in (result.get("denied") or [])
        # 什么都不说就是不许：`allowed` 只在明确授予且没明确拒绝时为真。
        return {**result, "allowed": granted and not denied,
                "permission": permission}
    return _authorize


__all__ = [
    "REGISTERED_PERMISSIONS",
    "build_authorizer",
    "build_sample_scanner",
]
