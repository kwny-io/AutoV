"""受限模板语言（方案 §3.5）。**纯函数**（§1.6）：不碰库、不碰时间、不碰随机。

只支持六件事，多一件都不要：

| 语法 | 例 |
|---|---|
| 路径引用 | `{{ camera.shot_size }}` |
| 枚举映射 | `{{ camera.shot_size \\| phrase_table }}` |
| 原样块 | `{{#verbatim dialogue}}，{{/verbatim}}` |
| 条件 | `{{#if lighting}}…{{/if}}` |
| 连接符 | `{{ join(beats, "，") }}` |
| 负向段 | `{{#negative}}…{{/negative}}` |

**没有函数调用、没有算术、没有正则、没有截断、没有时间、没有 I/O。**
尤其没有 `llm_rewrite`：加上它就是给编译器开一条改写事实的后门（§2.5）。

三条实现约定：

1. **路径必须被某段的 `from` 声明过。** 未声明的插值不是"业务校验失败"，而是
   实现缺陷的守门员 —— 正文里出现了数据却没有对应的声明。`UNSOURCED_COMPLETION`
   只在该分支触发（§3.5 第 3 项）。
2. **`verbatim` 块的内层是字面量，不插值。** 值本身逐字节追加，不 trim、不转义、
   不折行。对白只能从这个门出去（§1.2）。
3. **每个输出字符都有来源。** 渲染时同步登记区间与来源；服务层据此产出
   `derived_from` 并用 `check_coverage` 断言连续覆盖（§4.2 第 2 条）。

`reference_token_format` 的编号用 **position + 1**（§4.1 的示例 `position:0` →
`<image_1>`）。方案 §3.4 的散文说"编号就是顺序位置"，与它自己的示例差 1；按
示例实现，因为这正是下游会照着抄的那一处（`CONTRACT_GAPS` S20-46）。
"""

from __future__ import annotations

import math
import re

_MISSING = object()

# `a.b[0].c` 的段：标识符或 `[n]` / `[*]`。
_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\*|\d+)\]")

ALLOWED_RENDER = ("verbatim", "phrase_table", "template_string", "omit")


class RenderError(Exception):
    """渲染阶段的确定性失败。服务层把它翻成 `ServiceError`。

    不直接抛 `ServiceError` 是为了让本模块不依赖 `service`（避免循环导入），
    也让"纯函数"这件事在结构上成立：这里没有任何 `sqlite3` / `urllib` 痕迹。
    """

    def __init__(self, code: str, message: str, *, status: int = 422,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


# ---------------------------------------------------------------- 路径

def parse_path(path) -> list:
    """`a.b[0].c` → `['a','b',0,'c']`。`[*]` 记为 `'*'`。

    不引入 JSONPath 依赖：模板里只允许点号与 `[n]` / `[*]`，这样"路径语法"
    与"模板语法"都是能一眼看完的东西。任何别的写法直接拒绝，不猜。
    """
    if not isinstance(path, str) or not path:
        raise RenderError("TEMPLATE_BODY_INVALID", f"bad path {path!r}")
    tokens: list = []
    pos = 0
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = _TOKEN_RE.match(path, pos)
        if not m:
            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"bad path {path!r} at offset {pos}")
        name, index = m.group(1), m.group(2)
        if name is not None:
            tokens.append(name)
        elif index == "*":
            tokens.append("*")
        else:
            tokens.append(int(index))
        pos = m.end()
    if not tokens or tokens[0] == "*":
        raise RenderError("TEMPLATE_BODY_INVALID", f"bad path {path!r}")
    return tokens


def get_path(root, path: str):
    """按路径取值，返回 `(found, value)`。

    `[*]` 把列表逐项展开；空列表也返回"找到了"（值 `[]`）——「字段存在但为空」
    与「字段不存在」在 `#if` 里是两件事，不能合并。
    """
    return _walk(root, parse_path(path))


def _walk(current, tokens: list):
    if not tokens:
        return True, current
    head, rest = tokens[0], tokens[1:]
    if head == "*":
        if not isinstance(current, list):
            return False, _MISSING
        values = []
        for element in current:
            found, value = _walk(element, rest)
            if not found:
                return False, _MISSING
            values.append(value)
        return True, values
    if isinstance(head, int):
        if not isinstance(current, list) or head >= len(current):
            return False, _MISSING
        return _walk(current[head], rest)
    if not isinstance(current, dict) or head not in current:
        return False, _MISSING
    return _walk(current[head], rest)


