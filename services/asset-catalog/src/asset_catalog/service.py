"""S10 内核。方案：`plans/S10-asset-catalog.md`。

一句话边界：**S10 记"这一集绑了谁的哪个 revision"，不记"那个 revision 是什么"，
也不记"它现在还有效吗"。**

三件它刻意不做的事（方案 §0.2），代码里能逐条指出来：

1. **不判权利。** `rights_ref` 是四键指针，原样冻结进 revision；`resolve` 恒返回
   `rights_recheck_required: true`，没有关掉它的开关（§4.4 第 1 条）。
2. **不求影响闭包。** `usage` 只返回"哪些 manifest revision 用过这一版"，不返回
   "需要重做哪些集"，也不向 S21 发"资产升级"事件（§4.5）。
3. **不存 canonical。** 表里没有任何"当前该用哪个 owner revision"的列。
   `binding_set.current_revision` 指的是**绑定集自己的**最新 revision，不是资产
   的最新 revision -- 两者同名不同义，这是最容易写错的一处。

表里也没有内容列：`binding_entry` 只有 `owner_hash` 与 `rights_ref` 两枚指针，
上游的内容字段（外观描述、参考图、拓扑、音色样例）在 `deps` 层就被丢弃了，
所以这里就算想抄也抄不到。

**展开只发生在封存时**（§1.2）。`resolve` 读 `binding_expansion`，不重算继承；
没有"运行时展开"（§9 反模式 3）。全剧默认一改，已封存的集不受影响。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    ASSET_TYPES,
    CONSUMER_MANIFEST_USAGE,
    CONTRACT_GAPS,
    DEFAULT_SCOPES,
    ID_PREFIX,
    LIFECYCLE_KINDS,
    MAX_EXPANSION_ROWS,
    MAX_VARIANT_AXES,
    REVISION_STATES,
    SCOPES,
    SCOPE_SPECIFICITY,
    SCHEMA,
    _seed_registry,
)

SOURCE = "S10"
SCHEMA_VERSION = "2.0"
EVENT_BINDING_SET_CREATED = "asset.binding-set.created"
TOPIC_ASSET = "drama.asset-catalog.v2"
HOME_CELL_DEFAULT = "cell_a1"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# ------------------------------------------------------------------ 基础助手

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------------ 纯函数

def role_key_for(asset_type: str, owner_local_id: str) -> str:
    """§1.3：`<asset_type>:<owner_local_id>`，用 **owner 本地 id**。

    用 `asset_id` 会让一次 merge 之后历史 revision 的 `role_key` 指向一个已经
    改名的对象（§9 反模式 11）。owner 本地 id 在 owner 服务里是稳定的。
    """
    return f"{asset_type}:{owner_local_id}"


def parse_role_key(role_key: str) -> tuple[str, str]:
    if not isinstance(role_key, str) or ":" not in role_key:
        raise ServiceError(
            422, "ROLE_KEY_INVALID",
            "role_key must be '<asset_type>:<owner_local_id>' (plan §1.3)",
            details={"role_key": role_key})
    asset_type, _, owner_local_id = role_key.partition(":")
    if not asset_type or not owner_local_id or ":" in owner_local_id:
        # 第二个冒号会让 role_key 在 URL 路径里重新切分（/entries/{scope}/
        # {role_key}），同一个键就能被两个不同字符串指代 —— 拒绝。
        raise ServiceError(
            422, "ROLE_KEY_INVALID",
            "role_key must be '<asset_type>:<owner_local_id>' with no further "
            "colons (plan §1.3)",
            details={"role_key": role_key})
    return asset_type, owner_local_id


def rights_set_hash(rights_refs) -> str:
    """ABS-3：四键全等去重、按四键排序、`sha256(JCS(数组))`。

    这个算法是本服务定的，S02 必须采纳同一份（否则 E30-A3 恒判不一致）。
    它只对**指针**求哈希，不解释任何权利内容 -- S10 没有解释权（§0.2 第 1 条）。
    """
    unique = {}
    for ref in rights_refs:
        if not isinstance(ref, dict):
            continue
        key = (str(ref.get("rights_owner") or ""),
               str(ref.get("rights_id") or ""),
               int(ref.get("rights_revision") or 0),
               str(ref.get("rights_hash") or ""))
        if not all(key[:2]) or key[2] < 1 or not key[3]:
            continue
        unique[key] = {
            "rights_owner": key[0], "rights_id": key[1],
            "rights_revision": key[2], "rights_hash": key[3],
        }
    ordered = [unique[key] for key in sorted(unique)]
    return _sha256_hex(_jcs(ordered))


def _validate_rights_ref(ref) -> dict:
    if not isinstance(ref, dict):
        raise ServiceError(422, "RIGHTS_REF_INVALID",
                           "rights_ref must be an object",
                           details={"rights_ref": ref})
    out = {
        "rights_owner": str(ref.get("rights_owner") or ""),
        "rights_id": str(ref.get("rights_id") or ""),
        "rights_revision": ref.get("rights_revision"),
        "rights_hash": str(ref.get("rights_hash") or ""),
    }
    if not out["rights_owner"] or not out["rights_id"]:
        raise ServiceError(
            422, "RIGHTS_REF_INVALID",
            "rights_ref needs rights_owner and rights_id",
            details={"rights_ref": ref})
    if not isinstance(out["rights_revision"], int) or out["rights_revision"] < 1:
        raise ServiceError(
            422, "RIGHTS_REF_INVALID",
            "rights_ref.rights_revision must be a positive integer",
            details={"rights_ref": ref})
    if len(out["rights_hash"]) != 64:
        raise ServiceError(
            422, "RIGHTS_REF_INVALID",
            "rights_ref.rights_hash must be a 64-char hex digest",
            details={"rights_ref": ref})
    return out


def _validate_variant_selector(value) -> dict:
    """§11.2 第 4 条：**完全不解释**内容，只防止把 payload 撑爆。

    不认识的轴照样存下来并且原样返回；S10 不依赖任何轴名。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServiceError(422, "VARIANT_SELECTOR_INVALID",
                           "variant_selector must be an object")
    if len(value) > MAX_VARIANT_AXES:
        raise ServiceError(
            422, "VARIANT_SELECTOR_INVALID",
            f"variant_selector has more than {MAX_VARIANT_AXES} axes",
            details={"axes": len(value)})
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ServiceError(422, "VARIANT_SELECTOR_INVALID",
                               "variant_selector keys must be non-empty strings")
        if not isinstance(item, (str, int, float, bool)) and item is not None:
            raise ServiceError(
                422, "VARIANT_SELECTOR_INVALID",
                "variant_selector values must be scalars; S10 does not "
                "interpret them and will not store nested structures")
    return dict(value)


