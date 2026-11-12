"""S16 业务内核：正史事实的唯一权威。

三条不可让步的性质，代码里都能指到具体行：

1. **没有证据就没有事实**：`claim_revision` 的 `CHECK` 挡住无 manifest 的 accepted 行；
   候选住在 `claim_candidate`，结构上不参与查询。
2. **三轴正交**：`commit_sequence`（系统何时知道）/ `clock_id + tick`（剧情何时成立）/
   `audience_reveal_episode_order`（第几集揭示）分别过滤，任何两轴都不能冒充第三轴。
   这里没有一处用 `MAX(revision)` 或 `created_at` 回答"当前是什么"。
3. **不越权**：只发布失效*种子*（`changed_subject_ids` + `affected_story_window`）与
   失效下界 `B(C)`；闭包归 S09，ChangePlan 归 S07。`commit_manifest_binding` 一旦写下
   就不改，已交付集不会因为后续提交而悄悄换 checkpoint。

提交前实时复查（§3.6）走注入的 `manifest_authority`。**没有注入就是不放行**——宁可
checkpoint 不推进，也不凭"事件说它已接受"写事实。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .predicates import CARDINALITY_ONE, KIND_MIN_CONFIDENCE, PREDICATES
from .schema import SCHEMA

SOURCE = "s16.story-memory"

BRANCH_KINDS = ("canon", "experiment", "merge_preview")
COMMIT_KINDS = ("extract", "supersede", "revert", "merge", "branch_init", "repair")
# 只有这两类可以不经人自动合并（§4.2 末段）。
AUTO_MERGE_KINDS = ("BENIGN_ADD",)
REF_FIELDS = ("service_id", "id", "revision", "content_hash")
# R1 认的是 ref 的四个核验字段；R6 额外要求至少一条带 `span`（剧本行 / 镜头 id），
# 所以 span 是合法附加项，不参与 manifest 比对。
REF_OPTIONAL = ("span",)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


class Transaction:
    """显式事务。`isolation_level=None` 下 `with conn` 不开启事务，必须手写。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ---- checkpoint 链 -------------------------------------------------------

def genesis_checkpoint(series_id: str, branch_id: str) -> str:
    return _sha256_hex(f"memcp/v2/genesis\n{series_id}\n{branch_id}")


def checkpoint_hash(series_id: str, branch_id: str, seq: int,
                    previous_hex: str, changed: list[dict]) -> str:
    """§1.3：只对本次增量取哈希。

    `changed` 是本服务发出的 service ref（键是 `id`）；也接受 `claim_id` 写法，
    免得调用方为了算链再转一次形状。
    """
    lines = sorted(f"{c.get('claim_id') or c['id']}\t{c['revision']}\t"
                   f"{c['content_hash']}" for c in changed)
    material = "\n".join([
        "memcp/v2", series_id, branch_id, str(seq), previous_hex, *lines,
    ])
    return _sha256_hex(material)


def content_hash_of(revision_payload: dict) -> str:
    return _sha256_hex(_jcs(revision_payload))


def _superseded_revisions(conn, tenant_id, series_id, claim_id,
                          revision) -> set[int]:
    """该 revision 的 `supersedes` 点名的目标 revision 编号集合。

    §4.2 只允许「显式带 supersedes 证据的 VALUE_CONFLICT」自动合并；
    证据存在这里，判定就不能靠猜。
    """
    row = conn.execute(
        "SELECT supersedes FROM claim_revision WHERE tenant_id=? AND series_id=?"
        " AND claim_id=? AND revision=?",
        (tenant_id, series_id, claim_id, revision)).fetchone()
    if row is None or not row["supersedes"]:
        return set()
    try:
        refs = json.loads(row["supersedes"])
    except (TypeError, ValueError):
        return set()
    return {int(r["revision"]) for r in refs
            if isinstance(r, dict) and r.get("revision") is not None}


# ---- 区间代数 ------------------------------------------------------------

def _is_participants_change(kind: str, fact_jcs: str | None) -> bool:
    """这次 revision 差的是不是 `story_event` 的参与者名单。"""
    if kind != "story_event" or not fact_jcs:
        return False
    try:
        return json.loads(fact_jcs).get("predicate") == "participants"
    except (TypeError, ValueError):
        return False


def _overlaps(a_from: int, a_to: int | None, b_from: int, b_to: int | None) -> bool:
    """半开区间 `[from, to)`，`None` 表示 ∞。"""
    a_end = a_to if a_to is not None else 1 << 62
    b_end = b_to if b_to is not None else 1 << 62
    return a_from < b_end and b_from < a_end