def _stringify(value) -> str:
    """标量的确定性字符串化。bool 用 JSON 拼写，浮点走最短 repr。

    不用 `str()` 兜底：`None` 绝不能渲染成 "None" 混进 prompt。
    """
    if value is None:
        raise RenderError("TEMPLATE_BODY_INVALID", "null cannot be rendered")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return value
    raise RenderError("TEMPLATE_BODY_INVALID",
                      f"cannot render value of type {type(value).__name__}")


# ---------------------------------------------------------------- 词法/语法

class _Parser:
    """把一段文本解析成节点序列。

    - `("lit", text)`
    - `("ref", path)` / `("phrase", path)`
    - `("join", path, separator)`
    - `("verbatim", path, inner_literal)`
    - `("if", path, children)`
    - `("negative", children)`
    """

    def __init__(self, text: str):
        self.text = text

    def parse(self) -> list:
        nodes, _ = self._until(0, stop=None)
        return nodes

    def _until(self, pos: int, stop):
        nodes: list = []
        text = self.text
        while True:
            start = text.find("{{", pos)
            if start < 0:
                if stop is not None:
                    raise RenderError("TEMPLATE_BODY_INVALID",
                                      f"unterminated {stop!r} block")
                if pos < len(text):
                    nodes.append(("lit", text[pos:]))
                return nodes, len(text)
            if start > pos:
                nodes.append(("lit", text[pos:start]))
            end = text.find("}}", start)
            if end < 0:
                raise RenderError("TEMPLATE_BODY_INVALID", "unterminated '{{'")
            tag = text[start + 2:end].strip()
            body = end + 2

            if tag.startswith("/"):
                name = tag[1:].strip()
                if stop is None or name != stop:
                    raise RenderError("TEMPLATE_BODY_INVALID",
                                      f"unexpected closing tag {tag!r}")
                return nodes, body

            if tag.startswith("#"):
                name, _, arg = tag[1:].partition(" ")
                name, arg = name.strip(), arg.strip()
                if name == "verbatim":
                    close = text.find("{{/verbatim}}", body)
                    if close < 0:
                        raise RenderError(
                            "TEMPLATE_BODY_INVALID",
                            "unterminated '{{#verbatim}}' block")
                    nodes.append(("verbatim", arg, text[body:close]))
                    pos = close + len("{{/verbatim}}")
                    continue
                if name in ("if", "negative"):
                    children, pos = self._until(body, stop=name)
                    nodes.append(("if", arg, children) if name == "if"
                                 else ("negative", children))
                    continue
                raise RenderError("TEMPLATE_BODY_INVALID",
                                  f"unsupported block {name!r}")

            if tag.startswith("join("):
                if not tag.endswith(")"):
                    raise RenderError("TEMPLATE_BODY_INVALID",
                                      f"malformed join {tag!r}")
                path, separator = _split_join_args(tag[len("join("):-1])
                nodes.append(("join", path, separator))
                pos = body
                continue

            left, sep, right = tag.partition("|")
            if sep and right.strip() != "phrase_table":
                raise RenderError(
                    "TEMPLATE_BODY_INVALID",
                    f"unsupported filter {right.strip()!r}; only phrase_table")
            nodes.append(("phrase" if sep else "ref", left.strip()))
            pos = body


def _split_join_args(inner: str):
    """`beats, "，"` → `('beats', '，')`。分隔符只能是模板里的字面量（§3.5）。"""
    out: list[str] = []
    buf: list[str] = []
    quoted = False
    seen_quote = False
    for ch in inner:
        if ch == '"':
            quoted = not quoted
            seen_quote = True
            continue
        if ch == "," and not quoted:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf).strip())
    if not seen_quote:
        raise RenderError("TEMPLATE_BODY_INVALID",
                          "join separator must be a quoted literal, not data")
    if len(out) != 2:
        raise RenderError("TEMPLATE_BODY_INVALID",
                          'join takes exactly (path, "separator")')
    return out[0], out[1]


# ---------------------------------------------------------------- 片段

class Fragment:
    """一段渲染结果。

    `spans` 是相对本片段起点的 `(start, end, entry)`；拼接时整体平移。
    `target` 取 `main` / `negative`：负向段不进 `prompt_text`，故不参与覆盖断言。
    """

    __slots__ = ("section_id", "optional", "requires", "target", "text",
                 "spans", "empty")

    def __init__(self, section_id: str, optional: bool, requires: list,
                 target: str = "main"):
        self.section_id = section_id
        self.optional = optional
        self.requires = requires
        self.target = target
        self.text = ""
        self.spans: list = []
        self.empty = True


def _materialise(fragment: Fragment, pieces: list) -> None:
    offset = 0
    for piece, entry in pieces:
        end = offset + len(piece)
        if end > offset:
            fragment.spans.append((offset, end, dict(entry)))
            fragment.empty = False
        offset = end
    fragment.text = "".join(piece for piece, _ in pieces)


