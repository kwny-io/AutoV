"""S38 的内核：把 S37 的字级时间码 + 对白文本变成一条可交付的字幕轨。

两个必须在架构上分开的动作：

1. **生成**——切分、时码、排版、渲染、归档到 S28。
2. **校验**——`SUB.*` 检查写进一份 `qc_layer="subtitle"` 的 QCReport。

它们共享同一份 cue 行，但判定不参与渲染：渲染是纯函数（cue 行 + 格式 +
取整规则 → 字节），校验只读渲染结果与 cue 行。混在一起最后一定会出现
"为了过检查而改输出"，那正是方案 §4.3 禁止的静默缩字。

六个实现取舍：

1. **字节不进本库。** `.srt/.vtt/.ass` 在内存里渲染，立刻交 S28；库里只留
   `artifact_id + content_hash`。S28 不可达时轨道停在
   `rendered_pending_archive` 且不发 `post.subtitle.ready`（§3.0、E25-U5）。
2. **"重试不重渲染"只能靠"重渲染逐字节相同"兑现。** 字节不在本库，重试
   只能再算一遍。于是渲染写成纯函数，重试时与 `pending_render.content_hash`
   逐字节比对；不一致即 500 `RENDER_NOT_DETERMINISTIC`，绝不归档第二份
   （S38-C10）。
3. **三份文本三组列。** `cue` 上 `source_text_*` / `lexicon_*` /
   `translation_*` 各占一组，`display_text` 是派生的、不版本化。译文 stale
   时保留文本（可预览、不可正式交付），**从不自动重译**。
4. **`execution_fence` 与 `node_epoch` 各存各的。** track 存 fence、cue 存
   epoch；两列不互相赋值、不比较。
5. **本服务不推进任何接受指针。** track 的 `accepted`/`qc_passed` 状态值
   存在是因为 §9 的状态机画了它们，但内核没有任何代码路径写它们（S38-C15）。
   本服务也没有余额、没有第二份字节。
6. **判定顺序不能换。** 缺项 → 覆盖率 → 未得出结论 → blocker → major。
   先看严重度会把"有一项没查"读成"查过的都没问题"。

幂等由 `tenant_id + command_id + request_hash` 承担：同键同 body 返回首次
结果，同键不同 body 抛 `CommandConflict`（409），不是覆盖。

## 输入形状（全部由 `deps.py` 注入，测试里是闭包 stub）

```python
alignment(tenant, ref) -> {"report": {...qc report payload...},
                           "units": {"units": [{index, char_offset,
                                                char_length, start, end,
                                                ci_low, ci_high, ...}]}}
dialogue(tenant, ref)  -> {"script_id", "revision",
                           "lines": [{"line_id", "revision", "text", ...}]}
timeline(tenant, ref)  -> {"tick_rate", "duration_ticks", "clips": [
                           {"clip_id", "in_ticks", "out_ticks",
                            "speed_ratio"}], "completeness", ...}
lexicon(tenant, ref)   -> {"revision", "lexicon": {...}} | None
archive(tenant, req)   -> {"artifact_id", "content_hash", "byte_size"}
```

S37 的 `char_offset` 是**该条对白行内部**的字符偏移（S37 `_derive_script_tokens`
按每条 utterance 自己的 `text` 从 0 数起），所以单位 → 行的映射按
"逐行消费、偏移小于本行长度"进行；这一假设登记为 `S38-C18`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from . import schema

# ---------------------------------------------------------------- 常量

SOURCE = schema.SOURCE
SCHEMA_VERSION = schema.SCHEMA_VERSION
QC_LAYER = schema.QC_LAYER

EVALUATOR_NAME = "s38-subtitles"
EVALUATOR_VERSION = "2.0.0"
#: 镜像摘要由部署填入；未填时用固定串而不是编一个 sha256。
EVALUATOR_IMAGE_DIGEST = "sha256:unresolved"

#: `execution-context.v2` 的 18 个 required 字段。缺一个就不落任何一行：
#: 报告、事件、审计全都挂在这些坐标上。
REQUIRED_CONTEXT = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref",
    "actor", "command_id", "request_hash", "trace_id",
)
#: 允许缺值（但键必须在）的上下文字段。
NULLABLE_CONTEXT = ("memory_checkpoint",)

#: 报告 context 另需的两个坐标（`qc-report.v2` 的 required）。
REQUIRED_TOP_LEVEL = ("run_id", "shot_lineage_id")

#: 默认 locale profile（方案 §4 的数字）。命令只能收紧，上限由 schema.py 封顶。
DEFAULT_PROFILE = {
    "max_lines": schema.MAX_LINES,
    "required_coverage_min": 0.95,
    "safe_area": {"bottom_pct": 0.10, "side_pct": 0.05},
    "line_height_pct": 0.05,
    "style_template_ref": {"id": "tpl_sub_default", "revision": 1},
    "font": "Noto Sans CJK SC",
}

#: 片段切分的最小长度：太短的标点片段会和相邻片段合并。
MIN_SEGMENT_CHARS = 6

#: 判定阈值（与 S35/S37 同一口径）。
TAU_LOW = 0.40

#: 字幕文件的三条媒体类型。S28 用它决定存储与分发行为。
MEDIA_TYPES = {
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "ass": "text/x-ssa",
}


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return schema.ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _pos(value, default=0.0) -> float:
    return float(value) if _finite(value) and value >= 0 else default


def _b32(key_hex: str, length: int) -> str:
    raw = bytes.fromhex(key_hex)
    encoded = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return encoded[:length]


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


def stable_request_hash(body: dict) -> str:
    """命令幂等摘要：业务字段全进，trace 与时间戳不进。

    重投时 `trace_id` 变了，裸 `canonical_request_hash` 会算出不同摘要，把
    一次合法重投判成"同键不同 body"。
    """
    from idempotency_sdk import canonical_request_hash

    if not isinstance(body, dict):
        raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")
    candidate = dict(body)
    context = candidate.get("execution_context")
    if isinstance(context, dict):
        candidate["execution_context"] = {
            k: v for k, v in context.items()
            if k not in schema.VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(candidate)


def _hex64(value) -> str | None:
    """接受裸 64 位十六进制或 `sha256:` 前缀的摘要。"""
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


# ---------------------------------------------------------------- 派生 ID
#
# 全部由内容派生，不用随机数：同一份输入重跑得到同一组 ID，审计才能把两次
# 运行对起来（E26 的跨 Run 追踪）。

def track_id_for(*, tenant_id: str, timeline_id: str, target_locale: str,
                 source_locale: str, dialogue_id: str) -> str:
    """轨道 ID 跨 subtitle_revision 稳定（E26-U1：同 lineage，新 revision）。"""
    return "trk_" + _b32(_sha256_hex(_jcs({
        "tenant_id": tenant_id, "timeline_id": timeline_id,
        "target_locale": target_locale, "source_locale": source_locale,
        "dialogue_id": dialogue_id,
    })), 26)


def cue_id_for(track_id: str, line_id: str, segment_index: int) -> str:
    """cue 的稳定 ID：跨 revision 不变（§2.2 的注释）。"""
    return "cue_" + _b32(_sha256_hex(_jcs({
        "track_id": track_id, "line_id": line_id,
        "segment_index": segment_index,
    })), 26)


def report_id_for(*, track_id: str, subtitle_revision: int,
                  attempt_key: str) -> str:
    """`qc-report.v2` 要求 `^qc_[a-z0-9]{26}$`。"""
    return "qc_" + _b32(_sha256_hex(_jcs({
        "track_id": track_id, "subtitle_revision": subtitle_revision,
        "attempt_key": attempt_key,
    })), 26)


def defect_id_for(fingerprint: str, index: int) -> str:
    return "df_" + _b32(_sha256_hex(f"{fingerprint}:{index}"), 26)


def defect_fingerprint(*, check_id: str, kind: str, cue_id,
                       severity: str, start_seconds: float) -> str:
    """与 artifact hash 无关的稳定身份（E26-U2：跨 Run 同类复现计数 +1）。

    不含 `observed`（措辞随版本变）、不含精确 severity（blocker/major 抖动
    不该分裂指纹）。契约 pattern 是 `^[0-9a-f]{32}$`。
    """
    return _sha256_hex(_jcs({
        "qc_layer": QC_LAYER,
        "check_id": check_id,
        "type": kind,
        "cue_id": cue_id,
        "severity_bucket": "blocker" if severity == "blocker" else "non_blocker",
        "location_bucket": int(round(start_seconds / 0.5)),
    }))[:32]


# ---------------------------------------------------------------- 文本度量

#: 句末标点：优先在这些位置断句（§3 的「按语义/标点/停顿切分」）。
_SENTENCE_END = "。！？!?…；;"
#: 次一级断句点：句末点切不开时才用。
_CLAUSE_END = "，,、：:—"


def _tokens(text: str) -> list[str]:
    """切 token：汉字逐字，拉丁按词。

    `max_chars_per_line` 是**显示宽度**上限，所以按字符数度量；拉丁词整体
    不可分，超长单词因此会如实溢出（`SUB.LAYOUT.LINE_OVERFLOW`），而不是
    被硬拆成两半——硬拆会改变台词文本。
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if ord(char) < 128 and (char.isalnum() or char in "'-"):
            end = index
            while end < len(text) and ord(text[end]) < 128 \
                    and (text[end].isalnum() or text[end] in "'-"):
                end += 1
            out.append(text[index:end])
            index = end
        else:
            out.append(char)
            index += 1
    return out


def _join_token(left: str, token: str) -> str:
    """拼接时只在两边都是拉丁字母/数字**且已有词边界**时插空格。

    汉字之间不插空格；拉丁词之间插。判断用「唯一字符」而不是"整行是否
    含中文"，所以混合文本也能得到正确的空格。
    """
    if not left:
        return token
    left_char = left[-1]
    right_char = token[0]
    if ord(left_char) < 128 and left_char.isalnum() \
            and ord(right_char) < 128 and right_char.isalnum():
        return left + " " + token
    return left + token


def _wrap(text: str, limit: int) -> list[str]:
    """贪心断行。不截断、不省略——溢出由检查报告，不由排版掩盖。"""
    lines: list[str] = []
    current = ""
    for token in _tokens(text):
        candidate = _join_token(current, token)
        if not current or len(candidate) <= limit:
            current = candidate
        else:
            lines.append(current)
            current = token
    if current:
        lines.append(current)
    return lines or [""]


def _char_count(text: str) -> int:
    """显示宽度：不计空白。CPS 与行宽都用这一个口径。"""
    return sum(1 for char in text if not char.isspace())


# ---------------------------------------------------------------- 切分


def _split_points(text: str, *, prefer_sentence: bool = True) -> list[int]:
    marks = _SENTENCE_END if prefer_sentence else _SENTENCE_END + _CLAUSE_END
    return [index + 1 for index, char in enumerate(text) if char in marks]


def _segment_text(text: str) -> list[tuple[int, int]]:
    """把一行文本切成 `[(char_start, char_end)]` 段。

    §3 要求"按语义/标点/停顿切分"。停顿没有直接出现在输入里（S37 给的是
    置信区间，不是停顿标记），所以先用标点；切完仍然过短的段与相邻段合并，
    避免产出"嗯。"这种一句两字的 cue。
    """
    if not text:
        return []
    points = _split_points(text)
    if not points:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    start = 0
    for point in points:
        if point <= start:
            continue
        spans.append((start, point))
        start = point
    if start < len(text):
        spans.append((start, len(text)))
    # 合并过短段（只看文本宽度，不看 token 数）。
    merged: list[list[int]] = []
    for begin, end in spans:
        if merged and (_char_count(text[begin:end]) < MIN_SEGMENT_CHARS
                       or _char_count(text[merged[-1][0]:merged[-1][1]])
                       < MIN_SEGMENT_CHARS):
            merged[-1][1] = end
        else:
            merged.append([begin, end])
    return [(begin, end) for begin, end in merged]


def _split_span(text: str, begin: int, end: int) -> list[tuple[int, int]]:
    """二级切分：优先找句中逗号，其次按 token 数对半。**只用于重断句**（§4.3 第 1 步）。"""
    inner = text[begin:end]
    clause = [begin + point for point in _split_points(inner, prefer_sentence=False)
              if begin < begin + point < end]
    if len(clause) >= 2:
        spans, start = [], begin
        for point in clause:
            if point > start:
                spans.append((start, point))
                start = point
        if start < end:
            spans.append((start, end))
        if len(spans) >= 2:
            return spans
    tokens = _tokens(inner)
    if len(tokens) < 2:
        return [(begin, end)]
    half = len(inner) // 2
    # 找离中点最近的 token 边界（按字符位置累计）。
    position, best = begin, begin
    for token in tokens:
        position += len(token)
        if abs(position - (begin + half)) < abs(best - (begin + half)):
            best = position
    if best <= begin or best >= end:
        return [(begin, end)]
    return [(begin, best), (best, end)]


# ---------------------------------------------------------------- 时间码

