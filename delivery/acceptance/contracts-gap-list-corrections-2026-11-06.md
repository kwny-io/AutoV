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

## 9. S04 这一轮：两个 JSON 转义错误 + 一个手数的 hex 长度

写 `narrative.script-revision-created.v2.schema.json` 时，前两个错都被校验器当场拦住（不是靠读，
是靠 `json.loads` 和 `validate_examples.py`）；第三个是我自己的探针脚本把计数器写反，报了个
假数字。三条都不是「文档理解错」，是「手上的活没做完就往下走」。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | schema `x-conflicts[NAR-5].description` | 把 `episode_id = "ep_..."` 的直引号直接放进 JSON 字符串里，未转义 | `episode_id = \"ep_...\"` | 写长段中文时逐字混进了一个 ASCII 双引号。这类错**只在 `json.loads` 时才现形**，肉眼扫一遍看不出来——`check_all` 的 `json` 项当场报 `Expecting ',' delimiter: line 226 column 369`。定位方法是写了一段逐字符的字符串状态机，把「在一个字符串内遇到一个后面不跟 `,}]:` 的引号」全打印出来（全文 2 处，同一行），没有靠 grep。 |
| 2 | `contracts/examples/narrative-script-revision-created.json` 的 `content_hash` / `line_set_hash` | 手写的 63 位十六进制串 | `hashlib.sha256(...).hexdigest()`，并 `assert len(x) == 64` | 又手数了一遍 hex 位数（第 8 节第 4 条同源：手算代替脚本）。校验器报的是 `does not match '^[0-9a-f]{64}$'`，给出的是**整串**，不告诉你差在哪一位，所以肉眼找不出来。改成用 `hashlib` 生成后 `assert` 长度，不再手写。 |
| 3 | 探针脚本 `tools/_tmp_nar_probe.py` 的正例计数 | 打印 `payload positives accepted: 0/8`，而 8 条逐行都是 `ACCEPT` | `8/8`——`p += probe(...)` 里 `probe` 返回的是「**有**错误」，正例通过时返回 `False`，把 `False` 相加就是 0 | 计数器把「通过」和「返回假」当成了同一件事。发现方式是逐行读输出：8 行全是 `ACCEPT`、末尾却是 `0/8`，两者不可能同时成立。修法是把 `p += probe(...)` 改成 `p += (not probe(...))`。**如果只抄末尾那个 `0/8` 去写报告，就会写成一个跟事实相反的结论**，所以报告里的 `8/8` 是重跑后取的。 |

附一条同轮但**没有产生错误输出**的核对：`check_all` 的 `json` 计数是 **122**，而
`contracts/*.json`（55）+ `contracts/examples/*.json`（59）只有 114，差的 8 个是
`contracts/domain-v1/`（5）与 `contracts/quality/`（3）。所以那个计数是**递归**的
（`glob('contracts/**/*.json', recursive=True)`），不是 `contracts/` 顶层。这一条先按 114 推断
再按 122 实测对出来的，不是直接读校验器源码得来的——记在这里是为了下一轮别再用 114 去核。

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

## 10. S13 这一轮：三条断言写错了方向，其中一条我自己先写成了反面

