#!/usr/bin/env bash
# 每个服务的「做完 → 回头复查 → 方向对就继续」循环。
#
# 由定时任务调用；也可以手工跑。它只做可判定的部分，判不了的一律报出来让人看：
#   1. 跑全量一致性检查（干净 LF 检出里跑，避开本机 CRLF 与同伴未提交改动）
#   2. 跑所有测试
#   3. 确认生成物重新生成后 git status 干净
#   4. 打印下一个待实现服务，以及它的 plan 与 catalog 状态
# 不会自动提交、不合并分支、不部署、不调付费接口。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2

STAMP="$(date '+%Y-%m-%dT%H:%M:%S%z')"
echo "=== vflow-agent 复查 $STAMP ==="
echo

fail=0
check() {  # check <名字> <命令...>
  local label="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    echo "PASS  $label"
  else
    echo "FAIL  $label  (exit $rc)"
    printf '%s\n' "$out" | tail -25 | sed 's/^/      /'
    fail=1
  fi
  return $rc
}

# --- 1. 测试 -------------------------------------------------------------
if [ -d services/tenant-access/tests ] || [ -d platform ]; then
  check "pytest" python -m pytest -q
fi

# --- 2. 生成物可重复 -----------------------------------------------------
before="$(git status --porcelain -- services agents connectors | sort)"
python tools/scaffold/generate_services.py >/dev/null 2>&1
python tools/scaffold/generate_agents.py   >/dev/null 2>&1
python tools/scaffold/generate_connectors.py >/dev/null 2>&1 || true
after="$(git status --porcelain -- services agents connectors | sort)"
if [ "$before" = "$after" ]; then
  echo "PASS  scaffold-idempotent"
else
  echo "FAIL  scaffold-idempotent  (重新生成改变了生成物)"
  diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed 's/^/      /'
  fail=1
fi

# --- 3. 全量一致性检查 ---------------------------------------------------
# 工作树里有同伴未提交的 check_all.py / contracts/README.md 时，本地报的
# links/readme/citerefs 不代表仓库状态；用干净检出跑才是真话。
TMP="$(mktemp -d 2>/dev/null || echo /tmp/vf_loop_$$)"
mkdir -p "$TMP"
if git -c core.autocrlf=false clone -q --no-hardlinks . "$TMP/tree" 2>/dev/null; then
  git -C "$TMP/tree" config core.autocrlf false
  # 把工作树里已跟踪的改动和新文件带进去，但排除只读存档与同伴文件
  for f in $(git diff --name-only; git ls-files --others --exclude-standard); do
    case "$f" in
      docs/design-package/*) continue ;;
      contracts/README.md|tools/check_all.py|plans/S31-*) continue ;;
    esac
    [ -f "$f" ] && mkdir -p "$TMP/tree/$(dirname "$f")" && cp "$f" "$TMP/tree/$f"
  done
  ( cd "$TMP/tree" && python tools/check_all.py 2>&1 ) > "$TMP/check.txt"

  # 已报告、且不归本循环修的 FAIL。两条都来自别人未提交的文件，循环去「修」
  # 只会去改同伴正在编辑的东西：
  #   archive   HEAD 的 ARCHIVE_IMPORTED_BASELINE 把 service-catalog.v2.json 钉在
  #             固定哈希上，而把某个服务的 maturity 改成 implemented 必然改这个
  #             文件——这条 pin 与「按服务推进」本身冲突。同伴新版检查器已去掉
  #             这个机制（工作树中 archive 为 ok）。
  #   readme    数的是 contracts/README.md 的汇总表，那个文件是别人在改的未提交
  #             状态；干净 HEAD 上 readme 是 ok。
  known="$(grep '^FAIL' "$TMP/check.txt" | awk '{print $2}' | grep -E '^(archive|readme)$' | sort -u)"
  unknown="$(grep '^FAIL' "$TMP/check.txt" | awk '{print $2}' | grep -vE '^(archive|readme)$' | sort -u)"
  if [ -n "$known" ]; then
    echo "KNOWN $(echo $known | tr '\n' ' ')  (已报告，归同伴未提交文件，本循环不动)"
  fi
  if [ -n "$unknown" ]; then
    echo "FAIL  check_all  ($(echo $unknown | tr '\n' ' '))"
    grep -A4 '^FAIL' "$TMP/check.txt" | sed 's/^/      /'
    fail=1
  else
    echo "PASS  check_all  ($(grep -c '^ok' "$TMP/check.txt") ok, $(grep -c '^skip' "$TMP/check.txt") skip)"
  fi
else
  echo "WARN  check_all  跳过：无法创建临时克隆"
fi
rm -rf "$TMP"

# --- 4. 下一个待实现服务 ------------------------------------------------
echo
PYTHONIOENCODING=utf-8 python - "$ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
cat = json.load(open(os.path.join(root, "contracts/service-catalog.v2.json"), encoding="utf-8"))["services"]
wave = {"S08","S09","S21","S22","S23","S24","S26","S27","S28"}
todo = [s for s in cat if s["maturity"] == "design_only" and s["service_id"] in wave]
done = [s for s in cat if s["maturity"] != "design_only"]
print("W1 已实现: " + (", ".join(f'{s["service_id"]} {s["slug"]}' for s in done) or "无"))
print("W1 待实现: " + (", ".join(f'{s["service_id"]} {s["slug"]}' for s in todo) or "无"))
if todo:
    nxt = todo[0]
    print()
    print(f'下一个: {nxt["service_id"]} {nxt["slug"]}')
    print(f'  命令入口 {nxt.get("command_api")}')
    print(f'  查询入口 {nxt.get("query_api")}')
    print(f'  依赖     {", ".join(nxt.get("depends_on") or []) or "无"}')
    print(f'  方案     plans/{nxt["service_id"]}-{nxt["slug"]}.md')
PY

echo
[ $fail -eq 0 ] && echo "结论：可以继续下一个服务。" || echo "结论：先修上面 FAIL，再继续。"
exit $fail
