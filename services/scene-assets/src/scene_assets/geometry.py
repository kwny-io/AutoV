"""S13 的几何内核：纯函数，无数据库、无网络。

方案 §11.1 第 2 条把这里单列为"最容易出 bug 的地方"，要求"必须有大量单元测试"。
所以本模块**不碰 sqlite，也不碰 ServiceError 以外的任何东西**——输入是方案
§2.3 的拓扑 JSON，输出是"哪里不合法"，服务层负责把它翻成 422。

三条设计取舍，它们决定本模块能证明什么：

1. **只做 2D。** 方案 §2.3 与 §10 C13-3 明确不做 3D/点云。所以这里的
   "点"只有 `(x, y)`，`z` 一律不理；`floor_z_band` 是标签，不是坐标。
2. **单位固定为米。** `scale_unit != 'meter'` 直接判非法，不做换算。
   换算是调用方的事，S13 存下来的数必须只有一种含义。
3. **只判结构合法性，不判"这间屋子好不好"。** 视野够不够、家具摆得对不对
   是 S33/S34 的 QC（C13-2）。这里只回答"门挂在一个不存在的 unit 上"这类
   机器能判的问题。

每个 `check_*` 返回 `(problems, facts)`：`problems` 是**有序**的列表（先拓扑，
后 anchor），每项 `{code, target, reason}`；`facts` 是可落进
`spatial_check` 的摘要，用于 audit 与 `spatial_check.topology_valid`。
"""

from __future__ import annotations

import math

# 坐标比较的容差（米）。手画平面图的顶点常见 1e-6 级误差；方案 §4.1 第 3 条
# 说 anchor "在 polygon 内（或边界容差内）"，这个常量就是那个容差。
EPS = 1e-6

# 判定"点在线段上"的距离容差（米）。与 EPS 同量级，但语义不同：
# 一个是坐标相等，一个是到边的距离。分开写，避免以后调一个连带另一个。
ON_SEGMENT_TOL = 1e-6

# 墙体厚度不参与拓扑（§2.3 只有 polygon），所以"落在边界上"就是字面意思：
# 点到某条边的距离 <= ON_SEGMENT_TOL。

UNIT_KINDS = frozenset({"room", "corridor", "hall", "balcony", "shaft",
                        "outdoor", "stairwell"})

OPENING_KINDS = frozenset({"door", "window", "arch", "gate", "stair"})

# §4.1 第 2 条：按 location_class 的最低 anchor 数。`background` 无下限
# （§2.1："1 张参考即可，可无 canonical"）。
MIN_ANCHORS = {
    "primary_set": 4,
    "recurring_location": 2,
    "one_off": 1,
    "background": 0,
}

# §4.1 第 2 条对 `primary_set` 追加"覆盖 360°"。方案只说"覆盖 360°"，
# 没给数值口径；取 30° 是"四个正交方向 + 合理余量"的最小可用门槛，
# 记为 C13 缺口（schema.CONTRACT_GAPS 的 S13-9），不假装是原文。
PRIMARY_SET_MIN_YAW_SPREAD_DEG = 30.0


