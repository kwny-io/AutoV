"""S12 的上游。全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

按方案 `plans/S12-costume-assets.md` 的开头，S12 的 `depends_on` 只有 **S28**
和 **S01**。实际用起来是这样：

- **S28 artifact-ingest（必需）**：`GET /artifacts/{artifact_id}` 是 §4.1 第 1 条
  的唯一实现——artifact 存在、且 `content_hash` 与候选声明一致。读的是已实现
  的服务（`services/artifact-ingest`），返回体里有 `content_hash`（当前 revision
  的）。**没有它，一个 `artifact_id` 只是一个字符串**：可能是别的租户的、
  可能根本没落盘（F1、F11）。
- **S01 tenant-access（不接）**：S12 没有敏感字段，也没有 §5.4 以外的读权限
  语义。`TENANT_SCOPE_DENIED` 只用于"请求体 tenant_id 与签名上下文不一致"。
  接一个没被需要的授权跳只会让每条读路径多一个 503 来源。
- **QC（可选，C12-2 / CCA-1）**：方案说身份不变性结论来自 S33/S34，但 S33 是
  音效、S34 是配乐；S35/S36 的 QCReport v2 要求 `shot_ref`、`artifact_kind`
  也没有资产图片这一值。所以这一跳**默认不配置**：结论随候选命令内联传入
  （`evaluation`），读不到就按"没有结论"处理，**绝不用 `true` 兜底**。配置了
  也只读不判。
- **S02 权利（不接）**：`rights_ref` 是指针，S12 只存不判（§2.8、§9 第 10 条）。
- **S14 道具（不接）**：配饰引用只记 `unverified_refs`（§4.1 第 5 条、F14）。
  去问 S14 "这个 revision 在不在"会把"配饰先于道具出现"的正常流程卡死，
  也会让 S12 在 S14 宕机时写不了服装。

**未配置一律必然失败**（S28）：这条 503 分支就是 §5.4 的
`DEPENDENCY_UNAVAILABLE`（更准确是 `ARTIFACT_SOURCE_UNAVAILABLE`）。
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
        # 404 在这里是最要紧的一种：**S28 说没有**，不是"上游挂了"。
        # 原样上抛成 404，由 service 翻译成 422 ARTIFACT_NOT_FOUND。
        raise ServiceError(exc.code, payload.get("code") or "UPSTREAM_ERROR",
                           payload.get("message") or str(exc),
                           retryable=bool(payload.get("retryable")),
                           details=payload.get("details") or {})
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S28 资产

def build_artifact_source(base_url: str | None):
    """`(tenant_id, artifact_id) -> artifact 摘要`，`None` 表示"这个租户看不到"。

    §4.1 第 1 条只要求"存在且 hash 一致"。**不做本地缓存**：artifact 可能被
    tombstone（S28 的 `:tombstone`），缓存过的校验等于拿旧事实给新候选背书。

    404 与"上游不可达"必须分开：
    - S28 明确 404 → 返回 `None`，service 报 422 `ARTIFACT_NOT_FOUND`
      （这是调用方的错，不该重试）；
    - 超时/连接失败 → `ConnectionError`，service 报 503 且**不落候选**
      （F11：拿不到结论就存下来，等于把伪造的 id 写进不可变 revision）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28_BASE_URL is not configured; S12 will not accept a "
                "candidate whose variant artifacts it cannot verify, because "
                "an unverified artifact_id may belong to another tenant "
                "(plan §4.1 step 1, §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _get(tenant_id: str, artifact_id: str) -> dict | None:
        try:
            return _request(
                "GET", f"{prefix}/artifacts/"
                       f"{urllib.parse.quote(str(artifact_id))}",
                headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                # 不存在**和**"存在但你无权"在 S28 是同形，这里照收 ——
                # 区分它们就是泄漏别的租户有什么。
                return None
            raise

    return _get


# ---------------------------------------------------------------- QC（可选）

def build_quality_gate_source(base_url: str | None):
    """`(tenant_id, quality_gate_ref) -> 结论文档`。

    **默认不配置**，而且配置了也只是"读结论"。仓库里没有产出
    `identity_invariance_check` / 图文匹配分数的服务（C12-2 / CCA-1 / CCA-5）：
    - S33 = 音效，S34 = 配乐（方案 §4.2 把它们写成 QC，是错的）
    - S35 = technical-qc、S36 = semantic-qc，但 QCReport v2 要求 `shot_ref`，
      `artifact_kind` 也没有资产图片这一值，接不上服装 revision

    `quality_gate_ref` 的形状契约里没定义，所以在能读到之前，结论只能随
    命令内联传入（`body.evaluation`）。这个函数存在的意义是：**一旦** owner
    决定了 QC 服务，接线只改一个环境变量，不用改内核。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "QUALITY_GATE_UNAVAILABLE",
                "QUALITY_GATE_BASE_URL is not configured; pass the conclusion "
                "inline as body.evaluation instead (C12-2 / CCA-1)")

        return _unconfigured

    prefix = base_url.rstrip("/")

    def _fetch(tenant_id: str, ref) -> dict:
        gate_id = ref.get("id") if isinstance(ref, dict) else ref
        if not gate_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "quality_gate_ref needs an id")
        revision = (ref.get("revision") if isinstance(ref, dict) else None)
        path = f"{prefix}/quality-gates/{urllib.parse.quote(str(gate_id))}"
        if revision is not None:
            path += f"/revisions/{int(revision)}"
        return _request("GET", path, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)

    return _fetch
