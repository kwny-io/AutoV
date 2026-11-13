"""S20 的上游闭包。**只有 HTTP**，没有共享库、没有共享库表、没有 ORM 导入。

AGENTS.md 的红线：跨服务只能走契约面。这里每个 `build_*` 拿一个 base URL，
返回一个闭包；URL 为空时返回 `_unconfigured(...)`，调用即 503。

为什么空 URL 不是"跳过这一步"：S20 的所有输入都是**权威在他处**的东西 ——
镜头、绑定集、能力快照、对白对齐。缺一个就少了一个 pin，编出来的 prompt 会
看起来成功、实际上引用的是过期或不存在的东西。宁可 503（可重试），也不要
"尽力编译"（一个静默省略的引用）。这是 §1.3 与 T20-30 的要求。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from prompt_compiler.service import ServiceError, _missing_source

# 单个上游调用的硬超时。S20 在一次编译里要串行打 4 个上游；没有超时的话
# 一个卡住的 S10 会让整条 HTTP 请求挂住，而调用方（S25/S19）有自己的 deadline。
_TIMEOUT_SECONDS = 5.0


def _request(method: str, url: str, *, body: dict | None = None,
             headers: dict | None = None, timeout: float = _TIMEOUT_SECONDS):
    """一个极小的 JSON over HTTP。把网络错误翻译成 S20 的两种错误。

    - 上游返回 4xx/5xx：如果它自己给了 `{code, retryable, details}`，原样带过来
      （S20 只是搬运工，不重新解释上游的错误语义）；否则给 `UPSTREAM_ERROR`。
    - 连不上 / 超时：`ConnectionError`，由 `_upstream` 统一翻成 503。
    """
    data = None
    all_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        all_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        all_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=all_headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        code = payload.get("code") or "UPSTREAM_ERROR"
        raise ServiceError(
            exc.code, code,
            payload.get("message") or f"upstream returned {exc.code}",
            retryable=bool(payload.get("retryable")),
            details=payload.get("details") if isinstance(
                payload.get("details"), dict) else None) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(f"{method} {url} failed: {exc}") from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _base_map(base_urls: dict) -> dict:
    """去掉空值。空字符串会拼出 `"/shots/..."` 这种相对 URL，`urlopen` 直接
    抛 ValueError —— 那看起来像 bug 而不是"没配"，所以在这里挡掉。"""
    return {key: value.rstrip("/") for key, value in (base_urls or {}).items()
            if value}


def _unconfigured(service_id: str, what: str):
    """未配置的上游。调用即 503，`retryable=True`（是部署问题，不是请求问题）。"""
    def call(*_args, **_kwargs):
        # 用 `service._missing_source` 的那张表，不在这里再写一份 if/else：
        # 两份映射会在加第五个上游时立刻分叉。
        _missing_source(service_id, what)
    return call


# ---------------------------------------------------------------- 镜头源

def build_shot_source(base_url: str | None):
    """S06 `GET /shots/{id}/revisions/{revision}` → `{shot_spec, s06_meta}`。

    `s06_meta.content_hash` 是乐观并发的那个 pin：它与调用方给的
    `shot_ref.content_hash` 不一致 → 409 `SHOT_REVISION_MOVED`（不是失败）。
    """
    if not base_url:
        return _unconfigured("S06", "the shot revision")

    base = base_url.rstrip("/")

    def get_shot(shot_id: str, revision: int) -> dict | None:
        try:
            return _request(
                "GET", f"{base}/shots/{shot_id}/revisions/{revision}")
        except ServiceError as exc:
            if exc.status == 404:
                # 镜头不存在是**数据**问题，不是上游故障。返回 None 让服务层
                # 决定码；这里不把它降级成 503，否则调用方会一直重试一个
                # 永远不会出现的 revision。
                return None
            raise
    return get_shot


# ---------------------------------------------------------------- 绑定集源

def build_binding_source(base_url: str | None):
    """S10 `GET /binding-sets/{id}/revisions/{rev}`。

    返回的 `payload.entries[]` 里每一项的 `role_key` 形如
    `<asset_type>:<owner_local_id>` —— 那是 S10 的稳定身份（S20-43），
    不是方案 §3.2 虚构的 `global_asset_id`。
    """
    if not base_url:
        return _unconfigured("S10", "the binding set revision")

    base = base_url.rstrip("/")

    def get_binding_set(binding_set_id: str, revision: int) -> dict | None:
        try:
            return _request(
                "GET", f"{base}/binding-sets/{binding_set_id}"
                       f"/revisions/{revision}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
    return get_binding_set


# ---------------------------------------------------------------- 能力快照源

def build_capability_source(base_url: str | None):
    """S24 `GET /capability-snapshots/{snapshot_id}`。

    **注意：S24 的响应里没有 `content_hash`**（S20-34）。服务层用
    `sha256(JCS(response))` 派生一个，与调用方的 pin 比对。这不是"更好"，
    是唯一能在不 invent 字段的前提下把快照钉住的办法。
    """
    if not base_url:
        return _unconfigured("S24", "the capability snapshot")

    base = base_url.rstrip("/")

    def get_capability(snapshot_id: str) -> dict | None:
        try:
            return _request(
                "GET", f"{base}/capability-snapshots/{snapshot_id}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
    return get_capability


# ---------------------------------------------------------------- 对齐源

def build_alignment_source(base_url: str | None):
    """S37 `GET /alignment-reports/{id}` 与 `/units?start=&end=`。

    只读、只 pin。S20 不重算对齐，也不在对齐缺失时估时长（§2.4 那条
    "不估对白时长"）。
    """
    if not base_url:
        return _unconfigured("S37", "the dialogue alignment report")

    base = base_url.rstrip("/")

    def get_alignment(report_id: str) -> dict | None:
        try:
            return _request("GET", f"{base}/alignment-reports/{report_id}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    def get_units(report_id: str, *, start: float | None = None,
                  end: float | None = None) -> dict | None:
        url = f"{base}/alignment-reports/{report_id}/units"
        params = []
        if start is not None:
            params.append(f"start={start}")
        if end is not None:
            params.append(f"end={end}")
        if params:
            url += "?" + "&".join(params)
        try:
            return _request("GET", url)
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    return {"report": get_alignment, "units": get_units}


# ---------------------------------------------------------------- 回归集源

def build_regression_source(base_url: str | None):
    """模板发布时的回归集。**S20 不保管测试用例本体**：它只 pin 一个
    `{id, revision, content_hash}` 并在发布时要求调用方给出通过结论。

    `regression_set_ref` 的内容权威在模板作者那边（`plans/S20` §5.3）；本仓库
    没有为它建服务，所以未配置时发布路径会 503 —— 这是诚实的：没有回归集就
    不该发布模板（F23）。
    """
    if not base_url:
        def unavailable(_ref):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "no regression-set source is configured; publishing a "
                "template without a regression set is refused (F23)",
                retryable=True)
        return unavailable

    base = base_url.rstrip("/")

    def get_regression_set(ref: dict) -> dict | None:
        try:
            return _request(
                "GET", f"{base}/regression-sets/{ref.get('id')}"
                       f"/revisions/{ref.get('revision')}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
    return get_regression_set
