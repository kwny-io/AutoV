"""S20 prompt-compiler 的测试。

分层与仓库里其他服务一致：

- `Harness` 直接驱动内核，桩掉 S06 / S10 / S24 / S37 的读路径。桩返回的是
  上游**真实形状**（`{shot_spec, s06_meta}`、`payload.entries[]`、
  `mode_details[]`），不是随便一个 dict —— 形状一旦失真，这些测试就会证明
  一个不存在的契约。
- HTTP 测试用 `importlib` 把 `api/app.py` 加载到独立模块名，再替换模块级的
  `service` / `_idem`。直接 `import app` 会与其他服务的同名模块互相覆盖。

编号对齐方案 §9 的 T20-01…T20-72。每个断言上方写明它证明的是哪条红线。

三条反复出现的检查手法：

1. **确定性用「跑 N 次比 hash」**，不用「看代码像不像确定性代码」。
2. **不能只看错误码** —— 还断言"没有写 manifest"（`SELECT count(*)`），
   否则"报了错但已经落库"会静默通过。
3. **P0 的判定要在定稿文本上验**：对白漂移、禁用字段都对着 `prompt_text`
   断言，而不是对着渲染中间结果。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from idempotency_sdk import CommandConflict, CommandInProgress, \
    SqliteIdempotencyStore
from prompt_compiler import (
    PromptCompilerService,
    ServiceError,
    build_alignment_source,
    build_binding_source,
    build_capability_source,
    build_shot_source,
    manifest_content_hash,
)
from prompt_compiler.schema import CONTRACT_GAPS

ROOT = Path(__file__).resolve().parents[1]

TENANT = "tenant_alpha"
OTHER_TENANT = "tenant_beta"


def assert_gap(gap_id: str) -> dict:
    """文档里写「登记为 S20-XX」时，这句话本身要能被证伪。

    只在注释里写缺口编号是不可核对的：删除 `CONTRACT_GAPS` 里那条、或改掉
    它的 id，测试仍然全绿，"已登记"就成了空话。所有"未实现 / 冲突"类测试
    都从这里过一遍。
    """
    found = [g for g in CONTRACT_GAPS if g["id"] == gap_id]
    assert len(found) == 1, f"CONTRACT_GAPS has no unique entry {gap_id}"
    assert found[0].get("summary"), f"{gap_id} has no summary"
    return found[0]


def sha(char: str = "a") -> str:
    """确定性 64 位小写 hex。

    曾经是 `char * 64`（例如 64 个 `s`），这有两个问题：契约里所有
    `content_hash` 都要求 `^[0-9a-f]{64}$`，假 hash 会让"事件 data 通过
    schema 校验"这类断言永远不可能真跑；而且一个非法形状的夹具会让测试
    证明一个不存在的契约。用真 hash 后夹具本身就是契约合法的。
    """
    return hashlib.sha256(f"s20-fixture:{char}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 上游夹具

def make_shot(**over) -> dict:
    shot = {
        "schema_version": "1.0",
        "shot_id": "sht_0001",
        "revision": 3,
        "episode_id": "ep_01",
        "scene_id": "scn_0002",
        "display_locator": "3-12",
        "narrative_purpose": "建立人物与空间的紧张关系",
        "shot_type": "medium",
        "asset_bindings": {
            "characters": [{
                "character": {"id": "chr_linlan", "revision": 12},
                "costume": {"id": "cst_coat", "revision": 2},
                "voice": {"id": "vce_linlan", "revision": 5},
                "screen_role": "lead",
            }],
            "scene": {"id": "scn_hall", "revision": 4},
            "props": [{"id": "prp_letter", "revision": 1}],
            "style": {"id": "sty_noir", "revision": 1},
        },
        "camera": {"shot_size": "medium", "movement": "static"},
        "duration": {"target_seconds": 4.0},
        "output_spec": {"aspect_ratio": "16:9", "width": 1920,
                        "height": 1080, "fps": 24},
        "beats": [
            {"time_range": [0.0, 2.0], "action": "她推开门，停住"},
            {"time_range": [2.0, 4.0], "action": "她看向桌上的信"},
        ],
        "dialogue": [{"line_id": "ln_1", "character_id": "chr_linlan",
                      "text": "我回来了。", "emotion": "calm"}],
        "emotion": "calm",
        "lighting": "warm_amber",
        "continuity": {"previous_shot_id": "sht_0000"},
        "constraints": {"no_text_overlay": True},
    }
    shot.update(over)
    return shot


def make_binding_set(*, content_hash: str | None = None, entries=None) -> dict:
    if entries is None:
        entries = [
            _entry("character:chr_linlan", "character", "S11", "chr_linlan",
                   12, sha("1")),
            _entry("costume:cst_coat", "costume", "S12", "cst_coat", 2, sha("2")),
            _entry("prop:prp_letter", "prop", "S14", "prp_letter", 1, sha("3")),
            _entry("scene:scn_hall", "scene", "S13", "scn_hall", 4, sha("4")),
        ]
    return {
        "binding_set_id": "bs_0001",
        "revision": 2,
        "state": "active",
        "content_hash": content_hash or sha("b"),
        "payload": {"entries": entries},
        "entry_count": len(entries),
    }


def _entry(role_key, asset_type, owner_service, owner_local_id, owner_revision,
           owner_hash):
    return {
        "scope_kind": "shot",
        "scope_id": "sht_0001",
        "role_key": role_key,
        "asset_type": asset_type,
        "owner_service": owner_service,
        "owner_local_id": owner_local_id,
        "owner_revision": owner_revision,
        "owner_hash": owner_hash,
        "rights_ref": {"rights_id": "rgt_1", "revision": 1},
        "variant_selector": None,
        "resolved_from": "direct",
        "required": True,
    }


def make_capability(*, claims=None, tiers=(4, 5, 8), aspect="16:9",
                    modes=None, prompt_max=4000) -> dict:
    if modes is None:
        modes = [{
            "mode": "t2v",
            "support": "supported",
            "execution_kind": "async",
            "prompt_required": True,
            "prompt_max_chars": prompt_max,
            "output_specs": [{
                "aspect_ratio": aspect,
                "max_width": 1920,
                "max_height": 1080,
                "duration_tiers_s": list(tiers),
                "trims_to_nearest": True,
            }],
            "capability_claims": claims if claims is not None else [
                {"capability_key": "camera.shot_size", "support": "documented",
                 "params": {}, "evidence_id": "ev_1"},
            ],
        }]
    return {
        "snapshot_id": "cap_0001",
        "provider": "volcengine",
        "model_id": "seedance-1.0-pro",
        "model_snapshot": "seedance-1.0-pro@2026-10",
        "mode_details": modes,
    }


ADAPTER = {
    "adapter_id": "adb_seedance_video",
    "revision": 3,
    "provider": "volcengine",
    "model_snapshot": "seedance-1.0-pro@2026-10",
    "reference_token_format": "<image_{n}>",
    "token_budget": 8000,
    "reference_order_rule": {
        "roles": [
            {"role": "character", "position": 0},
            {"role": "costume", "position": 1},
            {"role": "prop", "position": 2},
            {"role": "scene", "position": 3},
        ],
        "allow_leftover": False,
    },
    "phrase_tables": {
        "camera.shot_size": {
            "medium": {"text": "中景", "phrase_id": "ph_medium"},
            "wide": {"text": "远景", "phrase_id": "ph_wide"},
        },
    },
    "speaker_prefix": "{speaker}：",
    "section_separator": "\n",
    "field_map": {},
    "unsupported_optionals": [],
}

TEMPLATE_BODY = {
    "modality": "video",
    "sections": [
        {"id": "purpose", "from": "narrative_purpose", "render": "verbatim"},
        {"id": "size", "from": "camera.shot_size", "render": "phrase_table"},
        {"id": "dialogue", "from": "dialogue", "render": "verbatim"},
        {"id": "negative",
         "text": "{{#negative}}模糊，水印，多余的手指{{/negative}}"},
    ],
    "negative": ["模糊", "水印"],
}


class Harness:
    """内核 + 幂等 + 全套假上游。"""

    def __init__(self, *, conn=None, shot=None, binding=None, capability=None,
                 alignment=None, regression=None):
        self.conn = conn or sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.idem = SqliteIdempotencyStore(self.conn)
        self.shot_payload = {
            "shot_spec": shot or make_shot(),
            "s06_meta": {"shot_lineage_id": "lin_1", "branch_id": "br_main",
                         "state": "accepted", "content_hash": sha("s"),
                         "sealed_at": "2026-10-01T00:00:00.000Z"},
        }
        self.binding_payload = binding or make_binding_set()
        self.capability_payload = capability or make_capability()
        self.alignment_payload = alignment or {
            "report_id": "alr_0001", "revision": 1,
            "units": [{"line_id": "ln_1", "start_ms": 100, "end_ms": 900}]}
        self.regression_payload = regression
        self.calls: list = []
        self.shot_calls = 0
        self.alignment_source = self._alignment
        self.service = PromptCompilerService(
            self.conn,
            shot_source=self._shot,
            binding_source=self._binding,
            capability_source=self._capability,
            alignment_source=self.alignment_source,
            regression_source=(self._regression
                               if regression is not None else None),
        )

    # ---- 桩
    def _shot(self, shot_id, revision):
        self.shot_calls += 1
        self.calls.append(("shot", shot_id, revision))
        return self.shot_payload

    def _binding(self, binding_set_id, revision):
        self.calls.append(("binding", binding_set_id, revision))
        return self.binding_payload

    def _capability(self, snapshot_id):
        self.calls.append(("capability", snapshot_id))
        return self.capability_payload

    def _alignment(self, report_id, *, start=None, end=None):
        self.calls.append(("alignment", report_id, start, end))
        return self.alignment_payload

    def _regression(self, ref):
        self.calls.append(("regression", ref))
        return self.regression_payload

    # ---- 便捷
    def template(self, *, template_id="tpl_shot_video", body=None,
                 provider="volcengine", modality="video") -> dict:
        return self.service.create_template(
            TENANT, f"cmd_tpl_{template_id}", {
                "template_id": template_id, "name": "镜头视频模板",
                "modality": modality, "provider": provider}, "h")

    def publish(self, *, template_id="tpl_shot_video", body=None,
                parent_revision=0, snapshots=None, min_rev=1, max_rev=5,
                scopes=None, command_id=None, tenant=TENANT) -> dict:
        payload = {
            "parent_revision": parent_revision,
            "compatible_schema": {"id": "shot-spec", "revision": 1,
                                  "content_hash": sha("c")},
            "body": body or TEMPLATE_BODY,
            "model_snapshots": snapshots or ["seedance-1.0-pro@2026-10"],
            "adapter_revision_min": min_rev,
            "adapter_revision_max": max_rev,
            "adapter_scopes": scopes if scopes is not None else [
                ADAPTER["adapter_id"]],
            "regression_set_ref": {"id": "rgs_1", "revision": 1,
                                   "content_hash": sha("r")},
            "author_subject": "author_1",
        }
        return self.service.publish_revision(
            tenant, command_id or f"cmd_pub_{parent_revision}", template_id,
            payload, "h")

    def adapter(self, **over) -> dict:
        payload = {**ADAPTER, **over}
        return self.service.put_adapter(
            TENANT, f"cmd_adb_{payload['adapter_id']}_{payload['revision']}",
            payload, "h")

    def ready(self, *, with_adapter=True, with_template=True) -> dict:
        if with_template:
            self.template()
        if with_adapter:
            self.adapter()
        if with_template:
            self.publish()
        return {"template_id": "tpl_shot_video", "revision": 1}

    def compile_body(self, **over) -> dict:
        body = {
            "target": {
                "template_ref": {"id": "tpl_shot_video", "revision": 1},
                "shot_ref": {"service_id": "S06", "id": "sht_0001",
                             "revision": 3},
                "model_snapshot": "seedance-1.0-pro@2026-10",
                "capability_snapshot_ref": {"id": "cap_0001"},
                "execution_context": {"node_id": "nd_1", "node_epoch": 1,
                                      "execution_fence": 1},
            },
            "binding_set_ref": {"id": "bs_0001", "revision": 2},
        }
        for key, value in over.items():
            if isinstance(value, dict) and isinstance(body.get(key), dict):
                body[key] = {**body[key], **value}
            else:
                body[key] = value
        return body

    def compile(self, *, command_id="cmd_compile_1", tenant=TENANT, **over):
        return self.service.compile(
            tenant, command_id, self.compile_body(**over), "h")

    def manifest_count(self, tenant=TENANT) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM compiled_prompt_manifest WHERE tenant_id=?",
            (tenant,)).fetchone()[0]

    def outbox_rows(self) -> list:
        return self.conn.execute(
            "SELECT * FROM outbox ORDER BY created_at").fetchall()

    def template_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM template").fetchone()[0]

    def revision_count(self, state=None) -> int:
        if state:
            return self.conn.execute(
                "SELECT count(*) FROM template_revision WHERE state=?",
                (state,)).fetchone()[0]
        return self.conn.execute(
            "SELECT count(*) FROM template_revision").fetchone()[0]


# ---------------------------------------------------------------- HTTP 夹具

def _http_client(monkeypatch, tmp_path, **over):
    monkeypatch.setenv("S20_DB", str(tmp_path / "s20.db"))
    path = ROOT / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s20_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s20_api_app"] = module
    h = Harness(conn=module._conn, **over)
    module.service = h.service
    module._idem = h.idem
    return TestClient(module.app), h, module


def post(client, path, body, *, command_id="c1", tenant=TENANT, **kw):
    return client.post(path, json=body,
                       headers={"Idempotency-Key": command_id,
                                "X-Tenant-Id": tenant}, **kw)


def get(client, path, *, tenant=TENANT):
    return client.get(path, headers={"X-Tenant-Id": tenant})


# =========================================================== 0. 冒烟

def test_t20_00_compile_smoke():
    """一次完整编译能走通，且产物形状是契约里那些字段。"""
    h = Harness()
    h.ready()
    out = h.compile()
    assert out["manifest_id"].startswith("cpm_")
    assert out["revision"] == 1
    assert out["modality"] == "video"
    assert len(out["content_hash"]) == 64
    int(out["content_hash"], 16)  # 是 hex，不是随便 64 个字符
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert manifest["shot_ref"]["id"] == "sht_0001"
    assert manifest["model_snapshot"] == "seedance-1.0-pro@2026-10"
    assert manifest["prompt_text"]
    assert [ref["position"] for ref in manifest["references"]] == [0, 1, 2, 3]
    assert [ref["token"] for ref in manifest["references"]] == [
        "<image_1>", "<image_2>", "<image_3>", "<image_4>"]
    assert manifest["duration"]["provider_tier_seconds"] == 4
    assert manifest["dialogue_segments"][0]["text"] == "我回来了。"


# =========================================================== 1. 确定性（T20-01…08）

def test_t20_01_same_inputs_one_hundred_times_same_hash():
    """T20-01：100 次编译同一输入，`content_hash` 全等。

    这不是"代码里没写 random"的静态断言，而是跑出来的：任何一处集合遍历、
    `repr`、时间格式化都会在这里暴露。
    """
    h = Harness()
    h.ready()
    hashes = {h.compile(command_id=f"cmd_{i}")["content_hash"]
              for i in range(100)}
    assert len(hashes) == 1
    # 第二层去重也不能漏：100 次只该有 1 份 manifest。
    assert h.manifest_count() == 1


def test_t20_02_two_deployments_compile_the_same_hash():
    """T20-02：同一输入在两个区域编译 ⇒ `content_hash` 相同。

    S20 没有任何按区域分叉的状态（没有 region 配置、没有本地化的短语表、
    adapter 由 `(provider, model_snapshot)` 决定）。所以"两个区域"在这里
    等于"两个互不相干的实例各建一份库"。若将来有人加了区域分支，这条会红。
    """
    hashes = []
    for _ in range(2):
        h = Harness()
        h.ready()
        hashes.append(h.compile()["content_hash"])
    assert hashes[0] == hashes[1]


def test_t20_03_cold_start_after_reopening_the_database(tmp_path):
    """T20-03：跨进程（冷启动）编译 ⇒ `content_hash` 相同。

    冷启动 = 关掉连接、丢掉全部内存态，用新连接重建 service 再编译。这条会
    抓住"把算出来的东西缓存进进程、重启后从库里读不回来"那类缺陷。
    """
    path = str(tmp_path / "s20.db")
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    warm = Harness(conn=conn)
    warm.ready()
    first = warm.compile(command_id="cmd_a")
    conn.close()

    conn2 = sqlite3.connect(path, check_same_thread=False,
                            isolation_level=None)
    conn2.row_factory = sqlite3.Row
    cold = Harness(conn=conn2)
    second = cold.compile(command_id="cmd_b")
    conn2.close()
    assert first["content_hash"] == second["content_hash"]
    assert first["manifest_id"] == second["manifest_id"]
    assert second["deduplicated"] is True


def test_t20_04_timezone_and_locale_do_not_change_the_hash(monkeypatch):
    """T20-04：改系统时区 / `locale` ⇒ `content_hash` 不变。

    编译路径里只允许 `_now()` 一处时间，且它不进 hash；也没有本地化的
    `strftime` 格式、没有 `locale.strcoll`。这条用真的环境变量改。
    """
    h = Harness()
    h.ready()
    baseline = h.compile(command_id="cmd_a")["content_hash"]

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    monkeypatch.setenv("LANG", "tr_TR.UTF-8")
    assert h.compile(command_id="cmd_b")["content_hash"] == baseline


def test_t20_05_fake_clock_changes_compiled_at_not_the_hash(monkeypatch):
    """T20-05：注入假时钟 ⇒ `content_hash` 不变，`compiled_at` 跟着变。

    两件事分开证：投影里没有 `compiled_at`（直接扰动字段重算），
    而 `compiled_at` 确实来自 `_now_dt()`（时钟往后拨，新 manifest 往后）。
    """
    import datetime

    import prompt_compiler.service as svc

    h = Harness()
    h.ready()
    real = svc._now_dt
    first = h.compile(command_id="cmd_a")
    m1 = h.service.get_manifest(TENANT, first["manifest_id"])

    monkeypatch.setattr(
        svc, "_now_dt", lambda: real() + datetime.timedelta(hours=5))
    # 同输入第二次：落到同一份 manifest，hash 不变。
    again = h.compile(command_id="cmd_b")
    assert again["content_hash"] == first["content_hash"]
    # 换一份输入才会新写 manifest，用来看 compiled_at 是否跟着时钟走。
    h.shot_payload["shot_spec"] = make_shot(emotion="tense")
    h.shot_payload["s06_meta"] = {**h.shot_payload["s06_meta"],
                                  "content_hash": sha("9")}
    later = h.compile(command_id="cmd_c")
    m2 = h.service.get_manifest(TENANT, later["manifest_id"])
    assert m2["compiled_at"] > m1["compiled_at"]

    # 直接证投影：只改 compiled_at，hash 不动。
    probe = {**m1, "compiled_at": "2099-01-01T00:00:00.000Z"}
    assert manifest_content_hash(probe) == first["content_hash"]


def test_t20_06_shuffled_rule_key_order_changes_nothing():
    """T20-06：打乱 `reference_order_rule` 的源 JSON 键序 ⇒ 顺序结果不变。

    存的是**数组**（§3.4 第 1 条），但每个元素是对象；对象键序不同必须解析
    成同一个规则。两个入参在字节上不同，编译结果必须相同。
    """
    def rule(reversed_keys: bool) -> dict:
        roles = [{"role": "character", "position": 0},
                 {"role": "costume", "position": 1},
                 {"role": "prop", "position": 2},
                 {"role": "scene", "position": 3}]
        if reversed_keys:
            roles = [{"position": r["position"], "role": r["role"]}
                     for r in roles]
        return {"roles": roles, "allow_leftover": False}

    outs = []
    for reversed_keys in (False, True):
        h = Harness()
        h.adapter(reference_order_rule=rule(reversed_keys))
        h.template()
        h.publish()
        out = h.compile()
        m = h.service.get_manifest(TENANT, out["manifest_id"])
        outs.append((out["content_hash"],
                     [(r["position"], r["role"]) for r in m["references"]]))
    assert outs[0][1] == [(0, "character"), (1, "costume"),
                          (2, "prop"), (3, "scene")]
    assert outs[0] == outs[1]


def test_t20_07_determinism_guard_sees_an_impure_renderer(monkeypatch):
    """T20-07：**守门测试** —— 故意注入非纯函数，T20-01 的断言必须变红。

    这条不是产品测试，它验的是"确定性检查本身看得见破坏"。做法是给渲染
    结果每段末尾追加一个随机字面量再跑 100 次；如果 hash 仍然全等，说明前面
    那些 100 次检查什么也没检查。

    不能用 `_stringify` 注入：它同时是词表查表的键，加了随机后缀会先变成
    `PHRASE_NOT_FOUND`，测的就不是确定性了。`_emit` 是"片段 + 来源"的
    出口，往这里加一个字面量片段仍然是**合法**的渲染结果（溯源连续），
    只有 hash 会变 —— 正是 T20-01 应当看见的那种破坏。
    """
    import random

    import prompt_compiler.render as render

    real = render._emit

    def impure(nodes, **kwargs):
        pieces = real(nodes, **kwargs)
        pieces.append((str(random.random()), {"kind": "template_literal"}))
        return pieces

    monkeypatch.setattr(render, "_emit", impure)
    h = Harness()
    h.ready()
    hashes = {h.compile(command_id=f"cmd_{i}")["content_hash"]
              for i in range(100)}
    # 注入随机数后 100 次不再收敛 —— 这正是 T20-01 会失败的样子。
    assert len(hashes) > 1


def test_t20_08_two_command_ids_one_manifest_one_replay():
    """T20-08：不同 `command_id`、相同输入 ⇒ 同一 manifest，其中一次 replay。

    这与幂等键不同层：幂等键去的是"同一命令重投"，这里是"两个命令但输入
    相同"。方案 §6.2 的第二层。
    """
    h = Harness()
    h.ready()
    first = h.compile(command_id="cmd_a")
    second = h.compile(command_id="cmd_b")
    assert first["manifest_id"] == second["manifest_id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert h.manifest_count() == 1


def test_manifest_content_hash_covers_warnings():
    """§4.1：`warnings` 进 `content_hash`。

    plan 的 T20 表没有单独一行，但 §4.1 明文要求：带 `OPTIONAL_OMITTED` 的
    产物与不带的不是同一份产物，否则"悄悄少了一段"会与"完整"共用 manifest。
    """
    base = Harness()
    base.ready()
    clean = base.compile(command_id="cmd_a")

    warned = Harness()
    warned.ready()
    warned.capability_payload = make_capability(claims=[])
    warned.conn.execute("UPDATE adapter_binding SET unsupported_optionals=?",
                        (json.dumps(["camera.shot_size"]),))
    warned.conn.execute(
        "UPDATE template_revision SET body=?",
        (json.dumps(_body_with_optional(), ensure_ascii=False),))
    out = warned.compile(command_id="cmd_a")
    assert clean["content_hash"] != out["content_hash"]


def _body_with_optional() -> dict:
    return {
        "modality": "video",
        "sections": [
            {"id": "purpose", "from": "narrative_purpose",
             "render": "verbatim"},
            {"id": "size", "from": "camera.shot_size",
             "render": "phrase_table", "optional": True,
             "requires": ["camera.shot_size"]},
            {"id": "dialogue", "from": "dialogue", "render": "verbatim"},
        ],
        "negative": ["模糊"],
    }


# =========================================================== 2. 对白（T20-09…17）

def test_t20_09_dialogue_is_verbatim_in_prompt():
    """T20-09：单人对白 ⇒ `dialogue_segments[0].text` 与原句逐字节相等。"""
    h = Harness()
    h.ready()
    out = h.compile()
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert manifest["dialogue_segments"][0]["text"] == "我回来了。"
    assert "我回来了。" in manifest["prompt_text"]


def test_t20_10_multi_line_dialogue_keeps_every_byte_and_punctuation():
    """T20-10：多句对白、含标点 ⇒ 每句逐字节相等，标点不变。

    S20 只做"顺序拼接"：不分句、不加句号、不合并标点。这条同时证明
    `dialogue_segments` 是逐句的，不是拼成一段再切回来。
    """
    lines = ["我回来了。", "啊？你……怎么在这？", "别动——"]
    h = Harness()
    h.shot_payload["shot_spec"] = make_shot(dialogue=[
        {"line_id": f"ln_{i}", "character_id": "chr_linlan", "text": text,
         "emotion": "calm"} for i, text in enumerate(lines, 1)])
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert [s["text"] for s in m["dialogue_segments"]] == lines
    for text in lines:
        assert text in m["prompt_text"]
    # 每段还带自己的 sha256 —— 下游不必信编译器，可以自己核。
    for seg, text in zip(m["dialogue_segments"], lines):
        assert seg["text_sha256"] == hashlib.sha256(
            text.encode("utf-8")).hexdigest()


def test_t20_11_fillers_are_not_removed():
    """T20-11：含语气词（"呃""啊"）⇒ 不被删除。

    去掉语气词是"润色"，属于 §1.2 明令禁止的改写。它们看着像噪声，
    实际是演员的表演指令。
    """
    line = "呃……我、我回来了啊。"
    h = Harness()
    h.shot_payload["shot_spec"] = make_shot(dialogue=[
        {"line_id": "ln_1", "character_id": "chr_linlan", "text": line,
         "emotion": "nervous"}])
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert m["dialogue_segments"][0]["text"] == line
    assert "呃" in m["prompt_text"] and "啊" in m["prompt_text"]


def test_t20_12_template_string_dialogue_and_truncation_are_drift():
    """T20-12：模板把对白放进 `template_string` 段、并截断 ⇒ 422 `DIALOGUE_TEXT_DRIFT`。

    行里有两件事，都要证明：

    1. **通道**：`{{ dialogue[0].text }}` 输出的是 `template_string` 而不是
       `verbatim`，字节可能完全正确，仍然拒绝 —— 因为那条路以后可以加格式化。
    2. **字节**：`verbatim` 区间的字节与原文不等（截断）也拒绝。

    渲染器自己不会截断，所以第 2 条只能从守门员那一侧验：手工构造一个
    「区间短一个字」的定稿文本，直接调用 `_step_dialogue`。若它只检查
    "原文是否出现在 prompt 里"，前缀仍在，会漏。
    """
    # 1. 通道：走真正的编译路径，模板用普通插值输出对白。
    h = Harness()
    h.adapter()
    h.template()
    h.publish(body={
        "modality": "video",
        "sections": [
            {"id": "purpose", "from": "narrative_purpose",
             "render": "verbatim"},
            {"id": "line", "from": "dialogue",
             "text": "{{ dialogue[0].text }}"},
        ],
        "negative": []})
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "DIALOGUE_TEXT_DRIFT"
    assert exc.value.status == 422
    assert exc.value.details["reason"] == "DIALOGUE_NOT_VERBATIM"
    assert h.manifest_count() == 0

    # 2. 字节：verbatim 区间被截断。
    source = "我回来了。"
    prompt_text = "我回来了"
    derived = [{"span": [0, 4], "kind": "verbatim", "path": "dialogue[0].text",
                "line_id": "ln_1"}]
    shot = {"dialogue": [{"line_id": "ln_1", "character_id": "chr_linlan",
                          "text": source}]}
    with pytest.raises(ServiceError) as exc2:
        PromptCompilerService._step_dialogue(shot, prompt_text, derived)
    assert exc2.value.code == "DIALOGUE_TEXT_DRIFT"
    assert exc2.value.status == 422
    assert exc2.value.details["reason"] == "DIALOGUE_BYTES_DIFFER"
    # 也证明它比对的是字节而不是"原文是否出现"：原文的前缀就在那里。
    assert source[:4] == prompt_text


def test_t20_13_dialogue_through_join_is_drift():
    """T20-13：模板用 `join()` 合并两条对白 ⇒ 422 `DIALOGUE_TEXT_DRIFT`。

    字节可能完全正确，但 `join` 是一条**未来会被格式化**的路（换分隔符、
    加句号），对白就从这条门出去了。所以这是通道校验，不是字节校验。
    """
    h = Harness()
    h.adapter()
    h.template()
    h.publish(body={
        "modality": "video",
        "sections": [
            {"id": "purpose", "from": "narrative_purpose",
             "render": "verbatim"},
            {"id": "lines", "from": "dialogue",
             "text": '{{ join(dialogue[*].text, "，") }}'},
        ],
        "negative": []})
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "DIALOGUE_TEXT_DRIFT"
    assert exc.value.status == 422
    assert exc.value.details["reason"] == "DIALOGUE_NOT_VERBATIM"
    assert h.manifest_count() == 0


def test_t20_14_translation_present_but_not_requested_stays_out():
    """T20-14：镜头带译文但编译请求没引用 ⇒ prompt 里只有原句。

    S20 不"顺手"把译文拼进去：译文是另一份带自己 revision 的事实，混进来
    会让 `content_hash` 无法区分"只编了原句"和"原句+译文"。
    """
    h = Harness()
    shot = make_shot()
    shot["dialogue"][0]["translations"] = [
        {"lang": "en", "text": "I am back.", "revision": 2}]
    h.shot_payload["shot_spec"] = shot
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert "我回来了。" in m["prompt_text"]
    assert "I am back." not in m["prompt_text"]
    assert [s["text"] for s in m["dialogue_segments"]] == ["我回来了。"]


def test_t20_15_explicit_translation_reference_has_no_contract_field():
    """T20-15：有译文且**显式引用** ⇒ 原句与译文都在，各自带 revision。

    **未实现**，登记为 S20-27：编译请求契约里没有引用译文的字段（§2.4 的
    请求体没有 `translation_refs`）。这条证明的是"契约里确实没有这个入口"，
    而不是"实现了它" —— 写一个能过的假断言比老实留空更坏。
    """
    assert_gap("S20-27")
    from prompt_compiler.schema import CONTRACT_GAPS

    assert any(g["id"] == "S20-27" for g in CONTRACT_GAPS)
    h = Harness()
    h.ready()
    body = h.compile_body()
    # 没有任何合法字段能点名一份译文；拼进去只会是未知键。
    assert "translation_refs" not in body
    assert "dialogue_translation_ref" not in body
    out = h.service.compile(TENANT, "cmd_a", body, "h")
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert "dialogue_translations" not in m


def test_t20_16_over_max_chars_is_refused_not_truncated():
    """T20-16：长对白超出 `max_chars` ⇒ 422 `RENDER_LIMIT_EXCEEDED`，**不截断**。

    截断是对白的改写，而且是静默的：产物看起来没错，只是少了半句。所以这里
    断言三件事：码、`details` 里有实际值与上限、零 manifest。
    """
    h = Harness()
    h.template()
    h.adapter()
    h.publish(body={**TEMPLATE_BODY, "max_chars": 20})
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "RENDER_LIMIT_EXCEEDED"
    assert exc.value.status == 422
    assert exc.value.details["kind"] == "max_chars"
    assert exc.value.details["actual"] > exc.value.details["limit"] == 20
    assert h.manifest_count() == 0


def test_t20_17_emoji_combining_and_bidi_are_byte_identical():
    """T20-17：含 emoji / 组合字符 / 双向文本 ⇒ 逐字节相等。

    这三类是"看起来一样、字节不同"的经典来源：ZWJ 序列（家庭 emoji）、
    组合变音符、以及 RTL 控制符。任何 normalization（NFC/NFD）都会改写
    对白，所以必须比字节而不是比"显示效果"。
    """
    line = "👩‍👩‍👧 مرحبا é ‫عربي‬"
    h = Harness()
    h.shot_payload["shot_spec"] = make_shot(dialogue=[
        {"line_id": "ln_1", "character_id": "chr_linlan", "text": line,
         "emotion": "calm"}])
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert m["dialogue_segments"][0]["text"] == line
    assert m["dialogue_segments"][0]["text"].encode("utf-8") == \
        line.encode("utf-8")
    assert line.encode("utf-8") in m["prompt_text"].encode("utf-8")
    # 组合字符仍是组合形式（没被 NFC 合成）：长度与源串一致。
    assert len(m["prompt_text"]) >= len(line)


def test_t20_17b_verbatim_text_is_not_trimmed_or_escaped():
    """§4.2 第 1 条：对白含前后空格与引号时逐字节保留（不 trim、不转义）。"""
    h = Harness()
    line = ' 他说"别动"。 '
    h.shot_payload["shot_spec"] = make_shot(dialogue=[{
        "line_id": "ln_1", "character_id": "chr_linlan", "text": line,
        "emotion": "calm"}])
    h.shot_payload["s06_meta"] = {**h.shot_payload["s06_meta"],
                                  "content_hash": sha("9")}
    h.ready()
    out = h.compile()
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert line in manifest["prompt_text"]
    assert manifest["dialogue_segments"][0]["text"] == line


# =========================================================== 3. 溯源（T20-18…22）

def test_t20_18_every_phrase_mapping_carries_a_phrase_id():
    """T20-18：枚举值全部命中词表 ⇒ 每条映射都有 `phrase_id`。

    光有文本不够：下游要靠 `phrase_id` 知道"这个'中景'是词表里的哪一条"，
    否则审计只能比字符串，改一个同义短语就查不出来。
    """
    h = Harness()
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    phrases = [e for e in m["derived_from"] if e["kind"] == "phrase_table"]
    assert phrases
    for entry in phrases:
        assert entry["phrase_id"] == "ph_medium"
        assert entry["path"] == "camera.shot_size"
        assert m["prompt_text"][entry["span"][0]:entry["span"][1]] == "中景"


def test_t20_19_unknown_enum_is_refused_not_invented():
    """T20-19：枚举值未命中词表 ⇒ 422，不臆造短语。

    §7 没给这一种分配码（S20-17），所以用 `REQUIRED_FIELD_UNSUPPORTED` +
    `details.reason='PHRASE_NOT_FOUND'`。关键断言是**没有回退成原文**。
    """
    h = Harness()
    h.shot_payload["shot_spec"] = make_shot(
        camera={"shot_size": "extreme_close", "movement": "static"})
    h.ready()
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "REQUIRED_FIELD_UNSUPPORTED"
    assert exc.value.status == 422
    assert exc.value.details["reason"] == "PHRASE_NOT_FOUND"
    assert exc.value.details["value"] == "extreme_close"
    assert h.manifest_count() == 0


def test_t20_20_undeclared_interpolation_is_unsourced_completion():
    """T20-20：模板写了未声明路径的插值 ⇒ 422 `UNSOURCED_COMPLETION`。

    这是实现缺陷的守门员：正文里出现了数据，却没有任何段声明它的来源。
    两层都存在 —— 发布时的内置回归先拒（`path_declared`），编译期 ⑤ 再拒
    一次。这里绕过发布层直接改库里已发布的 revision，专门验证第二层。
    """
    h = Harness()
    h.ready()
    body = json.loads(json.dumps(TEMPLATE_BODY))
    body["sections"][0] = {"id": "purpose", "text": "{{ camera.movement }}"}
    h.conn.execute("UPDATE template_revision SET body=?",
                   (json.dumps(body, ensure_ascii=False),))
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "UNSOURCED_COMPLETION"
    assert exc.value.status == 422
    assert exc.value.details["path"] == "camera.movement"
    assert h.manifest_count() == 0


def test_t20_20b_publish_regression_rejects_undeclared_paths():
    """同一缺口在发布门（§4.4 的 `path_declared` 断言）上也被拒。"""
    h = Harness()
    h.template()
    h.adapter()
    body = json.loads(json.dumps(TEMPLATE_BODY))
    body["sections"][0] = {"id": "purpose", "text": "{{ camera.movement }}"}
    with pytest.raises(ServiceError) as exc:
        h.publish(body=body)
    assert exc.value.code == "REGRESSION_NOT_PASSED"
    assert exc.value.status == 422
    assert exc.value.details["report"]["verdict"] == "fail"
    assert h.revision_count() == 0


def test_t20_21_derived_from_covers_entire_prompt():
    """T20-21：`derived_from` 覆盖 `prompt_text` 的全部字符，无空隙。

    模板字面量也算覆盖（`kind == template_literal`），否则"把数据直接写死在
    模板里"就能绕过溯源。
    """
    h = Harness()
    h.ready()
    out = h.compile()
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    entries = manifest["derived_from"]
    assert entries
    cursor = 0
    for entry in sorted(entries, key=lambda e: e["span"][0]):
        assert entry["span"][0] == cursor
        cursor = entry["span"][1]
    assert cursor == len(manifest["prompt_text"])
    assert any(e["kind"] == "template_literal" for e in entries)


def test_t20_22_manifest_traces_back_to_shot_spec_fields():
    """T20-22：反向验证 —— manifest 里每个非字面量片段都能回指 ShotSpec 字段。

    正向（T20-21）是"每个字符有来源"；反向是"每个来源确实存在于 ShotSpec"。
    只做正向时，模板可以凭空捏一个 `path`，覆盖仍然连续。
    """
    from prompt_compiler.render import get_path

    h = Harness()
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    shot = h.shot_payload["shot_spec"]
    traced = [e for e in m["derived_from"] if e.get("path")]
    assert traced
    for entry in traced:
        found, value = get_path(shot, entry["path"])
        assert found, f"{entry['path']} does not exist in ShotSpec"
        rendered = m["prompt_text"][entry["span"][0]:entry["span"][1]]
        if entry["kind"] == "verbatim":
            assert rendered == value
        elif entry["kind"] == "phrase_table":
            assert entry["phrase_id"]


# =========================================================== 4. 参考图（T20-23…29）

def test_t20_23_positions_are_consecutive_and_tokens_match():
    """T20-23：position 依次 0/1/2，token 编号与 position 一致。

    S20-46：§3.4 的散文说"编号就是顺序位置"（会给出 `<image_0>`），§4.1 的
    示例是 `position: 0` 配 `<image_1>`。两处冲突，这里按示例取
    `position + 1` —— 示例是唯一给了具体字节的一处；若 owner 裁定相反，是
    一行改动。
    """
    h = Harness()
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert [r["position"] for r in m["references"]] == [0, 1, 2, 3]
    assert [r["token"] for r in m["references"]] == [
        "<image_1>", "<image_2>", "<image_3>", "<image_4>"]
    for ref in m["references"]:
        assert ref["token"] == f"<image_{ref['position'] + 1}>"


def test_t20_24_style_in_last_position_is_unimplemented():
    """T20-24 在方案里的期望（"style 在末位 ⇒ position 最大"）**做不到**。

    S20-13：`ShotSpec.asset_bindings.style` 存在，但 S10 的 `ASSET_TYPES`
    只有 character/costume/scene/prop/voice/adaptation —— 没有 `style`，所以
    风格图根本进不了绑定集，S20 也就拿不到它的 artifact。没有 artifact 就
    没有 `position`，"style 的 position 是最大值"是一个无法成立的断言 ——
    这条测试的第一段先钉住这个缺口确实登记在册。

    实现的选择是**报错**而不是"跳过 style 继续排"：静默跳过会让模型以为
    `<image_4>` 是风格图，实际那是场景。这条测试钉住的是这一点，同时它也是
    S20-13 的可执行形式 —— owner 补上 `style` 类型后这条会变成绿灯并被删。
    """
    assert any(g["id"] == "S20-13" for g in CONTRACT_GAPS)
    # 绑定集里根本没有 `style` 这一类条目 —— 所以不是"实现偷懒不排它"。
    assert not any(e["asset_type"] == "style"
                   for e in make_binding_set()["payload"]["entries"])

    h = Harness()
    h.adapter(reference_order_rule={
        "roles": [{"role": "character", "position": 0},
                  {"role": "style", "position": 9}],
        "allow_leftover": False})
    h.template()
    h.publish()
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "REFERENCE_ORDER_UNRESOLVABLE"
    assert exc.value.status == 422
    assert exc.value.details["role"] == "style"
    assert exc.value.details["position"] == 9
    assert h.manifest_count() == 0


def test_t20_25_same_role_two_images_sorted_by_hint_then_id():
    """T20-25：同角色两张图，先按 `order_hint`，再按 `global_asset_id`。

    端到端部分：ShotSpec 里 props 的书写顺序变成 `order_hint`
    （`_shot_asset_roles` 用数组下标），所以两张道具图的先后由 ShotSpec 决定、
    且可重复。
    """
    h = Harness()
    h.adapter(reference_order_rule=[
        {"role": "prop", "position": 0},
        {"role": "character", "position": 1},
        {"role": "costume", "position": 2},
        {"role": "scene", "position": 3},
    ])
    h.template()
    h.publish()
    h.shot_payload["shot_spec"] = make_shot(asset_bindings={
        **make_shot()["asset_bindings"],
        "props": [{"id": "prp_z", "revision": 1},
                  {"id": "prp_a", "revision": 1}],
    })
    entries = [dict(e) for e in make_binding_set()["payload"]["entries"]
               if e["asset_type"] != "prop"]
    entries.append(_entry("prop:prp_z", "prop", "S14", "prp_z", 1, sha("5")))
    entries.append(_entry("prop:prp_a", "prop", "S14", "prp_a", 1, sha("6")))
    h.binding_payload = make_binding_set(entries=entries)
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    ids = [r["global_asset_id"] for r in m["references"]]
    assert ids[:2] == ["prop:prp_z", "prop:prp_a"]
    assert [r["position"] for r in m["references"]] == [0, 1, 2, 3, 4]

    # 末级键：order_hint 相同时按 global_asset_id 升序。这一半只能直接调
    # `order_references` —— 走 ShotSpec 时 hint 来自数组下标，永远不会相等。
    from prompt_compiler import order_references

    same_hint = [
        {"global_asset_id": "prop:prp_z", "role": "prop", "order_hint": 200,
         "artifact_ref": {"service_id": "S14", "id": "prp_z", "revision": 2,
                          "content_hash": sha("5")}},
        {"global_asset_id": "prop:prp_a", "role": "prop", "order_hint": 200,
         "artifact_ref": {"service_id": "S14", "id": "prp_a", "revision": 9,
                          "content_hash": sha("6")}},
    ]
    ordered, _ = order_references(
        same_hint, [{"role": "prop", "position": 0}],
        adapter_id="adb_x", token_format="<image_{n}>")
    assert [r["global_asset_id"] for r in ordered] == [
        "prop:prp_a", "prop:prp_z"]
    # 反向输入得到同一结果 —— 排序不是"保持输入顺序"。
    reversed_ordered, _ = order_references(
        list(reversed(same_hint)), [{"role": "prop", "position": 0}],
        adapter_id="adb_x", token_format="<image_{n}>")
    assert [r["global_asset_id"] for r in reversed_ordered] == [
        "prop:prp_a", "prop:prp_z"]


def test_t20_26_missing_required_role_is_order_unresolvable():
    """T20-26 / S20-44：adapter 点名了必需角色而绑定集没有 ⇒ 422 F9。

    这里缺的是 `prop`：adapter 规则里有 `position:2` 且未标
    `required:false`，所以是"adapter 与绑定集不匹配"，修其中之一。
    """
    h = Harness()
    h.ready()
    entries = [e for e in make_binding_set()["payload"]["entries"]
               if e["asset_type"] != "prop"]
    h.binding_payload = make_binding_set(entries=entries)
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "REFERENCE_ORDER_UNRESOLVABLE"
    assert exc.value.status == 422
    assert h.manifest_count() == 0


def test_t20_27_leftover_reference_is_appended_with_warning():
    """T20-27：规则没点名的角色，在 `allow_leftover` 下追加 + warning。

    反面：静默丢弃 —— 那会让模板引用的图在 prompt 里没有对应 token，
    下游按 token 取图时拿到错的一张。
    """
    h = Harness()
    h.adapter(reference_order_rule={
        "roles": [{"role": "character", "position": 0},
                  {"role": "scene", "position": 1}],
        "allow_leftover": True})
    h.template()
    h.publish()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert [r["role"] for r in m["references"]] == [
        "character", "scene", "costume", "prop"]
    assert [r["position"] for r in m["references"]] == [0, 1, 2, 3]
    assert [w["code"] for w in m["warnings"]].count(
        "UNMAPPED_REFERENCE_APPENDED") == 1


def test_t20_28_reference_order_is_part_of_content_hash():
    """T20-28：参考图顺序变了 ⇒ `content_hash` 不同。

    端到端造不出"只有顺序不同"的两份 manifest：顺序来自 adapter 的
    `reference_order_rule`，改了它 adapter 的 `content_hash` 也变（S20-50 还
    禁止同一 adapter 对同一快照发第二版）。所以这里对哈希投影做单元验证：
    取一份真实 manifest，只交换 `references[]` 的顺序，重算 hash。

    若 `manifest_content_hash` 漏掉 `references[]`（或漏掉 `position`/`token`），
    这条会失败 —— 而那意味着 `<image_1>` 换成另一张图时下游毫无察觉。
    """
    from prompt_compiler.service import manifest_content_hash

    h = Harness()
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert m["content_hash"] == manifest_content_hash(m)

    swapped = {**m, "references": [
        {**r, "position": len(m["references"]) - 1 - i,
         "token": f"<image_{len(m['references']) - i}>"}
        for i, r in enumerate(m["references"])]}
    assert manifest_content_hash(swapped) != m["content_hash"]

    # `position` 单独漏掉也要被抓到：只改位置、不动角色顺序。
    renumbered = {**m, "references": [
        {**r, "position": r["position"] + 1} for r in m["references"]]}
    assert manifest_content_hash(renumbered) != m["content_hash"]


def test_t20_29_artifact_revision_change_changes_hash():
    """T20-29：同一绑定集、两份 artifact revision 不同 ⇒ `content_hash` 不同。

    资产升版意味着"那张图换了"。prompt 文本可能一字未变，但产物不是同一份
    —— 下游按 manifest 派单时拿到的图不同。
    """
    h = Harness()
    h.ready()
    first = h.compile(command_id="cmd_a")
    entries = [dict(e) for e in make_binding_set()["payload"]["entries"]]
    entries[0] = {**entries[0], "owner_revision": 13, "owner_hash": sha("8")}
    h.binding_payload = make_binding_set(content_hash=sha("d"), entries=entries)
    second = h.compile(command_id="cmd_b", binding_set_ref={"revision": 3})
    assert first["content_hash"] != second["content_hash"]
    a = h.service.get_manifest(TENANT, first["manifest_id"])["references"]
    b = h.service.get_manifest(TENANT, second["manifest_id"])["references"]
    assert a[0]["artifact_ref"]["revision"] == 12
    assert b[0]["artifact_ref"]["revision"] == 13
    assert a[0]["token"] == b[0]["token"] == "<image_1>"


def test_gap_artifact_ref_points_at_owner_not_at_copied_content():
    """S20 不持有资产：`artifact_ref` 是 owner 的指针，不是内容副本。"""
    h = Harness()
    h.ready()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    character = next(r for r in m["references"] if r["role"] == "character")
    assert character["artifact_ref"] == {
        "service_id": "S11", "id": "chr_linlan", "revision": 12,
        "content_hash": sha("1")}
    assert "url" not in json.dumps(character)


# =========================================================== 5. 能力（T20-30…38）

def test_t20_30_required_capability_missing_is_422():
    """T20-30：必需段的 capability 不支持 ⇒ 422 `REQUIRED_FIELD_UNSUPPORTED`。

    两个 strict 取值都试：必需字段在任何配置下都不降级。
    """
    for strict in (True, False):
        h = Harness()
        h.adapter()
        h.template()
        h.publish(body={
            "modality": "video",
            "sections": [
                {"id": "purpose", "from": "narrative_purpose",
                 "render": "verbatim"},
                {"id": "size", "from": "camera.shot_size",
                 "render": "phrase_table", "requires": ["camera.shot_size"]},
            ],
            "negative": []})
        h.capability_payload = make_capability(claims=[])
        with pytest.raises(ServiceError) as exc:
            h.compile(options={"strict_capabilities": strict})
        assert exc.value.code == "REQUIRED_FIELD_UNSUPPORTED"
        assert exc.value.status == 422
        assert h.manifest_count() == 0


def test_t20_31_declared_optional_omitted_yields_warning():
    """T20-31：可选段 + adapter 声明过不可支持 ⇒ 200 + `OPTIONAL_OMITTED`。"""
    h = Harness()
    h.adapter(unsupported_optionals=["camera.shot_size"])
    h.template()
    h.publish(body=_body_with_optional())
    h.capability_payload = make_capability(claims=[])
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    codes = [w["code"] for w in m["warnings"]]
    assert "OPTIONAL_OMITTED" in codes
    omitted = next(w for w in m["warnings"] if w["code"] == "OPTIONAL_OMITTED")
    assert omitted["field"] == "camera.shot_size"
    # 省略是真的省略：词表命中的"中景"没有出现在正文里，且这段没有留下
    # 空洞（空段、多余分隔符）。其余段仍在。
    assert "中景" not in m["prompt_text"]
    assert "建立人物与空间的紧张关系" in m["prompt_text"]
    assert "我回来了。" in m["prompt_text"]


def test_t20_32_optional_omission_disallowed_is_422():
    """T20-32 / F11：可选段无能力 + `allow_optional_omission=false` ⇒ 422。

    与 T20-31 的唯一差别是这个开关。它存在的原因是：S25 做候选探测时想要
    "确定能做到"的清单，不希望"大概率能做到但会丢一段"混进去。
    """
    h = Harness()
    h.adapter(unsupported_optionals=["camera.shot_size"])
    h.template()
    h.publish(body=_body_with_optional())
    h.capability_payload = make_capability(claims=[])
    with pytest.raises(ServiceError) as exc:
        h.compile(options={"allow_optional_omission": False})
    assert exc.value.code == "REQUIRED_FIELD_UNSUPPORTED"
    assert exc.value.status == 422
    assert exc.value.details["missing"] == ["camera.shot_size"]
    assert h.manifest_count() == 0


def test_gap_undeclared_optional_is_not_silently_dropped():
    """S20-43：可选段从没被 adapter 声明过 ⇒ 422，不替模板作者决定。

    `optional: true` 只说明"作者不介意它缺席"；能不能缺席由
    `unsupported_optionals` 说话（§7 F10/F8 都限定在"已在
    `unsupported_optionals` 里声明过的情形"）。
    """
    h = Harness()
    h.adapter(unsupported_optionals=[])
    h.template()
    h.publish(body=_body_with_optional())
    h.capability_payload = make_capability(claims=[])
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "REQUIRED_FIELD_UNSUPPORTED"
    assert exc.value.details["reason"] == "NOT_DECLARED_OMITTABLE"
    assert h.manifest_count() == 0


def test_t20_33_out_of_range_parameter_is_refused_not_clamped():
    """T20-33：参数超 range ⇒ 422 `CAPABILITY_RANGE_VIOLATION`，永不裁剪。

    clamp 会静默改变创作意图，且下游无从知道。
    """
    h = Harness()
    h.adapter(parameter_ranges={"fps": {"min": 24, "max": 24}})
    h.template()
    h.publish()
    h.shot_payload["shot_spec"] = make_shot(
        output_spec={"aspect_ratio": "16:9", "width": 1920, "height": 1080,
                     "fps": 60})
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "CAPABILITY_RANGE_VIOLATION"
    assert exc.value.status == 422
    assert exc.value.details["value"] == 60
    assert h.manifest_count() == 0


def test_t20_34_wildcard_adapter_is_fallback_with_warning():
    """T20-34 / F8：通配 adapter 可用，但必须留 `ADAPTER_FALLBACK`。"""
    h = Harness()
    h.adapter(model_snapshot="*")
    h.template()
    h.publish()
    out = h.compile()
    m = h.service.get_manifest(TENANT, out["manifest_id"])
    assert any(w["code"] == "ADAPTER_FALLBACK" for w in m["warnings"])
    # 关键：manifest 记的是**请求的**快照，不是 adapter 的 '*'
    assert m["model_snapshot"] == "seedance-1.0-pro@2026-10"


def test_gap_wildcard_fallback_does_not_collapse_snapshots():
    """通配 adapter 下，两个不同快照不能收敛成同一份 manifest。

    曾经这里把 `adapter["model_snapshot"]`（`'*'`）写进 manifest，于是
    所有快照共用同一个 `manifest_input_once` 桶 —— 给 A 模型编的 prompt
    被当成给 B 模型编的那一份。
    """
    h = Harness()
    h.adapter(model_snapshot="*")
    h.template()
    h.publish()
    h.capability_payload = make_capability()
    first = h.compile(command_id="cmd_a")
    second = h.compile(command_id="cmd_b", target={
        "model_snapshot": "seedance-1.0-lite@2026-10"})
    assert first["manifest_id"] != second["manifest_id"]
    assert h.manifest_count() == 2


def test_t20_35_no_adapter_for_snapshot_is_422():
    """T20-35 / F7：没有精确也没有通配 adapter ⇒ 422，不从相似模型推断。"""
    h = Harness()
    h.adapter(model_snapshot="seedance-0.9@2026-08")
    h.template()
    h.publish()
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "NO_ADAPTER_FOR_SNAPSHOT"
    assert exc.value.status == 422
    assert h.manifest_count() == 0


def test_t20_36_adapter_revision_outside_template_range_is_422():
    """T20-36 / F27：adapter revision 超模板声明区间 ⇒ 422 `ADAPTER_NOT_APPLICABLE`。

    区间是模板对"我这版正文在哪些 adapter 上验过"的声明。超区间不是
    "用最新版凑合"，是把没验过的东西发出去。
    """
    h = Harness()
    h.adapter()          # revision 3
    h.template()
    h.publish(min_rev=5, max_rev=6)
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "ADAPTER_NOT_APPLICABLE"
    assert exc.value.status == 422
    assert exc.value.details["adapter_revision"] == 3
    assert h.manifest_count() == 0


def test_t20_37_upstream_down_is_503_with_retry_after():
    """T20-37 / F19：能力源不可用 ⇒ 503 + retryable，**绝不**降级编出来。"""
    h = Harness()
    h.ready()

    def broken(_snapshot_id):
        raise ConnectionError("S24 unreachable")

    h.service.capability_source = broken
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.status == 503
    assert exc.value.code == "CAPABILITY_SOURCE_UNAVAILABLE"
    assert exc.value.retryable is True
    assert h.manifest_count() == 0


def test_t20_38_capability_snapshot_upgrade_makes_a_new_manifest():
    """T20-38：同一模型的能力快照升级 ⇒ 新 manifest。

    `capability_snapshot_ref.content_hash` 变了，产物就不是同一份 —— 能力
    集决定了哪些段会被省略、档位是多少，它变了内容就可能变。反过来说，
    若这里收敛成同一份，下游会拿旧能力集下的 prompt 去打新模型。
    """
    h = Harness()
    h.ready()
    first = h.compile(command_id="cmd_a")
    before = h.service.get_manifest(TENANT, first["manifest_id"])

    upgraded = make_capability(claims=[
        {"capability_key": "camera.shot_size", "support": "documented",
         "params": {}, "evidence_id": "ev_1"},
        {"capability_key": "camera.movement", "support": "documented",
         "params": {}, "evidence_id": "ev_2"},
    ])
    h.capability_payload = upgraded
    second = h.compile(command_id="cmd_b")
    after = h.service.get_manifest(TENANT, second["manifest_id"])
    assert first["manifest_id"] != second["manifest_id"]
    assert before["capability_snapshot_ref"]["content_hash"] != \
        after["capability_snapshot_ref"]["content_hash"]
    assert h.manifest_count() == 2


def test_gap_mode_not_declared_is_not_assumed_supported():
    """S20-22：快照没声明这个 mode ⇒ 422，不假定"大概支持"。"""
    h = Harness()
    h.adapter()
    h.template()
    h.publish()
    h.capability_payload = make_capability(modes=[{
        "mode": "i2v", "support": "supported", "execution_kind": "async",
        "prompt_required": True, "prompt_max_chars": 4000,
        "output_specs": [], "capability_claims": []}])
    with pytest.raises(ServiceError) as exc:
        h.compile(options={"mode": "t2v"})
    assert exc.value.code == "REQUIRED_FIELD_UNSUPPORTED"
    assert exc.value.details["reason"] == "MODE_NOT_DECLARED"



def test_gap_adapter_same_snapshot_second_revision_is_409():
    """S20-50：同一 adapter 对同一快照发第二版 ⇒ 409，不是 500。

    §5.2 的主键允许 revision 累积，但 `adapter_binding_exact` 唯一索引不允许。
    `put_adapter` 必须把 `sqlite3.IntegrityError` 翻成带现有 revision 的 409
    —— 否则调用方拿到 500，看不出是"你重复了"还是"服务坏了"。
    """
    h = Harness()
    h.adapter()
    with pytest.raises(ServiceError) as exc:
        h.adapter(revision=4)
    assert exc.value.status == 409
    assert exc.value.code == "TEMPLATE_REVISION_CONFLICT"
    assert exc.value.details["existing_revision"] == 3
    assert exc.value.details["model_snapshot"] == "seedance-1.0-pro@2026-10"
    # 原版本仍在，没有被覆盖。
    assert h.service.get_adapter(TENANT, "adb_seedance_video", 3)[
        "revision"] == 3


def test_gap_adapter_revision_is_immutable():
    """同 (adapter_id, revision) 不同内容 ⇒ 409，不是覆盖。

    不可变是 409 而不是 upsert：模板的 `adapter_revision_min/max` 指的是
    revision 号，改掉内容等于让区间指向了没验过的映射。
    """
    h = Harness()
    h.adapter()
    with pytest.raises(ServiceError) as exc:
        h.adapter(token_budget=9999)
    assert exc.value.status == 409
    assert exc.value.code == "TEMPLATE_REVISION_CONFLICT"
    assert exc.value.details["current_content_hash"]
    assert h.service.get_adapter(TENANT, "adb_seedance_video", 3)[
        "token_budget"] == 8000


# =========================================================== 6. 幂等与并发（T20-39…46）

class _Clock:
    """把 `_now_dt` 拨到指定时刻，用来跑"60 秒后"这类断言。

    直接 `sleep 60` 会让测试套件不可接受地慢，而"换个常量"会让被测的
    判定变成常量对照。替换时钟只改时间来源，不改判定逻辑。
    """

    def __init__(self, monkeypatch, module, at: str):
        self.monkeypatch = monkeypatch
        self.module = module
        self.at = at
        self.installs()

    def installs(self):
        from datetime import datetime

        fixed = datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        self.monkeypatch.setattr(self.module, "_now_dt", lambda: fixed)

    def advance_to(self, at: str):
        from datetime import datetime

        fixed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        self.monkeypatch.setattr(self.module, "_now_dt", lambda: fixed)


def _http_client_factory(tmp_path):
    """返回一个 `(client, harness)` 工厂，复用于并发测试。

    每个 client 用同一个 DB 文件，但各自一个 `TestClient`。并发测试需要的是
    **多个连接**，`Harness()` 的 `:memory:` 连接是单例。
    """
    def build():
        import importlib.util

        path = ROOT / "api" / "app.py"
        spec = importlib.util.spec_from_file_location("s20_api_app", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        h = Harness(conn=module._conn)
        module.service = h.service
        module._idem = h.idem
        return TestClient(module.app), h, module
    return build


def test_t20_39_same_command_ten_times_is_one_execution(monkeypatch, tmp_path):
    """T20-39：同 `command_id` 同 body 连打 10 次 ⇒ 1 次执行、9 次回放。

    断言的是 `replayed` 标志的分布，不是"10 次都 200"——后者对"每次都真的
    重新编译"也成立。
    """
    monkeypatch.setenv("S20_DB", str(tmp_path / "s20.db"))
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    responses = [post(client, "/prompt-compilations", h.compile_body())
                 for _ in range(10)]
    assert {r.status_code for r in responses} == {201}
    bodies = [r.json() for r in responses]
    assert sum(1 for b in bodies if not b["replayed"]) == 1
    assert sum(1 for b in bodies if b["replayed"]) == 9
    assert len({b["manifest_id"] for b in bodies}) == 1
    assert h.manifest_count() == 1


def test_t20_40_same_command_different_body_is_409(monkeypatch, tmp_path):
    """T20-40 / F21：同键不同 body ⇒ 409 `IDEMPOTENCY_KEY_REUSED`，零写入。

    不是覆盖：调用方会以为自己改了参数，实际拿到的是上一份的结果。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    first = post(client, "/prompt-compilations", h.compile_body(),
                 command_id="cmd_same")
    assert first.status_code == 201
    before = h.manifest_count()

    changed = h.compile_body()
    changed["target"] = {**changed["target"],
                         "model_snapshot": "seedance-1.0-lite@2026-10"}
    second = post(client, "/prompt-compilations", changed,
                  command_id="cmd_same")
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert h.manifest_count() == before
    # 首份结果仍然可读 —— 409 没有把它抹掉。
    assert get(client, f"/prompt-compilations/{first.json()['manifest_id']}"
               ).status_code == 200


