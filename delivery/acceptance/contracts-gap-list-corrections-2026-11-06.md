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

## 7. S46 这一轮：我自己写错的两处，以及第 6 节留下的口径问题

写 `integration.command-accepted.v2.schema.json` 时有两处先写错、在探测/回源时发现并改掉。
两条都不是"文档里本来就有"的错，是我这一轮新引入的，所以按原写 → 正确 → 错因记下来。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | schema `properties.receipt_id.description` | 「见 ICA-8 的邻条 ICA-4 与 §9.1 的 E28 讨论」 | 「见本文件 `x-needs-owner` 第四条（「`receipt_id` 是裸主键」）」 | 裸主键这件事只写在 `x-needs-owner` 里，**没有编号**；`x-conflicts` 的 ICA-4 讲的是 `params_sha256` / `params_bytes`，ICA-8 讲的是示例的信封三处。我把一个"在 needs-owner 里"的说法硬套成了 conflicts 编号，等于造了一个指错门的交叉引用。 |
| 2 | `x-conflicts[ICA-3].s46_ddl` 与 `why_it_matters` | 「`NOT NULL` 且无 `DEFAULT` 的列有十二个」+ 列名清单一串（含 `receipt_id`，漏 `state`）；正文写「四个值对十二个必填列」 | 「必填且无 `DEFAULT` 的列有十三个」：显式 `NOT NULL` 十二个（`state` 在内），加 `receipt_id`（`text PRIMARY KEY`，主键隐含 `NOT NULL`）；正文改「四个值对十三个必填列」 | 我把 `receipt_id`（无 `NOT NULL` 字样）算进了"`NOT NULL` 列"，同时漏了 `state`（有 `NOT NULL`、无 `DEFAULT`，只是当时被我按"状态列有默认值"想当然了）。两种错互相抵消成 12，所以初看是对的。**回源方法：把 §5.2 行 489–524 的建表语句按列拆开逐行判 `NOT NULL` 与 `DEFAULT`**，不靠记忆数。 |

第 2 条值得单独说一句：**两个错误抵消成正确的总数，是最难自查的一类**。如果我只核"总数是不是 12"，
它一直是 12，看不出问题；只有把每一列单独判一遍，才会发现一个该在的不在、一个不该在的在。

复核口径（本节数字回这里，不沿用第 6 节）：

```python
import json, glob, re
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

# NOT NULL 判定要逐列，不能数总数
lines = open('plans/S46-developer-integration.md', encoding='utf-8').read().split('\n')
blk = '\n'.join(lines[488:524])
cols = [l for l in blk.split('\n')
        if re.search(r'^\s+\w+\s+[a-z]', l) and 'CONSTRAINT' not in l and 'CREATE' not in l]
nn = [c for c in cols if 'NOT NULL' in c and 'DEFAULT' not in c]
print('explicit NOT NULL, no DEFAULT:', len(nn))
for c in nn:
    print('   ', c.strip()[:70])
```

写这份更正时输出 `topics 55 schema_ref 55 data_schema_ref 40 payloads 95 envelope-only 15`；
`schema files 49`、`example json 57`；`python tools/check_all.py` 打印
`ok json 118 file(s)`、`ok payloads 95 schema ref(s), 40 topic(s) with a payload schema`、
`56 example(s) valid, 0 failing, 1 without a schema`、`9 check(s) passed`。
`NOT NULL` 那一条打印 12 行，加上隐含的 `receipt_id` 才是 13——这就是第 2 条错的来源。

第 6 节末尾那段说「`json` 的 116 与 `schema files` 的 48 相差很大」。那两句里的两个数都是**当时**的值：
本轮加了 1 个 schema、1 个示例，于是变成 118 / 49。**差值仍是 69，不是恒等式，仍然不能互相校验**——
这正说明第 6 节那句「只能靠每轮重跑 `check_all` 时顺手回填」是对的，因为它会随文件增减漂，而没有任何机制替它回填。

## 8. S47 这一轮：同一个字段我连错两版，两次都是没回读 DDL