这一轮加 `asset.scene-revision-accepted.v2.schema.json`。写的过程中有六处自查。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | schema `x-conflicts[SCN-6]` 初版 | 「S09 与 S17 都不消费本事件」，并以「两份方案全文命中 0」为据 | 该结论对 S09/S17 成立，但对**全仓**不成立：S10 §5.3 行 625 明写消费 `asset.character.revision.accepted 等 \| S11–S15`，而 S10 §1.1 行 27 把场景资产列在 S13 名下，所以本事件落在那个「等」里 | 我拿「点名的那两个服务没提」当成了「没人消费」。**「某两个服务的方案里没写」与「没有人消费」是两个命题**，前者我核对过（确实 0 命中），后者我没核对就写下了。改法是把结论收窄到能被证据支持的范围，并把 S10 那条一并写进去——它恰好让问题更尖锐：本事件被读的键（`scene_ref.revision`/`content_hash`）与它宣称存在的理由（`topology_hash`/`topology_changed`）是两组不相交的键 |
| 2 | schema `x-conflicts[SCN-1]` 初版 `s07_plan` 字段 | 「S07 的方案里没有 `impact_plan` 这个对象」 | S07 §2.1 行 112 的对象叫 `change_plan`、主键列叫 `plan_id`。说的是「名字不同」，不是「不存在」 | 我为了强调「三个名字对不上」把它写成了「不存在」，这会让读者以为 S07 缺这个能力。实际相反：S07 有完整实现（含 `plan_hash` 与 `cp_hash_uniq`），缺的是 S13 的 ref 指向它的名字 |
| 3 | 示例 `data.impact_plan_ref` | 我一度把它写成 `{"service_id":"S07","id":"plan_01J...","revision":1}`——一个能自圆其说的值 | 改回方案原文的 `{"service_id":"S09","id":"impact_01J...","revision":1}` | **这不是笔误，是选边**。SCN-1 的立场是「三处文档互不相同，本 schema 不替 owner 选」，而示例一旦写成 S07，就等于用示范数据裁定了争议。示例必须保持方案里的原样，让冲突在数据里也看得见——探针正例因此专门保留了三组互相矛盾的 `impact_plan_ref` 形态（`null`、S07 的 `plan_`、S09 的 `analysis_`），全部放行 |

另记一条**已知漏检**（不是本轮引入的缺陷）：信封的 `source` 与 `type` 都只收非空字符串
（`event-envelope.v2.schema.json` 的 `source` 是 `{"type":"string","minLength":1}`）。把示例的
`source` 改成 `S14` 而载荷仍是场景事件时，payload 层与 envelope 层都放行。本仓信封不对任何
topic 钉生产者与主题的对应关系。这不是新问题，但它决定了「A 服务发出的载荷一定是 A 的形状」
这件事**无法由 schema 保证**——只能靠 `event-topics.v2.json` 的 `producer` 字段靠 catalog
检查管住配置，管不住实例。

本轮的真实数字（脚本输出，未心算）：
`topics 55 / schema_ref 55 / data_schema_ref 43 / payloads 98 / envelope-only 12`、
`schema files 52`、`json(all) 124`、`example json 60`、差值 `124 − 52 = 72`。
`python tools/check_all.py` → `ok json 124 file(s)`、
`ok payloads 98 schema ref(s), 43 topic(s) with a payload schema`、
`59 example(s) valid, 0 failing, 1 without a schema`、`9 check(s) passed`。

### 10.1 本节自己又错了一次，而且错在「更正上一节」

上面那段收尾文字，初稿里原本还有一句「顺手更正上一节末尾那句推论」——说第 9 节那句
「每轮在两个数上各加 1，所以差值每轮也加 1」是错的，并给出「差值从 70 变成 72，即加了 2」。
这句话本身是错的，两条：

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 4 | 本节收尾段 | 「差值从 70 变成 72，即**加了 2**」 | 上一轮（HEAD `dae385e`）是 `122 / 51`，差 **71**；本轮 `124 / 52`，差 **72**。**从 71 变成 72，加了 1** | `json` 本轮确实加了 2（schema + 示例各 1），但我把「`json` 的增量」当成了「差值的增量」。差值 = `json − schema`，分子加 2、分母加 1，差值只加 1 |
| 5 | 同上 | 「第 9 节那句『差值每轮也加 1』是错的」 | 第 9 节没说错。凡是各轮都「+1 schema +1 示例」的轮次，差值就 +1；本轮恰好 +1 | 我没回读 HEAD 的两个数就先下了「上一节错了」的结论。**这比原错误更糟：它把一条正确的推论改成了错的** |

所以第 9 节的结论原样保留。顺带把那个真正稳定的关系写对：