class StoryMemoryService:
    def __init__(self, conn: sqlite3.Connection, *, clock=_now,
                 manifest_authority=None, kill_switch=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.outbox = Outbox(conn)
        self.clock = clock
        # 生产接线是 S08 的 accepted-pointer 查询与 S01/S02 的 kill switch 判定。
        # 未接线时一律不放行（§3.6），而不是默认通过。
        self.manifest_authority = manifest_authority
        self.kill_switch = kill_switch

    # ---- 基础 ------------------------------------------------------------

    def _now_ts(self) -> str:
        return self.clock() if isinstance(self.clock(), str) else _now()

    def _branch(self, tenant_id: str, series_id: str, branch_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM canon_branch WHERE tenant_id=? AND series_id=? AND branch_id=?",
            (tenant_id, series_id, branch_id)).fetchone()
        if row is None:
            # E28：不泄漏存在性。
            raise ServiceError(404, "NOT_FOUND",
                               f"series {series_id!r} branch {branch_id!r} not found")
        return row

    def _ancestry(self, tenant_id: str, series_id: str, branch_id: str) -> dict[str, int]:
        return {r["ancestor_branch_id"]: r["depth"] for r in self.conn.execute(
            "SELECT ancestor_branch_id, depth FROM branch_ancestry "
            "WHERE tenant_id=? AND series_id=? AND branch_id=?",
            (tenant_id, series_id, branch_id))}

    def _visible_ceiling(self, tenant_id: str, series_id: str, branch_id: str,
                         ancestry: dict[str, int]) -> dict[str, tuple[int, int]]:
        """每个祖先分支在 B 上的可见上界：自身 = head，祖先 = fork_commit_sequence。"""
        out: dict[str, tuple[int, int]] = {}
        head = self._branch(tenant_id, series_id, branch_id)
        out[branch_id] = (head["head_commit_sequence"], ancestry.get(branch_id, 0))
        for anc, depth in ancestry.items():
            if anc == branch_id:
                continue
            row = self._branch(tenant_id, series_id, anc)
            ceiling = (row["fork_commit_sequence"]
                       if row["fork_commit_sequence"] is not None
                       else row["head_commit_sequence"])
            out[anc] = (ceiling, depth)
        return out

    # ---- 注册：时钟 / 集窗口 / 实体 ---------------------------------------

    def register_clock(self, tenant_id: str, series_id: str, body: dict) -> dict:
        for key in ("clock_id", "clock_kind", "tick_min", "tick_max"):
            if key not in body:
                raise ServiceError(400, "MALFORMED_REQUEST", f"{key} is required")
        if body["clock_kind"] not in ("main", "flashback", "dream", "side"):
            raise ServiceError(400, "MALFORMED_REQUEST", "bad clock_kind")
        if body["tick_max"] < body["tick_min"]:
            raise ServiceError(400, "MALFORMED_REQUEST", "tick_max < tick_min")
        self.conn.execute(
            "INSERT INTO story_clock (tenant_id, series_id, clock_id, clock_kind,"
            " parent_clock_id, origin_episode_id, tick_min, tick_max)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, series_id, clock_id) DO UPDATE SET"
            " clock_kind=excluded.clock_kind, parent_clock_id=excluded.parent_clock_id,"
            " tick_min=excluded.tick_min, tick_max=excluded.tick_max",
            (tenant_id, series_id, body["clock_id"], body["clock_kind"],
             body.get("parent_clock_id"), body.get("origin_episode_id"),
             body["tick_min"], body["tick_max"]))
        return {"clock_id": body["clock_id"]}

    def register_episode_window(self, tenant_id: str, series_id: str, body: dict) -> dict:
        for key in ("episode_id", "episode_order", "clock_id", "tick_start", "tick_end"):
            if key not in body:
                raise ServiceError(400, "MALFORMED_REQUEST", f"{key} is required")
        if body["tick_end"] <= body["tick_start"]:
            raise ServiceError(400, "MALFORMED_REQUEST", "tick_end must exceed tick_start")
        clock = self.conn.execute(
            "SELECT 1 FROM story_clock WHERE tenant_id=? AND series_id=? AND clock_id=?",
            (tenant_id, series_id, body["clock_id"])).fetchone()
        if clock is None:
            raise ServiceError(422, "CLOCK_OUT_OF_RANGE", "clock_id is not registered")
        self.conn.execute(
            "INSERT INTO episode_story_window (tenant_id, series_id, episode_id,"
            " episode_order, clock_id, tick_start, tick_end) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, series_id, body["episode_id"], body["episode_order"],
             body["clock_id"], body["tick_start"], body["tick_end"]))
        for entity_id in body.get("touched_entities") or []:
            self.conn.execute(
                "INSERT OR IGNORE INTO episode_entity_touch"
                " (tenant_id, series_id, episode_id, episode_order, entity_id)"
                " VALUES (?,?,?,?,?)",
                (tenant_id, series_id, body["episode_id"], body["episode_order"],
                 entity_id))
        return {"episode_id": body["episode_id"]}

    def register_entity(self, tenant_id: str, series_id: str, body: dict) -> dict:
        for key in ("entity_id", "entity_type", "display_name"):
            if key not in body:
                raise ServiceError(400, "MALFORMED_REQUEST", f"{key} is required")
        status = body.get("status", "active")
        if status not in ("active", "deprecated", "merged_into"):
            raise ServiceError(400, "MALFORMED_REQUEST", "bad entity status")
        self.conn.execute(
            "INSERT INTO entity_registry (tenant_id, series_id, entity_id, entity_type,"
            " display_name, owner_service, status) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, series_id, entity_id) DO UPDATE SET"
            " entity_type=excluded.entity_type, display_name=excluded.display_name,"
            " owner_service=excluded.owner_service, status=excluded.status",
            (tenant_id, series_id, body["entity_id"], body["entity_type"],
             body["display_name"], body.get("owner_service"), status))
        for alias in body.get("aliases") or []:
            self.conn.execute(
                "INSERT INTO entity_alias (tenant_id, series_id, alias_text, entity_id,"
                " confidence, source) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, series_id, alias_text, entity_id) DO UPDATE SET"
                " confidence=excluded.confidence, source=excluded.source",
                (tenant_id, series_id, alias, body["entity_id"],
                 float(body.get("alias_confidence", 1.0)), "registry"))
        return {"entity_id": body["entity_id"]}

    def _resolve_entity(self, tenant_id: str, series_id: str, token: str) -> str | None:
        """先按 canonical id，再按别名；别名命中多于一个就当作解析失败（R3/R7）。"""
        row = self.conn.execute(
            "SELECT entity_id FROM entity_registry WHERE tenant_id=? AND series_id=?"
            " AND entity_id=? AND status='active'", (tenant_id, series_id, token)).fetchone()
        if row is not None:
            return row["entity_id"]
        hits = [r["entity_id"] for r in self.conn.execute(
            "SELECT DISTINCT entity_id FROM entity_alias WHERE tenant_id=? AND series_id=?"
            " AND alias_text=?", (tenant_id, series_id, token))]
        return hits[0] if len(hits) == 1 else None

    # ---- 分支 ------------------------------------------------------------

    def create_series(self, tenant_id: str, series_id: str, branch_id: str = "canon") -> dict:
        """建正史分支。genesis checkpoint 在 seq=0，不产生提交。"""
        ts = self._now_ts()
        with Transaction(self.conn):
            exists = self.conn.execute(
                "SELECT 1 FROM canon_branch WHERE tenant_id=? AND series_id=?",
                (tenant_id, series_id)).fetchone()
            if exists is None:
                genesis = genesis_checkpoint(series_id, branch_id)
                self.conn.execute(
                    "INSERT INTO canon_branch (tenant_id, series_id, branch_id,"
                    " parent_branch_id, fork_commit_sequence, fork_checkpoint_hash,"
                    " branch_kind, status, merge_status, head_commit_sequence,"
                    " head_checkpoint_hash, created_at)"
                    " VALUES (?,?,?,NULL,NULL,NULL,'canon','active','none',0,?,?)",
                    (tenant_id, series_id, branch_id, genesis, ts))
                self.conn.execute(
                    "INSERT INTO branch_ancestry (tenant_id, series_id, branch_id,"
                    " ancestor_branch_id, depth) VALUES (?,?,?,?,0)",
                    (tenant_id, series_id, branch_id, branch_id))
            else:
                genesis = self._branch(tenant_id, series_id,
                                       branch_id)["head_checkpoint_hash"]
        return {"branch_id": branch_id, "head_commit_sequence": 0,
                "head_checkpoint_hash": genesis}

    def fork_branch(self, tenant_id: str, series_id: str, body: dict) -> dict:
        branch_id = body.get("branch_id")
        from_branch = body.get("from_branch_id")
        if not branch_id or not from_branch:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "branch_id and from_branch_id are required")
        kind = body.get("branch_kind", "experiment")
        if kind not in BRANCH_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST", "bad branch_kind")

        with Transaction(self.conn):
            parent = self._branch(tenant_id, series_id, from_branch)  # 404 在锁内也一样
            seq = body.get("from_commit_sequence")
            if seq is None:
                seq = parent["head_commit_sequence"]
            if seq > parent["head_commit_sequence"]:
                raise ServiceError(409, "CHECKPOINT_CONFLICT",
                                   "from_commit_sequence is ahead of parent head",
                                   details={"current_checkpoint":
                                            parent["head_commit_sequence"]})
            fork_hash = self._checkpoint_at(tenant_id, series_id, from_branch, seq)
            if self.conn.execute(
                    "SELECT 1 FROM canon_branch WHERE tenant_id=? AND series_id=? AND"
                    " branch_id=?", (tenant_id, series_id, branch_id)).fetchone():
                raise ServiceError(409, "BRANCH_EXISTS", f"branch {branch_id!r} exists")

            ts = self._now_ts()
            # 新分支不复制任何 claim 行：可见性由 ancestry + 上界算出来（§1.2/§1.11）。
            self.conn.execute(
                "INSERT INTO canon_branch (tenant_id, series_id, branch_id,"
                " parent_branch_id, fork_commit_sequence, fork_checkpoint_hash,"
                " branch_kind, status, merge_status, head_commit_sequence,"
                " head_checkpoint_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?, 'active', 'none', 0, ?, ?)",
                (tenant_id, series_id, branch_id, from_branch, seq, fork_hash,
                 kind, fork_hash, ts))
            for anc, depth in self._ancestry(tenant_id, series_id, from_branch).items():
                self.conn.execute(
                    "INSERT INTO branch_ancestry (tenant_id, series_id, branch_id,"
                    " ancestor_branch_id, depth) VALUES (?,?,?,?,?)",
                    (tenant_id, series_id, branch_id, anc, depth + 1))
            new_seq = 1
            new_hash = checkpoint_hash(series_id, branch_id, new_seq, fork_hash, [])
            self.conn.execute(
                "INSERT INTO memory_commit (tenant_id, series_id, branch_id,"
                " commit_sequence, previous_sequence, previous_checkpoint_hash,"
                " checkpoint_hash, commit_kind, source_manifest_ref, merge_source_refs,"
                " revert_of_sequence, extraction_batch_id, command_id, actor_principal,"
                " recorded_at, claim_count)"
                " VALUES (?,?,?,?,?,?,?, 'branch_init', NULL, NULL, NULL, NULL, ?, ?, ?, 0)",
                (tenant_id, series_id, branch_id, new_seq, 0, fork_hash, new_hash,
                 body.get("command_id") or _new_id("fork_"),
                 body.get("actor_principal", "s16.story-memory"), ts))
            self.conn.execute(
                "UPDATE canon_branch SET head_commit_sequence=?, head_checkpoint_hash=?"
                " WHERE tenant_id=? AND series_id=? AND branch_id=?",
                (new_seq, new_hash, tenant_id, series_id, branch_id))
        return {"branch_id": branch_id, "fork_commit_sequence": seq,
                "head_commit_sequence": new_seq, "head_checkpoint_hash": new_hash}

    def _checkpoint_at(self, tenant_id: str, series_id: str,
                       branch_id: str, seq: int) -> str:
        if seq == 0:
            return genesis_checkpoint(series_id, branch_id)
        row = self.conn.execute(
            "SELECT checkpoint_hash FROM memory_commit WHERE tenant_id=? AND series_id=?"
            " AND branch_id=? AND commit_sequence=?",
            (tenant_id, series_id, branch_id, seq)).fetchone()
        if row is None:
            raise ServiceError(409, "CHECKPOINT_CONFLICT",
                               f"commit {seq} does not exist on {branch_id}",
                               details={"current_checkpoint": seq - 1})
        return row["checkpoint_hash"]

    # ---- 候选：R1–R8 -----------------------------------------------------

    def submit_candidates(self, tenant_id: str, series_id: str, body: dict) -> dict:
        branch_id = body.get("branch_id")
        batch_id = body.get("batch_id")
        candidates = body.get("candidates")
        source_manifest_ref = body.get("source_manifest_ref")
        if not branch_id or not batch_id or not isinstance(candidates, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "branch_id, batch_id and candidates[] are required")
        if not isinstance(source_manifest_ref, dict):
            raise ServiceError(400, "MALFORMED_REQUEST", "source_manifest_ref is required")
        self._branch(tenant_id, series_id, branch_id)

        ts = self._now_ts()
        results = []
        with Transaction(self.conn):
            for c in candidates:
                errors = self._validate_candidate(tenant_id, series_id, c,
                                                 source_manifest_ref)
                state = "rejected" if errors else "structurally_valid"
                if not errors:
                    errors = self._precheck_conflicts(tenant_id, series_id, branch_id, c)
                    if errors:
                        state = "rejected" if errors[0]["code"] in (
                            "CLAIM_CONFLICT", "BAD_INTERVAL") else "conflict"
                candidate_id = c.get("candidate_id") or _new_id("cand_")
                self.conn.execute(
                    "INSERT INTO claim_candidate (tenant_id, series_id, branch_id,"
                    " batch_id, candidate_id, proposed_claim_id, kind, fact, clock_id,"
                    " valid_from_tick, valid_to_tick, epistemic_scope, source_refs,"
                    " supersedes_claim_refs, proposition_claim_id, confidence, extracted_by,"
                    " extraction_manifest_ref,"
                    " validation_state, validation_errors, created_at, expires_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(tenant_id, series_id, branch_id, batch_id, candidate_id)"
                    " DO UPDATE SET validation_state=excluded.validation_state,"
                    " validation_errors=excluded.validation_errors",
                    (tenant_id, series_id, branch_id, batch_id, candidate_id,
                     c.get("proposed_claim_id"), c.get("kind"),
                     _jcs(c.get("fact") or {}), (c.get("story_validity") or {}).get("clock_id"),
                     (c.get("story_validity") or {}).get("valid_from_tick"),
                     (c.get("story_validity") or {}).get("valid_to_tick"),
                     _jcs(c["epistemic_scope"]) if c.get("epistemic_scope") else None,
                     _jcs(c.get("source_refs") or []),
                     _jcs(c.get("supersedes_claim_refs") or []),
                     c.get("proposition_claim_id"),
                     float(c.get("confidence", 0.0)),
                     c.get("extracted_by", "unknown"),
                     _jcs(source_manifest_ref), state, _jcs(errors), ts,
                     body.get("expires_at") or ts))
                # 抽取器无权决定提交序号，也无权指定租户：body 里带了就当没看见。
                if "tenant_id" in c and c["tenant_id"] != tenant_id:
                    self.conn.execute(
                        "UPDATE claim_candidate SET validation_errors=? WHERE tenant_id=?"
                        " AND series_id=? AND branch_id=? AND batch_id=?"
                        " AND candidate_id=?",
                        (_jcs(errors + [{"code": "TENANT_FIELD_IGNORED",
                                         "severity": "warning"}]),
                         tenant_id, series_id, branch_id, batch_id, candidate_id))
                results.append({"candidate_id": candidate_id, "state": state,
                                "errors": errors})
        return {"batch_id": batch_id, "candidates": results}

    def _validate_candidate(self, tenant_id: str, series_id: str, c: dict,
                            source_manifest_ref: dict) -> list[dict]:
        errs: list[dict] = []

        # R1 来源可核验：必须指向本 manifest 的 source_revisions 且 hash 一致。
        allowed = {(r.get("id"), r.get("revision"), r.get("content_hash"))
                   for r in source_manifest_ref.get("source_revisions") or []}
        refs = c.get("source_refs") or []
        if not refs:
            errs.append({"code": "INSUFFICIENT_EVIDENCE",
                         "detail": "source_refs must not be empty"})
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) - set(REF_FIELDS) - set(REF_OPTIONAL):
                errs.append({"code": "SOURCE_NOT_IN_MANIFEST",
                             "detail": "source_ref must be a service ref"})
                continue
            if (ref.get("id"), ref.get("revision"),
                    ref.get("content_hash")) not in allowed:
                errs.append({"code": "SOURCE_NOT_IN_MANIFEST",
                             "detail": f"{ref.get('id')}@{ref.get('revision')}"})

        fact = c.get("fact") or {}
        predicate = fact.get("predicate")
        # R2 谓词已注册（谓词表是代码常量，抽取器造不出来）。
        spec = PREDICATES.get(predicate)
        if spec is None:
            errs.append({"code": "UNKNOWN_PREDICATE", "detail": str(predicate)})
        else:
            value = fact.get("value")
            if value is None and not spec.get("nullable"):
                errs.append({"code": "VALUE_SCHEMA", "detail": "value is null"})
            if spec.get("value_kind") == "entity_id" and value is not None:
                if not isinstance(value, str) or not self._resolve_entity(
                        tenant_id, series_id, value):
                    errs.append({"code": "VALUE_SCHEMA",
                                 "detail": "value must resolve to an entity"})
            if spec.get("value_kind") == "claim_ref" and value is not None:
                if not isinstance(value, str) or not self.conn.execute(
                        "SELECT 1 FROM claim WHERE tenant_id=? AND series_id=?"
                        " AND claim_id=?",
                        (tenant_id, series_id, value)).fetchone():
                    errs.append({"code": "VALUE_SCHEMA",
                                 "detail": "value must be an existing claim id"})
            if spec.get("value_kind") == "enum" and value is not None:
                candidate = value.get("state") if isinstance(value, dict) else value
                if candidate not in spec["values"]:
                    errs.append({"code": "VALUE_SCHEMA",
                                 "detail": f"{candidate!r} not in {spec['values']}"})
            if spec.get("value_kind") == "number" and value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errs.append({"code": "VALUE_SCHEMA",
                                 "detail": "value must be a number"})

        # R3 主体可解析（canonical id 或唯一别名）。
        subject = fact.get("subject_id")
        if not subject or not self._resolve_entity(tenant_id, series_id, subject):
            errs.append({"code": "UNRESOLVED_ENTITY", "detail": str(subject)})

        validity = c.get("story_validity") or {}
        clock_id = validity.get("clock_id")
        # R4 时钟合法。
        clock = self.conn.execute(
            "SELECT tick_min, tick_max FROM story_clock WHERE tenant_id=? AND"
            " series_id=? AND clock_id=?", (tenant_id, series_id, clock_id)).fetchone()
        vfrom = validity.get("valid_from_tick")
        if clock is None:
            errs.append({"code": "CLOCK_OUT_OF_RANGE", "detail": str(clock_id)})
        elif not isinstance(vfrom, int) or not (
                clock["tick_min"] <= vfrom <= clock["tick_max"]):
            errs.append({"code": "CLOCK_OUT_OF_RANGE", "detail": str(vfrom)})

        # R5 区间合法。
        vto = validity.get("valid_to_tick")
        if vto is not None and (not isinstance(vfrom, int) or vto <= vfrom):
            errs.append({"code": "BAD_INTERVAL", "detail": f"{vfrom}..{vto}"})

        # R6 有证据跨度（至少一条带 span 的引用）且置信度过该 kind 的下限。
        kind = c.get("kind")
        floor = KIND_MIN_CONFIDENCE.get(kind, 0.0)
        if float(c.get("confidence", 0.0)) < floor:
            errs.append({"code": "INSUFFICIENT_EVIDENCE",
                         "detail": f"confidence below {floor} for {kind}"})
        if not any(isinstance(r, dict) and r.get("span") for r in refs):
            errs.append({"code": "INSUFFICIENT_EVIDENCE",
                         "detail": "no source_ref carries a span"})

        # R7 无幻觉 ID。
        for other in (c.get("references") or []):
            if not self._resolve_entity(tenant_id, series_id, other):
                errs.append({"code": "DANGLING_REF", "detail": str(other)})

        # R8 知情完整。
        if kind == "knowledge_state":
            prop = c.get("proposition_claim_id")
            if not prop or not self.conn.execute(
                    "SELECT 1 FROM claim WHERE tenant_id=? AND series_id=? AND"
                    " claim_id=?", (tenant_id, series_id, prop)).fetchone():
                errs.append({"code": "UNRESOLVED_PROPOSITION", "detail": str(prop)})
            scope = c.get("epistemic_scope") or {}
            if not scope.get("knower_ids"):
                errs.append({"code": "UNRESOLVED_PROPOSITION",
                             "detail": "knowledge_state needs knower_ids"})
        return errs

    # ---- 冲突预检 C1–C6 --------------------------------------------------

    def _precheck_conflicts(self, tenant_id: str, series_id: str,
                            branch_id: str, c: dict) -> list[dict]:
        fact = c.get("fact") or {}
        spec = PREDICATES.get(fact.get("predicate")) or {}
        validity = c.get("story_validity") or {}
        subject = fact.get("subject_id")
        claim_id = c.get("proposed_claim_id")
        supersedes = c.get("supersedes_claim_refs") or []

        rows = self._effective_rows(tenant_id, series_id, branch_id)
        errs: list[dict] = []

        for row in rows:
            same_claim = claim_id is not None and row["claim_id"] == claim_id
            same_slot = row["subject_key"] == self._slot_key(fact)
            if not (same_claim or same_slot):
                continue
            if not _overlaps(validity.get("valid_from_tick", 0),
                             validity.get("valid_to_tick"),
                             row["valid_from_tick"], row["valid_to_tick"]):
                continue
            if row["value_jcs"] == _jcs(fact.get("value")):
                continue
            # C1 区间值冲突：必须显式带 supersedes 指向被替代 revision。
            if same_claim:
                points_at = {r.get("id") for r in supersedes
                             if r.get("service_id") == "S16"}
                if row["claim_id"] not in points_at:
                    errs.append({"code": "CLAIM_CONFLICT",
                                 "detail": f"{row['claim_id']}@{row['revision']}"})
                continue
            # C3 唯一性违例（基数=1 的谓词出现两个不同值且区间重叠）。
            if spec.get("cardinality") == 1 and not supersedes:
                errs.append({"code": "CLAIM_CONFLICT",
                             "detail": f"cardinality=1 slot held by "
                                       f"{row['claim_id']}@{row['revision']}"})

        errs.extend(self._check_knowledge(tenant_id, series_id, branch_id, c))
        errs.extend(self._check_state_lifecycle(tenant_id, series_id, branch_id, c))
        errs.extend(self._check_thread(tenant_id, series_id, branch_id, c))
        errs.extend(self._check_causality(tenant_id, series_id, branch_id, c))
        return errs

    @staticmethod
    def _slot_key(fact: dict) -> str:
        return f"{fact.get('subject_id')}:{fact.get('predicate')}"

    def _check_knowledge(self, tenant_id, series_id, branch_id, c) -> list[dict]:
        """C2：同 (knower, proposition) 同区间不能既 knows 又 unaware。"""
        if c.get("kind") != "knowledge_state":
            return []
        scope = c.get("epistemic_scope") or {}
        belief = scope.get("belief_type")
        if belief not in ("knows", "believes", "unaware", "uncertain", "fact"):
            return []
        prop = c.get("proposition_claim_id")
        validity = c.get("story_validity") or {}
        opposing = {"knows": {"unaware"}, "unaware": {"knows", "believes"}}.get(belief, set())
        if not opposing:
            return []
        for knower in scope.get("knower_ids") or []:
            # 直接查投影，它是可重建的派生视图，不是第二权威。
            for row in self.conn.execute(
                    "SELECT k.claim_id, k.revision, k.belief_type, k.clock_id,"
                    " k.valid_from_tick, k.valid_to_tick FROM knowledge_state k"
                    " WHERE k.tenant_id=? AND k.series_id=? AND k.knower_id=?"
                    " AND k.proposition_claim_id=?",
                    (tenant_id, series_id, knower, prop)):
                if row["belief_type"] in opposing and _overlaps(
                        validity.get("valid_from_tick", 0), validity.get("valid_to_tick"),
                        row["valid_from_tick"], row["valid_to_tick"]):
                    return [{"code": "EPISTEMIC_CONFLICT",
                             "detail": f"{knower} vs {row['claim_id']}@{row['revision']}"}]
        return []

    def _check_state_lifecycle(self, tenant_id, series_id, branch_id, c) -> list[dict]:
        """C4：`dead`/`destroyed` 之后重新出现且没有 resurrection 事件 → 拒。"""
        fact = c.get("fact") or {}
        if fact.get("predicate") not in ("life_state", "existence"):
            return []
        value = fact.get("value")
        value = value.get("state") if isinstance(value, dict) else value
        if value in ("dead", "destroyed", "absent"):
            return []
        validity = c.get("story_validity") or {}
        vfrom = validity.get("valid_from_tick", 0)
        for row in self._effective_rows(tenant_id, series_id, branch_id):
            if row["subject_key"] != self._slot_key(fact):
                continue
            val = json.loads(row["value_jcs"])
            state = val.get("state") if isinstance(val, dict) else val
            if state not in ("dead", "destroyed", "absent"):
                continue
            if row["valid_to_tick"] is None or row["valid_to_tick"] > vfrom:
                revived = any(
                    str(e.get("predicate")) == "resurrection"
                    for e in c.get("causal_events") or [])
                if not revived:
                    return [{"code": "LIFECYCLE_CONFLICT",
                             "detail": f"{fact.get('subject_id')} was {state}"}]
        return []

    def _check_thread(self, tenant_id, series_id, branch_id, c) -> list[dict]:
        """C5：伏笔已在区间内 resolved，再来一条 resolved → 拒。"""
        if c.get("kind") != "plot_thread":
            return []
        fact = c.get("fact") or {}
        if fact.get("predicate") != "thread_state":
            return []
        value = fact.get("value")
        value = value.get("state") if isinstance(value, dict) else value
        if value != "resolved":
            return []
        validity = c.get("story_validity") or {}
        for row in self._effective_rows(tenant_id, series_id, branch_id):
            if row["subject_key"] != self._slot_key(fact):
                continue
            val = json.loads(row["value_jcs"])
            state = val.get("state") if isinstance(val, dict) else val
            if state == "resolved" and _overlaps(
                    validity.get("valid_from_tick", 0), validity.get("valid_to_tick"),
                    row["valid_from_tick"], row["valid_to_tick"]):
                return [{"code": "THREAD_CONFLICT",
                         "detail": f"{row['claim_id']}@{row['revision']} already resolved"}]
        return []

    def _check_causality(self, tenant_id, series_id, branch_id, c) -> list[dict]:
        """C6：`causes` 指向的事件 tick 晚于本事件 → 拒。"""
        fact = c.get("fact") or {}
        if fact.get("predicate") != "causes":
            return []
        target = fact.get("value")
        if not isinstance(target, str):
            return []
        validity = c.get("story_validity") or {}
        # 被引用事件的时点要问本分支当前生效的那条，不是「全库该 claim 的最大
        # revision」—— 别的分支（含 merge_preview）上编号更大的 revision 既不是
        # 本分支当前的答案，也不该能让本分支的校验换个结果。
        row = self.conn.execute(
            "SELECT e.valid_from_tick FROM canon_effective_claim e"
            " WHERE e.tenant_id=? AND e.series_id=? AND e.branch_id=?"
            "   AND e.claim_id=?",
            (tenant_id, series_id, branch_id, target)).fetchone()
        if row is not None and row["valid_from_tick"] > validity.get("valid_from_tick", 0):
            return [{"code": "CAUSAL_INVERSION",
                     "detail": f"{target} happens later"}]
        return []

    def _effective_rows(self, tenant_id: str, series_id: str,
                        branch_id: str) -> list[dict]:
        """本分支解析出的生效 revision，`value_jcs` 在 Python 侧规范化。

        SQLite 没有 jsonb 的规范化文本，所以冲突比较统一走 `_jcs(value)`，避免
        `1` 与 `1.0` 这类写法被当成两个值。分支解析规则只看本分支的
        `canon_effective_claim`：祖先的覆盖在提交时已经解析进来，读路径不再 JOIN
        祖先，也就不会在查询时悄悄换权威。
        """
        out = []
        for row in self.conn.execute(
                "SELECT e.claim_id, e.revision, e.kind, e.clock_id, e.valid_from_tick,"
                " e.valid_to_tick, r.fact AS fact_jcs,"
                " json_extract(r.fact, '$.subject_id') || ':' ||"
                " json_extract(r.fact, '$.predicate') AS subject_key"
                " FROM canon_effective_claim e JOIN claim_revision r"
                "   ON r.tenant_id=e.tenant_id AND r.series_id=e.series_id"
                "  AND r.claim_id=e.claim_id AND r.revision=e.revision"
                " WHERE e.tenant_id=? AND e.series_id=? AND e.branch_id=?",
                (tenant_id, series_id, branch_id)).fetchall():
            fact = json.loads(row["fact_jcs"])
            out.append({**dict(row), "value_jcs": _jcs(fact.get("value"))})
        return out

    # ---- 原子提交 --------------------------------------------------------

    def commit(self, tenant_id: str, series_id: str, body: dict) -> dict:
        branch_id = body.get("branch_id")
        expected = body.get("expected_checkpoint")
        commit_kind = body.get("commit_kind", "extract")
        if not branch_id or expected is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "branch_id and expected_checkpoint are required")
        if commit_kind not in COMMIT_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST", "bad commit_kind")
        manifest_ref = body.get("source_manifest_ref")

        # §3.6 实时复查发生在任何写入之前，且失败要留审计。
        block = self._recheck(tenant_id, series_id, branch_id, commit_kind, manifest_ref)
        if block is not None:
            with Transaction(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO commit_blocked (tenant_id, series_id, branch_id,"
                    " command_id, reason, details, recorded_at) VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, series_id, branch_id,
                     body.get("command_id") or _new_id("cmd_"),
                     block["code"], _jcs(block.get("details") or {}), self._now_ts()))
            raise ServiceError(409, "BLOCKED", block["message"],
                               details={"reason": block["code"], **block.get("details", {})})

        if commit_kind == "revert":
            return self._revert(tenant_id, series_id, body)
        if commit_kind == "merge":
            return self._merge(tenant_id, series_id, body)

        with Transaction(self.conn):
            head = self._branch(tenant_id, series_id, branch_id)
            self._cas(head, expected)
            seq = head["head_commit_sequence"] + 1

            revisions = body.get("claim_revisions")
            if revisions is None:
                revisions = self._promote(tenant_id, series_id, branch_id, body)
            accepted = [r for r in revisions if r.get("status", "accepted") == "accepted"]

            changed = []
            superseded_refs = []
            for rev in accepted:
                changed.append(self._write_revision(
                    tenant_id, series_id, branch_id, seq, rev, manifest_ref,
                    commit_kind, superseded_refs))
            new_hash = checkpoint_hash(series_id, branch_id, seq,
                                       head["head_checkpoint_hash"], changed)
            self._finish_commit(tenant_id, series_id, branch_id, seq, head,
                                new_hash, commit_kind, body, manifest_ref, changed,
                                superseded_refs, len(changed))
        self._publish(tenant_id, series_id, branch_id, seq, new_hash, commit_kind,
                      changed, superseded_refs, manifest_ref, body)
        return {"commit_sequence": seq, "checkpoint_hash": new_hash,
                "commit_kind": commit_kind, "claim_count": len(changed),
                "changed_claim_refs": changed,
                "superseded_claim_refs": superseded_refs}

    def _recheck(self, tenant_id, series_id, branch_id, commit_kind,
                 manifest_ref) -> dict | None:
        """四项实时复查（§3.6）。任何一项不能回答 = 不放行。"""
        branch = self._branch(tenant_id, series_id, branch_id)
        if branch["status"] == "frozen":
            return {"code": "BRANCH_FROZEN", "message": "branch is frozen"}
        if self.kill_switch is not None and self.kill_switch(tenant_id, series_id):
            return {"code": "KILL_SWITCH", "message": "kill switch is active"}
        if commit_kind in ("extract", "supersede"):
            if not isinstance(manifest_ref, dict):
                return {"code": "MANIFEST_REF_REQUIRED",
                        "message": "source_manifest_ref is required"}
            if self.manifest_authority is None:
                # 没有接线就不猜：事件说"已接受"不是放行依据。
                return {"code": "MANIFEST_AUTHORITY_UNCONFIGURED",
                        "message": "manifest authority is not wired"}
            verdict = self.manifest_authority(tenant_id, series_id, manifest_ref)
            if isinstance(verdict, dict) and verdict.get("accepted") is True:
                if (verdict.get("revision") is not None
                        and verdict["revision"] != manifest_ref.get("revision")):
                    return {"code": "MANIFEST_SUPERSEDED",
                            "message": "accepted pointer moved",
                            "details": {"accepted_revision": verdict["revision"]}}
                return None
            return {"code": "MANIFEST_NOT_ACCEPTED",
                    "message": "source manifest is not the accepted pointer"}
        return None

    @staticmethod
    def _cas(head: sqlite3.Row, expected: int) -> None:
        if head["head_commit_sequence"] != expected:
            raise ServiceError(409, "CHECKPOINT_CONFLICT",
                               "expected_checkpoint does not match head",
                               details={"current_checkpoint":
                                        head["head_commit_sequence"],
                                        "current_checkpoint_hash":
                                        head["head_checkpoint_hash"]})

    def _promote(self, tenant_id, series_id, branch_id, body) -> list[dict]:
        """从候选隔离区把 structurally_valid 的批次提升为提交内容。

        候选本身没有 commit_sequence，唯一的提升路径是这一处；`promoted` 态只在
        提交成功后由 `_write_revision` 回写。
        """
        batch_id = body.get("batch_id")
        if not batch_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "either claim_revisions or batch_id is required")
        rows = self.conn.execute(
            "SELECT * FROM claim_candidate WHERE tenant_id=? AND series_id=? AND"
            " branch_id=? AND batch_id=? AND validation_state='structurally_valid'",
            (tenant_id, series_id, branch_id, batch_id)).fetchall()
        if not rows:
            raise ServiceError(422, "NO_COMMITTABLE_CANDIDATES",
                               f"batch {batch_id!r} has no structurally_valid candidate")
        out = []
        for row in rows:
            fact = json.loads(row["fact"])
            scope = json.loads(row["epistemic_scope"]) if row["epistemic_scope"] else None
            out.append({
                "claim_id": row["proposed_claim_id"],
                "kind": row["kind"],
                "fact": fact,
                "story_validity": {"clock_id": row["clock_id"],
                                   "valid_from_tick": row["valid_from_tick"],
                                   "valid_to_tick": row["valid_to_tick"]},
                "epistemic_scope": scope,
                "source_refs": json.loads(row["source_refs"]),
                "supersedes_claim_refs": json.loads(row["supersedes_claim_refs"]),
                "confidence": row["confidence"],
                "proposition_claim_id": row["proposition_claim_id"],
                "references": ([fact["value"]]
                               if isinstance(fact.get("value"), str) else []),
                "candidate_ref": {"batch_id": row["batch_id"],
                                  "candidate_id": row["candidate_id"]},
            })
        return out

    def _write_revision(self, tenant_id, series_id, branch_id, seq, rev,
                        manifest_ref, commit_kind, superseded_out) -> dict:
        fact = rev["fact"]
        claim_id = rev.get("claim_id")
        if not claim_id:
            # 新谱系：稳定 ID 由 (kind, subject, predicate) 决定，跨 revision 不变。
            claim_id = "claim_" + _sha256_hex(
                f"{rev['kind']}\n{fact['subject_id']}\n{fact['predicate']}")[:20]
        existing = self.conn.execute(
            "SELECT created_sequence, origin_branch_id FROM claim WHERE tenant_id=?"
            " AND series_id=? AND claim_id=?",
            (tenant_id, series_id, claim_id)).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO claim (tenant_id, series_id, claim_id, kind, subject_id,"
                " predicate, origin_branch_id, created_sequence) VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, series_id, claim_id, rev["kind"], fact["subject_id"],
                 fact["predicate"], branch_id, seq))

        prev = self.conn.execute(
            "SELECT revision FROM claim_revision WHERE tenant_id=? AND series_id=?"
            " AND claim_id=? ORDER BY revision DESC LIMIT 1",
            (tenant_id, series_id, claim_id)).fetchone()
        revision = (prev["revision"] + 1) if prev else 1
        validity = rev.get("story_validity") or {}
        scope = rev.get("epistemic_scope") or {
            "perspective": "omniscient", "knower_ids": [], "belief_type": "fact",
            "audience_reveal_episode_order": 0,
        }
        ts = self._now_ts()
        payload = {
            "claim_id": claim_id, "revision": revision, "kind": rev["kind"],
            "fact": fact, "story_validity": validity,
            "source_refs": rev["source_refs"],
            "epistemic_scope": scope,
            "confidence": rev["confidence"],
        }
        digest = content_hash_of(payload)

        # 旧的 accepted revision 在本分支上被替代：置 superseded，但从不删除。
        for old in self.conn.execute(
                "SELECT revision, content_hash FROM claim_revision WHERE tenant_id=?"
                " AND series_id=? AND claim_id=? AND status='accepted'",
                (tenant_id, series_id, claim_id)).fetchall():
            self.conn.execute(
                "UPDATE claim_revision SET status='superseded' WHERE tenant_id=?"
                " AND series_id=? AND claim_id=? AND revision=?",
                (tenant_id, series_id, claim_id, old["revision"]))
            superseded_out.append({"service_id": "S16", "id": claim_id,
                                   "revision": old["revision"],
                                   "content_hash": old["content_hash"]})

        self.conn.execute(
            "INSERT INTO claim_revision (tenant_id, series_id, claim_id, revision,"
            " status, fact, clock_id, valid_from_tick, valid_to_tick, recorded_at,"
            " commit_sequence, branch_id, source_refs, accepted_manifest_ref,"
            " epistemic_scope, supersedes, confidence, review_state, content_hash)"
            " VALUES (?,?,?,?,'accepted',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, series_id, claim_id, revision, _jcs(fact),
             validity["clock_id"], validity["valid_from_tick"],
             validity.get("valid_to_tick"), ts, seq, branch_id,
             _jcs(rev["source_refs"]),
             _jcs(manifest_ref) if manifest_ref else None, _jcs(scope),
             _jcs(rev.get("supersedes_claim_refs") or []),
             float(rev["confidence"]),
             "auto" if commit_kind == "extract" else "human_accepted", digest))

        self._refresh_effective(tenant_id, series_id, branch_id, claim_id, revision,
                                rev["kind"], validity, seq)
        self._refresh_projection(tenant_id, series_id, claim_id, revision, rev, scope,
                                 validity)
        cand = rev.get("candidate_ref")
        if cand:
            self.conn.execute(
                "UPDATE claim_candidate SET validation_state='promoted' WHERE tenant_id=?"
                " AND series_id=? AND branch_id=? AND batch_id=? AND candidate_id=?",
                (tenant_id, series_id, branch_id, cand["batch_id"],
                 cand["candidate_id"]))
        return {"service_id": "S16", "id": claim_id, "revision": revision,
                "content_hash": digest}

    def _refresh_effective(self, tenant_id, series_id, branch_id, claim_id, revision,
                           kind, validity, seq) -> None:
        self.conn.execute(
            "INSERT INTO canon_effective_claim (tenant_id, series_id, branch_id, claim_id,"
            " revision, kind, clock_id, valid_from_tick, valid_to_tick, commit_sequence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(tenant_id, series_id, branch_id, claim_id) DO UPDATE SET"
            " revision=excluded.revision, kind=excluded.kind, clock_id=excluded.clock_id,"
            " valid_from_tick=excluded.valid_from_tick,"
            " valid_to_tick=excluded.valid_to_tick,"
            " commit_sequence=excluded.commit_sequence",
            (tenant_id, series_id, branch_id, claim_id, revision, kind,
             validity["clock_id"], validity["valid_from_tick"],
             validity.get("valid_to_tick"), seq))

    def _refresh_projection(self, tenant_id, series_id, claim_id, revision, rev,
                            scope, validity) -> None:
        if rev["kind"] != "knowledge_state":
            return
        self.conn.execute(
            "DELETE FROM knowledge_state WHERE tenant_id=? AND series_id=?"
            " AND claim_id=?", (tenant_id, series_id, claim_id))
        props = rev.get("proposition_claim_id") or rev.get("references") or []
        if isinstance(props, str):
            props = [props]
        for knower in scope.get("knower_ids") or []:
            self.conn.execute(
                "INSERT INTO knowledge_state (tenant_id, series_id, claim_id, revision,"
                " knower_id, proposition_claim_id, belief_type, source_event_id,"
                " clock_id, valid_from_tick, valid_to_tick,"
                " audience_reveal_episode_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, series_id, claim_id, revision, knower,
                 (props[0] if props else claim_id), scope.get("belief_type", "fact"),
                 (rev.get("source_refs") or [{}])[0].get("id", "unknown"),
                 validity["clock_id"], validity["valid_from_tick"],
                 validity.get("valid_to_tick"),
                 int(scope.get("audience_reveal_episode_order", 0))))

    def _finish_commit(self, tenant_id, series_id, branch_id, seq, head, new_hash,
                       commit_kind, body, manifest_ref, changed, superseded_refs,
                       claim_count) -> None:
        ts = self._now_ts()
        self.conn.execute(
            "INSERT INTO memory_commit (tenant_id, series_id, branch_id, commit_sequence,"
            " previous_sequence, previous_checkpoint_hash, checkpoint_hash, commit_kind,"
            " source_manifest_ref, merge_source_refs, revert_of_sequence,"
            " extraction_batch_id, command_id, actor_principal, recorded_at, claim_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, series_id, branch_id, seq, head["head_commit_sequence"],
             head["head_checkpoint_hash"], new_hash, commit_kind,
             _jcs(manifest_ref) if manifest_ref else None,
             _jcs(body.get("merge_source_refs")) if body.get("merge_source_refs") else None,
             body.get("revert_of_sequence"), body.get("batch_id"),
             body.get("command_id") or _new_id("cmd_"),
             body.get("actor_principal", "s16.story-memory"), ts, claim_count))
        if commit_kind == "extract" and isinstance(manifest_ref, dict):
            self.conn.execute(
                "INSERT INTO memory_commit_manifest (tenant_id, series_id, branch_id,"
                " commit_sequence, manifest_id, manifest_revision)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, series_id, branch_id, seq, manifest_ref.get("id"),
                 int(manifest_ref.get("revision") or 0)))
        binding = self._binding_for(tenant_id, series_id, body)
        if binding is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO commit_manifest_binding (tenant_id, series_id,"
                " branch_id, commit_sequence, manifest_ref, episode_id, episode_order,"
                " clock_id, tick_start, tick_end) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, series_id, branch_id, seq,
                 _jcs(manifest_ref) if manifest_ref else "{}",
                 binding["episode_id"], binding["episode_order"],
                 binding["clock_id"], binding["tick_start"], binding["tick_end"]))
        self.conn.execute(
            "UPDATE canon_branch SET head_commit_sequence=?, head_checkpoint_hash=?"
            " WHERE tenant_id=? AND series_id=? AND branch_id=?",
            (seq, new_hash, tenant_id, series_id, branch_id))

    def _binding_for(self, tenant_id, series_id, body) -> dict | None:
        episode_id = body.get("episode_id")
        if not episode_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM episode_story_window WHERE tenant_id=? AND series_id=?"
            " AND episode_id=?", (tenant_id, series_id, episode_id)).fetchone()
        if row is None:
            return None
        return {"episode_id": row["episode_id"], "episode_order": row["episode_order"],
                "clock_id": row["clock_id"], "tick_start": row["tick_start"],
                "tick_end": row["tick_end"]}

    # ---- 回滚 ------------------------------------------------------------

    def _revert(self, tenant_id, series_id, body) -> dict:
        branch_id = body["branch_id"]
        target = body.get("revert_of_sequence")
        if not isinstance(target, int) or target < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "revert_of_sequence must be >= 1")
        with Transaction(self.conn):
            head = self._branch(tenant_id, series_id, branch_id)
            self._cas(head, body["expected_checkpoint"])
            if target > head["head_commit_sequence"]:
                raise ServiceError(409, "CHECKPOINT_CONFLICT",
                                   "revert target is in the future")
            seq = head["head_commit_sequence"] + 1
            changed, superseded_refs = [], []
            # 回滚不 DELETE：为每个被 target 改变的 claim 生成一条"改回去"的新 revision。
            rows = self.conn.execute(
                "SELECT DISTINCT claim_id FROM claim_revision WHERE tenant_id=?"
                " AND series_id=? AND branch_id=? AND commit_sequence=?",
                (tenant_id, series_id, branch_id, target)).fetchall()
            for row in rows:
                # 不能筛 status='accepted'：要回退掉的那条 revision 恰恰已经被
                # 后一次提交置成 superseded，正是它才承载着"改回去"的值。
                # kind 在 claim 上，不在 claim_revision 上。
                # 必须限定 branch_id：revision 号是 claim 级全局递增，别的分支上
                # 编号更大的那条是该分支当前的答案，不是本分支改回去要去的地方。
                # 次序轴也只能是 commit_sequence（系统何时知道），不是 revision。
                prior = self.conn.execute(
                    "SELECT r.*, c.kind AS kind FROM claim_revision r JOIN claim c"
                    "   ON c.tenant_id=r.tenant_id AND c.series_id=r.series_id"
                    "  AND c.claim_id=r.claim_id"
                    " WHERE r.tenant_id=? AND r.series_id=? AND r.claim_id=?"
                    "   AND r.branch_id=? AND r.commit_sequence < ?"
                    " ORDER BY r.commit_sequence DESC, r.revision DESC LIMIT 1",
                    (tenant_id, series_id, row["claim_id"], branch_id,
                     target)).fetchone()
                if prior is None:
                    continue
                rev = {"claim_id": row["claim_id"], "kind": prior["kind"],
                       "fact": json.loads(prior["fact"]),
                       "story_validity": {"clock_id": prior["clock_id"],
                                          "valid_from_tick": prior["valid_from_tick"],
                                          "valid_to_tick": prior["valid_to_tick"]},
                       "epistemic_scope": json.loads(prior["epistemic_scope"]),
                       "source_refs": json.loads(prior["source_refs"]),
                       "confidence": prior["confidence"]}
                changed.append(self._write_revision(
                    tenant_id, series_id, branch_id, seq, rev,
                    json.loads(prior["accepted_manifest_ref"])
                    if prior["accepted_manifest_ref"] else None,
                    "revert", superseded_refs))
            new_hash = checkpoint_hash(series_id, branch_id, seq,
                                       head["head_checkpoint_hash"], changed)
            self._finish_commit(tenant_id, series_id, branch_id, seq, head, new_hash,
                                "revert", body, None, changed, superseded_refs,
                                len(changed))
        self._publish(tenant_id, series_id, branch_id, seq, new_hash, "revert", changed,
                      superseded_refs, None, body)
        return {"commit_sequence": seq, "checkpoint_hash": new_hash,
                "commit_kind": "revert", "revert_of_sequence": target,
                "claim_count": len(changed), "changed_claim_refs": changed,
                "superseded_claim_refs": superseded_refs}

    # ---- 合并 ------------------------------------------------------------

    def preview_merge(self, tenant_id: str, series_id: str, body: dict) -> dict:
        source = body.get("source_branch_id")
        target = body.get("target_branch_id")
        if not source or not target:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "source_branch_id and target_branch_id are required")
        conflicts = self._merge_conflicts(tenant_id, series_id, source, target)
        if not conflicts:
            return {"conflicts": [], "decisions_required": False, "auto_merge": True}
        return {"conflicts": conflicts, "decisions_required": any(
            c["default"] == "require_human" for c in conflicts),
            "auto_merge": all(c["default"] == "auto_merge" for c in conflicts)}

    def _merge_conflicts(self, tenant_id, series_id, source, target) -> list[dict]:
        src = self._effective_rows(tenant_id, series_id, source)
        tgt = {r["claim_id"]: r for r in
               self._effective_rows(tenant_id, series_id, target)}
        conflicts = []
        for row in src:
            other = tgt.get(row["claim_id"])
            if other is None:
                # 该 claim 在目标分支不存在：纯新增，可自动合并。
                conflicts.append({"kind": "BENIGN_ADD", "default": "auto_merge",
                                  "claim_id": row["claim_id"],
                                  "source_revision": row["revision"]})
                continue
            if other["revision"] == row["revision"]:
                continue
            same_value = other["value_jcs"] == row["value_jcs"]
            if same_value:
                continue
            overlapping = _overlaps(row["valid_from_tick"], row["valid_to_tick"],
                                    other["valid_from_tick"], other["valid_to_tick"])
            if overlapping:
                # §4.2：显式带 supersedes 证据的 VALUE_CONFLICT 也可以自动合并 ——
                # 源侧那条已经指名要替代目标侧这条，谁对谁错不需要人再判。
                auto = other["revision"] in _superseded_revisions(
                    self.conn, tenant_id, series_id, row["claim_id"],
                    row["revision"])
                if not auto and _is_participants_change(row["kind"], row["fact_jcs"]):
                    # story_event 的 participants 有专门类别（IDENTITY_DRIFT，warn）。
                    # 若这里再报一条 require_human 的 VALUE_CONFLICT，warn 就永远
                    # 观察不到 —— 方案把两者分开列，就说明这条该走专门那条。
                    continue
                conflicts.append({
                    "kind": "VALUE_CONFLICT",
                    "default": "auto_merge" if auto else "require_human",
                    "claim_id": row["claim_id"],
                    "source_revision": row["revision"],
                    "target_revision": other["revision"]})
        # 目标侧独有、且源侧同一 (subject,predicate) 插槽有不同量的场景。
        src_slots = {r["subject_key"]: r for r in src}
        for claim_id, row in tgt.items():
            mine = src_slots.get(row["subject_key"])
            if mine is not None and mine["claim_id"] != claim_id and (
                    mine["value_jcs"] != row["value_jcs"]):
                conflicts.append({"kind": "HOLDER_CONFLICT",
                                  "default": "require_human",
                                  "claim_id": claim_id,
                                  "conflicting_claim_id": mine["claim_id"]})
        conflicts.extend(self._merge_semantic_conflicts(
            tenant_id, series_id, src, tgt))
        return conflicts

    def _merge_semantic_conflicts(self, tenant_id, series_id, src, tgt) -> list[dict]:
        """§4.2 里不能靠 claim_id / 插槽看出来的三类：事件互斥、因果断裂、身份漂移。

        它们都要读 `fact` 的结构，所以在 `_effective_rows` 之上再做一遍。
        """
        out = []
        src_by_id = {r["claim_id"]: r for r in src}

        # EVENT_CONFLICT：同 `exclusive_group` 的两个事件（例如两种死法）。
        groups: dict[str, list[dict]] = {}
        for row in list(src) + list(tgt.values()):
            fact = self._fact_of(tenant_id, series_id, row["claim_id"], row["revision"])
            if fact.get("predicate") != "exclusive_group":
                continue
            group = fact.get("value")
            if isinstance(group, str):
                groups.setdefault(group, []).append(row)
        seen: set[tuple[str, str]] = set()
        for group, rows in groups.items():
            ids = sorted({r["claim_id"] for r in rows})
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    if (a, b) in seen:
                        continue
                    seen.add((a, b))
                    out.append({"kind": "EVENT_CONFLICT", "default": "require_human",
                                "claim_id": a, "conflicting_claim_id": b,
                                "exclusive_group": group})

        # CAUSAL_BREAK：目标分支里被替代的事件，仍被已接受的事件用 `causes` 引用。
        for claim_id, row in src_by_id.items():
            other = tgt.get(claim_id)
            if other is None or other["revision"] >= row["revision"]:
                continue
            for user in tgt.values():
                fact = self._fact_of(tenant_id, series_id, user["claim_id"],
                                     user["revision"])
                if fact.get("predicate") != "causes" or fact.get("value") != claim_id:
                    continue
                out.append({"kind": "CAUSAL_BREAK", "default": "require_human",
                            "claim_id": claim_id, "caused_by": user["claim_id"]})

        # IDENTITY_DRIFT：同一 story_event 两侧 participants 不同，且源侧没有 supersedes。
        for claim_id, row in src_by_id.items():
            other = tgt.get(claim_id)
            if other is None or other["revision"] == row["revision"]:
                continue
            mine = self._participants_of(tenant_id, series_id, claim_id,
                                         row["revision"])
            theirs = self._participants_of(tenant_id, series_id, claim_id,
                                           other["revision"])
            if mine == theirs:
                continue
            if other["revision"] in _superseded_revisions(
                    self.conn, tenant_id, series_id, claim_id, row["revision"]):
                continue
            out.append({"kind": "IDENTITY_DRIFT", "default": "warn",
                        "claim_id": claim_id,
                        "source_revision": row["revision"],
                        "target_revision": other["revision"]})
        return out

    def _participants_of(self, tenant_id, series_id, claim_id, revision) -> list[str]:
        """这条 revision 声明的参与者集合；不是 `participants` 谓词就是空集。

        fact 里参与者落在 `value` 上（`{"predicate":"participants","value":[...]}`），
        没有平铺的 `participants` 键 —— 直接读那个键会永远得到空集、永远不漂移。
        """
        fact = self._fact_of(tenant_id, series_id, claim_id, revision)
        if fact.get("predicate") != "participants":
            return []
        value = fact.get("value")
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return sorted(map(str, value))
        return []

    def _fact_of(self, tenant_id, series_id, claim_id, revision) -> dict:
        row = self.conn.execute(
            "SELECT fact AS fact_jcs FROM claim_revision WHERE tenant_id=?"
            " AND series_id=? AND claim_id=? AND revision=?",
            (tenant_id, series_id, claim_id, revision)).fetchone()
        return json.loads(row["fact_jcs"]) if row else {}

    def merge(self, tenant_id: str, series_id: str, body: dict) -> dict:
        source = body.get("source_branch_id")
        target = body.get("target_branch_id")
        if not source or not target:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "source_branch_id and target_branch_id are required")
        with Transaction(self.conn):
            head = self._branch(tenant_id, series_id, target)
            self._cas(head, body["expected_checkpoint"])
            conflicts = self._merge_conflicts(tenant_id, series_id, source, target)
            blocking = [c for c in conflicts if c["default"] != "auto_merge"]
            if blocking:
                # 不自动选边：把冲突清单交出去，checkpoint 不动。
                dec = body.get("merge_decisions") or {}
                unresolved = [c for c in blocking if c["claim_id"] not in dec]
                if unresolved:
                    raise ServiceError(409, "MERGE_CONFLICT",
                                       "merge requires human decisions",
                                       details={"conflicts": unresolved})
            seq = head["head_commit_sequence"] + 1
            src_head = self._branch(tenant_id, series_id, source)
            # merge 没有抽取 manifest；它的证据是源分支的提交指针。
            # §1.9 要求每条 accepted revision 都带 accepted_manifest_ref，
            # 合并出来的 revision 也一样 —— 否则"没有证据就没有事实"在这条路上开口子。
            evidence_ref = {"branch_id": source,
                            "commit_sequence": src_head["head_commit_sequence"],
                            "checkpoint_hash": src_head["head_checkpoint_hash"]}
            changed, superseded_refs = [], []
            src_rows = self._effective_rows(tenant_id, series_id, source)
            auto = {c["claim_id"] for c in conflicts if c["default"] == "auto_merge"}
            decided = set((body.get("merge_decisions") or {}).keys())
            for row in src_rows:
                if row["claim_id"] not in auto and row["claim_id"] not in decided:
                    continue
                rev = self._load_revision(tenant_id, series_id, row["claim_id"],
                                          row["revision"])
                changed.append(self._write_revision(
                    tenant_id, series_id, target, seq, rev, evidence_ref, "merge",
                    superseded_refs))
            new_hash = checkpoint_hash(series_id, branch_id=target, seq=seq,
                                       previous_hex=head["head_checkpoint_hash"],
                                       changed=changed)
            body = {**body, "merge_source_refs": [evidence_ref]}
            self._finish_commit(tenant_id, series_id, target, seq, head, new_hash,
                                "merge", body, None, changed, superseded_refs,
                                len(changed))
            self.conn.execute(
                "UPDATE canon_branch SET merge_status='merged' WHERE tenant_id=?"
                " AND series_id=? AND branch_id=?", (tenant_id, series_id, source))
        self._publish(tenant_id, series_id, target, seq, new_hash, "merge", changed,
                      superseded_refs, None, body)
        return {"commit_sequence": seq, "checkpoint_hash": new_hash,
                "commit_kind": "merge", "claim_count": len(changed),
                "changed_claim_refs": changed,
                "superseded_claim_refs": superseded_refs}

    def _load_revision(self, tenant_id, series_id, claim_id, revision) -> dict:
        # `kind` 在 claim 上（跨 revision 不变），不在 claim_revision 上。
        row = self.conn.execute(
            "SELECT r.*, c.kind AS kind FROM claim_revision r JOIN claim c"
            "   ON c.tenant_id=r.tenant_id AND c.series_id=r.series_id"
            "  AND c.claim_id=r.claim_id"
            " WHERE r.tenant_id=? AND r.series_id=? AND r.claim_id=? AND r.revision=?",
            (tenant_id, series_id, claim_id, revision)).fetchone()
        return {"claim_id": claim_id, "kind": row["kind"],
                "fact": json.loads(row["fact"]),
                "story_validity": {"clock_id": row["clock_id"],
                                   "valid_from_tick": row["valid_from_tick"],
                                   "valid_to_tick": row["valid_to_tick"]},
                "epistemic_scope": json.loads(row["epistemic_scope"]),
                "source_refs": json.loads(row["source_refs"]),
                "confidence": row["confidence"]}

    # ---- 发布 ------------------------------------------------------------

    def _publish(self, tenant_id, series_id, branch_id, seq, new_hash, commit_kind,
                 changed, superseded_refs, manifest_ref, body) -> None:
        subjects = sorted({c["id"] for c in changed})
        window = body.get("affected_story_window")
        with Transaction(self.conn):
            self.outbox.append(new_event(
                source=SOURCE, type="memory.canon.committed",
                subject=f"series/{series_id}/memory",
                tenant_id=tenant_id, home_cell=body.get("home_cell", "local"),
                aggregate_id=f"{series_id}/{branch_id}", sequence=seq,
                correlation_id=body.get("correlation_id") or body.get("command_id")
                or _new_id("corr_"),
                data={
                    "commit_sequence": seq,
                    "checkpoint_hash": new_hash,
                    "commit_kind": commit_kind,
                    "previous_sequence": seq - 1,
                    "changed_claim_refs": changed,
                    "superseded_claim_refs": superseded_refs,
                    # 只是失效种子，不是闭包（闭包归 S09）。
                    "changed_subject_ids": subjects,
                    "affected_story_window": window,
                    "source_manifest_ref": manifest_ref,
                    "episode_id": body.get("episode_id"),
                    "episode_order": (self._binding_for(tenant_id, series_id, body)
                                      or {}).get("episode_order"),
                }))

    # ---- 查询 ------------------------------------------------------------

    def query(self, tenant_id: str, series_id: str, body: dict) -> dict:
        branch_id = body.get("branch_id")
        minimum = body.get("minimum_checkpoint")
        if not branch_id or minimum is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "branch_id and minimum_checkpoint are required")
        branch = self._branch(tenant_id, series_id, branch_id)
        head = branch["head_commit_sequence"]
        # 硬门：绝不降级返回旧数据（反模式 6）。
        if head < minimum:
            raise ServiceError(409, "MEMORY_WATERMARK_BEHIND",
                               "branch head is behind the required checkpoint",
                               details={"current_checkpoint": head})
        checkpoint = body.get("as_of_commit_sequence")
        if checkpoint is None:
            checkpoint = head
        if checkpoint > head:
            raise ServiceError(409, "MEMORY_WATERMARK_BEHIND",
                               "as_of_commit_sequence is in the future",
                               details={"current_checkpoint": head})
        checkpoint_hash_value = self._checkpoint_at(tenant_id, series_id,
                                                    branch_id, checkpoint)

        clock_id = body.get("story_clock_id")
        from_tick = body.get("story_from_tick")
        to_tick = body.get("story_to_tick")
        perspective = body.get("perspective") or {"mode": "omniscient"}
        mode = perspective.get("mode", "omniscient")
        audience_order = perspective.get("audience_as_of_episode_order")
        characters = set(perspective.get("character_ids") or [])
        entity_ids = set(body.get("entity_ids") or [])
        kinds = set(body.get("kinds") or [])
        predicates = set(body.get("predicates") or [])
        include_superseded = bool(body.get("include_superseded"))

        claims, withheld, existence = [], [], []
        rows = self.conn.execute(
            "SELECT e.*, r.fact AS fact_jcs, r.epistemic_scope AS scope_jcs,"
            " r.source_refs AS refs_jcs, r.confidence, r.status, r.recorded_at,"
            " r.content_hash, r.review_state"
            " FROM canon_effective_claim e JOIN claim_revision r"
            "   ON r.tenant_id=e.tenant_id AND r.series_id=e.series_id"
            "  AND r.claim_id=e.claim_id AND r.revision=e.revision"
            " WHERE e.tenant_id=? AND e.series_id=? AND e.branch_id=?"
            "   AND e.commit_sequence <= ?"
            " ORDER BY e.claim_id", (tenant_id, series_id, branch_id,
                                     checkpoint)).fetchall()

        for env in rows:
            if env["status"] != "accepted" and not include_superseded:
                withheld.append(env["claim_id"])
                continue
            if clock_id and env["clock_id"] != clock_id:
                continue
            # 故事时间：区间必须覆盖查询点（半开）。
            if from_tick is not None:
                if not _overlaps(env["valid_from_tick"], env["valid_to_tick"],
                                 from_tick, to_tick if to_tick is not None
                                 else from_tick + 1):
                    continue
            fact = json.loads(env["fact_jcs"])
            if entity_ids and fact["subject_id"] not in entity_ids and not (
                    entity_ids & set(fact.get("objects") or [])):
                continue
            if kinds and env["kind"] not in kinds:
                continue
            if predicates and fact["predicate"] not in predicates:
                continue
            scope = json.loads(env["scope_jcs"])
            if mode == "character":
                if not self._visible_to_character(env, fact, scope, characters):
                    withheld.append(env["claim_id"])
                    continue
            # 揭示范围（观众视角）。
            if mode in ("character", "audience") and audience_order is not None:
                if int(scope.get("audience_reveal_episode_order", 0)) > audience_order:
                    if env["claim_id"] not in withheld:
                        withheld.append(env["claim_id"])
                    continue
            claims.append(self._render_claim(tenant_id, series_id, branch_id, env,
                                             fact, scope))

        for need in body.get("required_predicates") or []:
            subject = need.get("subject_id")
            predicate = need.get("predicate")
            found = any(c["fact"]["subject_id"] == subject
                        and c["fact"]["predicate"] == predicate for c in claims)
            existence.append({"predicate": predicate, "subject_id": subject,
                              "found": found, "checkpoint": checkpoint})

        unresolved = [c["claim_id"] for c in claims if c["claim_id"] in
                      self._conflicted_ids(tenant_id, series_id, branch_id)]
        return {"checkpoint": checkpoint, "checkpoint_hash": checkpoint_hash_value,
                "claims": claims, "withheld_claim_ids": sorted(set(withheld)),
                "existence_checks": existence,
                "unresolved_conflicts": sorted(set(unresolved))}

    def _visible_to_character(self, env, fact, scope, characters) -> bool:
        """闭世界可见性，§6.4 的公式逐条对应，默认不可见。

        只有两个放行口：这条 claim 本身是 `world_rule`，或者存在一条覆盖查询
        时点的 `knowledge_state` 说某个被查询角色知道这个命题。

        被查询 claim 自己的 `epistemic_scope` **不是**放行口 —— 那是让一条事实
        自己声明"谁能看见我"，等于在 S16 内部再造一个可见性权威。
        """
        if env["kind"] == "world_rule":
            return True
        if not characters:
            return False
        for knower in characters:
            row = self.conn.execute(
                "SELECT belief_type FROM knowledge_state WHERE tenant_id=?"
                " AND series_id=? AND knower_id=? AND proposition_claim_id=?"
                " AND valid_from_tick <= ? AND (valid_to_tick IS NULL OR"
                " valid_to_tick > ?)",
                (env["tenant_id"], env["series_id"], knower, env["claim_id"],
                 env["valid_from_tick"], env["valid_from_tick"])).fetchone()
            if row is not None and row["belief_type"] in ("knows", "believes",
                                                          "uncertain"):
                return True
        return False

    def _conflicted_ids(self, tenant_id, series_id, branch_id) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT proposed_claim_id FROM claim_candidate WHERE tenant_id=?"
            " AND series_id=? AND branch_id=? AND validation_state='conflict'",
            (tenant_id, series_id, branch_id)).fetchall()
        return {r["proposed_claim_id"] for r in rows if r["proposed_claim_id"]}

    def _render_claim(self, tenant_id, series_id, branch_id, env, fact,
                      scope) -> dict:
        return {
            "schema_version": "2.0",
            "claim_id": env["claim_id"],
            "revision": env["revision"],
            "tenant_id": tenant_id,
            "series_id": series_id,
            "branch_id": branch_id,
            "kind": env["kind"],
            "status": env["status"],
            "fact": fact,
            "story_validity": {"clock_id": env["clock_id"],
                               "valid_from_tick": env["valid_from_tick"],
                               "valid_to_tick": env["valid_to_tick"]},
            "recorded_at": env["recorded_at"],
            "commit_sequence": env["commit_sequence"],
            "source_refs": json.loads(env["refs_jcs"]),
            "accepted_manifest_ref": self._manifest_of(tenant_id, series_id,
                                                      branch_id, env["commit_sequence"]),
            "epistemic_scope": scope,
            "supersedes_claim_refs": [],
            "confidence": env["confidence"],
        }

    def _manifest_of(self, tenant_id, series_id, branch_id, seq):
        row = self.conn.execute(
            "SELECT source_manifest_ref FROM memory_commit WHERE tenant_id=? AND"
            " series_id=? AND branch_id=? AND commit_sequence=?",
            (tenant_id, series_id, branch_id, seq)).fetchone()
        if row is None or not row["source_manifest_ref"]:
            return None
        return json.loads(row["source_manifest_ref"])

    # ---- 失效下界 -------------------------------------------------------

    def invalidation_bound(self, tenant_id: str, series_id: str, body: dict) -> dict:
        """§5.2 的 B(C)：三个集合的最小 episode_order。**不返回集列表**——闭包归 S09。"""
        branch_id = body.get("branch_id")
        commit_sequence = body.get("commit_sequence")
        if not branch_id or commit_sequence is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "branch_id and commit_sequence are required")
        self._branch(tenant_id, series_id, branch_id)
        changed = self.conn.execute(
            "SELECT * FROM claim_revision WHERE tenant_id=? AND series_id=?"
            " AND branch_id=? AND commit_sequence=?",
            (tenant_id, series_id, branch_id, commit_sequence)).fetchall()
        origin = self._binding_for_seq(tenant_id, series_id, branch_id, commit_sequence)
        origin_order = origin["episode_order"] if origin else 1

        windows: list[int] = []
        for row in changed:
            fact = json.loads(row["fact"])
            for ep in self.conn.execute(
                    "SELECT episode_id, episode_order, tick_start, tick_end FROM"
                    " episode_story_window WHERE tenant_id=? AND series_id=?"
                    " AND clock_id=?", (tenant_id, series_id, row["clock_id"])):
                if _overlaps(ep["tick_start"], ep["tick_end"], row["valid_from_tick"],
                             row["valid_to_tick"]):
                    windows.append(ep["episode_order"])            # A 故事窗口重叠
            touched = {fact["subject_id"], *(fact.get("objects") or [])}
            for ep in self.conn.execute(
                    "SELECT DISTINCT episode_order, entity_id FROM episode_entity_touch"
                    " WHERE tenant_id=? AND series_id=? AND episode_order >= ?",
                    (tenant_id, series_id, origin_order)):
                if ep["entity_id"] in touched:
                    windows.append(ep["episode_order"])            # B 实体引用
            scope = json.loads(row["epistemic_scope"])
            reveal = int(scope.get("audience_reveal_episode_order", 0))
            if reveal:
                windows.append(reveal)                             # C 揭示变化
        return {"invalidation_bound": min(windows) if windows else None,
                "origin_episode_order": origin_order,
                "origin_episode_id": origin["episode_id"] if origin else None}

    def _binding_for_seq(self, tenant_id, series_id, branch_id, seq):
        row = self.conn.execute(
            "SELECT * FROM commit_manifest_binding WHERE tenant_id=? AND series_id=?"
            " AND branch_id=? AND commit_sequence=? ORDER BY episode_order LIMIT 1",
            (tenant_id, series_id, branch_id, seq)).fetchone()
        return dict(row) if row else None