def _check_declared(path: str, declared: set) -> None:
    """路径必须落在某段声明的 `from` 之下（相等或为其子路径）。"""
    if not isinstance(path, str) or not path:
        raise RenderError("TEMPLATE_BODY_INVALID", "empty path reference")
    for root in declared:
        if path == root or path.startswith(root + ".") or \
                path.startswith(root + "["):
            return
    raise RenderError(
        "UNSOURCED_COMPLETION",
        f"template interpolates {path!r}, which no section declares; this is "
        f"an implementation defect, not a business validation failure (§3.5)",
        details={"path": path})


def _phrase(phrases: dict, path: str, value):
    table = phrases.get(path)
    if not isinstance(table, dict):
        raise RenderError(
            "REQUIRED_FIELD_UNSUPPORTED",
            f"adapter has no phrase table for {path!r}",
            details={"reason": "PHRASE_NOT_FOUND", "path": path,
                     "value": value})
    hit = table.get(_stringify(value))
    if not isinstance(hit, dict) or not isinstance(hit.get("text"), str) \
            or not isinstance(hit.get("phrase_id"), str):
        # §7 没给"枚举值不在词表"分配码（S20-17）：用同一码 + reason 区分，
        # 不臆造短语，也不回退成原文。
        raise RenderError(
            "REQUIRED_FIELD_UNSUPPORTED",
            f"enum value {value!r} is not in the phrase table for {path!r}; "
            f"S20 does not invent phrases",
            details={"reason": "PHRASE_NOT_FOUND", "path": path,
                     "value": value})
    return hit["text"], hit["phrase_id"]


def _phrase_leaves(phrases: dict, path: str, value) -> list:
    """把一棵子树逐叶映射成短语，**键序固定**（字典按 key 排序）。

    §2.5 的示例是 `{"id":"camera","from":"camera","render":"phrase_table"}` ——
    `camera` 是一个对象，所以 phrase_table 必须能吃掉整个子树，否则那份示例
    根本编译不出来。叶子顺序不能用插入序：JSON 反序列化的 dict 顺序依赖源
    字节，那就不是纯函数了（§1.6）。
    """
    out: list = []
    if isinstance(value, dict):
        for key in sorted(value):
            out.extend(_phrase_leaves(phrases, f"{path}.{key}", value[key]))
        return out
    if isinstance(value, list):
        for index, element in enumerate(value):
            out.extend(_phrase_leaves(phrases, f"{path}[{index}]", element))
        return out
    text, phrase_id = _phrase(phrases, path, value)
    return [(text, phrase_id, path, value)]


def _require_value(root, path: str):
    found, value = get_path(root, path)
    if not found:
        raise RenderError(
            "SCHEMA_INCOMPATIBLE",
            f"declared path {path!r} is absent from the shot",
            details={"path": path, "reason": "PATH_ABSENT"})
    if value is _MISSING:
        raise RenderError(
            "SCHEMA_INCOMPATIBLE",
            f"declared path {path!r} is absent from the shot",
            details={"path": path, "reason": "PATH_ABSENT"})
    return value


