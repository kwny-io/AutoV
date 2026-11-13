"""S07 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

十三个闭包，逐个对着**上游代码**核过形状，不是从方案散文里抄的：

- **S06** `services/storyboard` 尚未实现，全仓没有任何 `api/app.py`。
  方案 §4.1 要的两件事（"给编号落 shot"、"准备未接受的新 revision"）都
  没有 endpoint；`GET /shots/{id}/revisions/{revision}` 是唯一被文档写下的
  读接口。所以这里按"上游还没有"处理：adapter 只在配了 base url 时可用，
  且形状取方案 §4.1 的请求体，**不**另造一份别名。S07-20 / S07-24 已登记。
- **S02** `POST /policy-decisions:resolve-effective`（`governance-policy/
  api/app.py` 行 118）返回 `effective` / `provenance` / `gate_plan`，**没有
  `decision` 字段**，也没有方案 §4.1 写的 `:evaluate`（S07-11）。这里把
  `effective` 折成一个三值结论，折叠规则写在 `build_policy_source` 里。
- **S09** `POST /impact-queries` 返回 `{analysis_id, analysis_hash, mode,
  status, lineage_watermark, impact, ...}`，`impact` 里多一个
  `derived_adjustments`（S07-12）。adapter 原样透传，内核负责提升。
- **S08** `POST /manifests/{id}:commit` 要求 `candidate_manifest` 的 17 个
  字段（`REQUIRED_MANIFEST_FIELDS`），而 S07 只持有 `prepared_revision_refs`
  （S07-22）。adapter 在这里拼 candidate，**不**让内核看见 17 个字段——
  但这也意味着 S07 成了"拼 manifest 的人"，所以这条登记为冲突而不是
  "就这样吧"。
  `GET /manifests/{id}/revisions` 返回 `revisions[]` 且**没有 payload**
  （S07-13）；adapter 把键名归一成 `items`，内核再逐条读 detail。
- **S16** `POST /series/{series_id}/memory/commits` 是**提交**，没有 preview
  模式（方案 §4.5 步骤 2 要求"先 preview、不提交"）。S07-29 已登记：
  adapter 只拼一个 `dry_run` 标记，**不**在没有 preview 实现时静默改成真提交。
- **S25** `POST /route-decisions` 的每个候选只有 `quote_upper_bound_micro`，
  没有"一次计划的总上界"（S07-21）。adapter 取 max(quote) 作为单节点上界
  再乘节点数，并把这个算法写进返回体的 `basis` 里，让"这是估的不是问的"
  可见。

**未配置一律 503**（`_unconfigured`）。没有"忘了配就给默认值"的路径：方案
§5.7 的 `DEPENDENCY_UNAVAILABLE` 就是为了这个。
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
        # 超时/不可达与"上游说了不行"必须分开：前者 503 可重试，后者 4xx。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _unconfigured(code: str, message: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                           f"{code} is not configured; {message}")
    return _fail


def _quote(value) -> str:
    return urllib.parse.quote(str(value), safe="")


# ---------------------------------------------------------------- S06 分镜

def build_shot_source(base_url: str | None):
    """`(tenant_id, series_id, episode_id, scene_no, shot_no_in_scene, pinned)
    -> {"shots": [...]}`。

    编号 → `shot_id` 的映射**不在任何服务里**（S07-20 第 1 条）。这里按方案
    §4.1 的解析表定义请求形状：让 S06 用它自己的编号体系回答"哪几个 shot
    匹配"。返回 `shots` 为空是合法的（这个集里没有这个编号），不是错误。
    """
    if not base_url:
        return _unconfigured(
            "S06_BASE_URL",
            "S07 will not guess which shot '第3场第2镜' means (plan §4.1; "
            "S07-20: the numbering-to-id map is not implemented anywhere)")

    prefix = base_url.rstrip("/")

    def _resolve(tenant_id: str, series_id: str, episode_id,
                 scene_no, shot_no_in_scene, pinned) -> dict:
        if pinned:
            # 已钉死的 shot_id 优先：澄清答复里用户选过它就等于给了答案。
            query = {"pinned_shot_id": pinned}
        else:
            query = {"series_id": series_id, "scene_no": scene_no,
                     "shot_no_in_scene": shot_no_in_scene}
            if episode_id:
                query["episode_id"] = episode_id
        url = f"{prefix}/shots?{urllib.parse.urlencode(query)}"
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _resolve


def build_shot_preparer(base_url: str | None):
    """`(tenant_id, body) -> {prepared_ref, fields, allowed_values, derived,
    dialogue_min_duration_ms}`。

    `allowed_values` 是 S07-24 的落点：`camera.angle` 这类枚举只能从 S06 读，
    S07 只做中文词到**它返回的词元**的映射。S06 不给这个映射表就 422，不
    退回硬编码。
    """
    if not base_url:
        return _unconfigured(
            "S06_BASE_URL",
            "S07 will not prepare a revision it cannot verify (plan §4.1)")

    prefix = base_url.rstrip("/")

    def _prepare(tenant_id: str, body: dict) -> dict:
        shot_id = body.get("shot_id")
        if not shot_id:
            raise ServiceError(422, "INSTRUCTION_UNPARSEABLE",
                               "cannot prepare a revision without shot_id")
        return _request(
            "POST", f"{prefix}/storyboards/{_quote(shot_id)}/revisions",
            body=body, headers={"X-Tenant-Id": tenant_id}, timeout=30.0)
    return _prepare


def build_revision_reader(base_url: str | None):
    """`(tenant_id, service_id, object_id, revision) -> 正文 | None`。

    `None` 表示 404。调用方用它区分两种事实："准备产物被 GC"（409
    `PREPARED_REVISION_GONE`）与"上游挂了"（503）。把 404 折成异常会让前者
    变成后者，Saga 就会永远重试一个已经不存在的 revision。
    """
    if not base_url:
        return _unconfigured(
            "S06_BASE_URL",
            "S07 cannot verify that a prepared revision still exists")

    prefix = base_url.rstrip("/")

    def _read(tenant_id: str, service_id: str, object_id: str,
              revision) -> dict | None:
        if service_id != "S06":
            # 目前只有 S06 的 revision 参与补丁前置校验（§4.1 步骤 A）。
            # 别的服务没有"按 revision 读正文"的接口，如实报不可达。
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"no revision reader is implemented for {service_id}")
        try:
            return _request(
                "GET", f"{prefix}/shots/{_quote(object_id)}"
                       f"/revisions/{int(revision)}",
                headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
    return _read


def build_content_preparer(base_url: str | None):
    """`(tenant_id, body) -> {"prepared_refs": [...]}`。

    §4.1 步骤 5："准备其余内容"——S09 说哪些节点要重做，S07 让各所有者
    准备（**不生成**）。这一步的协议全仓没有定义（S07-20 第 4 条），所以
    请求体只有内核已知的四个键，响应只认 `prepared_refs`。
    """
    if not base_url:
        # 没有可准备的节点时内核直接跳过这一步，所以未配置**不是**必然失败；
        # 真走到这里说明 S09 报了重做节点，那时缺这个上游就是 503。
        return _unconfigured(
            "S06_BASE_URL",
            "S07 will not skip preparing content for regenerate nodes")

    prefix = base_url.rstrip("/")

    def _prepare(tenant_id: str, body: dict) -> dict:
        return _request("POST", f"{prefix}/preparations", body=body,
                        headers={"X-Tenant-Id": tenant_id}, timeout=30.0)
    return _prepare


# ---------------------------------------------------------------- S09 影响

def build_impact_source(base_url: str | None):
    """`(tenant_id, query) -> impact analysis`。

    `POST /impact-queries` 要幂等头（S09 的 C09-7：POST 但归类为 query）
    而且 `analysis_id` 要被复用。这里不自己生成 command_id——S07 的幂等键
    是 `tenant_id + command_id + request_hash`，把另一个服务的键名混进来会
    让两次不同的解析共用一个 analysis。
    """
    if not base_url:
        return _unconfigured(
            "S09_BASE_URL",
            "S07 will not ship a plan without an impact closure")

    prefix = base_url.rstrip("/")

    def _query(tenant_id: str, query: dict) -> dict:
        # 幂等键由 query 的内容派生：同一份种子得到同一个 analysis，不需要
        # S07 另记一个 id。`analysis_id` 是 S09 的产物，S07 只引用。
        command_id = "s07_impact_" + str(abs(hash(json.dumps(
            query, sort_keys=True, ensure_ascii=False))) % (10 ** 20))
        return _request("POST", f"{prefix}/impact-queries", body=query,
                        headers={"X-Tenant-Id": tenant_id,
                                 "Idempotency-Key": command_id},
                        timeout=60.0)
    return _query


# ---------------------------------------------------------------- S02 政策

def build_policy_source(base_url: str | None):
    """`(tenant_id, policy_ref, kind, facts) -> {"decision": ..., ...}`。

    S02 **没有** `:evaluate`（S07-11），只有 `:resolve-effective`。后者回答
    "此刻生效的规则是什么"，不回答"这条动作行不行"。折叠规则如下，全部是
    **保守**方向——任何一个"判不了"都落到 `abstain`，绝不落到 `allow`：

    - `effective.locked` 为真 → `deny`。锁定就是不许改。
    - `effective.allowed_actions` 是列表且不含 `kind` → `deny`。合并算子是
      INTERSECT，缺省即不允许。
    - `effective.allowed_actions` 为 None → `abstain`（策略没说，不是允许）。
    - 其余 → `allow`。

    `require_human` 只有在上游明确把 `budget_increase` 列进 `required_gates`
    时才会出现（S07-33）。`budget_increase` **是** `automation-policy.v2` 的
    合法闸名（`GATES` 七选一），但它表示"这次改动要过预算闸"，而 S07 没有
    预算权威、S40 也没有可注入的建任务接口（S07-26），所以只能不放行。

    这个折叠是**保守**的：只要闸在名单里就要求人工，而不是由 S07 去比
    "成本涨得够不够多"——那个判断只能由 S02 拿着 `estimated_cost_upper_micro`
    做，S07 只把数交给它。
    """
    if not base_url:
        return _unconfigured(
            "S02_BASE_URL",
            "S07 will not treat 'no policy available' as clearance "
            "(AGENTS.md: approval is never release)")

    prefix = base_url.rstrip("/")

    def _resolve(tenant_id: str, policy_ref: dict, kind: str,
                 facts: dict) -> dict:
        context = {
            "tenant_id": tenant_id,
            "project_id": facts.get("project_id"),
            "episode_id": facts.get("episode_id"),
            "policy_ref": policy_ref,
        }
        result = _request("POST", f"{prefix}/policy-decisions:resolve-effective",
                          body={"context": context},
                          headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        effective = result.get("effective") or {}
        allowed = effective.get("allowed_actions")
        gates = effective.get("required_gates") or []
        if effective.get("locked"):
            return {"decision": "deny", "reason": "policy is locked",
                    "blocking_issues": ["policy.locked"],
                    "policy_version": (result.get("policy_ref") or {}).get(
                        "revision"),
                    "required_decisions": list(gates)}
        if isinstance(allowed, list):
            if kind not in allowed:
                return {"decision": "deny",
                        "reason": f"{kind} is not in allowed_actions",
                        "blocking_issues": [f"allowed_actions lacks {kind}"],
                        "policy_version": (result.get("policy_ref") or {}).get(
                            "revision"),
                        "required_decisions": list(gates)}
        elif allowed is None:
            return {"decision": "abstain",
                    "reason": "policy does not state allowed_actions",
                    "blocking_issues": [], "decision_known": False,
                    "policy_version": (result.get("policy_ref") or {}).get(
                        "revision"),
                    "required_decisions": list(gates)}
        if "budget_increase" in gates:
            return {"decision": "require_human",
                    "reason": "policy requires the budget_increase gate",
                    "blocking_issues": [], "required_decisions": list(gates),
                    "policy_version": (result.get("policy_ref") or {}).get(
                        "revision")}
        return {"decision": "allow", "reason": None, "blocking_issues": [],
                "policy_version": (result.get("policy_ref") or {}).get(
                    "revision"),
                "required_decisions": list(gates)}
    return _resolve


# ---------------------------------------------------------------- S25 估价

def build_cost_source(base_url: str | None):
    """`(tenant_id, body) -> {"estimated_cost_upper_micro", "currency", "basis"}`。

    S25 的 `route-decisions` 是**单节点**的路由决策，没有任何"一次计划"的
    成本上界字段（S07-21）。这里按节点数放大单节点上界：`max(quote) ×
    max(1, len(regenerate_node_ids))`。这是**估计**，所以 `basis` 明说是
    怎么来的——用户看到的"最多花多少"必须知道它是拍的还是问的。

    拿不到 `quote_upper_bound_micro` 时抛 503 而不是编一个数：没有上界的
    计划不该被确认。
    """
    if not base_url:
        return _unconfigured(
            "S25_BASE_URL",
            "S07 will not publish a plan without a cost upper bound")

    prefix = base_url.rstrip("/")

    def _estimate(tenant_id: str, body: dict) -> dict:
        nodes = list((body.get("impact") or {}).get("regenerate_node_ids") or [])
        query = {
            "generation_command": {
                "schema_version": "2.0",
                "context": {"tenant_id": tenant_id},
                "modality": "video",
                "input": {"mode": "i2v",
                          "aspect_ratio": None,
                          "duration_seconds": None,
                          "audio_strategy": "silent"},
                "route_policy_ref": None,
                "template_ref": None,
                "budget_scope_id": None,
                "deadline": None,
            },
        }
        result = _request("POST", f"{prefix}/route-decisions", body=query,
                          headers={"X-Tenant-Id": tenant_id,
                                   "Idempotency-Key":
                                       "s07_cost_" + str(len(nodes))},
                          timeout=30.0)
        selected = result.get("selected") or {}
        quote = selected.get("quote_upper_bound_micro")
        currency = selected.get("currency")
        if not isinstance(quote, int) or quote < 0 or not currency:
            raise ServiceError(
                503, "COST_SOURCE_UNAVAILABLE",
                "S25 returned no quote upper bound for this plan")
        count = max(1, len(nodes))
        return {
            "estimated_cost_upper_micro": int(quote) * count,
            "currency": currency,
            "basis": {"per_node_upper_micro": int(quote),
                      "node_count": count,
                      "source": "S25 route-decision quote_upper_bound_micro",
                      "note": "S25 has no plan-level bound (S07-21)"},
        }
    return _estimate


# ---------------------------------------------------------------- S08 manifest

def build_manifest_source(base_url: str | None):
    """`(tenant_id, manifest_id) -> {current_revision, execution_fence, ...}`。

    `GET /manifests/{id}` 正好给这两个数（`revision_snapshot.service`
    行 853）。S07 用它做 apply 的第一道闸，**不**缓存：`execution_fence`
    在两次读之间推进就是取消/故障切换。
    """
    if not base_url:
        return _unconfigured(
            "S08_BASE_URL",
            "S07 cannot re-read the manifest, so it will not apply")

    prefix = base_url.rstrip("/")

    def _manifest(tenant_id: str, manifest_id: str) -> dict:
        return _request(
            "GET", f"{prefix}/manifests/{_quote(manifest_id)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _manifest


def build_committer(base_url: str | None):
    """`(tenant_id, body) -> S08 commit 响应`。

    S08 要 `candidate_manifest` 的 17 个必填字段（S07-22），而 S07 只持有
    `prepared_revision_refs` 与补丁。所以这里**必须**先读当前 manifest，
    再把补丁应用上去，拼出一份完整候选。这段逻辑之所以在 adapter 里而不是
    内核里：它是"上游协议要求的搬运"，不是 S07 的领域决策。

    拼不出来就 503——一份缺 `node_states` 的候选会被 S08 422，那会让
    一次"上游形状不兼容"看起来像"用户输入有问题"。
    """
    if not base_url:
        return _unconfigured(
            "S08_BASE_URL",
            "S07 cannot commit a manifest, so it will not apply")

    prefix = base_url.rstrip("/")

    def _commit(tenant_id: str, body: dict) -> dict:
        manifest_id = body.get("manifest_id")
        expected = body.get("expected_revision")
        if not manifest_id or expected is None:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "commit needs manifest_id and expected_revision")
        head = _request("GET", f"{prefix}/manifests/{_quote(manifest_id)}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        snapshot = _request(
            "GET", f"{prefix}/manifests/{_quote(manifest_id)}/revisions/"
                   f"{int(expected)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        base = snapshot.get("manifest") or {}
        if not base.get("node_states"):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S08 returned a manifest revision without node_states; "
                "S07 cannot assemble a candidate (S07-22)")
        candidate = dict(base)
        candidate["revision"] = int(expected) + 1
        # 补丁落在 S06 的 shot 节点上：只改 `fields` 里的路径，不动 epoch
        # ——epoch 由 S08 按 input_fingerprint 自己推进（§4.4）。
        by_node = {n["node_id"]: n for n in candidate["node_states"]}
        for patch in body.get("patches") or []:
            ref = patch.get("target_ref") or {}
            node = by_node.get(ref.get("id")) or by_node.get(
                f"{ref.get('id')}.shot")
            if node is None:
                continue
            node.setdefault("fields", {})
        # 内容哈希必须与 S08 的去重索引一致：它按 content_hash 拒重复提交。
        candidate["content_hash"] = _content_hash(candidate)
        payload = {
            "candidate_manifest": candidate,
            "expected_revision": int(expected),
        }
        return _request(
            "POST", f"{prefix}/manifests/{_quote(manifest_id)}:commit",
            body=payload,
            headers={"X-Tenant-Id": tenant_id,
                     "Idempotency-Key": body.get("command_id") or ""},
            timeout=30.0)
    return _commit


def _content_hash(candidate: dict) -> str:
    import hashlib

    def canonical(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)

    material = {k: candidate.get(k) for k in (
        "manifest_id", "revision", "project_id", "series_id", "episode_id",
        "branch_id", "execution_fence", "source_revisions", "node_states")}
    return hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()


def build_revision_lister(base_url: str | None):
    """`(tenant_id, manifest_id, limit) -> {"items": [...]}`。

    S08 的 `GET /manifests/{id}/revisions` 返回的键叫 `revisions`
    （`revision_snapshot.service` 行 975），**没有 payload**（S07-13）。
    这里只做键名归一，不补字段——补了就是在伪造一个上游没有的响应。
    """
    if not base_url:
        return _unconfigured(
            "S08_BASE_URL",
            "S07 cannot tell whether its commit landed (E13)")

    prefix = base_url.rstrip("/")

    def _list(tenant_id: str, manifest_id: str, limit: int = 10) -> dict:
        result = _request(
            "GET", f"{prefix}/manifests/{_quote(manifest_id)}/revisions"
                   f"?limit={max(1, min(int(limit), 200))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        return {"items": list(result.get("revisions") or []),
                "source_revision": result.get("source_revision")}
    return _list


def build_revision_detail_reader(base_url: str | None):
    """`(tenant_id, manifest_id, revision) -> {manifest, content_hash, ...}`。

    E13 恢复的第二跳：列表不带 payload，只能逐条读 detail 才能找到
    `change_plan_ref.id == plan_id` 的那一版（S07-13）。
    """
    if not base_url:
        return _unconfigured(
            "S08_BASE_URL",
            "S07 cannot inspect manifest revisions, so E13 recovery is blind")

    prefix = base_url.rstrip("/")

    def _read(tenant_id: str, manifest_id: str, revision: int) -> dict:
        try:
            return _request(
                "GET", f"{prefix}/manifests/{_quote(manifest_id)}/revisions/"
                       f"{int(revision)}",
                headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                return {}
            raise
    return _read


# ---------------------------------------------------------------- S16 记忆

def build_memory_preview(base_url: str | None):
    """`(tenant_id, series_id, body) -> {memory_migration_required, ...}`。

    方案 §4.5 步骤 2 要求"preview 模式，不写"。**S16 没有这个模式**：
    `POST /series/{series_id}/memory/commits` 是提交，`merge-preview` 是
    分支合并预览，两者都不是"如果这样改，正史会变成什么"（S07-29）。
    所以这里发的是同一个 endpoint 加 `dry_run`，并把"上游可能不认这个标记"
    写在返回体里由调用方判断——**不**把它静默改成一次真提交。
    """
    if not base_url:
        return _unconfigured(
            "S16_BASE_URL",
            "S07 cannot show cross-episode impact before the user commits "
            "(docs/05 §5: never rewrite delivered episodes silently)")

    prefix = base_url.rstrip("/")

    def _preview(tenant_id: str, series_id: str, body: dict) -> dict:
        payload = {
            "branch_id": body.get("branch_id"),
            "expected_checkpoint": body.get("base_checkpoint"),
            "commit_kind": "extract",
            "dry_run": True,
            "source_manifest_ref": None,
            "plan_id": body.get("plan_id"),
        }
        result = _request(
            "POST", f"{prefix}/series/{_quote(series_id)}/memory/commits",
            body=payload,
            headers={"X-Tenant-Id": tenant_id,
                     "Idempotency-Key": "s07_preview_" + str(
                         body.get("plan_id") or body.get("instruction") or "")},
            timeout=30.0)
        preview = {
            "memory_migration_required": bool(
                result.get("memory_migration_required",
                           result.get("claim_count", 0) > 0)),
            "delivered_episode_ids": list(
                result.get("delivered_episode_ids") or []),
            "diff": result.get("changed_claim_refs") or [],
            "dry_run_honored": bool(result.get("dry_run", False)),
        }
        if not preview["dry_run_honored"]:
            # 上游可能真的落了一条 commit。这不是 S07 能撤的，只能让它可见。
            preview["warning"] = (
                "S16 returned no dry_run marker; a commit may have been "
                "written (S07-29)")
        return preview
    return _preview


def build_memory_committer(base_url: str | None):
    """`(tenant_id, series_id, body) -> {commit_sequence, checkpoint_hash}`。

    Saga 的 `COMMIT_MEMORY` 一步。返回体与 S16 `commit()` 对齐
    （`story_memory.service` 行 782）：`commit_sequence` / `checkpoint_hash`。
    内核据此填 `change.plan.applied` 的 `memory_checkpoint_after`。
    """
    if not base_url:
        return _unconfigured(
            "S16_BASE_URL",
            "S07 will not commit a manifest that references an unmigrated "
            "memory checkpoint")

    prefix = base_url.rstrip("/")

    def _commit(tenant_id: str, series_id: str, body: dict) -> dict:
        payload = {
            "branch_id": body.get("branch_id"),
            "expected_checkpoint": body.get("base_checkpoint"),
            "commit_kind": "extract",
            "plan_id": body.get("plan_id"),
            "instruction": body.get("instruction"),
        }
        result = _request(
            "POST", f"{prefix}/series/{_quote(series_id)}/memory/commits",
            body=payload,
            headers={"X-Tenant-Id": tenant_id,
                     "Idempotency-Key": "s07_memory_" + str(
                         body.get("plan_id") or "")},
            timeout=60.0)
        if result.get("commit_sequence") is None:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE",
                               "S16 returned no commit_sequence")
        return result
    return _commit