def _validate_scope_path(value) -> dict:
    """S10-1：调用方自报的父子关系，只用于一致性校验。

    没有它就无法回答 F9 里的"这个 shot 是不是属于本 scene"——S10 没有 shot
    的权威层级。这里不推断，只记录；同一 shot 被声明到两个 scene 时 422。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServiceError(422, "SCOPE_INCONSISTENT",
                           "scope_path must be an object")
    allowed = {"series_id", "episode_id", "scene_id"}
    unknown = set(value) - allowed
    if unknown:
        raise ServiceError(
            422, "SCOPE_INCONSISTENT",
            f"scope_path has unknown keys: {sorted(unknown)}",
            details={"allowed": sorted(allowed)})
    out = {}
    for key, item in value.items():
        if item is None:
            continue
        if not isinstance(item, str) or not item:
            raise ServiceError(422, "SCOPE_INCONSISTENT",
                               f"scope_path.{key} must be a non-empty string")
        out[key] = item
    return out


# ------------------------------------------------------------------ 内核

class AssetCatalogService:
    def __init__(self, conn: sqlite3.Connection, *, revision_source,
                 asset_status_source=None, manifest_source=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _seed_registry(self.conn)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        self.revision_source = revision_source
        self.asset_status_source = asset_status_source
        self.manifest_source = manifest_source

    # ------------------------------------------------------------ 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    # ------------------------------------------------------------ 读上游
    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """把"上游不可达"统一成 503，**绝不用缓存结论兜底**（§4.6 F1）。

        封存路径宁可失败也不产出悬空指针：一条指向不存在 revision 的绑定会在
        S08 提交时才暴露，那时整条准备流水线已经花完钱（§4.3）。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 读自己的表
    def _binding_set(self, tenant_id: str, binding_set_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM binding_set WHERE tenant_id=? AND binding_set_id=?",
            (tenant_id, binding_set_id)).fetchone()
        if row is None:
            # F10 / E28：跨租户与不存在同码，不泄漏存在性。
            raise ServiceError(404, "NOT_FOUND", "binding set not found")
        return dict(row)

    def _asset(self, tenant_id: str, asset_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM asset_identity WHERE tenant_id=? AND asset_id=?",
            (tenant_id, asset_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "asset not found")
        return dict(row)

    def _entries(self, tenant_id: str, binding_set_id: str,
                 floor: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM binding_entry WHERE tenant_id=? AND binding_set_id=? "
            "AND revision_floor=? ORDER BY scope_kind, scope_id, role_key",
            (tenant_id, binding_set_id, floor)).fetchall()
        return [dict(row) for row in rows]

    def _defaults(self, tenant_id: str, series_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM binding_default WHERE tenant_id=? AND series_id=? "
            "ORDER BY scope_kind, scope_id, role_key",
            (tenant_id, series_id)).fetchall()
        return [dict(row) for row in rows]

    # ================================================================ 资产

    def mint_asset(self, tenant_id: str, command_id: str, body: dict,
                   request_hash: str) -> dict:
        """§4.1。同一 owner 对象重复注册返回同一 `asset_id`，不报错。"""
        asset_type = body.get("asset_type")
        owner_service = body.get("owner_service")
        owner_local_id = body.get("owner_local_id")
        series_scope = body.get("series_scope") or "*"
        aliases = body.get("alias_texts") or []

        spec = ASSET_TYPES.get(asset_type)
        if spec is None:
            raise ServiceError(
                422, "TYPE_OWNER_MISMATCH",
                f"unknown asset_type {asset_type!r}; the type registry is "
                f"deployed with the service and tenants cannot add types "
                f"(plan §2.1)",
                details={"asset_type": asset_type,
                         "known": sorted(ASSET_TYPES)})
        if owner_service != spec["owner_service"]:
            # 不允许给 S14 的道具盖上 S13 的类型（§4.1 第 1 条）。
            raise ServiceError(
                422, "TYPE_OWNER_MISMATCH",
                f"asset_type {asset_type!r} is owned by {spec['owner_service']}, "
                f"not {owner_service!r}",
                details={"asset_type": asset_type,
                         "expected_owner_service": spec["owner_service"],
                         "owner_service": owner_service})
        if not isinstance(owner_local_id, str) or not owner_local_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "owner_local_id is required")
        if not isinstance(series_scope, str) or not series_scope:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "series_scope must be a non-empty string")
        if not isinstance(aliases, list) or any(
                not isinstance(a, str) or not a for a in aliases):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "alias_texts must be a list of non-empty strings")

        def produce() -> dict:
            existing = self.conn.execute(
                "SELECT * FROM asset_identity WHERE tenant_id=? AND "
                "owner_service=? AND owner_local_id=?",
                (tenant_id, owner_service, owner_local_id)).fetchone()
            if existing is not None:
                # 同 owner 对象重复注册：返回既有 ID，`created` 为 False。
                return {"asset": self._asset_out(dict(existing)),
                        "created": False}
            asset_id = _new_id("asset")
            now = _now()
            self.conn.execute(
                "INSERT INTO asset_identity (tenant_id, asset_id, asset_type,"
                " owner_service, owner_local_id, series_scope, status,"
                " merged_into, created_at)"
                " VALUES (?,?,?,?,?,?, 'active', NULL, ?)",
                (tenant_id, asset_id, asset_type, owner_service,
                 owner_local_id, series_scope, now))
            self.conn.execute(
                "INSERT INTO asset_lifecycle_event (tenant_id, asset_id, seq,"
                " event_kind, reason_code, reason_text, actor, occurred_at)"
                " VALUES (?,?,1,'registered',?,?,?,?)",
                (tenant_id, asset_id, "REGISTERED",
                 body.get("reason_text"), str(body.get("actor") or command_id),
                 now))
            for text in sorted(set(aliases)):
                self.conn.execute(
                    "INSERT OR IGNORE INTO asset_alias (tenant_id, asset_id,"
                    " alias_text, alias_kind, source) VALUES (?,?,?,'display',?)",
                    (tenant_id, asset_id, text, str(body.get("alias_source")
                                                     or "S10")))
            self._local_event(tenant_id, "asset.identity.registered", asset_id,
                              {"asset_id": asset_id, "asset_type": asset_type,
                               "owner_service": owner_service,
                               "owner_local_id": owner_local_id})
            return {"asset": self.get_asset(tenant_id, asset_id),
                    "created": True}

        return self._command(tenant_id, command_id, produce)

    def get_asset(self, tenant_id: str, asset_id: str) -> dict:
        """`GET /assets/{id}`：身份与生命周期，**不含内容**（§5.2）。"""
        asset = self._asset(tenant_id, asset_id)
        out = self._asset_out(asset)
        out["aliases"] = [
            dict(row) for row in self.conn.execute(
                "SELECT alias_text, alias_kind, source FROM asset_alias "
                "WHERE tenant_id=? AND asset_id=? ORDER BY alias_text",
                (tenant_id, asset_id)).fetchall()]
        out["lifecycle"] = [
            dict(row) for row in self.conn.execute(
                "SELECT seq, event_kind, reason_code, reason_text, actor,"
                " occurred_at FROM asset_lifecycle_event"
                " WHERE tenant_id=? AND asset_id=? ORDER BY seq",
                (tenant_id, asset_id)).fetchall()]
        # 方案 §3.3：merge 后旧 ID 的 resolve 要给出 `merged_to`，但绝不改写
        # 已提交 manifest 的引用（改写会让 content_hash 与内容不一致）。
        out["rights_recheck_required"] = True
        return out

    @staticmethod
    def _asset_out(asset: dict) -> dict:
        return {
            "asset_id": asset["asset_id"],
            "asset_type": asset["asset_type"],
            "owner_service": asset["owner_service"],
            "owner_local_id": asset["owner_local_id"],
            "role_key": role_key_for(asset["asset_type"],
                                     asset["owner_local_id"]),
            "series_scope": asset["series_scope"],
            "status": asset["status"],
            "merged_into": asset["merged_into"],
            "created_at": asset["created_at"],
            # 没有 canonical/current revision 字段：那是 owner 的状态，不是 S10 的。
        }

    def lookup_asset(self, tenant_id: str, owner_service: str,
                     owner_local_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM asset_identity WHERE tenant_id=? AND owner_service=? "
            "AND owner_local_id=?",
            (tenant_id, owner_service, owner_local_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "asset not found")
        return self.get_asset(tenant_id, row["asset_id"])

    def search_assets(self, tenant_id: str, *, alias_text: str | None = None,
                      asset_type: str | None = None,
                      series_scope: str | None = None,
                      status: str | None = None,
                      limit: int = DEFAULT_LIMIT) -> dict:
        """S10-22：检索投影。**别名只用于检索，不用于绑定解析**（§9 反模式 7）。"""
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        sql = ["SELECT DISTINCT a.* FROM asset_identity a"]
        params: list = []
        if alias_text:
            sql.append("JOIN asset_alias al ON al.tenant_id=a.tenant_id "
                       "AND al.asset_id=a.asset_id")
        where = ["a.tenant_id=?"]
        params.append(tenant_id)
        if alias_text:
            where.append("al.alias_text=?")
            params.append(alias_text)
        if asset_type:
            where.append("a.asset_type=?")
            params.append(asset_type)
        if series_scope:
            where.append("a.series_scope=?")
            params.append(series_scope)
        if status:
            where.append("a.status=?")
            params.append(status)
        sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY a.asset_type, a.asset_id LIMIT ?")
        params.append(limit)
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return {"items": [self._asset_out(dict(row)) for row in rows],
                "limit": limit}

    def _transition(self, tenant_id: str, command_id: str, asset_id: str,
                    body: dict, *, target: str, kind: str) -> dict:
        asset = self._asset(tenant_id, asset_id)
        reason_code = str(body.get("reason_code") or kind.upper())
        reason_text = body.get("reason_text")
        actor = str(body.get("actor") or command_id)

        def produce() -> dict:
            current = asset["status"]
            merged_into = asset["merged_into"]
            if target == "merged_into":
                target_id = body.get("merged_into") or body.get("target_asset_id")
                if not target_id:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       "merged_into target asset_id is required")
                if target_id == asset_id:
                    raise ServiceError(422, "MERGE_TARGET_INVALID",
                                       "an asset cannot be merged into itself")
                target_asset = self.conn.execute(
                    "SELECT * FROM asset_identity WHERE tenant_id=? AND "
                    "asset_id=?", (tenant_id, target_id)).fetchone()
                if target_asset is None:
                    raise ServiceError(422, "MERGE_TARGET_INVALID",
                                       "merge target does not exist")
                if target_asset["asset_type"] != asset["asset_type"]:
                    raise ServiceError(
                        422, "MERGE_TARGET_INVALID",
                        "merge target must have the same asset_type",
                        details={"expected": asset["asset_type"],
                                 "actual": target_asset["asset_type"]})
                if target_asset["status"] != "active":
                    raise ServiceError(
                        422, "MERGE_TARGET_INVALID",
                        "merge target must be active",
                        details={"target_status": target_asset["status"]})
                if current == "tombstoned":
                    raise ServiceError(
                        409, "ILLEGAL_TRANSITION",
                        "a tombstoned asset cannot be merged; revive it with a "
                        "new id and an alias (plan §3.3)")
                merged_into = target_id
                new_status = "merged_into"
            elif target == "tombstoned":
                if current != "deprecated":
                    # §3.3 只给了 deprecated → tombstoned 这条边（S10-14）。
                    raise ServiceError(
                        409, "ILLEGAL_TRANSITION",
                        f"only deprecated assets can be tombstoned; "
                        f"{asset_id} is {current}",
                        details={"status": current})
                new_status = "tombstoned"
            elif target == "deprecated":
                if current != "active":
                    raise ServiceError(
                        409, "ILLEGAL_TRANSITION",
                        f"only active assets can be deprecated; "
                        f"{asset_id} is {current}",
                        details={"status": current})
                new_status = "deprecated"
            elif target == "un_tombstoned":
                raise ServiceError(
                    409, "ILLEGAL_TRANSITION",
                    "tombstoned → active is not allowed; mint a new id and "
                    "keep an alias (plan §3.3)")
            else:  # pragma: no cover - 调用点固定
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"unsupported transition {target}")

            self.conn.execute(
                "UPDATE asset_identity SET status=?, merged_into=? "
                "WHERE tenant_id=? AND asset_id=?",
                (new_status, merged_into, tenant_id, asset_id))
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM asset_lifecycle_event "
                "WHERE tenant_id=? AND asset_id=?",
                (tenant_id, asset_id)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO asset_lifecycle_event (tenant_id, asset_id, seq,"
                " event_kind, reason_code, reason_text, actor, occurred_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, asset_id, seq, kind, reason_code, reason_text,
                 actor, _now()))
            # 生命周期只进本地事件，不发中继（C10-3：topic 未登记）。
            self._local_event(tenant_id, f"asset.identity.{kind}", asset_id,
                              {"asset_id": asset_id, "seq": seq,
                               "event_kind": kind,
                               "merged_into": merged_into})
            return {"asset": self.get_asset(tenant_id, asset_id)}

        return self._command(tenant_id, command_id, produce)

    def deprecate_asset(self, tenant_id, command_id, asset_id, body,
                        request_hash):
        return self._transition(tenant_id, command_id, asset_id, body or {},
                                target="deprecated", kind="deprecated")

    def tombstone_asset(self, tenant_id, command_id, asset_id, body,
                        request_hash):
        return self._transition(tenant_id, command_id, asset_id, body or {},
                                target="tombstoned", kind="tombstoned")

    def merge_asset(self, tenant_id, command_id, asset_id, body, request_hash):
        return self._transition(tenant_id, command_id, asset_id, body or {},
                                target="merged_into", kind="merged")

    # ------------------------------------------------------------ 本地事件
    def _local_event(self, tenant_id: str, event_type: str,
                     aggregate_id: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO local_event (tenant_id, local_event_id, event_type,"
            " aggregate_id, payload, created_at) VALUES (?,?,?,?,?,?)",
            (tenant_id, _new_id("local_event"), event_type, aggregate_id,
             _jcs(payload), _now()))

    # ============================================================ BindingSet

    def create_binding_set(self, tenant_id: str, command_id: str, body: dict,
                           request_hash: str) -> dict:
        """§5.1：同 `(series, episode, branch)` 返回既有容器，不报错。"""
        project_id = _require_str(body, "project_id")
        series_id = _require_str(body, "series_id")
        episode_id = _require_str(body, "episode_id")
        branch_id = _require_str(body, "branch_id")
        created_by = body.get("created_by") or command_id

        def produce() -> dict:
            existing = self.conn.execute(
                "SELECT * FROM binding_set WHERE tenant_id=? AND series_id=? "
                "AND episode_id=? AND branch_id=?",
                (tenant_id, series_id, episode_id, branch_id)).fetchone()
            if existing is not None:
                return {"binding_set": self._binding_out(dict(existing)),
                        "created": False}
            binding_set_id = _new_id("binding_set")
            self.conn.execute(
                "INSERT INTO binding_set (tenant_id, binding_set_id, project_id,"
                " series_id, episode_id, branch_id, head_revision,"
                " current_revision, state, archived_at, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,0,NULL,'OPEN',NULL,?,?)",
                (tenant_id, binding_set_id, project_id, series_id, episode_id,
                 branch_id, _jcs(created_by) if not isinstance(created_by, str)
                 else created_by, _now()))
            return {"binding_set": self.get_binding_set(tenant_id,
                                                        binding_set_id),
                    "created": True}

        return self._command(tenant_id, command_id, produce)

    @staticmethod
    def _binding_out(row: dict) -> dict:
        return {
            "binding_set_id": row["binding_set_id"],
            "project_id": row["project_id"],
            "series_id": row["series_id"],
            "episode_id": row["episode_id"],
            "branch_id": row["branch_id"],
            "head_revision": row["head_revision"],
            "current_revision": row["current_revision"],
            "state": row["state"],
            "archived_at": row["archived_at"],
            "created_at": row["created_at"],
        }

    def get_binding_set(self, tenant_id: str, binding_set_id: str) -> dict:
        row = self._binding_set(tenant_id, binding_set_id)
        out = self._binding_out(row)
        out["draft_entries"] = [
            _entry_out(entry) for entry in
            self._entries(tenant_id, binding_set_id, row["head_revision"] + 1)]
        return out

    def list_binding_sets(self, tenant_id: str, *, series_id=None,
                          episode_id=None, branch_id=None,
                          limit: int = DEFAULT_LIMIT) -> dict:
        """`GET /binding-sets`。无范围参数的列表在数据长大后就是事故，
        所以 `series_id` 是必填的。"""
        if not series_id:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "series_id is required: an unscoped binding-set list stops "
                "being answerable once the tenant has more than one series")
        limit = _clamp_limit(limit)
        sql = ["SELECT * FROM binding_set WHERE tenant_id=? AND series_id=?"]
        params: list = [tenant_id, series_id]
        if episode_id:
            sql.append("AND episode_id=?")
            params.append(episode_id)
        if branch_id:
            sql.append("AND branch_id=?")
            params.append(branch_id)
        sql.append("ORDER BY episode_id, branch_id LIMIT ?")
        params.append(limit)
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return {"items": [self._binding_out(dict(row)) for row in rows],
                "limit": limit}

    # ------------------------------------------------------------- 草稿条目

    def write_entry(self, tenant_id: str, command_id: str, binding_set_id: str,
                    body: dict, request_hash: str) -> dict:
        """§4.2。不校验 owner revision 是否存在（草稿允许先搭骨架），
        但记录 `unverified: true`，并在封存时补齐验证。"""
        scope_kind = body.get("scope_kind")
        if scope_kind not in SCOPES:
            raise ServiceError(422, "SCOPE_INCONSISTENT",
                               f"scope_kind must be one of {list(SCOPES)}",
                               details={"scope_kind": scope_kind})
        scope_id = _require_str(body, "scope_id")
        role_key = _require_str(body, "role_key")
        asset_type, owner_local_id = parse_role_key(role_key)
        spec = ASSET_TYPES.get(asset_type)
        if spec is None:
            raise ServiceError(
                422, "ROLE_KEY_INVALID",
                f"role_key names asset_type {asset_type!r}, which is not in the "
                f"deployed type registry",
                details={"asset_type": asset_type,
                         "known": sorted(ASSET_TYPES)})
        if "owner_service" in body and body["owner_service"] != spec[
                "owner_service"]:
            raise ServiceError(
                422, "TYPE_OWNER_MISMATCH",
                f"asset_type {asset_type!r} is owned by "
                f"{spec['owner_service']}, not {body['owner_service']!r}",
                details={"expected_owner_service": spec["owner_service"]})
        owner_revision = body.get("owner_revision")
        if not isinstance(owner_revision, int) or owner_revision < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "owner_revision must be a positive integer")
        owner_hash = _require_sha256(body, "owner_hash")
        rights_ref = _validate_rights_ref(body.get("rights_ref"))
        variant = _validate_variant_selector(body.get("variant_selector"))
        scope_path = _validate_scope_path(body.get("scope_path"))
        required = 1 if body.get("required", True) else 0
        note = body.get("note")
        expected_head = _require_head(body)

        def produce() -> dict:
            container = self._binding_set(tenant_id, binding_set_id)
            self._require_open(container)
            self._require_head(container, expected_head)
            floor = container["head_revision"] + 1
            self._check_scope_path(tenant_id, binding_set_id, scope_kind,
                                   scope_id, scope_path, floor)
            # 草稿阶段也拒绝已经墓碑的资产：那不是"先搭骨架"，是明知不可用。
            identity = self.conn.execute(
                "SELECT status, merged_into FROM asset_identity WHERE tenant_id=? "
                "AND owner_service=? AND owner_local_id=?",
                (tenant_id, spec["owner_service"], owner_local_id)).fetchone()
            if identity is not None:
                if identity["status"] == "tombstoned":
                    raise ServiceError(
                        422, "ASSET_TOMBSTONED",
                        f"{role_key} is tombstoned; a new binding cannot point "
                        f"at it (plan §2.7)",
                        details={"role_key": role_key})
                if identity["status"] == "merged_into":
                    raise ServiceError(
                        422, "ASSET_MERGED",
                        f"{role_key} was merged into {identity['merged_into']}; "
                        f"bind the target instead (plan §3.3)",
                        details={"role_key": role_key,
                                 "merged_into": identity["merged_into"]})
            try:
                self.conn.execute(
                    "INSERT INTO binding_entry (tenant_id, binding_set_id,"
                    " revision_floor, scope_kind, scope_id, role_key, asset_type,"
                    " owner_service, owner_local_id, owner_revision, owner_hash,"
                    " rights_ref, variant_selector, required, note, scope_path,"
                    " unverified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (tenant_id, binding_set_id, floor, scope_kind, scope_id,
                     role_key, asset_type, spec["owner_service"],
                     owner_local_id, owner_revision, owner_hash,
                     _jcs(rights_ref), _jcs(variant), required, note,
                     _jcs(scope_path)))
            except sqlite3.IntegrityError as exc:
                raise ServiceError(
                    409, "ENTRY_ALREADY_SET",
                    f"{role_key} at {scope_kind}/{scope_id} is already set for "
                    f"revision {floor}; S10 will not silently overwrite it "
                    f"(plan §4.2)",
                    details={"scope_kind": scope_kind, "scope_id": scope_id,
                             "role_key": role_key, "revision_floor": floor}) from exc
            return {"binding_set_id": binding_set_id,
                    "revision_floor": floor,
                    "entry": _entry_out(self.conn.execute(
                        "SELECT * FROM binding_entry WHERE tenant_id=? AND"
                        " binding_set_id=? AND revision_floor=? AND scope_kind=?"
                        " AND scope_id=? AND role_key=?",
                        (tenant_id, binding_set_id, floor, scope_kind, scope_id,
                         role_key)).fetchone())}

        return self._command(tenant_id, command_id, produce)

    def delete_entry(self, tenant_id: str, command_id: str,
                     binding_set_id: str, scope_kind: str, scope_id: str,
                     role_key: str, body: dict, request_hash: str) -> dict:
        body = body or {}
        if scope_kind not in SCOPES:
            raise ServiceError(422, "SCOPE_INCONSISTENT",
                               f"scope_kind must be one of {list(SCOPES)}")
        expected_head = _require_head(body)

        def produce() -> dict:
            container = self._binding_set(tenant_id, binding_set_id)
            self._require_open(container)
            self._require_head(container, expected_head)
            floor = container["head_revision"] + 1
            cur = self.conn.execute(
                "DELETE FROM binding_entry WHERE tenant_id=? AND binding_set_id=?"
                " AND revision_floor=? AND scope_kind=? AND scope_id=? "
                "AND role_key=?",
                (tenant_id, binding_set_id, floor, scope_kind, scope_id,
                 role_key))
            if cur.rowcount == 1:
                return {"deleted": True, "revision_floor": floor}
            # S10-2：继承来的条目（更低 floor）不能靠删草稿移除 —— 它属于
            # 已封存的历史。移除只能通过换 owner 后封新 revision。
            inherited = self.conn.execute(
                "SELECT 1 FROM binding_entry WHERE tenant_id=? AND"
                " binding_set_id=? AND scope_kind=? AND scope_id=? AND"
                " role_key=? LIMIT 1",
                (tenant_id, binding_set_id, scope_kind, scope_id,
                 role_key)).fetchone()
            if inherited is not None:
                raise ServiceError(
                    409, "ENTRY_NOT_EDITABLE",
                    f"{role_key} at {scope_kind}/{scope_id} comes from an "
                    f"already-sealed revision and cannot be deleted; seal a new "
                    f"revision with a different owner instead (CONTRACT_GAPS S10-2)",
                    details={"scope_kind": scope_kind, "scope_id": scope_id,
                             "role_key": role_key})
            raise ServiceError(404, "NOT_FOUND", "binding entry not found")

        return self._command(tenant_id, command_id, produce)

    def set_default(self, tenant_id: str, command_id: str,
                    binding_set_id: str, body: dict, request_hash: str) -> dict:
        """S10-8：全剧/集/场默认值。**只影响下一次封存**（§2.5）。"""
        container = self._binding_set(tenant_id, binding_set_id)
        scope_kind = body.get("scope_kind") or "series"
        if scope_kind not in DEFAULT_SCOPES:
            raise ServiceError(
                422, "SCOPE_INCONSISTENT",
                f"defaults exist only for {list(DEFAULT_SCOPES)}; a shot-level "
                f"binding must be explicit (plan §2.5)")
        scope_id = body.get("scope_id") or {
            "series": container["series_id"],
            "episode": container["episode_id"],
        }.get(scope_kind)
        scope_id = _require_str({"scope_id": scope_id}, "scope_id")
        role_key = _require_str(body, "role_key")
        asset_type, owner_local_id = parse_role_key(role_key)
        spec = ASSET_TYPES.get(asset_type)
        if spec is None:
            raise ServiceError(422, "ROLE_KEY_INVALID",
                               f"unknown asset_type {asset_type!r}",
                               details={"asset_type": asset_type})
        owner_revision = body.get("owner_revision")
        if not isinstance(owner_revision, int) or owner_revision < 1:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "owner_revision must be a positive integer")
        owner_hash = _require_sha256(body, "owner_hash")
        rights_ref = _validate_rights_ref(body.get("rights_ref"))
        variant = _validate_variant_selector(body.get("variant_selector"))

        def produce() -> dict:
            self.conn.execute(
                "INSERT INTO binding_default (tenant_id, series_id, scope_kind,"
                " scope_id, role_key, asset_type, owner_service, owner_local_id,"
                " owner_revision, owner_hash, rights_ref, variant_selector,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (tenant_id, series_id, scope_kind, scope_id,"
                " role_key) DO UPDATE SET asset_type=excluded.asset_type,"
                " owner_service=excluded.owner_service,"
                " owner_local_id=excluded.owner_local_id,"
                " owner_revision=excluded.owner_revision,"
                " owner_hash=excluded.owner_hash,"
                " rights_ref=excluded.rights_ref,"
                " variant_selector=excluded.variant_selector,"
                " updated_at=excluded.updated_at",
                (tenant_id, container["series_id"], scope_kind, scope_id,
                 role_key, asset_type, spec["owner_service"], owner_local_id,
                 owner_revision, owner_hash, _jcs(rights_ref), _jcs(variant),
                 _now()))
            return {"series_id": container["series_id"],
                    "scope_kind": scope_kind, "scope_id": scope_id,
                    "role_key": role_key,
                    "affects": "next_seal_only"}

        return self._command(tenant_id, command_id, produce)

    # --------------------------------------------------------------- 封存

    def seal(self, tenant_id: str, command_id: str, binding_set_id: str,
             body: dict, request_hash: str) -> dict:
        """§4.3 的九步，唯一一处顺序不能换的地方。

        与方案唯一的偏差是**第 3 步不在行锁里**：owner HTTP 放在两次短事务
        之间（§9 反模式 10）。第二次事务重读 head 做 CAS，所以"校验期间有人
        改了草稿"不会写出一个没校验过的 revision。

        顺带一个直接后果：`verify_owners=false` 时第二次事务里的 pointers 与
        第一次一致，不需要再校验；`import_mode` 把它标成 `deferred` 而不是
        `verified`（§4.3）。
        """
        expected_head = _require_head(body)
        verify_owners = body.get("verify_owners", True)
        import_mode = body.get("import_mode", False)
        sealed_by = body.get("sealed_by") or command_id
        if not isinstance(verify_owners, bool) or not isinstance(import_mode,
                                                                 bool):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "verify_owners and import_mode must be booleans")
        if not verify_owners:
            # §4.3：允许 false 的唯一场景是离线导入/灾备重建，且必须带授权。
            if not import_mode or not body.get("authorization"):
                raise ServiceError(
                    422, "IMPORT_NOT_AUTHORIZED",
                    "verify_owners=false requires import_mode=true and an "
                    "explicit authorization (plan §4.3); S10 will not seal an "
                    "unverified binding by accident")

        # ---- 阶段一：短事务里快照草稿（不持锁做 HTTP）
        with Transaction(self.conn):
            container = self._binding_set(tenant_id, binding_set_id)
            self._require_open(container)
            self._require_head(container, expected_head)
            floor = container["head_revision"] + 1
            overlay = self._overlay(tenant_id, binding_set_id, floor)
            defaults = self._defaults(tenant_id, container["series_id"])
        expansion = self._compute_expansion(container, overlay, defaults)
        if len(expansion) > MAX_EXPANSION_ROWS:
            raise ServiceError(
                422, "EXPANSION_TOO_LARGE",
                f"expansion has {len(expansion)} rows, above the "
                f"{MAX_EXPANSION_ROWS} limit (CONTRACT_GAPS S10-17)",
                details={"rows": len(expansion)})

        # ---- 阶段二：所有者校验（HTTP，事务外）
        verification = "deferred" if not verify_owners else "verified"
        pointers: dict = {}
        if verify_owners:
            pointers = self._verify_expansion(tenant_id, expansion)

        # ---- 阶段三：写入（重新 CAS head）
        def produce() -> dict:
            container2 = self._binding_set(tenant_id, binding_set_id)
            self._require_open(container2)
            self._require_head(container2, expected_head)
            floor2 = container2["head_revision"] + 1
            overlay2 = self._overlay(tenant_id, binding_set_id, floor2)
            defaults2 = self._defaults(tenant_id, container2["series_id"])
            if (floor2 != floor or _overlay_fingerprint(overlay2)
                    != _overlay_fingerprint(overlay)
                    or _overlay_fingerprint(defaults2)
                    != _overlay_fingerprint(defaults)):
                raise ServiceError(
                    409, "VERSION_CONFLICT",
                    "the draft changed while owner revisions were being "
                    "verified; re-read and seal again",
                    retryable=True, details={"head": container2["head_revision"]})
            rows = self._compute_expansion(container2, overlay2, defaults2)
            for row in rows:
                if not verify_owners:
                    row["unverified"] = 1
                    continue
                key = (row["owner_service"], row["owner_local_id"],
                       row["owner_revision"])
                refreshed = pointers.get(key)
                if refreshed is None:  # pragma: no cover - 两轮展开同构
                    raise ServiceError(
                        503, "DEPENDENCY_UNAVAILABLE",
                        "an owner revision was not verified; refusing to seal",
                        retryable=True)
                if refreshed["content_hash"] != row["owner_hash"]:
                    # F3：提交的 hash 与 owner 现在说的不一致。
                    raise ServiceError(
                        422, "OWNER_REF_INVALID",
                        f"{row['owner_service']} {row['owner_local_id']}@{row['owner_revision']}"
                        f" hashes to {refreshed['content_hash']}, not the "
                        f"submitted {row['owner_hash']}",
                        details={"service_id": row["owner_service"],
                                 "id": row["owner_local_id"],
                                 "revision": row["owner_revision"],
                                 "reason": "HASH_MISMATCH"})

            rights_refs = [row["rights_ref"] for row in rows]
            rights_hash = rights_set_hash(rights_refs)
            payload = {
                "binding_set_id": binding_set_id,
                "project_id": container2["project_id"],
                "series_id": container2["series_id"],
                "episode_id": container2["episode_id"],
                "branch_id": container2["branch_id"],
                "entries": [
                    {"scope_kind": row["scope_kind"],
                     "scope_id": row["scope_id"],
                     "role_key": row["role_key"],
                     "asset_type": row["asset_type"],
                     "owner_service": row["owner_service"],
                     "owner_local_id": row["owner_local_id"],
                     "owner_revision": row["owner_revision"],
                     "owner_hash": row["owner_hash"],
                     "rights_ref": row["rights_ref"],
                     "variant_selector": row["variant_selector"],
                     "resolved_from": row["resolved_from"],
                     "required": row["required"]}
                    for row in rows],
                "rights_pointer_set_hash": rights_hash,
                "expanded_from": _expanded_from(rows, verification),
            }
            digest = _sha256_hex(_jcs(payload))
            revision = container2["head_revision"] + 1
            prior = container2["current_revision"]
            try:
                self.conn.execute(
                    "INSERT INTO binding_set_revision (tenant_id,"
                    " binding_set_id, revision, state, content_hash, payload,"
                    " entry_count, expanded_from, rights_pointer_set,"
                    " base_revision, sealed_by, sealed_at)"
                    " VALUES (?,?,?,'SEALED',?,?,?,?,?,?,?,?)",
                    (tenant_id, binding_set_id, revision, digest, _jcs(payload),
                     len(rows), _jcs(payload["expanded_from"]),
                     _jcs(_rights_pointer_set(rows)), expected_head,
                     str(sealed_by), _now()))
            except sqlite3.IntegrityError as exc:
                # F7 / E12-A4：同一内容不能有两个 revision。
                existing = self.conn.execute(
                    "SELECT revision FROM binding_set_revision WHERE tenant_id=?"
                    " AND content_hash=?", (tenant_id, digest)).fetchone()
                raise ServiceError(
                    409, "DUPLICATE_BINDING_CONTENT",
                    "a binding revision with identical content already exists",
                    details={"existing_revision": existing["revision"]
                             if existing else None}) from exc
            if prior is not None:
                self.conn.execute(
                    "UPDATE binding_set_revision SET state='SUPERSEDED' "
                    "WHERE tenant_id=? AND binding_set_id=? AND revision=? "
                    "AND state='SEALED'",
                    (tenant_id, binding_set_id, prior))
            self.conn.executemany(
                "INSERT INTO binding_expansion (tenant_id, binding_set_id,"
                " revision, scope_kind, scope_id, role_key, asset_type,"
                " owner_service, owner_local_id, owner_revision, owner_hash,"
                " rights_ref, variant_selector, resolved_from, required)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(tenant_id, binding_set_id, revision, row["scope_kind"],
                  row["scope_id"], row["role_key"], row["asset_type"],
                  row["owner_service"], row["owner_local_id"],
                  row["owner_revision"], row["owner_hash"],
                  _jcs(row["rights_ref"]), _jcs(row["variant_selector"]),
                  row["resolved_from"], row["required"]) for row in rows])
            self.conn.execute(
                "UPDATE binding_set SET head_revision=?, current_revision=? "
                "WHERE tenant_id=? AND binding_set_id=?",
                (revision, revision, tenant_id, binding_set_id))
            self._emit_sealed(tenant_id, command_id, binding_set_id, revision,
                              digest, container2, rows, rights_hash, prior,
                              body)
            return self.get_revision(tenant_id, binding_set_id, revision)

        return self._command(tenant_id, command_id, produce)

    def _emit_sealed(self, tenant_id, command_id, binding_set_id, revision,
                     digest, container, rows, rights_hash, prior, body) -> None:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["owner_service"]] = counts.get(row["owner_service"], 0) + 1
        data = {
            "schema_version": SCHEMA_VERSION,
            # 契约字段（asset.binding-set-created.v2.schema.json）
            "binding_set_ref": {"service_id": SOURCE, "id": binding_set_id,
                                "revision": revision, "content_hash": digest},
            "series_id": container["series_id"],
            "episode_id": container["episode_id"],
            "branch_id": container["branch_id"],
            # ABS-4：数的是**展开结果**，不是 overlay。
            "entry_count": len(rows),
            "owner_service_counts": counts,
            "rights_revision_set_hash": rights_hash,
            # 方案 §5.3 的其余字段（契约里没有，但下游需要坐标与命令）。
            "project_id": container["project_id"],
            "command_id": command_id,
            "sealed_at": _now(),
            "verification": "deferred" if body.get("verify_owners", True)
            is False else "verified",
        }
        if prior is not None:
            data["superseded_revision"] = prior
        # ABS-5：首版没有旧 revision，就不发一个编造的 0，宁可省略。
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_BINDING_SET_CREATED,
            subject=f"binding-set/{binding_set_id}",
            tenant_id=tenant_id,
            home_cell=str(body.get("home_cell") or HOME_CELL_DEFAULT),
            aggregate_id=binding_set_id, sequence=revision,
            correlation_id=str(body.get("correlation_id") or command_id),
            causation_id=command_id, data=data))

    # ------------------------------------------------------------- 归档

    def archive(self, tenant_id: str, command_id: str, binding_set_id: str,
                body: dict, request_hash: str) -> dict:
        """S10-15：S10 没有"S08 确认无未完成分支"的查询，不假装验证过。"""
        body = body or {}
        if body.get("confirm_no_open_branches") is not True:
            raise ServiceError(
                409, "ARCHIVE_NOT_CONFIRMED",
                "archive requires confirm_no_open_branches=true from the "
                "caller that actually checked S08 (CONTRACT_GAPS S10-15)")

        def produce() -> dict:
            container = self._binding_set(tenant_id, binding_set_id)
            if container["state"] == "ARCHIVED":
                return {"binding_set": self._binding_out(container),
                        "archived": False}
            self.conn.execute(
                "UPDATE binding_set SET state='ARCHIVED', archived_at=? "
                "WHERE tenant_id=? AND binding_set_id=?",
                (_now(), tenant_id, binding_set_id))
            return {"binding_set": self.get_binding_set(tenant_id,
                                                        binding_set_id),
                    "archived": True}

        return self._command(tenant_id, command_id, produce)

    # ------------------------------------------------------------- 读路径

    def get_revision(self, tenant_id: str, binding_set_id: str,
                     revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM binding_set_revision WHERE tenant_id=? AND "
            "binding_set_id=? AND revision=?",
            (tenant_id, binding_set_id, int(revision))).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "binding revision not found")
        return _revision_out(dict(row))

    def resolve(self, tenant_id: str, asset_id: str, *,
                series_id: str | None = None, episode_id: str | None = None,
                branch_id: str | None = None, scene_id: str | None = None,
                shot_id: str | None = None,
                binding_set_id: str | None = None,
                binding_set_revision: int | None = None,
                variant_role: str | None = None) -> dict:
        """§4.4。**只读已物化的展开行**，从不回读 entry、从不重算继承。"""
        asset = self._asset(tenant_id, asset_id)
        if binding_set_id:
            container = self._binding_set(tenant_id, binding_set_id)
        else:
            if not (series_id and episode_id and branch_id):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "resolve needs binding_set_id or "
                    "series_id+episode_id+branch_id")
            row = self.conn.execute(
                "SELECT * FROM binding_set WHERE tenant_id=? AND series_id=? "
                "AND episode_id=? AND branch_id=?",
                (tenant_id, series_id, episode_id, branch_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND", "binding set not found")
            container = dict(row)

        is_preview = binding_set_revision is None
        if is_preview:
            revision = container["current_revision"]
            if revision is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    "this binding set has no sealed revision yet; preview "
                    "cannot be served from an empty draft")
        else:
            revision = int(binding_set_revision)
            self.get_revision(tenant_id, container["binding_set_id"], revision)

        scope_kind, scope_id = _requested_scope(container, scene_id, shot_id)
        rows = self._expansion_at(tenant_id, container, revision, scope_kind,
                                  scope_id)
        if variant_role:
            rows = [row for row in rows if row["role_key"] == variant_role]

        entries = [{
            "role_key": row["role_key"],
            "owner_ref": {"service_id": row["owner_service"],
                          "id": row["owner_local_id"],
                          "revision": row["owner_revision"],
                          "content_hash": row["owner_hash"]},
            "variant_selector": row["variant_selector"],
            "rights_ref": row["rights_ref"],
            "resolved_from": row["resolved_from"],
        } for row in rows]

        resolved_roles = {row["role_key"] for row in rows}
        required_roles = self._required_roles(tenant_id, container,
                                              container["binding_set_id"],
                                              revision)
        unresolved = sorted(required_roles - resolved_roles)
        if variant_role:
            unresolved = [role for role in unresolved if role == variant_role]

        revision_row = self.conn.execute(
            "SELECT expanded_from FROM binding_set_revision WHERE tenant_id=? "
            "AND binding_set_id=? AND revision=?",
            (tenant_id, container["binding_set_id"], revision)).fetchone()
        expanded_from = _loads(revision_row["expanded_from"], {}) \
            if revision_row is not None else {}
        projection = self._projection_state(tenant_id, container)

        out = {
            "binding_set_ref": {
                "service_id": SOURCE,
                "id": container["binding_set_id"],
                "revision": revision,
                "content_hash": self.conn.execute(
                    "SELECT content_hash FROM binding_set_revision WHERE "
                    "tenant_id=? AND binding_set_id=? AND revision=?",
                    (tenant_id, container["binding_set_id"],
                     revision)).fetchone()[0],
            },
            "asset_ref": _asset_ref(asset, rows),
            "resolution_scope": {"episode_id": container["episode_id"],
                                 "scene_id": scope_id if scope_kind == "scene"
                                 else scene_id,
                                 "shot_id": scope_id if scope_kind == "shot"
                                 else shot_id},
            "entries": entries,
            # §4.4 第 2 条：缺什么必须说，否则调用方会把"缺音色"当成
            # "这个镜头不需要音色"，然后交给模型脑补。
            "unresolved_required_roles": unresolved,
            # §4.4 第 1 条：恒为 true，没有开关。
            "rights_recheck_required": True,
            "source_revision": revision,
            "projection_watermark": projection["watermark"],
            "verification": expanded_from.get("owner_verification", "verified"),
            "is_preview": is_preview,
        }
        if is_preview:
            out["preview_basis"] = "current_revision"
        if asset["status"] == "merged_into":
            # §3.3：给 merged_to，但绝不改写已提交 manifest 的引用。
            out["merged_to"] = asset["merged_into"]
        return out

    def _expansion_at(self, tenant_id: str, container: dict, revision: int,
                      scope_kind: str | None, scope_id: str | None) -> list:
        """按请求的 scope 取已物化的展开行；取不到就**只**沿已物化的祖先层
        找，绝不回读 entry。"""
        for kind, sid in _scope_ladder(container, scope_kind, scope_id):
            rows = self.conn.execute(
                "SELECT * FROM binding_expansion WHERE tenant_id=? AND"
                " binding_set_id=? AND revision=? AND scope_kind=? AND"
                " scope_id=? ORDER BY role_key",
                (tenant_id, container["binding_set_id"], revision, kind,
                 sid)).fetchall()
            if rows:
                return [_expansion_out(dict(row)) for row in rows]
        return []

    def _required_roles(self, tenant_id: str, container: dict,
                        binding_set_id: str, revision: int) -> set:
        """F8：必需 role 的集合来自 overlay + 默认值，不由展开结果的空缺反推。

        S10-12：`required_for` 词表不存在，所以这里只认 `binding_entry.required`
        与"默认值一律必需"（S10-9）。
        """
        required: set = set()
        rows = self.conn.execute(
            "SELECT role_key, required FROM binding_entry WHERE tenant_id=? AND"
            " binding_set_id=? AND revision_floor<=?",
            (tenant_id, binding_set_id, revision)).fetchall()
        for row in rows:
            if row["required"]:
                required.add(row["role_key"])
        for row in self.conn.execute(
                "SELECT role_key FROM binding_default WHERE tenant_id=? AND"
                " series_id=?", (tenant_id, container["series_id"])).fetchall():
            required.add(row["role_key"])
        return required

    def usage(self, tenant_id: str, binding_set_id: str,
              revision: int) -> dict:
        """§4.5：**种子，不是闭包**。返回体里刻意没有"要不要重做"的字段。"""
        container = self._binding_set(tenant_id, binding_set_id)
        self.get_revision(tenant_id, binding_set_id, revision)
        rows = self.conn.execute(
            "SELECT manifest_id, manifest_revision, episode_id, branch_id,"
            " first_seen_at FROM binding_usage WHERE tenant_id=? AND"
            " binding_set_id=? AND revision=? ORDER BY manifest_id,"
            " manifest_revision",
            (tenant_id, binding_set_id, int(revision))).fetchall()
        projection = self._projection_state(tenant_id, container)
        return {
            "binding_set_id": binding_set_id,
            "revision": int(revision),
            "items": [{"manifest_id": row["manifest_id"],
                       "manifest_revision": row["manifest_revision"],
                       "episode_id": row["episode_id"],
                       "branch_id": row["branch_id"],
                       "first_seen_at": row["first_seen_at"]}
                      for row in rows],
            # §9 反模式 9：投影落后时必须说出来。
            "stale": projection["stale"],
            "stale_reason": projection["stale_reason"],
            "projection_watermark": projection["watermark"],
        }

    def _projection_state(self, tenant_id: str, container: dict) -> dict:
        row = self.conn.execute(
            "SELECT * FROM usage_projection_state WHERE tenant_id=? AND"
            " series_id=? AND episode_id=? AND branch_id=?",
            (tenant_id, container["series_id"], container["episode_id"],
             container["branch_id"])).fetchone()
        if row is None:
            return {"stale": False, "stale_reason": None,
                    "watermark": "1970-01-01T00:00:00.000Z",
                    "applied_sequence": 0}
        return {"stale": bool(row["stale"]),
                "stale_reason": row["stale_reason"],
                "watermark": row["last_event_at"],
                "applied_sequence": row["applied_sequence"]}

    # ------------------------------------------------------------ 使用投影

    def ingest_manifest_committed(self, envelope: dict) -> dict:
        """`snapshot.manifest.committed` → `binding_usage` 投影。

        事件的 payload **没有** `binding_set_ref`（`CONTRACT_GAPS` S10-11），
        所以两条来源：事件里有就直接用，没有就回源 S08。两条都没有时只记缺口
        （把该集标 stale），不阻塞任何写路径 —— S08 不是启动依赖（C10-7）。
        """
        tenant_id = str(envelope.get("tenant_id") or "")
        event_id = str(envelope.get("id") or "")
        if not tenant_id or not event_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "envelope needs tenant_id and id")
        if not self.inbox.consume(CONSUMER_MANIFEST_USAGE, event_id):
            return {"consumed": False, "reason": "DUPLICATE"}
        data = envelope.get("data") or {}
        manifest_ref = data.get("manifest_ref") or {}
        manifest_id = (manifest_ref.get("id") or data.get("manifest_id")
                       or envelope.get("aggregate_id"))
        manifest_revision = manifest_ref.get("revision")
        if manifest_revision is None:
            manifest_revision = data.get("revision") or envelope.get("sequence")
        if not manifest_id or manifest_revision is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "manifest event carries no manifest id/revision")
        manifest_revision = int(manifest_revision)

        binding_ref = data.get("binding_set_ref")
        coordinates = {key: data.get(key) for key in
                       ("project_id", "series_id", "episode_id", "branch_id")}
        source = "event"
        if not _valid_ref(binding_ref) or not all(coordinates.values()):
            fetched = self._fetch_manifest(tenant_id, str(manifest_id),
                                           manifest_revision)
            if fetched is None:
                self._mark_stale(tenant_id, coordinates,
                                 "S08_UNAVAILABLE_AND_EVENT_HAS_NO_BINDING_REF")
                return {"consumed": True, "projected": False,
                        "reason": "BINDING_REF_UNAVAILABLE"}
            source = "s08"
            binding_ref = fetched.get("binding_set_ref") or binding_ref
            for key in coordinates:
                coordinates[key] = coordinates[key] or fetched.get(key)
        if not _valid_ref(binding_ref):
            self._mark_stale(tenant_id, coordinates, "BINDING_REF_MISSING")
            return {"consumed": True, "projected": False,
                    "reason": "BINDING_REF_MISSING"}

        container = self.conn.execute(
            "SELECT * FROM binding_set WHERE tenant_id=? AND binding_set_id=?",
            (tenant_id, binding_ref["id"])).fetchone()
        if container is None:
            self._mark_stale(tenant_id, coordinates, "UNKNOWN_BINDING_SET")
            return {"consumed": True, "projected": False,
                    "reason": "UNKNOWN_BINDING_SET"}

        revision = int(binding_ref["revision"])
        sequence = envelope.get("sequence")
        applied = self.conn.execute(
            "SELECT applied_sequence FROM usage_projection_state WHERE"
            " tenant_id=? AND series_id=? AND episode_id=? AND branch_id=?",
            (tenant_id, container["series_id"], container["episode_id"],
             container["branch_id"])).fetchone()
        # S10-10：不做假缓冲。乱序照常落库（PK 含 manifest_revision，两版都留），
        # 但把投影标成 stale，让调用方知道它不完整。
        out_of_order = (isinstance(sequence, int) and applied is not None
                        and sequence < applied["applied_sequence"])
        self.conn.execute(
            "INSERT OR IGNORE INTO binding_usage (tenant_id, binding_set_id,"
            " revision, manifest_id, manifest_revision, project_id, series_id,"
            " episode_id, branch_id, first_seen_at, source_event_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, container["binding_set_id"], revision,
             str(manifest_id), manifest_revision,
             str(coordinates.get("project_id") or container["project_id"]),
             container["series_id"], container["episode_id"],
             container["branch_id"], _now(), event_id))
        self.conn.execute(
            "INSERT INTO usage_projection_state (tenant_id, series_id,"
            " episode_id, branch_id, applied_sequence, last_event_at, stale,"
            " stale_reason) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT (tenant_id, series_id, episode_id, branch_id)"
            " DO UPDATE SET applied_sequence=MAX(applied_sequence,"
            " excluded.applied_sequence), last_event_at=excluded.last_event_at,"
            " stale=excluded.stale, stale_reason=excluded.stale_reason",
            (tenant_id, container["series_id"], container["episode_id"],
             container["branch_id"],
             int(sequence) if isinstance(sequence, int) else 0,
             _now(), 1 if out_of_order else 0,
             "OUT_OF_ORDER_SEQUENCE" if out_of_order else None))
        return {"consumed": True, "projected": True, "source": source,
                "binding_set_id": container["binding_set_id"],
                "revision": revision, "stale": out_of_order}

    def _fetch_manifest(self, tenant_id: str, manifest_id: str,
                        revision: int) -> dict | None:
        if self.manifest_source is None:
            return None
        try:
            result = self._upstream(
                lambda: self.manifest_source(tenant_id, manifest_id, revision))
        except ServiceError as exc:
            if exc.status == 503:
                return None
            raise
        if not isinstance(result, dict):
            return None
        return result.get("manifest") or result

    def _mark_stale(self, tenant_id: str, coordinates: dict,
                    reason: str) -> None:
        series_id = coordinates.get("series_id")
        episode_id = coordinates.get("episode_id")
        branch_id = coordinates.get("branch_id")
        if not (series_id and episode_id and branch_id):
            return
        self.conn.execute(
            "INSERT INTO usage_projection_state (tenant_id, series_id,"
            " episode_id, branch_id, applied_sequence, last_event_at, stale,"
            " stale_reason) VALUES (?,?,?,?,0,?,1,?)"
            " ON CONFLICT (tenant_id, series_id, episode_id, branch_id)"
            " DO UPDATE SET stale=1, stale_reason=excluded.stale_reason,"
            " last_event_at=excluded.last_event_at",
            (tenant_id, series_id, episode_id, branch_id, _now(), reason))

    # ------------------------------------------------------------ 展开助手

    def _overlay(self, tenant_id: str, binding_set_id: str,
                 floor: int) -> list[dict]:
        """`floor` 及以下所有 entry 的"最后一层胜出"视图。"""
        rows = self.conn.execute(
            "SELECT * FROM binding_entry WHERE tenant_id=? AND binding_set_id=?"
            " AND revision_floor<=? ORDER BY revision_floor",
            (tenant_id, binding_set_id, floor)).fetchall()
        latest: dict = {}
        for row in rows:
            key = (row["scope_kind"], row["scope_id"], row["role_key"])
            latest[key] = dict(row)  # 后写覆盖先写：floor 升序
        return [latest[key] for key in sorted(latest)]

    def _compute_expansion(self, container: dict, overlay: list[dict],
                           defaults: list[dict]) -> list[dict]:
        """§4.3 第 2 步：每个 scope 节点、每个 role，取最具体的一层。

        目标节点 = 本集 + 所有在 overlay / 默认值里出现过的节点。**不为不存在
        的节点凭空生成行**，所以 resolve 对未物化的镜头只会沿已物化的祖先层找。
        """
        entries = {(row["scope_kind"], row["scope_id"], row["role_key"]): row
                   for row in overlay}
        default_map = {(row["scope_kind"], row["scope_id"], row["role_key"]): row
                       for row in defaults}
        series_id = container["series_id"]
        episode_id = container["episode_id"]

        nodes = {("episode", episode_id)}
        for kind, sid, _role in entries:
            nodes.add((kind, sid))
        for kind, sid, _role in default_map:
            if kind in DEFAULT_SCOPES:
                nodes.add((kind, sid))

        out = []
        for kind, sid in sorted(nodes):
            levels = _ancestor_levels(kind, sid, series_id, episode_id,
                                      entries)
            roles = set()
            for level_kind, level_id in levels:
                for (_k, _i, role) in entries:
                    if (_k, _i) == (level_kind, level_id):
                        roles.add(role)
                for (_k, _i, role) in default_map:
                    if (_k, _i) == (level_kind, level_id):
                        roles.add(role)
            for role in sorted(roles):
                for level_kind, level_id in levels:
                    row = entries.get((level_kind, level_id, role))
                    if row is not None:
                        out.append(_expansion_row(kind, sid, row,
                                                  level_kind, row["required"]))
                        break
                    row = default_map.get((level_kind, level_id, role))
                    if row is not None:
                        # S10-9：默认值一律必需。
                        out.append(_expansion_row(kind, sid, row, level_kind, 1))
                        break
        return out

    def _check_scope_path(self, tenant_id: str, binding_set_id: str,
                          scope_kind: str, scope_id: str, scope_path: dict,
                          floor: int) -> None:
        """S10-1：同一 shot 被声明到两个 scene 就是自相矛盾，422。

        不推断父子关系 —— S10 没有这张表，猜出来的层级比没有更危险。
        """
        scene_id = scope_path.get("scene_id")
        if scope_kind != "shot" or not scene_id:
            return
        rows = self.conn.execute(
            "SELECT scope_path FROM binding_entry WHERE tenant_id=? AND"
            " binding_set_id=? AND revision_floor<=? AND scope_kind='shot'"
            " AND scope_id=?", (tenant_id, binding_set_id, floor, scope_id)
        ).fetchall()
        for row in rows:
            other = _loads(row["scope_path"], {})
            if other.get("scene_id") and other["scene_id"] != scene_id:
                raise ServiceError(
                    422, "SCOPE_INCONSISTENT",
                    f"shot {scope_id} was already declared in scene "
                    f"{other['scene_id']}, not {scene_id} (CONTRACT_GAPS S10-1)",
                    details={"shot_id": scope_id,
                             "declared_scene_id": other["scene_id"],
                             "requested_scene_id": scene_id})

    def _verify_expansion(self, tenant_id: str, rows: list[dict]) -> dict:
        """按 distinct 指针校验，**在事务外**（§9 反模式 10）。"""
        wanted: dict = {}
        for row in rows:
            key = (row["owner_service"], row["owner_local_id"],
                   row["owner_revision"])
            wanted.setdefault(key, row)
        pointers: dict = {}
        for key in sorted(wanted):
            row = wanted[key]
            variant = row["variant_selector"].get("variant")
            pointer = self._upstream(
                lambda row=row, variant=variant: self.revision_source(
                    tenant_id, row["owner_service"], row["owner_local_id"],
                    row["owner_revision"],
                    variant if isinstance(variant, str) else None))
            if not isinstance(pointer, dict) or not pointer.get("content_hash"):
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    "owner returned a revision without a content_hash",
                    retryable=True)
            pointers[key] = pointer
        return pointers

    # ------------------------------------------------------------ 辅助判断

    def _require_open(self, container: dict) -> None:
        if container["state"] == "ARCHIVED":
            raise ServiceError(409, "BINDING_SET_ARCHIVED",
                               "an archived binding set accepts no new "
                               "revisions (plan §3.1 / F11)")

    @staticmethod
    def _require_head(container: dict, expected_head: int) -> None:
        if container["head_revision"] != expected_head:
            raise ServiceError(
                409, "VERSION_CONFLICT",
                f"expected head {expected_head}, actual "
                f"{container['head_revision']}",
                retryable=True,
                details={"head": container["head_revision"],
                         "expected_head": expected_head})