def _format_time(seconds: float, style: str) -> tuple[float, str]:
    """取整并格式化。返回 (取整后的秒, 字符串)。

    ASS 只有厘秒精度，**取整会改变区间**，所以返回取整值让调用方复检重叠。
    """
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    if style == "srt":
        return (total_ms / 1000.0,
                f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}")
    if style == "vtt":
        return (total_ms / 1000.0,
                f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}")
    # ASS：厘秒。先取厘秒再拆时分秒，避免"先毫秒再截断"造成两位误差。
    total_cs = max(0, int(round(seconds * 100)))
    hours, rest = divmod(total_cs, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return (total_cs / 100.0, f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}")


def _parse_ass_seconds(value: str) -> float:
    match = re.match(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$", value.strip())
    if not match:
        raise ValueError(f"bad ASS time: {value!r}")
    hours, minutes, secs, cs = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + secs + cs / 100.0


def _parse_srt_seconds(value: str) -> float:
    match = re.match(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$", value.strip())
    if not match:
        raise ValueError(f"bad SRT time: {value!r}")
    hours, minutes, secs, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + secs + millis / 1000.0


# ---------------------------------------------------------------- 版式


def _layout(text: str, *, max_chars: int, max_lines: int) -> dict:
    lines = _wrap(text, max_chars) if text else [""]
    return {
        "lines": lines,
        "line_count": len(lines),
        "max_chars_per_line": max(len(line) for line in lines),
        "overflow": any(len(line) > max_chars for line in lines),
        "too_many_lines": len(lines) > max_lines,
        "orphan": len(lines) > 1
                  and len(_tokens(lines[-1])) == 1
                  and len(_tokens(lines[-2])) > 1,
    }


def _safe_area(*, line_count: int, profile: dict) -> dict:
    """纯几何（§5.2 明确：坐标归 S38，压到画面元素归 S36）。

    画幅归一化到 [0,1]。字幕框底边距底 `bottom_pct`，每行高 `line_height_pct`。
    **不做像素估算**：没有画幅/字号契约（S38-C17），只有这两个比例是输入。
    """
    safe = profile.get("safe_area") or {}
    bottom = min(1.0, max(0.0, _pos(safe.get("bottom_pct"), 0.10)))
    side = min(0.5, max(0.0, _pos(safe.get("side_pct"), 0.05)))
    line_height = min(1.0, max(0.0, _pos(
        profile.get("line_height_pct"), DEFAULT_PROFILE["line_height_pct"])))
    height = min(1.0, max(0.001, line_count * line_height))
    y = 1.0 - bottom - height
    ok = y >= 0.0 and (1.0 - 2 * side) > 0.0
    return {
        "bbox": {"x": round(side, 6), "y": round(max(0.0, y), 6),
                 "width": round(max(1e-6, 1.0 - 2 * side), 6),
                 "height": round(height, 6)},
        "ok": bool(ok),
    }


# ---------------------------------------------------------------- 渲染
#
# 渲染是**纯函数**：同一份 cue 行 + 同一格式 → 同一串字节。重试的"不重新
# 渲染"靠这个性质兑现（S38-C10）。任何对时间、随机数、字典顺序的依赖都会
# 让 RENDER_NOT_DETERMINISTIC 变成常态。

ASS_PLAY_RES = (1920, 1080)


def render_srt(cues: list[dict]) -> str:
    blocks = []
    for ordinal, cue in enumerate(cues, start=1):
        _, begin = _format_time(cue["start_seconds"], "srt")
        _, end = _format_time(cue["end_seconds"], "srt")
        body = "\n".join(cue["lines"])
        blocks.append(f"{ordinal}\n{begin} --> {end}\n{body}\n")
    return "\n".join(blocks)


def render_vtt(cues: list[dict]) -> str:
    blocks = ["WEBVTT\n"]
    for ordinal, cue in enumerate(cues, start=1):
        _, begin = _format_time(cue["start_seconds"], "vtt")
        _, end = _format_time(cue["end_seconds"], "vtt")
        body = "\n".join(cue["lines"])
        blocks.append(f"{ordinal}\n{begin} --> {end}\n{body}\n")
    return "\n".join(blocks)


def render_ass(cues: list[dict], *, style_ref: str, font: str,
               profile: dict) -> str:
    width, height = ASS_PLAY_RES
    safe = profile.get("safe_area") or {}
    margin_v = int(round(_pos(safe.get("bottom_pct"), 0.10) * height))
    margin_lr = int(round(_pos(safe.get("side_pct"), 0.05) * width))
    fontsize = int(profile.get("font_size") or 48)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {style_ref},{font},{fontsize},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,"
        f"{margin_lr},{margin_lr},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    rows = []
    for cue in cues:
        _, begin = _format_time(cue["start_seconds"], "ass")
        _, end = _format_time(cue["end_seconds"], "ass")
        body = "\\N".join(cue["lines"])
        rows.append(f"Dialogue: 0,{begin},{end},{style_ref},,0,0,0,,{body}")
    return header + "\n".join(rows) + "\n"


def render(fmt: str, cues: list[dict], *, style_ref: str, font: str,
           profile: dict) -> str:
    if fmt == "srt":
        return render_srt(cues)
    if fmt == "vtt":
        return render_vtt(cues)
    if fmt == "ass":
        return render_ass(cues, style_ref=style_ref, font=font, profile=profile)
    raise ServiceError(422, "INVALID_FORMAT", f"unsupported format {fmt!r}")


# ---------------------------------------------------------------- 结果助手

def _res(check_id, status, reason, defects=None):
    return {"check_id": check_id, "status": status, "reason_code": reason,
            "defects": list(defects or [])}


def _not_evaluated(check_id, reason="dependency_unavailable"):
    return _res(check_id, "not_evaluated", reason)


def _failed(check_id, reason="evaluator_error"):
    return _res(check_id, "failed", reason)


def _na(check_id, reason="no_such_content"):
    return _res(check_id, "not_applicable", reason)


def _evaluated(check_id, defects=None):
    return _res(check_id, "evaluated", "ok", defects)


def _measured(value, unit, threshold_value, comparator):
    return {"value": float(value), "unit": unit,
            "threshold": (None if threshold_value is None
                          else float(threshold_value)),
            "comparator": comparator}


def _mk(*, check_id, severity, scope, start, end, expected, expected_source,
        observed, measured, evidence_id, actions, cue_id=None, confidence=1.0,
        region_bbox=None):
    """构造一条 defect。`detector` 恒为 deterministic。

    字幕没有模型判定：每一条都是对文本/时码/字节的确定计算，所以
    `confidence` 默认 1.0，由调用方在证据本身不完整时下调。
    """
    severity = schema.severity_after(
        schema.BY_ID[check_id]["severity_default"], severity,
        schema.BY_ID[check_id]["severity_max"])
    start = max(0.0, float(start))
    end = max(start + 1e-3, float(end))
    fingerprint = defect_fingerprint(
        check_id=check_id, kind="subtitle", cue_id=cue_id,
        severity=severity, start_seconds=start)
    return {
        "defect_fingerprint": fingerprint,
        "check_id": check_id,
        "type": "subtitle",
        "severity": severity,
        "scope": scope,
        "time_range": {"start_seconds": round(start, 3),
                       "end_seconds": round(end, 3)},
        "region_bbox": region_bbox,
        "character_id": None,
        "expected": expected,
        "expected_source": expected_source,
        "observed": observed,
        "measured": measured,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "detector": "deterministic",
        "evidence_artifact_ids": [evidence_id],
        "recommended_actions": list(actions),
    }


def _source(service_id, ref_id, revision):
    return {"service_id": service_id, "ref_id": str(ref_id),
            "ref_revision": max(1, int(revision or 1))}


def _default_actions(check_id):
    allowed = schema.BY_ID[check_id]["allowed_actions"]
    if schema.BY_ID[check_id].get("never_auto_repair"):
        return ["human_review"]
    return list(allowed)


# ---------------------------------------------------------------- coverage

def _coverage(*, duration, planned, evaluated, required_min) -> dict:
    """字幕的映射（§6）：没有帧、没有音频。

    `planned` 是**切分计划里的段数**，`evaluated` 是真正落了 cue 并跑完检查
    的段数。两者不相等时 `coverage_ratio < 1`——这正是"有些台词没配上时间码"
    的可观测形态（E25-U4）。
    """
    ratio = 0.0 if planned <= 0 else min(1.0, evaluated / planned)
    return {
        "media_duration_seconds": round(max(0.0, float(duration)), 6),
        "frames_sampled": 0,
        "temporal_windows_evaluated": int(evaluated),
        "planned_windows": int(planned),
        "coverage_ratio": round(ratio, 6),
        "required_coverage_min": float(required_min),
        "audio_analyzed": False,
        "audio_analyzed_seconds": 0.0,
        "sampling_policy_version": schema.SAMPLING_VERSION,
    }


# ---------------------------------------------------------------- decide

def decide(*, manifest, defects, coverage, confidence) -> dict:
    """判定。**顺序不能换**：缺项 → 覆盖率 → 未得出结论 → 严重度。

    S38 没有"自动修复已获授权"的输入（policy 只给了 ref，没有下发动作许可），
    所以任何 major 都转人；blocker 里只要有一条 `never_auto_repair` 就整体
    转人，不能挑一条可自动的去做——修好一条、留着另一条，事件会被读成
    "修过了"。
    """
    required = [entry["check_id"] for entry in manifest]
    present = {entry["check_id"] for entry in manifest}
    missing = [c for c in required if c not in present]
    if missing:
        return _abstain("incomplete_coverage", missing,
                        f"check_manifest is missing {sorted(missing)}")

    failed = [entry["check_id"] for entry in manifest
              if entry["status"] in ("failed", "not_evaluated")]
    minimum = float(coverage["required_coverage_min"])
    if coverage["coverage_ratio"] < minimum:
        return _abstain(_reason_for(manifest, failed) if failed
                        else "incomplete_coverage",
                        failed or required,
                        f"coverage_ratio {coverage['coverage_ratio']:.3f} < "
                        f"required {minimum:.3f}")
    if failed:
        return _abstain(_reason_for(manifest, failed), failed,
                        f"{len(failed)} required check(s) did not conclude")

    blockers = [d for d in defects if d["severity"] == "blocker"]
    if blockers:
        auto = [d for d in blockers
                if d["check_id"] not in schema.NEVER_AUTO]
        if auto and len(auto) == len(blockers):
            return _decided("repair", f"{auto[0]['check_id']} blocker")
        return _decided("human_review",
                        f"{blockers[0]['check_id']} blocker needs a human")

    majors = [d for d in defects if d["severity"] == "major"]
    if majors:
        return _decided("human_review",
                        f"{majors[0]['check_id']} major has no authorized "
                        f"automatic route in S38")
    if confidence < TAU_LOW:
        return _decided("human_review", "low_confidence")
    return _decided("pass", "all required checks evaluated; no blocker/major")


def _decided(decision, reason):
    return {"decision": decision, "decision_reason": reason, "abstain": None}


def _abstain(reason_code, failed_check_ids, detail):
    return {
        "decision": "abstain",
        "decision_reason": f"{reason_code}: {detail}",
        "abstain": {"reason_code": reason_code,
                    "failed_check_ids": sorted(set(failed_check_ids)),
                    "detail": detail},
    }


def _reason_for(manifest, failed) -> str:
    """把 manifest 的 reason_code 折成 `abstain.reason_code` 的 7 值闭集。

    两个理由集不同名：manifest 说 `dependency_unavailable`，abstain 只能写
    `dependency_stale`。直接透传会让报告过不了 `qc-report.v2` 校验，而这里
    恰好是最容易写错的地方。
    """
    priority = ("evaluator_timeout", "evaluator_error", "asset_unavailable",
                "dependency_unavailable", "sampling_not_selected",
                "policy_disabled")
    to_abstain = {
        "evaluator_timeout": "evaluator_timeout",
        "evaluator_error": "evaluator_error",
        "asset_unavailable": "asset_unavailable",
        "dependency_unavailable": "dependency_stale",
    }
    by_check = {entry["check_id"]: entry["reason_code"] for entry in manifest}
    codes = {by_check.get(check_id) for check_id in failed}
    for code in priority:
        if code in codes:
            return to_abstain.get(code, "incomplete_coverage")
    return "incomplete_coverage"


def build_dimensions(*, manifest, defects, observations) -> list:
    """`dimensions` 非空。分组分数只是可读汇总，判定不读它。"""
    groups: dict[str, list] = {}
    for check_id in schema.CHECK_IDS:
        groups.setdefault(check_id.split(".")[1].lower(), []).append(check_id)
    by_check = {entry["check_id"]: entry for entry in manifest}
    counts: dict[str, int] = {}
    for defect in defects:
        counts[defect["check_id"]] = counts.get(defect["check_id"], 0) + 1
    out = []
    for group, check_ids in groups.items():
        present = [c for c in check_ids if c in by_check]
        if not present:
            continue
        statuses = [by_check[c]["status"] for c in present]
        total = sum(counts.get(c, 0) for c in present)
        if any(status in ("failed", "not_evaluated") for status in statuses):
            score, reason = None, "some checks did not conclude"
        elif total:
            score, reason = 0.0, f"{total} defect(s)"
        elif all(status == "not_applicable" for status in statuses):
            score, reason = None, "no applicable content"
        else:
            score, reason = 1.0, "all checks passed"
        out.append({"name": group, "score": score, "reason": reason})
    out.extend(observations)
    if not out:
        out.append({"name": "subtitle", "score": None,
                    "reason": "no check produced a conclusion"})
    return out


def compute_confidence(*, manifest) -> float:
    """全部适用项 `evaluated` → 1.0；任何一项没得出结论 → 0.0。"""
    for entry in manifest:
        if entry["status"] == "not_applicable":
            continue
        if entry["status"] != "evaluated":
            return 0.0
    return 1.0


# ---------------------------------------------------------------- 时间线映射


def clip_map(clips: list[dict], tick_rate: int) -> list[dict]:
    """把 S41 的 clip 列表折成 `源时间 → 输出时间` 的分段线性映射。

    `speed_ratio` 改变的是**输出时长**：一段 `out-in` 的源区间在 `speed=2`
    时只占一半输出时间。所以输出区间长度 = `(out-in)/speed`，源时间到输出
    时间的换算除 speed。§3.1 的重算读的就是这张表。
    """
    ordered = sorted(
        (c for c in clips if isinstance(c, dict)),
        key=lambda c: int(c.get("in_ticks") or 0))
    out = []
    cursor = 0.0
    for clip in ordered:
        begin = int(clip.get("in_ticks") or 0) / tick_rate
        end = int(clip.get("out_ticks") or 0) / tick_rate
        speed = float(clip.get("speed_ratio") or 1.0)
        if end <= begin or speed <= 0:
            continue
        duration = (end - begin) / speed
        out.append({
            "clip_id": str(clip.get("clip_id") or ""),
            "source_in": begin, "source_out": end, "speed": speed,
            "output_start": cursor, "output_end": cursor + duration,
        })
        cursor += duration
    return out


def source_to_output(spans: list[dict], seconds: float,
                     duration: float) -> tuple[float, str]:
    """源时间 → (输出时间, clip_id)。落在所有 clip 之外时按端点夹取。

    夹取而不是丢弃：一个落在 EDL 之外的 cue 必须能被 `OUT_OF_RANGE` 看到，
    这里悄悄丢掉就等于替 EDL 决定"这段不该有字幕"。
    """
    if not spans:
        return max(0.0, min(seconds, duration)), ""
    for span in spans:
        if span["source_in"] <= seconds < span["source_out"]:
            return (span["output_start"]
                    + (seconds - span["source_in"]) / span["speed"],
                    span["clip_id"])
    if seconds < spans[0]["source_in"]:
        return spans[0]["output_start"], spans[0]["clip_id"]
    last = spans[-1]
    return (last["output_end"] + (seconds - last["source_out"]),
            last["clip_id"])


def output_to_source(spans: list[dict], seconds: float) -> float:
    """边时间的逆变换，用于 §3.1 只重算受影响的 cue。"""
    if not spans:
        return seconds
    for span in spans:
        if span["output_start"] <= seconds < span["output_end"]:
            return (span["source_in"]
                    + (seconds - span["output_start"]) * span["speed"])
    if seconds < spans[0]["output_start"]:
        return spans[0]["source_in"] + (seconds - spans[0]["output_start"])
    last = spans[-1]
    return last["source_out"] + (seconds - last["output_end"])


# ---------------------------------------------------------------- 配对与规划


def pair_units(units: list[dict], lines: list[dict]) -> list[list[dict]]:
    """把 S37 的 unit 分配到对白行。**只用顺序与字符偏移**。

    `char_offset` 是行内偏移（S37 按每条 utterance 自己的文本从 0 数），所以
    偏移回退（`offset < previous`）意味着换了一条 utterance；偏移越过当前
    行长（`offset >= len(text)`）意味着当前行没有对应的对齐单元。两种情况
    都前移一行。没有单元的行走不到任何 unit，于是它贡献计划数、不贡献 cue
    ——这正是 `coverage_ratio` 要暴露的东西（E25-U4 / S38-C18）。
    """
    out: list[list[dict]] = [[] for _ in lines]
    line_index = 0
    previous = -1
    for ordinal, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        offset = int(unit.get("char_offset") or 0)
        while line_index < len(lines) and (
                offset >= len(str(lines[line_index].get("text") or ""))
                or offset < previous):
            line_index += 1
            previous = -1
        if line_index >= len(lines):
            break
        entry = dict(unit)
        entry.setdefault("index", ordinal)
        out[line_index].append(entry)
        previous = offset
    return out


def _units_in(units: list[dict], begin: int, end: int) -> list[dict]:
    return [u for u in units
            if int(u.get("char_offset") or 0) < end
            and int(u.get("char_offset") or 0)
            + max(1, int(u.get("char_length") or 1)) > begin]


def _bounds(units: list[dict], begin: int, end: int):
    """一段文本的 (源起点, 源终点, ci_low, ci_high, 首 unit, 末 unit)。

    `start`/`end` 是点估计，`ci_low`/`ci_high` 是区间。§4.1 明说时码要用
    **区间边界**，否则对齐抖动会让字幕闪；点估计只用来算漂移。
    """
    inside = _units_in(units, begin, end)
    if not inside:
        return None
    first, last = inside[0], inside[-1]
    low = first.get("ci_low")
    high = last.get("ci_high")
    low = float(low) if _finite(low) else float(first.get("start") or 0.0)
    high = float(high) if _finite(high) else float(last.get("end") or 0.0)
    return {
        "source_start": float(first.get("start") or low),
        "source_end": float(last.get("end") or high),
        "ci_low": low,
        "ci_high": high,
        "unit_start": int(first.get("index") or 0),
        "unit_end": int(last.get("index") or 0),
    }


def _split_for_cps(text: str, span: tuple[int, int], units: list[dict],
                   max_cps: float, depth: int) -> list[tuple[int, int]]:
    """§4.3 第 1 步：超 CPS 时重新切分（不是缩字）。"""
    bounds = _bounds(units, span[0], span[1])
    if bounds is None:
        return [span]
    duration = max(1e-6, bounds["ci_high"] - bounds["ci_low"])
    cps = _char_count(text[span[0]:span[1]]) / duration
    if cps <= max_cps or depth <= 0:
        return [span]
    pieces = _split_span(text, span[0], span[1])
    if len(pieces) < 2:
        return [span]
    out: list[tuple[int, int]] = []
    for piece in pieces:
        if _bounds(units, piece[0], piece[1]) is None:
            # 拆出来的半边没有对齐单元 → 拆了也排不出时码，保持原样。
            return [span]
        out.extend(_split_for_cps(text, piece, units, max_cps, depth - 1))
    return out


def plan_cues(*, track_id: str, lines: list[dict], units: list[dict],
              spans: list[dict], duration: float, profile: dict,
              translations: dict, lexicon, dialogue_revision: int,
              style_ref: str, node_epoch: int,
              alignment_report_id: str) -> dict:
    """切分 + 时码 + 版式。返回 `{planned, cues}`，**不落库、不渲染**。

    `planned` 是切分计划里的段数（没有对齐单元的行按 1 段计）。`evaluated`
    由调用方按真正产出的 cue 数填——两者之差就是报告的覆盖率缺口。
    """
    max_cps = float(profile["max_cps"])
    max_chars = int(profile["max_chars_per_line"])
    max_lines = int(profile["max_lines"])

    per_line = pair_units(units, lines)
    rows: list[dict] = []
    planned = 0
    for line_index, line in enumerate(lines):
        text = str(line.get("text") or "")
        if not text.strip():
            continue
        line_units = per_line[line_index]
        if not line_units:
            # 没有时间码就不能有 cue；但计划数是 1——否则覆盖率会假装 100%。
            planned += 1
            continue
        segment_spans: list[tuple[int, int]] = []
        for span in _segment_text(text):
            segment_spans.extend(
                _split_for_cps(text, span, line_units, max_cps, depth=3))
        planned += len(segment_spans)
        for segment_index, span in enumerate(segment_spans):
            bounds = _bounds(line_units, span[0], span[1])
            if bounds is None:
                continue
            rows.append({
                "line": line, "line_index": line_index, "text": text,
                "span": span, "segment_index": segment_index,
                "bounds": bounds,
            })

    # 先按源时间排序，再统一做 lead-in / lead-out：提前量取决于邻居，逐条
    # 独立算会在连续台词上造出重叠（§4.2）。
    rows.sort(key=lambda row: (row["bounds"]["ci_low"], row["line_index"],
                               row["span"][0]))
    low_high = []
    for row in rows:
        out_start, clip_id = source_to_output(
            spans, row["bounds"]["ci_low"], duration)
        out_end, _ = source_to_output(spans, row["bounds"]["ci_high"], duration)
        row["clip_id"] = clip_id
        low_high.append((out_start, out_end))

    cues: list[dict] = []
    for index, row in enumerate(rows):
        out_start, out_end = low_high[index]
        previous_end = low_high[index - 1][1] if index else 0.0
        next_start = (low_high[index + 1][0]
                      if index + 1 < len(low_high) else duration)
        lead_in = min(schema.MAX_LEAD_IN_MS / 1000.0,
                      max(0.0, out_start - previous_end))
        lead_out = min(schema.MAX_LEAD_OUT_MS / 1000.0,
                       max(0.0, next_start - out_end))
        start = max(0.0, out_start - lead_in)
        end = min(duration, out_end + lead_out)
        minimum = schema.MIN_DURATION_MS / 1000.0
        if end - start < minimum:
            # 向邻居扩展，不越过下一条 cue 的起点，也不越过 timeline 末尾。
            end = max(end, min(start + minimum, next_start))
        if end < start:
            # E24-U7：整条 cue 落在 timeline 末尾之后时，`min(duration, ...)`
            # 会把 end 压到 start 之前。夹到零长会让 DDL 拒绝整条命令，而
            # "这条 cue 在 EDL 之外"正是 OUT_OF_RANGE 要报告的事实——把它
            # 藏进一次 500 就等于替 EDL 删掉了这段台词。
            end = start + minimum
        start = min(start, schema.MAX_SECONDS)
        end = min(end, schema.MAX_SECONDS)
        cues.append(_cue_row(
            track_id=track_id, row=row, start=start, end=end,
            aligned_start=out_start, aligned_end=out_end,
            profile=profile, translations=translations, lexicon=lexicon,
            dialogue_revision=dialogue_revision, style_ref=style_ref,
            node_epoch=node_epoch,
            alignment_report_id=alignment_report_id,
            cue_index=index, max_chars=max_chars, max_lines=max_lines))
    return {"planned": planned, "cues": cues}


def _translation_slice(text: str, span: tuple[int, int], source_length: int):
    """按源字符占比切译文。只有单段行才可能拿到整句。

    这是**近似**：译文的断句与源文不必对齐，任何按比例切的方案都会在长句
    上偏。之所以仍然这么做，是因为另一种选择（每条 cue 都用整句译文）会让
    相邻 cue 重复同一段文字——那比偏一点更糟。登记为 `S38-C19`。
    """
    if not text:
        return ""
    if source_length <= 0:
        return text
    total = max(1, len(text))
    begin = int(round(total * span[0] / source_length))
    end = int(round(total * span[1] / source_length))
    piece = text[begin:max(begin + 1, end)].strip()
    return piece or text.strip()


def _cue_row(*, track_id, row, start, end, aligned_start, aligned_end,
             profile, translations, lexicon, dialogue_revision, style_ref,
             node_epoch, alignment_report_id, cue_index, max_chars,
             max_lines) -> dict:
    line = row["line"]
    line_id = str(line.get("line_id") or line.get("id") or "")
    line_text = str(line.get("text") or "")
    span = row["span"]
    source_text = line_text[span[0]:span[1]]
    translation = translations.get(line_id)
    # `_translations` 按行存的是 `texts[line_id]`，不是整份 `text`。按行取，
    # 否则每条 cue 都退回源文：译文永远进不了 `display_text`，
    # `TRANSLATION_LENGTH_DRIFT` 也就永远不会触发（V2 要求 display 可变）。
    translation_text = (translation or {}).get("texts", {}).get(line_id)
    if translation_text:
        display = _translation_slice(translation_text, span, len(line_text))
    else:
        display = source_text
    display = display.strip() or source_text.strip()
    layout = _layout(display, max_chars=max_chars, max_lines=max_lines)
    safe = _safe_area(line_count=layout["line_count"], profile=profile)
    return {
        "cue_id": cue_id_for(track_id, line_id, row["segment_index"]),
        "cue_index": cue_index,
        "clip_id": row["clip_id"] or "unplaced",
        "script_line_id": line_id,
        "source_text_revision": int(line.get("revision") or dialogue_revision),
        "source_text_hash": _sha256_hex(line_text),
        "lexicon_ref_id": (lexicon or {}).get("id"),
        "lexicon_revision": (lexicon or {}).get("revision"),
        "lexicon_hash": (lexicon or {}).get("content_hash"),
        "translation_id": (translation or {}).get("translation_id"),
        "translation_revision": (translation or {}).get("revision"),
        "translation_hash": (translation or {}).get("content_hash"),
        "translation_status": (translation or {}).get("status"),
        "display_text": display,
        "source_text": source_text,
        "translation_text": translation_text,
        # 译文/源文的长度比按**整行**算（见 `_length_ratio`）。
        "translation_ratio": (
            1.0 if not translation_text
            else _char_count(translation_text)
            / max(1, _char_count(line_text))),
        "start_seconds": round(float(start), 6),
        "end_seconds": round(float(end), 6),
        # 未加 lead-in/out 的输出区间：DRIFT_VS_ALIGNMENT 要跟它比，
        # 而不是跟加了提前量的边界比。
        "aligned_start": round(float(aligned_start), 6),
        "aligned_end": round(float(aligned_end), 6),
        "alignment_report_id": alignment_report_id,
        "align_unit_start": row["bounds"]["unit_start"],
        "align_unit_end": row["bounds"]["unit_end"],
        "align_low": round(float(row["bounds"]["ci_low"]), 6),
        "align_high": round(float(row["bounds"]["ci_high"]), 6),
        "node_epoch": int(node_epoch),
        "line_count": layout["line_count"],
        "max_chars_per_line": layout["max_chars_per_line"],
        "style_ref": style_ref,
        "safe_area_ok": 1 if safe["ok"] else 0,
        "lines": layout["lines"],
        "layout": layout,
        "safe": safe,
        "source_line_text": line_text,
        "span": span,
    }


# ---------------------------------------------------------------- 检查引擎
#
# 24 条检查按 §5 分四组。每条都遵守同一条规矩：**先问"判不了吗"，再问
# "过了吗"**。前置条件不满足时记 `not_evaluated` / `not_applicable`，绝不
# 落一个"没有 defect 所以通过"。

EPS = 1e-6


def _expected_ref(check_id, *, dialogue_id, dialogue_revision,
                  alignment_report_id):
    """缺陷的 `expected_source`。谁定的基准就写谁，不问模型。"""
    service_id = schema.expected_source(check_id)
    if service_id == "S37":
        return _source("S37", alignment_report_id, 1)
    return _source(service_id, dialogue_id, dialogue_revision)


def _ordered(cues):
    return sorted(cues, key=lambda cue: (cue["start_seconds"],
                                         cue["cue_index"]))


def _ass_pair(cue):
    """渲染后这条 cue 在 ASS 里的 (start_cs, end_cs)。"""
    start, _ = _format_time(cue["start_seconds"], "ass")
    end, _ = _format_time(cue["end_seconds"], "ass")
    return int(round(start * 100)), int(round(end * 100))


def _timing(defects, cues, *, duration, alignment_ok, timeline_has_clips,
            alignment_report_id, dialogue_id, dialogue_revision, evidence_id,
            profile):
    """§5.1 的八条。S37 弃权时整组 `not_evaluated`（E25-U1）。"""
    out = []
    if not alignment_ok:
        for check_id in ("SUB.TIMING.OVERLAP",
                         "SUB.TIMING.NEGATIVE_DURATION",
                         "SUB.TIMING.TOO_SHORT", "SUB.TIMING.TOO_LONG",
                         "SUB.TIMING.DRIFT_VS_ALIGNMENT",
                         "SUB.TIMING.OUT_OF_RANGE"):
            out.append(_not_evaluated(check_id, "dependency_unavailable"))
        out.append(_na("SUB.TIMING.CPS_EXCEEDED", "no_dialogue"))
        out.append(_na("SUB.TIMING.FRAME_BOUNDARY", "no_dialogue"))
        return out

    ordered = _ordered(cues)
    overlap, negative, short, long_, cps, out_of_range, drift = ([] for _ in
                                                                range(7))
    max_cps = float(profile["max_cps"])

    for cue in ordered:
        begin = float(cue["start_seconds"])
        end = float(cue["end_seconds"])
        elapsed = end - begin
        if end <= begin + EPS:
            negative.append(_mk(
                check_id="SUB.TIMING.NEGATIVE_DURATION", severity="blocker",
                scope="window", start=begin, end=max(end, begin + 1e-3),
                expected="end > start",
                expected_source=_expected_ref(
                    "SUB.TIMING.NEGATIVE_DURATION", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} ends at or before it starts",
                measured=_measured(max(0.0, elapsed) * 1000, "ms", 0.0, "gt"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.TIMING.NEGATIVE_DURATION")))
        else:
            if elapsed < schema.MIN_DURATION_MS / 1000.0:
                short.append(_mk(
                    check_id="SUB.TIMING.TOO_SHORT", severity="major",
                    scope="window", start=begin, end=end,
                    expected=f"duration >= {schema.MIN_DURATION_MS}ms",
                    expected_source=_expected_ref(
                        "SUB.TIMING.TOO_SHORT", dialogue_id=dialogue_id,
                        dialogue_revision=dialogue_revision,
                        alignment_report_id=alignment_report_id),
                    observed=f"cue {cue['cue_id']} lasts "
                             f"{elapsed * 1000:.0f}ms and cannot be extended "
                             f"into its neighbours",
                    measured=_measured(elapsed * 1000, "ms",
                                       schema.MIN_DURATION_MS, "gte"),
                    evidence_id=evidence_id, cue_id=cue["cue_id"],
                    actions=_default_actions("SUB.TIMING.TOO_SHORT")))
            if elapsed > schema.MAX_DURATION_MS / 1000.0:
                long_.append(_mk(
                    check_id="SUB.TIMING.TOO_LONG", severity="minor",
                    scope="window", start=begin, end=end,
                    expected=f"duration <= {schema.MAX_DURATION_MS}ms",
                    expected_source=_expected_ref(
                        "SUB.TIMING.TOO_LONG", dialogue_id=dialogue_id,
                        dialogue_revision=dialogue_revision,
                        alignment_report_id=alignment_report_id),
                    observed=f"cue {cue['cue_id']} lasts "
                             f"{elapsed * 1000:.0f}ms",
                    measured=_measured(elapsed * 1000, "ms",
                                       schema.MAX_DURATION_MS, "lte"),
                    evidence_id=evidence_id, cue_id=cue["cue_id"],
                    actions=_default_actions("SUB.TIMING.TOO_LONG")))
        chars = _char_count(cue["display_text"])
        rate = chars / elapsed if elapsed > EPS else float("inf")
        if rate > max_cps:
            # §4.3 的三步已用完：切分在 plan_cues 里做过，扩展在时码阶段做过。
            # 到这里还超，只能人改文案或放宽 profile——不许静默缩字。
            cps.append(_mk(
                check_id="SUB.TIMING.CPS_EXCEEDED", severity="major",
                scope="window", start=begin, end=end,
                expected=f"cps <= {max_cps:g}",
                expected_source=_expected_ref(
                    "SUB.TIMING.CPS_EXCEEDED", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} reads at {rate:.2f} cps "
                         f"({chars} chars / {elapsed:.3f}s)",
                measured=_measured(rate, "chars_per_second", max_cps, "lte"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.TIMING.CPS_EXCEEDED")))
        unplaced = timeline_has_clips and cue.get("clip_id") in (
            None, "", "unplaced")
        if begin < -EPS or end > duration + EPS or unplaced:
            out_of_range.append(_mk(
                check_id="SUB.TIMING.OUT_OF_RANGE", severity="blocker",
                scope="window", start=max(0.0, begin), end=end,
                expected=f"cue inside [0, {duration:.3f}]",
                expected_source=_expected_ref(
                    "SUB.TIMING.OUT_OF_RANGE", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} spans {begin:.3f}..{end:.3f} "
                         f"on clip {cue.get('clip_id')!r}",
                measured=_measured(max(0.0, end - duration), "seconds", 0.0,
                                   "lte"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.TIMING.OUT_OF_RANGE")))

        # 与 S37 的对齐区间比：允许 lead_in / lead_out 与容差，超出的部分才是漂移。
        aligned_start = float(cue.get("aligned_start", begin))
        aligned_end = float(cue.get("aligned_end", end))
        tol = schema.DRIFT_TOLERANCE_MS / 1000.0
        start_drift = max(0.0, begin - aligned_start,
                          aligned_start - begin
                          - schema.MAX_LEAD_IN_MS / 1000.0 - tol)
        end_drift = max(0.0, aligned_end - end, end - aligned_end
                        - schema.MAX_LEAD_OUT_MS / 1000.0 - tol)
        worst = max(start_drift, end_drift)
        if worst > tol:
            drift.append(_mk(
                check_id="SUB.TIMING.DRIFT_VS_ALIGNMENT", severity="major",
                scope="window", start=begin, end=end,
                expected=f"cue boundary within "
                         f"{(schema.DRIFT_TOLERANCE_MS / 1000.0):.3f}s of "
                         f"the S37 interval",
                expected_source=_expected_ref(
                    "SUB.TIMING.DRIFT_VS_ALIGNMENT", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} drifted {worst * 1000:.0f}ms "
                         f"from {aligned_start:.3f}..{aligned_end:.3f}",
                measured=_measured(worst * 1000, "ms",
                                   schema.DRIFT_TOLERANCE_MS, "lte"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.TIMING.DRIFT_VS_ALIGNMENT")))

    for prev, cur in zip(ordered, ordered[1:]):
        overlap_seconds = float(prev["end_seconds"]) - float(cur["start_seconds"])
        if overlap_seconds > EPS:
            overlap.append(_mk(
                check_id="SUB.TIMING.OVERLAP", severity="blocker",
                scope="window",
                start=float(prev["start_seconds"]),
                end=float(cur["end_seconds"]),
                expected="adjacent cues do not share output time",
                expected_source=_expected_ref(
                    "SUB.TIMING.OVERLAP", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cues {prev['cue_id']} and {cur['cue_id']} overlap "
                         f"by {overlap_seconds * 1000:.0f}ms",
                measured=_measured(overlap_seconds * 1000, "ms", 0.0, "lte"),
                evidence_id=evidence_id, cue_id=cur["cue_id"],
                actions=_default_actions("SUB.TIMING.OVERLAP")))
            continue
        # ASS 只有厘秒精度。取整后区间塌成零长的 cue 会和任何相邻时间点相撞，
        # 这是 E24-U2 要抓的那一类（§4.1 明说必须复检）。
        p_start, p_end = _ass_pair(prev)
        c_start, c_end = _ass_pair(cur)
        if c_end <= c_start or (c_start < p_end and c_start > p_start):
            overlap.append(_mk(
                check_id="SUB.TIMING.OVERLAP", severity="blocker",
                scope="window",
                start=float(prev["start_seconds"]),
                end=float(cur["end_seconds"]),
                expected="ASS centisecond rounding preserves a positive "
                         "interval and the cue order",
                expected_source=_expected_ref(
                    "SUB.TIMING.OVERLAP", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"after ASS rounding cue {cur['cue_id']} occupies "
                         f"{c_start / 100:.2f}..{c_end / 100:.2f}s following "
                         f"{p_start / 100:.2f}..{p_end / 100:.2f}s",
                measured=_measured((c_end - c_start) / 100, "seconds", 0.0,
                                   "gt"),
                evidence_id=evidence_id, cue_id=cur["cue_id"],
                actions=_default_actions("SUB.TIMING.OVERLAP")))

    defects.extend(overlap + negative + short + long_ + cps + out_of_range
                   + drift)
    frame_rate = profile.get("frame_rate")
    if frame_rate:
        numerator, denominator = frame_rate
        boundary = []
        for cue in ordered:
            frames = cue["start_seconds"] * numerator / denominator
            if abs(frames - round(frames)) > 1e-3:
                boundary.append(_mk(
                    check_id="SUB.TIMING.FRAME_BOUNDARY", severity="info",
                    scope="frame", start=cue["start_seconds"],
                    end=cue["end_seconds"],
                    expected=f"start on a {numerator}/{denominator} frame "
                             f"boundary",
                    expected_source=_expected_ref(
                        "SUB.TIMING.FRAME_BOUNDARY", dialogue_id=dialogue_id,
                        dialogue_revision=dialogue_revision,
                        alignment_report_id=alignment_report_id),
                    observed=f"cue {cue['cue_id']} starts "
                             f"{frames:.3f} frames in",
                    measured=_measured(frames, "frames", round(frames), "eq"),
                    evidence_id=evidence_id, cue_id=cue["cue_id"],
                    actions=_default_actions("SUB.TIMING.FRAME_BOUNDARY")))
        defects.extend(boundary)
        out.append(_evaluated("SUB.TIMING.FRAME_BOUNDARY",
                              [d["defect_fingerprint"] for d in boundary]))
    else:
        # 契约里没有帧率（deps 的 timeline 投影只有 tick_rate）。这是交付
        # 规格策略，不是"查不出来"：登记为 policy_disabled，不进 abstain。
        out.append(_na("SUB.TIMING.FRAME_BOUNDARY", "policy_disabled"))

    out.append(_evaluated("SUB.TIMING.OVERLAP",
                          [d["defect_fingerprint"] for d in overlap]))
    out.append(_evaluated("SUB.TIMING.NEGATIVE_DURATION",
                          [d["defect_fingerprint"] for d in negative]))
    out.append(_evaluated("SUB.TIMING.TOO_SHORT",
                          [d["defect_fingerprint"] for d in short]))
    out.append(_evaluated("SUB.TIMING.TOO_LONG",
                          [d["defect_fingerprint"] for d in long_]))
    out.append(_evaluated("SUB.TIMING.CPS_EXCEEDED",
                          [d["defect_fingerprint"] for d in cps]))
    out.append(_evaluated("SUB.TIMING.OUT_OF_RANGE",
                          [d["defect_fingerprint"] for d in out_of_range]))
    out.append(_evaluated("SUB.TIMING.DRIFT_VS_ALIGNMENT",
                          [d["defect_fingerprint"] for d in drift]))
    return out


def _layout_checks(defects, cues, *, profile, formats, style_ref, style_known,
                   dialogue_id, dialogue_revision, alignment_report_id,
                   evidence_id):
    """§5.2 的五条。"""
    out = []
    overflow, too_many, unsafe, orphan = [], [], [], []
    for cue in cues:
        layout = cue["layout"]
        begin, end = cue["start_seconds"], cue["end_seconds"]
        if layout["overflow"]:
            overflow.append(_mk(
                check_id="SUB.LAYOUT.LINE_OVERFLOW", severity="major",
                scope="window", start=begin, end=end,
                expected=f"each line <= {profile['max_chars_per_line']} "
                         f"characters",
                expected_source=_expected_ref(
                    "SUB.LAYOUT.LINE_OVERFLOW", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} has a "
                         f"{layout['max_chars_per_line']}-character line",
                measured=_measured(layout["max_chars_per_line"], "characters",
                                   profile["max_chars_per_line"], "lte"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.LAYOUT.LINE_OVERFLOW")))
        if layout["too_many_lines"]:
            too_many.append(_mk(
                check_id="SUB.LAYOUT.TOO_MANY_LINES", severity="major",
                scope="window", start=begin, end=end,
                expected=f"line_count <= {profile['max_lines']}",
                expected_source=_expected_ref(
                    "SUB.LAYOUT.TOO_MANY_LINES", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} wraps to "
                         f"{layout['line_count']} lines",
                measured=_measured(layout["line_count"], "lines",
                                   profile["max_lines"], "lte"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.LAYOUT.TOO_MANY_LINES")))
        if not cue["safe"]["ok"]:
            unsafe.append(_mk(
                check_id="SUB.LAYOUT.SAFE_AREA", severity="major",
                scope="window", start=begin, end=end,
                expected="text box fully inside the profile safe area",
                expected_source=_expected_ref(
                    "SUB.LAYOUT.SAFE_AREA", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} box extends above the safe "
                         f"area with {layout['line_count']} lines",
                measured=None, evidence_id=evidence_id,
                cue_id=cue["cue_id"],
                region_bbox=cue["safe"]["bbox"],
                actions=_default_actions("SUB.LAYOUT.SAFE_AREA")))
        if layout["orphan"]:
            orphan.append(_mk(
                check_id="SUB.LAYOUT.ORPHAN_WORD", severity="info",
                scope="window", start=begin, end=end,
                expected="the last line carries more than one token",
                expected_source=_expected_ref(
                    "SUB.LAYOUT.ORPHAN_WORD", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"cue {cue['cue_id']} last line is a single token",
                measured=_measured(1, "tokens", 1, "gt"),
                evidence_id=evidence_id, cue_id=cue["cue_id"],
                actions=_default_actions("SUB.LAYOUT.ORPHAN_WORD")))
    defects.extend(overflow + too_many + unsafe + orphan)
    out.append(_evaluated("SUB.LAYOUT.LINE_OVERFLOW",
                          [d["defect_fingerprint"] for d in overflow]))
    out.append(_evaluated("SUB.LAYOUT.TOO_MANY_LINES",
                          [d["defect_fingerprint"] for d in too_many]))
    out.append(_evaluated("SUB.LAYOUT.SAFE_AREA",
                          [d["defect_fingerprint"] for d in unsafe]))
    out.append(_evaluated("SUB.LAYOUT.ORPHAN_WORD",
                          [d["defect_fingerprint"] for d in orphan]))

    if "ass" not in formats:
        # 没有 ASS 产物就没有字体可引；这是"没有适用内容"，不是"字体没问题"。
        out.append(_na("SUB.LAYOUT.FONT_MISSING", "no_such_content"))
    elif not style_known:
        # C17：没有版式模板契约，拿不到字体白名单。不判通过。
        out.append(_not_evaluated("SUB.LAYOUT.FONT_MISSING",
                                  "dependency_unavailable"))
    else:
        missing_font = _missing_fonts(cues, profile)
        if missing_font is None:
            out.append(_evaluated("SUB.LAYOUT.FONT_MISSING", []))
        else:
            item = _mk(
                check_id="SUB.LAYOUT.FONT_MISSING", severity="blocker",
                scope="track", start=min(c["start_seconds"] for c in cues),
                end=max(c["end_seconds"] for c in cues),
                expected=f"font {profile.get('font')!r} is available to the "
                         f"renderer",
                expected_source=_expected_ref(
                    "SUB.LAYOUT.FONT_MISSING", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"font {missing_font!r} is not in fonts_available",
                measured=_measured(0, "fonts", 1, "gte"),
                evidence_id=evidence_id,
                actions=_default_actions("SUB.LAYOUT.FONT_MISSING"))
            defects.append(item)
            out.append(_evaluated("SUB.LAYOUT.FONT_MISSING",
                                  [item["defect_fingerprint"]]))
    return out


def _missing_fonts(cues, profile):
    """返回渲染实际要用的字体名，若它不在白名单；否则 None。"""
    if not cues:
        return None
    font = str(profile.get("font") or DEFAULT_PROFILE["font"])
    available = profile.get("fonts_available")
    if not available:
        return None
    return None if font in set(available) else font


def _text_checks(defects, cues, *, expected_source_revision, source_locale,
                 target_locale, lexicon_present, profile, dialogue_id,
                 dialogue_revision, alignment_report_id, evidence_id):
    """§5.3 的七条。`SCRIPT_CONFLICT` 只产观测，永不产 defect。"""
    out: list[dict] = []
    observations: list[dict] = []
    begin_all = min((c["start_seconds"] for c in cues), default=0.0)
    end_all = max((c["end_seconds"] for c in cues), default=0.0)
    translated = target_locale != source_locale

    def _tdefect(check_id, severity, cue, observed, measured, expected,
                 *, track_scope):
        return _mk(
            check_id=check_id, severity=severity,
            scope="track" if track_scope else "window",
            start=begin_all if track_scope else cue["start_seconds"],
            end=end_all if track_scope else cue["end_seconds"],
            expected=expected,
            expected_source=_expected_ref(
                check_id, dialogue_id=dialogue_id,
                dialogue_revision=dialogue_revision,
                alignment_report_id=alignment_report_id),
            observed=observed, measured=measured, evidence_id=evidence_id,
            cue_id=cue["cue_id"], actions=_default_actions(check_id))

    # --- SOURCE_STALE：cue 的构建基准是否落后于 S06 当前 revision。
    stale = [c for c in cues
             if int(c["source_text_revision"] or 0) < expected_source_revision]
    source_defects = [
        _mk(check_id="SUB.TEXT.SOURCE_STALE", severity="blocker",
            scope="track", start=c["start_seconds"], end=c["end_seconds"],
            expected=f"cue built against S06 revision "
                     f"{expected_source_revision}",
            expected_source=_source("S06", dialogue_id,
                                    expected_source_revision),
            observed=f"cue {c['cue_id']} was built against revision "
                     f"{c['source_text_revision']}",
            measured=_measured(int(c["source_text_revision"]), "revision",
                               expected_source_revision, "gte"),
            evidence_id=evidence_id, cue_id=c["cue_id"],
            actions=_default_actions("SUB.TEXT.SOURCE_STALE"))
        for c in stale]
    defects.extend(source_defects)
    out.append(_evaluated("SUB.TEXT.SOURCE_STALE",
                          [d["defect_fingerprint"] for d in source_defects]))

    # --- 译文四条。
    #
    # 目标语言 == 源语言时没有译文这回事，四条全部 `not_applicable
    # (no_such_content)`。判成 evaluated 会让"同语言交付"永远带假通过。
    if not translated:
        for check_id in ("SUB.TEXT.TRANSLATION_STALE",
                         "SUB.TEXT.TRANSLATION_DRAFT",
                         "SUB.TEXT.TRANSLATION_MISSING",
                         "SUB.TEXT.TRANSLATION_LENGTH_DRIFT"):
            out.append(_na(check_id, "no_such_content"))
    else:
        stale_t = [c for c in cues
                   if c.get("translation_status") == "stale"]
        draft_t = [c for c in cues
                   if c.get("translation_status") == "draft"]
        # E25-U2：翻译作业超时/未产出 → 没有 translation_id。这是 blocker，
        # **不是**"空译文但通过"。
        missing_t = [c for c in cues if not c.get("translation_id")]
        drift_t = [c for c in cues if c.get("translation_id")
                   and _length_drift(c)]

        stale_defects = [
            _tdefect("SUB.TEXT.TRANSLATION_STALE", "blocker", c,
                     f"cue {c['cue_id']} uses translation "
                     f"{c.get('translation_id')} which is stale against a "
                     f"newer source revision",
                     _measured(1, "stale_translations", 0, "lte"),
                     "translation status is reviewed or accepted",
                     track_scope=True)
            for c in stale_t]
        draft_defects = [
            _tdefect("SUB.TEXT.TRANSLATION_DRAFT", "major", c,
                     f"cue {c['cue_id']} uses a draft translation",
                     _measured(1, "draft_translations", 0, "lte"),
                     "translation status is reviewed or accepted",
                     track_scope=True)
            for c in draft_t]
        missing_defects = [
            _tdefect("SUB.TEXT.TRANSLATION_MISSING", "blocker", c,
                     f"cue {c['cue_id']} has no translation for "
                     f"{target_locale}",
                     _measured(0, "translations", 1, "gte"),
                     f"a translation for locale {target_locale}",
                     track_scope=False)
            for c in missing_t]
        drift_defects = []
        for cue in drift_t:
            ratio = float(cue["translation_ratio"])
            drift_defects.append(_tdefect(
                "SUB.TEXT.TRANSLATION_LENGTH_DRIFT", "major", cue,
                f"cue {cue['cue_id']} translation is {ratio:.2f}x the "
                f"length of its source line",
                _measured(ratio, "ratio",
                          1 + schema.TRANSLATION_LENGTH_DRIFT, "lte"),
                f"length ratio within "
                f"{1 - schema.TRANSLATION_LENGTH_DRIFT:.2f}.."
                f"{1 + schema.TRANSLATION_LENGTH_DRIFT:.2f}",
                track_scope=False))

        defects.extend(stale_defects + draft_defects + missing_defects
                       + drift_defects)
        out.append(_evaluated("SUB.TEXT.TRANSLATION_STALE",
                              [d["defect_fingerprint"]
                               for d in stale_defects]))
        out.append(_evaluated("SUB.TEXT.TRANSLATION_DRAFT",
                              [d["defect_fingerprint"]
                               for d in draft_defects]))
        out.append(_evaluated("SUB.TEXT.TRANSLATION_MISSING",
                              [d["defect_fingerprint"]
                               for d in missing_defects]))
        out.append(_evaluated("SUB.TEXT.TRANSLATION_LENGTH_DRIFT",
                              [d["defect_fingerprint"]
                               for d in drift_defects]))

    # --- LEXICON_UNUSED。E25-U3：读不到词典 → dependency_unavailable，
    # 不把"没引用"判成缺陷（那会惩罚一个没被告知该引用什么的调用方）。
    if not lexicon_present:
        out.append(_not_evaluated("SUB.TEXT.LEXICON_UNUSED",
                                  "dependency_unavailable"))
    else:
        unused = [c for c in cues if not c.get("lexicon_ref_id")]
        lexicon_defects = []
        if unused:
            lexicon_defects.append(_mk(
                check_id="SUB.TEXT.LEXICON_UNUSED", severity="minor",
                scope="track", start=begin_all, end=end_all,
                expected="every cue references the pronunciation lexicon "
                         "revision it was read with",
                expected_source=_source("S15", "lexicon", 1),
                observed=f"{len(unused)} of {len(cues)} cue(s) carry no "
                         f"lexicon reference",
                measured=_measured(len(cues) - len(unused), "referencing_cues",
                                   len(cues), "gte"),
                evidence_id=evidence_id,
                actions=_default_actions("SUB.TEXT.LEXICON_UNUSED")))
        defects.extend(lexicon_defects)
        out.append(_evaluated("SUB.TEXT.LEXICON_UNUSED",
                              [d["defect_fingerprint"]
                               for d in lexicon_defects]))

    # --- SCRIPT_CONFLICT 是观测型：只写 dimension，永不产 defect。
    # "某行台词有字没有对齐单元"是 S06 与 S37 之间的分歧，谁对谁错是创作
    # 决定（注册表 `OBSERVATION_ONLY`）。
    gaps = [c for c in cues
            if c["span"][1] < len(c["source_line_text"])]
    if gaps:
        observations.append({
            "name": "script_conflict",
            "score": None,
            "reason": f"{len(gaps)} cue(s) cover less text than their script "
                      f"line; S38 records the observation only",
        })
    out.append(_evaluated("SUB.TEXT.SCRIPT_CONFLICT", []))
    return out, observations


def _length_ratio(cue):
    """整行级别的译文/源文长度比（不是 cue 片段的比）。

    §5.3 说的是"译文长度相对源文"，源文的单位是**对白行**；按 cue 片段比
    会把一条长行切成的三段各自放大成 3 倍，制造假缺陷。
    """
    source = _char_count(cue.get("source_line_text") or "")
    translated = _char_count(cue.get("translation_text") or "")
    if source <= 0:
        return 1.0
    return translated / source


def _length_drift(cue):
    return abs(float(cue["translation_ratio"]) - 1.0) \
        > schema.TRANSLATION_LENGTH_DRIFT


def _format_checks(defects, *, rendered, formats, style_ref, track_start,
                   track_end, dialogue_id, dialogue_revision,
                   alignment_report_id, evidence_id):
    """§5.4 的四条。检查的是**渲染出来的字节**，不是意图。"""
    out = []
    begin = float(track_start)
    end = max(float(track_end), begin + 1e-3)

    def _entry(check_id, defects_for_check, observed, expected, measured):
        item = None
        if observed is not None:
            item = _mk(
                check_id=check_id, severity="blocker", scope="track",
                start=begin, end=end, expected=expected,
                expected_source=_expected_ref(
                    check_id, dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=observed, measured=measured, evidence_id=evidence_id,
                actions=_default_actions(check_id))
            defects_for_check.append(item)
        return _evaluated(check_id,
                          [item["defect_fingerprint"]] if item else [])

    srt = rendered.get("srt")
    if srt is None:
        out.append(_na("SUB.FORMAT.SRT_SYNTAX", "no_such_content"))
    else:
        out.append(_entry(
            "SUB.FORMAT.SRT_SYNTAX", defects, _srt_problem(srt),
            "sequential indices, valid time lines, blank-line separated "
            "blocks", None))

    invalid = []
    for fmt in sorted(rendered):
        text = rendered[fmt]
        try:
            encoded = text.encode("utf-8")
            if b"\x00" in encoded or encoded.decode("utf-8") != text:
                invalid.append(f"{fmt}: NUL byte or round-trip mismatch")
        except UnicodeEncodeError as exc:
            invalid.append(f"{fmt}: {exc}")
    if invalid:
        item = _mk(
            check_id="SUB.FORMAT.ENCODING", severity="blocker", scope="track",
            start=begin, end=end,
            expected="every rendition is valid UTF-8 with no NUL bytes",
            expected_source=_expected_ref(
                "SUB.FORMAT.ENCODING", dialogue_id=dialogue_id,
                dialogue_revision=dialogue_revision,
                alignment_report_id=alignment_report_id),
            observed="; ".join(invalid),
            measured=_measured(len(invalid), "invalid_renditions", 0, "lte"),
            evidence_id=evidence_id,
            actions=_default_actions("SUB.FORMAT.ENCODING"))
        defects.append(item)
        out.append(_evaluated("SUB.FORMAT.ENCODING",
                              [item["defect_fingerprint"]]))
    else:
        out.append(_evaluated("SUB.FORMAT.ENCODING", []))

    ass = rendered.get("ass")
    if ass is None:
        out.append(_na("SUB.FORMAT.ASS_STYLE_MISSING", "no_such_content"))
    else:
        missing = _ass_undefined_style(ass)
        item = None
        if missing:
            item = _mk(
                check_id="SUB.FORMAT.ASS_STYLE_MISSING", severity="blocker",
                scope="track", start=begin, end=end,
                expected=f"style {style_ref!r} is defined in [V4+ Styles]",
                expected_source=_expected_ref(
                    "SUB.FORMAT.ASS_STYLE_MISSING", dialogue_id=dialogue_id,
                    dialogue_revision=dialogue_revision,
                    alignment_report_id=alignment_report_id),
                observed=f"Dialogue events reference undefined style "
                         f"{missing!r}",
                measured=_measured(0, "defined_styles", 1, "gte"),
                evidence_id=evidence_id,
                actions=_default_actions("SUB.FORMAT.ASS_STYLE_MISSING"))
            defects.append(item)
        out.append(_evaluated("SUB.FORMAT.ASS_STYLE_MISSING",
                              [item["defect_fingerprint"]] if item else []))

    # BOM 是 info：本服务的渲染器从不写 BOM，所以这条永远 evaluated。
    # 之所以仍然评估而不 `not_applicable`，是因为"没 BOM"与"没查"在报告里
    # 必须可区分（S39 会把 not_applicable 排进 abstain 的候选）。
    bom = [fmt for fmt in sorted(rendered)
           if rendered[fmt].startswith("\ufeff")]
    bom_defects = [_mk(
        check_id="SUB.FORMAT.BOM", severity="info", scope="track",
        start=begin, end=end, expected="no byte-order mark in the rendition",
        expected_source=_expected_ref(
            "SUB.FORMAT.BOM", dialogue_id=dialogue_id,
            dialogue_revision=dialogue_revision,
            alignment_report_id=alignment_report_id),
        observed=f"{fmt} rendition starts with a BOM",
        measured=_measured(1, "bom_renditions", 0, "lte"),
        evidence_id=evidence_id, actions=_default_actions("SUB.FORMAT.BOM"))
        for fmt in bom]
    defects.extend(bom_defects)
    out.append(_evaluated("SUB.FORMAT.BOM",
                          [d["defect_fingerprint"] for d in bom_defects]))
    return out


_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


def _srt_problem(text: str) -> str | None:
    blocks = [block for block in text.strip().split("\n\n") if block.strip()]
    for ordinal, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 2:
            return f"block {ordinal} has no time line"
        if not lines[0].strip().isdigit() or int(lines[0]) != ordinal:
            return f"block {ordinal} index is {lines[0]!r}"
        if not _SRT_TIME.match(lines[1].strip()):
            return f"block {ordinal} time line is {lines[1]!r}"
        if not any(line.strip() for line in lines[2:]):
            return f"block {ordinal} has no text"
    return None


def _ass_undefined_style(text: str) -> str | None:
    defined = set()
    used = set()
    for line in text.splitlines():
        if line.startswith("Style:"):
            defined.add(line[len("Style:"):].split(",", 1)[0].strip())
        elif line.startswith("Dialogue:"):
            parts = line[len("Dialogue:"):].split(",")
            if len(parts) > 3:
                used.add(parts[3].strip())
    missing = sorted(used - defined)
    return missing[0] if missing else None


def evaluate(*, chosen, cues, planned, duration, alignment_ok,
             timeline_has_clips, alignment_report_id, dialogue_id,
             dialogue_revision, profile, formats, rendered, source_locale,
             target_locale, expected_source_revision, lexicon_present,
             style_known, style_ref, evidence_id) -> dict:
    """跑选中的检查，返回 manifest / defects / observations / coverage。

    覆盖率的分子是**真正落了 cue 的段数**，分母是切分计划的段数。计划里
    有、但没配上时间码的段进不了任何检查，于是 ratio < 1——这正是"有些台词
    没配上"的可观测形态（E25-U4）。分子分母都由 `plan_cues` 产出，不是
    事后估算。
    """
    defects: list[dict] = []
    manifest: list[dict] = []
    track_start = min((c["start_seconds"] for c in cues), default=0.0)
    track_end = max((c["end_seconds"] for c in cues), default=0.0)

    groups = {
        "SUB.TIMING.": lambda: _timing(
            defects, cues, duration=duration, alignment_ok=alignment_ok,
            timeline_has_clips=timeline_has_clips,
            alignment_report_id=alignment_report_id,
            dialogue_id=dialogue_id, dialogue_revision=dialogue_revision,
            evidence_id=evidence_id, profile=profile),
        "SUB.LAYOUT.": lambda: _layout_checks(
            defects, cues, profile=profile, formats=formats,
            style_ref=style_ref, style_known=style_known,
            dialogue_id=dialogue_id, dialogue_revision=dialogue_revision,
            alignment_report_id=alignment_report_id,
            evidence_id=evidence_id),
        "SUB.TEXT.": lambda: _text_checks(
            defects, cues, expected_source_revision=expected_source_revision,
            source_locale=source_locale, target_locale=target_locale,
            lexicon_present=lexicon_present, profile=profile,
            dialogue_id=dialogue_id, dialogue_revision=dialogue_revision,
            alignment_report_id=alignment_report_id,
            evidence_id=evidence_id),
        "SUB.FORMAT.": lambda: _format_checks(
            defects, rendered=rendered, formats=formats, style_ref=style_ref,
            track_start=track_start, track_end=track_end,
            dialogue_id=dialogue_id, dialogue_revision=dialogue_revision,
            alignment_report_id=alignment_report_id,
            evidence_id=evidence_id),
    }
    observations: list[dict] = []
    for prefix, run in groups.items():
        if not any(check_id.startswith(prefix) for check_id in chosen):
            continue
        product = run()
        if prefix == "SUB.TEXT.":
            entries, obs = product
            observations.extend(obs)
        else:
            entries = product
        manifest.extend(entry for entry in entries
                        if entry["check_id"] in chosen)
    order = {check_id: index for index, check_id in enumerate(chosen)}
    manifest.sort(key=lambda entry: order[entry["check_id"]])
    # defect 的顺序也跟 manifest 一致，报告可逐字节复现。
    defects.sort(key=lambda defect: (order.get(defect["check_id"], 0),
                                     defect["defect_fingerprint"]))
    coverage = _coverage(duration=duration, planned=planned,
                         evaluated=len(cues),
                         required_min=float(
                             profile.get("required_coverage_min", 0.95)))
    return {"manifest": manifest, "defects": defects,
            "observations": observations, "coverage": coverage}


# ---------------------------------------------------------------- 配置解析

#: locale 前缀 → CPS/行长上限。不认识的语言按拉丁文口径，不按中文——
#: 给一个日文 8 CPS 的上限去卡英文字幕会造出成片的假 major 缺陷。
_LOCALE_FAMILY = {"zh": "zh", "ja": "ja", "en": "en"}


def locale_family(locale: str) -> str:
    return _LOCALE_FAMILY.get(str(locale).split("-")[0].lower(), "en")


def _profile(body: dict, *, target_locale: str) -> dict:
    """命令 profile 只能**收紧**默认值（C5）。

    每个上限取 `min(命令值, 方案默认值)`：调用方可以把 9 CPS 收到 7，
    不能放宽到 20。放行一个更宽的值等于让命令替方案改验收标准。
    """
    family = locale_family(target_locale)
    default_cps = schema.MAX_CPS[family]
    default_chars = schema.MAX_CHARS_PER_LINE[family]
    raw = body.get("profile")
    raw = raw if isinstance(raw, dict) else {}

    def _cap(value, ceiling):
        value = _pos(value, ceiling)
        return min(value, ceiling) if value > 0 else ceiling

    safe = raw.get("safe_area") if isinstance(raw.get("safe_area"), dict) else {}
    style = raw.get("style_template_ref")
    style = style if isinstance(style, dict) else {}
    formats = raw.get("format") or list(schema.FORMATS)
    if not isinstance(formats, (list, tuple)) or not formats:
        raise ServiceError(422, "INVALID_PROFILE",
                           "profile.format must be a non-empty list")
    unknown = [f for f in formats if f not in schema.FORMATS]
    if unknown:
        raise ServiceError(422, "INVALID_PROFILE",
                           f"unknown format(s) {sorted(unknown)}; "
                           f"allowed {list(schema.FORMATS)}")
    fonts = raw.get("fonts_available")
    frame_rate = raw.get("frame_rate")
    if frame_rate is not None:
        if not (isinstance(frame_rate, (list, tuple)) and len(frame_rate) == 2
                and all(isinstance(x, int) and x > 0 for x in frame_rate)):
            raise ServiceError(422, "INVALID_PROFILE",
                               "profile.frame_rate must be [num, den]")
        frame_rate = [int(frame_rate[0]), int(frame_rate[1])]
    return {
        "max_cps": int(_cap(raw.get("max_cps"), default_cps) or default_cps),
        "max_chars_per_line": int(_cap(raw.get("max_chars_per_line"),
                                       default_chars) or default_chars),
        "max_lines": int(_cap(raw.get("max_lines"), schema.MAX_LINES)
                         or schema.MAX_LINES),
        "required_coverage_min": min(
            1.0, max(0.0, _pos(raw.get("required_coverage_min"), 0.95))),
        "safe_area": {
            "bottom_pct": min(1.0, max(0.0, _pos(safe.get("bottom_pct"), 0.10))),
            "side_pct": min(0.5, max(0.0, _pos(safe.get("side_pct"), 0.05))),
        },
        "line_height_pct": min(0.5, max(0.005, _pos(
            raw.get("line_height_pct"), DEFAULT_PROFILE["line_height_pct"]))),
        "style_template_ref": {
            "id": str(style.get("id") or "tpl_sub_default"),
            "revision": max(1, int(_pos(style.get("revision"), 1))),
        },
        "style_ref": str(style.get("id") or "tpl_sub_default"),
        "font": str(raw.get("font") or DEFAULT_PROFILE["font"]),
        "font_size": int(raw.get("font_size") or 48),
        "fonts_available": ([str(f) for f in fonts]
                            if isinstance(fonts, (list, tuple)) and fonts
                            else None),
        "frame_rate": frame_rate,
        "format": list(dict.fromkeys(str(f) for f in formats)),
    }


def _require_dict(value, name):
    if not isinstance(value, dict):
        raise ServiceError(422, "INVALID_SUBTITLE_JOB", f"{name} is required")
    return value


def _validate_context(tenant_id: str, body: dict, context) -> None:
    """签名上下文的校验。租户只来自平台，body 只用于比对（AGENTS.md）。"""
    context = _require_dict(context, "execution_context")
    if context.get("tenant_id") != tenant_id:
        raise ServiceError(403, "TENANT_MISMATCH",
                           "execution_context.tenant_id does not match the "
                           "credential")
    missing = [k for k in REQUIRED_CONTEXT
               if context.get(k) is None and k not in NULLABLE_CONTEXT]
    for key in ("policy_ref", "manifest_ref"):
        ref = context.get(key)
        if isinstance(ref, dict):
            missing += [f"{key}.{sub}" for sub in
                        ("id", "revision", "content_hash")
                        if ref.get(sub) is None]
        else:
            missing.append(key)
    actor = context.get("actor")
    if isinstance(actor, dict):
        missing += [f"actor.{sub}" for sub in
                    ("principal_id", "principal_type", "delegation_id")
                    if actor.get(sub) is None]
    else:
        missing.append("actor")
    missing += [f"{key}" for key in REQUIRED_TOP_LEVEL if body.get(key) is None]
    for key, service_id in (("timeline_ref", "S41"), ("alignment_ref", "S37"),
                            ("dialogue_ref", "S06")):
        ref = body.get(key)
        if not (isinstance(ref, dict) and ref.get("id")):
            missing.append(f"{key}.id")
            continue
        if ref.get("revision") is None:
            missing.append(f"{key}.revision")
        declared = ref.get("service_id")
        if declared is not None and declared != service_id:
            # 一个写着 S39 的 alignment_ref 会让 expected_source 指向错的服务。
            raise ServiceError(422, "INVALID_SUBTITLE_JOB",
                               f"{key}.service_id must be {service_id!r}, "
                               f"got {declared!r}")
    if not body.get("target_locale"):
        missing.append("target_locale")
    if missing:
        raise ServiceError(
            422, "INVALID_SUBTITLE_JOB",
            f"missing {sorted(set(missing))}; these are the coordinates the "
            f"track and its report are recorded against")


# ---------------------------------------------------------------- 报告装配

def report_content_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "report_content_hash"}
    return _sha256_hex(_jcs(body))


def build_payload(*, report_id, generation, tenant_id, context, body,
                  track_id, subtitle_revision, duration, artifacts,
                  cue_count, stale_translation_count, manifest, defects,
                  coverage, decided, confidence, observations,
                  attempt_slot_id, profile, target_locale, source_locale,
                  cue_rows, supersedes_ref=None):
    """QCReport v2（`qc_layer="subtitle"`）。

    `subject.artifact_id` 取**本 revision 的第一个已归档 format** 的
    artifact_id：字幕轨没有视频那样的单一媒体制品，三种格式都是渲染结果，
    报告的主体是轨道本身而 S28 的 id 只是它的代表。一个都没归档时退到
    track_id（C16：不编造 `art_` id）。
    """
    policy_ref = (context.get("policy_ref") or {})
    manifest_ref = (context.get("manifest_ref") or {})
    representative = artifacts[0]["artifact_id"] if artifacts else track_id
    subject_hash = (artifacts[0]["content_hash"] if artifacts
                    else _sha256_hex(_jcs({
                        "track_id": track_id,
                        "subtitle_revision": subtitle_revision,
                        "cues": [{k: cue[k] for k in (
                            "cue_id", "start_seconds", "end_seconds",
                            "display_text")} for cue in cue_rows],
                    })))

    # `qc_report` 的主键是 report_id 本身（同一 id 只能有一代），所以
    # `supersedes` 指向**上一份报告的 id**，不能写自己的 id 再自称上一代。
    supersedes = None
    if isinstance(supersedes_ref, dict) and supersedes_ref.get("report_id"):
        supersedes = {"report_id": str(supersedes_ref["report_id"]),
                      "report_generation": max(1, int(_pos(
                          supersedes_ref.get("report_generation"), 1)))}
    repair_context = None
    raw_repair = body.get("repair_context")
    if isinstance(raw_repair, dict):
        repair_context = {
            "repair_plan_id": str(raw_repair.get("repair_plan_id") or
                                  "unknown"),
            "attempt_slot_id": str(raw_repair.get("attempt_slot_id") or
                                   "unknown"),
            "attempt_seq": max(1, int(_pos(raw_repair.get("attempt_seq"), 1))),
            "defect_fingerprints": [str(x) for x in
                                    (raw_repair.get("defect_fingerprints")
                                     or [])],
        }

    group_defects = []
    for index, defect in enumerate(defects):
        item = dict(defect)
        item["defect_id"] = defect_id_for(defect["defect_fingerprint"], index)
        group_defects.append(item)

    # manifest 的条目只带契约允许的三个键（`additionalProperties:false`）。
    manifest_out = [{"check_id": entry["check_id"],
                     "status": entry["status"],
                     "reason_code": entry["reason_code"]}
                    for entry in manifest]

    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "report_generation": generation,
        "qc_layer": QC_LAYER,
        "context": {
            "tenant_id": tenant_id,
            "project_id": str(context.get("project_id")),
            "series_id": str(context.get("series_id")),
            "branch_id": str(context.get("branch_id")),
            "episode_id": str(context.get("episode_id")),
            "home_cell": str(context.get("home_cell") or "unknown"),
            "manifest_ref": {
                "id": str(manifest_ref.get("id") or "manifest"),
                "revision": max(1, int(_pos(manifest_ref.get("revision"), 1))),
                "content_hash": _hex64(manifest_ref.get("content_hash"))
                or "0" * 64,
            },
            "run_id": str(body.get("run_id")),
            "execution_fence": max(1, int(_pos(
                context.get("execution_fence"), 1))),
            "shot_lineage_id": str(body.get("shot_lineage_id")),
            # qc-report.v2 把 policy_ref.service_id 钉成 "S02"。命令给的
            # policy 是 S02 的策略文档，这里只重写这一个字段。
            "policy_ref": {
                "service_id": "S02",
                "id": str(policy_ref.get("id") or "policy"),
                "revision": max(1, int(_pos(policy_ref.get("revision"), 1))),
                "content_hash": _hex64(policy_ref.get("content_hash"))
                or "0" * 64,
            },
            "trace_id": str(context.get("trace_id") or ""),
        },
        "subject": {
            "artifact_id": str(representative),
            "artifact_sha256": subject_hash,
            "artifact_kind": "subtitle_track",
            # §6：字幕没有画面与音轨。null 不是"没测"，是"这一层没有"。
            "media": {"duration_seconds": round(max(0.0, float(duration)), 6),
                      "video": None, "audio": None},
        },
        # C7：报告是整轨级。用 track 自身填 shot_ref，并在 dimensions 里
        # 写明，不冒充镜头级结论。
        "shot_ref": {
            "shot_id": track_id,
            "shot_revision": max(1, int(subtitle_revision)),
            "logical_node_id": str(context.get("node_id") or track_id),
            "node_epoch": max(1, int(_pos(context.get("node_epoch"), 1))),
        },
        "evaluator": {
            "name": EVALUATOR_NAME,
            "version": EVALUATOR_VERSION,
            "image_digest": EVALUATOR_IMAGE_DIGEST,
            "model_snapshot": None,
        },
        "policy_version": str(policy_ref.get("revision") or "unknown"),
        "sampling_policy_version": schema.SAMPLING_VERSION,
        "check_manifest": manifest_out,
        "defects": group_defects,
        "dimensions": build_dimensions(manifest=manifest, defects=group_defects,
                                       observations=observations),
        "coverage": coverage,
        "decision": decided["decision"],
        "decision_reason": decided["decision_reason"],
        "abstain": decided["abstain"],
        "confidence": confidence,
        "supersedes": supersedes,
        "repair_context": repair_context,
        "evidence_bundle_refs": _evidence_refs(
            artifacts=artifacts, alignment_ref=body.get("alignment_ref") or {},
            dialogue_ref=body.get("dialogue_ref") or {}),
        "created_at": _now(),
    }
    # `qc-report.v2` 的 23 个必填键里有它。摘要**不含自己**，所以先装配
    # 再算，顺序不能换；算完写回才能让 S39 校验报告未被篡改。
    payload["report_content_hash"] = report_content_hash(payload)
    return payload


def _evidence_refs(*, artifacts, alignment_ref, dialogue_ref) -> list[dict]:
    """证据包引用。`minItems 1`。

    优先引用已归档的 rendition（它们才是"渲染后的轨道"）；一个都没有时
    退到 S37 的对齐报告（时间码缺陷的原始证据）与 S06 的台词 revision。
    两者都是真实存在的引用，不是编出来的占位。
    """
    refs = []
    for artifact in artifacts:
        refs.append({"service_id": "S28", "id": artifact["artifact_id"],
                     "revision": 1,
                     "content_hash": artifact["content_hash"]})
    if alignment_ref.get("id"):
        refs.append({
            "service_id": "S37", "id": str(alignment_ref["id"]),
            "revision": max(1, int(_pos(alignment_ref.get("revision"), 1))),
            "content_hash": _hex64(alignment_ref.get("content_hash"))
            or "0" * 64,
        })
    if dialogue_ref.get("id"):
        refs.append({
            "service_id": "S06", "id": str(dialogue_ref["id"]),
            "revision": max(1, int(_pos(dialogue_ref.get("revision"), 1))),
            "content_hash": _hex64(dialogue_ref.get("content_hash"))
            or "0" * 64,
        })
    if not refs:
        # 走不到这里：命令必须带 alignment_ref / dialogue_ref。留一处显式
        # 兜底是为了在契约变化时得到 422 而不是空数组。
        raise ServiceError(422, "INVALID_SUBTITLE_JOB",
                           "no evidence reference is available for the report")
    return refs


#: 决策 → track 状态。**没有任何一条通向 `accepted` / `qc_passed`**（C15）。
DECISION_STATE = {
    "pass": "archived",
    "repair": "repair_pending",
    "human_review": "repair_pending",
    # 弃权 = 有字节但没得出结论。E25-U1 要求"仍出草稿"，所以停在 rendered。
    "abstain": "rendered",
}


#: 命令体里的本地化入口（C14）。只收 draft / reviewed。
def _translation_entries(body: dict) -> list[dict]:
    localization = body.get("localization")
    if localization is None:
        return []
    if not isinstance(localization, dict):
        raise ServiceError(422, "INVALID_SUBTITLE_JOB",
                           "localization must be an object")
    raw = localization.get("translations") or []
    if not isinstance(raw, list):
        raise ServiceError(422, "INVALID_SUBTITLE_JOB",
                           "localization.translations must be a list")
    return [item for item in raw if isinstance(item, dict)]


def _line_texts(item: dict) -> dict[str, str]:
    texts = {}
    for line in item.get("lines") or []:
        if not isinstance(line, dict):
            continue
        line_id = line.get("line_id")
        if line_id is None:
            continue
        texts[str(line_id)] = str(line.get("text") or "")
    return texts


# ---------------------------------------------------------------- 服务

class SubtitleService:
    """S38 的写路径。**没有字节入口，也没有 accepted 入口。**

    构造顺序不能改：`executescript(SCHEMA)` 与 `Outbox.__init__` 都含
    `executescript`，而它会隐式提交。两者必须在任何 `BEGIN IMMEDIATE`
    之前跑完，否则命令事务会被悄悄提交、ROLLBACK 失效。
    """

    def __init__(self, conn: sqlite3.Connection, *, alignment_source,
                 timeline_source, dialogue_source, lexicon_source=None,
                 archive_source=None, artifact_url_source=None,
                 translation_source=None, enabled: bool = False,
                 enabled_checks=()):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(schema.SCHEMA)
        self.outbox = Outbox(self.conn)
        # 平台 SDK 的 Inbox 只写 (consumer, event_id, received_at)，而本服务
        # 的 inbox 表多了 tenant/sequence 三列，同名不同形。**不用平台类**，
        # 否则 CREATE TABLE IF NOT EXISTS 会静默跳过本服务的 DDL。
        self.alignment_source = alignment_source
        self.timeline_source = timeline_source
        self.dialogue_source = dialogue_source
        self.lexicon_source = lexicon_source
        self.archive_source = archive_source
        self.artifact_url_source = artifact_url_source
        # 存在但从不调用：stale 译文不自动重译（付费 S26，需人验收）。
        self.translation_source = translation_source
        self.enabled = bool(enabled)
        unknown = [c for c in enabled_checks if c not in schema.CHECK_IDS]
        if unknown:
            raise ValueError("unknown check_id(s) in S38_CHECKS: "
                             f"{sorted(unknown)}")
        self.enabled_checks = tuple(enabled_checks) or tuple(schema.CHECK_IDS)
        self.metrics: dict[str, int] = {}

    # ------------------------------------------------------------ 工具
    def _bump(self, name: str, amount: int = 1) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + amount

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str):
        """上游不可达统一 503，**绝不用缓存结论兜底**。"""
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

    # ------------------------------------------------------------ 上游读取
    def _alignment(self, tenant_id: str, ref: dict) -> dict:
        result = self._upstream(
            lambda: self.alignment_source(tenant_id, ref, "latest"),
            "ALIGNMENT_UNAVAILABLE")
        if not isinstance(result, dict):
            raise ServiceError(503, "ALIGNMENT_UNAVAILABLE",
                               "S37 returned no alignment document",
                               retryable=True)
        report = result.get("report") if isinstance(result.get("report"),
                                                    dict) else {}
        inner = report.get("payload") if isinstance(report.get("payload"),
                                                    dict) else report
        units_raw = result.get("units")
        units = (units_raw.get("units") if isinstance(units_raw, dict)
                 else units_raw) or []
        units = [u for u in units if isinstance(u, dict)]
        decision = str(inner.get("decision") or report.get("decision") or "")
        if not units and decision in ("", "abstain"):
            # S37 弃权时仍然给这一点：它没有时间码，S38 不能编。E25-U1 要的
            # 是"仍出草稿"，但草稿只能来自 units；没有 units 就没有草稿，
            # 这是 422 而不是"空字幕通过"。
            raise ServiceError(
                422, "NO_ALIGNMENT_UNITS",
                "the S37 report carries no alignment units; S38 will not "
                "invent word timecodes (plan §4.1)")
        return {"report": inner, "units": units,
                "decision": decision or "unknown"}
    def _timeline(self, tenant_id: str, ref: dict) -> dict:
        result = self._upstream(
            lambda: self.timeline_source(tenant_id, ref),
            "TIMELINE_UNAVAILABLE")
        if not isinstance(result, dict):
            raise ServiceError(503, "TIMELINE_UNAVAILABLE",
                               "S41 returned no timeline revision",
                               retryable=True)
        tick_rate = result.get("tick_rate")
        if not isinstance(tick_rate, int) or tick_rate <= 0:
            # PTR-2：tick_rate 不在事件里，读不到就不排时码。假定 90000 会让
            # 整条轨道的时间码按一个不存在的刻度算出来。
            raise ServiceError(
                503, "TIMELINE_UNAVAILABLE",
                "S41 revision has no tick_rate; S38 will not assume one "
                "(PTR-2)", retryable=True)
        clips = [c for c in (result.get("clips") or []) if isinstance(c, dict)]
        duration_ticks = result.get("duration_ticks")
        duration = (float(duration_ticks) / tick_rate
                    if _finite(duration_ticks) and duration_ticks > 0 else None)
        return {"timeline_id": str(result.get("timeline_id") or ref["id"]),
                "revision": int(result.get("revision") or ref.get("revision")
                                or 1),
                "tick_rate": tick_rate, "clips": clips,
                "duration": duration,
                "completeness": result.get("completeness")}

    def _dialogue(self, tenant_id: str, ref: dict) -> dict:
        result = self._upstream(
            lambda: self.dialogue_source(tenant_id, ref),
            "DIALOGUE_UNAVAILABLE")
        if not isinstance(result, dict):
            raise ServiceError(503, "DIALOGUE_UNAVAILABLE",
                               "S06 returned no dialogue revision",
                               retryable=True)
        lines = [dict(line) for line in (result.get("lines") or [])
                 if isinstance(line, dict) and str(line.get("text")
                                                   or "").strip()]
        return {"dialogue_id": str(result.get("script_id") or ref["id"]),
                "revision": int(result.get("revision")
                                or ref.get("revision") or 1),
                "lines": lines}

    def _lexicon(self, tenant_id: str, ref):
        """词典缺席**不是命令失败**：E25-U3 要求它就是
        `not_evaluated(dependency_unavailable)`，报告 abstain。

        S15 不可达时若抛 503，命令会被上游整条重投；而重投只会再失败一次。
        缺席的诚实形态是"这次没判得了"——`lexicon_present=False` 让
        `SUB.TEXT.LEXICON_UNUSED` 记 `dependency_unavailable`，判定转
        abstain。4xx（我们发错了 ref）仍然照抛。
        """
        if not ref:
            return None
        if not isinstance(ref, dict) or not ref.get("id"):
            raise ServiceError(422, "INVALID_SUBTITLE_JOB",
                               "lexicon_ref must be an object with an id")
        if self.lexicon_source is None:
            return None
        try:
            return self.lexicon_source(tenant_id, ref)
        except ConnectionError:
            return None
        except ServiceError as exc:
            if exc.status >= 500:
                return None
            raise

    # ------------------------------------------------------------ 命令
    def check(self, tenant_id: str, command_id: str, body: dict,
              request_hash: str) -> dict:
        """`POST /subtitle-jobs`：生成 + 自校验 + 归档 + 事件。"""
        if not self.enabled:
            # 方案 S38-C13：关闭时命令 403。只读面不因开关而关闭——审计
            # 可见性不该被一个生产开关收回。
            raise ServiceError(
                403, "SERVICE_DISABLED",
                "S38_ENABLED is not set; S38 will not produce a subtitle "
                "track (S38-C13)")
        context = body.get("execution_context")
        _validate_context(tenant_id, body, context)

        try:
            chosen = schema.select((body.get("checks") or {}).get("include"),
                                   (body.get("checks") or {}).get("exclude"))
        except ValueError as exc:
            raise ServiceError(422, "INVALID_CHECKS", str(exc)) from exc
        chosen = [c for c in chosen if c in self.enabled_checks]
        if not chosen:
            raise ServiceError(
                422, "NO_CHECKS_ENABLED",
                "checks.include selects no check that S38_CHECKS enables; "
                "refusing to emit an empty manifest, because that reads as a "
                "pass without measuring anything")

        target_locale = str(body["target_locale"])
        source_locale = str(body.get("source_locale") or target_locale)
        profile = _profile(body, target_locale=target_locale)

        alignment = self._alignment(tenant_id, body["alignment_ref"])
        timeline = self._timeline(tenant_id, body["timeline_ref"])
        dialogue = self._dialogue(tenant_id, body["dialogue_ref"])
        lexicon = self._lexicon(tenant_id, body.get("lexicon_ref"))
        if not dialogue["lines"]:
            raise ServiceError(
                422, "NO_DIALOGUE",
                "S06 returned no non-empty dialogue lines; S38 will not "
                "render an empty subtitle track")

        track_id = track_id_for(
            tenant_id=tenant_id, timeline_id=timeline["timeline_id"],
            target_locale=target_locale, source_locale=source_locale,
            dialogue_id=dialogue["dialogue_id"])
        previous = _row(self.conn.execute(
            "SELECT * FROM track WHERE tenant_id=? AND track_id=?",
            (tenant_id, track_id)).fetchone())
        subtitle_revision = (int(previous["subtitle_revision"]) + 1
                             if previous else 1)

        translations = self._translations(tenant_id, body, dialogue)
        duration = timeline["duration"]
        if duration is None:
            duration = max((float(u.get("ci_high") or u.get("end") or 0.0)
                            for u in alignment["units"]), default=0.0)
        spans = clip_map(timeline["clips"], timeline["tick_rate"])

        plan = plan_cues(
            track_id=track_id, lines=dialogue["lines"],
            units=alignment["units"], spans=spans, duration=duration,
            profile=profile, translations=translations, lexicon=lexicon,
            dialogue_revision=dialogue["revision"],
            style_ref=profile["style_ref"],
            node_epoch=max(1, int(_pos(context.get("node_epoch"), 1))),
            alignment_report_id=str(body["alignment_ref"]["id"]))
        cues = plan["cues"]
        if not cues:
            raise ServiceError(
                422, "NO_PLACEABLE_CUES",
                "no script line could be bound to an alignment unit; a "
                "subtitle track with zero cues cannot satisfy any check")

        return self._finish(
            tenant_id=tenant_id, command_id=command_id, body=body,
            context=context, track_id=track_id,
            subtitle_revision=subtitle_revision,
            previous=previous, plan=plan, cues=cues, timeline=timeline,
            dialogue=dialogue, lexicon=lexicon, profile=profile,
            target_locale=target_locale, source_locale=source_locale,
            duration=duration, chosen=chosen,
            alignment_ok=(alignment["decision"] == "pass"),
            alignment_report_id=str(body["alignment_ref"]["id"]),
            translations=translations, request_hash=request_hash)

    # ------------------------------------------------------------ 译文入口
    def _translations(self, tenant_id: str, body: dict,
                      dialogue: dict) -> dict:
        """C14：命令体里的译文。同 id+revision 不同内容 → 409，不覆盖。"""
        entries = _translation_entries(body)
        if not entries:
            return {}
        source_revision = dialogue["revision"]
        source_hash = _sha256_hex(_jcs(
            [{"line_id": str(line.get("line_id")), "text": line.get("text")}
             for line in dialogue["lines"]]))
        out: dict[str, dict] = {}
        for item in entries:
            translation_id = item.get("translation_id")
            if not translation_id:
                raise ServiceError(
                    422, "INVALID_SUBTITLE_JOB",
                    "localization.translations[].translation_id is required")
            revision = int(_pos(item.get("revision"), 0))
            if revision < 1:
                raise ServiceError(
                    422, "INVALID_SUBTITLE_JOB",
                    "localization.translations[].revision must be >= 1")
            status = str(item.get("status") or "draft")
            if status not in schema.COMMAND_TRANSLATION_STATUSES:
                # C6：accepted 不能由命令声明，那等于调用方自证审核通过。
                raise ServiceError(
                    422, "INVALID_TRANSLATION_STATUS",
                    f"translation status {status!r} cannot be set by a "
                    f"command; allowed "
                    f"{list(schema.COMMAND_TRANSLATION_STATUSES)} (S38-C6)")
            texts = _line_texts(item)
            if not texts:
                raise ServiceError(
                    422, "INVALID_SUBTITLE_JOB",
                    "localization.translations[].lines is empty; an empty "
                    "translation is not a translation")
            content_hash = _sha256_hex(_jcs(texts))
            existing = _row(self.conn.execute(
                "SELECT content_hash, status FROM translation"
                " WHERE translation_id=? AND revision=?",
                (str(translation_id), revision)).fetchone())
            if existing is not None and existing["content_hash"] != content_hash:
                # 同 id+revision 不同内容 = 有人改了历史。覆盖会让已引用的
                # cue 指向一份不存在的译文。
                raise ServiceError(
                    409, "TRANSLATION_REVISION_CONFLICT",
                    f"translation {translation_id}@{revision} already exists "
                    f"with a different content_hash")
            if existing is not None:
                status = str(existing["status"])
            entry = {
                "translation_id": str(translation_id), "revision": revision,
                "target_locale": str(item.get("target_locale")
                                     or body["target_locale"]),
                "source_text_revision": source_revision,
                "source_text_hash": source_hash,
                "content_hash": content_hash, "status": status,
                "translated_by": item.get("translated_by"),
                "reviewer": item.get("reviewer"),
                "texts": texts, "fresh": existing is None,
            }
            for line_id in texts:
                out[line_id] = entry
        return out


    # ------------------------------------------------------------ 收尾
    def _finish(self, *, tenant_id, command_id, body, context, track_id,
                subtitle_revision, previous, plan, cues, timeline, dialogue,
                lexicon, profile, target_locale, source_locale, duration,
                chosen, alignment_ok, alignment_report_id, translations,
                request_hash) -> dict:
        """渲染 → 归档 → 自校验 → 落库 → 事件。全部在同一个命令事务里。

        归档失败**不是命令失败**：字节已经渲染出来了，只是没地方放。轨道停在
        `rendered_pending_archive`，不发 `post.subtitle.ready`（§3.0 / E25-U5）。
        把"归档失败"报成 500 会让上游重投整条命令，而重投会再生成一个
        `subtitle_revision`——同一条轨道被算了两遍。
        """
        formats = list(profile["format"])
        rendered = {
            fmt: render(fmt, cues, style_ref=profile["style_ref"],
                        font=profile["font"], profile=profile)
            for fmt in formats}
        blobs = {fmt: text.encode("utf-8") for fmt, text in rendered.items()}
        # `_sha256_hex` 也是先 encode('utf-8') 再算，两者对同一串字节等价。
        digests = {fmt: _sha256_hex(rendered[fmt]) for fmt in formats}
        sizes = {fmt: len(blobs[fmt]) for fmt in formats}

        artifacts: list[dict] = []
        pending: list[str] = []
        archive_errors: dict[str, str] = {}
        for fmt in formats:
            request = {
                "media_type": MEDIA_TYPES[fmt],
                "content_b64": base64.b64encode(blobs[fmt]).decode("ascii"),
                "content_hash": digests[fmt],
                "metadata": {"subtitle_track_id": track_id,
                             "subtitle_revision": subtitle_revision,
                             "format": fmt, "target_locale": target_locale},
            }
            try:
                if self.archive_source is None:
                    raise ServiceError(
                        503, "ARCHIVE_UNAVAILABLE",
                        "no archive source is wired; S38 holds no subtitle "
                        "bytes and cannot publish (plan §3.0)")
                stored = self.archive_source(tenant_id, request)
            except ConnectionError as exc:
                pending.append(fmt)
                archive_errors[fmt] = f"unreachable: {exc}"
                continue
            except ServiceError as exc:
                # 5xx = 上游暂时不行，留给重试；4xx = 我们发的东西不对，
                # 重试只会重复犯同一个错，直接抛。
                if exc.status >= 500:
                    pending.append(fmt)
                    archive_errors[fmt] = f"{exc.code}: {exc}"
                    continue
                raise
            if not isinstance(stored, dict):
                raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                                   "artifact-ingest did not return an object")
            artifact_id = stored.get("artifact_id")
            if not isinstance(artifact_id, str) \
                    or not artifact_id.startswith("art_"):
                # C9：不把任意字符串当 S28 指针。
                raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                                   "artifact_id is not in the S28 ID space",
                                   details={"artifact_id": artifact_id})
            content_hash = stored.get("content_hash")
            if content_hash != digests[fmt]:
                # S28 是字节权威。它说存下的字节是另一份，说明中途被改了；
                # 记下我们的摘要会让 rendition 指向一段我们没算过的内容。
                raise ServiceError(
                    502, "ARTIFACT_HASH_MISMATCH",
                    "artifact-ingest returned a content_hash that differs from "
                    "the bytes S38 rendered",
                    details={"format": fmt, "rendered_hash": digests[fmt],
                             "stored_hash": content_hash})
            byte_size = stored.get("byte_size")
            if not isinstance(byte_size, int) or byte_size <= 0:
                raise ServiceError(502, "ARCHIVE_BAD_RESPONSE",
                                   "artifact-ingest did not return byte_size",
                                   details={"byte_size": byte_size})
            artifacts.append({"format": fmt, "artifact_id": artifact_id,
                              "content_hash": content_hash,
                              "byte_size": int(byte_size)})

        stale_translation_count = sum(
            1 for cue in cues if cue.get("translation_status") == "stale")
        evaluated = evaluate(
            chosen=chosen, cues=cues, planned=plan["planned"],
            duration=duration, alignment_ok=alignment_ok,
            timeline_has_clips=bool(timeline["clips"]),
            alignment_report_id=alignment_report_id,
            dialogue_id=dialogue["dialogue_id"],
            dialogue_revision=dialogue["revision"], profile=profile,
            formats=formats, rendered=rendered, source_locale=source_locale,
            target_locale=target_locale,
            expected_source_revision=dialogue["revision"],
            lexicon_present=bool(lexicon),
            # C17：没有字体白名单就不判 FONT_MISSING 通过，也不判失败。
            style_known=profile.get("fonts_available") is not None,
            style_ref=profile["style_ref"],
            evidence_id=str(artifacts[0]["artifact_id"] if artifacts
                            else (alignment_report_id or track_id)))
        confidence = compute_confidence(manifest=evaluated["manifest"])
        decided = decide(manifest=evaluated["manifest"],
                         defects=evaluated["defects"],
                         coverage=evaluated["coverage"],
                         confidence=confidence)

        repair = body.get("repair_context")
        attempt_slot_id = (repair.get("attempt_slot_id")
                           if isinstance(repair, dict) else None)
        report_id = report_id_for(
            track_id=track_id, subtitle_revision=subtitle_revision,
            attempt_key=str(attempt_slot_id or "initial"))
        # 代次按 (track, subtitle_revision) 数，**按 report_id 数会撞主键**：
        # `qc_report.report_id` 是主键，同一 id 不能落两代。
        prior = _row(self.conn.execute(
            "SELECT report_id, report_generation FROM qc_report"
            " WHERE tenant_id=? AND track_id=? AND subtitle_revision=?"
            " ORDER BY report_generation DESC, created_at DESC LIMIT 1",
            (tenant_id, track_id, subtitle_revision)).fetchone())
        generation = 1 + (int(prior["report_generation"]) if prior else 0)

        payload = build_payload(
            report_id=report_id, generation=generation, tenant_id=tenant_id,
            context=context, body=body, track_id=track_id,
            subtitle_revision=subtitle_revision, duration=duration,
            artifacts=artifacts, cue_count=len(cues),
            stale_translation_count=stale_translation_count,
            manifest=evaluated["manifest"], defects=evaluated["defects"],
            coverage=evaluated["coverage"], decided=decided,
            confidence=confidence, observations=evaluated["observations"],
            attempt_slot_id=attempt_slot_id, profile=profile,
            target_locale=target_locale, source_locale=source_locale,
            cue_rows=cues, supersedes_ref=prior)
        digest = payload["report_content_hash"]

        state = ("rendered_pending_archive" if pending
                 else DECISION_STATE[decided["decision"]])
        result = {
            "track_id": track_id,
            "subtitle_revision": subtitle_revision,
            "state": state,
            "qc_report_id": report_id,
            "report_generation": generation,
            "qc_decision": decided["decision"],
            "decision_reason": decided["decision_reason"],
            "cue_count": len(cues),
            "formats": formats,
            "artifacts": [{"format": item["format"],
                           "artifact_id": item["artifact_id"],
                           "content_hash": item["content_hash"]}
                          for item in artifacts],
            "pending_formats": pending,
            "stale_translation_count": stale_translation_count,
            "content_hash": digest,
        }

        def _produce():
            self._persist(
                tenant_id=tenant_id, command_id=command_id, body=body,
                context=context, track_id=track_id,
                subtitle_revision=subtitle_revision, previous=previous,
                cues=cues, timeline=timeline, dialogue=dialogue,
                profile=profile, target_locale=target_locale,
                source_locale=source_locale, artifacts=artifacts,
                pending=pending, archive_errors=archive_errors,
                digests=digests, sizes=sizes, payload=payload, digest=digest,
                decided=decided, state=state,
                stale_translation_count=stale_translation_count,
                translations=translations, cue_count=len(cues))

        self._command(tenant_id, command_id, _produce)
        self._bump("subtitle_tracks_total")
        if pending:
            self._bump("archive_pending_total")
        return result

    def _persist(self, *, tenant_id, command_id, body, context, track_id,
                 subtitle_revision, previous, cues, timeline, dialogue,
                 profile, target_locale, source_locale, artifacts, pending,
                 archive_errors, digests, sizes, payload, digest, decided,
                 state,
                 stale_translation_count, translations, cue_count) -> None:
        """十一张表 + outbox，一个事务。

        `track.execution_fence` 与 `cue.node_epoch` 分别来自签名的
        execution_context：前者是 run/branch 的，后者是单个 logical_node 的。
        这里只赋值、从不比较，也不从一个推出另一个（AGENTS.md / C4）。
        """
        now = _now()
        fence = max(1, int(_pos(context.get("execution_fence"), 1)))
        epoch = max(1, int(_pos(context.get("node_epoch"), 1)))
        dialogue_hash = (_hex64((body.get("dialogue_ref") or {}).get(
            "content_hash")) or _sha256_hex(_jcs(dialogue["lines"])))
        alignment_hash = (_hex64((body.get("alignment_ref") or {}).get(
            "content_hash")) or "0" * 64)
        row = [
            ("track_id", track_id),
            ("tenant_id", tenant_id),
            ("branch_id", str(context.get("branch_id"))),
            ("episode_id", str(context.get("episode_id"))),
            ("timeline_id", timeline["timeline_id"]),
            ("timeline_revision", int(timeline["revision"])),
            ("target_locale", target_locale),
            ("source_locale", source_locale),
            ("subtitle_revision", int(subtitle_revision)),
            ("state", state),
            ("execution_fence", fence),
            ("node_epoch", epoch),
            ("profile_json", _jcs(profile)),
            ("alignment_report_id", str(body["alignment_ref"]["id"])),
            ("alignment_revision", max(1, int(_pos(
                (body.get("alignment_ref") or {}).get("revision"), 1)))),
            ("alignment_hash", alignment_hash),
            ("dialogue_id", dialogue["dialogue_id"]),
            ("dialogue_revision", int(dialogue["revision"])),
            ("dialogue_hash", dialogue_hash),
            ("qc_report_id", payload["report_id"]),
            ("qc_decision", decided["decision"]),
            ("supersedes_revision",
             int(previous["subtitle_revision"]) if previous else None),
            ("created_at", previous["created_at"] if previous else now),
            ("updated_at", now),
        ]
        columns = ", ".join(name for name, _ in row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{name}=excluded.{name}"
                            for name, _ in row if name not in
                            ("track_id", "created_at"))
        self.conn.execute(
            f"INSERT INTO track ({columns}) VALUES ({marks})"
            f" ON CONFLICT(track_id) DO UPDATE SET {updates}",
            tuple(value for _, value in row))

        # cue 是「当前 revision 的 cue 集」（§2.2 的 DDL 没有 revision 列），
        # 换 revision 时整组替换；旧 revision 的字节与报告在 rendition /
        # qc_report 里按 revision 各自留档。
        self.conn.execute("DELETE FROM cue WHERE track_id=?", (track_id,))
        for cue in cues:
            self.conn.execute(
                "INSERT INTO cue (track_id, cue_id, cue_index, clip_id,"
                " script_line_id, source_text_revision, source_text_hash,"
                " lexicon_ref_id, lexicon_revision, lexicon_hash,"
                " translation_id, translation_revision, translation_hash,"
                " display_text, start_seconds, end_seconds,"
                " alignment_report_id, align_unit_start, align_unit_end,"
                " node_epoch, line_count, max_chars_per_line, style_ref,"
                " safe_area_ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?)",
                (track_id, cue["cue_id"], int(cue["cue_index"]),
                 cue["clip_id"], cue["script_line_id"],
                 int(cue["source_text_revision"]), cue["source_text_hash"],
                 cue["lexicon_ref_id"], cue["lexicon_revision"],
                 cue["lexicon_hash"], cue["translation_id"],
                 cue["translation_revision"], cue["translation_hash"],
                 cue["display_text"], float(cue["start_seconds"]),
                 float(cue["end_seconds"]), cue["alignment_report_id"],
                 int(cue["align_unit_start"]), int(cue["align_unit_end"]),
                 int(cue["node_epoch"]), int(cue["line_count"]),
                 int(cue["max_chars_per_line"]), cue["style_ref"],
                 int(cue["safe_area_ok"])))

        # 译文只插不覆盖：同一 (id, revision) 的既有行是权威（可能是 reviewed
        # / accepted，甚至是 stale），命令体的 status 只是初次写入的值。
        seen = {}
        for entry in translations.values():
            seen[(entry["translation_id"], entry["revision"])] = entry
        for (translation_id, revision), entry in seen.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO translation (translation_id, revision,"
                " tenant_id, target_locale, source_text_revision,"
                " source_text_hash, content_hash, status, translated_by,"
                " reviewer, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (translation_id, int(revision), tenant_id,
                 entry["target_locale"], int(entry["source_text_revision"]),
                 entry["source_text_hash"], entry["content_hash"],
                 entry["status"],
                 _jcs(entry["translated_by"])
                 if entry["translated_by"] is not None else None,
                 _jcs(entry["reviewer"])
                 if entry["reviewer"] is not None else None, now))
        for cue in cues:
            if not cue.get("translation_id"):
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO cue_text (translation_id, revision,"
                " cue_id, text) VALUES (?,?,?,?)",
                (cue["translation_id"], int(cue["translation_revision"]),
                 cue["cue_id"], cue["display_text"]))

        # 字节归属：rendition 只有指针，pending_render 连指针都没有。
        self.conn.execute(
            "DELETE FROM rendition WHERE track_id=? AND subtitle_revision=?",
            (track_id, subtitle_revision))
        self.conn.execute(
            "DELETE FROM pending_render WHERE track_id=? AND subtitle_revision=?",
            (track_id, subtitle_revision))
        for item in artifacts:
            self.conn.execute(
                "INSERT INTO rendition (track_id, subtitle_revision, format,"
                " artifact_id, content_hash, byte_size, rendered_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (track_id, subtitle_revision, item["format"],
                 item["artifact_id"], item["content_hash"],
                 int(item["byte_size"]), now))
        for fmt in pending:
            self.conn.execute(
                "INSERT INTO pending_render (track_id, subtitle_revision,"
                " format, content_hash, byte_size, attempts, last_error,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (track_id, subtitle_revision, fmt, digests[fmt],
                 sizes[fmt], 0, archive_errors.get(fmt), now))

        # §3.1 的 EDL 差分基准：只是缓存，权威仍是 S41。
        self.conn.execute("DELETE FROM clip_ref WHERE track_id=?", (track_id,))
        tick_rate = int(timeline["tick_rate"])
        for clip in sorted(timeline["clips"],
                           key=lambda c: int(c.get("in_ticks") or 0)):
            in_ticks = int(clip.get("in_ticks") or 0)
            out_ticks = int(clip.get("out_ticks") or 0)
            if out_ticks <= in_ticks:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO clip_ref (track_id, clip_id, in_ticks,"
                " out_ticks, speed_ratio, tick_rate) VALUES (?,?,?,?,?,?)",
                (track_id, str(clip.get("clip_id") or ""), in_ticks,
                 out_ticks, float(clip.get("speed_ratio") or 1.0), tick_rate))

        self.conn.execute(
            "INSERT INTO qc_report (report_id, tenant_id, track_id,"
            " subtitle_revision, report_generation, decision,"
            " report_content_hash, payload_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (payload["report_id"], tenant_id, track_id, subtitle_revision,
             payload["report_generation"], decided["decision"], digest,
             _jcs(payload), payload["created_at"]))
        for entry in payload["check_manifest"]:
            self.conn.execute(
                "INSERT INTO qc_check (report_id, check_id, status,"
                " reason_code) VALUES (?,?,?,?)",
                (payload["report_id"], entry["check_id"], entry["status"],
                 entry["reason_code"]))
        for defect in payload["defects"]:
            self.conn.execute(
                "INSERT INTO qc_defect (report_id, defect_id,"
                " defect_fingerprint, check_id) VALUES (?,?,?,?)",
                (payload["report_id"], defect["defect_id"],
                 defect["defect_fingerprint"], defect["check_id"]))

        # §7：归档成功的轨道才发事件，且与事实同事务。
        if state in schema.READY_STATES and not pending:
            self._emit_ready(
                tenant_id=tenant_id, command_id=command_id,
                correlation_id=str(context.get("run_id") or command_id),
                home_cell=str(context.get("home_cell") or "unknown"),
                track_id=track_id, subtitle_revision=subtitle_revision,
                episode_id=str(context.get("episode_id")),
                branch_id=str(context.get("branch_id")),
                timeline_id=timeline["timeline_id"],
                timeline_revision=int(timeline["revision"]),
                target_locale=target_locale, cue_count=cue_count,
                artifacts=artifacts, report_id=payload["report_id"],
                report_generation=payload["report_generation"],
                report_digest=digest, decision=decided["decision"],
                defect_count=len(payload["defects"]),
                stale_translation_count=stale_translation_count)

    def _emit_ready(self, *, tenant_id, command_id, correlation_id, home_cell,
                    track_id, subtitle_revision, episode_id, branch_id,
                    timeline_id, timeline_revision, target_locale, cue_count,
                    artifacts, report_id, report_generation, report_digest,
                    decision, defect_count, stale_translation_count) -> str:
        """`post.subtitle.ready`。`aggregate_id = track_id`、序号 = revision（§2.2）。"""
        ordered = sorted(artifacts, key=lambda item: item["format"])
        data = {
            "schema_version": SCHEMA_VERSION,
            "track_id": track_id,
            "subtitle_revision": int(subtitle_revision),
            "episode_id": episode_id,
            "branch_id": branch_id,
            "timeline_ref": {"id": str(timeline_id),
                             "revision": int(timeline_revision)},
            "target_locale": target_locale,
            "cue_count": int(cue_count),
            "formats": [item["format"] for item in ordered],
            "artifacts": [{"format": item["format"],
                           "artifact_id": item["artifact_id"],
                           "content_hash": item["content_hash"]}
                          for item in ordered],
            "qc_report_ref": {"service_id": SOURCE, "id": report_id,
                              "revision": int(report_generation),
                              "content_hash": report_digest},
            "qc_decision": decision,
            "defect_count": int(defect_count),
            "stale_translation_count": int(stale_translation_count),
        }
        track_hash = _track_content_hash(ordered)
        self.outbox.append(new_event(
            source=SOURCE, type=schema.EVENT_READY, subject=track_id,
            tenant_id=tenant_id, home_cell=home_cell,
            aggregate_id=track_id, sequence=int(subtitle_revision),
            correlation_id=correlation_id, causation_id=command_id,
            data=data,
            data_ref={"service_id": SOURCE, "id": track_id,
                      "revision": int(subtitle_revision),
                      "content_hash": track_hash},
            schema_version=SCHEMA_VERSION))
        self._bump("subtitle_ready_events_total")
        return track_hash

    # ------------------------------------------------------------ 查询
    def get_track(self, tenant_id: str, track_id: str) -> dict:
        """跨租户与不存在同一个 404，不泄漏存在性。"""
        track = _row(self.conn.execute(
            "SELECT * FROM track WHERE tenant_id=? AND track_id=?",
            (tenant_id, track_id)).fetchone())
        if track is None:
            raise ServiceError(404, "TRACK_NOT_FOUND",
                               f"no subtitle track {track_id}")
        cues = [_cue_view(row) for row in self.conn.execute(
            "SELECT c.*, t.status AS translation_status FROM cue c"
            " LEFT JOIN translation t"
            "   ON t.translation_id = c.translation_id"
            "  AND t.revision = c.translation_revision"
            " WHERE c.track_id=? ORDER BY c.cue_index", (track_id,))]
        renditions = [_row(row) for row in self.conn.execute(
            "SELECT format, subtitle_revision, artifact_id, content_hash,"
            " byte_size, rendered_at FROM rendition WHERE track_id=?"
            " ORDER BY subtitle_revision, format", (track_id,))]
        pending = [_row(row) for row in self.conn.execute(
            "SELECT format, subtitle_revision, content_hash, byte_size,"
            " attempts, last_error, created_at FROM pending_render"
            " WHERE track_id=? ORDER BY subtitle_revision, format",
            (track_id,))]
        report = _row(self.conn.execute(
            "SELECT report_id, report_generation, decision,"
            " report_content_hash FROM qc_report WHERE tenant_id=?"
            " AND track_id=? ORDER BY report_generation DESC, created_at DESC"
            " LIMIT 1", (tenant_id, track_id)).fetchone())
        return {
            "track_id": track["track_id"],
            "tenant_id": track["tenant_id"],
            "subtitle_revision": track["subtitle_revision"],
            "state": track["state"],
            "branch_id": track["branch_id"],
            "episode_id": track["episode_id"],
            "timeline_ref": {"service_id": "S41", "id": track["timeline_id"],
                             "revision": track["timeline_revision"]},
            "target_locale": track["target_locale"],
            "source_locale": track["source_locale"],
            # 两列分别回显，不换算、不比较。
            "execution_fence": track["execution_fence"],
            "node_epoch": track["node_epoch"],
            "profile": _loads(track["profile_json"], {}),
            "alignment_ref": {"service_id": "S37",
                              "id": track["alignment_report_id"],
                              "revision": track["alignment_revision"],
                              "content_hash": track["alignment_hash"]},
            "dialogue_ref": {"service_id": "S06",
                             "id": track["dialogue_id"],
                             "revision": track["dialogue_revision"],
                             "content_hash": track["dialogue_hash"]},
            "cue_count": len(cues),
            "cues": cues,
            "renditions": renditions,
            "pending_formats": pending,
            "qc_report_ref": (None if report is None else {
                "service_id": SOURCE, "id": report["report_id"],
                "revision": report["report_generation"],
                "content_hash": report["report_content_hash"]}),
            "qc_decision": track["qc_decision"],
            "supersedes_revision": track["supersedes_revision"],
            "created_at": track["created_at"],
            "updated_at": track["updated_at"],
        }

    def get_rendition_url(self, tenant_id: str, track_id: str,
                          revision, fmt: str) -> dict:
        """签名 URL。**本服务从不返回字节**（§3.0）。"""
        track = _row(self.conn.execute(
            "SELECT track_id FROM track WHERE tenant_id=? AND track_id=?",
            (tenant_id, track_id)).fetchone())
        if track is None:
            raise ServiceError(404, "TRACK_NOT_FOUND",
                               f"no subtitle track {track_id}")
        try:
            number = int(revision)
        except (TypeError, ValueError):
            raise ServiceError(422, "INVALID_REVISION",
                               f"revision={revision!r} is not an integer")
        if fmt not in schema.FORMATS:
            raise ServiceError(422, "INVALID_FORMAT",
                               f"format={fmt!r} is not one of "
                               f"{list(schema.FORMATS)}")
        row = _row(self.conn.execute(
            "SELECT * FROM rendition WHERE track_id=? AND subtitle_revision=?"
            " AND format=?", (track_id, number, fmt)).fetchone())
        if row is None:
            waiting = _row(self.conn.execute(
                "SELECT attempts, last_error FROM pending_render"
                " WHERE track_id=? AND subtitle_revision=? AND format=?",
                (track_id, number, fmt)).fetchone())
            if waiting is not None:
                # 渲染好了但没归档：指针不存在，给 409 而不是 404，让调用方
                # 知道"再等等"和"根本没有这一版"是两件事。
                raise ServiceError(
                    409, "RENDITION_NOT_ARCHIVED",
                    f"{fmt} rendition of revision {number} is rendered but not "
                    f"archived yet", retryable=True,
                    details={"attempts": waiting["attempts"],
                             "last_error": waiting["last_error"]})
            raise ServiceError(404, "RENDITION_NOT_FOUND",
                               f"no {fmt} rendition for revision {number}")
        if self.artifact_url_source is None:
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "no artifact URL source is wired; S38 does not serve bytes",
                retryable=True)
        signed = self._upstream(
            lambda: self.artifact_url_source(tenant_id, row["artifact_id"]),
            "ARTIFACT_SOURCE_UNAVAILABLE")
        if not isinstance(signed, dict) or not signed.get("url"):
            raise ServiceError(502, "ARTIFACT_SOURCE_BAD_RESPONSE",
                               "artifact source returned no url")
        return {"track_id": track_id, "subtitle_revision": number,
                "format": fmt, "artifact_id": row["artifact_id"],
                "content_hash": row["content_hash"],
                "byte_size": row["byte_size"],
                "url": str(signed["url"]),
                "expires_at": signed.get("expires_at")}

    # ------------------------------------------------------------ 归档重试
    def retry_pending_archive(self, tenant_id: str, track_id: str) -> dict:
        """重渲染 → **逐字节比对** → 归档 → 补发事件。

        "重试不重新渲染"在本服务里只能是"重新渲染得到同一串字节"：库里没有
        字节。哈希对不上就 500 `RENDER_NOT_DETERMINISTIC`，绝不归档第二份
        （C10）。
        """
        track = _row(self.conn.execute(
            "SELECT * FROM track WHERE tenant_id=? AND track_id=?",
            (tenant_id, track_id)).fetchone())
        if track is None:
            raise ServiceError(404, "TRACK_NOT_FOUND",
                               f"no subtitle track {track_id}")
        profile = _loads(track["profile_json"], {})
        pending = [_row(row) for row in self.conn.execute(
            "SELECT * FROM pending_render WHERE track_id=?"
            " AND subtitle_revision=? ORDER BY format",
            (track_id, track["subtitle_revision"]))]
        if not pending:
            return {"track_id": track_id,
                    "subtitle_revision": track["subtitle_revision"],
                    "archived_formats": [], "pending_formats": [],
                    "event_emitted": False}

        limit = int(profile.get("max_chars_per_line")
                    or DEFAULT_PROFILE.get("max_chars_per_line") or 16)
        cues = [{"start_seconds": row["start_seconds"],
                 "end_seconds": row["end_seconds"],
                 "lines": _wrap(row["display_text"], limit)}
                for row in self.conn.execute(
                    "SELECT start_seconds, end_seconds, display_text"
                    " FROM cue WHERE track_id=? ORDER BY cue_index",
                    (track_id,))]
        if not cues:
            raise ServiceError(409, "NO_CUES_TO_RENDER",
                               "the track has no cue rows to re-render")
        archived: list[dict] = []
        remaining: list[str] = []
        emitted = False
        state = str(track["state"])
        with Transaction(self.conn):
            for item in pending:
                fmt = item["format"]
                text = render(fmt, cues,
                              style_ref=str(profile.get("style_ref")
                                            or "tpl_sub_default"),
                              font=str(profile.get("font")
                                       or DEFAULT_PROFILE["font"]),
                              profile=profile)
                blob = text.encode("utf-8")
                if _sha256_hex(text) != item["content_hash"] \
                        or len(blob) != item["byte_size"]:
                    # C10：字节不可复现时宁可停下。归档第二份会让
                    # pending_render 的摘要变成一个没人核过的值。
                    raise ServiceError(
                        500, "RENDER_NOT_DETERMINISTIC",
                        f"{fmt} re-render does not reproduce the recorded "
                        f"content_hash; S38 will not archive a second copy")
                try:
                    if self.archive_source is None:
                        raise ServiceError(
                            503, "ARCHIVE_UNAVAILABLE",
                            "no archive source is wired (plan §3.0)")
                    stored = self.archive_source(tenant_id, {
                        "media_type": MEDIA_TYPES[fmt],
                        "content_b64": base64.b64encode(blob).decode("ascii"),
                        "content_hash": item["content_hash"],
                        "metadata": {"subtitle_track_id": track_id,
                                     "subtitle_revision":
                                         track["subtitle_revision"],
                                     "format": fmt,
                                     "target_locale": track["target_locale"]},
                    })
                except (ConnectionError, ServiceError) as exc:
                    if isinstance(exc, ServiceError) and exc.status < 500:
                        raise
                    self.conn.execute(
                        "UPDATE pending_render SET attempts=attempts+1,"
                        " last_error=? WHERE track_id=? AND subtitle_revision=?"
                        " AND format=?",
                        (f"{type(exc).__name__}: {exc}", track_id,
                         track["subtitle_revision"], fmt))
                    remaining.append(fmt)
                    continue
                if not isinstance(stored, dict) \
                        or not str(stored.get("artifact_id") or "").startswith(
                            "art_") \
                        or stored.get("content_hash") != item["content_hash"]:
                    raise ServiceError(
                        502, "ARCHIVE_BAD_RESPONSE",
                        "artifact-ingest did not return the archived pointer "
                        "S38 asked for", details={"format": fmt})
                self.conn.execute(
                    "INSERT INTO rendition (track_id, subtitle_revision,"
                    " format, artifact_id, content_hash, byte_size,"
                    " rendered_at) VALUES (?,?,?,?,?,?,?)",
                    (track_id, track["subtitle_revision"], fmt,
                     stored["artifact_id"], stored["content_hash"],
                     int(item["byte_size"]), _now()))
                self.conn.execute(
                    "DELETE FROM pending_render WHERE track_id=?"
                    " AND subtitle_revision=? AND format=?",
                    (track_id, track["subtitle_revision"], fmt))
                archived.append({"format": fmt,
                                 "artifact_id": stored["artifact_id"],
                                 "content_hash": stored["content_hash"]})

            state = (state if remaining
                     else DECISION_STATE.get(str(track["qc_decision"]),
                                             "rendered"))
            self.conn.execute(
                "UPDATE track SET state=?, updated_at=? WHERE track_id=?",
                (state, _now(), track_id))
            if not remaining:
                emitted = self._emit_after_archive(tenant_id, track)

        return {"track_id": track_id,
                "subtitle_revision": track["subtitle_revision"],
                "state": state,
                "archived_formats": [item["format"] for item in archived],
                "pending_formats": remaining,
                "event_emitted": emitted}

    def _emit_after_archive(self, tenant_id: str, track: dict) -> bool:
        """补发用：所有格式都归档了才发，且只发一次。"""
        revision = int(track["subtitle_revision"])
        exists = self.conn.execute(
            "SELECT 1 FROM outbox WHERE aggregate_id=? AND sequence=?"
            " AND event_type=?", (track["track_id"], revision,
                                  schema.EVENT_READY)).fetchone()
        if exists is not None:
            return False
        artifacts = [_row(row) for row in self.conn.execute(
            "SELECT format, artifact_id, content_hash FROM rendition"
            " WHERE track_id=? AND subtitle_revision=? ORDER BY format",
            (track["track_id"], revision))]
        if not artifacts:
            return False
        report = _row(self.conn.execute(
            "SELECT report_id, report_generation, decision,"
            " report_content_hash FROM qc_report WHERE tenant_id=?"
            " AND track_id=? AND subtitle_revision=? ORDER BY"
            " report_generation DESC LIMIT 1",
            (tenant_id, track["track_id"], revision)).fetchone())
        if report is None:
            return False
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM cue WHERE track_id=?",
            (track["track_id"],)).fetchone()["n"]
        stale = self.conn.execute(
            "SELECT COUNT(*) AS n FROM cue c JOIN translation t"
            " ON t.translation_id=c.translation_id"
            " AND t.revision=c.translation_revision"
            " WHERE c.track_id=? AND t.status='stale'",
            (track["track_id"],)).fetchone()["n"]
        defects = dict(self.conn.execute(
            "SELECT report_id, COUNT(*) AS n FROM qc_defect WHERE report_id=?"
            " GROUP BY report_id", (report["report_id"],)).fetchone()
            or {"n": 0})["n"]
        self._emit_ready(
            tenant_id=tenant_id, command_id="retry_pending_archive",
            correlation_id=f"retry:{track['track_id']}:{revision}",
            home_cell="unknown", track_id=track["track_id"],
            subtitle_revision=revision, episode_id=track["episode_id"],
            branch_id=track["branch_id"], timeline_id=track["timeline_id"],
            timeline_revision=int(track["timeline_revision"]),
            target_locale=track["target_locale"], cue_count=int(total),
            artifacts=artifacts, report_id=report["report_id"],
            report_generation=report["report_generation"],
            report_digest=report["report_content_hash"],
            decision=report["decision"], defect_count=int(defects),
            stale_translation_count=int(stale))
        return True

    # ------------------------------------------------------------ 事件投影
    def _consume_once(self, consumer: str, tenant_id: str,
                      envelope: dict) -> bool:
        """本服务自己的 inbox。返回 True 表示首次见到。"""
        event_id = envelope.get("id")
        if not event_id:
            raise ServiceError(400, "INVALID_EVENT", "event.id is required")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbox (consumer, event_id, tenant_id,"
            " event_type, aggregate_id, sequence, received_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (consumer, str(event_id), tenant_id,
             str(envelope.get("type") or ""),
             str(envelope.get("aggregate_id") or ""),
             envelope.get("sequence"), _now()))
        return cur.rowcount == 1

    def consume_script_revision_event(self, tenant_id: str,
                                      envelope: dict) -> dict:
        """§2.3：源文 revision 前进 → 既有译文标 stale，**不重译**。

        事件只带 `revision` / `line_set_hash`，没有逐行 hash（S38-C20），所以
        无法判断"哪几行变了"。保守做法：整体标 stale——一块该 stale 的译文
        会让交付门拦住，一块漏标的 stale 会把过时译文送进成片。
        """
        data = envelope.get("data") if isinstance(envelope.get("data"),
                                                  dict) else {}
        script_id = str(data.get("script_id") or "")
        try:
            revision = int(data.get("revision"))
        except (TypeError, ValueError):
            raise ServiceError(400, "INVALID_EVENT",
                               "narrative.script.revision.created needs an "
                               "integer data.revision")
        with Transaction(self.conn):
            first = self._consume_once(schema.CONSUMER_SCRIPT, tenant_id,
                                       envelope)
            if not first:
                return {"duplicate": True, "tracks": 0,
                        "translations_marked_stale": 0}
            rows = self.conn.execute(
                "SELECT track_id, subtitle_revision, dialogue_revision"
                " FROM track WHERE tenant_id=? AND dialogue_id=?",
                (tenant_id, script_id)).fetchall()
            tracks = 0
            marked = 0
            stale_now = _now()
            for row in rows:
                if revision <= int(row["dialogue_revision"]):
                    # 乱序/重放：一个更旧的 revision 不能把新译文标 stale。
                    continue
                tracks += 1
                cur = self.conn.execute(
                    "UPDATE translation SET status='stale'"
                    " WHERE tenant_id=? AND status IN ('draft','reviewed',"
                    " 'accepted') AND source_text_revision < ?"
                    " AND translation_id IN ("
                    "   SELECT translation_id FROM cue WHERE track_id=?)",
                    (tenant_id, revision, row["track_id"]))
                marked += cur.rowcount
                # cue 记录"构建时基于哪一版原句"。文本不动：重译要花钱且要人
                # 验收（§2.3），本服务没有那条路径。
                self.conn.execute(
                    "UPDATE cue SET source_text_revision=?"
                    " WHERE track_id=? AND source_text_revision < ?",
                    (revision, row["track_id"], revision))
                self.conn.execute(
                    "UPDATE track SET dialogue_revision=?, updated_at=?"
                    " WHERE track_id=?", (revision, stale_now,
                                          row["track_id"]))
        self._bump("script_revision_events_total")
        return {"duplicate": False, "tracks": tracks,
                "translations_marked_stale": marked}

    def consume_timeline_revision_event(self, tenant_id: str,
                                        envelope: dict) -> dict:
        """§3.1：只重算与变化 clip 相关的 cue，其余逐字段不动。

        事件不带 clips / tick_rate（C3），所以必须回读 S41；读不到就 503 让
        上游重投，不做部分应用。

        重算后的时码与已归档的字节不再对应，因此这一版字幕作废：`subtitle_
        revision` +1、状态回 `draft`。旧 revision 的 rendition / qc_report
        按 revision 各自留档，不被覆盖。
        """
        data = envelope.get("data") if isinstance(envelope.get("data"),
                                                  dict) else {}
        timeline_id = str(data.get("timeline_id") or "")
        try:
            revision = int(data.get("revision"))
        except (TypeError, ValueError):
            raise ServiceError(400, "INVALID_EVENT",
                               "post.timeline.revision.created needs an "
                               "integer data.revision")
        # 先读 S41 再落 inbox：反过来会在"读失败 → 503 重投"时把事件判成
        # 重复，EDL 变化永远不会被应用。
        fresh = self._timeline(tenant_id, {"id": timeline_id,
                                           "revision": revision})
        spans_new = clip_map(fresh["clips"], fresh["tick_rate"])
        if fresh["duration"] is None:
            fresh["duration"] = max((u["output_end"] for u in spans_new),
                                    default=0.0)

        with Transaction(self.conn):
            if not self._consume_once(schema.CONSUMER_TIMELINE, tenant_id,
                                      envelope):
                return {"duplicate": True, "affected_cues": 0}
            rows = self.conn.execute(
                "SELECT * FROM track WHERE tenant_id=? AND timeline_id=?"
                " AND timeline_revision < ?",
                (tenant_id, timeline_id, revision)).fetchall()
            affected_total = 0
            changed_total: list[str] = []
            for track in rows:
                affected, changed = self._recompute_timeline(
                    tenant_id=tenant_id, track=track, revision=revision,
                    spans_new=spans_new, duration=fresh["duration"],
                    tick_rate=fresh["tick_rate"], clips=fresh["clips"])
                affected_total += affected
                changed_total.extend(changed)
        self._bump("timeline_revision_events_total")
        return {"duplicate": False, "affected_cues": affected_total,
                "changed_clips": sorted(set(changed_total))}

    def _recompute_timeline(self, *, tenant_id, track, revision, spans_new,
                            duration, tick_rate, clips) -> tuple[int, list]:
        """§3.1：只动受影响的 cue。

        旧快照用**它自己记的 tick_rate** 反算。用新事件的 tick_rate 去换算
        旧 ticks，会让每一段"没改"的 clip 看起来都变了。
        """
        track_id = track["track_id"]
        snapshot = [_row(row) for row in self.conn.execute(
            "SELECT * FROM clip_ref WHERE track_id=?", (track_id,))]
        old_rate = (int(snapshot[0]["tick_rate"]) if snapshot else int(tick_rate))
        old_spans = clip_map(
            [{"clip_id": row["clip_id"], "in_ticks": row["in_ticks"],
              "out_ticks": row["out_ticks"], "speed_ratio": row["speed_ratio"]}
             for row in snapshot], old_rate)

        old_by_id = {span["clip_id"]: span for span in old_spans}
        changed = []
        for clip in clips:
            clip_id = str(clip.get("clip_id") or "")
            span = next((item for item in spans_new
                         if item["clip_id"] == clip_id), None)
            before = old_by_id.get(clip_id)
            if span is None or before is None:
                changed.append(clip_id)
            elif (abs(span["source_in"] - before["source_in"]) > 1e-9
                  or abs(span["source_out"] - before["source_out"]) > 1e-9
                  or abs(span["speed"] - before["speed"]) > 1e-9):
                changed.append(clip_id)
        changed_set = set(changed)

        affected = 0
        cues = self.conn.execute(
            "SELECT * FROM cue WHERE track_id=? ORDER BY cue_index",
            (track_id,)).fetchall()
        for cue in cues:
            if cue["clip_id"] not in changed_set:
                # §3.1：未受影响的时码原样保留——连一次浮点重算都不做。
                continue
            affected += 1
            source_begin = output_to_source(old_spans,
                                            float(cue["start_seconds"]))
            source_end = output_to_source(old_spans, float(cue["end_seconds"]))
            begin, _ = source_to_output(spans_new, source_begin, duration)
            end, _ = source_to_output(spans_new, source_end, duration)
            begin = max(0.0, min(duration, begin))
            end = max(begin, min(duration, end))
            minimum = schema.MIN_DURATION_MS / 1000.0
            if end - begin < minimum:
                end = min(duration, begin + minimum)
            self.conn.execute(
                "UPDATE cue SET start_seconds=?, end_seconds=?"
                " WHERE track_id=? AND cue_id=?",
                (round(begin, 6), round(end, 6), track_id, cue["cue_id"]))

        # 时间线坐标总是前进；只有真改了 cue 才动 subtitle_revision。
        # 旧 revision 的 rendition / qc_report 按 revision 各自留档。
        if affected:
            self.conn.execute(
                "UPDATE track SET timeline_revision=?, subtitle_revision=?,"
                " supersedes_revision=?, state='draft', updated_at=?"
                " WHERE track_id=?",
                (revision, int(track["subtitle_revision"]) + 1,
                 int(track["subtitle_revision"]), _now(), track_id))
        else:
            self.conn.execute(
                "UPDATE track SET timeline_revision=?, updated_at=?"
                " WHERE track_id=?", (revision, _now(), track_id))

        self.conn.execute("DELETE FROM clip_ref WHERE track_id=?", (track_id,))
        for clip in sorted(clips, key=lambda c: int(c.get("in_ticks") or 0)):
            in_ticks = int(clip.get("in_ticks") or 0)
            out_ticks = int(clip.get("out_ticks") or 0)
            if out_ticks <= in_ticks:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO clip_ref (track_id, clip_id,"
                " in_ticks, out_ticks, speed_ratio, tick_rate)"
                " VALUES (?,?,?,?,?,?)",
                (track_id, str(clip.get("clip_id") or ""), in_ticks,
                 out_ticks, float(clip.get("speed_ratio") or 1.0),
                 int(tick_rate)))
        return affected, changed

    # ------------------------------------------------------------ 译文状态机
    def mark_translation_reviewed(self, tenant_id: str, translation_id: str,
                                  revision: int, reviewer) -> dict:
        """draft → reviewed。**不提供路由**（C6）：审核通过不是放行依据。"""
        return self._translation_transition(
            tenant_id, translation_id, revision, source=("draft",),
            target="reviewed", reviewer=reviewer)

    def mark_translation_accepted(self, tenant_id: str, translation_id: str,
                                  revision: int, reviewer=None) -> dict:
        """reviewed → accepted。只给具备该权限的内部调用方（C6）。"""
        return self._translation_transition(
            tenant_id, translation_id, revision, source=("reviewed",),
            target="accepted", reviewer=reviewer)

    def _translation_transition(self, tenant_id, translation_id, revision,
                                *, source, target, reviewer) -> dict:
        row = _row(self.conn.execute(
            "SELECT status FROM translation WHERE tenant_id=?"
            " AND translation_id=? AND revision=?",
            (tenant_id, str(translation_id), int(revision))).fetchone())
        if row is None:
            raise ServiceError(404, "TRANSLATION_NOT_FOUND",
                               f"no translation {translation_id}@{revision}")
        if row["status"] not in source:
            # stale 也不能被 reviewed：一份 stale 译文先要有人重译。
            raise ServiceError(
                409, "INVALID_TRANSLATION_TRANSITION",
                f"translation {translation_id}@{revision} is "
                f"{row['status']!r}; {target!r} requires {list(source)}")
        with Transaction(self.conn):
            self.conn.execute(
                "UPDATE translation SET status=?, reviewer=COALESCE(?,"
                " reviewer) WHERE tenant_id=? AND translation_id=?"
                " AND revision=?",
                (target, _jcs(reviewer) if reviewer is not None else None,
                 tenant_id, str(translation_id), int(revision)))
        return {"translation_id": str(translation_id),
                "revision": int(revision), "status": target}


def _cue_view(row) -> dict:
    """cue 的三组版本引用并列回显（V5 的审计面）。"""
    item = dict(row)
    return {
        "cue_id": item["cue_id"],
        "cue_index": item["cue_index"],
        "clip_id": item["clip_id"],
        "script_line_id": item["script_line_id"],
        # 三份文本，三组 (id, revision, hash)。
        "source_text": {"service_id": "S06",
                        "revision": item["source_text_revision"],
                        "content_hash": item["source_text_hash"]},
        "lexicon": {"service_id": "S15", "id": item["lexicon_ref_id"],
                    "revision": item["lexicon_revision"],
                    "content_hash": item["lexicon_hash"]},
        "translation": {"service_id": "S38", "id": item["translation_id"],
                        "revision": item["translation_revision"],
                        "content_hash": item["translation_hash"],
                        "status": item.get("translation_status")},
        "display_text": item["display_text"],
        "start_seconds": item["start_seconds"],
        "end_seconds": item["end_seconds"],
        "alignment_report_id": item["alignment_report_id"],
        "align_unit_start": item["align_unit_start"],
        "align_unit_end": item["align_unit_end"],
        "node_epoch": item["node_epoch"],
        "line_count": item["line_count"],
        "max_chars_per_line": item["max_chars_per_line"],
        "style_ref": item["style_ref"],
        "safe_area_ok": bool(item["safe_area_ok"]),
    }


def _track_content_hash(artifacts) -> str:
    """轨道的身份摘要：只有指针与摘要，不参与任何字节计算。"""
    return _sha256_hex(_jcs([
        {"format": item["format"], "artifact_id": item["artifact_id"],
         "content_hash": item["content_hash"]}
        for item in sorted(artifacts, key=lambda item: item["format"])]))