```
json(all) − schema files = examples + 12
其中 12 = 4 个非 schema 的根级 JSON（event-topics / mcp-tools / redaction-policy / service-catalog）
        + 3 个 quality/ + 5 个 domain-v1/
本轮核对：124 − 52 = 72，60 + 12 = 72 ✓
```

这个恒等式能当校验和用，而「差值恒定」不能——因为 `examples` 在涨。上一节该说的话是这句，
我却在它的位置上写了一个更差的错误结论。

## 11. S14 这一轮：41 处行号引用指错了行

这一轮加 `asset.prop-revision-accepted.v2.schema.json` 与它的示例。schema 与示例都一次写成，
但**位置引用大量指错**——这是本轮真正的工作量。

### 11.1 行号引用：写出时凭记忆，回读时靠脚本

初稿写完后用 `tools/_tmp_lineref.py` 把 schema 里**每一条** `行 N` / `行 N–M` 都解到
`plans/S14-prop-assets.md` 的实际那一行，把该行内容打出来人工核对；`tools/_tmp_pincheck.py`
再用一份断言表复述一遍（每条断言 `(行号, 该行必须含的子串)`），两轮结论一致。
最终 `tools/_tmp_fixrefs.py` 落了 **44 条替换，其中 42 条真正改变了文本**，涵盖
**41 处行号重映射**（另两条一改键数、一条是文案）。

一处口径先说明：`行 N` 在本 schema 里出现 **233 次**，去重后是 **108 个不同行号**。
「修了 41 处」数的是**重映射的种类**，不是出现次数——同一个错行号复制到 7 个字段上，
只算一处错，但要改 7 次。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `prop_revision` 建表引用 | `§2.1 行 161–178` | `§2.1 行 160–178` | 起点少 1。我把「`CREATE TABLE` 下面那行」当成建表行，实际的 `CREATE TABLE` 在它上一行 |
| 2 | `:accept` 命令体 | `行 439` | `行 436` | 差 3。命令体是 `:accept`（435）之后的第三个代码围栏行，我从围栏内的第一条内容往回数，把中间的空行漏了 |
| 3 | 四处 `text_fingerprint = sha256(canonical_text)` | `行 183`（7 处） | `行 189` | 差 6。这条说明在 `prop` 建表语句之后的段落里，不在建表语句内。7 处全部同错，因为我是复制粘贴的同一句 |
| 4 | 告警规则 | `行 651` | `行 639` | 差 12。§7 有两段：metrics（630–635）和 alarms（639）。我把「metrics 之后第 16 行」当成了告警，实际告警开头就在 639 |
| 5 | `weapon` 反例（未决第 4 条） | `行 760` | `行 761` | 差 1。这条由 `_tmp_recheck.py` 的断言表抓出——我为了让 `prop_kind` 的 `enum` 有依据，去读 §11.1 的未决清单，把标题行当成了内容行 |

其余重映射（脚本原样，不逐条列）：`行 437→442`（接受事务范围）、`行 103→102`（§1.3 起）、
`行 211→212`（主键）、`行 163→162`（`prp_` 注释）、`行 180→183` 与 `行 180→187`（同一处
错号被两个字段引用，指向两个不同的正确行）、`行 454→455`、`行 287→289`、`行 535→534`、
`行 445→446`、`行 456→457`、`行 202→203`、`行 204→224`、`行 390→419`（差 29，最大的一处）、
`行 421→420`、`行 358→348`、`行 682→665`、`行 233→236`、`行 655→646`、`行 656→647`、
`行 220→222`、`行 5→8` 与 `行 11→10`（这两条指 S35，不是我原先以为的行）。

修法：`tools/_tmp_fixrefs.py`，每条替换都带 `assert s.count(old) == 1`，改完 `json.loads`
重新解析成功才落盘。这个脚本失败过三次：

- 一次 0 命中——我写的旧串和新串在空格上有差异，`assert` 直接拦下，没有静默跳过；
- 一次 3 命中——我把长串写在短串之后，短的先把长的切开了。改成**长串优先**；
- 一次是批量 `行 183` → `行 189` 的 heredoc 已经把其中三条先改了。删掉已应用的条目。

