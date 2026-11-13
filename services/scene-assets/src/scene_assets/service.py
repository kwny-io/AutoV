"""S13 内核。方案：`plans/S13-scene-assets.md`。

一句话边界：**S13 是「这个空间长什么样、门在哪、窗朝哪、轴线怎么走」的唯一权威。**

它不判越轴（S17）、不存机位（S09）、不记谁在场（S16）、不存参考图字节
（S28）、不把可移动道具当固定物（S14）。§0.2 的四条「不做」在这里是结构性的：
没有 shot 表、没有 occupant 表、没有 axis_violation 表，`scene_fixed_object`
没有状态列。

三层分开（§1.1），它们的物理形态不同、更新路径也不同：

1. **拓扑**（`topology`）——权威，机器可校验，hash 单独发布（`topology_hash`）。
2. **anchor**（`anchor_manifest` / `scene_anchor`）——证据，一旦封存永不替换。
   换 anchor 必须接受新 revision，旧 revision 的 anchor 仍可读。
3. **光照**（`lighting_manifest` / `scene_lighting_variant`）——派生，
   同一空间在别的光照下的渲染；**读不到就是读不到**，绝不退回 `day`。

四个刻意的保守分支，它们的默认行为都会让下游拿到错误的几何：

- S28 不可达 → 503 且**不落候选**（F16）。把超时当成「artifact 不存在」
  会把一次抖动变成一次 422，调用方会去换素材而不是重试。
- 请求不存在的 lighting → 422（F11）。退回 day 会让夜景镜头用白天光照生成，
  且 S17 可能判「通过」。
- `topology_changed=true` 缺影响计划 → 422（F9）。没有影响计划就不接受，
  否则所有已生成的镜头都可能作废而系统不报错。
- 库里的 `canonical_revision` / `topology_hash` **只在 canonical 真的移动时**
  才变（S13-12）。新 revision 不等于新 canonical（§1.5）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Outbox, new_event

from . import geometry
from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA

SOURCE = "S13"
SCHEMA_VERSION = "2.0"

EVENT_SCENE_REVISION_ACCEPTED = "asset.scene.revision.accepted"
TOPIC_SCENE = "drama.scene-assets.v2"

# §2.6 的 `expires_at DEFAULT now() + interval '30 days'`。
CANDIDATE_TTL_DAYS = 30

# 一次候选最多带多少个固定物 / anchor。方案没给上限；有上限才能在
# 一个明显是脚本错误的请求上快速失败，而不是写十万行派生数据。
MAX_ANCHORS = 64
MAX_FIXED_OBJECTS = 2000

SCENE_KINDS = frozenset({"interior", "exterior", "hybrid", "abstract", "vehicle"})
LOCATION_CLASSES = frozenset({"primary_set", "recurring_location", "one_off",
                              "background"})
SOURCE_KINDS = frozenset({"manual_upload", "generated", "imported", "derived",
                          "scanned_floorplan"})
DECISION_KINDS = frozenset({"human_approved", "policy_auto", "initial_import"})

# 事件里 era 只允许这三个键（契约 `additionalProperties: false`）。
ERA_EVENT_KEYS = ("label", "year_band", "region")


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
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。

    不能写成 `with conn:`：连接是 `isolation_level=None`（autocommit），
    那时 `with conn:` 既不 BEGIN 也不 COMMIT，只在异常时回滚一个不存在的
    事务——锁与原子性都没有。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：哈希

def topology_hash(topology: dict) -> str:
    """只哈希拓扑。§1.2：让调用方能问「空间变了没有」而不必比对整包。"""
    return _sha256_hex(_jcs(topology))


def revision_content_hash(payload: dict) -> str:
    """整包 sha256(JCS(payload))。

    **哈希对象是本仓库自定的投影**：`contracts/assets/scene-revision.v1
    .schema.json` 还不存在（§11.1 第 1 条要求它先写），所以两个实现今天
    算不出同一个数。见 CONTRACT_GAPS 的 S13-1。投影只含 revision 的业务
    内容，不含 `accepted_at` / `accepted_by` / `revision` / `scene_asset_id`
    ——那几项每次接受都不同，含进去会让 E12「重复接受返回首次结果」在
    hash 层就无法自洽。
    """
    projection = {key: payload.get(key) for key in (
        "source_kind", "era_snapshot", "topology", "anchor_manifest",
        "lighting_manifest", "lighting_axes", "fixed_objects", "rights_ref",
        "quality_gate_ref", "topology_changed")}
    return _sha256_hex(_jcs(projection))


# ------------------------------------------------------------ 纯函数：入参

def _require_object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be an object")
    return value


def _require_list(value, name: str) -> list:
    if not isinstance(value, list):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be an array")
    return value


def _require_str(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{name} must be a non-empty string")
    return value


def _require_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{name} must be an integer")
    return value


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be a boolean")
    return value


def _era(value) -> dict:
    era = _require_object(value, "era")
    _require_str(era.get("label"), "era.label")
    for key in era:
        if key not in ERA_EVENT_KEYS:
            # 事件的 era 是 additionalProperties:false。库里存得下多余键，
            # 但发事件时会被裁掉；与其让两个地方看见两份 era，不如在入口就拒。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"era has unknown key {key!r}; allowed keys are "
                               f"{list(ERA_EVENT_KEYS)}")
    for key in ("year_band", "region"):
        if key in era and not isinstance(era[key], str):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"era.{key} must be a string")
    return era


def _artifact_ref(value, where: str) -> dict:
    """`{service_id:'S28', id, content_hash}` / `{artifact_id, …}` / 字符串 id。

    只要求「能指向一个 artifact」。`content_hash` 可以缺——缺了就没法比对，
    这种引用会在 `evaluation` 里记成 `hash_verified=false`，而不是被当成
    「hash 一致」。
    """
    if isinstance(value, str):
        if not value:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"{where}.artifact_ref is empty")
        return {"id": value, "content_hash": None}
    ref = _require_object(value, f"{where}.artifact_ref")
    artifact_id = ref.get("artifact_id") or ref.get("id")
    _require_str(artifact_id, f"{where}.artifact_ref.id")
    content_hash = ref.get("content_hash") or ref.get("sha256")
    if content_hash is not None:
        content_hash = _require_str(content_hash, f"{where}.artifact_ref.content_hash")
        if len(content_hash) != 64 or any(c not in "0123456789abcdef"
                                          for c in content_hash):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"{where}.artifact_ref.content_hash must be 64 "
                               f"lowercase hex")
    return {"service_id": ref.get("service_id") or "S28",
            "id": artifact_id, "revision": ref.get("revision"),
            "content_hash": content_hash}


def _impact_plan_ref(value) -> dict | None:
    """`{service_id, id, revision}` 或 None。三键齐、id 非空（SCN-1）。

    **不检查 service_id 是不是 S09。** F18 补充说得很清楚：S13 只校验
    「有值」的形态，不 dereference、不判归属。SCN-1 里 S09/S07 的命名
    冲突不是本服务能裁决的。
    """
    if value is None:
        return None
    ref = _require_object(value, "impact_plan_ref")
    _require_str(ref.get("service_id"), "impact_plan_ref.service_id")
    _require_str(ref.get("id"), "impact_plan_ref.id")
    revision = ref.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           "impact_plan_ref.revision must be an integer >= 1")
    return {"service_id": ref["service_id"], "id": ref["id"],
            "revision": revision}


def _anchor_entry(anchor_id: str, raw) -> dict:
    """把 manifest 的一项规范成内部形态。

    同时接受方案 §2.2 注释里的 `{artifact_ref, camera}` 与 §4.3 响应里的
    扁平形态。两种都在仓库里出现过，收一种就等于让另一种静默失败。
    """
    entry = _require_object(raw, f"anchor_manifest.{anchor_id}")
    camera = entry.get("camera") if isinstance(entry.get("camera"), dict) else {}
    fused = {**camera, **{k: v for k, v in entry.items() if k != "camera"}}

    position = fused.get("position_m")
    if not isinstance(position, dict):
        # 也接受 [x, y] / [x, y, z]：拓扑用数组，anchor 用对象，两处都在
        # 方案的示例里出现过。
        if isinstance(position, (list, tuple)) and len(position) >= 2:
            position = {"x": position[0], "y": position[1]}
            if len(position) >= 3:
                position["z"] = position[2]
        else:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"anchor_manifest.{anchor_id}.position_m must be "
                               f"an object or a point")
    for axis in ("x", "y"):
        value = position.get(axis)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"anchor_manifest.{anchor_id}.position_m.{axis} "
                               f"must be a number")
    yaw = fused.get("yaw_deg")
    if isinstance(yaw, bool) or not isinstance(yaw, (int, float)):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"anchor_manifest.{anchor_id}.yaw_deg must be a number")
    hfov = fused.get("hfov_deg")
    if isinstance(hfov, bool) or not isinstance(hfov, (int, float)):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"anchor_manifest.{anchor_id}.hfov_deg must be a number")
    if not (0 < float(hfov) < 180):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"anchor_manifest.{anchor_id}.hfov_deg must be in "
                           f"(0, 180)")
    pitch = fused.get("pitch_deg", 0)
    if isinstance(pitch, bool) or not isinstance(pitch, (int, float)):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"anchor_manifest.{anchor_id}.pitch_deg must be a number")
    return {
        "anchor_id": anchor_id,
        "position_m": position,
        "yaw_deg": float(yaw),
        "pitch_deg": float(pitch),
        "hfov_deg": float(hfov),
        "unit_id": _require_str(fused.get("unit_id"),
                                f"anchor_manifest.{anchor_id}.unit_id"),
        "looks_at": fused.get("looks_at"),
        "artifact_ref": _artifact_ref(
            fused.get("artifact_ref"),
            f"anchor_manifest.{anchor_id}"),
        "lighting_kind": fused.get("lighting_kind") or "day",
    }


# ------------------------------------------------------------------ 服务

class SceneService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_source=None,
                 authorizer=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        # S28：draft 路径必需（§4.1 第 5 条）。未注入时必然 503，不是跳过。
        self.artifact_source = artifact_source
        # S01：今天不在主路径上（见 CONTRACT_GAPS S13-5），保留注入点。
        self.authorizer = authorizer

    # ------------------------------------------------------------ 读场景头
    def _scene_row(self, tenant_id: str, scene_asset_id: str):
        row = self.conn.execute(
            "SELECT * FROM scene_asset WHERE tenant_id=? AND scene_asset_id=?",
            (tenant_id, scene_asset_id)).fetchone()
        if row is None:
            # 跨租户 → 404，不确认存在（E28）。
            raise ServiceError(404, "NOT_FOUND",
                               f"no scene asset {scene_asset_id}")
        return row

    def _revision_row(self, tenant_id: str, scene_asset_id: str, revision: int):
        row = self.conn.execute(
            "SELECT * FROM scene_revision WHERE tenant_id=? AND scene_asset_id=?"
            " AND revision=?", (tenant_id, scene_asset_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"scene {scene_asset_id} has no revision {revision}")
        return row

    # ------------------------------------------------------------ 登记场景
    def create_scene(self, tenant_id: str, command_id: str, body: dict, *,
                     request_hash: str | None = None) -> dict:
        """`POST /scene-assets`。§6.1：`(tenant_id, series_scope, display_name)`
        唯一，所以重复登记**返回既有 ID**（E12），不是报错。

        身份字段（project_id / scene_kind / location_class / era.label）任一
        不同时不能复用同一行——那等于把两个不同的空间合成一个。方案没有
        对应的错误码，用 `SCENE_IDENTITY_CONFLICT`（S13-4）。
        """
        body = _require_object(body, "body")
        project_id = _require_str(body.get("project_id"), "project_id")
        series_scope = _require_str(body.get("series_scope"), "series_scope")
        display_name = _require_str(body.get("display_name"), "display_name")
        scene_kind = _require_str(body.get("scene_kind"), "scene_kind")
        if scene_kind not in SCENE_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"scene_kind must be one of {sorted(SCENE_KINDS)}")
        location_class = _require_str(body.get("location_class"), "location_class")
        if location_class not in LOCATION_CLASSES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"location_class must be one of "
                               f"{sorted(LOCATION_CLASSES)}")
        era = _era(body.get("era"))
        created_by = body.get("created_by")
        if created_by is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "created_by is required")
        if not isinstance(created_by, (dict, str)):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "created_by must be an object or a string")

        existing = self.conn.execute(
            "SELECT * FROM scene_asset WHERE tenant_id=? AND series_scope=?"
            " AND display_name=?",
            (tenant_id, series_scope, display_name)).fetchone()
        if existing is not None:
            same = (existing["project_id"] == project_id
                    and existing["scene_kind"] == scene_kind
                    and existing["location_class"] == location_class
                    and _loads(existing["era"], {}) == era)
            if not same:
                raise ServiceError(
                    409, "SCENE_IDENTITY_CONFLICT",
                    f"{display_name!r} already exists in series {series_scope!r} "
                    f"with a different project/scene_kind/location_class/era; a "
                    f"different space needs a different scene_asset_id",
                    details={"scene_asset_id": existing["scene_asset_id"]})
            return {"scene_asset_id": existing["scene_asset_id"],
                    "revision": existing["current_revision"],
                    "status": existing["status"], "existing": True}

        scene_asset_id = _new_id("scene")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO scene_asset (tenant_id, scene_asset_id, project_id,"
                " series_scope, display_name, scene_kind, era, location_class,"
                " status, canonical_revision, current_revision, topology_hash,"
                " created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0, NULL, ?, ?)",
                (tenant_id, scene_asset_id, project_id, series_scope, display_name,
                 scene_kind, _jcs(era), location_class,
                 _jcs(created_by) if isinstance(created_by, dict) else created_by,
                 _now()))
        return {"scene_asset_id": scene_asset_id, "revision": 0,
                "status": "active", "existing": False}

    # ------------------------------------------------------------ 起草候选
    def draft_candidate(self, tenant_id: str, command_id: str,
                        scene_asset_id: str, body: dict, *,
                        request_hash: str | None = None) -> dict:
        """`POST /scene-assets/{id}/candidates`。§4.1 的七步，顺序照抄。

        事务外校验 → S28 → 事务内锁 + CAS + 写候选。S28 调用放在事务外，
        是因为一次 5 秒的网络往返不该持有 `scene_asset` 的行锁（§4.1 的
        标题也写着「事务外校验」；第 5 条是下游调用，是那一节的例外，
        记在 CONTRACT_GAPS 的 S13-13）。
        """
        body = _require_object(body, "body")
        expected = _require_int(body.get("expected_current_revision"),
                                "expected_current_revision")
        source_kind = _require_str(body.get("source_kind"), "source_kind")
        if source_kind not in SOURCE_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"source_kind must be one of {sorted(SOURCE_KINDS)}")
        era_snapshot = _era(body.get("era_snapshot"))
        topology = _require_object(body.get("topology"), "topology")
        anchor_manifest = _require_object(body.get("anchor_manifest"),
                                          "anchor_manifest")
        lighting_manifest = _require_object(body.get("lighting_manifest"),
                                            "lighting_manifest")
        lighting_axes = _require_list(body.get("lighting_axes"), "lighting_axes")
        fixed_objects = _require_list(body.get("fixed_objects", []),
                                      "fixed_objects")
        rights_ref = _require_object(body.get("rights_ref"), "rights_ref")
        if not rights_ref:
            # SCN-4：键集未定义，但「非空对象」是契约给的下限。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "rights_ref must be a non-empty object")
        quality_gate_ref = body.get("quality_gate_ref")
        if quality_gate_ref is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "quality_gate_ref is required (it may be an "
                               "opaque object or string, but not absent)")
        topology_changed = _bool(body.get("topology_changed"), "topology_changed")

        scene = self._scene_row(tenant_id, scene_asset_id)
        if scene["status"] == "tombstoned":
            # §3.4：已下线的场景不能有新 canonical，也不该接受新 revision。
            raise ServiceError(422, "SCENE_TOMBSTONED",
                               f"scene {scene_asset_id} is tombstoned")
        if expected != scene["current_revision"]:
            raise ServiceError(409, "VERSION_CONFLICT",
                               f"expected_current_revision={expected} but scene "
                               f"is at {scene['current_revision']}",
                               retryable=True,
                               details={"current_revision":
                                        scene["current_revision"]})

        location_class = scene["location_class"]
        era_label = _loads(scene["era"], {}).get("label")

        # ---- 1) 拓扑合法性
        problems, facts = geometry.validate_topology(topology)
        if problems:
            first = problems[0]
            raise ServiceError(422, "TOPOLOGY_INVALID",
                               f"topology is invalid: {first['reason']}",
                               details={"target": first["target"],
                                        "reason": first["reason"],
                                        "problems": problems})
        parsed_units = facts["parsed_units"]

        # ---- anchor 解析（2、3、4 都要它）
        anchors = self._parse_anchors(anchor_manifest)

        # ---- 2) anchor 覆盖
        coverage = geometry.anchor_coverage(len(anchors), location_class)
        if not coverage["ok"]:
            raise ServiceError(
                422, "ANCHOR_COVERAGE_INSUFFICIENT",
                f"{location_class} needs at least {coverage['required']} anchors, "
                f"got {coverage['actual']}",
                details=coverage)
        if location_class == "primary_set":
            # §4.1 第 2 条：`primary_set` 要求「覆盖 360°」。方案没给角度
            # 门槛，取两两最小夹角的最大值 >= 30°（S13-8）。
            spread = geometry.yaw_spread([a["yaw_deg"] for a in anchors])
            if spread < geometry.PRIMARY_SET_MIN_YAW_SPREAD_DEG:
                raise ServiceError(
                    422, "ANCHOR_COVERAGE_INSUFFICIENT",
                    f"primary_set anchors cover only {spread:.1f} degrees; "
                    f"they must span at least "
                    f"{geometry.PRIMARY_SET_MIN_YAW_SPREAD_DEG} degrees "
                    f"(threshold pending owner sign-off, CONTRACT_GAPS S13-8)",
                    details={"required": coverage["required"],
                             "actual": coverage["actual"],
                             "yaw_spread_deg": spread})

        # ---- 3) anchor 必须在自己的 unit 内
        for anchor in anchors:
            anchor_problems, _ = geometry.validate_anchor_position(
                anchor, parsed_units)
            if anchor_problems:
                first = anchor_problems[0]
                raise ServiceError(422, "ANCHOR_OUTSIDE_UNIT",
                                   f"anchor {anchor['anchor_id']}: "
                                   f"{first['reason']}",
                                   details={"anchor_id": anchor["anchor_id"]})

        # ---- 4) 光照矩阵
        variants = self._parse_lighting(anchors, lighting_axes, lighting_manifest)
        for variant in variants:
            # 光照矩阵里的每一格也必须有真实素材，否则读路径会在接受之后才
            # 发现一个空引用。这里只是把引用收集起来，S28 检查在下面统一做。
            if not variant["artifact_ref"].get("id"):
                raise ServiceError(
                    422, "LIGHTING_COVERAGE_INCOMPLETE",
                    f"anchor {variant['anchor_id']!r} {variant['lighting']!r} "
                    f"variant has no artifact id",
                    details={"anchor_id": variant["anchor_id"],
                             "lighting": variant["lighting"]})

        # ---- 6) 固定物足迹（先于 S28：本地能判的错误不必等网络）
        objects = self._parse_fixed_objects(fixed_objects)
        for obj in objects:
            object_problems = geometry.validate_fixed_object(obj, parsed_units)
            if object_problems:
                first = object_problems[0]
                raise ServiceError(422, "FIXED_OBJECT_OUT_OF_BOUNDS",
                                   f"fixed object {obj['object_id']}: "
                                   f"{first['reason']}",
                                   details={"object_id": obj["object_id"]})

        # ---- 7) topology_changed 自洽
        parent = None
        if scene["current_revision"] > 0:
            parent = self._revision_row(tenant_id, scene_asset_id,
                                        scene["current_revision"])
        new_topology_hash = topology_hash(topology)
        parent_topology_hash = parent["topology_hash"] if parent else None
        actual_changed = (parent is not None
                          and new_topology_hash != parent_topology_hash)
        if topology_changed and not actual_changed:
            raise ServiceError(
                422, "TOPOLOGY_CHANGE_DECLARED_BUT_NONE",
                "topology_changed=true but topology_hash equals the parent's; "
                "declare false (this would otherwise gate on a human review "
                "that has nothing to review)",
                details={"topology_hash": new_topology_hash,
                         "parent_topology_hash": parent_topology_hash})
        if not topology_changed and actual_changed:
            raise ServiceError(
                422, "TOPOLOGY_CHANGE_NOT_DECLARED",
                "topology_hash differs from the parent's but topology_changed "
                "is false; downstream would reuse stale spatial checks",
                details={"topology_hash": new_topology_hash,
                         "parent_topology_hash": parent_topology_hash})

        # ---- 5) artifact 校验（S28 HEAD）
        evaluation = self._verify_artifacts(
            tenant_id, anchors, variants,
            parent_revision=scene["current_revision"])

        payload = {
            "source_kind": source_kind,
            "era_snapshot": era_snapshot,
            "topology": topology,
            "anchor_manifest": anchor_manifest,
            "lighting_manifest": lighting_manifest,
            "lighting_axes": list(lighting_axes),
            "fixed_objects": fixed_objects,
            "rights_ref": rights_ref,
            "quality_gate_ref": quality_gate_ref,
            "topology_changed": topology_changed,
        }
        digest = revision_content_hash(payload)
        spatial_check = {
            "topology_valid": facts["topology_valid"],
            "openings_attached": facts["openings_attached"],
            "axis_check": {
                # S13 **不判**越轴（§0.2 第 1 条）。这里只记录拓扑里声明的
                # 北向，供 S17 自己判；字段名沿用方案的 `axis_check`，但
                # 内容是「输入」而不是「结论」。
                "north_axis_deg": (topology.get("orientation") or {})
                                    .get("north_axis_deg"),
                "checked_by": None,
            },
            "anchor_coverage": coverage,
            "anchors_inside_units": len(anchors),
            "openings": facts["openings"],
            "units": facts["units"],
        }
        evaluation = {**evaluation,
                      "era_label_matches_scene": era_snapshot.get("label") == era_label}

        expires_at = (_now_dt() + timedelta(days=CANDIDATE_TTL_DAYS)) \
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        candidate_id = _new_id("candidate")
        now = _now()
        with Transaction(self.conn):
            # 锁 scene_asset 后再 CAS 一次：事务外的校验可能跑了几秒
            # （S28），期间别的接受可能已经推进了 current_revision。
            row = self.conn.execute(
                "SELECT current_revision, status FROM scene_asset"
                " WHERE tenant_id=? AND scene_asset_id=?",
                (tenant_id, scene_asset_id)).fetchone()
            if row is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no scene asset {scene_asset_id}")
            if row["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"scene {scene_asset_id} is tombstoned")
            if row["current_revision"] != expected:
                raise ServiceError(409, "VERSION_CONFLICT",
                                   f"expected_current_revision={expected} but "
                                   f"scene moved to {row['current_revision']}",
                                   retryable=True,
                                   details={"current_revision":
                                            row["current_revision"]})
            self.conn.execute(
                "INSERT INTO scene_candidate (tenant_id, candidate_id,"
                " scene_asset_id, content_hash, topology_hash, payload,"
                " spatial_check, evaluation, state, created_by, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
                (tenant_id, candidate_id, scene_asset_id, digest,
                 new_topology_hash, _jcs(payload), _jcs(spatial_check),
                 _jcs(evaluation),
                 _jcs(body.get("created_by") or {"service_id": SOURCE}),
                 now, expires_at))

        return {
            "candidate_id": candidate_id,
            "scene_asset_id": scene_asset_id,
            "state": "PENDING",
            "content_hash": digest,
            "topology_hash": new_topology_hash,
            "topology_changed": topology_changed,
            "spatial_check": spatial_check,
            "evaluation": evaluation,
            "expires_at": expires_at,
        }

    # ------------------------------------------------------------ 接受候选
    def accept_candidate(self, tenant_id: str, command_id: str,
                         scene_asset_id: str, candidate_id: str, body: dict, *,
                         request_hash: str | None = None) -> dict:
        """`POST /scene-assets/{id}/candidates/{cid}:accept`。§4.2 的九步。

        派生表（anchor / lighting / fixed_object / anchor_relationship）是
        `payload` 的展开，可从 payload 重建（runbook `rebuild-scene-derivations`）。
        `payload` 是权威，派生表只服务查询。
        """
        body = _require_object(body, "body")
        expected = _require_int(body.get("expected_current_revision"),
                                "expected_current_revision")
        set_canonical = bool(body.get("set_canonical", False))
        decision_kind = _require_str(body.get("decision_kind"), "decision_kind")
        if decision_kind not in DECISION_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"decision_kind must be one of "
                               f"{sorted(DECISION_KINDS)}")
        impact_plan_ref = _impact_plan_ref(body.get("impact_plan_ref"))
        reason_text = body.get("reason_text")
        # 事件封套要求 `home_cell`。它不是这个命令的业务参数，但也不能靠猜：
        # 调用方在 body 里给就原样带上，没给就记 `unknown`——伪造一个 cell
        # 会让事件被路由到错误的区域。
        home_cell = str(body.get("home_cell") or "unknown")

        accepted_at = _now()
        accepted_by = body.get("accepted_by")
        if accepted_by is None:
            accepted_by = {"service_id": SOURCE, "principal_id": command_id}

        # TTL 到期是终态，而且必须**留在库里**。下面的事务在拒绝时会
        # ROLLBACK，所以「顺手把 state 改成 EXPIRED 再抛错」什么也留不下
        # ——每次重试都会重新看到 PENDING。这里单独提交一次状态转移，
        # 条件里带 state='PENDING'，并发下只有一个请求能把它翻过去。
        expired = self.conn.execute(
            "UPDATE scene_candidate SET state='EXPIRED'"
            " WHERE tenant_id=? AND candidate_id=? AND scene_asset_id=?"
            " AND state='PENDING' AND expires_at <= ?",
            (tenant_id, candidate_id, scene_asset_id, accepted_at))
        if expired.rowcount == 1:
            raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                               f"candidate {candidate_id} has expired; a "
                               f"candidate is a snapshot, not a standing offer")

        with Transaction(self.conn):
            scene = self.conn.execute(
                "SELECT * FROM scene_asset WHERE tenant_id=? AND scene_asset_id=?",
                (tenant_id, scene_asset_id)).fetchone()
            if scene is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no scene asset {scene_asset_id}")
            if scene["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"scene {scene_asset_id} is tombstoned")
            if scene["current_revision"] != expected:
                # F15 / F18：行锁 + CAS。两个并发接受里只有一个能过这一关。
                raise ServiceError(409, "VERSION_CONFLICT",
                                   f"expected_current_revision={expected} but "
                                   f"scene is at {scene['current_revision']}",
                                   retryable=True,
                                   details={"current_revision":
                                            scene["current_revision"]})

            candidate = self.conn.execute(
                "SELECT * FROM scene_candidate WHERE tenant_id=?"
                " AND candidate_id=?", (tenant_id, candidate_id)).fetchone()
            if candidate is None or candidate["scene_asset_id"] != scene_asset_id:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no candidate {candidate_id} for scene "
                                   f"{scene_asset_id}")
            if candidate["state"] != "PENDING":
                raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                                   f"candidate {candidate_id} is "
                                   f"{candidate['state']}, not PENDING")

            payload = _loads(candidate["payload"], {})
            spatial_check = _loads(candidate["spatial_check"], {})
            topology_changed = bool(payload.get("topology_changed"))

            # 3) 拓扑变更门：人审 + 影响计划引用。§3.3 的两条硬约束。
            self._assert_topology_change_gated(
                topology_changed=topology_changed,
                decision_kind=decision_kind,
                impact_plan_ref=impact_plan_ref)

            # 4) 主场景要成为 canonical 时必须有完整拓扑（F10 / E05-D2）。
            if (set_canonical and scene["location_class"] == "primary_set"
                    and spatial_check.get("topology_valid") is not True):
                raise ServiceError(
                    422, "TOPOLOGY_INVALID_FOR_PRIMARY",
                    "a primary_set scene cannot become canonical without a "
                    "valid topology; its spatial checks would be meaningless")

            # 5) 新 revision
            revision = int(scene["current_revision"]) + 1
            duplicate = self.conn.execute(
                "SELECT scene_asset_id, revision FROM scene_revision"
                " WHERE tenant_id=? AND content_hash=?",
                (tenant_id, candidate["content_hash"])).fetchone()
            if duplicate is not None:
                raise ServiceError(
                    409, "DUPLICATE_SCENE_CONTENT",
                    f"this exact revision content already exists as "
                    f"{duplicate['scene_asset_id']}@{duplicate['revision']}",
                    details={"scene_asset_id": duplicate["scene_asset_id"],
                             "revision": duplicate["revision"]})

            parent_revision = int(scene["current_revision"]) or None
            previous_topology_hash = scene["topology_hash"]
            try:
                self.conn.execute(
                    "INSERT INTO scene_revision (tenant_id, scene_asset_id,"
                    " revision, content_hash, topology_hash, payload, topology,"
                    " anchor_manifest, fixed_objects, lighting_axes,"
                    " lighting_manifest, era_snapshot, rights_ref, source_kind,"
                    " parent_revision, topology_changed, impact_plan_ref,"
                    " quality_gate_ref, accepted_at, accepted_by)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?)",
                    (tenant_id, scene_asset_id, revision,
                     candidate["content_hash"], candidate["topology_hash"],
                     _jcs(payload), _jcs(payload.get("topology") or {}),
                     _jcs(payload.get("anchor_manifest") or {}),
                     _jcs(payload.get("fixed_objects") or []),
                     _jcs(payload.get("lighting_axes") or []),
                     _jcs(payload.get("lighting_manifest") or {}),
                     _jcs(payload.get("era_snapshot") or {}),
                     _jcs(payload.get("rights_ref") or {}),
                     payload.get("source_kind"), parent_revision,
                     1 if topology_changed else 0,
                     _jcs(impact_plan_ref) if impact_plan_ref else None,
                     _jcs(payload.get("quality_gate_ref"))
                     if isinstance(payload.get("quality_gate_ref"), (dict, list))
                     else str(payload.get("quality_gate_ref")),
                     accepted_at,
                     _jcs(accepted_by) if isinstance(accepted_by, dict)
                     else str(accepted_by)))
            except sqlite3.IntegrityError as exc:
                # 唯一索引 `sr_hash_uniq` 是最后一道：预检与插入之间的
                # 竞态由它兜住，仍然翻成 409 而不是 500。
                if "sr_hash_uniq" in str(exc) or "UNIQUE" in str(exc).upper():
                    raise ServiceError(409, "DUPLICATE_SCENE_CONTENT",
                                       "this exact revision content already "
                                       "exists") from exc
                raise

            # 6) 派生展开
            self._expand_derivations(tenant_id, scene_asset_id, revision, payload)

            # 7) canonical
            became_canonical = False
            if set_canonical:
                self.conn.execute(
                    "UPDATE canonical_pointer SET superseded_at=?"
                    " WHERE tenant_id=? AND scene_asset_id=?"
                    " AND superseded_at IS NULL",
                    (accepted_at, tenant_id, scene_asset_id))
                prior = self.conn.execute(
                    "SELECT canonical_revision FROM scene_asset"
                    " WHERE tenant_id=? AND scene_asset_id=?",
                    (tenant_id, scene_asset_id)).fetchone()["canonical_revision"]
                self.conn.execute(
                    "INSERT INTO canonical_pointer (tenant_id, scene_asset_id,"
                    " revision, topology_hash, decided_at, decided_by,"
                    " decision_kind, reason_text, prior_canonical, superseded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (tenant_id, scene_asset_id, revision,
                     candidate["topology_hash"], accepted_at,
                     _jcs(accepted_by) if isinstance(accepted_by, dict)
                     else str(accepted_by),
                     decision_kind, reason_text, prior))
                self.conn.execute(
                    "UPDATE scene_asset SET canonical_revision=?,"
                    " current_revision=?, topology_hash=?"
                    " WHERE tenant_id=? AND scene_asset_id=?",
                    (revision, revision, candidate["topology_hash"], tenant_id,
                     scene_asset_id))
                became_canonical = True
            else:
                # §4.2 第 7 步只说「更新 scene_asset」，但 `topology_hash`
                # 的定义是「当前 canonical 的拓扑 hash」（§2.1）。非 canonical
                # 的 revision 不能改写它（S13-12），否则这个列就同时表示
                # 两个意思。
                self.conn.execute(
                    "UPDATE scene_asset SET current_revision=?"
                    " WHERE tenant_id=? AND scene_asset_id=?",
                    (revision, tenant_id, scene_asset_id))

            # 8) 候选终态
            self.conn.execute(
                "UPDATE scene_candidate SET state='ACCEPTED'"
                " WHERE tenant_id=? AND candidate_id=?",
                (tenant_id, candidate_id))

            # 9) 事件与事实同事务
            canonical_revision = self.conn.execute(
                "SELECT canonical_revision FROM scene_asset"
                " WHERE tenant_id=? AND scene_asset_id=?",
                (tenant_id, scene_asset_id)).fetchone()["canonical_revision"]
            self._emit(
                tenant_id=tenant_id, scene_asset_id=scene_asset_id,
                revision=revision, payload=payload,
                content_hash=candidate["content_hash"],
                topology_hash_value=candidate["topology_hash"],
                topology_changed=topology_changed,
                impact_plan_ref=impact_plan_ref,
                became_canonical=became_canonical,
                canonical_revision=canonical_revision,
                location_class=scene["location_class"],
                era_snapshot=payload.get("era_snapshot") or {},
                command_id=command_id, home_cell=home_cell)

        return {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": revision,
                          "content_hash": candidate["content_hash"]},
            "topology_hash": candidate["topology_hash"],
            "previous_topology_hash": previous_topology_hash,
            "parent_revision": parent_revision,
            "topology_changed": topology_changed,
            "became_canonical": became_canonical,
            "canonical_revision": canonical_revision,
            "impact_plan_ref": impact_plan_ref,
            "candidate_id": candidate_id,
        }

    def reject_candidate(self, tenant_id: str, command_id: str,
                         scene_asset_id: str, candidate_id: str, body: dict, *,
                         request_hash: str | None = None) -> dict:
        body = _require_object(body, "body")
        reason_text = body.get("reason_text")
        with Transaction(self.conn):
            self._scene_row(tenant_id, scene_asset_id)
            candidate = self.conn.execute(
                "SELECT * FROM scene_candidate WHERE tenant_id=?"
                " AND candidate_id=?", (tenant_id, candidate_id)).fetchone()
            if candidate is None or candidate["scene_asset_id"] != scene_asset_id:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no candidate {candidate_id} for scene "
                                   f"{scene_asset_id}")
            if candidate["state"] != "PENDING":
                raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                                   f"candidate {candidate_id} is "
                                   f"{candidate['state']}, not PENDING")
            self.conn.execute(
                "UPDATE scene_candidate SET state='REJECTED'"
                " WHERE tenant_id=? AND candidate_id=?",
                (tenant_id, candidate_id))
            evaluation = _loads(candidate["evaluation"], {})
            evaluation = {**evaluation, "rejected_reason": reason_text}
            self.conn.execute(
                "UPDATE scene_candidate SET evaluation=?"
                " WHERE tenant_id=? AND candidate_id=?",
                (_jcs(evaluation), tenant_id, candidate_id))
        return {"candidate_id": candidate_id, "state": "REJECTED"}

    # ------------------------------------------------------------ canonical
    def set_canonical(self, tenant_id: str, command_id: str,
                      scene_asset_id: str, body: dict, *,
                      request_hash: str | None = None) -> dict:
        """`POST /scene-assets/{id}:set-canonical`。

        **不产生 revision、不发事件**（SCN-2）。按事件缓存 canonical 的
        消费方必须回读 `GET /scene-assets/{id}/canonical`。
        """
        body = _require_object(body, "body")
        revision = _require_int(body.get("revision"), "revision")
        decision_kind = _require_str(body.get("decision_kind"), "decision_kind")
        if decision_kind not in DECISION_KINDS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"decision_kind must be one of "
                               f"{sorted(DECISION_KINDS)}")
        reason_text = body.get("reason_text")
        decided_by = body.get("decided_by")
        if decided_by is None:
            decided_by = {"service_id": SOURCE, "principal_id": command_id}

        now = _now()
        with Transaction(self.conn):
            scene = self._scene_row(tenant_id, scene_asset_id)
            if scene["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"scene {scene_asset_id} is tombstoned")
            target = self._revision_row(tenant_id, scene_asset_id, revision)
            # 与 accept 同一条门：拓扑变更的 revision 只能靠人审定 canonical。
            self._assert_topology_change_gated(
                topology_changed=bool(target["topology_changed"]),
                decision_kind=decision_kind,
                impact_plan_ref=_loads(target["impact_plan_ref"], None)
                if target["impact_plan_ref"] else None)

            prior = scene["canonical_revision"]
            self.conn.execute(
                "UPDATE canonical_pointer SET superseded_at=?"
                " WHERE tenant_id=? AND scene_asset_id=?"
                " AND superseded_at IS NULL",
                (now, tenant_id, scene_asset_id))
            self.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, scene_asset_id,"
                " revision, topology_hash, decided_at, decided_by, decision_kind,"
                " reason_text, prior_canonical, superseded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (tenant_id, scene_asset_id, revision, target["topology_hash"],
                 now,
                 _jcs(decided_by) if isinstance(decided_by, dict)
                 else str(decided_by),
                 decision_kind, reason_text, prior))
            self.conn.execute(
                "UPDATE scene_asset SET canonical_revision=?, topology_hash=?"
                " WHERE tenant_id=? AND scene_asset_id=?",
                (revision, target["topology_hash"], tenant_id, scene_asset_id))
        return {"scene_asset_id": scene_asset_id, "revision": revision,
                "prior_canonical": prior, "topology_hash": target["topology_hash"],
                "decision_kind": decision_kind, "became_canonical": True}

    @staticmethod
    def _assert_topology_change_gated(*, topology_changed: bool,
                                      decision_kind: str,
                                      impact_plan_ref: dict | None) -> None:
        """§3.3 / F8 / F9 / §4.2 第 3 步。

        **两侧都调**：accept 与 set-canonical。只在一侧判，另一侧就是绕过。
        顺序照 §4.2：先判人审，再判影响计划——两个都缺时先报"必须人审"，
        因为它是更根本的那条。
        """
        if not topology_changed:
            return
        if decision_kind != "human_approved":
            raise ServiceError(
                422, "TOPOLOGY_CHANGE_REQUIRES_HUMAN",
                f"topology_changed=true requires decision_kind='human_approved', "
                f"got {decision_kind!r}; policy_auto must never move a scene's "
                f"geometry (plan §3.3)")
        if not impact_plan_ref:
            raise ServiceError(
                422, "IMPACT_PLAN_REQUIRED",
                "topology_changed=true requires impact_plan_ref (S07/S09); "
                "without it every shot already generated in this scene may be "
                "invalidated with no trail")

    # ------------------------------------------------------------ 生命周期
    def deprecate(self, tenant_id: str, command_id: str, scene_asset_id: str,
                  body: dict | None, *, request_hash: str | None = None) -> dict:
        with Transaction(self.conn):
            scene = self._scene_row(tenant_id, scene_asset_id)
            if scene["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"scene {scene_asset_id} is tombstoned")
            # active → deprecated（§3.2）。已是 deprecated 时原样返回：
            # 客户端重试一次下线不该变成一次错误。
            if scene["status"] == "active":
                self.conn.execute(
                    "UPDATE scene_asset SET status='deprecated'"
                    " WHERE tenant_id=? AND scene_asset_id=?",
                    (tenant_id, scene_asset_id))
        return {"scene_asset_id": scene_asset_id, "status": "deprecated"}

    def tombstone(self, tenant_id: str, command_id: str, scene_asset_id: str,
                  body: dict | None, *, request_hash: str | None = None) -> dict:
        """终态。**不 supersede canonical 指针**（S13-14）：历史必须留着，
        读路径靠 `status='tombstoned'` 让调用方自己决定要不要用。"""
        with Transaction(self.conn):
            scene = self._scene_row(tenant_id, scene_asset_id)
            if scene["status"] == "tombstoned":
                return {"scene_asset_id": scene_asset_id, "status": "tombstoned"}
            self.conn.execute(
                "UPDATE scene_asset SET status='tombstoned'"
                " WHERE tenant_id=? AND scene_asset_id=?",
                (tenant_id, scene_asset_id))
        return {"scene_asset_id": scene_asset_id, "status": "tombstoned"}

    def merge(self, tenant_id: str, command_id: str, scene_asset_id: str,
              body: dict, *, request_hash: str | None = None) -> dict:
        """`POST /scene-assets/{id}:merge`（S13-2）。

        合并 = 源场景置 `deprecated` 并记下 `merged_into`。**不搬 revision、
        不改目标**：revision 属于源场景的历史，搬到目标下会让目标的 revision
        序列出现不属于它的空间。
        """
        body = _require_object(body, "body")
        target_id = _require_str(body.get("target_scene_asset_id"),
                                 "target_scene_asset_id")
        if target_id == scene_asset_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "a scene cannot be merged into itself")
        with Transaction(self.conn):
            scene = self._scene_row(tenant_id, scene_asset_id)
            if scene["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"scene {scene_asset_id} is tombstoned")
            target = self._scene_row(tenant_id, target_id)
            if target["status"] == "tombstoned":
                raise ServiceError(422, "SCENE_TOMBSTONED",
                                   f"target scene {target_id} is tombstoned")
            if scene["merged_into"] not in (None, target_id):
                raise ServiceError(
                    422, "SCENE_ALREADY_MERGED",
                    f"scene {scene_asset_id} is already merged into "
                    f"{scene['merged_into']}; merging is not re-targetable")
            self.conn.execute(
                "UPDATE scene_asset SET status='deprecated', merged_into=?"
                " WHERE tenant_id=? AND scene_asset_id=?",
                (target_id, tenant_id, scene_asset_id))
        return {"scene_asset_id": scene_asset_id, "merged_into": target_id,
                "status": "deprecated"}

    # ------------------------------------------------------------ 查询
    def get_scene(self, tenant_id: str, scene_asset_id: str) -> dict:
        scene = self._scene_row(tenant_id, scene_asset_id)
        return {
            "scene_asset_id": scene["scene_asset_id"],
            "project_id": scene["project_id"],
            "series_scope": scene["series_scope"],
            "display_name": scene["display_name"],
            "scene_kind": scene["scene_kind"],
            "era": _loads(scene["era"], {}),
            "location_class": scene["location_class"],
            "status": scene["status"],
            "canonical_revision": scene["canonical_revision"],
            "current_revision": scene["current_revision"],
            "topology_hash": scene["topology_hash"],
            "merged_into": scene["merged_into"],
            "created_at": scene["created_at"],
            # E30-D1：S13 永远不承诺权利有效。撤销后 revision 仍然可读，
            # 但调用方每次都要自己复查。
            "rights_recheck_required": True,
        }

    def get_revision(self, tenant_id: str, scene_asset_id: str,
                     revision: int) -> dict:
        scene = self._scene_row(tenant_id, scene_asset_id)
        row = self._revision_row(tenant_id, scene_asset_id, revision)
        return {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": row["revision"],
                          "content_hash": row["content_hash"]},
            "topology_hash": row["topology_hash"],
            "content_hash": row["content_hash"],
            "payload": _loads(row["payload"], {}),
            "topology": _loads(row["topology"], {}),
            "anchor_manifest": _loads(row["anchor_manifest"], {}),
            "fixed_objects": _loads(row["fixed_objects"], []),
            "lighting_axes": _loads(row["lighting_axes"], []),
            "lighting_manifest": _loads(row["lighting_manifest"], {}),
            "era_snapshot": _loads(row["era_snapshot"], {}),
            "rights_ref": _loads(row["rights_ref"], {}),
            "source_kind": row["source_kind"],
            "parent_revision": row["parent_revision"],
            "topology_changed": bool(row["topology_changed"]),
            "impact_plan_ref": _loads(row["impact_plan_ref"], None)
            if row["impact_plan_ref"] else None,
            "quality_gate_ref": _loads(row["quality_gate_ref"],
                                       row["quality_gate_ref"]),
            "accepted_at": row["accepted_at"],
            "accepted_by": _loads(row["accepted_by"], row["accepted_by"]),
            "is_canonical": scene["canonical_revision"] == row["revision"],
            "canonical_revision": scene["canonical_revision"],
            # E30-D1：撤销后仍返回 revision，但永远要求复查。
            "rights_recheck_required": True,
        }

    def get_anchors(self, tenant_id: str, scene_asset_id: str, revision: int, *,
                    direction: str | None = None,
                    lighting: str | None = None) -> dict:
        """§4.3。两条硬约束：

        - 请求不存在的 lighting → 422 `LIGHTING_NOT_AVAILABLE`，**绝不退回 day**。
        - 请求不存在的 anchor → 404 `ANCHOR_NOT_FOUND`。
        """
        scene = self._scene_row(tenant_id, scene_asset_id)
        self._revision_row(tenant_id, scene_asset_id, revision)
        rows = self.conn.execute(
            "SELECT * FROM scene_anchor WHERE tenant_id=? AND scene_asset_id=?"
            " AND revision=? ORDER BY anchor_id",
            (tenant_id, scene_asset_id, revision)).fetchall()
        anchors = {row["anchor_id"]: row for row in rows}
        if direction is not None and direction not in anchors:
            raise ServiceError(404, "ANCHOR_NOT_FOUND",
                               f"revision {revision} has no anchor {direction!r}",
                               details={"anchor_id": direction,
                                        "available": sorted(anchors)})
        selected = [anchors[direction]] if direction else list(anchors.values())

        if lighting is not None:
            # 可用集合按 anchor 分别算：同一 revision 里不同 anchor 的基准
            # 光照可能不同（一个朝窗、一个朝内墙）。
            for row in selected:
                available = self._available_lighting(tenant_id, scene_asset_id,
                                                    revision, row["anchor_id"])
                if lighting not in available:
                    raise ServiceError(
                        422, "LIGHTING_NOT_AVAILABLE",
                        f"anchor {row['anchor_id']!r} has no {lighting!r} variant "
                        f"(available: {available}); S13 never falls back to "
                        f"'day' — a night shot would silently render in daylight",
                        details={"lighting": lighting,
                                 "available": available,
                                 "anchor_id": row["anchor_id"]})

        out = []
        for row in selected:
            artifacts = self._anchor_artifacts(tenant_id, scene_asset_id,
                                              revision, row["anchor_id"])
            if lighting is not None:
                artifacts = {lighting: artifacts[lighting]}
            out.append({
                "anchor_id": row["anchor_id"],
                "position_m": _loads(row["position_m"], {}),
                "yaw_deg": row["yaw_deg"],
                "pitch_deg": row["pitch_deg"],
                "hfov_deg": row["hfov_deg"],
                "unit_id": row["unit_id"],
                "looks_at": row["looks_at"],
                "lighting_kind": row["lighting_kind"],
                "artifacts": artifacts,
            })
        base = {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": revision,
                          "content_hash": self._revision_row(
                              tenant_id, scene_asset_id, revision)["content_hash"]},
            "is_canonical": scene["canonical_revision"] == revision,
            "canonical_revision": scene["canonical_revision"],
            "rights_recheck_required": True,
        }
        if direction is not None:
            return {**base, "anchor": out[0]}
        return {**base, "anchors": out}

    def get_topology(self, tenant_id: str, scene_asset_id: str,
                     revision: int) -> dict:
        scene = self._scene_row(tenant_id, scene_asset_id)
        row = self._revision_row(tenant_id, scene_asset_id, revision)
        objects = self.conn.execute(
            "SELECT * FROM scene_fixed_object WHERE tenant_id=?"
            " AND scene_asset_id=? AND revision=? ORDER BY object_id",
            (tenant_id, scene_asset_id, revision)).fetchall()
        anchors = self.conn.execute(
            "SELECT * FROM scene_anchor WHERE tenant_id=? AND scene_asset_id=?"
            " AND revision=? ORDER BY anchor_id",
            (tenant_id, scene_asset_id, revision)).fetchall()
        era = _loads(row["era_snapshot"], {})
        return {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": revision,
                          "content_hash": row["content_hash"]},
            "topology_hash": row["topology_hash"],
            "topology": _loads(row["topology"], {}),
            "fixed_objects": [
                {"object_id": obj["object_id"], "label": obj["label"],
                 "unit_id": obj["unit_id"],
                 "blocks_walkable": bool(obj["blocks_walkable"]),
                 "is_load_bearing": bool(obj["is_load_bearing"]),
                 "height_cm": obj["height_cm"],
                 "footprint": _loads(obj["footprint"], [])}
                for obj in objects],
            "anchor_cameras": {
                anchor["anchor_id"]: {
                    "position_m": _loads(anchor["position_m"], {}),
                    "yaw_deg": anchor["yaw_deg"],
                    "pitch_deg": anchor["pitch_deg"],
                    "hfov_deg": anchor["hfov_deg"],
                    "unit_id": anchor["unit_id"],
                }
                for anchor in anchors},
            # §4.3 的响应只列 label/year_band；库里多存的键不在这里发明。
            "era": {k: era[k] for k in ("label", "year_band") if k in era},
            "rights_recheck_required": True,
            "is_canonical": scene["canonical_revision"] == revision,
        }

    def get_fixed_objects(self, tenant_id: str, scene_asset_id: str,
                          revision: int, *, unit_id: str | None = None) -> dict:
        scene = self._scene_row(tenant_id, scene_asset_id)
        self._revision_row(tenant_id, scene_asset_id, revision)
        clause = ("SELECT * FROM scene_fixed_object WHERE tenant_id=?"
                  " AND scene_asset_id=? AND revision=?")
        params: list = [tenant_id, scene_asset_id, revision]
        if unit_id is not None:
            clause += " AND unit_id=?"
            params.append(unit_id)
        rows = self.conn.execute(clause + " ORDER BY object_id", params).fetchall()
        return {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": revision,
                          "content_hash": self._revision_row(
                              tenant_id, scene_asset_id, revision)["content_hash"]},
            "unit_id": unit_id,
            "items": [
                {"object_id": row["object_id"], "label": row["label"],
                 "unit_id": row["unit_id"],
                 "footprint": _loads(row["footprint"], []),
                 "height_cm": row["height_cm"],
                 "is_load_bearing": bool(row["is_load_bearing"]),
                 "blocks_walkable": bool(row["blocks_walkable"]),
                 "description": row["description"]}
                for row in rows],
            "is_canonical": scene["canonical_revision"] == revision,
            "canonical_revision": scene["canonical_revision"],
        }

    def get_canonical(self, tenant_id: str, scene_asset_id: str) -> dict:
        """E05-D1：没有 canonical 时 **404 `CANONICAL_NOT_SET`**，不是空对象。

        一个"成功但为空"的响应对 gate 是最坏的情况：它看起来像一次成功的
        查询，gate 可能据此放行。
        """
        scene = self._scene_row(tenant_id, scene_asset_id)
        pointer = self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=? AND scene_asset_id=?"
            " AND superseded_at IS NULL", (tenant_id, scene_asset_id)).fetchone()
        if pointer is None:
            raise ServiceError(404, "CANONICAL_NOT_SET",
                               f"scene {scene_asset_id} has no canonical revision")
        revision = self._revision_row(tenant_id, scene_asset_id,
                                      pointer["revision"])
        return {
            "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                          "revision": pointer["revision"],
                          "content_hash": revision["content_hash"]},
            "topology_hash": pointer["topology_hash"],
            "canonical_revision": pointer["revision"],
            "current_revision": scene["current_revision"],
            "decided_at": pointer["decided_at"],
            "decided_by": _loads(pointer["decided_by"], pointer["decided_by"]),
            "decision_kind": pointer["decision_kind"],
            "reason_text": pointer["reason_text"],
            "prior_canonical": pointer["prior_canonical"],
            "status": scene["status"],
            "rights_recheck_required": True,
        }

    def lookup(self, tenant_id: str, series_scope: str, alias: str) -> dict:
        """`GET /scene-assets:lookup?series_scope=&alias=`。

        `alias` 在方案里没有定义。本实现按 `display_name` 匹配，并要求命中
        **唯一**一行——唯一约束本来就保证一个 series 里没有重名（S13-3），
        所以这里只是把唯一约束翻译成一次查询。
        """
        series_scope = _require_str(series_scope, "series_scope")
        alias = _require_str(alias, "alias")
        row = self.conn.execute(
            "SELECT * FROM scene_asset WHERE tenant_id=? AND series_scope=?"
            " AND display_name=?", (tenant_id, series_scope, alias)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no scene named {alias!r} in series "
                               f"{series_scope!r}")
        return self.get_scene(tenant_id, row["scene_asset_id"])

    # ------------------------------------------------------------ 内部
    def _parse_anchors(self, manifest: dict) -> list[dict]:
        if len(manifest) > MAX_ANCHORS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"at most {MAX_ANCHORS} anchors per revision")
        anchors = []
        for anchor_id in sorted(manifest):
            if not isinstance(anchor_id, str) or not anchor_id:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "anchor ids must be non-empty strings")
            anchors.append(_anchor_entry(anchor_id, manifest[anchor_id]))
        return anchors

    def _parse_lighting(self, anchors: list[dict], axes: list,
                        manifest: dict) -> list[dict]:
        """展开光照矩阵，缺一格就 422（F4）。

        基准光照（`anchor.lighting_kind`）允许只在 anchor 的 `artifact_ref`
        里出现——DDL 说 `scene_lighting_variant` 存的是「其他光照下的渲染」。
        其他轴必须在 `lighting_manifest[anchor_id][lighting]` 里。
        """
        cleaned: list[str] = []
        for axis in axes:
            if not isinstance(axis, str) or not axis:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "lighting_axes entries must be non-empty strings")
            if axis in cleaned:
                # 重复轴在结构上就是一个集合（§2.4 的主键），不是两行。
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"lighting_axes lists {axis!r} twice")
            cleaned.append(axis)
        if not cleaned:
            # 至少要有基准光照。零个轴意味着"这个场景没有可切换的光照"，
            # 但 DDL 的 `lighting_axes jsonb NOT NULL` 与 §4.3 的
            # `artifacts{day,night}` 都假设至少有一个。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "lighting_axes must not be empty")

        variants = []
        for anchor in anchors:
            per_anchor = manifest.get(anchor["anchor_id"], {})
            if not isinstance(per_anchor, dict):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"lighting_manifest[{anchor['anchor_id']!r}] "
                                   f"must be an object")
            for axis in cleaned:
                if axis == anchor["lighting_kind"]:
                    ref = anchor["artifact_ref"]
                else:
                    raw = per_anchor.get(axis)
                    if raw is None:
                        raise ServiceError(
                            422, "LIGHTING_COVERAGE_INCOMPLETE",
                            f"anchor {anchor['anchor_id']!r} has no "
                            f"{axis!r} variant; every lighting axis must be "
                            f"covered for every anchor",
                            details={"anchor_id": anchor["anchor_id"],
                                     "lighting": axis,
                                     "available": [anchor["lighting_kind"]]
                                     + sorted(k for k in per_anchor
                                              if k != anchor["lighting_kind"])})
                    ref = _artifact_ref(
                        raw, f"lighting_manifest.{anchor['anchor_id']}.{axis}")
                variants.append({"anchor_id": anchor["anchor_id"],
                                 "lighting": axis, "artifact_ref": ref})
        return variants

    def _parse_fixed_objects(self, objects: list) -> list[dict]:
        if len(objects) > MAX_FIXED_OBJECTS:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"at most {MAX_FIXED_OBJECTS} fixed objects")
        parsed = []
        seen = set()
        for raw in objects:
            obj = _require_object(raw, "fixed_objects[]")
            object_id = _require_str(obj.get("object_id"), "fixed_objects[].object_id")
            if object_id in seen:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"fixed object {object_id!r} appears twice")
            seen.add(object_id)
            if obj.get("footprint") is None:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"fixed object {object_id!r} has no footprint")
            height = obj.get("height_cm")
            if height is not None and (isinstance(height, bool)
                                       or not isinstance(height, int)):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"fixed object {object_id!r} height_cm must be "
                                   f"an integer")
            parsed.append({
                "object_id": object_id,
                "label": _require_str(obj.get("label"),
                                      f"fixed_objects[{object_id}].label"),
                "unit_id": _require_str(obj.get("unit_id"),
                                        f"fixed_objects[{object_id}].unit_id"),
                "footprint": obj["footprint"],
                "height_cm": height,
                "is_load_bearing": bool(obj.get("is_load_bearing", False)),
                "blocks_walkable": bool(obj.get("blocks_walkable", True)),
                "description": obj.get("description"),
            })
        return parsed

    def _verify_artifacts(self, tenant_id: str, anchors: list[dict],
                          variants: list[dict], *, parent_revision) -> dict:
        """§4.1 第 5 条。F13 / F16 的分界在这里。

        - `artifact_source` 未注入 / 抛 `ServiceError(503)` / `ConnectionError`
          → 503 `DEPENDENCY_UNAVAILABLE`，**候选不落库**（F16）。
        - S28 说 404 → 422 `ARTIFACT_NOT_FOUND`（F13）。
        - 声明的 `content_hash` 与 S28 的不符 → 422 `ARTIFACT_HASH_MISMATCH`。
        - 引用里没有 `content_hash` → 记 `hash_verified=false`，不假装一致。
        """
        if self.artifact_source is None:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "no S28 artifact source configured; S13 will not accept a "
                "revision whose artifact references it cannot verify (F16)",
                retryable=True)

        refs: list[dict] = []
        for anchor in anchors:
            refs.append(anchor["artifact_ref"])
        for variant in variants:
            refs.append(variant["artifact_ref"])

        # 同一个 artifact 在一次候选里会出现多次：anchor 自己的 `artifact_ref`
        # 同时也是它的基准光照变体（§2.4）。按 id 去重——否则一次起草会对
        # S28 发 12 次 HEAD 而不是 8 次，且每次超时都会放大成一次 503。
        first_ref: dict[str, dict] = {}
        declared_by_id: dict[str, set] = {}
        for ref in refs:
            artifact_id = ref.get("id")
            if artifact_id not in first_ref:
                first_ref[artifact_id] = ref
                declared_by_id[artifact_id] = set()
            if ref.get("content_hash"):
                declared_by_id[artifact_id].add(ref["content_hash"])

        checked = 0
        hashed = 0
        for artifact_id, ref in first_ref.items():
            try:
                head = self.artifact_source(tenant_id, ref)
            except ConnectionError as exc:
                raise ServiceError(
                    503, "DEPENDENCY_UNAVAILABLE",
                    f"S28 is unreachable while verifying artifact "
                    f"{ref.get('id')!r}; the candidate is not persisted (F16)",
                    retryable=True) from exc
            except ServiceError as exc:
                if exc.status >= 500:
                    raise ServiceError(
                        503, "DEPENDENCY_UNAVAILABLE",
                        f"S28 failed while verifying artifact {ref.get('id')!r}: "
                        f"{exc}; the candidate is not persisted (F16)",
                        retryable=True) from exc
                if exc.code == "ARTIFACT_NOT_FOUND" or exc.status == 404:
                    raise ServiceError(
                        422, "ARTIFACT_NOT_FOUND",
                        f"artifact {ref.get('id')!r} does not exist in S28",
                        details={"artifact_id": ref.get("id")}) from exc
                raise
            checked += 1
            declarations = declared_by_id[artifact_id]
            if len(declarations) > 1:
                # 同一个 artifact 在同一次候选里被两个引用声明成两个 hash：
                # 这不是「与 S28 不符」，是请求自身矛盾，不该拿网络去裁决。
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"artifact {artifact_id!r} is declared with "
                    f"{sorted(declarations)} in the same candidate",
                    details={"artifact_id": artifact_id})
            declared = next(iter(declarations), None)
            actual = (head or {}).get("content_hash")
            if declared and actual and declared != actual:
                raise ServiceError(
                    422, "ARTIFACT_HASH_MISMATCH",
                    f"artifact {artifact_id!r} declares {declared} but S28 has "
                    f"{actual}",
                    details={"artifact_id": artifact_id,
                             "declared": declared, "actual": actual})
            if declared and actual:
                hashed += 1

        return {"artifacts_checked": checked,
                "artifacts_hash_verified": hashed,
                "artifacts_without_hash": checked - hashed,
                "verified_at": _now(),
                "verified_at_revision": parent_revision}

    def _expand_derivations(self, tenant_id: str, scene_asset_id: str,
                            revision: int, payload: dict) -> None:
        """§4.2 第 6 步。从 payload 展开，不做任何再校验——payload 在起草时
        已经过了七步；在这里重判只会让「起草通过、接受被拒」多出一条路径。
        """
        manifest = payload.get("anchor_manifest") or {}
        anchors = []
        for anchor_id in sorted(manifest):
            anchor = _anchor_entry(anchor_id, manifest[anchor_id])
            anchors.append(anchor)
            self.conn.execute(
                "INSERT INTO scene_anchor (tenant_id, scene_asset_id, revision,"
                " anchor_id, position_m, yaw_deg, pitch_deg, hfov_deg, unit_id,"
                " looks_at, artifact_ref, lighting_kind)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, scene_asset_id, revision, anchor_id,
                 _jcs(anchor["position_m"]), anchor["yaw_deg"],
                 anchor["pitch_deg"], anchor["hfov_deg"], anchor["unit_id"],
                 _jcs(anchor["looks_at"]) if anchor["looks_at"] is not None
                 else None,
                 _jcs(anchor["artifact_ref"]), anchor["lighting_kind"]))

        for anchor in anchors:
            per_anchor = (payload.get("lighting_manifest") or {}).get(
                anchor["anchor_id"], {})
            for lighting in sorted(payload.get("lighting_axes") or []):
                if lighting == anchor["lighting_kind"]:
                    ref = anchor["artifact_ref"]
                else:
                    ref = _artifact_ref(
                        per_anchor.get(lighting),
                        f"lighting_manifest.{anchor['anchor_id']}.{lighting}")
                self.conn.execute(
                    "INSERT INTO scene_lighting_variant (tenant_id,"
                    " scene_asset_id, revision, anchor_id, lighting, artifact_ref)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (tenant_id, scene_asset_id, revision, anchor["anchor_id"],
                     lighting, _jcs(ref)))

        for obj in payload.get("fixed_objects") or []:
            self.conn.execute(
                "INSERT INTO scene_fixed_object (tenant_id, scene_asset_id,"
                " revision, object_id, label, unit_id, footprint, height_cm,"
                " is_load_bearing, blocks_walkable, description)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, scene_asset_id, revision, obj["object_id"],
                 obj["label"], obj["unit_id"], _jcs(obj["footprint"]),
                 obj.get("height_cm"),
                 1 if obj.get("is_load_bearing") else 0,
                 1 if obj.get("blocks_walkable", True) else 0,
                 obj.get("description")))

        # anchor_relationship 是推导值（§2.7），不是连续性结论。
        for left in anchors:
            for right in anchors:
                if left["anchor_id"] == right["anchor_id"]:
                    continue
                self.conn.execute(
                    "INSERT INTO anchor_relationship (tenant_id, scene_asset_id,"
                    " revision, from_anchor, to_anchor, relative_yaw_deg,"
                    " shared_features) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, scene_asset_id, revision, left["anchor_id"],
                     right["anchor_id"],
                     geometry.relative_yaw(left["yaw_deg"], right["yaw_deg"]),
                     _jcs(geometry.shared_features(left, right))))

    def _available_lighting(self, tenant_id: str, scene_asset_id: str,
                            revision: int, anchor_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT lighting FROM scene_lighting_variant WHERE tenant_id=?"
            " AND scene_asset_id=? AND revision=? AND anchor_id=?"
            " ORDER BY lighting",
            (tenant_id, scene_asset_id, revision, anchor_id)).fetchall()
        return [row["lighting"] for row in rows]

    def _anchor_artifacts(self, tenant_id: str, scene_asset_id: str,
                          revision: int, anchor_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT lighting, artifact_ref FROM scene_lighting_variant"
            " WHERE tenant_id=? AND scene_asset_id=? AND revision=?"
            " AND anchor_id=? ORDER BY lighting",
            (tenant_id, scene_asset_id, revision, anchor_id)).fetchall()
        return {row["lighting"]: _loads(row["artifact_ref"], {}) for row in rows}

    def _emit(self, *, tenant_id, scene_asset_id, revision, payload,
              content_hash, topology_hash_value, topology_changed,
              impact_plan_ref, became_canonical, canonical_revision,
              location_class, era_snapshot, command_id, home_cell) -> None:
        """§5.3 的 11 个 data 键。**一个不多、一个不少**——契约是
        `additionalProperties: false`。"""
        era = {key: era_snapshot[key] for key in ERA_EVENT_KEYS
               if key in era_snapshot}
        anchor_ids = sorted((payload.get("anchor_manifest") or {}).keys())
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_SCENE_REVISION_ACCEPTED,
            subject=f"scene/{scene_asset_id}",
            tenant_id=tenant_id,
            home_cell=home_cell,
            aggregate_id=scene_asset_id, sequence=revision,
            correlation_id=command_id, causation_id=command_id,
            data={
                "schema_version": SCHEMA_VERSION,
                "scene_ref": {"service_id": SOURCE, "id": scene_asset_id,
                              "revision": revision, "content_hash": content_hash},
                "topology_hash": topology_hash_value,
                "topology_changed": bool(topology_changed),
                "impact_plan_ref": impact_plan_ref,
                "became_canonical": bool(became_canonical),
                "canonical_revision": canonical_revision,
                "location_class": location_class,
                "era": era,
                "lighting_axes": list(payload.get("lighting_axes") or []),
                "anchor_ids": anchor_ids,
                "rights_ref": payload.get("rights_ref") or {},
            },
            # 契约要求 `data_ref: null`（载体是内联 data，不是外置引用）。
            data_ref=None,
            schema_version=SCHEMA_VERSION))