def test_t20_41_in_flight_second_request_is_409_with_retry_after(
        monkeypatch, tmp_path):
    """T20-41 / F22：首次执行未落结果时重发 ⇒ 409 `COMMAND_IN_PROGRESS`。

    `Retry-After: 2` 是方案点名的（F22），与 S10 的 `COMMAND_IN_FLIGHT` +
    1 秒不同：编译比建资产慢，1 秒会让调用方在真正完成前空打三次。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    body = h.compile_body()
    from idempotency_sdk import canonical_request_hash

    # 手工占位：模拟"第一次请求已经 begin 但还没 finish"。
    h.idem.begin(TENANT, "cmd_inflight", canonical_request_hash(body))
    response = post(client, "/prompt-compilations", body,
                    command_id="cmd_inflight")
    assert response.status_code == 409
    assert response.json()["code"] == "COMMAND_IN_PROGRESS"
    assert response.headers["Retry-After"] == "2"
    assert h.manifest_count() == 0


def test_t20_42_stale_in_progress_blocks_forever_without_cleanup():
    """T20-42：崩溃留下的 `IN_PROGRESS` 行**没有**清理任务。

    方案的期望是"60 秒后清理，重发可成功"。本服务没有后台任务、没有
    `expires_at` 列（SDK 的 DDL 只有 `result IS NULL` 表示在途），所以崩溃
    留下的行会让同 `command_id` 永远 409。登记为 S20-51。

    这条测试记录的是**现状**，不是期望：它让"没实现"这件事在测试里可见，
    而不是让一个没有覆盖的场景看起来通过。
    """
    assert_gap("S20-51")
    h = Harness()
    h.ready()
    body = h.compile_body()
    from idempotency_sdk import canonical_request_hash

    h.idem.begin(TENANT, "cmd_crashed", canonical_request_hash(body))
    with pytest.raises(CommandInProgress):
        h.idem.begin(TENANT, "cmd_crashed", canonical_request_hash(body))
    # 库里那一行没有时间戳可判"多久算旧"。
    row = h.conn.execute(
        "SELECT result, created_at FROM idempotency_record "
        "WHERE tenant_id=? AND command_id=?",
        (TENANT, "cmd_crashed")).fetchone()
    assert row["result"] is None
    assert row["created_at"]  # 有创建时间，但没有 expires_at 列
    columns = {r[1] for r in h.conn.execute(
        "PRAGMA table_info(idempotency_record)").fetchall()}
    assert "expires_at" not in columns


def test_t20_43_two_command_ids_same_input_converge(tmp_path):
    """T20-43：两个不同 `command_id` 并发编译同一输入 ⇒ 1 份 manifest。

    每个线程一个连接，跑在同一个 DB 文件上。第二层去重（`manifest_input_once`）
    必须让两者收敛；`BEGIN IMMEDIATE` 把写排成串行，所以后到的那个看到既有行。
    """
    import sqlite3 as _sqlite3
    import threading

    path = tmp_path / "concurrent.db"
    seed = _sqlite3.connect(path, isolation_level=None)
    seed.row_factory = _sqlite3.Row
    setup = Harness(conn=seed)
    setup.ready()
    body = setup.compile_body()
    seed.close()

    results, errors = [], []
    barrier = threading.Barrier(2)

    def attempt(command_id):
        conn = _sqlite3.connect(path, isolation_level=None, timeout=10)
        conn.row_factory = _sqlite3.Row
        h = Harness(conn=conn)
        # 每个连接都要有自己的 idem store；同一文件上的表是共享的。
        barrier.wait()
        try:
            results.append(h.service.compile(TENANT, command_id, body, "h"))
        except Exception as exc:      # noqa: BLE001 - 收集后再断言
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(f"cmd_{i}",))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert len(results) == 2
    assert len({r["manifest_id"] for r in results}) == 1
    check = _sqlite3.connect(path, isolation_level=None)
    assert check.execute(
        "SELECT count(*) FROM compiled_prompt_manifest").fetchone()[0] == 1
    assert check.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    check.close()


def test_t20_44_concurrent_publish_one_wins(tmp_path):
    """T20-44：两个请求以同一 `parent_revision` 并发发布 ⇒ 一个成功一个 409。"""
    import sqlite3 as _sqlite3
    import threading

    path = tmp_path / "publish.db"
    seed = _sqlite3.connect(path, isolation_level=None)
    seed.row_factory = _sqlite3.Row
    setup = Harness(conn=seed)
    setup.template()
    setup.adapter()
    setup.publish(parent_revision=0)          # 到 revision 1
    seed.close()

    results, errors = [], []
    barrier = threading.Barrier(2)

    def attempt(command_id):
        conn = _sqlite3.connect(path, isolation_level=None, timeout=10)
        conn.row_factory = _sqlite3.Row
        h = Harness(conn=conn)
        barrier.wait()
        try:
            # 两者都以为当前是 revision 1，都想发 revision 2。
            results.append(h.publish(command_id=command_id,
                                     parent_revision=1))
        except ServiceError as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(f"cmd_pub_{i}",))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, (results, errors)
    assert len(errors) == 1
    assert errors[0].code == "TEMPLATE_REVISION_CONFLICT"
    check = _sqlite3.connect(path, isolation_level=None)
    assert check.execute(
        "SELECT count(*) FROM template_revision").fetchone()[0] == 2
    check.close()


def test_t20_45_idempotency_records_never_expire():
    """T20-45：幂等记录**不会** 30 天后过期。

    方案 §5.5 给 `idempotency_record` 加了 `expires_at`，但 `idempotency_sdk`
    的 DDL 没有这一列，也没有清理任务。后果：`command_id` 是永久键，
    调用方不能跨月复用同一个 id。登记为 S20-51。
    """
    assert_gap("S20-51")
    h = Harness()
    columns = {r[1] for r in h.conn.execute(
        "PRAGMA table_info(idempotency_record)").fetchall()}
    assert columns == {"tenant_id", "command_id", "request_hash", "result",
                       "created_at"}
    assert "expires_at" not in columns


def test_t20_46_replaying_does_not_duplicate_side_effects(
        monkeypatch, tmp_path):
    """T20-46：回放不产生新的 outbox 行、不新写 manifest。

    `DONE` 行只持有 JSON 结果，回放从它读 —— 不是"再执行一次拿到同样的
    数据"。若实现把回放做成重跑，outbox 会多一行，下游收到重复事件。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    first = post(client, "/prompt-compilations", h.compile_body(),
                 command_id="cmd_replay")
    assert first.status_code == 201
    rows_after_first = len(h.outbox_rows())
    manifest_after_first = h.manifest_count()
    for _ in range(5):
        again = post(client, "/prompt-compilations", h.compile_body(),
                     command_id="cmd_replay")
        assert again.status_code == 201
        assert again.json()["replayed"] is True
        assert again.json()["manifest_id"] == first.json()["manifest_id"]
    assert len(h.outbox_rows()) == rows_after_first == 1
    assert h.manifest_count() == manifest_after_first == 1


