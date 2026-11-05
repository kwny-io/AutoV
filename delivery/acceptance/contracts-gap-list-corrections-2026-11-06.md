# `contracts/README.md`「设计时留下的缺口」前两条写错了

日期：2026-11-06　范围：`contracts/openapi.v2.yaml`、`contracts/mcp-tools.v2.design.json`、`docs/13-api-product.md`
状态：**#1 已撤销（说法不成立）；#2 已重写（缺口真实，但不是原来写的那件事）**

## 0. 一句话

README 说「21 个操作没有 `x-owner`」「MCP 工具没有 scope 字段」——
两条都不成立：归属字段叫 `x-service-id`，21 个操作全带；scope 叫 `required_scope`，12 个工具全带。
但顺着查下去，MCP 工具**确实**和 `docs/13` 对不上：13 个具名工具里 5 个没有对应条目，
JSON 里 4 个条目 `docs/13` 从没提过，还有一个 scope 值不在五档闭集里。

## 1. #1 不成立：归属字段存在，只是不叫那个名字

```
$ python - <<'PY'
import re
t=open('contracts/openapi.v2.yaml').read()
print('x-service-id:', len(re.findall(r'x-service-id:',t)))
print('x-owner     :', len(re.findall(r'x-owner',t)))
PY
x-service-id: 21
x-owner     : 0
```

21 个操作，21 个 `x-service-id`，一个不少。取值全部命中 `service-catalog.v2.json` 的
48 个 `service_id`，没有悬空引用：

| 行 | 操作 | 服务 | 路径 |
|---|---|---|---|
| 15 | `createEpisodeRun` | S21 | `/episodes/{episode_id}/runs` |
| 59 | `getRun` | S21 | `/runs/{run_id}` |
| 81 | `commandRun` | S21 | `/runs/{run_id}/commands` |
| 124 | `streamRun` | S47 | `/runs/{run_id}/events` |
| 152 | `planChange` | S07 | `/change-plans` |
| 191 | `applyChange` | S07 | `/change-plans/{plan_id}/apply` |
| 236 | `getEffectivePolicy` | S02 | `/projects/{project_id}/effective-policy` |
| 258 | `createPolicyRevision` | S02 | `/policies/{policy_id}/revisions` |
| 300 | `queryMemory` | S16 | `/series/{series_id}/memory/query` |
| 359 | `commitMemory` | S16 | `/series/{series_id}/memory/commits` |
| 407 | `createContextPack` | S18 | `/context-packs` |
| 446 | `commitManifest` | S08 | `/manifests/{manifest_id}/commits` |
| 491 | `getManifest` | S08 | `/manifests/{manifest_id}/revisions/{revision}` |
| 518 | `estimateRoute` | S25 | `/route-decisions` |
| 562 | `createGenerationJob` | S26 | `/generation-jobs` |
| 601 | `getAdmission` | S26 | `/admissions/{admission_id}` |
| 624 | `decideReview` | S40 | `/review-tasks/{review_task_id}/decisions` |
| 671 | `prepareDelivery` | S43 | `/delivery-packages` |
| 708 | `publishDelivery` | S43 | `/delivery-packages/{delivery_id}/publish` |
| 749 | `getBudget` | S23 | `/budgets/{budget_id}` |
| 787 | `getMetrics` | S44 | `/series/{series_id}/metrics` |

另有 5 处 `x-access-boundary: internal_or_explicit_service_scope`（行 394 / 478 / 505 / 592 / 615），
标的是不该对客户端直开内部边界的操作。

**结论**：工具可以直接按 `x-service-id` 自动归属。原文「所有权映射只存在于散文里，
工具无法自动归属到服务」是错的。`docs/design-package/contracts/README.md` 里也没有
`x-owner` 这个词——这是 README 自己写出来的，不是从设计包抄错的，所以改它不涉及存档对照。

## 2. #2 缺口真实，但不是「没有 scope 字段」

12 个工具全部带 `required_scope`：

| 工具 | `required_scope` |
|---|---|
| `drama.memory.query` | `read` |
| `drama.context.preview` | `read` |
| `drama.policy.get` | `read` |
| `drama.run.get` | `read` |
| `drama.change.plan` | `plan` |
| `drama.episode.generate` | `execute` |
| `drama.change.apply` | `execute` |
| `drama.run.command` | `execute` |
| `drama.delivery.prepare` | `execute` |
| `drama.review.decide` | `review` |
| `drama.policy.change` | `admin` |
| `drama.delivery.publish` | **`publish`** |

真正对不上的有三件事。

### 2.1 `publish` 不在五档闭集里

