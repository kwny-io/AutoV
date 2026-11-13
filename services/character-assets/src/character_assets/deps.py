"""S11 的上游。全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

S11 的 `depends_on` 只有 **S28** 与 **S01**，但方案对它们的使用完全不同：

- **S28 artifact-ingest（必需）**：`GET /artifacts/{artifact_id}` 是 §4.2 第 1 步
  的唯一实现——artifact 存在、且 `content_hash` 与候选声明一致。**没有它，
  一个 `artifact_id` 只是一个字符串**：可能是别的租户的，可能根本没落盘，
  而它会变成全剧所有镜头的身份参考（F1/F2/E29-B2）。
- **S01（不接，C11-18）**：`service.yaml` 列了它，但方案全文没有一处调用。
  §2.2 行 150 把质量门来源写成「S33/S34/S01」，而 S01 是租户/权限服务，
  不产视觉 QC。凭空接一个「看起来能判点什么」的上游，会让 S11 变成第二个
  权威——所以这里**没有** `build_authorizer`。
- **QC（可选，默认不配置）**：仓库里没有任何服务产出方案 §4.2 说的
  「身份一致性 / 多角度一致性」结论（C11-11）。S35 是技术指标，S36 是叙事
  一致性，都不是。所以默认不配置：结论随命令内联传入，读不到就记空对象，
  **绝不用 `pass=true` 兜底**（E24-B1 依赖这一步不做假）。
- **S02 权利（不接）**：`rights_ref` 是指针，S11 只存不判（§9 反模式 5）。
  `rights_recheck_required` 恒为 true 就是这一条的读取侧表达。

**未配置一律必然失败**（S28）：这条 503 分支就是 §5.4 的
`DEPENDENCY_UNAVAILABLE`。唯一例外是 QC——契约从没定义过它。
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

    §4.2 第 1 步只要求"存在且 hash 一致"。**不做本地缓存**：artifact 可能被
    S28 的 `:tombstone` 下线，缓存的校验结果等于拿旧事实给新候选背书——
    而 S11 的候选一旦被接受就是**不可变**的身份基准。

    404 与"上游不可达"必须分开：
    - S28 明确 404 → 返回 `None`，service 报 422 `ARTIFACT_NOT_FOUND`
      （调用方的错，不该重试）；
    - 超时/连接失败 → `ConnectionError`，service 报 503 且**不落候选**
      （F10：拿不到结论就存下来，等于把伪造的 id 写进 revision）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28_BASE_URL is not configured; S11 will not draft a "
                "candidate whose artifacts it cannot verify, because an "
                "unverified artifact_id may belong to another tenant "
                "(plan §4.2 step 1, §9)")
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
                # 不存在**和**"存在但你无权"在 S28 是同形，这里照收——
                # 区分它们就是泄漏别的租户有什么。
                return None
            raise

    return _get


# ---------------------------------------------------------------- QC（可选）

def build_quality_gate_source(base_url: str | None):
    """`(tenant_id, quality_gate_ref) -> 结论文档`。

    **默认不配置**，而且配置了也只是"读结论"。仓库里没有产出
    `identity_consistency` / `view_consistency` 的服务（C11-11）：
    - S35 = technical-qc（画面/音频技术指标，不是"这是不是同一个人"）
    - S36 = semantic-qc（叙事一致性）
    - S33 = 音效、S34 = 配乐（方案 §2.2 行 150 写成「S33/S34/S01」是笔误）

    `quality_gate_ref` 的形状契约里没定义，所以在能读到之前，结论只能随
    命令内联传入（`body.quality_gate`）。这个函数存在的意义是：**一旦**
    owner 决定了 QC 服务，接线只改一个环境变量，不用改内核。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "QUALITY_GATE_UNAVAILABLE",
                "QUALITY_GATE_BASE_URL is not configured; pass the conclusion "
                "inline as body.quality_gate instead (C11-11)")
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