def _emit(nodes: list, *, root, declared: set, phrases: dict,
          speaker_prefix: str) -> list:
    """求值节点序列，返回 `(片段文本, 来源)` 列表。"""
    pieces: list = []

    def walk(node_list):
        for node in node_list:
            kind = node[0]
            if kind == "lit":
                if node[1]:
                    pieces.append((node[1], {"kind": "template_literal"}))
                continue

            if kind in ("ref", "phrase", "join", "verbatim"):
                _check_declared(node[1], declared)

            if kind == "ref":
                path = node[1]
                value = _require_value(root, path)
                if isinstance(value, (list, dict)):
                    raise RenderError(
                        "TEMPLATE_BODY_INVALID",
                        f"{path!r} is a container; use `join` or a verbatim "
                        f"block")
                pieces.append((_stringify(value),
                               {"kind": "template_string", "path": path}))
                continue

            if kind == "phrase":
                path = node[1]
                value = _require_value(root, path)
                leaves = _phrase_leaves(phrases, path, value)
                pieces.append(("，".join(t for t, *_ in leaves), {
                    "kind": "phrase_table", "path": path,
                    "value": value if not isinstance(value, (list, dict))
                             else [l[3] for l in leaves],
                    "phrase_id": ",".join(l[1] for l in leaves)}))
                continue

            if kind == "join":
                path, separator = node[1], node[2]
                value = _require_value(root, path)
                if not isinstance(value, list):
                    raise RenderError("TEMPLATE_BODY_INVALID",
                                      f"join needs a list at {path!r}")
                for element in value:
                    if isinstance(element, (dict, list)):
                        raise RenderError(
                            "TEMPLATE_BODY_INVALID",
                            f"join({path!r}) has a non-scalar element; point "
                            f"the path at the scalar instead")
                pieces.append((separator.join(_stringify(v) for v in value), {
                    "kind": "template_string", "path": path,
                    "join": separator}))
                continue

            if kind == "verbatim":
                path, literal = node[1], node[2]
                value = _require_value(root, path)
                is_list = isinstance(value, list)
                elements = value if is_list else [value]
                for index, element in enumerate(elements):
                    if index and is_list and literal:
                        # 内层字面量只作元素间分隔，绝不插值（§3.5）。
                        pieces.append((literal, {"kind": "template_literal"}))
                    if isinstance(element, dict) and "text" in element:
                        speaker = element.get("speaker_id") or \
                            element.get("character_id") or ""
                        if speaker_prefix and speaker:
                            pieces.append((speaker_prefix.format(
                                speaker=speaker), {"kind": "template_literal"}))
                        pieces.append((_stringify(element["text"]), {
                            "kind": "verbatim",
                            "path": f"{path}[{index}].text" if is_list
                                    else f"{path}.text",
                            "line_id": element.get("line_id")}))
                    else:
                        pieces.append((_stringify(element), {
                            "kind": "verbatim",
                            "path": f"{path}[{index}]" if is_list else path}))
                continue

            if kind == "if":
                path, children = node[1], node[2]
                _check_declared(path, declared)
                found, value = get_path(root, path)
                # 只判断存在性：不比较字符串、不看真值、不看空容器（§3.5）。
                if found and value is not _MISSING and value is not None:
                    walk(children)
                continue

            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"unknown node {kind!r}")

    walk(nodes)
    return pieces


# ---------------------------------------------------------------- 对外

def render_sections(*, sections: list, root, phrases: dict,
                    speaker_prefix: str = "") -> list:
    """渲染 `body.sections[]`，返回片段。**不拼接** —— 能力校验会删段。

    `declared` 取**整份模板**所有段 `from` 的并集，不是"被引用段的集合"：
    `{{#if lighting}}` 出现在 subject 段里、而 lighting 段是 `omit`，这是合法
    的 —— 声明的是"这个字段允许被引用"。

    段的两种形态：

    - 有 `text`（S20-33）：走受限语法，`from` 只用于声明（可以为空）。
    - 无 `text`：按 `render` 整段输出 `from` 处的值。`verbatim` 逐字节；
      `phrase_table` 查词表。

    `{{#negative}}` 是 `text` 里唯一允许的顶层块，它整段改投 `target='negative'`，
    不进 `prompt_text`。
    """
    declared = set()
    for section in sections:
        if isinstance(section, dict) and section.get("from"):
            declared.add(str(section["from"]).strip())

    fragments: list = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"section {index} is not an object")
        source = section.get("from")
        render_as = section.get("render") or "verbatim"
        if render_as not in ALLOWED_RENDER:
            raise RenderError(
                "TEMPLATE_BODY_INVALID",
                f"section {index} has render={render_as!r}; only "
                f"verbatim/phrase_table/template_string/omit exist "
                f"(there is no llm_rewrite)")
        requires = section.get("requires") or []
        if not isinstance(requires, list) or \
                any(not isinstance(r, str) or not r for r in requires):
            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"section {index} requires[] must be strings")
        sid = str(section.get("id") or f"s{index}")
        optional = bool(section.get("optional"))
        text = section.get("text")
        if text is not None and not isinstance(text, str):
            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"section {sid} text must be a string")

        if render_as == "omit":
            continue

        if text:
            nodes = _Parser(text).parse()
            main = Fragment(sid, optional, requires, "main")
            _materialise(main, _emit([n for n in nodes if n[0] != "negative"],
                                     root=root, declared=declared,
                                     phrases=phrases,
                                     speaker_prefix=speaker_prefix))
            fragments.append(main)
            for node in nodes:
                if node[0] != "negative":
                    continue
                frag = Fragment(sid + ":negative", optional, requires,
                                "negative")
                _materialise(frag, _emit(node[1], root=root,
                                         declared=declared, phrases=phrases,
                                         speaker_prefix=speaker_prefix))
                fragments.append(frag)
            continue

        if not source:
            raise RenderError("TEMPLATE_BODY_INVALID",
                              f"section {sid} has neither from nor text")
        if render_as == "template_string":
            raise RenderError(
                "TEMPLATE_BODY_INVALID",
                f"section {sid} is template_string but has no text")

        path = str(source)
        found, value = get_path(root, path)
        if not found or value is _MISSING:
            if optional:
                # 可选段缺数据：不渲染，也不报错。这正是 `optional:true` 的
                # 意义，且不属于 §7 的任何失败分支。
                fragments.append(Fragment(sid, True, requires, "main"))
                continue
            raise RenderError(
                "SCHEMA_INCOMPATIBLE",
                f"section {sid} reads {source!r}, absent from the shot",
                details={"path": path, "reason": "PATH_ABSENT"})

        fragment = Fragment(sid, optional, requires, "main")
        if render_as == "phrase_table":
            leaves = _phrase_leaves(phrases, path, value)
            _materialise(fragment, [("，".join(t for t, *_ in leaves), {
                "kind": "phrase_table", "path": path,
                "value": value if not isinstance(value, (list, dict))
                         else [l[3] for l in leaves],
                "phrase_id": ",".join(l[1] for l in leaves)})])
            fragments.append(fragment)
            continue

        # verbatim（默认）
        if isinstance(value, list):
            pieces: list = []
            for i, element in enumerate(value):
                if isinstance(element, dict) and "text" in element:
                    speaker = element.get("speaker_id") or \
                        element.get("character_id") or ""
                    if speaker_prefix and speaker:
                        pieces.append((speaker_prefix.format(speaker=speaker),
                                       {"kind": "template_literal"}))
                    pieces.append((_stringify(element["text"]), {
                        "kind": "verbatim", "path": f"{path}[{i}].text",
                        "line_id": element.get("line_id")}))
                else:
                    pieces.append((_stringify(element), {
                        "kind": "verbatim", "path": f"{path}[{i}]"}))
                if i < len(value) - 1:
                    pieces.append(("\n", {"kind": "template_literal"}))
            _materialise(fragment, pieces)
        else:
            _materialise(fragment, [(_stringify(value),
                                     {"kind": "verbatim", "path": path})])
        fragments.append(fragment)
    return fragments