**这三条不是脚本的毛病，是「改引用」这件事本身要求替换必须唯一且可验证。** 没有
`assert count == 1`，第 2 种失败会静默改错两处。

### 11.2 两个我自己数错的数字

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 6 | §5.3 事件 `data` 的键数 | 「12 个 data 键」 | **11 个** | 我的计数循环把 `"data":{` 这一行本身也算成了一个键。方案 L554–566 的 `data` 内是 11 个键，`schema_version` 是本仓约定加的第十二个 `required` |
| 7 | `prop_revision` 的列数 | 「15 列」 | **17 列** | 我数的是「有注释的列」，漏了 `revision` 与 `quality_gate_ref` 两条没带行尾注释的。L195–211 逐行数才是 17 |

### 11.3 差值恒等式：我又把口径混了一次

中途核对 `contracts/**/*.json` 与 `contracts/**/*.schema.json` 时算出
`126 − 60 = 66`，然后拿它去比 `61 + 12 = 73`，差 7，一度以为有 7 个文件丢了。

错在**两个数用了不同的作用域**：`126` 是递归统计的，`60` 也是递归的，但恒等式
`examples + 12` 里的 `12` 是按**根级**非 schema JSON 数出来的（4 个根级 + 3 个
`quality/` + 5 个 `domain-v1/`）。同口径下应该拿根级 schema 数：

```
contracts/*.schema.json        = 53   （根级，非递归）
contracts/**/*.json            = 126  （递归）
contracts/examples/*.json      = 61
126 − 53 = 73,  61 + 12 = 73  ✓
```

递归 schema 是 60，多出的 7 个是 `quality/`(3) + `domain-v1/`(5) 里那些**不在** `12` 里的
文件——两边的 7 恰好相等，所以差值才是 7。这不是新错误，第 9 节已经记过一次同类；
区别是那次是「恒等式写错」，这次是「套用时忘了换回根级」。**恒等式能用，前提是两个数同口径。**

### 11.4 本轮真实数字（脚本输出）

```
topics total          = 55
with data_schema_ref  = 44
envelope-only         = 11
payload refs (schema_ref + data_schema_ref) = 99
contracts/**/*.json   = 126
contracts/*.schema.json = 53
contracts/examples/*.json = 61
payload schemas (title ends with 「的 data 载荷」) = 2, all require schema_version: True
```

`python tools/check_all.py`：

```
ok    json       126 file(s)
ok    payloads   99 schema ref(s), 44 topic(s) with a payload schema
ok    checkids   99 check_id(s), 3 known non-conforming
ok    links      1321 local link(s)
ok    catalog    48 service(s), 55 topic(s)
ok    families   14 family/families
ok    scaffold   120 generated file(s)
60 example(s) valid, 0 failing, 1 without a schema
ok    examples   contracts/examples/
ok    registry   no check-registry.v2.json yet; nothing to validate

9 check(s) passed
```

探针（`tools/_tmp_probe_prop.py`）：payload 正例 8/8、负例 18/18、`allOf` 4/4、envelope 4/4，
`ALL PROBES PASS`。

### 11.5 顺带修掉的一处：`contracts/README.md` 的三层图与检查摘要

README 里 `data_schema_ref` 的计数是 43，检查摘要是 `124 / 98 / 43 / 59`，三层图也写 43 / 12。
本轮全部按上表重数后改为 `44 / 11 / 126 / 99 / 60`。

散文里另有三处提到 envelope-only 的**历史**数量（行 797 的 17、行 861 与行 918 的 16）。
**这三处不是错的**——它们写的是「当时」。我没有把它们递减成 10/9，而是在原处注明
「今天剩 11 个」。递减会把一段带时间锚点的观察改成一句当下的错话。

---

## 12. S11 这一轮：又一次凭记忆写行号，94 处引用里 8 处指错