def _point(value) -> tuple[float, float] | None:
    """`[x, y]` / `{"x":…, "y":…}` → `(x, y)`。其余一律 None。

    接受两种形态是因为方案 §2.3 的示例用数组，而 anchor 的 `position_m`
    用对象（`{x,y,z}`）。拓扑只认数组；这里是共享的解析助手。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = value[0], value[1]
    elif isinstance(value, dict) and "x" in value and "y" in value:
        x, y = value["x"], value["y"]
    else:
        return None
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    if not math.isfinite(float(x)) or not math.isfinite(float(y)):
        return None
    return float(x), float(y)


def polygon(points) -> list[tuple[float, float]] | None:
    """解析 polygon。返回去掉重复闭合点的顶点表，或 None。"""
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        return None
    out = []
    for item in points:
        parsed = _point(item)
        if parsed is None:
            return None
        out.append(parsed)
    return out


def is_closed(points: list[tuple[float, float]]) -> bool:
    """方案 §4.1 第 1 条："unit polygon 闭合"。

    两种写法都接受：首尾点显式相同（`[[0,0],[6,0],[6,4],[0,4],[0,0]]`），
    或首尾不同、由判定者隐式闭合（`[[0,0],[6,0],[6,4],[0,4]]`）。
    方案 §2.3 的示例是后者——所以**不能要求显式闭合**，只能要求
    "把首尾连起来以后没有退化"。显式闭合但中间有重复点仍算非法。
    """
    n = len(points)
    if n < 3:
        return False
    dedup = [points[0]]
    for p in points[1:]:
        if _close(p, dedup[-1]):
            continue
        dedup.append(p)
    if len(dedup) >= 2 and _close(dedup[0], dedup[-1]):
        dedup.pop()
    return len(dedup) >= 3


def _close(a, b) -> bool:
    return abs(a[0] - b[0]) <= EPS and abs(a[1] - b[1]) <= EPS


def area(points: list[tuple[float, float]]) -> float:
    """鞋带公式，取绝对值。退化 polygon 面积为 0。"""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(p, a, b) -> bool:
    if abs(_orient(a, b, p)) > ON_SEGMENT_TOL:
        return False
    return (min(a[0], b[0]) - ON_SEGMENT_TOL <= p[0] <= max(a[0], b[0]) + ON_SEGMENT_TOL
            and min(a[1], b[1]) - ON_SEGMENT_TOL <= p[1] <= max(a[1], b[1]) + ON_SEGMENT_TOL)


def point_in_polygon(p, points, *, boundary_ok: bool = True) -> bool:
    """射线法。`boundary_ok=True` 时边界上的点算"在内"。

    §4.1 第 3 条允许"边界容差内"，所以判定 anchor 位置用 boundary_ok=True；
    判定"footprint 在 unit 内"也用同一个（贴墙的桌子是合法的）。
    """
    n = len(points)
    if boundary_ok:
        for i in range(n):
            if _on_segment(p, points[i], points[(i + 1) % n]):
                return True
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > p[1]) != (yj > p[1]):
            x_cross = (xj - xi) * (p[1] - yi) / (yj - yi) + xi
            if p[0] < x_cross:
                inside = not inside
        j = i
    return inside


def segment_covered_by_boundary(seg, points) -> bool:
    """线段是否落在 polygon 边界上。

    方案 §4.1 第 1 条要求 opening 的 `wall_segment` "落在 unit 边界上"。
    实现方式：两端点都在边界上、且中点也在边界上。三者同时成立即认为
    整段贴着边界——对凸/凹 polygon 的直线边都够用，且不会把"弦"误判为边
    （弦的中点一定不在边界上，除非它恰好贴着另一条边）。

    已知局限：一条穿过顶点、两端落在两条不同边上的线段，中点可能恰好
    落在第三条边上，从而误判。方案没有给更细的口径，记在 CONTRACT_GAPS
    里，不用一个未定义的"墙厚"去补。
    """
    a, b = seg
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    return (_on_polygon_boundary(a, points) and _on_polygon_boundary(b, points)
            and _on_polygon_boundary(mid, points))


def _on_polygon_boundary(p, points) -> bool:
    n = len(points)
    for i in range(n):
        if _on_segment(p, points[i], points[(i + 1) % n]):
            return True
    return False


def polygons_overlap(a, b) -> bool:
    """两个 polygon 是否有面积重叠。

    方案 §4.1 第 1 条："不重叠（除相邻共享边）"。所以**只共享边不算重叠**，
    这是本函数与一般相交测试的差别：贴在一起的两个房间（共享一面墙）必须
    通过。实现：先排除"仅接触"，再看是否有边相交或有顶点严格落在对方内部。
    """
    if not _bbox_overlaps(a, b):
        return False
    # 顶点严格在对方内部 → 真重叠（共享边上的点被 point_in_polygon 的
    # boundary_ok=False 排除）。
    for p in a:
        if point_in_polygon(p, b, boundary_ok=False):
            return True
    for p in b:
        if point_in_polygon(p, a, boundary_ok=False):
            return True
    # 边相交且不是共线重叠。共线共享边在这里被判为"接触"，不算重叠。
    for i in range(len(a)):
        a1, a2 = a[i], a[(i + 1) % len(a)]
        for j in range(len(b)):
            b1, b2 = b[j], b[(j + 1) % len(b)]
            if _segments_cross_strictly(a1, a2, b1, b2):
                return True
    return False


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_overlaps(a, b) -> bool:
    ax1, ay1, ax2, ay2 = _bbox(a)
    bx1, by1, bx2, by2 = _bbox(b)
    return not (ax2 <= bx1 + EPS or bx2 <= ax1 + EPS
                or ay2 <= by1 + EPS or by2 <= ay1 + EPS)


def _segments_cross_strictly(a1, a2, b1, b2) -> bool:
    d1 = _orient(b1, b2, a1)
    d2 = _orient(b1, b2, a2)
    d3 = _orient(a1, a2, b1)
    d4 = _orient(a1, a2, b2)
    if abs(d1) <= ON_SEGMENT_TOL or abs(d2) <= ON_SEGMENT_TOL:
        return False
    if abs(d3) <= ON_SEGMENT_TOL or abs(d4) <= ON_SEGMENT_TOL:
        return False
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


# ---------------------------------------------------------------- 拓扑校验

def validate_topology(topology, *, require_openings_attached: bool = True):
    """方案 §4.1 第 1 条的完整校验。返回 `(problems, facts)`。

    检查顺序与方案一致：unit → opening → orientation → walkable → 重叠。
    第一个问题不会中断后续检查——调用方要能一次看到全部问题，而不是
    改一处跑一次（§11.1 第 2 条要求"错误定位到具体 opening/unit"）。

    问题项的形状 `{"code": "TOPOLOGY_INVALID", "target": ..., "reason": ...}`
    与 F1 的 `422 TOPOLOGY_INVALID {opening_id, reason}` 对齐；`target` 是
    出问题的 id（unit_id / opening_id / structure_id），没有具体对象时是
    字段名。
    """
    problems: list[dict] = []
    facts: dict = {
        "topology_valid": False,
        "units": 0,
        "openings": 0,
        "openings_attached": False,
        "parsed_units": {},
    }

    def bad(target, reason):
        problems.append({"code": "TOPOLOGY_INVALID", "target": target,
                         "reason": reason})

    if not isinstance(topology, dict):
        bad("topology", "topology must be an object")
        return problems, facts

    if topology.get("scale_unit") != "meter":
        # §2.3："坐标单位固定为米"。不收别的单位，也不换算。
        bad("scale_unit", "scale_unit must be 'meter' (coordinates are meters, 2D)")

    units_raw = topology.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        bad("units", "topology.units must be a non-empty array")
        return problems, facts

    parsed_units: dict[str, list] = {}
    for unit in units_raw:
        if not isinstance(unit, dict):
            bad("units", "each unit must be an object")
            continue
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id:
            bad("units", "unit.unit_id is required")
            continue
        if unit_id in parsed_units:
            bad(unit_id, "duplicate unit_id")
            continue
        kind = unit.get("kind")
        if kind not in UNIT_KINDS:
            # §2.3 的示例只出现 room/corridor，但 unit 的取值集没写死；
            # 这里只拦明显不是空间的东西（例如把 wall 当 unit）。
            bad(unit_id, f"unit.kind {kind!r} is not a space kind")
        pts = polygon(unit.get("polygon"))
        if pts is None:
            bad(unit_id, "unit.polygon must be an array of >= 3 [x, y] points")
            continue
        if not is_closed(pts):
            bad(unit_id, "unit.polygon is not closed (degenerate after dedupe)")
            continue
        if area(pts) <= EPS:
            # 零面积 polygon 的"内部"是空的：anchor 永远判不在里面，
            # walkable 也覆盖不了任何东西。
            bad(unit_id, "unit.polygon has zero area")
            continue
        parsed_units[unit_id] = pts

    if not parsed_units:
        return problems, facts

    facts["units"] = len(parsed_units)
    facts["parsed_units"] = parsed_units

    # 重叠：只共享边合法，共享面积非法。
    unit_ids = list(parsed_units)
    for i in range(len(unit_ids)):
        for j in range(i + 1, len(unit_ids)):
            left, right = unit_ids[i], unit_ids[j]
            if polygons_overlap(parsed_units[left], parsed_units[right]):
                bad(left, f"unit overlaps unit {right} (shared edges are allowed, "
                          f"shared area is not)")

    # ---- opening
    openings_raw = topology.get("openings", [])
    if not isinstance(openings_raw, list):
        bad("openings", "topology.openings must be an array")
        openings_raw = []
    seen_openings = set()
    attached = 0
    for opening in openings_raw:
        if not isinstance(opening, dict):
            bad("openings", "each opening must be an object")
            continue
        opening_id = opening.get("opening_id")
        if not isinstance(opening_id, str) or not opening_id:
            bad("openings", "opening.opening_id is required")
            continue
        if opening_id in seen_openings:
            bad(opening_id, "duplicate opening_id")
            continue
        seen_openings.add(opening_id)

        kind = opening.get("kind")
        if kind not in OPENING_KINDS:
            bad(opening_id, f"opening.kind {kind!r} is not an opening kind")

        unit_a = opening.get("unit_a")
        unit_b = opening.get("unit_b")  # 可以为 None：开向外部
        if unit_a not in parsed_units:
            bad(opening_id, f"unit_a {unit_a!r} does not exist")
            continue
        if unit_b is not None and unit_b not in parsed_units:
            bad(opening_id, f"unit_b {unit_b!r} does not exist")
            continue
        if unit_b == unit_a:
            bad(opening_id, "unit_a and unit_b are the same unit")

        seg_raw = opening.get("wall_segment")
        seg_ok = False
        if not isinstance(seg_raw, (list, tuple)) or len(seg_raw) != 2:
            bad(opening_id, "wall_segment must be a pair of points")
        else:
            p1, p2 = _point(seg_raw[0]), _point(seg_raw[1])
            if p1 is None or p2 is None:
                bad(opening_id, "wall_segment points must be finite [x, y]")
            elif _close(p1, p2):
                bad(opening_id, "wall_segment has zero length")
            else:
                seg_ok = True
                if require_openings_attached:
                    if not segment_covered_by_boundary((p1, p2), parsed_units[unit_a]):
                        bad(opening_id, "wall_segment is not on unit_a's boundary")
                    elif unit_b is not None and not segment_covered_by_boundary(
                            (p1, p2), parsed_units[unit_b]):
                        # 门同时属于两个房间时必须同时贴两边的墙——这正是
                        # "同一门在两个 anchor 的墙段不同"能被机器抓住的原因（E24-D3）。
                        bad(opening_id, "wall_segment is not on unit_b's boundary")
        if seg_ok and (unit_a in parsed_units) and (
                unit_b is None or unit_b in parsed_units):
            attached += 1

    facts["openings"] = len(seen_openings)
    facts["openings_attached"] = bool(seen_openings) and attached == len(seen_openings)

    # ---- orientation
    orientation = topology.get("orientation")
    if not isinstance(orientation, dict):
        bad("orientation", "orientation.north_axis_deg is required")
    else:
        north = orientation.get("north_axis_deg")
        if isinstance(north, bool) or not isinstance(north, (int, float)):
            bad("orientation", "orientation.north_axis_deg must be a number")
        elif not math.isfinite(float(north)):
            bad("orientation", "orientation.north_axis_deg must be finite")
        elif not (0.0 <= float(north) < 360.0):
            # 0 与 360 是同一个方向；收 [0, 360) 让"归一化"只有一个答案。
            bad("orientation", "orientation.north_axis_deg must be in [0, 360)")
        ref_unit = orientation.get("unit_north_ref")
        if ref_unit is not None and ref_unit not in parsed_units:
            bad("orientation", f"unit_north_ref {ref_unit!r} does not exist")

    # ---- walkable
    walkable = topology.get("walkable")
    if not isinstance(walkable, dict):
        # §2.3："walkable 必须覆盖所有非墙 unit"。缺整块就是没覆盖。
        bad("walkable", "walkable must be an object covering every unit")
    else:
        missing = [u for u in parsed_units if u not in walkable]
        if missing:
            bad("walkable", f"walkable does not cover units: {sorted(missing)}")
        for unit_id in walkable:
            if unit_id not in parsed_units:
                # 指向不存在的 unit 说明两边已经不一致了；当错误而不是忽略。
                bad(unit_id, "walkable refers to a unit that does not exist")

    # ---- structures（列/楼梯等）
    structures = topology.get("structures", [])
    if not isinstance(structures, list):
        bad("structures", "topology.structures must be an array")
    else:
        seen_structures = set()
        for structure in structures:
            if not isinstance(structure, dict):
                bad("structures", "each structure must be an object")
                continue
            structure_id = structure.get("structure_id")
            if not isinstance(structure_id, str) or not structure_id:
                bad("structures", "structure.structure_id is required")
                continue
            if structure_id in seen_structures:
                bad(structure_id, "duplicate structure_id")
                continue
            seen_structures.add(structure_id)
            pts = polygon(structure.get("footprint"))
            if pts is None:
                bad(structure_id, "structure.footprint must be a >= 3 point polygon")
                continue
            if not _footprint_in_any_unit(pts, parsed_units):
                bad(structure_id, "structure.footprint is outside every unit")

    facts["topology_valid"] = not problems
    return problems, facts


def _footprint_in_any_unit(pts, parsed_units) -> bool:
    for candidate in parsed_units.values():
        if all(point_in_polygon(p, candidate) for p in pts):
            return True
    return False


# ---------------------------------------------------------------- anchor 校验

def anchor_coverage(count: int, location_class: str) -> dict:
    """§4.1 第 2 条。返回 `{"ok":bool,"required":n,"actual":count}`。"""
    required = MIN_ANCHORS.get(location_class, 0)
    return {"ok": count >= required, "required": required, "actual": count}


def yaw_spread(yaws) -> float:
    """一组水平朝向覆盖的角度跨度。

    两两之间的最小夹角取最大值：3 个相隔 120° 得 120°，4 个正交方向得 180°
    （0° 与 180° 这对本身相隔半圈），全部朝向同一方向得 0°。用圆形距离（不是
    线性差），否则 350° 与 10° 会被算成 340°。

    这个量回答的是「最开的两台相机差多少」，不是「覆盖了多少扇墙」。对
    `primary_set` 的「覆盖 360°」，方案没给门槛（S13-8），所以只用它区分
    「全朝一面墙」与「确实散开」；门槛取 30°。
    """
    values = [float(y) % 360.0 for y in yaws]
    if len(values) < 2:
        return 0.0
    best = 0.0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            diff = abs(values[i] - values[j])
            best = max(best, min(diff, 360.0 - diff))
    return best


def validate_anchor_position(anchor: dict, parsed_units: dict):
    """§4.1 第 3 条：anchor 的 `position_m` 必须在其 `unit_id` 内。

    返回 `(problems, facts)`。`unit_id` 不存在、或者在别的 unit 里，都算
    `ANCHOR_OUTSIDE_UNIT`——方案 F3 只给这一个码，不拆。
    """
    problems: list[dict] = []
    anchor_id = anchor.get("anchor_id")
    unit_id = anchor.get("unit_id")
    if unit_id not in parsed_units:
        problems.append({"code": "ANCHOR_OUTSIDE_UNIT", "anchor_id": anchor_id,
                         "reason": f"unit_id {unit_id!r} does not exist"})
        return problems, {}
    pos = _point(anchor.get("position_m"))
    if pos is None:
        problems.append({"code": "ANCHOR_OUTSIDE_UNIT", "anchor_id": anchor_id,
                         "reason": "position_m must be finite"})
        return problems, {}
    if not point_in_polygon(pos, parsed_units[unit_id]):
        problems.append({"code": "ANCHOR_OUTSIDE_UNIT", "anchor_id": anchor_id,
                         "reason": f"position_m is outside unit {unit_id}"})
    return problems, {"position": pos}


def validate_fixed_object(obj: dict, parsed_units: dict) -> list[dict]:
    """§4.1 第 6 条：固定物 `footprint` 必须落在其 `unit_id` 内。"""
    object_id = obj.get("object_id")
    unit_id = obj.get("unit_id")
    if unit_id not in parsed_units:
        return [{"code": "FIXED_OBJECT_OUT_OF_BOUNDS", "object_id": object_id,
                 "reason": f"unit_id {unit_id!r} does not exist"}]
    pts = polygon(obj.get("footprint"))
    if pts is None:
        return [{"code": "FIXED_OBJECT_OUT_OF_BOUNDS", "object_id": object_id,
                 "reason": "footprint must be a >= 3 point polygon"}]
    inside = all(point_in_polygon(p, parsed_units[unit_id]) for p in pts)
    if not inside:
        return [{"code": "FIXED_OBJECT_OUT_OF_BOUNDS", "object_id": object_id,
                 "reason": f"footprint is outside unit {unit_id}"}]
    return []


# ---------------------------------------------------------------- 派生关系

def relative_yaw(from_yaw: float, to_yaw: float) -> float:
    """`to` 相对 `from` 的偏航，归一到 (-180, 180]。

    方案 §2.7 的 `anchor_relationship.relative_yaw_deg` 是这一条。归一化
    让"从 front 看 left"与"从 left 看 front"分别是 -90 / +90，而不是
    270 / 90——后者会让 S17 的下游每次都要自己再取一次模。
    """
    diff = (float(to_yaw) - float(from_yaw)) % 360.0
    if diff > 180.0:
        diff -= 360.0
    return diff


def shared_features(from_anchor: dict, to_anchor: dict) -> list[str]:
    """两个 anchor 的 `looks_at` 交集，加上它们共同所属 unit。

    方案只说 `shared_features` 是"两个 anchor 都应看到的物体/开口"，没给
    算法。这里取**保守**的交集：只算两边都显式声明的 `looks_at`，再加上
    同一 unit。不按 hfov 做几何可见性推理——那需要真实美术资产的位置，
    本服务没有（§11.2 第 1 条）。
    """
    left = set(from_anchor.get("looks_at") or [])
    right = set(to_anchor.get("looks_at") or [])
    shared = sorted(left & right)
    if from_anchor.get("unit_id") and from_anchor.get("unit_id") == to_anchor.get("unit_id"):
        shared.append(f"unit:{from_anchor['unit_id']}")
    return shared