# ------------------------------------------------------------------ 纯辅助

def _require_str(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ServiceError(400, "MALFORMED_REQUEST", f"{key} is required")
    return value


def _require_sha256(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{key} must be a 64-char hex digest")
    return value


def _require_head(body: dict) -> int:
    value = body.get("expected_head")
    if not isinstance(value, int) or value < 0:
        raise ServiceError(
            400, "MALFORMED_REQUEST",
            "expected_head is required and must be a non-negative integer")
    return value


def _clamp_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _entry_out(row) -> dict:
    row = dict(row)
    return {
        "scope_kind": row["scope_kind"],
        "scope_id": row["scope_id"],
        "role_key": row["role_key"],
        "asset_type": row["asset_type"],
        "owner_service": row["owner_service"],
        "owner_local_id": row["owner_local_id"],
        "owner_revision": row["owner_revision"],
        "owner_hash": row["owner_hash"],
        "rights_ref": _loads(row["rights_ref"], {}),
        "variant_selector": _loads(row["variant_selector"], {}),
        "required": bool(row["required"]),
        "note": row["note"],
        "unverified": bool(row["unverified"]),
    }


def _expansion_out(row: dict) -> dict:
    return {
        "scope_kind": row["scope_kind"], "scope_id": row["scope_id"],
        "role_key": row["role_key"], "asset_type": row["asset_type"],
        "owner_service": row["owner_service"],
        "owner_local_id": row["owner_local_id"],
        "owner_revision": row["owner_revision"],
        "owner_hash": row["owner_hash"],
        "rights_ref": _loads(row["rights_ref"], {}),
        "variant_selector": _loads(row["variant_selector"], {}),
        "resolved_from": row["resolved_from"],
        "required": bool(row["required"]),
    }


def _revision_out(row: dict) -> dict:
    return {
        "binding_set_id": row["binding_set_id"],
        "revision": row["revision"],
        "state": row["state"],
        "content_hash": row["content_hash"],
        "payload": _loads(row["payload"], {}),
        "entry_count": row["entry_count"],
        "expanded_from": _loads(row["expanded_from"], {}),
        "rights_pointer_set": _loads(row["rights_pointer_set"], []),
        "base_revision": row["base_revision"],
        "sealed_by": row["sealed_by"],
        "sealed_at": row["sealed_at"],
    }


def _expansion_row(target_kind: str, target_id: str, source: dict,
                   resolved_from: str, required) -> dict:
    return {
        "scope_kind": target_kind, "scope_id": target_id,
        "role_key": source["role_key"], "asset_type": source["asset_type"],
        "owner_service": source["owner_service"],
        "owner_local_id": source["owner_local_id"],
        "owner_revision": source["owner_revision"],
        "owner_hash": source["owner_hash"],
        "rights_ref": _loads(source["rights_ref"], {}),
        "variant_selector": _loads(source["variant_selector"], {}),
        "resolved_from": resolved_from,
        "required": 1 if required else 0,
        "unverified": 0,
    }


def _ancestor_levels(kind: str, sid: str, series_id: str, episode_id: str,
                     entries: dict) -> list[tuple[str, str]]:
    """从最具体到最抽象。shot → scene 的那一跳只能用调用方自报的 scope_path。"""
    levels: list[tuple[str, str]] = [(kind, sid)]
    if kind == "shot":
        scene_ids = set()
        for (scope_kind, scope_id, _role), row in entries.items():
            if scope_kind == "shot" and scope_id == sid:
                declared = _loads(row.get("scope_path"), {})
                if declared.get("scene_id"):
                    scene_ids.add(declared["scene_id"])
        for scene_id in sorted(scene_ids):
            levels.append(("scene", scene_id))
    if kind in ("shot", "scene"):
        levels.append(("episode", episode_id))
    if kind in ("shot", "scene", "episode"):
        levels.append(("series", series_id))
    return levels


def _scope_ladder(container: dict, kind: str | None,
                  sid: str | None) -> list[tuple[str, str]]:
    """resolve 的查找阶梯：**只查已物化的层**，不回读 entry。"""
    ladder: list[tuple[str, str]] = []
    if kind and sid:
        ladder.append((kind, sid))
        if kind == "shot":
            ladder.append(("episode", container["episode_id"]))
        if kind in ("shot", "scene"):
            ladder.append(("episode", container["episode_id"]))
    ladder.append(("episode", container["episode_id"]))
    ladder.append(("series", container["series_id"]))
    seen, out = set(), []
    for item in ladder:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _requested_scope(container: dict, scene_id, shot_id) -> tuple:
    if shot_id:
        return "shot", shot_id
    if scene_id:
        return "scene", scene_id
    return "episode", container["episode_id"]


def _asset_ref(asset: dict, rows: list[dict]) -> dict:
    """§4.4 的 `asset_ref`：该资产在这个 scope 里对应哪一条指针。

    只标注，不裁剪 `entries[]`（S10-21）。
    """
    for row in rows:
        if (row["owner_service"] == asset["owner_service"]
                and row["owner_local_id"] == asset["owner_local_id"]):
            return {"asset_id": asset["asset_id"],
                    "role_key": row["role_key"],
                    "owner_ref": {"service_id": row["owner_service"],
                                  "id": row["owner_local_id"],
                                  "revision": row["owner_revision"],
                                  "content_hash": row["owner_hash"]},
                    "resolved_from": row["resolved_from"]}
    return {"asset_id": asset["asset_id"], "role_key": None,
            "owner_ref": None, "resolved_from": None}


def _rights_pointer_set(rows: list[dict]) -> list[dict]:
    unique = {}
    for row in rows:
        ref = row["rights_ref"]
        key = (ref.get("rights_owner"), ref.get("rights_id"),
               ref.get("rights_revision"), ref.get("rights_hash"))
        unique[key] = dict(ref)
    return [unique[key] for key in sorted(unique)]


def _expanded_from(rows: list[dict], verification: str) -> dict:
    counts = {"shot": 0, "scene": 0, "episode": 0, "series": 0}
    for row in rows:
        counts[row["resolved_from"]] = counts.get(row["resolved_from"], 0) + 1
    return {
        "series_default_rev": 1 if counts["series"] else 0,
        "episode_override_rev": 1 if counts["episode"] else 0,
        "scene_count": counts["scene"],
        "shot_count": counts["shot"],
        "owner_verification": verification,
    }


def _overlay_fingerprint(rows: list[dict]) -> str:
    """两次事务之间草稿有没有变的指纹（按内容，不按时间戳）。"""
    return _jcs(sorted(
        [{key: row.get(key) for key in
          ("scope_kind", "scope_id", "role_key", "owner_revision", "owner_hash",
           "rights_ref", "variant_selector", "required")}
         for row in rows], key=lambda item: _jcs(item)))


def _valid_ref(ref) -> bool:
    if not isinstance(ref, dict):
        return False
    required = ("service_id", "id", "revision", "content_hash")
    if any(key not in ref for key in required):
        return False
    return (isinstance(ref["service_id"], str) and ref["service_id"]
            and isinstance(ref["id"], str) and ref["id"]
            and isinstance(ref["revision"], int) and ref["revision"] >= 1
            and isinstance(ref["content_hash"], str)
            and len(ref["content_hash"]) == 64)