# =========================================================== 7. 跨租户与安全（T20-47…53）

def test_t20_47_cross_tenant_manifest_read_is_404(monkeypatch, tmp_path):
    """T20-47 / E28：租户 A 读租户 B 的 manifest ⇒ 404，无存在性泄漏。"""
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    created = post(client, "/prompt-compilations", h.compile_body())
    assert created.status_code == 201
    manifest_id = created.json()["manifest_id"]

    foreign = get(client, f"/prompt-compilations/{manifest_id}",
                  tenant=OTHER_TENANT)
    absent = get(client, "/prompt-compilations/cpm_nope", tenant=OTHER_TENANT)
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json()["code"] == absent.json()["code"] == "NOT_FOUND"
    assert foreign.json()["details"] == absent.json()["details"] == {}
    assert "prompt_text" not in foreign.text
    # 报错消息回显的是**调用方自己传的 id**（`cpm_nope` 也一样回显），所以
    # `id in message` 不构成存在性预言机；两者除 id 外必须逐字段相同。
    assert (foreign.json()["message"].replace(manifest_id, "<ID>")
            == absent.json()["message"].replace("cpm_nope", "<ID>"))


def test_t20_48_cross_tenant_template_is_404(monkeypatch, tmp_path):
    """T20-48：租户 A 用租户 B 的 `template_id` 编译 ⇒ 404。

    不是 422：知道"B 有这个模板但你不能用"本身就是泄漏。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    body = h.compile_body()
    response = post(client, "/prompt-compilations", body, tenant=OTHER_TENANT)
    assert response.status_code == 404
    # 对拍：B 眼里的 A 模板 === B 眼里根本不存在的模板。两者不可区分。
    missing = post(client, "/prompt-compilations",
                   h.compile_body(target={
                       "template_ref": {"id": "tpl_no_such", "revision": 1}}),
                   command_id="cmd_other", tenant=OTHER_TENANT)
    assert missing.status_code == 404
    assert response.json()["code"] == missing.json()["code"]
    assert response.json()["details"] == missing.json()["details"] == {}
    assert h.manifest_count(OTHER_TENANT) == 0


def test_t20_49_body_tenant_id_must_match_signed_context(monkeypatch,
                                                         tmp_path):
    """T20-49：body 里自带 `tenant_id` 且与签名头不符 ⇒ 403。

    方案写的是"被忽略，用 TCT 值"。实现取更严的一侧：**拒绝**。理由是忽略
    会让一个把租户写错的调用方拿到 201，误以为自己操作的是另一个租户；而
    忽略与信任之间只差一次重构。这条记在 S20-52。

    一致时允许通过（body 里的 tenant_id 只是回显）。
    """
    assert_gap("S20-52")
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    body = {**h.compile_body(), "tenant_id": OTHER_TENANT}
    denied = post(client, "/prompt-compilations", body, tenant=TENANT)
    assert denied.status_code == 403
    assert denied.json()["code"] == "TENANT_SCOPE_DENIED"
    assert h.manifest_count() == 0

    agreeing = {**h.compile_body(), "tenant_id": TENANT}
    allowed = post(client, "/prompt-compilations", agreeing, tenant=TENANT)
    assert allowed.status_code == 201


def test_t20_50_missing_tenant_header_is_400(monkeypatch, tmp_path):
    """T20-50 的一半：缺 `X-Tenant-Id` ⇒ 400。另一半（`creative.prompt.compile`
    权限）本服务没有权限模型，登记为 S20-53。
    """
    assert_gap("S20-53")
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    response = client.post("/prompt-compilations", json=h.compile_body(),
                           headers={"Idempotency-Key": "c1"})
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert h.manifest_count() == 0


def test_t20_51_tenant_id_in_prompt_is_forbidden():
    """T20-51 / F18：`prompt_text` 含租户标识 ⇒ 422。

    查的是**值**也查**键名**：`tenant_id` 出现在正文里同样拒绝 —— 说明模板
    作者在往 prompt 里塞标识符，那正是 F18 要拦的。
    """
    h = Harness()
    h.adapter()
    h.template()
    h.publish(body={
        "modality": "video",
        "sections": [{"id": "purpose", "from": "narrative_purpose",
                      "render": "verbatim"}],
        "negative": []})
    # 把租户值塞进镜头的一个自由文本字段。
    h.shot_payload["shot_spec"] = make_shot(
        narrative_purpose=f"给 {TENANT} 用的镜头")
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "OUTPUT_CONTAINS_FORBIDDEN_FIELD"
    assert exc.value.status == 422
    assert exc.value.details["field"] == "tenant_id"
    assert h.manifest_count() == 0


def test_t20_51b_identifier_key_name_in_prompt_is_forbidden():
    """F18 的另一半：键名 `tenant_id` 出现在正文里也拒绝。"""
    h = Harness()
    h.adapter()
    h.template()
    h.publish(body={
        "modality": "video",
        "sections": [{"id": "purpose",
                      "text": "根据 tenant_id 渲染镜头", "from": "beats"}],
        "negative": []})
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "OUTPUT_CONTAINS_FORBIDDEN_FIELD"
    assert exc.value.details["field"] == "tenant_id"


def test_t20_52_service_has_no_logging_of_prompt_text():
    """T20-52：本服务**不写任何日志**，所以无从泄漏 `prompt_text`。

    这条不是"我审过代码"，而是可执行的：扫本服务的源码，确认没有
    `logging` / `print` / `logger`。有朝一日加了日志，这条立刻红。
    """
    import re

    root = ROOT / "src" / "prompt_compiler"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"^\s*import logging", r"^\s*from logging",
                        r"\blogging\.", r"^\s*print\("):
            if re.search(pattern, text, re.M):
                offenders.append((path.name, pattern))
    assert offenders == []
    api = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    assert "logging" not in api and "print(" not in api


def test_t20_53_prompt_text_is_stored_in_plaintext():
    """T20-53：方案要求字段级加密（KMS，按租户分密钥），**未实现**。

    E31 的前置条件，§11 C7 的一部分。本服务没有 KMS 依赖、没有密钥轮换、
    没有 `kms` 配置项。这条测试把现状写出来：库里是明文。

    反面意义：若有人误以为"已经加密了"并把客户内容当密文处理（例如把
    `prompt_text` 当不可读的 blob），这条会失败并提醒他。
    """
    assert_gap("S20-C7")
    h = Harness()
    h.ready()
    out = h.compile()
    stored = h.conn.execute(
        "SELECT prompt_text FROM compiled_prompt_manifest WHERE manifest_id=?",
        (out["manifest_id"],)).fetchone()[0]
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert stored == manifest["prompt_text"]
    assert "我回来了。" in stored
    # 没有加密实现：无 KMS 客户端、无密钥配置、无 crypto 依赖。
    # （`api/app.py` 里的 `KMS_UNAVAILABLE` 只是 Retry-After 名单里的一个
    # 未来错误码，不是加密路径 —— 这正是容易被误读成"已加密"的地方。）
    sources = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "src" / "prompt_compiler").rglob("*.py")))
    for absent in ("from cryptography", "import cryptography",
                   "Fernet", "AESGCM", "kms_client", "S20_KMS_KEY"):
        assert absent not in sources, absent


# =========================================================== 8. 事件与 outbox（T20-54…60）

def test_t20_54_exactly_one_outbox_row_with_sequence_one():
    """T20-54：编译成功 ⇒ outbox 恰好 1 行，`aggregate_sequence=1`。

    manifest 不可变、只发一次，所以序号恒为 1（§6.4）。
    """
    h = Harness()
    h.ready()
    out = h.compile()
    rows = h.outbox_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["aggregate_id"] == out["manifest_id"]
    assert row["event_type"] == "prompt.compiled"
    assert row["published_at"] is None
    payload = json.loads(row["payload"])
    assert payload["aggregate_sequence"] == 1
    assert payload["aggregate_id"] == out["manifest_id"]


def test_t20_55_relay_retry_redelivers_without_losing():
    """T20-55：重复触发 relay ⇒ 消费者收到重复事件，但**一条都不丢**。

    at-least-once 的"至多"部分由消费者按 `event_id` 去重，不是由 S20 保证
    恰好一次。这里模拟"发出去了、还没来得及 mark_published 就崩了"：同一
    行会再被取一次，`event_id` 相同（消费者可去重），且第二次取到的批次里
    它仍在 —— 没有丢。
    """
    from event_sdk import Outbox

    h = Harness()
    h.ready()
    out = h.compile()
    outbox = Outbox(h.conn)

    first_batch = outbox.unpublished()
    assert len(first_batch) == 1
    # "已投递但未确认" —— 不调用 mark_published 就是崩溃现场。
    retry_batch = Outbox(h.conn).unpublished()
    assert len(retry_batch) == 1
    assert retry_batch[0]["id"] == first_batch[0]["id"]
    assert retry_batch[0]["type"] == "prompt.compiled"
    assert retry_batch[0]["aggregate_id"] == out["manifest_id"]

    # 只有确认之后才消失；确认只影响这一条。
    outbox.mark_published(first_batch[0]["id"])
    assert Outbox(h.conn).unpublished() == []


def test_second_layer_dedup_emits_no_second_event():
    """§6.2 第二层：命中共有 manifest 时**不**再插 outbox 行。

    plan 的 T20 表没有单独一行，但它与 T20-55 是两件事：T20-55 说 relay 可以
    重发同一行（at-least-once），这条说业务层不会为同一份 manifest 造第二行。
    缺了它，"重发"和"重复发"会被混为一谈。
    """
    h = Harness()
    h.ready()
    first = h.compile(command_id="cmd_a")
    second = h.compile(command_id="cmd_b")
    assert second["deduplicated"] is True
    assert first["manifest_id"] == second["manifest_id"]
    assert len(h.outbox_rows()) == 1


def test_t20_56_failed_compile_leaves_no_outbox_row():
    """T20-56：业务失败时 outbox 无残留行。

    `_step_save` 把 manifest 与 outbox 放在**同一个** `Transaction` 里；任何
    一步失败都在写之前，所以不会出现"事件发了但 manifest 没有"。
    """
    h = Harness()
    h.ready()
    # 必然是失败：快照给的 prompt 上限比这份模板渲染出的文本短。
    h.capability_payload = make_capability(prompt_max=10)
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "RENDER_LIMIT_EXCEEDED"
    assert h.outbox_rows() == []
    assert h.manifest_count() == 0


def test_t20_57_event_data_never_contains_prompt_text():
    """T20-57：事件 `data` 只有 ref，没有 `prompt_text` / 对白正文。

    E31：事件会进多副本留存（Kafka + 归档），客户内容不能跟着走。
    """
    h = Harness()
    h.ready()
    out = h.compile()
    payload = json.loads(h.outbox_rows()[0]["payload"])
    data = payload["data"]
    assert "prompt_text" not in data
    assert "negative_prompt" not in data
    assert "dialogue_segments" not in data
    serialized = json.dumps(data, ensure_ascii=False)
    assert "我回来了。" not in serialized
    assert "建立人物与空间的紧张关系" not in serialized
    # 有 ref 能反查。
    assert data["manifest_ref"] == {"id": out["manifest_id"], "revision": 1,
                                    "content_hash": out["content_hash"]}


def test_t20_58_event_data_validates_against_the_payload_schema():
    """T20-58：事件 `data` 通过 `prompt.compiled.v2.schema.json`。

    S20-49：**信封**通不过 `event-envelope.v2`（SDK 写 `sequence`、契约要
    `aggregate_sequence`，且 `additionalProperties: false`），所以这里只验
    `data` 部分。这不是放宽标准，是把无法通过的断言登记成缺口。
    """
    assert_gap("S20-49")
    import jsonschema

    contracts = ROOT.parents[1] / "contracts"
    schema_path = contracts / "prompt.compiled.v2.schema.json"
    assert schema_path.exists(), schema_path
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    h = Harness()
    h.ready()
    h.compile()
    payload = json.loads(h.outbox_rows()[0]["payload"])
    data = payload["data"]
    # `schema_version` 在 data 里（contracts 要求，§4.3 的示例漏了它）。
    assert data["schema_version"] == "2.0"
    jsonschema.validate(instance=data, schema=schema)

    # 反向验证：把必填键去掉，校验必须失败 —— 否则上面那句可能是空转。
    broken = {k: v for k, v in data.items() if k != "model_snapshot"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=broken, schema=schema)


def test_t20_59_relay_restart_does_not_republish_published_rows():
    """T20-59：relay 崩溃恢复后，未发布的继续发，已发布的不重发。

    `Outbox.unpublished()` 只看 `published_at IS NULL`。S20 没有自己的 relay
    （属平台组件），所以这里验的是"它依赖的那个查询"。
    """
    from event_sdk import Outbox

    h = Harness()
    h.ready()
    h.compile(command_id="cmd_a")
    outbox = Outbox(h.conn)
    first_batch = outbox.unpublished()
    assert len(first_batch) == 1
    outbox.mark_published(first_batch[0]["id"])
    # "崩溃重启"：新建一个 Outbox 实例，等价于进程重启。
    assert Outbox(h.conn).unpublished() == []
    # 第二份 manifest 的事件仍会发出。
    h.shot_payload["shot_spec"] = make_shot(emotion="tense")
    h.shot_payload["s06_meta"] = {**h.shot_payload["s06_meta"],
                                  "content_hash": sha("9")}
    h.compile(command_id="cmd_b")
    assert len(Outbox(h.conn).unpublished()) == 1


def test_t20_60_no_archive_task_exists():
    """T20-60：方案要求"已发布行 7 天后进归档、14 天后删除"，**未实现**。

    本服务没有归档表、没有归档任务。SDK 的 outbox 只有 `published_at`，
    没有 `archived_at`。这条记录现状（S20-54），避免"归档策略已生效"的误解
    —— 那个误解在合规审计时会变成实际风险。
    """
    assert_gap("S20-54")
    h = Harness()
    tables = {r[0] for r in h.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "outbox" in tables
    assert not any("archive" in t for t in tables)
    columns = {r[1] for r in h.conn.execute(
        "PRAGMA table_info(outbox)").fetchall()}
    assert "archived_at" not in columns
    assert "published_at" in columns


# =========================================================== 9. 状态机与版本（T20-61…68）

def _insert_draft(h: Harness, *, revision: int, template_id: str =
                  "tpl_shot_video", body: dict | None = None) -> None:
    """直接插一版 DRAFT。

    经 API 插不出来：`publish_revision` 一次调用就写完「DRAFT → 跑回归 →
    PUBLISHED」，回归失败则一行都不写（S20-37）。所以 DRAFT 在本实现里是
    **事务内的中间态**，不是可观测状态。这里用 SQL 造出那个状态，是为了测
    「万一存在 DRAFT 被引用，编译必须拒绝」这条守卫本身 —— 守卫存在，
    只是入口被关掉了。见 S20-37。
    """
    from prompt_compiler.service import template_content_hash

    doc = {"compatible_schema": {"id": "shot-spec", "revision": 1,
                                 "content_hash": sha("c")},
           "modality": "video", "provider": "volcengine",
           "model_snapshots": ["seedance-1.0-pro@2026-10"],
           "adapter_scopes": [ADAPTER["adapter_id"]],
           "body": body or TEMPLATE_BODY,
           "adapter_revision_min": 1, "adapter_revision_max": 5,
           "regression_set_ref": {"id": "rgs_1", "revision": 1,
                                  "content_hash": sha("r")}}
    h.conn.execute(
        "INSERT INTO template_revision (tenant_id, template_id, revision,"
        " state, compatible_schema, body, adapter_revision_min,"
        " adapter_revision_max, model_snapshots, adapter_scopes,"
        " regression_set_ref, regression_verdict, content_hash,"
        " author_subject, published_at, retired_at, created_at)"
        " VALUES (?,?,?,'DRAFT',?,?,?,?,?,?,?,'pending',?,'author_1',NULL,"
        " NULL,?)",
        (TENANT, template_id, revision, json.dumps(doc["compatible_schema"]),
         json.dumps(doc["body"], ensure_ascii=False), 1, 5,
         json.dumps(doc["model_snapshots"]), json.dumps(doc["adapter_scopes"]),
         json.dumps(doc["regression_set_ref"]), template_content_hash(doc),
         "2026-10-01T00:00:00Z"))


def test_t20_61_draft_is_not_compilable():
    """T20-61 / F2：被引用的 revision 是 DRAFT ⇒ 422 `TEMPLATE_NOT_PUBLISHED`。"""
    h = Harness()
    h.template()
    h.adapter()
    _insert_draft(h, revision=1)
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert exc.value.code == "TEMPLATE_NOT_PUBLISHED"
    assert exc.value.status == 422
    assert h.manifest_count() == 0


def test_t20_62_regression_failure_publishes_nothing():
    """T20-62 / F23：回归不过 ⇒ 422 `REGRESSION_NOT_PASSED`，且不写 revision。

    S20-37：方案说「版本仍 DRAFT」，但本实现一行都不写 —— 留下的 DRAFT 会
    让重试撞 `template_revision_one_draft`，且幂等包装在异常时本来就 ROLLBACK。
    """
    h = Harness()
    h.template()
    h.adapter()
    # `phrase_table` 段引用了一个 adapter 里没有的路径 ⇒ 回归 case 失败。
    body = {"modality": "video",
            "sections": [{"id": "cam", "from": "camera.lens.mm",
                          "render": "phrase_table"}],
            "negative": []}
    with pytest.raises(ServiceError) as exc:
        h.publish(body=body)
    assert exc.value.code == "REGRESSION_NOT_PASSED"
    assert exc.value.status == 422
    assert exc.value.details["report"]["verdict"] == "fail"
    failed = [c for c in exc.value.details["report"]["cases"]
              if any(a["result"] == "fail" for a in c["assertions"])]
    assert len(failed) == 1
    assert h.revision_count() == 0
    # 失败了也能重来：换一个好 body，同一个 parent_revision=0 照样发布。
    assert h.publish(parent_revision=0)["revision"] == 1
    assert h.revision_count() == 1


def test_t20_63_retired_template_cannot_compile():
    """T20-63 / F3：退役后编译 ⇒ 422 `TEMPLATE_RETIRED`。"""
    h = Harness()
    h.ready()
    assert h.compile(command_id="cmd_a")["revision"] == 1
    h.service.retire_revision(TENANT, "cmd_retire", "tpl_shot_video", 1, {},
                              "h")
    with pytest.raises(ServiceError) as exc:
        h.compile(command_id="cmd_b")
    assert exc.value.code == "TEMPLATE_RETIRED"
    assert exc.value.status == 422
    # 退役**没有**删掉任何东西。
    assert h.manifest_count() == 1


def test_t20_64_retired_template_keeps_historical_manifests():
    """T20-64：退役后读历史 manifest ⇒ 200（manifest 是事实记录）。"""
    h = Harness()
    h.ready()
    out = h.compile()
    h.service.retire_revision(TENANT, "cmd_retire", "tpl_shot_video", 1, {},
                              "h")
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert manifest["manifest_id"] == out["manifest_id"]
    assert manifest["prompt_text"]
    assert manifest["template_source_ref"]["revision"] == 1


def test_t20_65_retired_template_is_still_readable():
    """T20-65：退役后 `GET` 模板 ⇒ 200，`state` 变成 RETIRED（可读）。"""
    h = Harness()
    h.ready()
    h.service.retire_revision(TENANT, "cmd_retire", "tpl_shot_video", 1, {},
                              "h")
    got = h.service.get_template_revision(TENANT, "tpl_shot_video", 1)
    assert got["state"] == "RETIRED"
    assert got["retired_at"]
    assert got["published_at"]  # 发布时刻仍在
    assert got["body"]["sections"]
    listed = h.service.list_template_revisions(TENANT, "tpl_shot_video")
    assert [r["state"] for r in listed["revisions"]] == ["RETIRED"]


def test_t20_66_retire_during_compile_does_not_abort_it():
    """T20-66 / S20-38：编译进行中退役，本次编译正常完成。

    退役只挡**新**编译（S20-38 的守卫是"无未完成编译"，但 S20 没有在途登记
    表）。这里把退役塞在读镜头的回调里 —— 那时步骤 ① 的状态检查已经过了，
    正是一次"编译中途"。若实现每一步都重查状态，这条会 422。
    """
    assert_gap("S20-38")
    h = Harness()
    h.ready()
    original = h._shot

    def retiring_shot(shot_id, revision):
        h.service.retire_revision(TENANT, "cmd_retire", "tpl_shot_video", 1,
                                  {}, "h")
        return original(shot_id, revision)

    h.service.shot_source = retiring_shot
    out = h.compile()
    assert out["manifest_id"].startswith("cpm_")
    assert h.service.get_manifest(TENANT, out["manifest_id"])["prompt_text"]
    with pytest.raises(ServiceError) as exc:
        h.compile(command_id="cmd_after")
    assert exc.value.code == "TEMPLATE_RETIRED"


def test_t20_67_new_regression_set_version_changes_revision_hash():
    """T20-67：换回归集版本再发布 ⇒ 新的 `template_revision.content_hash`。

    回归集是**发布门**，它变了就等于"这一版是在另一组断言下通过的"，所以
    必须是不同的内容。若 hash 不含它，两次"内容相同、门不同"的发布会共用一个
    hash，下游无法分辨哪一版被哪套断言背书。
    """
    h = Harness()
    h.ready()                                   # revision 1
    first = h.service.get_template_revision(TENANT, "tpl_shot_video", 1)
    payload = {
        "parent_revision": 1,
        "compatible_schema": {"id": "shot-spec", "revision": 1,
                              "content_hash": sha("c")},
        "body": TEMPLATE_BODY,
        "model_snapshots": ["seedance-1.0-pro@2026-10"],
        "adapter_revision_min": 1, "adapter_revision_max": 5,
        "adapter_scopes": [ADAPTER["adapter_id"]],
        "regression_set_ref": {"id": "rgs_1", "revision": 2,
                               "content_hash": sha("R")},
        "author_subject": "author_1",
    }
    h.service.publish_revision(TENANT, "cmd_pub_2", "tpl_shot_video", payload,
                               "h")
    second = h.service.get_template_revision(TENANT, "tpl_shot_video", 2)
    assert first["body"] == second["body"]          # 正文一模一样
    assert second["regression_set_ref"]["revision"] == 2
    assert first["content_hash"] != second["content_hash"]

    # 反面：回归集版本号变了但 content_hash 没变 ⇒ 仍是不同内容（版本号参与
    # hash），所以"只改 revision 不改内容"也不会碰撞。
    payload["parent_revision"] = 2
    payload["regression_set_ref"] = {"id": "rgs_1", "revision": 3,
                                     "content_hash": sha("R")}
    h.service.publish_revision(TENANT, "cmd_pub_3", "tpl_shot_video", payload,
                               "h")
    third = h.service.get_template_revision(TENANT, "tpl_shot_video", 3)
    assert third["content_hash"] != second["content_hash"]


def test_t20_68_changing_template_body_changes_hash():
    """T20-68：模板 revision 内容改动 ⇒ `content_hash` 必变。

    逐个字段改一遍，而不是只改一处：只要有一个字段不进 hash，改它就等于
    "悄悄发布"—— 下游按 hash 判"这份 prompt 变了吗"会得到否。
    """
    base = {"modality": "video",
            "sections": [{"id": "purpose", "from": "narrative_purpose",
                          "render": "verbatim"}],
            "negative": ["模糊"]}
    h = Harness()
    h.template()
    h.adapter()
    first = h.publish(parent_revision=0, body=base)["content_hash"]

    mutations = {
        "section_text": {"modality": "video",
                         "sections": [{"id": "purpose",
                                       "from": "narrative_purpose",
                                       "render": "verbatim"}],
                         "negative": ["模糊", "水印"]},
        "render_type": {"modality": "video",
                        "sections": [{"id": "purpose",
                                      "from": "narrative_purpose",
                                      "render": "template_string",
                                      "text": "{{ narrative_purpose }}"}],
                        "negative": ["模糊"]},
    }
    hashes = {first}
    for index, (_label, body) in enumerate(mutations.items(), start=1):
        out = h.publish(parent_revision=index, body=body)
        hashes.add(out["content_hash"])
    assert len(hashes) == 3          # 三次发布三个 hash


# =========================================================== 10. 时长（T20-69…72）

def test_t20_69_target_four_tier_five_is_trim_with_window():
    """T20-69 / E03：目标 4.0s、档位 5s ⇒ `handling=trim` 且 `trim_window` 完整。

    S20 **不**把 4.0 改成 5 往下走：S41 的 EDL 会因此少 1 秒而没人知道。
    """
    h = Harness()
    h.ready()
    # 默认 fixture 的档位是 (4,5,8)，最小 >= 4 的档位就是 4 —— 那是"刚好
    # 命中"，`trim_window` 是满长度而不是裁剪。方案举的例子要求**没有 4 秒
    # 档位**，所以这里把档位换成 (5,8)。
    h.capability_payload = make_capability(tiers=(5, 8))
    out = h.compile()
    duration = h.service.get_manifest(TENANT, out["manifest_id"])["duration"]
    assert duration["target_seconds"] == 4.0
    assert duration["provider_tier_seconds"] == 5
    assert duration["overrun_seconds"] == 1.0
    assert duration["handling"] == "trim"
    assert duration["trim_window"] == {"from": 0.0, "to": 4.0}


def test_t20_70_no_tier_reaches_target_is_unacceptable():
    """T20-70 的一半：没有 ≥ 目标的档位 ⇒ `unacceptable`，编译仍成功。

    §1.5：`handling=unacceptable` 时很编**仍然成功** —— manifest 是事实记录，
    "这份产物不可用"本身是事实。拒绝编译会让 S25 连"这个候选为什么不可用"
    都看不到。
    """
    h = Harness()
    h.ready()
    h.capability_payload = make_capability(tiers=(2, 3))
    out = h.compile()
    duration = h.service.get_manifest(TENANT, out["manifest_id"])["duration"]
    assert duration["handling"] == "unacceptable"
    assert duration["provider_tier_seconds"] is None
    assert duration["overrun_seconds"] is None
    assert duration["trim_window"] is None
    # 没有任何"就近取档"。
    assert duration["target_seconds"] == 4.0


def test_gap_large_overrun_is_still_labelled_trim():
    """T20-70 的另一半：**未实现** `retime`，也没有"超多少算不可接受"的阈值。

    目标 4.0s、唯一档位 8s 时，本实现照样给 `handling=trim` + 4 秒的
    `trim_window`。等于说"剪掉一半"与"剪掉 0.1 秒"是同一个标签。方案说
    「视配置 `retime` 或 `unacceptable`」，但没给阈值，本服务也没有配置项，
    所以这里记录现状并登记 S20-55。S25 若按 `handling` 做硬过滤，这个标签
    会让它放行一个实际不可用的候选。
    """
    assert_gap("S20-55")
    h = Harness()
    h.ready()
    h.capability_payload = make_capability(tiers=(8,))
    out = h.compile()
    duration = h.service.get_manifest(TENANT, out["manifest_id"])["duration"]
    assert duration["handling"] == "trim"
    assert duration["overrun_seconds"] == 4.0
    assert duration["provider_tier_seconds"] == 8


def test_t20_71_unacceptable_manifest_carries_the_verdict_for_s25():
    """T20-71：`unacceptable` 的判定必须留在 manifest 里，供 S25 硬过滤。

    真正的过滤属 S25（跨服务），S20 只负责把判定写清楚。可执行的部分是：
    manifest 里能读到 `handling` 与 `overrun_seconds`，**并且它同一份
    manifest 的 `content_hash` 覆盖了它** —— 否则 S25 缓存过的候选会绕过。
    """
    h = Harness()
    h.ready()
    h.capability_payload = make_capability(tiers=(2, 3))
    out = h.compile()
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert manifest["duration"]["handling"] == "unacceptable"
    # `manifest_content_hash` 吃的是 manifest 形状（含 `shot_ref` 等嵌套
    # 对象），不是扁平投影 —— 所以直接把读回来的 manifest 喂给它。
    assert manifest_content_hash(manifest) == out["content_hash"]
    # 反面：把 `handling` 改掉，hash 必须变。否则 S25 缓存的旧候选会带着
    # "可用"的标签绕过这次判定。
    bumped = {**manifest, "duration": {
        **manifest["duration"], "handling": "trim"}}
    assert manifest_content_hash(bumped) != out["content_hash"]


def test_t20_72_native_locked_voice_with_dialogue_needs_alignment():
    """T20-72 / F16：原生对白镜头缺对齐 ⇒ 422 `DIALOGUE_ALIGNMENT_REQUIRED`。"""
    h = Harness()
    h.ready()
    body = h.compile_body()
    body["audio_strategy"] = "native_locked_voice"
    with pytest.raises(ServiceError) as exc:
        h.service.compile(TENANT, "cmd_native", body, "h")
    assert exc.value.code == "DIALOGUE_ALIGNMENT_REQUIRED"
    assert exc.value.status == 422
    assert h.manifest_count() == 0


def test_t20_72b_alignment_present_lets_native_locked_voice_compile():
    """对照：给了 `dialogue_alignment_ref` 就能编 —— 否则上一条可能只是"这个
    策略永远编不过"。"""
    h = Harness()
    h.ready()
    body = h.compile_body()
    body["audio_strategy"] = "native_locked_voice"
    body["dialogue_alignment_ref"] = {"id": "alr_0001", "revision": 1}
    out = h.service.compile(TENANT, "cmd_native_ok", body, "h")
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    from prompt_compiler.service import _hash_of

    assert manifest["dialogue_alignment_ref"] == {
        "service_id": "S37", "id": "alr_0001", "revision": 1,
        "content_hash": _hash_of(h.alignment_payload)}