这是**连续第三轮**同一个毛病（S14 41 处、S13 若干、本轮 8 处）。根因不是不会数，是
**写的时候从脑子里取行号，没有回读**。本轮改成：先写脚本把每一条要引用的行 dump 出来，
再写正文；即便这样，仍有 8 处是在「写正文时顺手补的一句引用」上出的错——脚本只覆盖了
事先列出的那些。教训是脚本要在**正文写完后再跑一遍全量扫描**，而不是只验事先想起来的。

### 12.1 八个指错的行号

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `CRC-1` 的 `event_keys`、`CRC-1` 的 `fix_shape` | `§5.3 行 552–559` | **`行 553–559`** | 552 是 `"data":{` 这一行本身，不是键。7 个键在 553–559。**与第 11 节第 6 条是同一个错**（那次是 S14 的「12 个键」数成了 11），我记了更正表却没有把这条教训带过来 |
| 2 | `CRC-3` 的 `rights_event` | `§5.3 行 546–560 的事件块` | **`行 545–562`** | 546 是 `"specversion"` 那一行（信封开始的第二行），不是 `{`。事件块从 545 的 `{` 到 562 的 `}` |
| 3 | `x-needs-owner` 第 5 条、`CRC-5` 的 `consequence` | `rehash-revision` runbook `行 649` | **`行 648`** | 648 才是 `rehash-revision`；649 是 `drift-audit` |
| 4 | `canonical_revision` 的 `description`、`CRC-4` 的 `fix_shape` / 相关处 | 事务第 5 步 `行 437–441`（后又改成 `438–441`） | **`行 437–440`** | 437 是 `-- 5) set_canonical:` 注释行，438–440 是那三条 `UPDATE`/`INSERT`，441 是空行。**我第一次改成了 438–441，把空行算了进去，是错上加错** |
| 5 | `became_canonical` 的 `description`（`s17_reality`） | `§4.2 行 313–322` | **`行 313–321`** | 313 是 `### 4.2` 标题，314 空行，315 表头，316 分隔行，317–321 是五行依赖，322 空行 |
| 6 | `CRC-2` 的 `s17_reality` | `§4.2 ... 行 323` 引作「依赖读取」范围之内 | 同上，**313–321** | 323 是「S17 不缓存 S16 的事实」那句，**不是**依赖表的一部分；引它做别的断言可以，但不能算进依赖读取的行范围 |
| 7 | `CRC-6` 的 `s11_refs` | 「共 **3** 处 `S33/S34` 当质检」，并声称另外两处没问题 | **5 处命中、4 处误用** | `git grep -F "S33/S34"` 实测 5 行：150、205、373、750、753。我漏了 373（`→ S33/S34 QC（身份一致性、多角度一致性）`，判决书里最直白的一处），又把 750（`identity_features` 是「S33/S34/S17/S48 的共同输入」，另一种误用）判成了无关 |
| 8 | 同上，`why_it_matters` | 「S14 七处、S11 **三处**、S12 六处、S13 两处」 | **S14 七、S12 六、S11 五、S13 二** | 沿用第 7 条的数错。四份方案的数字都是脚本 `git grep -F` 打出来的，只有 S11 那个是我手写的 |

第 3 条和第 4 条都是「改一次、又错一次」：第 3 条我第一次把 648 写成了 649，回读时发现改对；
第 4 条我把范围从 437–441 改成 438–441（以为要跳过注释行），结果又把 441 这个空行包了进去。
**范围类引用要么首尾都落在真实内容行上，要么就别用范围。**

### 12.2 两个我自己数错了的数字

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 9 | `view_count` 的 `description` | `character` 的 **14 列**（行 98–109） | **12 列** | 行 98–109 是 12 行数据列，我把 `PRIMARY KEY`（110）与 `UNIQUE`（111）也算成了列。`character_revision` 的 17 列同样是脚本数的（136–152），那一个是对的 |
| 10 | `view_count` 的 `description`、README 同处 | `§2.3 行 168–196` | **`行 168–194`** | 195、196 已经是 `### 2.4` 的标题与空行。**与第 11 节第 7 条是同一类**：范围尾巴伸进了下一节 |

