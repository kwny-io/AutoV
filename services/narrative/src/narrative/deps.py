"""S04 的上游，全部注入式 callback，不 import 任何服务的业务模块（AGENTS.md）。

三条上游，形状都是**读上游代码核对过的**：

- **S03** `GET /episodes/{episode_id}` 返回
  `{episode_id, series_id, project_id, ...}` —— 用来校验 `episode_id` 确实
  属于调用方给的 `series_id`（G10）。S03 没有 `GET /series/{id}`，所以
  `series_id` 本身只能当不透明引用存，不校验存在性。
- **S19** `POST /agent-tasks` 返回带 `task_id` 的任务体；`GET /agent-tasks/{id}`
  用来回查 `produced_by.task_id`（§4.6 第 16 条）。S04 只读 `task_id`，不看
  任务状态 —— 任务是否成功与"这条 revision 是谁产出的"是两个事实。
- **正文对象存储**（G1 未定介质）：`put/get/delete/list_prefix` 四个动作。
  它不是 HTTP 服务，是 S04 自己的私有存储适配器；未配置即 503，**不回落到
  把正文塞进 S03/S28 的桶**。

未配置一律 503，没有「忘了配就给默认值」的路径（§9 第 14 条）。
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


# ---------------------------------------------------------------- S03 目录

def build_catalog_source(base_url: str | None):
    """`(tenant_id, episode_id) -> episode 上下文`。

    S03 在跨租户时返回 404 `REF_NOT_FOUND`（同码），这里原样上抛；内核把它
    转成 S04 自己的 `EPISODE_NOT_FOUND`，不泄露"这个 episode 在别的租户存在"。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CATALOG_UNAVAILABLE",
                "S03_BASE_URL is not configured; S04 will not link an episode "
                "it cannot verify belongs to the given series (S04-G10)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _episode(tenant_id: str, episode_id: str) -> dict:
        result = _request(
            "GET", f"{prefix}/episodes/{urllib.parse.quote(str(episode_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(result, dict) or "series_id" not in result:
            raise ServiceError(
                503, "CATALOG_UNAVAILABLE",
                "S03 returned an episode body without series_id; an "
                "unverifiable link is not a verified one")
        return result
    return _episode


# ---------------------------------------------------------------- S19 任务

def build_task_source(base_url: str | None):
    """返回两个 callback：`get(tenant_id, task_id)` 与
    `submit(tenant_id, command_id, task_request)`。

    `submit` 的幂等键用**调用方的 command_id**：同一 `:extract-claims` 命令
    重试时不能变成第二个付费任务。S04 不碰预算与 provider（S19/S26 的事）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19_BASE_URL is not configured; S04 will not trust an "
                "unverifiable produced_by.task_id and will not record an "
                "extraction it cannot start")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _get(tenant_id: str, task_id: str) -> dict:
        result = _request(
            "GET", f"{prefix}/agent-tasks/{urllib.parse.quote(str(task_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(result, dict):
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19 returned no task body")
        return result

    def _submit(tenant_id: str, command_id: str,
                task_request: dict) -> dict:
        result = _request(
            "POST", f"{prefix}/agent-tasks", body=task_request,
            headers={"X-Tenant-Id": tenant_id,
                     "Idempotency-Key": command_id}, timeout=30.0)
        if not isinstance(result, dict) or not result.get("task_id"):
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19 accepted the extraction but returned no task_id; S04 "
                "cannot record a run it cannot name")
        return result

    # 内核对 `get` 与 `submit` 的用法不同；把两个都挂成属性，同时保留可调用。
    def _dispatch(tenant_id: str, task_id: str) -> dict:
        return _get(tenant_id, task_id)

    _dispatch.submit = _submit
    _dispatch.get = _get
    return _dispatch


# ---------------------------------------------------------------- 正文存储

class InMemoryBodyStore:
    """测试与本地开发用的正文存储。**不是生产介质**（G1 仍待 owner 决定）。

    放在 deps.py 而不是测试里：`collect_orphan_chunks` 需要 `list_prefix`，
    生产适配器也要实现同一组四个动作，测试用这个才能覆盖到真实调用路径。
    """

    def __init__(self):
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, payload: bytes) -> None:
        self._objects[key] = bytes(payload)

    def get(self, key: str):
        return self._objects.get(key)

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    def list_prefix(self, prefix: str):
        return sorted(key for key in self._objects if key.startswith(prefix))


def build_body_store(backend: str | None = None, **options):
    """正文存储适配器。`backend` 为空即 503（G1：介质未定，不许默认）。

    已知取值只有 `"memory"`（本地/测试）。生产介质没定之前，任何"生产"取值
    都是错的，所以这里直接拒绝，而不是给一个看起来能跑的默认。
    """
    if not backend:
        raise ServiceError(
            503, "BODY_STORE_UNAVAILABLE",
            "S04_BODY_STORE is not configured; script text has no home yet "
            "(plan §11 open question 1 / S04-G1)")
    if backend == "memory":
        return InMemoryBodyStore()
    raise ServiceError(
        503, "BODY_STORE_UNAVAILABLE",
        f"body store backend {backend!r} is not implemented; only 'memory' "
        f"exists until G1 is decided")