def test_t20_72c_visual_only_native_shot_needs_no_alignment():
    """没有对白的镜头即使是原生音色也不需要对齐（§2.4 的条件是"与"）。"""
    h = Harness()
    shot = make_shot(dialogue=[])
    h.shot_payload["shot_spec"] = shot
    h.shot_payload["s06_meta"] = {**h.shot_payload["s06_meta"],
                                  "content_hash": sha("9")}
    h.ready()
    body = h.compile_body()
    body["audio_strategy"] = "native_locked_voice"
    out = h.service.compile(TENANT, "cmd_visual", body, "h")
    manifest = h.service.get_manifest(TENANT, out["manifest_id"])
    assert manifest["dialogue_alignment_ref"] is None
    assert manifest["dialogue_segments"] == []


# =========================================================== 11. HTTP 层

def test_http_compile_roundtrip(monkeypatch, tmp_path):
    """HTTP 全链路：201 → GET 拿到同一份 manifest → 事件恰好一行。"""
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    created = post(client, "/prompt-compilations", h.compile_body())
    assert created.status_code == 201
    body = created.json()
    assert body["replayed"] is False
    assert body["revision"] == 1

    fetched = get(client, f"/prompt-compilations/{body['manifest_id']}")
    assert fetched.status_code == 200
    manifest = fetched.json()
    assert manifest["content_hash"] == body["content_hash"]
    assert manifest["prompt_text"]
    assert len(h.outbox_rows()) == 1


