"""把 48 个服务挂进一个进程，给 studio-web 本地预览用。

为什么不是「一个服务一个 uvicorn 进程」：`import fastapi` 单次约 2.4s，16 个
进程并发冷启动会互相争抢同一批 `.pyc`，实测要 60s 才全部就绪，而单进程挂载
是 6s。**这只用于本地预览**，不是部署形态——服务在生产里各自独立部署
（docs/16 §2），这个入口不改变那件事。

前缀是 `/s<n>/`，对应各 `service.yaml` 的 S 编号。前端通过
`/api/<slug>` 访问，`serve.py` 负责把 slug 映射到这里的路径。
"""

from __future__ import annotations

import glob
import os
import sys

from fastapi import FastAPI

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for name in ("event-sdk", "idempotency-sdk", "auth-sdk", "telemetry-sdk"):
    path = os.path.join(REPO, "platform", name, "src")
    if os.path.isdir(path):
        sys.path.insert(0, path)

DB_DIR = os.environ.get("STUDIO_DB_DIR")

app = FastAPI(title="studio-bff (local preview)", version="0.1.0")

# service 目录名 -> S 编号前缀。前缀取自各服务 app.title，不是猜的。
SLAUGS = [
    ("tenant-access", "s01"), ("governance-policy", "s02"),
    ("project-catalog", "s03"), ("narrative", "s04"),
    ("breakdown", "s05"), ("storyboard", "s06"),
    ("change-management", "s07"), ("revision-snapshot", "s08"),
    ("lineage-impact", "s09"), ("asset-catalog", "s10"),
    ("character-assets", "s11"), ("costume-assets", "s12"),
    ("scene-assets", "s13"), ("prop-assets", "s14"),
    ("voice-assets", "s15"), ("story-memory", "s16"),
    ("continuity", "s17"), ("context-retrieval", "s18"),
    ("agent-runtime", "s19"), ("prompt-compiler", "s20"),
    ("orchestration", "s21"), ("scheduler-quota", "s22"),
    ("budget-ledger", "s23"), ("model-registry", "s24"),
    ("model-routing", "s25"), ("generation-jobs", "s26"),
    ("provider-connectors", "s27"), ("artifact-ingest", "s28"),
    ("image-production", "s29"), ("video-production", "s30"),
    ("speech-production", "s31"), ("lipsync", "s32"),
    ("sound-effects", "s33"), ("music", "s34"),
    ("technical-qc", "s35"), ("semantic-qc", "s36"),
    ("audio-alignment", "s37"), ("subtitles", "s38"),
    ("repair-planning", "s39"), ("human-review", "s40"),
    ("editorial", "s41"), ("composition", "s42"),
    ("delivery", "s43"), ("evaluation-analytics", "s44"),
    ("audit", "s45"), ("developer-integration", "s46"),
    ("realtime-projection", "s47"), ("adaptation-training", "s48"),
]

DB_ENV = {
    "tenant-access": "S01_DB", "governance-policy": "S02_DB",
    "project-catalog": "S03_DB", "narrative": "S04_DB",
    "breakdown": "S05_DB", "storyboard": "S06_DB",
    "change-management": "S07_DB", "revision-snapshot": "S08_DB",
    "lineage-impact": "S09_DB", "asset-catalog": "S10_DB",
    "character-assets": "S11_DB", "costume-assets": "S12_DB",
    "scene-assets": "S13_DB", "prop-assets": "S14_DB",
    "voice-assets": "S15_DB", "story-memory": "S16_DB",
    "continuity": "S17_DB", "context-retrieval": "S18_DB",
    "agent-runtime": "S19_DB", "prompt-compiler": "S20_DB",
    "orchestration": "S21_DB", "scheduler-quota": "S22_DB",
    "budget-ledger": "S23_DB", "model-registry": "S24_DB",
    "model-routing": "S25_DB", "generation-jobs": "S26_DB",
    "provider-connectors": "S27_DB", "artifact-ingest": "S28_DB",
    "image-production": "S29_DB", "video-production": "S30_DB",
    "speech-production": "S31_DB", "lipsync": "S32_DB",
    "sound-effects": "S33_DB", "music": "S34_DB",
    "technical-qc": "S35_DB", "semantic-qc": "S36_DB",
    "audio-alignment": "S37_DB", "subtitles": "S38_DB",
    "repair-planning": "S39_DB", "human-review": "S40_DB",
    "editorial": "S41_DB", "composition": "S42_DB",
    "delivery": "S43_DB", "evaluation-analytics": "S44_DB",
    "audit": "S45_DB", "developer-integration": "S46_DB",
    "realtime-projection": "S47_DB", "adaptation-training": "S48_DB",
}


def _prepare_env(service: str) -> None:
    if DB_DIR:
        os.makedirs(DB_DIR, exist_ok=True)
        os.environ[DB_ENV[service]] = os.path.join(DB_DIR, f"{service}.db")
    # 服务模块顶层就把连接建好了，所以环境变量必须在 import 之前设好。
    # 前端预览不配置上游地址：读不到的依赖应当 503，而不是拿假数据糊过去。


def load(service: str):
    root = os.path.join(REPO, "services", service)
    sys.path.insert(0, root)
    for sub in ("src", "api"):
        path = os.path.join(root, sub)
        if os.path.isdir(path):
            sys.path.insert(0, path)
    # 每个服务都叫 `api.app` / `deps`。用独立 module 名避免互相覆盖。
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"svc_{service.replace('-', '_')}_app",
        os.path.join(root, "api", "app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.app


LOADED: dict[str, str] = {}

for service, prefix in SLAUGS:
    _prepare_env(service)
    try:
        sub_app = load(service)
        app.mount(f"/{prefix}", sub_app)
        LOADED[prefix] = service
    except Exception as exc:  # 一个服务坏了不该拖垮整个预览
        LOADED[prefix] = f"FAILED: {type(exc).__name__}: {exc}"
    finally:
        # 清掉 `api` 包，下一个服务会重新登记自己的
        for name in [n for n in sys.modules
                     if n == "api" or n.startswith("api.")]:
            sys.modules.pop(name, None)


@app.get("/_studio/services")
def services():
    return {"mounted": LOADED}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("STUDIO_BACKEND_PORT", "8800")),
                log_level="warning")