写 `projection.cursor-advanced.v2.schema.json` 时，`data.run_id` 这一条我先后写成两个错误结论，
都被自己的行号复核抓回来了。第三次才对。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | schema `x-conflicts[PCA-5]` 初版 | 「`sse_cursor` 没有 `run_id` 列（§5.3 行 550–560）」 | 有，§5.3 行 554：`run_id text NOT NULL` | 凭「游标表只需要游标」的印象下结论，没打开 DDL |
| 2 | 同上，第二版 | 「`subscription_registration` 也有 `run_id`（行 566），两表各存一遍同一事实、没有约束让它们相等」 | `subscription_registration` **没有 `run_id` 列**；行 566 是 `tenant_id`。范围由 `scope_kind IN ('run','tenant')`（行 568）+ 可空 `scope_id`（行 569）表达 | 第一版错后只回读了 `sse_cursor` 一半，把「另一张表应该有」当成了「另一张表有」 |
| 3 | §4.5 错误码引用（PCA-6） | `SUBSCRIPTION_NOT_FOUND` 在行 466 | 行 465；行 466 是 `IDEMPOTENCY_KEY_REUSED` | 表里连续三行都以 `| 4` 开头（404/404/409），数行时按内容猜没按行号核 |
| 4 | 本节「复核口径」末段 | 「`118 − 49 = 69`，本轮变成 120 / 50，**差值仍是 70**」 | 差值从 69 变成 **70**，不是「仍是」 | 心算了两个两位数减法；前三条是没回读文档，这一条是没让脚本算 |

**第 2 条把结论改反了，改出来的是一个比原结论更硬的冲突**：DDL 承认租户级订阅（`scope_kind='tenant'`，
`scope_id` 可空），而 `sse_cursor.run_id` 是 `NOT NULL`、本事件的 `run_id` 又是必需项——
**租户级订阅在游标表和本事件两处都无处安放**；且租户级订阅要同时跟 N 个 run 的水位，
而 `last_sent_watermark` 只有一个值，`aggregate_sequence = watermark` 的跨 run 单调性同时消失
（与 PCA-1 是同一个洞的另一面）。

三处错因是同一个：**凭印象推断表里有什么**。第一次只改了一半，第二次改得比第一次更远。
所以我在这轮之后加了一条自查动作：**凡是 schema 里写「某表有/没有某列」，先用脚本按行号取出那一行、**
**把整段 DDL 的列名打出来对一遍**，不靠记忆。上面 33 处引用就是这么做过的（0 处不符）。

复核口径（本节数字回这张表数）：

```python
import json, glob
reg = json.load(open('contracts/event-topics.v2.json', encoding='utf-8'))
def walk(o):
    if isinstance(o, dict):
        if 'event_type' in o: yield o
        for v in o.values(): yield from walk(v)
    elif isinstance(o, list):
        for v in o: yield from walk(v)
t = list(walk(reg))
s = sum(1 for x in t if 'schema_ref' in x)
d = sum(1 for x in t if 'data_schema_ref' in x)
print('topics', len(t), 'schema_ref', s, 'data_schema_ref', d,
      'payloads', s + d, 'envelope-only', len(t) - d)
print('schema files', len(glob.glob('contracts/*.schema.json')))
print('example json', len(glob.glob('contracts/examples/*.json')))
```

写这份更正时输出 `topics 55 schema_ref 55 data_schema_ref 41 payloads 96 envelope-only 14`、
`schema files 50`、`example json 58`；`python tools/check_all.py` 打印
`ok json 120 file(s)`、`ok payloads 96 schema ref(s), 41 topic(s) with a payload schema`、
`57 example(s) valid, 0 failing, 1 without a schema`、`9 check(s) passed`。

第 7 节末尾那段说「`json` 的 118 与 `schema files` 的 49」（那处原文写的差值是 69，`118 − 49 = 69`，对）。
本轮又加了 1 个 schema、1 个示例，于是变成 120 / 50，差值是 **70**——不是「仍是 69」。
这不影响第 6 节那句结论（差值不是恒等式、两个数不能互相校验），但它说明**连差值本身也不是常量**：
每轮都在两个数上各加 1，所以差值每轮也加 1。想拿差值当校验和的话，同样得每轮回填。

写这一段时我先写成「差值仍是 70」——把 `118 − 49 = 69` 当成了 70。这是本节第 4 个错，
错因与上面三条不同：前三条是没回读文档，这一条是**心算了两个两位数的减法**。
规矩同样记下：**写进报告的每一个差、和、比值，都用脚本算一遍贴输出，不写在正文里现算**。