def test_http_missing_idempotency_key_is_400(monkeypatch, tmp_path):
    """缺 `Idempotency-Key` ⇒ 400 `MALFORMED_REQUEST`，不落库。

    这个头不是可选的：没有它，"重投"就无法被识别，任何超时重试都会产生第二
    份 manifest 或第二次付费。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    response = client.post("/prompt-compilations", json=h.compile_body(),
                           headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert h.manifest_count() == 0


def test_http_command_id_mismatch_is_400(monkeypatch, tmp_path):
    """头里的键与 body 里的 `command_id` 不一致 ⇒ 400（不是"随便取一个"）。"""
    client, h, _module = _http_client(monkeypatch, tmp_path)
    h.ready()
    body = {**h.compile_body(), "command_id": "cmd_in_body"}
    response = post(client, "/prompt-compilations", body,
                    command_id="cmd_in_header")
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert h.manifest_count() == 0


def test_http_adapter_path_is_authoritative(monkeypatch, tmp_path):
    """PUT adapter 的路径与 body 的 id/revision 不一致 ⇒ 400。

    否则调用方以为写的是 A、实际覆盖了 B。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    payload = {**ADAPTER, "adapter_id": "adb_other"}
    response = client.put(
        f"/adapters/{ADAPTER['adapter_id']}/revisions/{ADAPTER['revision']}",
        json=payload,
        headers={"Idempotency-Key": "c1", "X-Tenant-Id": TENANT})
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"
    assert h.conn.execute(
        "SELECT count(*) FROM adapter_binding").fetchone()[0] == 0