### 12.3 一个我该早点承认的口径错误

`CRC-2` 里为了证明「S17 不订阅任何事件」，我引了 S17 行 323「S17 不缓存 S16 的事实」。
这句话**成立且有用**，但它是 S17 对 S16 的陈述，不是对 S11 的。真正的证据是
`git grep -F` 的零命中（`asset.character.revision.accepted`、`became_canonical`、
`identity_feature_hash`、`character_revision` 在 S17 里全为 0）加上 §4.2 那张只读依赖表。
引用一句相邻但不同主语的句子，会让论证看起来比实际强——这正是本仓库反复记的那类问题
（「某某需要它」的句子没人回执）。已把主证据与旁证分开写。

### 12.4 本轮真实数字（脚本输出，不沿用第 11 节的中间结果）

```
topics total          = 55
with data_schema_ref  = 45
envelope-only         = 10
payload refs (schema_ref + data_schema_ref) = 100

contracts/**/*.json        = 128   （递归）
contracts/*.schema.json    =  54   （根级，非递归）
contracts/**/*.schema.json =  61   （递归）
contracts/examples/*.json  =  62

128 − 54 = 74,   62 + 12 = 74   ✓
```

12 个「既不是例子也不是根级 schema」的 JSON：`domain-v1/` 的 5 个 `.v1.schema.json`、
`event-topics.v2.json`、`mcp-tools.v2.design.json`、`redaction-policy.v2.json`、
`service-catalog.v2.json`、`quality/action-families.v2.json`、`quality/check-registry.v2.schema.json`、
`quality/qc-report.v2.schema.json`。

**第 11.3 节的恒等式这一轮换了数字，仍然成立**：上次是 `126 − 53 = 73 = 61 + 12`，加了 1 个
payload schema、1 个例子、1 个 topic 之后是 `128 − 54 = 74 = 62 + 12`。两边各加 1，
是因为新增的 schema 落在根级、新增的例子落在 `examples/`。**注意 `128` 与 `54` 必须一个递归
一个根级**——拿递归的 61 去减就会差 7，第 11.3 节已经踩过一次。

`python tools/check_all.py`：

```
ok    json       128 file(s)
ok    payloads   100 schema ref(s), 45 topic(s) with a payload schema
ok    checkids   99 check_id(s), 3 known non-conforming
ok    links      1321 local link(s)
ok    catalog    48 service(s), 55 topic(s)
ok    families   14 family/families
ok    scaffold   120 generated file(s)
61 example(s) valid, 0 failing, 1 without a schema
ok    examples   contracts/examples/
ok    registry   no check-registry.v2.json yet; nothing to validate

9 check(s) passed
```

探针（`tools/_tmp_probe11b.py`，78 条）：payload 负例 56/56、payload 正例 11/11、
envelope 负例 10/10、envelope 正例 1/1，**FAILS = 0**。

第一版探针报了 5 个 FAIL，全部是**我的期望值写错**，不是 schema 错：信封对
`source`/`type`/`traceparent` 只收 `{"type":"string","minLength":1}`，所以「把 `source`
改成 S14」「把 `type` 改成不存在的主题」「`traceparent` 写垃圾」三种都放行。读
`event-envelope.v2.schema.json` 确认后，把这三条从「应拒」改成「已知漏检」，并在 README
里与 S13 那条并列写明。**这是信封的已知限制，不是本 schema 的缺陷。**

### 12.5 一条本该在第一次就查的事实

`view_count` 我在 schema 里写成「全仓唯一出现」——这个结论是 `git grep -F view_count plans/`
打出来的，只命中 S11 自己的 558 行。但同一句话我第一次是用「只出现在 §5.3 的示例里」这种
描述写的，没有贴命令输出。本仓的规矩是「写进报告的每一个数都用脚本算一遍贴输出」，
对「某个词在整份方案里只出现一次」这种断言同样适用——它是个可证伪的事实断言，不是修辞。
**断言「唯一」「从不」「没有任何」时，必须先跑 grep 并留下输出。**

---

