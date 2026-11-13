"""S08 业务逻辑：manifest 版本、accepted pointer、execution_fence、node_epoch。

三条命令各自是一条 CAS 路径：
  `:commit`  按 `(tenant, series, episode, branch)` 串行，推进 revision
  `:accept`  accepted pointer 的 CAS，晚到的审核走 rebase-or-reject
  `:fence`   递增 branch 级 fence，把旧 fence 下未接受的 revision 置 VOIDED

S08 只写自己的库。下游只以 `service_id/id/revision/content_hash` 引用出现，
不复制内容；任何「这一集现在是什么」的问题回 S08，「这条内容是什么」回所有者。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import EVENT_TYPE, TOPIC

SOURCE = "s08.revision-snapshot"

REQUIRED_MANIFEST_FIELDS = (
    "schema_version", "manifest_id", "revision", "tenant_id", "project_id",
    "series_id", "episode_id", "branch_id", "execution_fence", "home_cell",
    "source_revisions", "binding_set_ref", "memory_checkpoint", "policy_ref",
    "node_states", "parent_manifest_ref", "content_hash",
)
REQUIRED_NODE_FIELDS = (
    "node_id", "node_epoch", "input_fingerprint", "locked", "accepted_artifact_ref",
)
REF_FIELDS = ("service_id", "id", "revision", "content_hash")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_ref(ref) -> bool:
    if not isinstance(ref, dict):
        return False
    if set(ref) - set(REF_FIELDS):
        return False
    return all(isinstance(ref.get(k), str) and ref[k] for k in
               ("service_id", "id", "content_hash")) and isinstance(ref.get("revision"), int)


class RevisionSnapshotService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.outbox = Outbox(conn)

    # ---- 内部 -----------------------------------------------------------------

    def _manifest_row(self, tenant_id: str, manifest_id: str) -> sqlite3.Row:
        # 跨租户与不存在同码：不泄漏存在性（E28）。
        row = self.conn.execute(
            "SELECT * FROM manifest WHERE tenant_id = ? AND manifest_id = ?",
            (tenant_id, manifest_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "manifest does not exist")
        return row

    def _revision_row(self, tenant_id: str, manifest_id: str,
                      revision: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM manifest_revision"
            " WHERE tenant_id = ? AND manifest_id = ? AND revision = ?",
            (tenant_id, manifest_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "REVISION_NOT_FOUND",
                               f"revision {revision} does not exist")
        return row

    @staticmethod
    def _node_map(payload: dict) -> dict[str, dict]:
        return {n["node_id"]: n for n in payload.get("node_states") or []}

    @staticmethod
    def _node_signature(node: dict) -> tuple:
        return (node.get("input_fingerprint"),
                _jcs(node.get("accepted_artifact_ref")))

    def _changed_nodes(self, before: dict, after: dict) -> set[str]:
        a, b = self._node_map(before), self._node_map(after)
        changed = set(a) ^ set(b)
        for node_id in set(a) & set(b):
            if self._node_signature(a[node_id]) != self._node_signature(b[node_id]):
                changed.add(node_id)
        return changed

    def _emit(self, tenant_id: str, manifest_id: str, revision: int,
              correlation_id: str, data: dict) -> str:
        return self.outbox.append(new_event(
            source=SOURCE,
            type=EVENT_TYPE,
            subject=f"manifest/{manifest_id}",
            tenant_id=tenant_id,
            home_cell=data.get("home_cell", "cell_a1"),
            aggregate_id=manifest_id,
            sequence=revision,
            correlation_id=correlation_id,
            data=data,
        ))

    # ---- 校验 -----------------------------------------------------------------

    def _validate_candidate(self, tenant_id: str, candidate: dict) -> None:
        missing = [f for f in REQUIRED_MANIFEST_FIELDS if f not in candidate]
        if missing:
            raise ServiceError(422, "SCHEMA_INVALID",
                               f"candidate_manifest lacks {missing}")
        if candidate["tenant_id"] != tenant_id:
            # 租户上下文由平台签名绑定；body 里的 tenant 只是一致性声明。
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "candidate manifest belongs to another tenant")
        if not isinstance(candidate["manifest_id"], str) or not candidate["manifest_id"]:
            raise ServiceError(422, "SCHEMA_INVALID", "manifest_id must be a string")
        ch = candidate["content_hash"]
        if not isinstance(ch, str) or len(ch) != 64 or any(
                c not in "0123456789abcdef" for c in ch):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "content_hash must be 64 lowercase hex chars")
        if not isinstance(candidate["node_states"], list) or not candidate["node_states"]:
            raise ServiceError(422, "SCHEMA_INVALID", "node_states must be a non-empty list")
        seen = set()
        for node in candidate["node_states"]:
            if not isinstance(node, dict):
                raise ServiceError(422, "SCHEMA_INVALID", "node_states items must be objects")
            absent = [f for f in REQUIRED_NODE_FIELDS if f not in node]
            if absent:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"node {node.get('node_id')!r} lacks {absent}")
            if node["node_id"] in seen:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"duplicate node_id {node['node_id']!r}")
            seen.add(node["node_id"])
            if not isinstance(node["node_epoch"], int) or node["node_epoch"] < 1:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"node {node['node_id']!r} has invalid node_epoch")
            if not isinstance(node["input_fingerprint"], str) or len(
                    node["input_fingerprint"]) != 64:
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"node {node['node_id']!r} has invalid input_fingerprint")
            if node["accepted_artifact_ref"] is not None and not _valid_ref(
                    node["accepted_artifact_ref"]):
                raise ServiceError(422, "SCHEMA_INVALID",
                                   f"node {node['node_id']!r} has malformed accepted_artifact_ref")
        for field in ("binding_set_ref", "policy_ref"):
            if not _valid_ref(candidate[field]):
                raise ServiceError(422, "SCHEMA_INVALID", f"{field} is not a valid ref")
        if candidate["parent_manifest_ref"] is not None and not _valid_ref(
                candidate["parent_manifest_ref"]):
            raise ServiceError(422, "SCHEMA_INVALID", "parent_manifest_ref is not a valid ref")

    # ---- :commit --------------------------------------------------------------

    def commit(self, tenant_id: str, manifest_id: str, body: dict) -> dict:
        candidate = body.get("candidate_manifest")
        if not isinstance(candidate, dict):
            raise ServiceError(422, "SCHEMA_INVALID", "candidate_manifest is required")
        self._validate_candidate(tenant_id, candidate)
        if candidate["manifest_id"] != manifest_id:
            raise ServiceError(422, "SCHEMA_INVALID",
                               "path manifest_id does not match candidate_manifest")
        expected_revision = body.get("expected_revision")
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expected_revision must be a non-negative integer")
        command_id = body["command_id"]

        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM manifest WHERE tenant_id = ? AND manifest_id = ?",
                (tenant_id, manifest_id)).fetchone()

            if row is None:
                return self._commit_create(tenant_id, manifest_id, candidate,
                                           expected_revision, command_id, body)

            head = row["current_revision"]
            if expected_revision > head:
                raise ServiceError(409, "VERSION_CONFLICT",
                                   f"expected_revision {expected_revision} is ahead of head {head}",
                                   retryable=True, details={"head_revision": head})

            rebased_from = None
            base = None
            head_payload = json.loads(
                self._revision_row(tenant_id, manifest_id, head)["payload"])
            if expected_revision < head:
                # 晚到的提交：只有在两边改动的节点不相交时才能 rebase。
                base = self._revision_row(tenant_id, manifest_id, expected_revision)
                ours = self._changed_nodes(json.loads(base["payload"]), candidate)
                theirs = self._changed_nodes(json.loads(base["payload"]), head_payload)
                if ours & theirs:
                    raise ServiceError(
                        409, "VERSION_CONFLICT",
                        f"expected_revision {expected_revision} conflicts on "
                        f"{sorted(ours & theirs)} with head {head}",
                        retryable=True, details={"head_revision": head})
                rebased_from = expected_revision

            if candidate["execution_fence"] < row["execution_fence"]:
                raise ServiceError(409, "STALE_EXECUTION_FENCE",
                                   f"candidate carries fence {candidate['execution_fence']},"
                                   f" head is {row['execution_fence']}",
                                   details={"current_fence": row["execution_fence"]})

            if rebased_from is not None:
                # 双方的改动都要在：候选为「没动」的节点取 head 的版本，
                # 候选改动过的节点覆盖 head。直接采用候选会把 head 的并发改动抹掉。
                candidate = self._merge_onto_head(
                    candidate, head_payload, json.loads(base["payload"]))

            new_revision = head + 1
            stored = self._stored_nodes(tenant_id, manifest_id)
            merged, changed = self._apply_node_epochs(candidate, stored)

            dup = self.conn.execute(
                "SELECT revision FROM manifest_revision"
                " WHERE tenant_id = ? AND manifest_id = ? AND content_hash = ?",
                (tenant_id, manifest_id, candidate["content_hash"])).fetchone()
            if dup is not None:
                raise ServiceError(
                    409, "DUPLICATE_MANIFEST_CONTENT",
                    f"content_hash already exists at revision {dup['revision']}",
                    details={"revision": dup["revision"]})

            previous_fence = row["execution_fence"]
            self._insert_revision(tenant_id, manifest_id, candidate, merged,
                                  new_revision, head, expected_revision, command_id)
            # §4：新 revision 一落地，旧的 COMMITTED 版本就成历史。已 ACCEPTED 的
            # 不动——接受态不是「被取代」，它记着当时的接受水位。
            self.conn.execute(
                "UPDATE manifest_revision SET state = 'SUPERSEDED'"
                " WHERE tenant_id = ? AND manifest_id = ? AND revision = ?"
                " AND state = 'COMMITTED'",
                (tenant_id, manifest_id, head))
            self.conn.execute(
                "UPDATE manifest SET current_revision = ?, execution_fence = ?,"
                " updated_at = ?, lock_version = lock_version + 1"
                " WHERE tenant_id = ? AND manifest_id = ?",
                (new_revision, candidate["execution_fence"], _now(),
                 tenant_id, manifest_id))
            self._write_node_states(tenant_id, manifest_id, merged, new_revision)
            # accepted pointer 不受 commit 影响：提交不推进接受水位。

            event_id = self._emit(tenant_id, manifest_id, new_revision, command_id, {
                "manifest_ref": {"id": manifest_id, "revision": new_revision,
                                 "content_hash": candidate["content_hash"]},
                "parent_manifest_ref": self._parent_ref(tenant_id, manifest_id, head),
                "execution_fence": candidate["execution_fence"],
                "changed_node_epochs": changed,
                "accepted_pointers_delta": [],
                "accepted_revision": row["accepted_revision"],
                "source_revisions": candidate["source_revisions"],
                "trigger": "COMMIT",
                "home_cell": candidate["home_cell"],
            })
            return {
                "manifest": self._render_manifest(tenant_id, manifest_id, new_revision),
                "previous_revision": head,
                "rebased": rebased_from is not None,
                "rebased_from_revision": rebased_from,
                "previous_fence": previous_fence,
                "changed_node_epochs": changed,
                "outbox_event_id": event_id,
            }

    def _commit_create(self, tenant_id: str, manifest_id: str, candidate: dict,
                       expected_revision: int, command_id: str, body: dict) -> dict:
        if expected_revision != 0:
            raise ServiceError(409, "VERSION_CONFLICT",
                               "manifest does not exist; expected_revision must be 0",
                               retryable=True, details={"head_revision": 0})
        now = _now()
        self.conn.execute(
            "INSERT INTO manifest (manifest_id, tenant_id, project_id, series_id,"
            " episode_id, branch_id, home_cell, current_revision, accepted_revision,"
            " execution_fence, parent_manifest_id, created_at, updated_at, lock_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, NULL, ?, ?, 0)",
            (manifest_id, tenant_id, candidate["project_id"], candidate["series_id"],
             candidate["episode_id"], candidate["branch_id"], candidate["home_cell"],
             candidate["execution_fence"], now, now))
        merged, changed = self._apply_node_epochs(candidate, {})
        self._insert_revision(tenant_id, manifest_id, candidate, merged, 1, 0, 0,
                              command_id)
        self._write_node_states(tenant_id, manifest_id, merged, 1)
        event_id = self._emit(tenant_id, manifest_id, 1, command_id, {
            "manifest_ref": {"id": manifest_id, "revision": 1,
                             "content_hash": candidate["content_hash"]},
            "parent_manifest_ref": None,
            "execution_fence": candidate["execution_fence"],
            "changed_node_epochs": changed,
            "accepted_pointers_delta": [],
            "accepted_revision": None,
            "source_revisions": candidate["source_revisions"],
            "trigger": "COMMIT",
            "home_cell": candidate["home_cell"],
        })
        return {
            "manifest": self._render_manifest(tenant_id, manifest_id, 1),
            "previous_revision": 0,
            "rebased": False,
            "rebased_from_revision": None,
            "changed_node_epochs": changed,
            "outbox_event_id": event_id,
        }

    def _merge_onto_head(self, candidate: dict, head_payload: dict,
                         base_payload: dict) -> dict:
        """把候选 rebase 到 head：候选没动的节点用 head 的版本。"""
        base_nodes = self._node_map(base_payload)
        head_nodes = self._node_map(head_payload)
        merged: dict[str, dict] = {}
        for node_id, node in base_nodes.items():
            merged[node_id] = head_nodes.get(node_id, node)
        for node_id, node in self._node_map(candidate).items():
            if node_id in base_nodes and self._node_signature(
                    node) == self._node_signature(base_nodes[node_id]):
                continue  # 候选没动这个节点，保留 head 的并发改动
            merged[node_id] = node
        out = dict(candidate)
        out["node_states"] = [merged[n] for n in sorted(merged)]
        return out

    def _stored_nodes(self, tenant_id: str, manifest_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM node_state WHERE tenant_id = ? AND manifest_id = ?",
            (tenant_id, manifest_id)).fetchall()
        return {r["node_id"]: r for r in rows}

    def _apply_node_epochs(self, candidate: dict,
                           stored: dict[str, sqlite3.Row]) -> tuple[list[dict], list[dict]]:
        """按 §3.2 计算权威 node_states，并返回真正变化的 epoch。

        epoch 只由 `input_fingerprint` 变化推进：改一个镜头不推进其它节点，
        在途任务继续有效。客户端给的 epoch 只做比较，不采纳。
        """
        merged = []
        changed = []
        for node in candidate["node_states"]:
            node_id = node["node_id"]
            prior = stored.get(node_id)
            client_epoch = node["node_epoch"]
            if prior is None:
                if client_epoch > 1:
                    raise ServiceError(
                        422, "NODE_EPOCH_SKIP",
                        f"node {node_id!r} claims epoch {client_epoch} but it is new")
                epoch = 1
            else:
                current = prior["node_epoch"]
                if client_epoch > current:
                    raise ServiceError(
                        422, "NODE_EPOCH_SKIP",
                        f"node {node_id!r} claims epoch {client_epoch}, current is {current}")
                if client_epoch < current:
                    raise ServiceError(
                        409, "STALE_NODE_EPOCH",
                        f"node {node_id!r} claims epoch {client_epoch}, current is {current}",
                        details={"nodes": [{"node_id": node_id, "current": current}]})
                epoch = current + 1 if (
                    node["input_fingerprint"] != prior["input_fingerprint"]) else current
            if epoch != client_epoch:
                changed.append({"node_id": node_id, "from": client_epoch, "to": epoch})
            entry = dict(node)
            entry["node_epoch"] = epoch
            merged.append(entry)
        return merged, changed

    def _insert_revision(self, tenant_id: str, manifest_id: str, candidate: dict,
                         merged: list[dict], revision: int, parent_revision: int,
                         base_revision: int, command_id: str) -> None:
        payload = dict(candidate)
        payload["revision"] = revision
        payload["node_states"] = merged
        if parent_revision:
            payload["parent_manifest_ref"] = {
                "service_id": "S08", "id": manifest_id, "revision": parent_revision,
                "content_hash": self._content_hash_at(tenant_id, manifest_id, parent_revision),
            }
        self.conn.execute(
            "INSERT INTO manifest_revision (manifest_id, revision, tenant_id, content_hash,"
            " parent_revision, base_revision, execution_fence, state, payload,"
            " source_revisions, change_plan_ref, policy_decision_ref, committed_by,"
            " committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'COMMITTED', ?, ?, ?, ?, ?, ?)",
            (manifest_id, revision, tenant_id, candidate["content_hash"],
             parent_revision or None, base_revision, candidate["execution_fence"],
             _jcs(payload), _jcs(candidate["source_revisions"]),
             _jcs(candidate.get("change_plan_ref")) if candidate.get("change_plan_ref") else None,
             candidate.get("policy_decision_id"),
             command_id, _now()))

    def _content_hash_at(self, tenant_id: str, manifest_id: str, revision: int) -> str:
        row = self.conn.execute(
            "SELECT content_hash FROM manifest_revision"
            " WHERE tenant_id = ? AND manifest_id = ? AND revision = ?",
            (tenant_id, manifest_id, revision)).fetchone()
        return row["content_hash"] if row else ""

    def _parent_ref(self, tenant_id: str, manifest_id: str, revision: int):
        if not revision:
            return None
        return {"service_id": "S08", "id": manifest_id, "revision": revision,
                "content_hash": self._content_hash_at(tenant_id, manifest_id, revision)}

    def _write_node_states(self, tenant_id: str, manifest_id: str,
                           merged: list[dict], revision: int) -> None:
        now = _now()
        for node in merged:
            existing = self.conn.execute(
                "SELECT accepted_artifact_ref, accepted_at_revision FROM node_state"
                " WHERE manifest_id = ? AND node_id = ?",
                (manifest_id, node["node_id"])).fetchone()
            if existing is None:
                # candidate 里带的 accepted_artifact_ref 只是「这个版本产出的制品」；
                # 接受指针只能由 :accept 写。首次见到节点时它必然是未接受的。
                self.conn.execute(
                    "INSERT INTO node_state (manifest_id, node_id, tenant_id, node_epoch,"
                    " input_fingerprint, locked, accepted_artifact_ref, accepted_at_revision,"
                    " accepted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                    (manifest_id, node["node_id"], tenant_id, node["node_epoch"],
                     node["input_fingerprint"], int(bool(node["locked"])), now))
            else:
                # 接受指针由 :accept 推进；commit 只更新 epoch / 指纹 / 锁，
                # 绝不因为「新版本带着同一个 ref」就重写 accepted_at_revision。
                self.conn.execute(
                    "UPDATE node_state SET node_epoch = ?, input_fingerprint = ?,"
                    " locked = ?, updated_at = ? WHERE manifest_id = ? AND node_id = ?",
                    (node["node_epoch"], node["input_fingerprint"],
                     int(bool(node["locked"])), now, manifest_id, node["node_id"]))

    # ---- :accept --------------------------------------------------------------

    def accept(self, tenant_id: str, manifest_id: str, body: dict) -> dict:
        node_ids = body.get("node_ids") or []
        if not isinstance(node_ids, list) or any(not isinstance(n, str) for n in node_ids):
            raise ServiceError(400, "MALFORMED_REQUEST", "node_ids must be a list of strings")
        expected_revision = body.get("expected_revision")
        expected_fence = body.get("expected_fence")
        for name, value in (("expected_revision", expected_revision),
                            ("expected_fence", expected_fence)):
            if not isinstance(value, int) or value < 1:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"{name} must be a positive integer")
        decision_ref = body.get("decision_ref")
        policy_ref = body.get("policy_ref")
        if not _valid_ref(decision_ref):
            raise ServiceError(422, "SCHEMA_INVALID", "decision_ref is not a valid ref")
        if not _valid_ref(policy_ref):
            raise ServiceError(422, "SCHEMA_INVALID", "policy_ref is not a valid ref")
        actor = body.get("actor")
        if not isinstance(actor, dict) or not actor.get("principal_id"):
            raise ServiceError(422, "SCHEMA_INVALID", "actor.principal_id is required")
        command_id = body["command_id"]
        qc_report_refs = body.get("qc_report_refs") or []
        override_reason = body.get("override_reason")

        # 被拒的接受必须留档，而留档要在业务写入回滚之后仍存在。所以先把失败
        # 记进 pending，退出 with（审计行随提交落盘），再抛。
        pending: ServiceError | None = None
        with self.conn:
            row = self._manifest_row(tenant_id, manifest_id)
            head = row["current_revision"]
            head_payload = json.loads(
                self._revision_row(tenant_id, manifest_id, head)["payload"])
            head_nodes = self._node_map(head_payload)
            wanted = node_ids or sorted(head_nodes)

            if expected_fence != row["execution_fence"]:
                pending = ServiceError(
                    409, "STALE_EXECUTION_FENCE",
                    f"expected_fence {expected_fence}, head is {row['execution_fence']}",
                    details={"current_fence": row["execution_fence"]})
            elif expected_revision > head:
                pending = ServiceError(
                    409, "VERSION_CONFLICT",
                    f"expected_revision {expected_revision} is ahead of head {head}",
                    retryable=True, details={"head_revision": head})
            else:
                target = self._revision_row(tenant_id, manifest_id, expected_revision)
                # 只挡 VOIDED：作废是终态。SUPERSEDED 不是拒绝理由——§4 明说
                # 「晚到的审核想接受旧 revision 时，走的不是这个转移，而是 §3.3
                # 的 rebase-or-reject」，能否接受由下面的证据闸与 rebase 条件判。
                if target["state"] == "VOIDED":
                    pending = ServiceError(
                        409, "ILLEGAL_STATE_TRANSITION",
                        f"revision {expected_revision} is VOIDED")
                else:
                    unknown = [n for n in wanted if n not in head_nodes]
                    if unknown:
                        pending = ServiceError(404, "NOT_FOUND",
                                               f"node {unknown[0]!r} does not exist")

            decision = self._review_evidence(tenant_id, decision_ref)
            rebased_from = None
            if pending is None:
                if decision is None:
                    pending = ServiceError(409, "STALE_DECISION",
                                           "review decision evidence is not present")
                else:
                    stale, _detail = self._check_decision_against(
                        decision, expected_fence, expected_revision, head,
                        head_nodes, wanted)
                    if stale:
                        refuse = self._rebase_allowed(
                            tenant_id, manifest_id, decision, expected_revision, head,
                            head_nodes, wanted)
                        if refuse is not None:
                            self._record_accept_commit(
                                tenant_id, manifest_id, expected_revision, wanted,
                                expected_revision, expected_fence, decision_ref,
                                policy_ref, qc_report_refs, actor,
                                "REJECTED_STALE", refuse, None)
                            pending = ServiceError(
                                409, "STALE_DECISION",
                                "late decision cannot be rebased", details=refuse)
                        else:
                            rebased_from = expected_revision

            if pending is None:
                qc = self._qc_evidence(tenant_id, qc_report_refs)
                bad = self._qc_mismatch(qc, head_nodes, wanted)
                if bad:
                    pending = ServiceError(422, "QC_EVIDENCE_MISMATCH",
                                           f"QC artifact hash disagrees for {sorted(bad)}",
                                           details={"nodes": sorted(bad)})
                else:
                    locked = [n for n in wanted if head_nodes[n]["locked"]]
                    if locked and not override_reason:
                        pending = ServiceError(422, "NODE_LOCKED",
                                               f"nodes are locked: {sorted(locked)}",
                                               details={"nodes": sorted(locked)})

            delta = []
            accepted_revision = row["accepted_revision"]
            commit_id = None
            if pending is None:
                now = _now()
                for node_id in wanted:
                    node = head_nodes[node_id]
                    self.conn.execute(
                        "UPDATE node_state SET accepted_artifact_ref = ?,"
                        " accepted_at_revision = ?, accepted_at = ?, updated_at = ?"
                        " WHERE tenant_id = ? AND manifest_id = ? AND node_id = ?",
                        (_jcs(node["accepted_artifact_ref"]), head, now, now,
                         tenant_id, manifest_id, node_id))
                    delta.append({"node_id": node_id,
                                  "accepted_artifact_ref": node["accepted_artifact_ref"],
                                  "accepted_at_revision": head})

                # 「全部节点已接受」必须在持有 manifest 行锁时算（§5.3），
                # 否则两笔并发接受各自以为对方还没接受。
                outstanding = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM node_state WHERE tenant_id = ?"
                    " AND manifest_id = ? AND accepted_artifact_ref IS NULL",
                    (tenant_id, manifest_id)).fetchone()["n"]
                if outstanding == 0:
                    accepted_revision = head
                    self.conn.execute(
                        "UPDATE manifest SET accepted_revision = ?, updated_at = ?,"
                        " lock_version = lock_version + 1"
                        " WHERE tenant_id = ? AND manifest_id = ?",
                        (accepted_revision, now, tenant_id, manifest_id))
                    self.conn.execute(
                        "UPDATE manifest_revision SET state = 'ACCEPTED'"
                        " WHERE tenant_id = ? AND manifest_id = ? AND revision = ?",
                        (tenant_id, manifest_id, head))

                commit_id = self._record_accept_commit(
                    tenant_id, manifest_id, head, wanted, expected_revision,
                    expected_fence, decision_ref, policy_ref, qc_report_refs, actor,
                    "ACCEPTED", None, rebased_from)
                # SMC-1：ACCEPT 的 aggregate_sequence 与 COMMIT 共用 revision，会撞
                # §1.6 的 outbox 唯一键。事件形态待裁决，这里只落审计行不伪造事件。
                return {
                    "accepted_revision": accepted_revision,
                    "nodes": delta,
                    "acceptance_commit_id": commit_id,
                    "rebased": rebased_from is not None,
                    "rebased_from_revision": rebased_from,
                }

        # 走到这里说明被拒：审计行随 with 提交，然后才抛。
        assert pending is not None
        raise pending

    def _review_evidence(self, tenant_id: str, ref) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM review_evidence WHERE tenant_id = ? AND decision_id = ?"
            " AND decision_revision = ?",
            (tenant_id, ref["id"], ref["revision"])).fetchone()

    def _qc_evidence(self, tenant_id: str, refs: list) -> dict[tuple, sqlite3.Row]:
        out = {}
        for ref in refs:
            if not _valid_ref(ref):
                raise ServiceError(422, "SCHEMA_INVALID", "qc_report_refs item is malformed")
            rows = self.conn.execute(
                "SELECT * FROM qc_evidence WHERE tenant_id = ? AND report_id = ?"
                " AND report_revision = ?",
                (tenant_id, ref["id"], ref["revision"])).fetchall()
            for row in rows:
                out[(ref["id"], ref["revision"], row["node_id"])] = row
        return out

    def _check_decision_against(self, decision: sqlite3.Row, expected_fence: int,
                                expected_revision: int, head: int,
                                head_nodes: dict, wanted: list[str]):
        if decision["execution_fence"] != expected_fence:
            return True, {"reason": "FENCE_CHANGED",
                          "decision_fence": decision["execution_fence"],
                          "expected_fence": expected_fence}
        if decision["verdict"] != "approved":
            return True, {"reason": "DECISION_NOT_APPROVED", "verdict": decision["verdict"]}
        if expected_revision < head:
            return True, {"reason": "REVISION_BEHIND", "decision_revision": expected_revision,
                          "head_revision": head}
        for node_id in wanted:
            row = decision["node_epochs"] and json.loads(decision["node_epochs"]).get(node_id)
            if row is None:
                return True, {"reason": "NODE_NOT_COVERED", "node_id": node_id}
            if row != head_nodes[node_id]["node_epoch"]:
                return True, {"reason": "NODE_EPOCH_CHANGED", "node_id": node_id,
                              "decision_epoch": row,
                              "current_epoch": head_nodes[node_id]["node_epoch"]}
        return False, {}

    def _rebase_allowed(self, tenant_id: str, manifest_id: str, decision: sqlite3.Row,
                        expected_revision: int, head: int, head_nodes: dict,
                        wanted: list[str]):
        """§3.3 的晚到接受：仅当四个条件全成立才允许 rebase。返回 None 表示允许。"""
        if decision["execution_fence"] != self._manifest_row(
                tenant_id, manifest_id)["execution_fence"]:
            return {"reason": "FENCE_CHANGED"}
        if decision["manifest_id"] != manifest_id:
            return {"reason": "MANIFEST_CHANGED"}
        if decision["verdict"] != "approved":
            return {"reason": "DECISION_NOT_APPROVED", "verdict": decision["verdict"]}
        base_payload = json.loads(
            self._revision_row(tenant_id, manifest_id, expected_revision)["payload"])
        base_nodes = self._node_map(base_payload)
        epochs = json.loads(decision["node_epochs"] or "{}")
        hashes = json.loads(decision["artifact_hashes"] or "{}")
        for node_id in wanted:
            if epochs.get(node_id) != head_nodes[node_id]["node_epoch"]:
                return {"reason": "NODE_EPOCH_CHANGED", "node_id": node_id}
            if node_id not in base_nodes:
                return {"reason": "NODE_ABSENT", "node_id": node_id}
            if base_nodes[node_id]["input_fingerprint"] != head_nodes[node_id]["input_fingerprint"]:
                return {"reason": "INPUT_CHANGED", "node_id": node_id}
            if _jcs(base_nodes[node_id]["accepted_artifact_ref"]) != _jcs(
                    head_nodes[node_id]["accepted_artifact_ref"]):
                return {"reason": "ACCEPTED_MOVED", "node_id": node_id}
            if hashes.get(node_id) != (
                    head_nodes[node_id]["accepted_artifact_ref"] or {}).get("content_hash"):
                return {"reason": "ARTIFACT_MOVED", "node_id": node_id}
        return None

    def _qc_mismatch(self, qc: dict[tuple, sqlite3.Row], head_nodes: dict,
                     wanted: list[str]) -> set[str]:
        by_node = {}
        for (_report_id, _rev, node_id), row in qc.items():
            by_node.setdefault(node_id, []).append(row)
        bad = set()
        for node_id in wanted:
            expected = (head_nodes[node_id]["accepted_artifact_ref"] or {}).get("content_hash")
            if expected is None:
                # 没有制品就没有可比对的 hash；接受门槛由其它闸（决定/锁）把关。
                continue
            rows = by_node.get(node_id)
            if not rows or any(row["artifact_hash"] != expected for row in rows):
                bad.add(node_id)
        return bad

    def _record_accept_commit(self, tenant_id: str, manifest_id: str, revision: int,
                              node_ids: list[str], expected_revision: int,
                              expected_fence: int, decision_ref: dict, policy_ref: dict,
                              qc_report_refs: list, actor: dict, result: str,
                              reject_detail: dict | None,
                              rebased_from: int | None) -> str:
        commit_id = _new_id("ac_")
        self.conn.execute(
            "INSERT INTO acceptance_commit (commit_id, tenant_id, manifest_id, revision,"
            " node_ids, expected_revision, expected_fence, decision_ref, policy_ref,"
            " qc_report_refs, evidence_refs, actor, result, reject_detail,"
            " rebased_from_revision, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
            (commit_id, tenant_id, manifest_id, revision, ",".join(node_ids),
             expected_revision, expected_fence, _jcs(decision_ref), _jcs(policy_ref),
             _jcs(qc_report_refs), _jcs(actor), result,
             _jcs(reject_detail) if reject_detail else None, rebased_from, _now()))
        return commit_id

    # ---- :fence ---------------------------------------------------------------

    def fence(self, tenant_id: str, series_id: str, branch_id: str, body: dict) -> dict:
        expected_fence = body.get("expected_fence")
        if not isinstance(expected_fence, int) or expected_fence < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expected_fence must be a positive integer")
        reason = body.get("reason")
        if reason not in ("RUN_CANCELED", "RUN_FAILOVER", "BRANCH_ABANDONED", "MANUAL"):
            raise ServiceError(422, "SCHEMA_INVALID", f"unknown fence reason {reason!r}")
        now = _now()
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM branch_state WHERE tenant_id = ? AND series_id = ?"
                " AND branch_id = ?", (tenant_id, series_id, branch_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND", "branch does not exist")
            if row["execution_fence"] != expected_fence:
                raise ServiceError(
                    409, "FENCE_CONFLICT",
                    f"expected_fence {expected_fence}, current is {row['execution_fence']}",
                    retryable=True, details={"current_fence": row["execution_fence"]})
            new_fence = expected_fence + 1
            self.conn.execute(
                "UPDATE branch_state SET execution_fence = ?, fence_reason = ?,"
                " fence_ref = ?, updated_at = ? WHERE tenant_id = ? AND series_id = ?"
                " AND branch_id = ?",
                (new_fence, reason, body.get("fence_ref"), now,
                 tenant_id, series_id, branch_id))

            manifests = self.conn.execute(
                "SELECT manifest_id, current_revision, execution_fence FROM manifest"
                " WHERE tenant_id = ? AND series_id = ? AND branch_id = ?"
                " ORDER BY manifest_id", (tenant_id, series_id, branch_id)).fetchall()
            voided = []
            for m in manifests:
                # 判「旧 fence」要拿**新** fence 比：m 上的 execution_fence 还是旧值。
                voided += self._void_revisions(tenant_id, m, new_fence)
                self.conn.execute(
                    "UPDATE manifest SET execution_fence = ?, updated_at = ?,"
                    " lock_version = lock_version + 1"
                    " WHERE tenant_id = ? AND manifest_id = ?",
                    (new_fence, now, tenant_id, m["manifest_id"]))
            return {"execution_fence": new_fence, "voided_revisions": voided}

    def _void_revisions(self, tenant_id: str, manifest: sqlite3.Row,
                        new_fence: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT revision FROM manifest_revision"
            " WHERE tenant_id = ? AND manifest_id = ?"
            " AND state IN ('COMMITTED','ACCEPTED') AND execution_fence < ?"
            " ORDER BY revision",
            (tenant_id, manifest["manifest_id"], new_fence)).fetchall()
        revisions = [r["revision"] for r in rows]
        for revision in revisions:
            self.conn.execute(
                "UPDATE manifest_revision SET state = 'VOIDED'"
                " WHERE tenant_id = ? AND manifest_id = ? AND revision = ?",
                (tenant_id, manifest["manifest_id"], revision))
        return revisions

    def register_branch(self, tenant_id: str, body: dict) -> dict:
        """分支壳。真实链路里由 project.episode.created 建；内核里显式提供。"""
        series_id, branch_id = body.get("series_id"), body.get("branch_id")
        if not series_id or not branch_id:
            raise ServiceError(422, "SCHEMA_INVALID", "series_id and branch_id are required")
        fence = body.get("execution_fence", 1)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO branch_state (tenant_id, series_id, branch_id,"
                " execution_fence, updated_at) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, series_id, branch_id, fence, _now()))
            row = self.conn.execute(
                "SELECT * FROM branch_state WHERE tenant_id = ? AND series_id = ?"
                " AND branch_id = ?", (tenant_id, series_id, branch_id)).fetchone()
        return {"series_id": series_id, "branch_id": branch_id,
                "execution_fence": row["execution_fence"]}

    # ---- 证据摄取 --------------------------------------------------------------

    def ingest_quality(self, tenant_id: str, body: dict) -> dict:
        """S35/S36/S37 的 QC 结果。只增不改；重复投递按主键忽略（E32）。"""
        report_id = body.get("report_id")
        node_id = body.get("node_id")
        if not isinstance(report_id, str) or not isinstance(node_id, str):
            raise ServiceError(422, "SCHEMA_INVALID", "report_id and node_id are required")
        for field in ("report_revision", "manifest_revision", "execution_fence", "node_epoch"):
            if not isinstance(body.get(field), int):
                raise ServiceError(422, "SCHEMA_INVALID", f"{field} must be an integer")
        if not body.get("manifest_id") or not body.get("artifact_hash"):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "manifest_id and artifact_hash are required")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO qc_evidence (tenant_id, report_id, report_revision,"
                " content_hash, manifest_id, manifest_revision, execution_fence, node_id,"
                " node_epoch, artifact_hash, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, report_id, body["report_revision"],
                 body.get("content_hash", ""), body["manifest_id"],
                 body["manifest_revision"], body["execution_fence"], node_id,
                 body["node_epoch"], body["artifact_hash"], _now()))
        return {"received": True, "report_id": report_id, "node_id": node_id}

    def ingest_review(self, tenant_id: str, body: dict) -> dict:
        """S40 的审核决定。只增不改；绝不因为事件到达就推进 accepted pointer。"""
        decision_id = body.get("decision_id")
        if not isinstance(decision_id, str) or not isinstance(body.get("decision_revision"), int):
            raise ServiceError(422, "SCHEMA_INVALID",
                               "decision_id and integer decision_revision are required")
        if body.get("verdict") not in ("approved", "rejected"):
            raise ServiceError(422, "SCHEMA_INVALID", "verdict must be approved or rejected")
        for field in ("manifest_id", "manifest_revision", "execution_fence"):
            if not body.get(field):
                raise ServiceError(422, "SCHEMA_INVALID", f"{field} is required")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO review_evidence (tenant_id, decision_id,"
                " decision_revision, content_hash, manifest_id, manifest_revision,"
                " execution_fence, verdict, node_epochs, artifact_hashes, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, decision_id, body["decision_revision"],
                 body.get("content_hash", ""), body["manifest_id"],
                 body["manifest_revision"], body["execution_fence"], body["verdict"],
                 _jcs(body.get("node_epochs") or {}),
                 _jcs(body.get("artifact_hashes") or {}), _now()))
        return {"received": True, "decision_id": decision_id}

    # ---- 查询 -----------------------------------------------------------------

    def get_manifest(self, tenant_id: str, manifest_id: str) -> dict:
        row = self._manifest_row(tenant_id, manifest_id)
        return {
            "manifest_id": row["manifest_id"],
            "project_id": row["project_id"],
            "series_id": row["series_id"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "home_cell": row["home_cell"],
            "current_revision": row["current_revision"],
            "accepted_revision": row["accepted_revision"],
            "execution_fence": row["execution_fence"],
            "lock_version": row["lock_version"],
            "source_revision": row["current_revision"],
            "read_at": _now(),
        }

    def _render_manifest(self, tenant_id: str, manifest_id: str, revision: int) -> dict:
        row = self._revision_row(tenant_id, manifest_id, revision)
        payload = json.loads(row["payload"])
        payload["revision"] = revision
        return payload

    def get_revision(self, tenant_id: str, manifest_id: str, revision: int) -> dict:
        row = self._revision_row(tenant_id, manifest_id, revision)
        head = self._manifest_row(tenant_id, manifest_id)["current_revision"]
        return {
            "manifest": json.loads(row["payload"]),
            "state": row["state"],
            "content_hash": row["content_hash"],
            "committed_at": row["committed_at"],
            "source_revision": head,
            "read_at": _now(),
        }

    def accepted_pointers(self, tenant_id: str, manifest_id: str) -> dict:
        head = self._manifest_row(tenant_id, manifest_id)["current_revision"]
        rows = self.conn.execute(
            "SELECT node_id, node_epoch, accepted_artifact_ref, accepted_at_revision"
            " FROM node_state WHERE tenant_id = ? AND manifest_id = ? ORDER BY node_id",
            (tenant_id, manifest_id)).fetchall()
        nodes = []
        for row in rows:
            ref = json.loads(row["accepted_artifact_ref"]) if row["accepted_artifact_ref"] else None
            nodes.append({"node_id": row["node_id"], "node_epoch": row["node_epoch"],
                          "accepted_artifact_ref": ref,
                          "accepted_at_revision": row["accepted_at_revision"]})
        return {"manifest_id": manifest_id, "nodes": nodes,
                "source_revision": head, "read_at": _now()}

    def node_diff(self, tenant_id: str, manifest_id: str, from_revision: int,
                  to_revision: int | None = None, node_id: str | None = None) -> dict:
        head = self._manifest_row(tenant_id, manifest_id)["current_revision"]
        to_revision = head if to_revision is None else to_revision
        if from_revision < 1 or from_revision > head or to_revision < 1 or to_revision > head:
            raise ServiceError(404, "REVISION_NOT_FOUND",
                               "revision is outside this manifest's history")
        a = json.loads(self._revision_row(tenant_id, manifest_id, from_revision)["payload"])
        b = json.loads(self._revision_row(tenant_id, manifest_id, to_revision)["payload"])
        if node_id is not None:
            return self._diff_one(manifest_id, from_revision, to_revision, head,
                                  self._node_map(a).get(node_id),
                                  self._node_map(b).get(node_id), node_id)

        na, nb = self._node_map(a), self._node_map(b)
        names = sorted(set(na) | set(nb))
        if len(names) > 2000:
            raise ServiceError(413, "NODE_DIFF_TOO_LARGE",
                               f"{len(names)} nodes; pass node_id to narrow the range")
        nodes, summary = self._classify(na, nb, names)
        etag = _sha256_text(_jcs([json.loads(json.dumps(a)), json.loads(json.dumps(b))]))
        return {"manifest_id": manifest_id, "from_revision": from_revision,
                "to_revision": to_revision, "source_revision": head,
                "nodes": nodes, "summary": summary, "etag": etag}

    def _diff_one(self, manifest_id: str, from_revision: int, to_revision: int,
                  head: int, old: dict | None, new: dict | None, node_id: str) -> dict:
        nodes, summary = self._classify({}, {}, [node_id],
                                        {node_id: old} if old else {},
                                        {node_id: new} if new else {})
        return {"manifest_id": manifest_id, "from_revision": from_revision,
                "to_revision": to_revision, "source_revision": head,
                "nodes": nodes, "summary": summary,
                "etag": _sha256_text(_jcs([old, new]))}

    def _classify(self, na: dict, nb: dict, names: list[str],
                  only_a: dict | None = None, only_b: dict | None = None):
        na = na if only_a is None else only_a
        nb = nb if only_b is None else only_b
        nodes = []
        summary = {"unchanged": 0, "input_changed": 0, "accepted_moved": 0,
                   "input_and_accepted_changed": 0, "added": 0, "removed": 0}
        for node_id in names:
            old, new = na.get(node_id), nb.get(node_id)
            if old is None:
                change, bucket = "ADDED", "added"
            elif new is None:
                change, bucket = "REMOVED", "removed"
            else:
                input_moved = old["input_fingerprint"] != new["input_fingerprint"]
                accepted_moved = _jcs(old.get("accepted_artifact_ref")) != _jcs(
                    new.get("accepted_artifact_ref"))
                if input_moved and accepted_moved:
                    change, bucket = "INPUT_AND_ACCEPTED_CHANGED", "input_and_accepted_changed"
                elif input_moved:
                    change, bucket = "INPUT_CHANGED", "input_changed"
                elif accepted_moved:
                    change, bucket = "ACCEPTED_MOVED", "accepted_moved"
                else:
                    change, bucket = "UNCHANGED", "unchanged"
            summary[bucket] += 1
            nodes.append({
                "node_id": node_id, "change": change,
                "from_input_fingerprint": old["input_fingerprint"] if old else None,
                "to_input_fingerprint": new["input_fingerprint"] if new else None,
                "from_accepted_artifact_ref": old.get("accepted_artifact_ref") if old else None,
                "to_accepted_artifact_ref": new.get("accepted_artifact_ref") if new else None,
                "from_locked": bool(old["locked"]) if old else None,
                "to_locked": bool(new["locked"]) if new else None,
            })
        return nodes, summary

    def list_revisions(self, tenant_id: str, manifest_id: str, state: str | None = None,
                       after_revision: int = 0, limit: int = 50) -> dict:
        head = self._manifest_row(tenant_id, manifest_id)["current_revision"]
        sql = ("SELECT revision, state, content_hash, execution_fence, committed_at"
               " FROM manifest_revision WHERE tenant_id = ? AND manifest_id = ?"
               " AND revision > ?")
        args: list = [tenant_id, manifest_id, after_revision]
        if state:
            sql += " AND state = ?"
            args.append(state)
        sql += " ORDER BY revision DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        rows = self.conn.execute(sql, args).fetchall()
        return {"manifest_id": manifest_id,
                "revisions": [dict(r) for r in rows],
                "source_revision": head, "read_at": _now()}

    def episode_manifests(self, tenant_id: str, episode_id: str,
                          branch_id: str | None = None) -> dict:
        sql = ("SELECT * FROM manifest WHERE tenant_id = ? AND episode_id = ?")
        args: list = [tenant_id, episode_id]
        if branch_id:
            sql += " AND branch_id = ?"
            args.append(branch_id)
        rows = self.conn.execute(sql + " ORDER BY branch_id", args).fetchall()
        return {"episode_id": episode_id,
                "manifests": [{
                    "manifest_id": r["manifest_id"], "branch_id": r["branch_id"],
                    "current_revision": r["current_revision"],
                    "accepted_revision": r["accepted_revision"],
                    "execution_fence": r["execution_fence"],
                } for r in rows],
                "read_at": _now()}

    def acceptance_commits(self, tenant_id: str, manifest_id: str | None = None,
                           result: str | None = None, limit: int = 50) -> dict:
        sql = ("SELECT * FROM acceptance_commit WHERE tenant_id = ?")
        args: list = [tenant_id]
        if manifest_id:
            sql += " AND manifest_id = ?"
            args.append(manifest_id)
        if result:
            sql += " AND result = ?"
            args.append(result)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            out.append({
                "commit_id": row["commit_id"], "manifest_id": row["manifest_id"],
                "revision": row["revision"],
                "node_ids": [n for n in row["node_ids"].split(",") if n],
                "result": row["result"],
                "reject_detail": json.loads(row["reject_detail"]) if row["reject_detail"] else None,
                "rebased_from_revision": row["rebased_from_revision"],
                "created_at": row["created_at"],
            })
        return {"acceptance_commits": out, "read_at": _now()}