def test_http_readiness_lists_contract_gaps(monkeypatch, tmp_path):
    """`/health/readiness` 报告 `CONTRACT_GAPS`。

    缺口必须能被运行时读到：只写在源码里的缺口在事故复盘时等同于不存在。
    """
    client, h, _module = _http_client(monkeypatch, tmp_path)
    response = client.get("/health/readiness")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert [g["id"] for g in body["contract_gaps"]] == \
        [g["id"] for g in CONTRACT_GAPS]
    assert len(body["contract_gaps"]) == len(CONTRACT_GAPS) > 40


def test_http_error_envelope_has_no_traceback(monkeypatch, tmp_path):
    """500 类未处理异常也不泄漏内部路径。

    这里用一个必然触发上游未配置的路径：`S20_SHOT_URL` 未设 → `deps`
    闭包返回 503（不是 500）。断言的是响应体形状，不是状态码。
    """
    client, h, module = _http_client(monkeypatch, tmp_path)
    h.ready()   # 模板与 adapter 先就位，失败点才会落在打镜头上
    # 装回 `api/app.py` 启动时的 service：它的上游是 `deps` 的未配置闭包
    # （`S06_BASE_URL` 为空），所以打镜头那一步必然是 503。
    module.service = PromptCompilerService(
        module._conn,
        shot_source=build_shot_source(None),
        binding_source=build_binding_source(None),
        capability_source=build_capability_source(None),
        alignment_source=build_alignment_source(None))
    response = post(client, "/prompt-compilations", h.compile_body())
    assert response.status_code == 503
    payload = response.json()
    assert payload["retryable"] is True
    # T20-37 的"`Retry-After` 存在"是 HTTP 层的事 —— 内核只给 `retryable`，
    # 头由异常处理器加。没有它，调用方只能靠猜什么时候重试。
    assert response.headers["Retry-After"] == "2"
    for leak in ("Traceback", "site-packages", "File \""):
        assert leak not in response.text


