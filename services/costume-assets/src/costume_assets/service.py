"""S12 内核。方案：`plans/S12-costume-assets.md`。

一句话边界：**S12 是"这套服装长什么样、拆成哪几件、有哪些破损/湿度/妆造档位"
的唯一权威；它不知道也不能知道"这一场谁穿它、现在破成什么样"。**

四条结构性事实，代码里每一处判断都从它们推出来：

1. **变体是能力，不是事实**（§1.1）。`variant_axes` 回答"我能画出哪几种"，
   永远不回答"现在是哪一种"。读不到请求的变体时**绝不退回默认**（§4.3、F5）
   ——S16 说"肩膀撕破了"、S12 退回 `none`，S17 就会拿完好的图判"通过"，
   画面与故事不符而没有任何一层报错。
2. **canonical ≠ 最新**（§1.4）。`costume.canonical_revision` 与
   `current_revision` 分开存，接受新版本不自动移动指针；`:set-canonical`
   能不发事件地移动它（CCA-4）。
3. **`character_ref` 是建议，不是约束**（§1.2）。它是软引用：可空、不比身份、
   不拦通用服装。把它做成强外键会让 500 个背景角色卡在角色登记上。
4. **配饰可以悬空**（§4.1 第 5 条、F14）。配饰常常先在服装侧出现、后进道具库。
   硬拒会卡流程，不记录就没人知道它悬空——折中是"允许 + 显式记录"，
   收到 S14 事件后只做**本地**标记，不改 revision。

事务形状与 S14 同构：`BEGIN IMMEDIATE` → 业务事实 → `outbox.append`
→ 同一次 `COMMIT`。事件先发后写或先写后发都会留下一致性空洞。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    CANDIDATE_STATES,
    COSTUME_KINDS,
    DECISION_KINDS,
    ID_PREFIX,
    REF_ROLES,
    SCHEMA,
    SOURCE_KINDS,
    STATUSES,
)

SOURCE = "S12"
SCHEMA_VERSION = "2.0"

EVENT_COSTUME_REVISION_ACCEPTED = "asset.costume.revision.accepted"
EVENT_PROP_REVISION_ACCEPTED = "asset.prop.revision.accepted"
EVENT_ARTIFACT_ARCHIVED = "artifact.archived"
TOPIC_COSTUME = "drama.costume-assets.v2"

# §5.3 的消费面：S28 的归档事件只做审计；S14 的接受事件把悬空配饰标为已验证。
CONSUMER_ARTIFACT_ARCHIVED = "S12.artifact-archived"
CONSUMER_PROP_REVISION = "S12.prop-revision"

# §2.5：候选默认 30 天过期。过期不删，改成 EXPIRED —— "曾经提过又被放过"
# 本身是审计信息。
CANDIDATE_TTL = timedelta(days=30)

# §5.4 / C12-17：方案给了 429 但没说触发条件。按 costume 的 PENDING 候选数
# 封顶，只做本地背压，不是业务配额（配额归 S23）。
MAX_PENDING_CANDIDATES = 64

# §2.3：`color_primary` / `color_secondary` 只收 #RRGGBB（大小写不敏感）。
# 三字母简写、`rgb()`、"深红色"这些自由文本一律拒（§2.3、§9 反模式 7）。
_HEX = "0123456789abcdefABCDEF"


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


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


def _is_hex(value) -> bool:
    return (isinstance(value, str) and len(value) == 7 and value[0] == "#"
            and all(ch in _HEX for ch in value[1:]))


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


# ------------------------------------------------------------ 纯函数

def content_hash(payload: dict) -> str:
    """§2.2 `content_hash`：**纯内容**的规范化 sha256，不含 `costume_id`。

    不含 `costume_id` 是方案的索引决定的（`costr_hash_uniq (tenant_id,
    content_hash)`，C12-12）：同一租户里两套服装内容一字不差会被当成"重复"，
    而不是两条记录。不含 `accepted_at` / `revision`：那是每次接受都不同的坐标，
    放进去就没有"同内容重复接受"可判了（F6）。

    `quality_gate_ref` 也不进投影：它描述的是"谁检查过这一版"，不是"这一版
    长什么样"。同一套服装在两次 QC 之后被接受两次，内容没变就不该产生第二
    条不可变 revision。
    """
    projection = {
        "outfit": payload.get("outfit"),
        "material_palette": payload.get("material_palette"),
        "accessories": payload.get("accessories"),
        "variant_manifest": payload.get("variant_manifest"),
        "variant_axes": payload.get("variant_axes"),
        "rights_ref": payload.get("rights_ref"),
        "source_kind": payload.get("source_kind"),
        "parent_revision": payload.get("parent_revision"),
    }
    return _sha256_hex(_jcs(projection))


def variant_default(axes: dict) -> dict:
    """每个轴取**第一个声明值**为默认（C12-10：方案没定义默认值语义）。

    `damage` 的例子里第一个值正好是 `none`，所以这个约定与直觉一致；但它
    仍然是本实现选定的约定，不是契约定下的——写在这里，是因为"读路径回显
    未指定轴取了什么"必须可复算。
    """
    return {axis: values[0] for axis, values in sorted(axes.items())
            if isinstance(values, list) and values}


def variant_check(axes: dict, manifest: dict) -> dict:
    """§2.5 的 `variant_check`：缺哪些、多了哪些。

    `missing_variants` 非空时 `policy_auto` 接受被禁止（§2.2、§4.2 第 3 步）。
    `extra_variants` 只是记录——多画一个没声明的档位不是错误，但在排障时
    能解释"为什么图比轴多"。
    """
    missing, extra = [], []
    for axis in sorted(axes):
        for value in axes[axis]:
            if value not in (manifest.get(axis) or {}):
                missing.append({"axis": axis, "value": value})
    for axis in sorted(manifest):
        declared = axes.get(axis) or []
        for value in sorted(manifest[axis]):
            if value not in declared:
                extra.append({"axis": axis, "value": value})
    return {"missing_variants": missing, "extra_variants": extra}


# ------------------------------------------------------------ 服务

class CostumeAssetsService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 quality_gate_source=None,
                 max_pending_candidates: int = MAX_PENDING_CANDIDATES):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)
        # S28：**必需**。没有 artifact 校验就等于没有资产正确性（F1、F11）。
        self.artifact_source = artifact_source
        # QC：**可选**。方案把结论来源写成 `quality_gate_ref`，而全仓没有产出
        # 它的服务（C12-2 / CCA-1）。传 None 时结论只能随命令内联传入；
        # 读不到就按"没有结论"处理，绝不用 true 兜底。
        self.quality_gate_source = quality_gate_source
        self.max_pending_candidates = max_pending_candidates

    # ------------------------------------------------------------ 事务助手
    def _sweep_expired(self, tenant_id: str, costume_id: str | None = None) -> int:
        """把过期的 PENDING 候选落成 EXPIRED（§3.1 的 `ttl──► EXPIRED`）。

        **必须走自己的事务**，不能塞进命令事务里"顺便标一下"：命令随后
        抛 409，`Transaction` 会回滚，那条 UPDATE 也就没了。告警说候选
        过期了、库里却永远还是 PENDING——下一次 draft 的 64 上限照样数它。
        """
        sql = ("UPDATE costume_candidate SET state='EXPIRED'"
               " WHERE tenant_id=? AND state='PENDING' AND expires_at<=?")
        params: tuple = (tenant_id, _now())
        if costume_id is not None:
            sql += " AND costume_id=?"
            params += (costume_id,)
        with Transaction(self.conn):
            cur = self.conn.execute(sql, params)
            return cur.rowcount

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """上游不可达 → 503。**绝不拿缓存的结论兜底**（F11）。

        资产比路由更不该凑合：一个校验不到的 artifact 就是一张可能画错的
        参考图。
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

    # ------------------------------------------------------------ 通用校验
    def _load_costume(self, tenant_id: str, costume_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM costume WHERE tenant_id=? AND costume_id=?",
            (tenant_id, costume_id)).fetchone())
        if row is None:
            # 跨租户与不存在同形（F12/E28）：确认"存在但你没权限"本身就是泄漏。
            raise ServiceError(404, "NOT_FOUND", f"no costume {costume_id}")
        return row

    def _cas(self, costume: dict, expected) -> None:
        if expected is None:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "expected_current_revision is required: this service never "
                "overwrites a revision it has not seen")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expected_current_revision must be an integer")
        if int(expected) != int(costume["current_revision"]):
            raise ServiceError(
                409, "VERSION_CONFLICT",
                f"costume {costume['costume_id']} is at revision "
                f"{costume['current_revision']}, expected {expected}",
                retryable=True,
                details={"expected": int(expected),
                         "current": int(costume["current_revision"])})

    @staticmethod
    def _require(condition, message: str) -> None:
        if not condition:
            raise ServiceError(400, "MALFORMED_REQUEST", message)

    def _character_ref(self, value) -> dict | None:
        """§1.2 软引用：只校验形状，**不校验 S11 里有没有这个角色**。"""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "character_ref must be an object or null")
        service_id = value.get("service_id")
        identifier = value.get("id")
        if not isinstance(service_id, str) or not service_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "character_ref.service_id is required")
        if not isinstance(identifier, str) or not identifier:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "character_ref.id is required")
        # 只留两键：事件契约的 character_ref 是 additionalProperties:false。
        return {"service_id": service_id, "id": identifier}

    def _validate_outfit(self, outfit) -> dict:
        """§2.3 的三条约束：`piece_id` 唯一、`layering_order` 覆盖全部、颜色 hex。

        颜色单独报 `COLOR_FORMAT_INVALID`（F4），其余报 `OUTFIT_INCONSISTENT`
        （F3）——调用方要能一眼分清"我少写了一件"和"我把 `#B23A48` 写成了
        `深红色`"。
        """
        if not isinstance(outfit, dict):
            raise ServiceError(422, "OUTFIT_INCONSISTENT",
                               "outfit must be an object with pieces[]")
        pieces = outfit.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise ServiceError(422, "OUTFIT_INCONSISTENT",
                               "outfit.pieces must be a non-empty list")
        normalized: list[dict] = []
        seen: list[str] = []
        bad_colors: list[str] = []
        for index, piece in enumerate(pieces):
            if not isinstance(piece, dict):
                raise ServiceError(422, "OUTFIT_INCONSISTENT",
                                   f"outfit.pieces[{index}] must be an object")
            piece_id = piece.get("piece_id")
            if not isinstance(piece_id, str) or not piece_id:
                raise ServiceError(422, "OUTFIT_INCONSISTENT",
                                   f"outfit.pieces[{index}].piece_id is required")
            if piece_id in seen:
                # 同一 revision 里两件同 id 的部件：读路径不知道 layering_order
                # 指的是哪一件（F3）。
                raise ServiceError(
                    422, "OUTFIT_INCONSISTENT",
                    f"piece_id {piece_id!r} appears more than once",
                    details={"piece_id": piece_id})
            seen.append(piece_id)
            material = piece.get("material")
            if not isinstance(material, str) or not material:
                # 派生表 `costume_material_ref.material` 是 NOT NULL；
                # 缺了它这一件就检索不到，也说明 outfit 不完整。
                raise ServiceError(
                    422, "OUTFIT_INCONSISTENT",
                    f"piece {piece_id!r} has no material",
                    details={"piece_id": piece_id})
            primary = piece.get("color_primary")
            if not _is_hex(primary):
                bad_colors.append(f"{piece_id}.color_primary")
            secondary = piece.get("color_secondary")
            if secondary is not None and not _is_hex(secondary):
                bad_colors.append(f"{piece_id}.color_secondary")
            item = {
                "piece_id": piece_id,
                "garment": piece.get("garment"),
                "color_primary": primary,
                "color_secondary": secondary,
                "material": material,
                "pattern": piece.get("pattern"),
                "fit": piece.get("fit"),
                "coverage": piece.get("coverage"),
                "length": piece.get("length"),
                "heel_cm_band": piece.get("heel_cm_band"),
                "removable": bool(piece.get("removable")),
                "sheen": piece.get("sheen"),
                "texture_note": piece.get("texture_note"),
            }
            normalized.append(item)
        if bad_colors:
            raise ServiceError(
                422, "COLOR_FORMAT_INVALID",
                "colors must be #RRGGBB hex; free text is not main data "
                "(plan §2.3, §9 anti-pattern 7)",
                details={"pieces": bad_colors})
        order = outfit.get("layering_order")
        if not isinstance(order, list):
            raise ServiceError(422, "OUTFIT_INCONSISTENT",
                               "outfit.layering_order must be a list of piece_id")
        cleaned_order: list[str] = []
        for entry in order:
            if not isinstance(entry, str) or not entry:
                raise ServiceError(422, "OUTFIT_INCONSISTENT",
                                   "layering_order entries must be piece_id strings")
            if entry not in seen:
                raise ServiceError(
                    422, "OUTFIT_INCONSISTENT",
                    f"layering_order names unknown piece {entry!r}",
                    details={"missing_pieces": [], "unknown_pieces": [entry]})
            if entry in cleaned_order:
                raise ServiceError(422, "OUTFIT_INCONSISTENT",
                                   f"layering_order repeats {entry!r}")
            cleaned_order.append(entry)
        uncovered = [piece_id for piece_id in seen
                     if piece_id not in cleaned_order]
        if uncovered:
            # §2.3：顺序必须覆盖所有 piece，否则遮挡关系答不出来（F3）。
            raise ServiceError(
                422, "OUTFIT_INCONSISTENT",
                f"layering_order does not cover: {uncovered}",
                details={"missing_pieces": uncovered, "unknown_pieces": []})
        return {
            "pieces": normalized,
            "layering_order": cleaned_order,
            "silhouette": outfit.get("silhouette"),
            "era_style": outfit.get("era_style"),
        }

    def _validate_variants(self, variant_axes, variant_manifest) -> tuple:
        """§4.1 第 2 条：`variant_axes` 每个取值都要有 artifact。"""
        if not isinstance(variant_axes, dict) or not variant_axes:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "variant_axes must be a non-empty object {axis: [values]} "
                "(the event contract requires at least one axis)")
        if not isinstance(variant_manifest, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "variant_manifest must be an object "
                               "{axis: {value: artifact_ref}}")
        axes: dict[str, list[str]] = {}
        for axis, values in variant_axes.items():
            if not isinstance(axis, str) or not axis:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "variant axis names must be non-empty")
            if not isinstance(values, list) or not values:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"variant axis {axis!r} must be a non-empty "
                                   f"list of values")
            seen: list[str] = []
            for value in values:
                if not isinstance(value, str) or not value:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       f"variant axis {axis!r} has a non-string "
                                       f"value")
                if value not in seen:
                    seen.append(value)
            axes[axis] = seen

        manifest: dict[str, dict[str, dict]] = {}
        for axis, values in variant_manifest.items():
            if not isinstance(values, dict):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"variant_manifest[{axis!r}] must map value -> artifact")
            manifest[axis] = {}
            for value, ref in values.items():
                manifest[axis][value] = _artifact_ref(ref)

        # 只允许"声明了却没有图"这一种缺法；manifest 里多出来的值不算错，
        # 记进 variant_check.extra_variants（§2.5）。
        missing = []
        for axis, values in axes.items():
            for value in values:
                if value not in (manifest.get(axis) or {}):
                    missing.append({"axis": axis, "value": value})
        if missing:
            # 声明了 `torn_shoulder` 却没有图，等于 S16 说"撕破了"、画面
            # 画不出来（§2.2、§9 反模式 8）。
            raise ServiceError(
                422, "VARIANT_COVERAGE_INCOMPLETE",
                f"variant_axes declares values with no artifact: {missing}",
                details={"missing_variants": missing,
                         "axis": missing[0]["axis"],
                         "missing_values": [m["value"] for m in missing
                                            if m["axis"] == missing[0]["axis"]]})
        return axes, manifest

    def _validate_artifacts(self, tenant_id: str, manifest: dict) -> None:
        """§4.1 第 1 条：每个 artifact 存在且 hash 一致。

        S28 超时 → 503 且**不落候选**（F11）。拿不到校验结论就存下来，
        等于把一个可能是伪造的 artifact_id 写进不可变 revision。
        """
        for axis in sorted(manifest):
            for value in sorted(manifest[axis]):
                ref = manifest[axis][value]
                artifact_id = ref.get("artifact_id")
                if not artifact_id:
                    raise ServiceError(
                        400, "MALFORMED_REQUEST",
                        f"variant {axis}:{value} has no artifact_id")
                artifact = self._upstream(
                    lambda aid=artifact_id: self.artifact_source(tenant_id, aid))
                if not artifact:
                    raise ServiceError(
                        422, "ARTIFACT_NOT_FOUND",
                        f"artifact {artifact_id!r} does not exist (or is not "
                        f"visible in this tenant)",
                        details={"axis": axis, "value": value,
                                 "artifact_id": artifact_id})
                actual = (artifact.get("content_hash")
                          or artifact.get("sha256"))
                declared = ref.get("content_hash")
                if declared and actual and str(declared) != str(actual):
                    raise ServiceError(
                        422, "ARTIFACT_HASH_MISMATCH",
                        f"artifact {artifact_id!r} has hash {actual}, the "
                        f"candidate declared {declared}",
                        details={"axis": axis, "value": value,
                                 "artifact_id": artifact_id,
                                 "expected": declared, "actual": actual})
                ref["content_hash"] = actual or declared

    def _accessories(self, accessories) -> tuple[list[dict], list[dict]]:
        """§4.1 第 5 条：配饰**不校验存在**，只记悬空引用。

        返回 `(accessories, unverified_refs)`。`unverified_refs` 的元素形状是
        本服务定义的（C12-14 / CCA-3）：契约、方案、S14 三处都没定义它，
        所以这里选最小可消解的集合——`target_service + target_id` 就是消解
        需要的全部信息，`target_revision` 有就带上。
        """
        if accessories is None:
            return [], []
        if not isinstance(accessories, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "accessories must be a list")
        cleaned, unresolved = [], []
        for index, item in enumerate(accessories):
            if not isinstance(item, dict):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"accessories[{index}] must be an object")
            cleaned.append(item)
            target_service = item.get("target_service")
            target_id = item.get("target_id")
            if not target_service or not target_id:
                # 纯描述性配饰（"珍珠项链"）不引用任何服务，没有可消解的东西。
                continue
            if not isinstance(target_service, str) or not isinstance(
                    target_id, str):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "accessory target_service/target_id must be "
                                   "strings")
            target_revision = item.get("target_revision")
            if target_revision is not None and (
                    isinstance(target_revision, bool)
                    or not isinstance(target_revision, int)):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "accessory target_revision must be an integer")
            key = f"{target_service}:{target_id}"
            if target_revision is not None:
                key += f"@{target_revision}"
            unresolved.append({
                "ref_key": key,
                "target_service": target_service,
                "target_id": target_id,
                "target_revision": target_revision,
            })
        return cleaned, unresolved

    def _evaluation(self, tenant_id: str, body: dict) -> dict:
        """读 QC 结论。**只读，不判**（§4.2、§9 反模式 5）。

        优先级：命令内联的 `evaluation` → 配置了的 QC 源 → `{}`。
        `{}` 表示"没有结论"，不是"通过"——CCA-5：`identity_invariance_check`
        缺失时按"不是 pass"处理。
        """
        inline = body.get("evaluation")
        if isinstance(inline, dict):
            return inline
        if self.quality_gate_source is None:
            return {}
        ref = body.get("quality_gate_ref")
        if not ref:
            return {}
        return self._upstream(
            lambda: self.quality_gate_source(tenant_id, ref)) or {}

    @staticmethod
    def _image_text_match_failed(evaluation: dict) -> bool:
        """E24-C1 的判定口径：只认 `evaluation.image_text_match`。

        接受 `false` / `{"pass": false}` / `"fail"` 三种写法——方案没有给
        QCReport 的形状（C12-2/CCA-1），这是本实现能读的最小集合。缺失或
        无法解释时**不判负**：把"没检查"当成"失败"会拒掉每一条没有 QC 的
        草稿，那比误放行的代价更常见、也更难解释。
        """
        value = evaluation.get("image_text_match")
        if isinstance(value, bool):
            return value is False
        if isinstance(value, str):
            return value.strip().lower() in ("fail", "failed", "mismatch")
        if isinstance(value, dict):
            if "pass" in value:
                return value.get("pass") is False
            if "passed" in value:
                return value.get("passed") is False
            if "matched" in value:
                return value.get("matched") is False
        return False

    # ------------------------------------------------------------ 命令：登记
    def create_costume(self, tenant_id: str, command_id: str,
                       body: dict) -> dict:
        """`POST /costumes`。§6.1 的自然幂等键是 (series_scope,
        display_name, costume_kind)。"""
        project_id = body.get("project_id")
        series_scope = body.get("series_scope")
        display_name = body.get("display_name")
        costume_kind = body.get("costume_kind")
        self._require(isinstance(project_id, str) and project_id,
                      "project_id is required")
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(display_name, str) and display_name,
                      "display_name is required")
        self._require(costume_kind in COSTUME_KINDS,
                      f"costume_kind must be one of {list(COSTUME_KINDS)}")
        character_ref = self._character_ref(body.get("character_ref"))
        principal = str(body.get("principal_id") or tenant_id)

        def _produce():
            existing = _row(self.conn.execute(
                "SELECT * FROM costume WHERE tenant_id=? AND series_scope=?"
                " AND display_name=? AND costume_kind=?",
                (tenant_id, series_scope, display_name,
                 costume_kind)).fetchone())
            if existing is not None:
                # E12：重复登记返回既有 ID。两条记录同名同租户会让 `:lookup`
                # 无解，也会让 S10 的引用分成两边。
                return _costume_out(existing, created=False)
            costume_id = _new_id("costume")
            self.conn.execute(
                "INSERT INTO costume (tenant_id, costume_id, project_id,"
                " series_scope, display_name, costume_kind, character_ref,"
                " status, canonical_revision, current_revision, created_by,"
                " created_at) VALUES (?,?,?,?,?,?,?, 'active', NULL, 0, ?, ?)",
                (tenant_id, costume_id, project_id, series_scope, display_name,
                 costume_kind,
                 None if character_ref is None else _jcs(character_ref),
                 _jcs({"principal_id": principal}), _now()))
            return _costume_out(self._load_costume(tenant_id, costume_id),
                                created=True)

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 命令：候选
    def draft_candidate(self, tenant_id: str, command_id: str,
                        costume_id: str, body: dict) -> dict:
        """`POST /costumes/{id}/candidates`（§4.1）。事务外校验，事务内 CAS + 写。"""
        costume = self._load_costume(tenant_id, costume_id)
        if costume["status"] == "tombstoned":
            # §3.3：已下线服装不能再有新版本（F10）。
            raise ServiceError(422, "COSTUME_TOMBSTONED",
                               f"costume {costume_id} is tombstoned")
        self._cas(costume, body.get("expected_current_revision"))
        # 事务外先回收过期位：否则下面数到的 pending 里混着早已过期的死候选，
        # 用户会因为"看不见的 64 条"被 429（§3.1 的 ttl──► EXPIRED）。
        self._sweep_expired(tenant_id, costume_id)

        source_kind = body.get("source_kind")
        self._require(source_kind in SOURCE_KINDS,
                      f"source_kind must be one of {list(SOURCE_KINDS)}")
        rights_ref = body.get("rights_ref")
        self._require(isinstance(rights_ref, dict) and rights_ref,
                      "rights_ref must be a non-empty object (S12 stores the "
                      "pointer only; the verdict is S02's)")
        quality_gate_ref = body.get("quality_gate_ref")
        self._require(quality_gate_ref is not None,
                      "quality_gate_ref is required")
        parent_revision = body.get("parent_revision")
        if parent_revision is not None:
            self._require(isinstance(parent_revision, int)
                          and not isinstance(parent_revision, bool),
                          "parent_revision must be an integer")
            exists = self.conn.execute(
                "SELECT 1 FROM costume_revision WHERE tenant_id=?"
                " AND costume_id=? AND revision=?",
                (tenant_id, costume_id, parent_revision)).fetchone()
            if exists is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"parent revision {parent_revision} does not exist")

        outfit = self._validate_outfit(body.get("outfit"))
        palette = body.get("material_palette")
        self._require(isinstance(palette, dict),
                      "material_palette must be an object")
        accessories, unresolved = self._accessories(body.get("accessories"))
        axes, manifest = self._validate_variants(
            body.get("variant_axes"), body.get("variant_manifest"))

        # 事务外：S28。它慢、会超时，且失败时不该占着写锁（F11）。
        self._validate_artifacts(tenant_id, manifest)

        evaluation = self._evaluation(tenant_id, body)
        check = variant_check(axes, manifest)

        payload = {
            "outfit": outfit,
            "material_palette": palette,
            "accessories": accessories,
            "variant_manifest": manifest,
            "variant_axes": axes,
            "rights_ref": rights_ref,
            "source_kind": source_kind,
            "parent_revision": parent_revision,
        }
        digest = content_hash(payload)
        candidate_id = _new_id("candidate")
        now = _now()
        expires_at = (_now_dt() + CANDIDATE_TTL).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        def _produce():
            self._cas(self._load_costume(tenant_id, costume_id),
                      body.get("expected_current_revision"))
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM costume_candidate WHERE tenant_id=?"
                " AND costume_id=? AND state='PENDING'",
                (tenant_id, costume_id)).fetchone()[0]
            if pending >= self.max_pending_candidates:
                # C12-17：本地背压，不是业务配额。配额归 S23。
                raise ServiceError(
                    429, "WRITE_BACKPRESSURE",
                    f"costume {costume_id} already has {pending} pending "
                    f"candidates (limit {self.max_pending_candidates})",
                    retryable=True,
                    details={"pending": pending,
                             "limit": self.max_pending_candidates})
            self.conn.execute(
                "INSERT INTO costume_candidate (tenant_id, candidate_id,"
                " costume_id, content_hash, payload, evaluation, variant_check,"
                " state, created_by, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?, 'PENDING', ?, ?, ?)",
                (tenant_id, candidate_id, costume_id, digest,
                 _dumps(payload), _dumps(evaluation), _dumps(check),
                 _jcs({"principal_id": str(body.get("principal_id")
                                           or tenant_id)}),
                 now, expires_at))
            return {
                "candidate_id": candidate_id,
                "costume_id": costume_id,
                "content_hash": digest,
                "variant_check": check,
                # F14：允许引用 S14 还没有的道具，但必须显式告诉调用方
                # 这些引用现在是悬空的。
                "unverified_refs": unresolved,
                "state": "PENDING",
                "expires_at": expires_at,
            }

        return self._command(tenant_id, command_id, _produce)

    def reject_candidate(self, tenant_id: str, command_id: str,
                         costume_id: str, candidate_id: str,
                         body: dict) -> dict:
        self._load_costume(tenant_id, costume_id)
        # 过期落库必须在这个命令事务**之前**完成。写在这里面然后抛 409，
        # 回滚会把 EXPIRED 一起撤掉——§3.1 的 ttl 边永远不成立。
        self._sweep_expired(tenant_id, costume_id)

        def _produce():
            candidate = self._load_candidate(tenant_id, costume_id, candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}")
            self.conn.execute(
                "UPDATE costume_candidate SET state='REJECTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))
            return {"candidate_id": candidate_id, "costume_id": costume_id,
                    "state": "REJECTED",
                    "reason_text": body.get("reason_text")}

        return self._command(tenant_id, command_id, _produce)

    def accept_candidate(self, tenant_id: str, command_id: str,
                         costume_id: str, candidate_id: str,
                         body: dict) -> dict:
        """`POST /costumes/{id}/candidates/{cid}:accept`（§4.2 的九步）。

        锁顺序按 §6.3：costume → candidate → canonical_pointer → revision →
        material_ref → outbox。SQLite 的 `BEGIN IMMEDIATE` 让整段写事务串行。
        """
        self._load_costume(tenant_id, costume_id)
        # 同 reject：过期落库先于命令事务，否则 409 一回滚，候选永远 PENDING。
        self._sweep_expired(tenant_id, costume_id)
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        set_canonical = bool(body.get("set_canonical"))
        reason_text = body.get("reason_text")
        principal = str(body.get("principal_id") or tenant_id)
        home_cell = str(body.get("home_cell") or "unknown")
        correlation_id = str(body.get("correlation_id") or command_id)

        # E24-C1（C12-13）：图文匹配失败的候选必须落成 REJECTED，且不写
        # revision。这一步在**命令事务之外**完成——在命令事务里写 REJECTED
        # 然后抛 422，回滚会把 REJECTED 一起撤掉，候选永远停在 PENDING，
        # 下一次接受还会再走一遍同一条失败路径。
        self._reject_on_quality_gate(tenant_id, costume_id, candidate_id)

        def _produce():
            # 1) 锁 costume + CAS
            costume = self._load_costume(tenant_id, costume_id)
            if costume["status"] == "tombstoned":
                raise ServiceError(422, "COSTUME_TOMBSTONED",
                                   f"costume {costume_id} is tombstoned")
            self._cas(costume, body.get("expected_current_revision"))

            # 2) 锁 candidate
            candidate = self._load_candidate(tenant_id, costume_id, candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}")
            if _expired(candidate["expires_at"]):
                # 上面 `_sweep_expired` 已在本事务之前落过库；能走到这里说明
                # 扫描之后、加锁之前刚好跨过截止线，直接拒掉即可。这里**不再
                # 写库**——在本事务里写然后抛 409 只会一起回滚。
                raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                                   f"candidate {candidate_id} has expired")

            payload = _loads(candidate["payload"], {})
            evaluation = _loads(candidate["evaluation"], {})
            check = _loads(candidate["variant_check"], {})

            # 3) 升 canonical 的两道门（§4.2）。只在 set_canonical 时适用：
            #    不升 canonical 的版本是"候选里多了一版"，下游还没人引用它。
            if set_canonical:
                missing = check.get("missing_variants") or []
                if missing and decision_kind == "policy_auto":
                    # C12-11：正常路径上 reach 不到（起草已经拒了）；但它挡住
                    # 的是"升级前入库的旧候选被 policy_auto 悄悄放行"。
                    raise ServiceError(
                        422, "VARIANT_COVERAGE_INCOMPLETE",
                        "cannot auto-accept a candidate with missing variants "
                        "(its gaps would make downstream fall back to a "
                        "default look)",
                        details={"missing_variants": missing})
                identity = evaluation.get("identity_invariance_check")
                if identity != "pass" and decision_kind == "policy_auto":
                    # CCA-5：没有形状、没有取值枚举，所以只认字面量 'pass'。
                    # 缺失 / 'fail' / 'IDENTITY_CHANGED' 一律挡。
                    raise ServiceError(
                        422, "IDENTITY_CHECK_REQUIRED_FOR_AUTO",
                        "policy_auto requires a passing identity invariance "
                        "check; S12 reads the conclusion, it never runs the "
                        "comparison (plan §4.2)",
                        details={"identity_invariance_check": identity})

            revision = int(costume["current_revision"]) + 1

            # 4) 插 revision。hash 冲突 = 同内容重复接受（F6）。
            try:
                self.conn.execute(
                    "INSERT INTO costume_revision (tenant_id, costume_id,"
                    " revision, content_hash, payload, outfit, material_palette,"
                    " accessories, variant_manifest, variant_axes, rights_ref,"
                    " source_kind, parent_revision, quality_gate_ref,"
                    " accepted_at, accepted_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, costume_id, revision,
                     candidate["content_hash"], _dumps(payload),
                     _dumps(payload.get("outfit") or {}),
                     _dumps(payload.get("material_palette") or {}),
                     _dumps(payload.get("accessories") or []),
                     _dumps(payload.get("variant_manifest") or {}),
                     _dumps(payload.get("variant_axes") or {}),
                     _dumps(payload.get("rights_ref") or {}),
                     payload.get("source_kind"),
                     payload.get("parent_revision"),
                     _dumps(body.get("quality_gate_ref") or {}),
                     _now(), _jcs({"principal_id": principal})))
            except sqlite3.IntegrityError as exc:
                if "content_hash" in str(exc):
                    raise ServiceError(
                        409, "DUPLICATE_COSTUME_CONTENT",
                        "this content has already been accepted for this "
                        "tenant (content_hash is unique per tenant)",
                        details={"content_hash": candidate["content_hash"]}
                    ) from exc
                raise

            # 5) 派生表：材质（§2.4）与悬空引用（§4.1 第 5 条）。
            #    两张表都能从 payload 重建，权威永远是 revision。
            for piece in (payload.get("outfit") or {}).get("pieces") or []:
                self.conn.execute(
                    "INSERT INTO costume_material_ref (tenant_id, costume_id,"
                    " revision, piece_id, material, sheen, texture_note)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (tenant_id, costume_id, revision, piece["piece_id"],
                     piece["material"], piece.get("sheen"),
                     piece.get("texture_note")))
            unverified = []
            for item in payload.get("accessories") or []:
                target_service = item.get("target_service")
                target_id = item.get("target_id")
                if not target_service or not target_id:
                    continue
                target_revision = item.get("target_revision")
                ref_key = f"{target_service}:{target_id}"
                if target_revision is not None:
                    ref_key += f"@{target_revision}"
                self.conn.execute(
                    "INSERT OR IGNORE INTO costume_unverified_ref (tenant_id,"
                    " costume_id, revision, ref_key, target_service, target_id,"
                    " target_revision, ref_payload)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, costume_id, revision, ref_key, target_service,
                     target_id, target_revision, _jcs(item)))
                unverified.append({
                    "ref_key": ref_key,
                    "target_service": target_service,
                    "target_id": target_id,
                    "target_revision": target_revision,
                })

            canonical_revision = costume["canonical_revision"]
            became_canonical = False
            # 6) 移动 canonical 指针（只在 set_canonical 时）
            if set_canonical:
                self.conn.execute(
                    "UPDATE canonical_pointer SET superseded_at=?"
                    " WHERE tenant_id=? AND costume_id=? AND"
                    " superseded_at IS NULL",
                    (_now(), tenant_id, costume_id))
                self.conn.execute(
                    "INSERT INTO canonical_pointer (tenant_id, costume_id,"
                    " revision, decided_at, decided_by, decision_kind,"
                    " reason_text, prior_canonical, superseded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (tenant_id, costume_id, revision, _now(),
                     _jcs({"principal_id": principal}), decision_kind,
                     reason_text, costume["canonical_revision"]))
                self.conn.execute(
                    "UPDATE costume SET current_revision=?, canonical_revision=?"
                    " WHERE tenant_id=? AND costume_id=?",
                    (revision, revision, tenant_id, costume_id))
                canonical_revision = revision
                became_canonical = True
            else:
                self.conn.execute(
                    "UPDATE costume SET current_revision=? WHERE tenant_id=?"
                    " AND costume_id=?", (revision, tenant_id, costume_id))

            # 7) 候选状态
            self.conn.execute(
                "UPDATE costume_candidate SET state='ACCEPTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))

            # 8) 事件与事实同事务（§5.3；契约 9 个 data 键，一个不多）。
            #    注意 `:set-canonical` 不发事件（CCA-4），所以这里的
            #    `became_canonical` 只描述"这次接受是否升了指针"。
            character_ref = _loads(costume["character_ref"], None)
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_COSTUME_REVISION_ACCEPTED,
                subject=f"costume/{costume_id}", tenant_id=tenant_id,
                home_cell=home_cell, aggregate_id=costume_id, sequence=revision,
                correlation_id=correlation_id, causation_id=command_id,
                data={
                    "schema_version": SCHEMA_VERSION,
                    "costume_ref": {"service_id": SOURCE, "id": costume_id,
                                    "revision": revision,
                                    "content_hash": candidate["content_hash"]},
                    "character_ref": character_ref,
                    "costume_kind": costume["costume_kind"],
                    "became_canonical": became_canonical,
                    "canonical_revision": canonical_revision,
                    "variant_axes": payload.get("variant_axes") or {},
                    "unverified_refs": unverified,
                    "rights_ref": payload.get("rights_ref") or {},
                },
                schema_version=SCHEMA_VERSION))

            return {
                "costume_id": costume_id,
                "revision": revision,
                "candidate_id": candidate_id,
                "content_hash": candidate["content_hash"],
                "became_canonical": became_canonical,
                "canonical_revision": canonical_revision,
                "unverified_refs": unverified,
                "state": "ACCEPTED",
            }

        return self._command(tenant_id, command_id, _produce)

    def _reject_on_quality_gate(self, tenant_id: str, costume_id: str,
                                candidate_id: str) -> None:
        """E24-C1：QC 判图文不匹配时，候选落 REJECTED、不写 revision。

        独立事务（见调用点注释）。只动本租户的 PENDING 候选；已经不是 PENDING
        的交给主路径按 409 处理，不在这里抢答。
        """
        candidate = _row(self.conn.execute(
            "SELECT state, evaluation FROM costume_candidate WHERE tenant_id=?"
            " AND costume_id=? AND candidate_id=?",
            (tenant_id, costume_id, candidate_id)).fetchone())
        if candidate is None or candidate["state"] != "PENDING":
            return
        evaluation = _loads(candidate["evaluation"], {})
        if not self._image_text_match_failed(evaluation):
            return
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE costume_candidate SET state='REJECTED' WHERE"
                " tenant_id=? AND candidate_id=? AND state='PENDING'",
                (tenant_id, candidate_id))
        raise ServiceError(
            422, "QUALITY_GATE_REJECTED",
            "the quality gate reports the revision image does not match its "
            "outfit; the candidate was rejected and no revision was written "
            "(S12 reads the conclusion, it never runs the comparison)",
            details={"image_text_match": evaluation.get("image_text_match")})

    def _load_candidate(self, tenant_id: str, costume_id: str,
                        candidate_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM costume_candidate WHERE tenant_id=? AND costume_id=?"
            " AND candidate_id=?", (tenant_id, costume_id,
                                    candidate_id)).fetchone())
        if row is None:
            raise ServiceError(
                404, "NOT_FOUND",
                f"no candidate {candidate_id} for costume {costume_id}")
        return row

    # ------------------------------------------------------------ 命令：指针与状态
    def set_canonical(self, tenant_id: str, command_id: str,
                      costume_id: str, body: dict) -> dict:
        """`POST /costumes/{id}:set-canonical`（CCA-4：**不发事件**）。

        方案把 canonical 的移动做成了独立命令，而 topics 里没有它的主题。
        按事件缓存 canonical 的消费方必须回读 `GET /costumes/{id}/canonical`
        ——这条写在这里，是因为它是这个设计对下游的硬要求。
        """
        revision = body.get("revision")
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(isinstance(revision, int)
                      and not isinstance(revision, bool),
                      "revision must be an integer")
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        principal = str(body.get("principal_id") or tenant_id)

        def _produce():
            costume = self._load_costume(tenant_id, costume_id)
            if costume["status"] == "tombstoned":
                # §3.3：已下线不应有新基准（F10）。
                raise ServiceError(422, "COSTUME_TOMBSTONED",
                                   f"costume {costume_id} is tombstoned")
            target = _row(self.conn.execute(
                "SELECT * FROM costume_revision WHERE tenant_id=?"
                " AND costume_id=? AND revision=?",
                (tenant_id, costume_id, revision)).fetchone())
            if target is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"no revision {revision} for costume {costume_id}")
            if costume["canonical_revision"] == revision:
                # 幂等：已经在指的这一版，不重写也不报错。
                return {"costume_id": costume_id,
                        "canonical_revision": revision, "changed": False}
            self.conn.execute(
                "UPDATE canonical_pointer SET superseded_at=? WHERE tenant_id=?"
                " AND costume_id=? AND superseded_at IS NULL",
                (_now(), tenant_id, costume_id))
            # 回退到**用过的** revision（§3.2 的箭头、E08-C2）时主键
            # (tenant_id, costume_id, revision) 已经占着，只能把那一行重新
            # 启用；INSERT 会撞主键。代价是那一行原来的 decided_at/by/kind
            # 被覆盖——登记为 C12-19，要完整移动史得加一张 move-log 表。
            existing = self.conn.execute(
                "SELECT 1 FROM canonical_pointer WHERE tenant_id=?"
                " AND costume_id=? AND revision=?",
                (tenant_id, costume_id, revision)).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO canonical_pointer (tenant_id, costume_id,"
                    " revision, decided_at, decided_by, decision_kind,"
                    " reason_text, prior_canonical, superseded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (tenant_id, costume_id, revision, _now(),
                     _jcs({"principal_id": principal}), decision_kind,
                     body.get("reason_text"), costume["canonical_revision"]))
            else:
                self.conn.execute(
                    "UPDATE canonical_pointer SET superseded_at=NULL,"
                    " decided_at=?, decided_by=?, decision_kind=?,"
                    " reason_text=?, prior_canonical=?"
                    " WHERE tenant_id=? AND costume_id=? AND revision=?",
                    (_now(), _jcs({"principal_id": principal}), decision_kind,
                     body.get("reason_text"), costume["canonical_revision"],
                     tenant_id, costume_id, revision))
            self.conn.execute(
                "UPDATE costume SET canonical_revision=? WHERE tenant_id=?"
                " AND costume_id=?",
                (revision, tenant_id, costume_id))
            return {"costume_id": costume_id, "canonical_revision": revision,
                    "prior_canonical": costume["canonical_revision"],
                    "changed": True}

        return self._command(tenant_id, command_id, _produce)

    def change_status(self, tenant_id: str, command_id: str, costume_id: str,
                      target: str, body: dict) -> dict:
        """`:deprecate` / `:tombstone`。§3.2 的三态直线，`tombstoned` 不能复活
        （§3.3：复活走新 costume_id，否则旧绑定会指向一个"又活了"的对象）。"""
        self._require(target in ("deprecated", "tombstoned"),
                      "target status must be deprecated or tombstoned")

        def _produce():
            costume = self._load_costume(tenant_id, costume_id)
            current = costume["status"]
            allowed = ({"active"} if target == "deprecated"
                       else {"active", "deprecated"})
            if current not in allowed:
                raise ServiceError(
                    409, "INVALID_STATUS_TRANSITION",
                    f"cannot move costume {costume_id} from {current} to "
                    f"{target}",
                    details={"from": current, "to": target})
            self.conn.execute(
                "UPDATE costume SET status=? WHERE tenant_id=? AND costume_id=?",
                (target, tenant_id, costume_id))
            return {"costume_id": costume_id, "status": target,
                    "previous_status": current}

        return self._command(tenant_id, command_id, _produce)

    def add_revision_references(self, tenant_id: str, command_id: str,
                               costume_id: str, body: dict) -> dict:
        """`POST /costumes/{id}/revision-references`（§2.6）。

        `worn_with` 是**推荐组合**，不是强制；S10 的 binding 决定实际组合。
        只允许挂在已接受的 revision 上（不可变事实），重复登记无副作用
        （主键含全部键）。
        """
        revision = body.get("revision")
        self._require(isinstance(revision, int)
                      and not isinstance(revision, bool),
                      "revision must be an integer")
        raw = body.get("references")
        if raw is None and body.get("ref_role") is not None:
            raw = [body]
        if not isinstance(raw, list) or not raw:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "references must be a non-empty list")
        refs = []
        for index, ref in enumerate(raw):
            if not isinstance(ref, dict):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"references[{index}] must be an object")
            role = ref.get("ref_role")
            self._require(role in REF_ROLES,
                          f"ref_role must be one of {list(REF_ROLES)}")
            target_service = ref.get("target_service")
            target_id = ref.get("target_id")
            self._require(isinstance(target_service, str) and target_service,
                          "target_service is required")
            self._require(isinstance(target_id, str) and target_id,
                          "target_id is required")
            target_revision = ref.get("target_revision")
            self._require(isinstance(target_revision, int)
                          and not isinstance(target_revision, bool),
                          "target_revision must be an integer")
            target_hash = ref.get("target_hash")
            if target_hash is not None:
                self._require(
                    isinstance(target_hash, str) and len(target_hash) == 64
                    and all(ch in "0123456789abcdef" for ch in target_hash),
                    "target_hash must be 64 lowercase hex characters")
            refs.append((role, target_service, target_id, target_revision,
                         target_hash))

        def _produce():
            self._load_costume(tenant_id, costume_id)
            exists = self.conn.execute(
                "SELECT 1 FROM costume_revision WHERE tenant_id=?"
                " AND costume_id=? AND revision=?",
                (tenant_id, costume_id, revision)).fetchone()
            if exists is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"no revision {revision} for costume {costume_id}")
            created = 0
            for ref in refs:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO revision_reference (tenant_id,"
                    " costume_id, revision, ref_role, target_service, target_id,"
                    " target_revision, target_hash) VALUES (?,?,?,?,?,?,?,?)",
                    (tenant_id, costume_id, revision) + ref)
                created += cur.rowcount
            return {"costume_id": costume_id, "revision": revision,
                    "references": len(refs), "created": created}

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 读
    def get_costume(self, tenant_id: str, costume_id: str) -> dict:
        costume = self._load_costume(tenant_id, costume_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=? AND costume_id=?"
            " AND superseded_at IS NULL", (tenant_id, costume_id)).fetchone())
        return {
            "costume": _costume_out(costume, created=None),
            "canonical": _pointer_out(pointer),
            # E30-C1：S12 从不缓存权利状态，读就要复查。
            "rights_recheck_required": True,
        }

    def get_revision(self, tenant_id: str, costume_id: str, revision: int, *,
                     variant: str | None = None) -> dict:
        costume = self._load_costume(tenant_id, costume_id)
        row = _row(self.conn.execute(
            "SELECT * FROM costume_revision WHERE tenant_id=? AND costume_id=?"
            " AND revision=?", (tenant_id, costume_id, revision)).fetchone())
        if row is None:
            raise ServiceError(
                404, "NOT_FOUND",
                f"no revision {revision} for costume {costume_id}")
        axes = _loads(row["variant_axes"], {})
        manifest = _loads(row["variant_manifest"], {})
        selector = _parse_variant_selector(variant)
        # 未指定的轴取该轴第一个声明值，并把解析结果回显（C12-10）；
        # 已指定但本版没有的轴 → 422，**绝不退回默认**（F5）。
        resolved = variant_default(axes)
        for axis, value in selector.items():
            available = axes.get(axis)
            if not available or value not in available:
                raise ServiceError(
                    422, "VARIANT_NOT_AVAILABLE",
                    f"variant {axis}:{value} is not available in revision "
                    f"{revision}",
                    details={"axis": axis, "value": value,
                             "available": available or []})
            chosen = (manifest.get(axis) or {}).get(value)
            if chosen is None:
                # 轴声明了但 manifest 里没有：只可能是绕过了起草校验的行
                # （导入/旧数据）。这时仍然不能退回默认。
                raise ServiceError(
                    422, "VARIANT_NOT_AVAILABLE",
                    f"variant {axis}:{value} has no artifact in revision "
                    f"{revision}",
                    details={"axis": axis, "value": value,
                             "available": available})
            resolved[axis] = value
        artifacts: dict[str, dict] = {}
        for axis, value in sorted(resolved.items()):
            ref = (manifest.get(axis) or {}).get(value)
            if ref is not None:
                artifacts[f"{axis}:{value}"] = ref
        return {
            "costume_ref": {"service_id": SOURCE, "id": costume_id,
                            "revision": revision,
                            "content_hash": row["content_hash"]},
            "character_ref": _loads(costume["character_ref"], None),
            "costume_kind": costume["costume_kind"],
            "variant": resolved,
            "artifacts": artifacts,
            "outfit": _loads(row["outfit"], {}),
            "material_palette": _loads(row["material_palette"], {}),
            "accessories": _loads(row["accessories"], []),
            "variant_axes": axes,
            "rights_ref": _loads(row["rights_ref"], {}),
            "is_canonical": costume["canonical_revision"] == revision,
            "canonical_revision": costume["canonical_revision"],
            "rights_recheck_required": True,
        }

    def get_canonical(self, tenant_id: str, costume_id: str) -> dict:
        self._load_costume(tenant_id, costume_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=? AND costume_id=?"
            " AND superseded_at IS NULL", (tenant_id, costume_id)).fetchone())
        if pointer is None:
            # 与"没有这套服装"分开：服装在、只是还没定 canonical
            # （`generic_wardrobe` 可以永久如此，CCA-7）。
            raise ServiceError(404, "CANONICAL_NOT_SET",
                               f"costume {costume_id} has no canonical revision")
        row = _row(self.conn.execute(
            "SELECT content_hash FROM costume_revision WHERE tenant_id=?"
            " AND costume_id=? AND revision=?",
            (tenant_id, costume_id, pointer["revision"])).fetchone())
        return {"costume_ref": {"service_id": SOURCE, "id": costume_id,
                                "revision": pointer["revision"],
                                "content_hash": (row or {}).get("content_hash")},
                "canonical": _pointer_out(pointer),
                "rights_recheck_required": True}

    def lookup(self, tenant_id: str, *, series_scope: str,
               alias: str) -> dict:
        """`GET /costumes:lookup`（C12-9）。

        唯一键是 (series_scope, display_name, costume_kind)，所以同名不同类
        会命中多行。返回 `items[]` 而不是"第一个"——取第一个会让结果取决于
        插入顺序。没有模糊匹配，也没有第二张别名表。
        """
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(alias, str) and alias, "alias is required")
        rows = self.conn.execute(
            "SELECT * FROM costume WHERE tenant_id=? AND series_scope=?"
            " AND display_name=? ORDER BY costume_kind, costume_id",
            (tenant_id, series_scope, alias)).fetchall()
        if not rows:
            raise ServiceError(404, "NOT_FOUND",
                               f"no costume named {alias!r} in {series_scope!r}")
        return {"items": [_costume_out(dict(r), created=None) for r in rows],
                "rights_recheck_required": True}

    # ------------------------------------------------------------ 事件消费
    def consume(self, envelope: dict) -> dict:
        """§5.3 的两条事件。**只做本地标记**，不改任何 revision、不发事件。

        - `asset.prop.revision.accepted`（S14）：把 `unverified_refs` 里匹配
          的项标为已验证。CCA-3：S14 完全不知道 `unverified_refs` 这回事，
          所以这是 S12 自己的本地事实，不是跨服务协议。
        - `artifact.archived`（S28）：审计。S12 不因归档删任何行——归档是
          S28 的处置，revision 仍然指向那个 id，是否还能取到图是 S28 的事。

        不认识的事件不是错误：不回 4xx 让上游重试（上游会永远重试）。
        """
        event_type = envelope.get("type")
        if event_type not in (EVENT_PROP_REVISION_ACCEPTED,
                              "asset.prop.revision.accepted",
                              "artifact.archived"):
            return {"consumed": False, "reason": "unrouted"}
        tenant_id = envelope.get("tenant_id")
        event_id = envelope.get("id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ServiceError(422, "INVALID_CONTEXT", "event lacks tenant_id")
        if not isinstance(event_id, str) or not event_id:
            raise ServiceError(422, "INVALID_CONTEXT", "event lacks id")
        consumer = (CONSUMER_PROP_REVISION
                    if event_type == EVENT_PROP_REVISION_ACCEPTED
                    else CONSUMER_ARTIFACT_ARCHIVED)
        with Transaction(self.conn):
            if not self.inbox.consume(consumer, event_id):
                return {"consumed": False, "reason": "duplicate"}
            if event_type == EVENT_PROP_REVISION_ACCEPTED:
                return {"consumed": True,
                        **self._on_prop_revision(tenant_id, envelope)}
            return {"consumed": True, "audited": True, "changed": 0}

    def _on_prop_revision(self, tenant_id: str, envelope: dict) -> dict:
        data = envelope.get("data") or {}
        prop_ref = data.get("prop_ref") or {}
        prop_id = prop_ref.get("id")
        revision = prop_ref.get("revision")
        if not prop_id:
            return {"verified": 0}
        sql = ("UPDATE costume_unverified_ref SET verified_at=?, verified_by=? "
               "WHERE tenant_id=? AND target_service='S14' AND target_id=? "
               "AND verified_at IS NULL")
        params: list = [_now(), "S14", tenant_id, prop_id]
        if isinstance(revision, int) and not isinstance(revision, bool):
            # 只消解"指定了这一版"或"没指定版本"的引用；指向别的 revision 的
            # 引用还悬空。把 revision 忽略掉会把"引用了第 1 版、S14 出了第 2 版"
            # 误标成已验证。
            sql += " AND (target_revision IS NULL OR target_revision=?)"
            params.append(revision)
        cur = self.conn.execute(sql, tuple(params))
        return {"verified": cur.rowcount}


# ------------------------------------------------------------ 纯函数：装配

def _artifact_ref(ref) -> dict:
    """`variant_manifest` 的叶子。允许 `"art_1"` 或
    `{"artifact_id":..., "content_hash":...}`。"""
    if isinstance(ref, str):
        return {"artifact_id": ref, "content_hash": None}
    if isinstance(ref, dict):
        artifact_id = ref.get("artifact_id") or ref.get("id")
        return {"artifact_id": artifact_id,
                "content_hash": ref.get("content_hash") or ref.get("sha256")}
    return {"artifact_id": None, "content_hash": None}


def _costume_out(costume: dict, *, created) -> dict:
    out = {
        "costume_id": costume["costume_id"],
        "project_id": costume["project_id"],
        "series_scope": costume["series_scope"],
        "display_name": costume["display_name"],
        "costume_kind": costume["costume_kind"],
        # 软引用原样返回：调用方要自己去 S11 解析（§1.2）。
        "character_ref": _loads(costume["character_ref"], None),
        "status": costume["status"],
        "canonical_revision": costume["canonical_revision"],
        "current_revision": costume["current_revision"],
        "created_at": costume["created_at"],
    }
    if created is not None:
        out["created"] = created
    return out


def _pointer_out(pointer) -> dict | None:
    if pointer is None:
        return None
    return {
        "revision": pointer["revision"],
        "decided_at": pointer["decided_at"],
        "decided_by": _loads(pointer["decided_by"], {}),
        "decision_kind": pointer["decision_kind"],
        "reason_text": pointer["reason_text"],
        "prior_canonical": pointer["prior_canonical"],
    }


def _parse_variant_selector(value: str | None) -> dict[str, str]:
    """`damage:torn_shoulder,wetness:damp` → `{"damage": ..., ...}`。"""
    if not value:
        return {}
    selector: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"variant selector {part!r} must be axis:value")
        axis, _, chosen = part.partition(":")
        axis, chosen = axis.strip(), chosen.strip()
        if not axis or not chosen:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"variant selector {part!r} must be axis:value")
        if axis in selector and selector[axis] != chosen:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                f"variant axis {axis!r} was selected twice")
        selector[axis] = chosen
    return selector


def _expired(expires_at: str) -> bool:
    try:
        deadline = datetime.fromisoformat(
            str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return deadline <= _now_dt()