[docs/13:13](../../docs/13-api-product.md) 定的是 `read / plan / execute / review / admin` 五档。
`drama.delivery.publish` 用了第六个值 `publish`。四档都有实例，`plan` 只有一条，
`publish` 一条。要么五档表少了一档，要么这条该降到 `execute`——**这是授权粒度决策，不是提取**。

### 2.2 工具集合与 `docs/13` 的具名清单不一致

```
JSON 工具 12 | docs/13 具名 13 | 交集 8
docs/13 有、JSON 无 : artifact.compare, episode.plan, memory.extract, model.estimate, project.get
JSON 有、docs/13 无 : delivery.prepare, delivery.publish, policy.get, run.command
```

两侧各 5 / 4 条不同。`docs/13` 把这些工具按「只读 / 有界推理费 / 单独授权」三组摆放，
JSON 只有 12 条扁平清单，两边没法一一对上。是 `docs/13` 举例而非穷举，还是 JSON 漏了 5 个工具/
多了 4 个，**两读都成立，本文不选边**。

### 2.3 能直接确认的只有一件事

`model.estimate`、`episode.plan`、`memory.extract` 三个在 `docs/13` 里承担「要求 `plan:compute` 预算」
的角色，JSON 里没有对应工具——如果 JSON 是权威，那么这句话描述的能力在 MCP 面上不存在。
反过来若 `docs/13` 是权威，JSON 缺 3 个工具。这不是措辞差异，是能力清单差异。

## 3. 动过什么

- `contracts/README.md` 缺口 1：撤销。改成一条**已验证的现状**，并写明字段名是 `x-service-id` 不是 `x-owner`。
- `contracts/README.md` 缺口 2：按 §2 重写，把「没有 scope 字段」换成三条具体不一致，标 `NEEDS_OWNER`。
- **没有改 `openapi.v2.yaml`**（不需要改，字段是全的）。
- **没有改 `mcp-tools.v2.design.json`**（补/删工具、改 scope 都是授权决策）。
- **没有改 `docs/13`**。

## 4. 复核方式

```bash
python -I -c "
import re,json
t=open('contracts/openapi.v2.yaml',encoding='utf-8').read()
ids=set(re.findall(r'x-service-id:\s*(\S+)',t))
cat=json.load(open('contracts/service-catalog.v2.json',encoding='utf-8'))
known={s['service_id'] for s in cat['services']}
print('ops with owner:',len(ids),'unknown:',ids-known)
d=json.load(open('contracts/mcp-tools.v2.design.json',encoding='utf-8'))
print('scopes:',sorted({x['required_scope'] for x in d['tools']}))
"
```

`docs/13` 那条清单里 `memory.extract` / `model.estimate` 紧贴中文字符，
用 `\b[a-z_]+\.[a-z_]+\b` 抽会漏掉它们（`费model.estimate` 处没有词边界）。用不带 `\b` 的
`[a-z_]+\.[a-z_]+` 才能抽全 13 个。

## 5. 同一节里的统计数字：`payloads` 93 → 92

`contracts/README.md` §「一个入口跑完所有仓库级检查」新写入的数字里，有一处是错的。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` L899，`payloads` 检查项 | 93 条 `schema_ref` / `data_schema_ref` | **92 条** | 按「每加一个带 payload 的 topic，schema_ref 与 data_schema_ref 各 +1」推的，把 55 + 37 = 92 误写成 93。`schema_ref` 是每个 topic 一条、恒定 55 条；`data_schema_ref` 才是随 payload 增长的那一列（36 → 37）。上一轮提交前的值是 91（55 + 36），本轮只该 +1。 |
| 2 | `contracts/README.md` L903，`examples` 检查项 | 53 个示例 | **53 个示例（不改）** | 这条一度被我误判为应改成 54，理由是 `contracts/examples/` 下有 54 个 `.json`。但校验器打印的 `53 example(s) valid, 0 failing, 1 without a schema` 里，第三个数字是「没有对应 schema、被跳过」的示例（`capacity-plan.json`），不计入「对 schema 校验」的示例数。README 沿用的始终是 valid 数：HEAD~1 是 52 valid / 53 个 `.json`，HEAD~2 是 51 valid / 52 个 `.json`，两版 README 写的都是 valid 数。误改已撤回。 |

**两条的错因是同一类**：都是从表里数出来的量跳到手写的数，中间夹了一步心算。
第 1 条多算了 1；第 2 条换错了计数的基（文件数 vs 通过校验数）。
复核时两边都回到源头数：

```python
import json
reg = json.load(open('contracts/event-topics.v2.json', encoding='utf-8'))