def join_fragments(fragments: list, *, target: str,
                   separator: str = "\n") -> tuple:
    """拼接同一去向的片段，返回 `(文本, 已平移到整体坐标的来源列表)`。

    空片段（`optional` 段缺数据、`omit` 段）不参与拼接，也不留分隔符 ——
    否则两个省略项之间会多出一个逗号，而那是"凭空多出的字符"、没有来源。
    """
    usable = [f for f in fragments if f.target == target and not f.empty]
    texts: list = []
    entries: list = []
    offset = 0
    for index, fragment in enumerate(usable):
        if index and separator:
            texts.append(separator)
            # 分隔符也要登记来源：它由模板/渲染器插入，不是数据也不是凭空来的。
            entries.append({"span": [offset, offset + len(separator)],
                            "kind": "template_literal"})
            offset += len(separator)
        texts.append(fragment.text)
        for start, end, entry in fragment.spans:
            entries.append({"span": [offset + start, offset + end], **entry})
        offset += len(fragment.text)
    return "".join(texts), entries


def check_coverage(text: str, entries: list) -> None:
    """断言 `entries` 的区间**连续且完整**覆盖 `text`。

    这是 §4.2 第 2 条的可执行形式：任何一段字符都必须能说出它从哪来。
    不连续 = 渲染器漏登记了某个片段，那是实现缺陷、不是渲染失败的正常出口，
    所以这里也抛 `UNSOURCED_COMPLETION`（同一个守门员）。
    """
    cursor = 0
    for entry in sorted(entries, key=lambda e: e["span"][0]):
        start, end = entry["span"]
        if start != cursor:
            raise RenderError(
                "UNSOURCED_COMPLETION",
                f"prompt_text is not fully attributed at offset {cursor}; "
                f"the renderer dropped a span (implementation defect)",
                details={"offset": cursor, "got": start})
        cursor = end
    if cursor != len(text):
        raise RenderError(
            "UNSOURCED_COMPLETION",
            f"prompt_text has {len(text) - cursor} unattributed trailing "
            f"character(s) at offset {cursor}",
            details={"offset": cursor, "text_length": len(text)})


def estimate_tokens(text: str) -> int:
    """确定性粗估：CJK 按 1 token，其余 4 字符 1 token。

    它**不进** `content_hash`（§4.1 末段）：tokenizer 升级会改数字，但产物内容
    没变。只用于 `max_chars` / `token_budget` 的粗筛。
    """
    cjk = 0
    other = 0
    for ch in text:
        if "⺀" <= ch <= "鿿" or "＀" <= ch <= "￯":
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)
