"""S18 业务内核：上下文检索与组装。

守住的几条，对应 `plans/S18-context-retrieval.md`：

1. **不拥有事实**（§0 / 反模式 1）。pack 里只有 `claim_ref`、禁则、水位、
   `content_hash`。库里没有 `claim` / `fact` / `state` 表。事实的唯一入口是注入的
   `memory_query`，每次实时问，不缓存。
2. **可见性不在这里重写**（§3 / 反模式 3）。分支、故事有效期、角色知情、
   观众揭示这四步由 S16 的 `query` 用 `perspective` 参数执行。S18 如果自己再判
   一遍，就出现了第二个"谁看得见什么"的权威 —— 两份公式迟早分叉，且分叉方向
   无从判定。S18 只消费 S16 的 `claims` 与 `withheld_claim_ids`。
3. **水位不够就拒绝**（§5 / 反模式 7）。`MEMORY_WATERMARK_BEHIND` 原样上抛 409，
   `MEMORY_UNAVAILABLE` 上抛 503。绝不返回旧 pack 标 `stale=true` 让调用方自己判断。
4. **硬事实不被预算截断**（§4.4 / 反模式 8）。放不下就 `BUDGET_TOO_SMALL_FOR_
   REQUIRED_FACTS` 并附最低需求，不静默裁剪。
5. **禁则只说限制，不说内容**（§4.8 / 反模式 5）。禁则文本过泄漏扫描，命中即拒绝。

一个刻意的取舍：语义补充池在本实现里为空（没有向量索引）。方案 §5.2 允许硬事实
完全走 S16、语义池走本地索引；只做前半段是正确但覆盖更窄的版本，所以每个 pack
都显式报 `semantic_coverage: "disabled"`，而不是假装检索完整。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    CANDIDATE_ROLES,
    CONTRACT_GAPS,
    DEFAULT_SECTION_BUDGET,
    HARD_KINDS,
    PERSPECTIVES,
    SCHEMA,
    SECTION_OF_KIND,
    SECTION_ORDER,
    TASK_ROLES,
)

SOURCE = "s18.context-retrieval"
EVENT_TYPE = "context.pack.created"
SCHEMA_VERSION = "2.0"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def _upstream_code(exc: BaseException) -> str | None:
    """按属性认上游错误码，不按类型认（同 S17，上游抛的是它自己的类）。"""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def _upstream_details(exc: BaseException) -> dict:
    details = getattr(exc, "details", None)
    return details if isinstance(details, dict) else {}


class Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


def _approx_tokens(text: str) -> int:
    """确定性 token 估算：不用真实 tokenizer，避免依赖与不确定。

    按 UTF-8 字节 / 3 估。中文约 3 字节/字、英文约 1 token/4 字节，取 3 是本仓
    其他服务的口径。它的作用是**预算分账可比、可复现**，不是精确计费。
    """
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


class ContextRetrievalService:
    def __init__(self, conn: sqlite3.Connection, *, memory_query, clock=_now):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self._memory_query = memory_query
        self._clock = clock

    # ---- 入口 ------------------------------------------------------------

    def create_pack(self, tenant_id: str, body: dict) -> dict:
        request = self._validate(tenant_id, body)
        checkpoint = request["required_checkpoint"]
        branch_id = request["branch_id"]

        # §5 情形 A 之外的一切：硬事实永远实时问 S16，从不读本地派生物。
        query_body = {
            "branch_id": branch_id,
            "minimum_checkpoint": checkpoint["commit_sequence"],
            "as_of_commit_sequence": checkpoint["commit_sequence"],
            "perspective": self._perspective_for_s16(request),
            "kinds": list(request["required_kinds"]),
            "story_clock_id": request["story_clock_id"],
            "story_from_tick": request["story_tick"],
            "story_to_tick": request["story_tick"] + 1,
        }
        if request["focus_entity_ids"]:
            query_body["entity_ids"] = list(request["focus_entity_ids"])

        try:
            query = self._memory_query(tenant_id, request["series_id"], query_body)
        except Exception as exc:
            code = _upstream_code(exc)
            # §5.1：B 情形原样 409；C 情形 503。都不降级、不返回 stale pack。
            if code in ("MEMORY_WATERMARK_BEHIND", "MEMORY_UNAVAILABLE"):
                raise ServiceError(
                    409 if code == "MEMORY_WATERMARK_BEHIND" else 503,
                    code, "memory authority cannot satisfy the required "
                          "checkpoint", retryable=code == "MEMORY_UNAVAILABLE",
                    details=_upstream_details(exc))
            raise

        actual_seq = query.get("checkpoint")
        actual_hash = query.get("checkpoint_hash")
        # §2 第 1 条：required_checkpoint 是硬门。S16 报上来的 checkpoint 必须
        # 就是它，既不能少也不能多。
        if actual_seq != checkpoint["commit_sequence"]:
            raise ServiceError(
                409, "CHECKPOINT_MISMATCH",
                "memory authority returned a different checkpoint",
                details={"declared": checkpoint["commit_sequence"],
                         "actual": actual_seq})
        if checkpoint.get("content_hash") is not None \
                and checkpoint["content_hash"] != actual_hash:
            raise ServiceError(
                409, "CHECKPOINT_MISMATCH",
                "checkpoint content_hash does not match",
                details={"declared_hash": checkpoint.get("content_hash"),
                         "actual_hash": actual_hash})

        pack = self._assemble(tenant_id, request, query)
        with Transaction(self.conn):
            self._insert_pack(tenant_id, request, pack)
            self._insert_assumptions(tenant_id, request, pack)
            self._upsert_watermark(tenant_id, request, actual_seq, actual_hash)
            self._publish_pack_created(tenant_id, request, pack)
        return self._render(tenant_id, pack)

    def get_pack(self, tenant_id: str, pack_id: str,
                 revision: int | None = None) -> dict:
        """§1.5：契约对象 + 结构化扩展视图。跨租户 404，不泄漏存在性。"""
        if revision is None:
            row = self.conn.execute(
                "SELECT * FROM context_pack WHERE tenant_id=? AND context_pack_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (tenant_id, pack_id)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM context_pack WHERE tenant_id=? AND context_pack_id=?"
                " AND revision=?", (tenant_id, pack_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "PACK_NOT_FOUND", pack_id)
        assumptions = self.conn.execute(
            "SELECT * FROM prohibited_assumption WHERE tenant_id=?"
            " AND context_pack_id=? AND revision=? ORDER BY assumption_id",
            (tenant_id, pack_id, row["revision"])).fetchall()
        return {
            "pack": self._render_row(row),
            "prohibited_assumption_details": [
                self._render_assumption_row(a) for a in assumptions],
            "x-contract-gaps": list(CONTRACT_GAPS),
        }

    # ---- 请求校验 --------------------------------------------------------

    def _validate(self, tenant_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "body must be an object")
        for field in ("command_id", "series_id", "branch_id", "episode_id",
                      "episode_order", "task_role", "perspective",
                      "required_checkpoint", "token_budget", "story_tick",
                      "story_clock_id"):
            if field not in body:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{field} is required")

        role = body["task_role"]
        if role not in TASK_ROLES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"task_role must be one of {TASK_ROLES}")

        perspective = body["perspective"]
        if not isinstance(perspective, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "perspective must be an object")
        mode = perspective.get("mode")
        if mode not in PERSPECTIVES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"perspective.mode must be one of {PERSPECTIVES}")
        characters = perspective.get("character_ids") or []
        if mode == "character" and not characters:
            # character 视角不点名角色 = 闭世界下什么都不可见，静默返回空 pack
            # 比报错危险得多。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "perspective.character_ids is required when "
                               "mode is 'character'")

        checkpoint = body["required_checkpoint"]
        if not isinstance(checkpoint, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "required_checkpoint must be an object")
        seq = checkpoint.get("commit_sequence")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "required_checkpoint.commit_sequence must be >= 0")

        budget = body["token_budget"]
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "token_budget must be a positive integer")

        # §2 第 4 条：story_tick 必填，缺了不能默认当前。
        tick = body["story_tick"]
        if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "story_tick must be a non-negative integer")
        if not body["story_clock_id"]:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "story_clock_id is required")

        include_candidates = bool(body.get("include_candidates"))
        if include_candidates and role not in CANDIDATE_ROLES:
            raise ServiceError(
                400, "CANDIDATES_NOT_ALLOWED",
                f"include_candidates is only allowed for {CANDIDATE_ROLES}")

        order = body["episode_order"]
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "episode_order must be >= 1")

        focus = body.get("focus") or {}
        required = self._required_kinds(tenant_id, role)
        return {
            "command_id": body["command_id"],
            "series_id": body["series_id"],
            "branch_id": body["branch_id"],
            "episode_id": body["episode_id"],
            "episode_order": order,
            "task_role": role,
            "perspective_mode": mode,
            "perspective_characters": list(characters),
            "required_checkpoint": checkpoint,
            "token_budget": budget,
            "story_tick": tick,
            "story_clock_id": body["story_clock_id"],
            "focus_entity_ids": list(focus.get("entity_ids") or []),
            "focus_thread_ids": list(focus.get("thread_ids") or []),
            "include_candidates": include_candidates,
            "required_kinds": required,
            "manifest_ref": body.get("manifest_ref"),
            "policy_ref": body.get("policy_ref"),
            "home_cell": body.get("home_cell") or "cell-local",
            "correlation_id": body.get("correlation_id") or body["command_id"],
        }

    def _required_kinds(self, tenant_id: str, role: str) -> tuple[str, ...]:
        """§4.1 + §7：硬事实全进，role 表只加不减。

        方案 §7 的「必含 kinds」是**下限**，不是白名单 —— 写成白名单会让 writer
        包漏掉 `entity_state`，于是 E08-1「改稿后第 3 集 writer pack 引用新
        revision 的持有者」永远拿不到那条 claim，而缺一个硬事实就会写错稿。
        """
        row = self.conn.execute(
            "SELECT required_kinds FROM role_profile WHERE tenant_id=?"
            " AND task_role=? ORDER BY profile_version DESC LIMIT 1",
            (tenant_id, role)).fetchone()
        if row is not None:
            return tuple(sorted(set(HARD_KINDS) |
                                set(json.loads(row["required_kinds"]))))
        return tuple(sorted(set(HARD_KINDS) | set(ROLE_EXTRA_KINDS.get(role, ()))))

    def _perspective_for_s16(self, request: dict) -> dict:
        """把 §2 的 perspective 翻成 S16 query 的 `perspective` 参数。

        `character` 视角把角色集交给 S16 —— 闭世界可见性公式只存在于 S16
        （`_visible_to_character`），S18 不复制它。
        """
        mode = request["perspective_mode"]
        out = {"mode": mode,
               "audience_as_of_episode_order": request["episode_order"]}
        if mode == "character":
            out["character_ids"] = list(request["perspective_characters"])
        return out

    # ---- 组装 ------------------------------------------------------------

    def _assemble(self, tenant_id: str, request: dict, query: dict) -> dict:
        claims = query.get("claims") or []
        withheld = set(query.get("withheld_claim_ids") or [])

        by_kind: dict[str, list] = {}
        for claim in claims:
            by_kind.setdefault(claim.get("kind"), []).append(claim)

        # §4.5：禁则由观测到的知识/状态差异推导，而不是由模型自由生成。
        assumptions = self._build_assumptions(request, claims, withheld,
                                              query)
        self._assert_no_leak(request, assumptions, claims,
                             sorted(withheld))

        sections, breakdown, total = self._allocate(tenant_id, request, by_kind)
        if not total["fits"]:
            raise ServiceError(
                422, "BUDGET_TOO_SMALL_FOR_REQUIRED_FACTS",
                "token budget cannot hold the required hard facts",
                details={"minimum_tokens": total["minimum"],
                         "token_budget": request["token_budget"],
                         "per_section": total["minimum_by_section"]})

        claim_refs = self._claim_refs(sections)
        conflicts = list(query.get("unresolved_conflicts") or [])

        content_hash = _sha256_hex(_jcs({
            "tenant_id": tenant_id,
            "series_id": request["series_id"],
            "branch_id": request["branch_id"],
            "target_episode_order": request["episode_order"],
            "task_role": request["task_role"],
            "perspective": request["perspective_mode"],
            "perspective_character_ids": sorted(
                request["perspective_characters"]),
            "checkpoint_sequence": query.get("checkpoint"),
            "claim_refs": claim_refs,
            "prohibited_assumptions": [a["text"] for a in assumptions],
        }))

        return {
            "context_pack_id": "ctx_" + uuid.uuid4().hex[:24],
            "revision": 1,
            "series_id": request["series_id"],
            "branch_id": request["branch_id"],
            "task_role": request["task_role"],
            "target_episode_id": request["episode_id"],
            "target_episode_order": request["episode_order"],
            "perspective": request["perspective_mode"],
            "perspective_character_ids": request["perspective_characters"],
            "checkpoint_sequence": query.get("checkpoint"),
            "checkpoint_hash": query.get("checkpoint_hash"),
            "source_watermark": query.get("checkpoint"),
            "required_checkpoint": request["required_checkpoint"]["commit_sequence"],
            "manifest_ref": request["manifest_ref"],
            "policy_ref": request["policy_ref"],
            "claim_refs": claim_refs,
            # 本实现没有 S10 / S28 的资产解析注入；空数组而不是假造 id。
            "asset_refs": [],
            "plot_thread_refs": [self._ref(c) for c in by_kind.get("plot_thread", [])],
            "episode_blocks": self._episode_blocks(request, by_kind),
            "prohibited_assumptions": [a["text"] for a in assumptions],
            "assumption_rows": assumptions,
            "unresolved_conflicts": conflicts,
            "token_budget": request["token_budget"],
            "tokens_used": total["used"],
            "budget_breakdown": breakdown,
            "forbidden_leak_scan": request.get("_leak_scan")
                                   or {"scanned_terms": 0, "hits": 0,
                                       "result": "clean"},
            "semantic_coverage": "disabled",
            "candidate": request["include_candidates"],
            "pack_for_delivery": False,
            "content_hash": content_hash,
        }

    # ---- 预算 ------------------------------------------------------------

    def _allocate(self, tenant_id: str, request: dict,
                  by_kind: dict) -> tuple[dict, list, dict]:
        """§4.4。硬事实先无条件占位，剩余给补充池。确定性：排序键全确定。"""
        budget = request["token_budget"]
        profile = self._section_budget(tenant_id, request["task_role"])

        sections = {name: [] for name in SECTION_ORDER}
        for kind in request["required_kinds"]:
            for claim in by_kind.get(kind, []):
                sections[SECTION_OF_KIND.get(kind, "continuity")].append(
                    self._entry(claim))

        used = 0
        minimum_by_section = {}
        breakdown = []
        for name in SECTION_ORDER:
            entries = sorted(sections[name], key=lambda e: (e["kind"], e["id"]))
            cap = int(budget * profile.get(name, 0.0))
            need = sum(e["tokens"] for e in entries)
            minimum_by_section[name] = need
            kept = []
            for entry in entries:
                if entry["tokens"] <= cap:
                    kept.append(entry)
                    cap -= entry["tokens"]
                else:
                    # 硬事实放不进 section → 不截断，整包失败（反模式 8）。
                    return {}, [], {
                        "fits": False, "minimum": sum(minimum_by_section.values()),
                        "minimum_by_section": minimum_by_section,
                        "used": used}
            sections[name] = kept
            section_used = sum(e["tokens"] for e in kept)
            used += section_used
            breakdown.append({
                "section": name, "budget": int(budget * profile.get(name, 0.0)),
                "used": section_used, "dropped": [],
            })
        # 硬事实总量不得超过总预算 —— 每 section 各自够但总账不够同样失败。
        if used > budget:
            return {}, [], {"fits": False,
                            "minimum": used,
                            "minimum_by_section": minimum_by_section,
                            "used": used}
        return sections, breakdown, {"fits": True, "used": used,
                                     "minimum": used,
                                     "minimum_by_section": minimum_by_section}

    def _section_budget(self, tenant_id: str, role: str) -> dict:
        row = self.conn.execute(
            "SELECT section_budget FROM role_profile WHERE tenant_id=?"
            " AND task_role=? ORDER BY profile_version DESC LIMIT 1",
            (tenant_id, role)).fetchone()
        if row is not None:
            return json.loads(row["section_budget"])
        return dict(DEFAULT_SECTION_BUDGET)

    def _entry(self, claim: dict) -> dict:
        fact = claim.get("fact") or {}
        text = _jcs({"kind": claim.get("kind"),
                     "subject": fact.get("subject_id"),
                     "predicate": fact.get("predicate"),
                     "value": fact.get("value")})
        return {"kind": claim.get("kind"), "id": claim.get("claim_id"),
                "revision": claim.get("revision"),
                "content_hash": claim.get("content_hash"),
                "tokens": _approx_tokens(text), "claim": claim}

    def _claim_refs(self, sections: dict) -> list:
        refs = []
        for name in SECTION_ORDER:
            for entry in sections[name]:
                ref = self._ref(entry["claim"])
                if ref is not None:
                    refs.append(ref)
        return refs

    def _ref(self, claim: dict) -> dict | None:
        """契约要求 `content_hash` 是 64-hex，而 S16 的 `_render_claim` 不返回它。

        上游给了就用真的；没给用 `_placeholder_hash` 的确定性占位（只满足形状，
        不声称是内容哈希），缺口在 `CONTRACT_GAPS` 里逐条报出。
        """
        return {
            "service_id": "S16",
            "id": claim["claim_id"],
            "revision": claim.get("revision") or 1,
            "content_hash": claim.get("content_hash") or _placeholder_hash(claim),
        }

    # ---- 禁则 ------------------------------------------------------------

    def _build_assumptions(self, request: dict, claims: list, withheld: set,
                           query: dict) -> list:
        """§4.5。每条禁则都能追溯到一个 claim 或一条 S16 判定。

        「只说限制，不说内容」(§4.8)：禁则里不出现被禁止命题的取值。
        """
        out = []
        seen = {}

        def add(kind, severity, text, basis, refs, entities, template):
            key = (kind, text)
            if key in seen:
                return
            seen[key] = True
            out.append({
                "assumption_id": f"pa_{len(out) + 1:03d}",
                "rule_kind": kind,
                "severity": severity,
                "text": text,
                "basis": basis,
                "claim_refs": refs,
                "entity_ids": entities,
                "generated_by": template,
            })

        mode = request["perspective_mode"]
        characters = request["perspective_characters"]

        # §4.2 character 视角：S16 判定为不可见的知识命题 → KNOWLEDGE 禁则。
        # 依据是 S16 的 withheld 列表，不是 S18 自己算的可见性。被扣下的 claim
        # 正文本就拿不到，所以禁则只能说「存在不可见命题」，不说内容（§4.8）。
        if mode == "character" and withheld:
            add("KNOWLEDGE", "hard",
                "部分命题对当前视角的角色不可见，不得在生成中表现为已知晓",
                {"reason_code": "S16_WITHHELD",
                 "withheld_count": len(withheld)},
                [], sorted(characters), "knowledge-unaware@v1")

        # §4.2 `believes` 错误值：S16 已把「她相信的值」作为可见 claim 给出，
        # 真相本身不在 claims 里。禁则只能说限制。
        #
        # `belief_type` 在 `epistemic_scope` 里，不在 `fact` 里 —— S16 的
        # `_write_revision` 是从 scope 取它写进 knowledge_state 投影的。
        for claim in claims:
            if claim.get("kind") != "knowledge_state":
                continue
            fact = claim.get("fact") or {}
            scope = claim.get("epistemic_scope") or {}
            if scope.get("belief_type") == "believes":
                add("KNOWLEDGE_BELIEF", "hard",
                    f"{fact.get('subject_id')} 对该命题持有一个与真实情况不同的判断，"
                    "不得表现为已知晓真实情况",
                    {"claim_id": claim["claim_id"],
                     "revision": claim.get("revision")},
                    [self._ref(claim)], [fact.get("subject_id")],
                    "knowledge-belief@v1")

        # §4.5 STATE：状态在有效期内，不得无事件改变。
        for claim in claims:
            kind = claim.get("kind")
            fact = claim.get("fact") or {}
            if kind == "entity_state":
                predicate = fact.get("predicate") or ""
                add("STATE", "hard",
                    f"{fact.get('subject_id')} 的 {predicate} 处于已记录状态，"
                    "不得在缺少事件的情况下改变",
                    {"claim_id": claim["claim_id"],
                     "revision": claim.get("revision")},
                    [self._ref(claim)], [fact.get("subject_id")],
                    "state-ongoing@v1")
            elif kind == "relationship":
                add("RELATION", "hard",
                    f"{fact.get('subject_id')} 的当前关系已确定，不得表现为相反关系",
                    {"claim_id": claim["claim_id"],
                     "revision": claim.get("revision")},
                    [self._ref(claim)], [fact.get("subject_id")],
                    "relation@v1")
            elif kind == "plot_thread":
                add("THREAD", "hard",
                    f"伏笔 {fact.get('subject_id')} 尚未到揭示时点，不得提前回收",
                    {"claim_id": claim["claim_id"],
                     "revision": claim.get("revision")},
                    [self._ref(claim)], [fact.get("subject_id")],
                    "thread-open@v1")

        # §4.5 BRANCH：非 canon 分支请求的产物不得被当作 canon 事实。
        if request["branch_id"] != "canon":
            add("BRANCH", "soft",
                "本上下文的产地不是 canon 分支，不得把其中的内容当作正史",
                {"reason_code": "NON_CANON_BRANCH",
                 "branch_id": request["branch_id"]},
                [], [], "branch@v1")

        # §4.5 CANDIDATE：候选包永久不可交付。
        if request["include_candidates"]:
            add("CANDIDATE", "hard",
                "本上下文处于候选模式，其中的内容未经接受，不得当作既有事实",
                {"reason_code": "CANDIDATE_MODE"},
                [], [], "candidate@v1")

        out.sort(key=lambda a: (0 if a["severity"] == "hard" else 1,
                                a["assumption_id"]))
        return out

    def _assert_no_leak(self, request: dict, assumptions: list,
                        visible: list, withheld_claims: list) -> None:
        """§4.8。禁则只说限制，不说内容 —— 扫描的禁词是**不可见**的 claim。

        方向很容易搞反：拿"可见 claim 的 value"当禁词，会把禁则里合法的实体
        指称（例如「道具 X 处于已记录状态」）判成泄漏；真正该防的是把**看不见
        的**那条事实抄进禁则 ——「林岚尚不知道陈默是她失散多年的哥哥」。
        所以禁词来自 §4.8 第 1 步的集合 I（被过滤掉、`withheld_claim_ids` 里
        的那些）。

        本实现只能扫 `withheld_claim_ids` 本身：S16 对不可见 claim 只给 id，不给
        正文，这正是闭世界的正确行为，S18 拿不到它的取值。因此这里断言的是
        **禁则文本里不得出现任何 withheld claim id**（有人为了可读性把 id 塞进
        禁则，等于标注了"这条存在"）。基于 entity_registry 别名的扫描（§4.8 第
        2 步）无法实现并在 `CONTRACT_GAPS` 里登记。
        """
        hidden_ids = [str(cid) for cid in withheld_claims if cid]
        for row in assumptions:
            if not row["text"].strip():
                raise ServiceError(
                    500, "LEAK_DETECTED", "prohibited assumption text is empty")
            for hidden in hidden_ids:
                if hidden in row["text"]:
                    raise ServiceError(
                        500, "LEAK_DETECTED",
                        "prohibited assumption names a withheld claim")
            # 禁则不得重复它引用不到的东西：每条禁则的 basis 必须可追溯。
            if not row["basis"]:
                raise ServiceError(
                    500, "LEAK_DETECTED",
                    "prohibited assumption carries no traceable basis")
        request["_leak_scan"] = {
            "scanned_terms": len(assumptions) + len(hidden_ids),
            "visible_claims": len(visible),
            "withheld_claims": len(hidden_ids),
            "hits": 0, "result": "clean"}

    # ---- 摘要块 ----------------------------------------------------------

    def _episode_blocks(self, request: dict, by_kind: dict) -> list:
        """§4.6。S18 不隐式调模型生成摘要 —— 缺 EpisodeSummary 就报缺。"""
        return [{
            "block_id": f"blk_ep{request['episode_order']}",
            "episode_order": request["episode_order"],
            "layer": "intent",
            "summary_text": None,
            "claim_refs": [],
            "tokens": 0,
            "missing": True,
            "requires": "plan:compute",
        }]

    # ---- 落库 ------------------------------------------------------------

    def _insert_pack(self, tenant_id: str, request: dict, pack: dict) -> None:
        self.conn.execute(
            "INSERT INTO context_pack (tenant_id, context_pack_id, revision,"
            " command_id, series_id, branch_id, task_role, target_episode_id,"
            " target_episode_order, perspective, perspective_character_ids,"
            " checkpoint_sequence, checkpoint_hash, source_watermark,"
            " required_checkpoint, manifest_ref, policy_ref, claim_refs,"
            " asset_refs, plot_thread_refs, episode_blocks,"
            " prohibited_assumptions, unresolved_conflicts, token_budget,"
            " tokens_used, budget_breakdown, forbidden_leak_scan, content_hash,"
            " candidate, pack_for_delivery, stale, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, pack["context_pack_id"], pack["revision"],
             request["command_id"], pack["series_id"], pack["branch_id"],
             pack["task_role"], pack["target_episode_id"],
             pack["target_episode_order"], pack["perspective"],
             _jcs(pack["perspective_character_ids"]),
             pack["checkpoint_sequence"], pack["checkpoint_hash"] or "",
             pack["source_watermark"], pack["required_checkpoint"],
             _jcs(pack["manifest_ref"]) if pack["manifest_ref"] else None,
             _jcs(pack["policy_ref"]) if pack["policy_ref"] else None,
             _jcs(pack["claim_refs"]), _jcs(pack["asset_refs"]),
             _jcs(pack["plot_thread_refs"]), _jcs(pack["episode_blocks"]),
             _jcs(pack["prohibited_assumptions"]),
             _jcs(pack["unresolved_conflicts"]), pack["token_budget"],
             pack["tokens_used"], _jcs(pack["budget_breakdown"]),
             _jcs(pack["forbidden_leak_scan"]), pack["content_hash"],
             1 if pack["candidate"] else 0,
             1 if pack["pack_for_delivery"] else 0, 0, self._clock()))

    def _insert_assumptions(self, tenant_id: str, request: dict,
                            pack: dict) -> None:
        for row in pack["assumption_rows"]:
            self.conn.execute(
                "INSERT INTO prohibited_assumption (tenant_id, context_pack_id,"
                " revision, assumption_id, text, rule_kind, severity, basis,"
                " claim_refs, entity_ids, generated_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, pack["context_pack_id"], pack["revision"],
                 row["assumption_id"], row["text"], row["rule_kind"],
                 row["severity"], _jcs(row["basis"]),
                 _jcs(row["claim_refs"]), _jcs(row["entity_ids"]),
                 row["generated_by"]))

    def _upsert_watermark(self, tenant_id: str, request: dict, seq: int,
                          hash_value: str | None) -> None:
        """§6.5：`applied_commit_sequence` 只增不减。更小的序列丢弃。"""
        current = self.conn.execute(
            "SELECT applied_commit_sequence FROM watermark WHERE tenant_id=?"
            " AND series_id=? AND branch_id=?",
            (tenant_id, request["series_id"], request["branch_id"])).fetchone()
        if current is not None and current["applied_commit_sequence"] >= seq:
            return
        self.conn.execute(
            "INSERT INTO watermark (tenant_id, series_id, branch_id,"
            " applied_commit_sequence, applied_checkpoint_hash, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, series_id, branch_id) DO UPDATE SET"
            " applied_commit_sequence=excluded.applied_commit_sequence,"
            " applied_checkpoint_hash=excluded.applied_checkpoint_hash,"
            " updated_at=excluded.updated_at",
            (tenant_id, request["series_id"], request["branch_id"], seq,
             hash_value, self._clock()))

    def _publish_pack_created(self, tenant_id: str, request: dict,
                              pack: dict) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "context_pack_id": pack["context_pack_id"],
            "revision": pack["revision"],
            "task_role": pack["task_role"],
            "target_episode_order": pack["target_episode_order"],
            "checkpoint": {"commit_sequence": pack["checkpoint_sequence"],
                           "content_hash": pack["checkpoint_hash"]},
            "claim_ref_count": len(pack["claim_refs"]),
            "prohibited_assumption_count": len(pack["prohibited_assumptions"]),
            "tokens_used": pack["tokens_used"],
            "content_hash": pack["content_hash"],
        }
        sequence = self._next_sequence(tenant_id, pack)
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_TYPE,
            subject=f"{pack['series_id']}/{pack['branch_id']}/"
                    f"ep{pack['target_episode_order']}",
            tenant_id=tenant_id, home_cell=request["home_cell"],
            aggregate_id=f"{pack['series_id']}:{pack['branch_id']}:"
                         f"ep{pack['target_episode_order']}",
            sequence=sequence,
            correlation_id=request["correlation_id"], data=data))

    def _next_sequence(self, tenant_id: str, pack: dict) -> int:
        """同一 aggregate 下的事件序号。

        不能用 pack revision：同一集、同一 aggregate 下可以有多个 task_role 的
        pack，各自 revision 都是 1。用「该 aggregate 已有事件数 + 1」。
        """
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE tenant_id=?"
            " AND aggregate_id=?", (tenant_id,
                                    f"{pack['series_id']}:{pack['branch_id']}:"
                                    f"ep{pack['target_episode_order']}")).fetchone()
        return int(count["n"]) + 1

    # ---- 渲染 ------------------------------------------------------------

    def _render(self, tenant_id: str, pack: dict) -> dict:
        """§1.5：契约对象本身。扩展字段只在 GET 的详细视图里。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "context_pack_id": pack["context_pack_id"],
            "revision": pack["revision"],
            "tenant_id": tenant_id,
            "series_id": pack["series_id"],
            "branch_id": pack["branch_id"],
            "task_role": pack["task_role"],
            "target_episode_order": pack["target_episode_order"],
            "perspective": pack["perspective"],
            "checkpoint": {"commit_sequence": pack["checkpoint_sequence"],
                           "content_hash": pack["checkpoint_hash"]},
            "claim_refs": pack["claim_refs"],
            "asset_refs": pack["asset_refs"],
            "unresolved_conflicts": pack["unresolved_conflicts"],
            "prohibited_assumptions": pack["prohibited_assumptions"],
            "token_budget": pack["token_budget"],
            "tokens_used": pack["tokens_used"],
            "source_watermark": pack["source_watermark"],
            "content_hash": pack["content_hash"],
        }

    def _render_row(self, row) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "context_pack_id": row["context_pack_id"],
            "revision": row["revision"],
            "tenant_id": row["tenant_id"],
            "series_id": row["series_id"],
            "branch_id": row["branch_id"],
            "task_role": row["task_role"],
            "target_episode_order": row["target_episode_order"],
            "perspective": row["perspective"],
            "perspective_character_ids": json.loads(
                row["perspective_character_ids"]),
            "checkpoint": {"commit_sequence": row["checkpoint_sequence"],
                           "content_hash": row["checkpoint_hash"]},
            "claim_refs": json.loads(row["claim_refs"]),
            "asset_refs": json.loads(row["asset_refs"]),
            "unresolved_conflicts": json.loads(row["unresolved_conflicts"]),
            "prohibited_assumptions": json.loads(row["prohibited_assumptions"]),
            "episode_blocks": json.loads(row["episode_blocks"]),
            "budget_breakdown": json.loads(row["budget_breakdown"]),
            "token_budget": row["token_budget"],
            "tokens_used": row["tokens_used"],
            "source_watermark": row["source_watermark"],
            "candidate": bool(row["candidate"]),
            "pack_for_delivery": bool(row["pack_for_delivery"]),
            "stale": bool(row["stale"]),
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
        }

    def _render_assumption_row(self, row) -> dict:
        return {
            "assumption_id": row["assumption_id"],
            "text": row["text"],
            "rule_kind": row["rule_kind"],
            "severity": row["severity"],
            "basis": json.loads(row["basis"]),
            "claim_refs": json.loads(row["claim_refs"]),
            "entity_ids": json.loads(row["entity_ids"]),
            "generated_by": row["generated_by"],
        }


# §7 的行是**必含种类的下限**，不是排他集：§4.1 说「硬事实全进」，§10.1 的
# E08-1 也要求 writer 包里出现 `entity_state` 的持有者。所以实际查询种类是
# `HARD_KINDS ∪ 该 role 额外强调的种类`。
ROLE_EXTRA_KINDS = {
    "writer": (),
    "storyboard": ("story_event",),
    "prompt": (),
    "qc": (),
    "continuity": (),
    "preview": (),
    "review": (),
}


def _placeholder_hash(claim: dict) -> str:
    """上游没给 `content_hash` 时的契约占位。

    这不是"编一个 hash" —— 它是 `claim_id + revision` 的确定性函数，只保证契约字段
    形状合法（64-hex），不声称是内容哈希。缺口在 `CONTRACT_GAPS` 里逐条报出。
    选它而不是报错，是因为 S16 目前**一定**不返回该字段：让每次调用都失败会
    把"契约缺口"变成"服务不可用"。
    """
    return _sha256_hex(f"unavailable:{claim.get('claim_id')}:"
                       f"{claim.get('revision')}")