def walk(o):
    if isinstance(o, dict):
        if 'event_type' in o:
            yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)

t = list(walk(reg))
s = sum(1 for x in t if 'schema_ref' in x)
d = sum(1 for x in t if 'data_schema_ref' in x)
print('topics', len(t), 'schema_ref', s, 'data_schema_ref', d, 'payloads', s + d)
```

输出 `topics 55 schema_ref 55 data_schema_ref 37 payloads 92`。

`payloads` 的语义是「两类引用加起来」，不是「payload schema 文件数」（后者是 37）。
一个 topic 同时有 `schema_ref` 和 `data_schema_ref`，所以 55 + 37 才是 README 那句话的数。

`examples` 的语义是「通过 schema 校验的示例数」，不是 `contracts/examples/` 下的文件数：
目录里共 56 个文件，其中 54 个 `.json`、2 个 `.yaml`（`runtime-safety.yaml`、
`service-interactions.yaml`）不参与校验，`.json` 里又有 1 个（`capacity-plan.json`）没有对应 schema。

## 6. README 里三处一直对不上的数：`json` 112 → 116、`data_schema_ref` 37 → 38（当时）、图里 19 → 17（当时）

第 5 节复核 `payloads` 与 `examples` 时，顺手把同一节其余的数字也回源数了一遍。
发现**三个在我这轮动手之前就已经是错的**，不是这轮新引入的。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` L962，`json` 检查项 | 112 个 JSON 解析 | **116 个** | 每轮新增 schema/示例后没有回填这一句。`git show HEAD:contracts/README.md`、`HEAD~1`、`HEAD~2` 三版都写 112，说明它不是某次改动引入的偏差，而是**从某轮起就再没跟过**。实测 `python tools/check_all.py` 这一行一直是活的：本轮之前是 114，本轮加两个文件后是 116。 |
| 2 | `contracts/README.md` L948–949，三层文件关系图 | `data_schema_ref──▶ 37 个 payload schema` / 「其余 19 个 envelope-only」 | **当时应为 38 / 17** | 同一句话在 L61 已经写对了（「38 个 topic 带，17 个仍是 envelope-only」），图里那份是**另一处独立抄写的数**，改一处不会连带改另一处。37 + 19 = 56 ≠ 55 这个恒等式本来就在报错：`schema_ref` 恒定 55 条，两类引用不该拆出比 topic 总数还多的数。 |
| 3 | 同上一格 | 同上 | 同上 | 与第 2 条同源：图把「带 payload 的 topic 数」和「envelope-only 的 topic 数」当成两个可独立维护的量写了两遍，而它们其实互补（39 + 16 = 55）。**本轮的修法是把两处各自的数都回源重算，不是只改一处让它们相等。** |

三条的错因是同一类，和第 5 节那两条一样：**同一事实在文件里写了两遍，改动只落到其中一遍**。
L61 与 L948–949 讲的是同一件事（多少个 topic 带 payload），却没有任何机制保证两者一致。

复核口径（本节所有数字都回这张表数，不沿用第 5 节的中间结果）：

```python
import json, glob
reg = json.load(open('contracts/event-topics.v2.json', encoding='utf-8'))

def walk(o):
    if isinstance(o, dict):
        if 'event_type' in o:
            yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)

t = list(walk(reg))
s = sum(1 for x in t if 'schema_ref' in x)
d = sum(1 for x in t if 'data_schema_ref' in x)
print('topics', len(t), 'schema_ref', s, 'data_schema_ref', d,
      'payloads', s + d, 'envelope-only', len(t) - d)
print('schema files', len(glob.glob('contracts/*.schema.json')))
print('example json', len(glob.glob('contracts/examples/*.json')))
```

写这份更正时输出 `topics 55 schema_ref 55 data_schema_ref 39 payloads 94 envelope-only 16`、
`schema files 48`、`example json 56`；`python tools/check_all.py` 打印
`ok json 116 file(s)`、`ok payloads 94 schema ref(s), 39 topic(s) with a payload schema`、
`55 example(s) valid, 0 failing, 1 without a schema`、`9 check(s) passed`。

`json` 的 116 与 `schema files` 的 48 相差很大，是正常的：`json` 数的是**全仓**能解析的 JSON
（含 `contracts/examples/*.json`、`contracts/service-catalog.v2.json`、`docs/design-package/` 下
的存档、生成物等），不是 `contracts/*.schema.json`。这两个数不能互相校验，这也是 L962 那句话
容易失守的原因——它没有对应的恒等式，只能靠每轮重跑 `check_all` 时顺手回填。