# =========================================================== 12. 错误码逐条（§9 表格里每个码都要有能叫出它的用例）

def _codes_in(exc_info) -> str:
    return exc_info.value.code


def test_code_binding_source_unavailable():
    """`BINDING_SOURCE_UNAVAILABLE`：S10 未配置（闭包为 None）。

    F19：缺一个 pin 就是 503，不是"少一张参考图也能编"。
    """
    h = Harness()
    h.ready(with_template=True)
    h.service.binding_source = None
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "BINDING_SOURCE_UNAVAILABLE"
    assert exc.value.status == 503
    assert exc.value.retryable is True
    assert h.manifest_count() == 0


def test_code_shot_source_unavailable():
    h = Harness()
    h.ready(with_adapter=True)
    h.service.shot_source = None
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "SHOT_SOURCE_UNAVAILABLE"
    assert exc.value.status == 503


def test_code_capability_source_unavailable():
    h = Harness()
    h.ready()
    h.service.capability_source = None
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "CAPABILITY_SOURCE_UNAVAILABLE"
    assert exc.value.status == 503


def test_code_alignment_source_unavailable():
    """有对白 + 原生音色时，S37 未配置 ⇒ 503（不是"估一个时长"）。"""
    h = Harness()
    h.ready()
    h.service.alignment_source = None
    body = h.compile_body()
    body["audio_strategy"] = "native_locked_voice"
    body["dialogue_alignment_ref"] = {"id": "alr_0001", "revision": 1}
    with pytest.raises(ServiceError) as exc:
        h.service.compile(TENANT, "cmd_x", body, "h")
    assert _codes_in(exc) == "ALIGNMENT_SOURCE_UNAVAILABLE"
    assert exc.value.status == 503


