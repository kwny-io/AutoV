"""拉起 studio-web 本地预览：后端单进程 + 前端静态托管 + 一份演示数据。

用法：`python apps/studio-web/launcher.py`

播种只走服务自己的写入口。S47 的入口是 `POST /events:consume`（平台投递
路径），那是它唯一合法的写入方式；绕过去直接 INSERT，界面看到的东西就不
再经过真实的事件应用逻辑。

其余服务的写入口都挂在 S01 授权上下文后面，而写权限键不在
`contracts/identity/permission-registry.v2.json` 里（缺口 G5，服务侧返回
503 `PERMISSION_KEY_UNREGISTERED`）。这些页面如实显示"服务在线、暂无记录"
并写明原因，不硬造数据——硬造就得直接写别人的库，那是 AGENTS.md 第一条
禁止的事。

演示数据标 `tenant-demo`，界面所有数字都带"演示数据"标记；一次模型调用
都不发（AGENTS.md：真实付费生成要显式授权）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RUN = HERE / "_run"

TENANT = "tenant-demo"
BACKEND_PORT = 8800
WEB_PORT = 8080

RUN_SPECS = [
    # (run_id, episode, branch, state, shots, spent_micros)
    ("run-ep01", "ep-01", "canon", "SUCCEEDED", 24, 8_420_000),
    ("run-ep02", "ep-02", "canon", "RUNNING", 31, 5_130_000),
    ("run-ep02-alt", "ep-02", "branch-b", "WAITING_REVIEW", 18, 2_980_000),
    ("run-ep03", "ep-03", "canon", "RUNNING", 27, 3_260_000),
    ("run-ep03-alt", "ep-03", "branch-alt", "PAUSED", 12, 940_000),
    ("run-ep04", "ep-04", "canon", "FAILED", 19, 1_770_000),
    ("run-ep05", "ep-05", "canon", "QUEUED", 22, 0),
    ("run-ep06", "ep-06", "canon", "WAITING_BUDGET", 15, 610_000),
]

TIMELINE = {
    "SUCCEEDED": ["QUEUED", "RUNNING", "SUCCEEDED"],
    "RUNNING": ["QUEUED", "RUNNING"],
    "WAITING_REVIEW": ["QUEUED", "RUNNING", "WAITING_REVIEW"],
    "PAUSED": ["QUEUED", "RUNNING", "PAUSED"],
    "FAILED": ["QUEUED", "RUNNING", "FAILED"],
    "QUEUED": ["QUEUED"],
    "WAITING_BUDGET": ["QUEUED", "RUNNING", "WAITING_BUDGET"],
}


def wait_ready(port, path="/docs", timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def post(path, body, tenant=TENANT):
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{BACKEND_PORT}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Tenant-Id": tenant,
                 "Idempotency-Key": body.get("command_id", ""),
                 "X-Principal-Id": "principal-demo"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except Exception as exc:
        return 0, repr(exc)


def seed():
    """按真实事件契约播种。

    两个坑都是踩过的：

    1. 投影读的是 `data.completed_nodes`（契约字段名），不是 `completed_shots`
       ——后者只是 §4.2 的对外展示名（C47-16）。发错了服务会静默忽略，
       界面就显示成 0。
    2. `total_shots` 在 S47 里**根本没有来源**（同一处 C47-16），发过去也
       不会有任何效果。所以这里不发，界面也不假装知道总镜数。

    花费走 `finance.ledger.entry-posted`（SETTLE + budget_after.consumed_micro），
    那是投影唯一接受金额的路径：RESERVE/RELEASE 不算花费。
    """
    applied = 0
    for index, (run_id, episode, branch, state, shots, spent) in enumerate(
            RUN_SPECS):
        for sequence, run_state in enumerate(TIMELINE[state], start=1):
            envelope = {
                "id": f"ev-{run_id}-{sequence}",
                "tenant_id": TENANT,
                "type": "orchestration.run.state-changed",
                "sequence": sequence,
                "time": f"2026-11-14T06:{index * 2 + sequence:02d}:00.000Z",
                "data": {
                    "run_id": run_id,
                    "state": run_state,
                    "project_id": "proj-demo",
                    "episode_id": episode,
                    "branch_id": branch,
                    "completed_nodes": 0 if sequence == 1
                    else int(shots * .65),
                },
            }
            status, payload = post("/s47/events:consume", {"event": envelope})
            if status == 200 and isinstance(payload, dict) and \
                    payload.get("applied"):
                applied += 1
            elif status != 200:
                print(f"  ! {run_id}#{sequence}: {status} {payload}")

        if spent:
            ledger = {
                "id": f"ev-{run_id}-settle",
                "tenant_id": TENANT,
                "type": "finance.ledger.entry-posted",
                "time": f"2026-11-14T06:{index * 2 + 5:02d}:30.000Z",
                "data": {
                    "entry_type": "SETTLE",
                    "budget_after": {"consumed_micro": spent},
                    "attribution": {"run_id": run_id, "project_id": "proj-demo",
                                    "episode_id": episode, "branch_id": branch},
                },
            }
            status, payload = post("/s47/events:consume", {"event": ledger})
            if status == 200 and isinstance(payload, dict) and \
                    payload.get("applied"):
                applied += 1
            elif status != 200:
                print(f"  ! {run_id} settle: {status} {payload}")

    print(f"  S47 投影：应用 {applied} 条事件 / {len(RUN_SPECS)} 个 run")

    # 空库下先确认其余服务的读入口在线（写入口受缺口 G5 限制）
    for path, label in [("/s03/projects", "项目目录"),
                        ("/s40/review-tasks", "审片任务"),
                        ("/s22/capacity", "容量"),
                        ("/s43/delivery-packages", "交付包")]:
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{BACKEND_PORT}{path}",
                    headers={"X-Tenant-Id": TENANT}), timeout=10) as r:
                print(f"  {label}: {r.status}")
        except Exception as exc:
            print(f"  ! {label}: {exc}")


def main():
    RUN.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["STUDIO_DB_DIR"] = str(RUN)
    env["STUDIO_BACKEND_PORT"] = str(BACKEND_PORT)

    print("启动后端（48 个服务单进程挂载）…")
    backend = subprocess.Popen(
        [sys.executable, str(HERE / "backend.py")], cwd=str(REPO),
        env=env, stdout=(RUN / "backend.log").open("ab"),
        stderr=subprocess.STDOUT)
    if not wait_ready(BACKEND_PORT, "/_studio/services"):
        print("后端没起来，日志：apps/studio-web/_run/backend.log")
        backend.terminate()
        return 1
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{BACKEND_PORT}/_studio/services",
                timeout=10) as r:
            mounted = json.loads(r.read())["mounted"]
        failed = {k: v for k, v in mounted.items()
                  if str(v).startswith("FAILED")}
        print(f"  挂载 {len(mounted) - len(failed)}/48"
              + (f"，失败 {failed}" if failed else ""))
    except Exception as exc:
        print(f"  ! 挂载清单读取失败：{exc}")

    print("播种演示数据…")
    seed()

    print("启动前端…")
    front = subprocess.Popen(
        [sys.executable, str(HERE / "serve.py")], cwd=str(REPO),
        env={**env, "STUDIO_WEB_PORT": str(WEB_PORT)},
        stdout=(RUN / "front.log").open("ab"), stderr=subprocess.STDOUT)
    if not wait_ready(WEB_PORT, "/services.json", timeout=30):
        print("前端没起来，日志：apps/studio-web/_run/front.log")
        front.terminate()
        backend.terminate()
        return 1

    print(f"\n  打开 http://127.0.0.1:{WEB_PORT}/\n  Ctrl-C 结束\n")
    try:
        front.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (front, backend):
            proc.terminate()
        for proc in (front, backend):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
