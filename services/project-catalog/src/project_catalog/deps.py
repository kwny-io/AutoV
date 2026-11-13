"""S03 的上游，全部是注入式 callback，不是 import。

AGENTS.md 禁止互相 import 业务 ORM / 跨库查询，只能走服务边界。三个上游的真实
契约和方案假设的长度不一样，适配写在这里，**不让内核看到差异**：

- **S01** `GET /authorization-context?scope_kind=&scope_id=&principal_id=&purpose=`
  返回 `permissions` / `denied` / `revocation_epoch`，**不返回角色**。本服务
  要的是"这个人此刻能不能对这个项目做这件事"，正好是它答的题。缺的是权限键
  本身：注册表 40 个键里只有 `project.get`（缺口 G5），所以写入类动作的键在
  `REGISTERED_PERMISSIONS` 之外，默认必然 503 —— **不用读权限顶写权限**。一个
  能读项目的凭证不应因此能建或归档项目。
- **S21** 没有 run 计数查询（缺口 G11）。归档时方案要 `ACTIVE_RUNS_EXIST` 告警，
  计数只能靠注入的 `run_probe`。没配就**不报这条告警**，不是报 count=0 ——
  伪造的 0 会被读成"已核实：没有在跑的东西"。
- **S16** 管叙事时间，`story_window_ref.clock_id` 的合法性只有它能判。§4.6 要求
  未知 clock 只告警不拦。没配就不告警（同上理由）。S16 的查询形状本方案没有
  写死，所以这里只收一个 callback，不猜 endpoint。

未配置 base url 时返回必然失败的 callback。没有"忘了配就放行"的路径。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .schema import READ_PERMISSION, REGISTERED_PERMISSIONS  # noqa: F401
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
        # 超时/不可达要和"上游说了不行"分开：前者是 503 可重试，后者是 4xx。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> dict:
    return _request("GET", url, headers=headers, timeout=timeout)


def build_authorizer(base_url: str | None, *, deadline_seconds: float = 0.8):
    """S01 的授权上下文。§4.1 第 3 步给 800ms 预算，超时即 503 不落库。

    返回 `(tenant_id, principal_id, scope_kind, scope_id, permission) -> dict`，
    dict 至少含 `allowed: bool`。`allowed` 只在 S01 明确把该权限列进
    `permissions` 且没列进 `denied` 时为真 —— 什么都不说就是不许。
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
        # 键没注册就不必问 S01：它永远不会把未注册的键列进 permissions，
        # 而"因为没有这个键所以拒"和"这个人没这个权限"必须分开报。
        if permission not in REGISTERED_PERMISSIONS:
            raise ServiceError(
                503, "PERMISSION_KEY_UNREGISTERED",
                f"permission {permission!r} is not in "
                "contracts/identity/permission-registry.v2.json; S03 has no way "
                "to authorize this action yet (gap G5)")
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
            # 上游不可达 → 503，且**事务还没开始**（调用方在 BEGIN 之前问）。
            raise ServiceError(503, "AUTH_CONTEXT_UNAVAILABLE",
                               str(exc)) from exc
        granted = permission in (result.get("permissions") or [])
        denied = permission in (result.get("denied") or [])
        return {**result, "allowed": granted and not denied,
                "permission": permission}
    return _authorize


def build_run_probe(base_url: str | None):
    """S21 的活动 run 计数。没配就返回 None（缺口 G11），不返回 0。

    `None` 的语义是"没查"，`0` 的语义是"查了，没有"。归档响应必须能区分这两者，
    否则一条 ACTIVE_RUNS_EXIST count=0 的告警会读成已核实安全。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _count(tenant_id: str, project_id: str) -> int | None:
        try:
            result = _get(f"{prefix}/projects/{project_id}/runs"
                          "?state=active&limit=0",
                          headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except (ServiceError, ConnectionError):
            return None
        total = result.get("total")
        return int(total) if isinstance(total, int) else None
    return _count


def build_clock_validator(base_url: str | None):
    """S16 的叙事时钟存在性。没配就返回 None（查不了就不告警）。

    返回 `clock_id -> bool`。`True` 表示 S16 认得这个时钟。方案 §4.6 规定未知
    clock 只进 `warnings[]`，不阻断写入 —— 叙事时间是 S16 的事实，S03 无权因
    自己查不到就拒收。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _knows(tenant_id: str, clock_id: str) -> bool | None:
        try:
            result = _get(f"{prefix}/story-clocks/{clock_id}",
                          headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except (ServiceError, ConnectionError):
            return None
        return bool(result.get("clock_id"))
    return _knows