def test_code_invalid_schema():
    """`INVALID_SCHEMA`：400，形状问题在第一步就拒。"""
    h = Harness()
    h.ready()
    with pytest.raises(ServiceError) as exc:
        h.service.compile(TENANT, "cmd_x", {"target": "not-an-object"}, "h")
    assert _codes_in(exc) == "INVALID_SCHEMA"
    assert exc.value.status == 400
    with pytest.raises(ServiceError) as exc2:
        h.service.compile(TENANT, "cmd_y", h.compile_body(target={
            "shot_ref": {"id": "sht_0001"}}), "h")
    assert _codes_in(exc2) == "INVALID_SCHEMA"


def test_code_schema_incompatible_missing_shot_key():
    """`SCHEMA_INCOMPATIBLE`：镜头缺 `SHOT_REQUIRED` 的键。"""
    h = Harness()
    h.ready()
    broken = make_shot()
    broken.pop("camera")
    h.shot_payload["shot_spec"] = broken
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "SCHEMA_INCOMPATIBLE"
    assert exc.value.status == 422
    assert "camera" in exc.value.details["missing"]


def test_code_schema_incompatible_prose_beats():
    """`SCHEMA_INCOMPATIBLE`：`beats` 是散文 ⇒ 拒绝解析（F5）。"""
    h = Harness()
    h.shot_payload["shot_spec"] = make_shot(
        beats=["她推开门停住，然后看向桌上的信"])
    h.shot_payload["s06_meta"] = {**h.shot_payload["s06_meta"],
                                  "content_hash": sha("9")}
    h.ready()
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "SCHEMA_INCOMPATIBLE"
    assert exc.value.status == 422


def test_code_shot_revision_moved():
    """`SHOT_REVISION_MOVED`：调用方 pin 的 `content_hash` 与 S06 当前不同。

    409 且 retryable：这是乐观并发，不是失败 —— 回报当前 ref 让调用方自己
    决定要不要在新版上重编。
    """
    h = Harness()
    h.ready()
    body = h.compile_body(target={"shot_ref": {
        "service_id": "S06", "id": "sht_0001", "revision": 3,
        "content_hash": sha("z")}})
    with pytest.raises(ServiceError) as exc:
        h.service.compile(TENANT, "cmd_x", body, "h")
    assert _codes_in(exc) == "SHOT_REVISION_MOVED"
    assert exc.value.status == 409
    assert exc.value.retryable is True
    assert exc.value.details["current"]["content_hash"] == sha("s")


def test_code_source_revision_moved_for_template_and_adapter():
    """`SOURCE_REVISION_MOVED`：模板或 adapter 的 pin 与库里不符。

    两条路径都很重要：模板改了 hash 意味着有人动过一版已发布的内容（P0），
    adapter 改了意味着同一 revision 上换了映射（也 P0）。
    """
    h = Harness()
    h.ready()
    body = h.compile_body(target={"template_ref": {
        "id": "tpl_shot_video", "revision": 1, "content_hash": sha("w")}})
    with pytest.raises(ServiceError) as exc:
        h.service.compile(TENANT, "cmd_a", body, "h")
    assert _codes_in(exc) == "SOURCE_REVISION_MOVED"
    assert exc.value.status == 409
    assert exc.value.details["source"] == "S20"

    h2 = Harness()
    h2.ready()
    body = h2.compile_body(target={"adapter_binding_ref": {
        "id": ADAPTER["adapter_id"],
        "content_hash": sha("v")}})
    with pytest.raises(ServiceError) as exc2:
        h2.service.compile(TENANT, "cmd_b", body, "h")
    assert _codes_in(exc2) == "SOURCE_REVISION_MOVED"
    assert exc2.value.details["source"] == "S20"
    assert exc2.value.details["selected"]["id"] == ADAPTER["adapter_id"]
    # 点名一个**别的** adapter 也是同一个码：调用方读了过期数据。
    body = h2.compile_body(target={"adapter_binding_ref": {
        "id": "adb_some_other"}})
    with pytest.raises(ServiceError) as exc3:
        h2.service.compile(TENANT, "cmd_c", body, "h")
    assert _codes_in(exc3) == "SOURCE_REVISION_MOVED"


def test_code_reference_dangling():
    """`REFERENCE_DANGLING`：ShotSpec 声明了 adapter **没点名**的资产，而绑定集
    里也没有（F6 —— 该改 S10）。
    """
    h = Harness()
    h.template()
    # adapter 必须以"缩减规则"入库（同一快照只能有一版 adapter，S20-50）。
    h.adapter(reference_order_rule={
        "roles": [{"role": "character", "position": 0},
                  {"role": "costume", "position": 1},
                  {"role": "scene", "position": 2}],
        "allow_leftover": False})
    h.publish()
    entries = [
        _entry("character:chr_linlan", "character", "S11", "chr_linlan", 12,
               sha("1")),
        _entry("costume:cst_coat", "costume", "S12", "cst_coat", 2, sha("2")),
        _entry("scene:scn_hall", "scene", "S13", "scn_hall", 4, sha("4")),
    ]
    h.binding_payload = make_binding_set(entries=entries)
    with pytest.raises(ServiceError) as exc:
        h.compile()
    assert _codes_in(exc) == "REFERENCE_DANGLING"
    assert exc.value.status == 422
    assert [m["role"] for m in exc.value.details["missing"]] == ["prop"]
    assert h.manifest_count() == 0


def test_code_template_not_published_on_probe():
    """同一码 `TEMPLATE_NOT_PUBLISHED` 在 probe 路径上也成立。"""
    h = Harness()
    h.template()
    h.adapter()
    _insert_draft(h, revision=1)
    with pytest.raises(ServiceError) as exc:
        h.service.probe(TENANT, {"template_ref": {"id": "tpl_shot_video",
                                                  "revision": 1},
                                 "model_snapshots": [
                                     "seedance-1.0-pro@2026-10"]})
    assert _codes_in(exc) == "TEMPLATE_NOT_PUBLISHED"


def test_code_template_retired_on_probe():
    h = Harness()
    h.ready()
    h.service.retire_revision(TENANT, "cmd_r", "tpl_shot_video", 1, {}, "h")
    with pytest.raises(ServiceError) as exc:
        h.service.probe(TENANT, {"template_ref": {"id": "tpl_shot_video",
                                                  "revision": 1},
                                 "model_snapshots": [
                                     "seedance-1.0-pro@2026-10"]})
    assert _codes_in(exc) == "TEMPLATE_RETIRED"


def test_code_adapter_not_applicable_on_probe():
    """`ADAPTER_NOT_APPLICABLE`：adapter revision 落在模板区间外。"""
    h = Harness()
    h.ready()
    h.adapter(revision=9, model_snapshot="seedance-1.0-pro@2026-11")
    with pytest.raises(ServiceError) as exc:
        h.compile(target={"model_snapshot": "seedance-1.0-pro@2026-11"})
    assert _codes_in(exc) == "ADAPTER_NOT_APPLICABLE"
    assert exc.value.status == 422
    assert exc.value.details["adapter_revision"] == 9
    assert exc.value.details["max"] == 5


# =========================================================== 13. 探测与适配器读取

def test_probe_reports_adapter_and_optional_omissions_only():
    """§2.6：probe 回答 adapter × 区间 × 可选省略，**不评估能力**。

    §4.1 的 `note` 字段是这个限制的显式记录（S20-41）—— 没有它，调用方会把
    "probe 通过"读成"能编"。
    """
    assert_gap("S20-41")
    h = Harness()
    h.ready()
    result = h.service.probe(TENANT, {
        "template_ref": {"id": "tpl_shot_video", "revision": 1},
        "model_snapshots": ["seedance-1.0-pro@2026-10"],
        "requirement_set_ref": {"id": "rq_1", "revision": 2}})
    assert result["requirement_set_ref"] == {"id": "rq_1", "revision": 2}
    first = result["results"][0]
    assert first["model_snapshot"] == "seedance-1.0-pro@2026-10"
    assert first["verdict"] == "compatible"
    assert first["adapter_binding_ref"]["id"] == ADAPTER["adapter_id"]
    assert first["adapter_binding_ref"]["revision"] == ADAPTER["revision"]
    assert first["missing_required_capabilities"] == []
    assert "S20-41" in result["note"]
    assert h.manifest_count() == 0


def test_probe_batch_limit():
    """§2.6 的批量上限：超过就 400，不静默截断。"""
    h = Harness()
    h.ready()
    with pytest.raises(ServiceError) as exc:
        h.service.probe(TENANT, {
            "template_ref": {"id": "tpl_shot_video", "revision": 1},
            "model_snapshots": [f"seedance-1.0-pro@2026-{i:02d}"
                                for i in range(1, 40)]})
    assert _codes_in(exc) == "INVALID_SCHEMA"
    assert exc.value.status == 400
    assert exc.value.details["limit"] < 40


def test_adapter_read_is_tenant_scoped_and_roundtrips():
    """adapter 读回的形状与写入一致，且跨租户不可见。"""
    h = Harness()
    h.adapter()
    got = h.service.get_adapter(TENANT, ADAPTER["adapter_id"],
                                ADAPTER["revision"])
    assert got["adapter_id"] == ADAPTER["adapter_id"]
    assert got["revision"] == ADAPTER["revision"]
    assert got["reference_token_format"] == "<image_{n}>"
    assert got["phrase_tables"]["camera.shot_size"]["medium"]["text"] == "中景"
    assert got["content_hash"] == h.conn.execute(
        "SELECT content_hash FROM adapter_binding WHERE adapter_id=?",
        (ADAPTER["adapter_id"],)).fetchone()[0]
    with pytest.raises(ServiceError) as exc:
        h.service.get_adapter(OTHER_TENANT, ADAPTER["adapter_id"],
                              ADAPTER["revision"])
    assert exc.value.code == "NOT_FOUND"


def test_template_create_is_idempotent_and_tenant_scoped():
    """建模板可重投；两个租户可以有同名 `template_id` 互不影响。"""
    h = Harness()
    first = h.template()
    # 内核层：重复建是 409（S20-36 找不到更合适的码），不是"返回上一条"。
    with pytest.raises(ServiceError) as exc:
        h.service.create_template(TENANT, "cmd_other", {
            "template_id": "tpl_shot_video", "name": "镜头视频模板",
            "modality": "video", "provider": "volcengine"}, "h")
    assert exc.value.code == "TEMPLATE_EXISTS"
    assert exc.value.status == 409
    assert h.template_count() == 1
    assert first["current_revision"] == 0
    other = h.service.create_template(OTHER_TENANT, "cmd_tpl_tpl_shot_video", {
        "template_id": "tpl_shot_video", "name": "镜头视频模板",
        "modality": "video", "provider": "volcengine"}, "h")
    assert other["template_id"] == "tpl_shot_video"
    assert h.template_count() == 2
