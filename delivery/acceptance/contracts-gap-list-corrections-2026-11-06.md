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


---

## 13. S12 这一轮：`variant_axes` 自己就没发全，而点名读它的订阅方零命中

S12 是本轮最省事的一篇——`plans/S12-costume-assets.md` 708 行，§5.3 的事件示例把 8 个键
逐行列了出来，`costume_revision` 的 DDL 也把 16 列列齐了。所以「事件发了什么、没发什么」
可以纯脚本比对，不需要推。结果是：**键集可以对上，但方案自己给出的理由对不上。**

### 13.1 修正第 12 轮留下的一条数

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | §12.1 第 8 条（`CRC-6` 的 `why_it_matters`） | 「S14 七处、S12 **六处**、S11 五处、S13 两处」 | **S12 九（6 对 + 3 单）、S14 八（7 对 + 1 单）、S11 六（5 对 + 1 单）、S13 二（2 对，无单写），合计 25** | 那四个数里，S11 是上一轮刚把「成对 + 裸」双口径改正过的，另外三个都是从 `git grep -F "S33/S34"` 的命中数抄来的**成对口径**。本轮逐篇扫，S12 另有行 204 / 320 / 615 三处单写 `S33`，**S14 有行 571、S11 有行 537** 各一处单写——四篇里三篇被漏数。**同一个错在同一份报告里刚改完一次，紧接着又在其他三篇上犯了一遍** |

`CRC-6` 正文里的四个数我**没有改**（那是 S11 那份 schema 的 `x-conflicts`）；本节这一行是
本轮逐篇实测的口径，后续若统一，以本行为准。


### 13.2 本轮脚本输出（不沿用第 12.4 节的中间结果）

```
S12 S33/S34 相关行
  total lines mentioning S33 or S34 : 9
  literal "S33/S34"             : 6  [234, 422, 665, 681, 686, 697]
  bare S33 not in the pair       : 3  [204, 320, 615]

costume      112–126 : 12 data columns
costume_revision 148–164 : 16 data columns
event data keys 517–524  : 8
  ['costume_ref','character_ref','costume_kind','became_canonical',
   'canonical_revision','variant_axes','unverified_refs','rights_ref']

revision columns not carried by the event : 10
  ['payload','outfit','material_palette','accessories','variant_manifest',
   'source_kind','parent_revision','quality_gate_ref','accepted_at','accepted_by']
event-only keys (not a revision column)    : 6
  ['costume_ref','character_ref','costume_kind','became_canonical',
   'canonical_revision','unverified_refs']

axes declared in §1.3 (行 84–86) : 3  ['damage','wetness','makeup']
axes in event example (行 522)   : 2  ['damage','wetness']
```

全仓单次出现（`git grep -F`）：

```
identity_invariance_check  ->  1 处（plans/S12-costume-assets.md:411）
IDENTITY_CHANGED           ->  1 处（plans/S12-costume-assets.md:640）
asset.costume.revision.accepted -> 3 处，全部在 plans/S12-costume-assets.md（418 / 511 / 683）
unverified_refs 在 plans/S14-prop-assets.md -> 0
variant_axes   在 plans/S17-continuity.md   -> 0
costume        在 plans/S17-continuity.md   -> 0
costume        在 plans/S33-sound-effects.md / plans/S34-music.md -> 0 / 0
入库           在 plans/S35-technical-qc.md / plans/S36-semantic-qc.md -> 0 / 0
UNIQUE (…aggregate…) 形态的 outbox DDL 行（48 篇方案逐一扫）
  36 篇：UNIQUE (aggregate_id, aggregate_sequence|aggregate_seq, event_type)  无 tenant_id
   2 篇：UNIQUE (tenant_id, aggregate_id, aggregate_seq)                     S03:366 / S04:366
  10 篇：没有这张表的 DDL 行，无法判定（S07 S09 S16 S17 S21 S27 S41 S42 S43 S44）
inbox PRIMARY KEY (consumer, event_id)：35 篇，无一带 tenant_id（另 13 篇无此 DDL 行）
```

这一轮的计数与第 12.4 节的关系（同一个恒等式，两边各加 1）：

```
topics total          = 55          （不变）
with data_schema_ref  = 46          （45 + 1，本轮加了 costume）
envelope-only         =  9          （10 − 1）
payload refs          = 101         （100 + 1）

contracts/**/*.json        = 130   （128 + 2：1 schema + 1 example）
contracts/*.schema.json    =  55   （ 54 + 1，新 schema 落在根级）
contracts/**/*.schema.json =  62   （ 61 + 1）
contracts/examples/*.json  =  63   （ 62 + 1）

130 − 55 = 75,   63 + 12 = 75   ✓
```

`links` 仍是 1321——新增的 README 小节与本节都没引入新的相对链接。

### 13.3 本轮改了哪些文件

| 文件 | 动作 |
|---|---|
| `contracts/asset.costume-revision-accepted.v2.schema.json` | 新增。8 个载荷键，`x-needs-owner` 6 条、`x-conflicts` 7 条（CCA-1..CCA-7） |
| `contracts/examples/asset-costume-revision-accepted.json` | 新增。按 §5.3 行 507–528 的示例补上 `data.schema_version`（全仓统一约定），两个 hash 用 `hashlib` 生成（`assert len==64`） |
| `contracts/event-topics.v2.json` | `asset.costume.revision.accepted` 加 `data_schema_ref`，位置紧随 `schema_ref`（与相邻四项同形） |
| `tools/validate/validate_examples.py` | `SCHEMA_OF` 加 `asset-costume-revision-accepted` |
| `contracts/README.md` | 线表加一行；第 61 行 45/10 → 46/9；三条关系图与入口说明里的 45→46、10→9、128→130、100→101、61→62；`asset.character` 小节之后新增 S12 小节 |
| `delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md` | 本节 |

### 13.4 本轮发现的问题（都写进了 schema 的 `x-conflicts`，不在这里选边）

七条里有三条是资产族共有的，不是 S12 独有：

- **`S33/S34` 当质检（CCA-1）**——资产族四篇都错，合计 25 行（S12 九、S14 八、S11 六、S13 二）。**改编号修不好**：S35/S36 的
  `QCReport v2` 要求顶层 `shot_ref`，资产图没有 `shot_id`/`node_epoch`，过不了 schema。所以
  `quality_gate_ref`（`NOT NULL`）在今天没有可执行的产出方。
- **canonical 在零事件下移动（CCA-4）**——`:set-canonical` 不产生 revision 也不产生事件，
  而 `decision_kind` / `prior_canonical` 从不进事件。S11 / S13 / S14 / S12 四个服务同一形状。
- **outbox 唯一键不含 `tenant_id`（CCA-6）**——§2 开头那句「所有表的主键/唯一约束含
  `tenant_id`（E28）」在 36 篇方案里对 outbox 不成立、在 S03 / S04 两篇成立。规则句与模板从源头
  就不一致，而且今天已经有两种落地。

S12 独有的四条：`variant_axes` 少一轴（CCA-2）、`unverified_refs` 无形状且消解方零命中
（CCA-3）、两个判据全仓各只出现一次（CCA-5）、`canonical_revision=null` 的规则只在散文里
（CCA-7）。

### 13.5 探针

`_tmp_probe_s12.py`（已删，输出记在此处）：89 条，payload 负例 68、payload 正例 15、
envelope 负例 5、envelope 正例 1，**failures = 0**。正例里 4 条是刻意的「不选边」放行：
`character_ref=null`、`variant_axes` 带未声明的第三轴或未列名的轴名/取值、
`unverified_refs` 任一开对象、`rights_ref` 任意两键。这四条都不是疏漏——它们正是
schema 拒绝替 owner 决定的那几处。

与 S11 / S13 / S14 同源的一条已知漏检仍未消除：信封对 `source`/`type`/`traceparent` 只收
非空字符串，两层校验都放行生产者与主题不匹配的实例。

`python tools/check_all.py` 九项全过：`json 130`、`payloads 101 / 46`、`checkids 99`、
`links 1321`、`catalog 48 / 55`、`families 14`、`scaffold 120`、`examples 62`、`registry` 跳过。

---

## 14. S02 这一轮：事件的全部规格只有一行，而那一行里的名字没有定义

S12 那一轮最难的是把散文里的键拼出来。S02 相反：**方案里没有任何事件 JSON**。
`plans/S02-governance-policy.md` 824 行，`specversion` 0 次、`"data"` 0 次、`data_ref` 0 次。
事件的全部规格是 §10.2 行 718 的一行发布说明，加上行 713 的两条 aggregate 赋值：

> `- 发布：`policy.revision.published`（topic `drama.governance-policy.v2`，partition key `tenant_id:aggregate_id`），与 DB 提交同事务写 Outbox。`
>
> `` `aggregate_id = policy_revision_id`，`aggregate_sequence = revision`。 ``

所以这一轮和前面几轮的性质不同：前面是「方案给了形状、契约层没有承载」，这一轮是
**方案给了主题名和分区键，其余全是判断**。schema 里四个键，每一条都在 description 里写明
「挂在哪一句上」还是「按全仓约定补的」——区分这两者，比键集本身重要。

结果是：**能纯脚本比对的只有 aggregate 那两条赋值，而它们恰好是全篇最站不住的地方。**

### 14.1 修正前几轮留下的六条数

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | 本节 schema 的 `schema_version` description、`POL-10` 三处 | 「46 份 topic 载荷」「46 份 schema」「160 处」 | **47 份 / 167 处** | 46 与 160 是**本轮写 schema 之前**的口径。本轮新增的那份 schema 自身含 7 处 `^[0-9a-f]{64}$`（1 处真 `pattern` + 6 处 description 里引用的字面量），160 + 7 = 167；文件数 46 + 1 = 47。**这与第 13 轮同一类错**：在一个会随本轮改动的分母上引用了本轮之前的值 |
| 2 | `POL-10` 的 `repo_wide_form` | 「含 `sha256:` 的有 12 份」 | **13 份** | 同上——本轮那份 schema 的 description 里写了 `sha256:`，自己进了这个集合。12 → 13 是我改过的、不是源的 |
| 3 | `policy_ref` description、`POL-2` 的 `but_the_event_hides_it` | 「`policy_id` 在 §10.1 里出现 4 次」「全文出现 4 次」 | **全篇 9 次；§10.1 里 6 次** | 4 是只数了 §10.1 的 DDL **行**（而非出现次数），且漏了行 650 / 655 两处 `PRIMARY KEY (tenant_id, policy_id)` 与行 208 的 `load_revision(layer.policy_id, …)`。实测 9 次 = 6（DDL）+ 3（散文/示例） |
| 4 | `POL-1` 的 `only_revision_shaped_aggregate` | 「覆盖 34 个方案」 | **28 篇方案** | 34 是服务数量（S01–S48 里绝大多数），不是**含 `aggregate_id = X` 语句的方案篇数**。35 处语句分布在 28 篇里——S06 一篇有两处（`storyboard_id` / `shot_id`），另有 20 篇根本没有这句话 |
| 5 | `POL-8` 的 `summary`、`the_block` | 「8 处 `NOT NULL`」 | **8 行、共 16 个列** | `NOT NULL` 是**列级**约束，而 §10.1 的 3 张真 DDL 把多列挤在一行（行 700 一行就有 3 个）。按行数是 8，按受约束的列是 16。两者都对，但只能说一个——本轮统一写「8 行共 16 个列」 |
| 6 | 本节 §6 探针的 `bare-column tables` | 第一版输出 6 张，漏 `safety_tightening` | **7 张** | 探针正则写成 `^(\w+)\($`（要求行尾就是左括号），而 `safety_tightening(` 后面跟了行尾注释 `-- 紧急收紧，绕过常规 checkpoint`。**这是探针的错，不是方案的错**：7 这个数在 schema 正文里从一开始就是对的，错的是我拿一个不完整的探针去核它 |

第 6 条值得单独一句：**探针给出的数比正文小时，先怀疑探针。** 这一条的探针输出当时
少了 1，而正文的 7 是对的——如果按「以脚本为准」照改正文，就会把一个正确的数改错。
规矩是「每个数都用脚本算」，不是「脚本无论算什么都对」。

### 14.2 本轮脚本输出（不沿用第 13.2 节的中间结果）

`_tmp_probe_s02.py`（已删）：

```
plans/S02-governance-policy.md : 824 lines

--- 1. plan-side occurrence census (whole plan) ---
  specversion                0  []
  "data"                     0  []
  data_ref                   0  []
  policy_revision_id         2  [699, 713]
  sha256:                    4  [338, 480, 484, 520]
  scope_binding              3  [147, 384, 658]
  effective_window           4  [235, 351, 376, 566]
  effective_at               1  [649]
  policy_id                  9  [139, 208, 355, 382, 648, 650, 654, 655, 659]
  audit                      0  []
  审计                         0  []
  E31                        0  []
  published_by               0  []
  policy_switch_request      1  [608]
  current_revision           1  [654]
  INSERT                     1  [651]
  UPDATE                     2  [651, 714]

--- 2. title convention across contracts/*.schema.json ---
  payload v2       43
  <name> v2         9
  的 data 载荷         4
        asset.character-revision-accepted.v2.schema.json
        asset.costume-revision-accepted.v2.schema.json
        asset.prop-revision-accepted.v2.schema.json
        asset.scene-revision-accepted.v2.schema.json

--- 3. aggregate_id = X  sentences across all plans ---
  distinct targets : 26
  total sentences  : 35
  targets containing 'revision' : 1  ['policy_revision_id']
     analysis_id              S09-lineage-impact.md:754
     breakdown_id             S05-breakdown.md:475
     budget_id                S23-budget-ledger.md:483
     episode_id               S03-project-catalog.md:664
     export_id                S45-audit.md:443
     grant_id                 S01-tenant-access.md:520
     job_id                   S48-adaptation-training.md:421
     manifest_id              S08-revision-snapshot.md:643
     operation_id             S46-developer-integration.md:369
     permit_id                S22-scheduler-quota.md:235
     plan_id                  S07-change-management.md:858
     policy_revision_id       S02-governance-policy.md:713
     provider_job_id          S26-generation-jobs.md:1014
     repair_plan_id           S39-repair-planning.md:744
     report_id                S36-semantic-qc.md:390
     review_task_id           S40-human-review.md:634
     route_id                 S25-model-routing.md:504
     run_id                   S21-orchestration.md:841
     script_id                S04-narrative.md:711
     shot_id                  S06-storyboard.md:862
     storyboard_id            S06-storyboard.md:545
     subscription_id          S47-realtime-projection.md:431
     task_id                  S19-agent-runtime.md:524
     timeline_id              S41-editorial.md:612
     track_id                 S38-subtitles.md:162
     voice_id                 S15-voice-assets.md:441

--- 4. depends_on containing S02 (service-catalog) ---
  declare S02 : 7
     S07  ['S08', 'S09', 'S18', 'S02']
     S19  ['S02', 'S26']
     S21  ['S08', 'S02', 'S22', 'S23']
     S25  ['S24', 'S20', 'S02']
     S39  ['S35', 'S36', 'S37', 'S09', 'S02']
     S40  ['S01', 'S02', 'S08']
     S46  ['S01', 'S02', 'S07', 'S21']

--- 5. which plans mention the topic, vs depends_on ---
  plans mentioning topic : 7  {'S02': 5, 'S07': 1, 'S08': 1, 'S29': 1, 'S33': 2, 'S34': 1, 'S40': 1}
  depends_on S02 but never mentions topic : ['S19', 'S21', 'S25', 'S39', 'S46']
  mentions topic but no depends_on S02   : ['S02', 'S08', 'S29', 'S33', 'S34']

--- 6. §10.1 DDL block census (lines 646-711) ---
  block lines : 66
  CREATE TABLE : 3  -> ['governance_policy.idempotency_record', 'governance_policy.outbox', 'governance_policy.inbox']
  NOT NULL     : 8  lines [689, 690, 692, 699, 700, 701, 707, 708]
  CHECK        : 1  lines [690]
  bare-column tables : 7  ['automation_policy_revision', 'policy_publish_pointer', 'policy_scope_binding', 'gate_instance', 'policy_decision', 'policy_exception', 'safety_tightening']

--- 7. §10.1 table line numbers ---
     L647  automation_policy_revision(
     L653  policy_publish_pointer(
     L658  policy_scope_binding(
     L663  gate_instance(
     L670  policy_decision(
     L678  policy_exception(
     L683  safety_tightening(                        -- 紧急收紧，绕过常规 checkpoint

--- 8. contract inventory (this round) ---
  topics                     55
  with data_schema_ref       47
  envelope-only              8
  rows declaring consumers   0
  contracts/**/*.json        132
  contracts/*.schema.json    56
  contracts/**/*.schema.json 63
  contracts/examples/*.json  64

--- 9. openapi paths and S02's ---
  total paths : 21
  S02 paths   : ['/projects/{project_id}/effective-policy', '/policies/{policy_id}/revisions']
  has /policy-decisions:evaluate          : False
  has /policy-decisions:resolve-effective : False
  effective-policy 200 schema_ref present : True

--- 10. hex pattern census ---
  '^[0-9a-f]{64}$' : 167 occurrences across 47 files
  schema files mentioning sha256: : 13
  sha256: inside any "pattern" value : 0
```

NOT NULL 的列数另算（`_tmp_nums2.py`，已删）：

```
--- NOT NULL: lines vs columns in the §10.1 block ---
  lines with NOT NULL : 8  -> [689, 690, 692, 699, 700, 701, 707, 708]
  columns bearing NOT NULL : 16
```

第 5 段那个 `{'S02': 5, ...}` 里 S02 自己算了进去，所以「7 篇提到」= S02 + 六篇消费方；
`mentions topic but no depends_on S02` 那一行的 5 个里有 S02 自己是生产者，不是消费方没声明依赖——
真正的「消费方没声明 S02 依赖」是 **S08 / S29 / S33 / S34 四个**。

这一轮的计数与第 13.2 节的关系（同一个恒等式，两边各加 1）：

```
topics total          = 55          （不变：payload schema 不新增 topic）
with data_schema_ref  = 47          （46 + 1）
envelope-only         =  8          （ 9 − 1）
payload refs          = 102         （101 + 1）

contracts/**/*.json        = 132   （130 + 2：1 schema + 1 example）
contracts/*.schema.json    =  56   （ 55 + 1，新 schema 落在根级）
contracts/**/*.schema.json =  63   （ 62 + 1）
contracts/examples/*.json  =  64   （ 63 + 1）

132 − 56 = 76,   64 + 12 = 76   ✓
```

`links` 仍是 1321——本节没引入新的相对链接（schema 与 README 里的都是行号引用，不是链接）。
`catalog` 仍是 48 / 55——payload schema 不改变服务元数据。

### 14.3 本轮改了哪些文件

| 文件 | 动作 |
|---|---|
| `contracts/policy.revision-published.v2.schema.json` | 新增。4 个载荷键（`schema_version` / `policy_ref` / `effective_at` / `created_by`），`x-needs-owner` 10 条、`x-conflicts` 10 条（POL-1..POL-10） |
| `contracts/examples/policy-revision-published.json` | 新增。17 个 envelope 键 + `data` + `data_ref: null`；`content_hash` 用 `hashlib` 生成（`assert len==64`），其余取自 `contracts/examples/automation-policy.json` 的同一份策略 |
| `contracts/event-topics.v2.json` | `policy.revision.published` 加 `data_schema_ref`，位置紧随 `schema_ref` | 
| `tools/validate/validate_examples.py` | `SCHEMA_OF` 加 `policy-revision-published`（放在 `breakdown-revision-created` 之后、`capacity-plan` 之前） |
| `contracts/README.md` | 线表加一行；L61 46/9 → 47/8；L801、L864、L922 三处「今天剩 N 个」10 → 8；L1429 那条**本来就错的 45 → 47**、9 → 8；L1443–1447 的入口数字（130→132、101→102、46→47、62→63） |
| `delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md` | 本节 |

`contracts/README.md` L1429 是这一轮的额外收获：它写着 `data_schema_ref ──▶ 45 个 payload schema`，
与同一份文件 L61 的「46 个 topic 带」**在本轮之前就已经差 1**（L1430 还写着「其余 9 个」，
而 46 + 9 = 55 才是对的，45 + 9 = 54）。**同一份 README 里两个数互相矛盾，而九个检查没有一项核对它们**——
`check_all.py` 的 `payloads` 从 `event-topics.v2.json` 算，README 里的散文没有人管。

### 14.4 本轮发现的问题（都写进 schema 的 `x-conflicts`，不在这里选边）

十条里，**六条是 S02 这一篇独有的**，四条是跨服务的。

S02 独有的六条：

- **`policy_revision_id` 没有定义（POL-1）**——全仓 2 次命中，一次是 outbox 的列注释、一次是赋值句。
  `automation_policy_revision` 的 8 个列里没有它。它是全仓 **26 个 `aggregate_id` 目标里唯一带
  `revision` 字样的那个**，其余 25 个都指向稳定聚合身份（`manifest_id` / `run_id` / `shot_id` /
  `budget_id` …）；资产族即便事件以 revision 为单位，`aggregate_id` 也是 `costume_id` 这类稳定值。
- **`aggregate_sequence = revision` 的分母在事件里（POL-2）**——`revision` 是 per-`policy_id`
  的（`policy_publish_pointer` 主键 `(tenant_id, policy_id)`），而事件的原始规格一个 `policy_id`
  都不发。于是 E12-06（行 812）「指针单调（不回退）」在订阅方没有可比的聚合。本 schema 用
  `policy_ref.id` 把它补上，但因果是我加的。
- **`scope_binding` 无定义、无写路径、而指针 TTL 只有 5 秒（POL-3）**——它是发布指针的缓存键
  （行 384）、`LayersFor` 的第一步（行 147）、一张表的主键段（行 658），三处全部在 S02，键集
  从未定义；`policy_scope_binding` 没有任何写入描述，`policy_publish_pointer.current_revision`
  也从不被推进。**用一个 at-least-once、保留 14 天的事件去失效一个 5 秒就自己过期的缓存**，
  而事件本身说不出哪些 scope 受影响。
- **`effective_at` 可未来时而不 gate 任何东西（POL-4）**——真正被判的 `effective_window.until`
  在 `automation-policy.v2.schema.json` 里**没有来源字段**（§11 G2 行 739 自己记了这一半），
  另一半没人记：从未说过「未到 `effective_at` 时旧 revision 仍有效」。
- **`policy_switch_request` 只活在一条箭头里（POL-9）**——行 608 写「S02 写
  `policy_switch_request(run_id, new_revision, requested_checkpoint)`」，全篇 1 次；§10.1 的 10 张表
  里没有它，也没有任何列叫 `run_id`；§11 的 G1–G7 也没记这条。
- **`POST /policy-decisions:evaluate` 没有路径（POL-7）**——catalog 把它写成 `query_api`，
  `openapi.v2.yaml` 的 21 个路径里没有；已有的 `/projects/{project_id}/effective-policy`
  200 响应 `$ref` 的是**存储文档** `automation-policy.v2.schema.json`，而 §5 描述的
  `EffectivePolicy` 有 `provenance` / `explain` / `gate_plan` / `computed_at` 等键，
  其中 `reviewer_roles` / `allowed_actions` / `locked` / `effective_window` 四项在存储文档根级
  根本不存在。

跨服务的四条：

- **`consumers` 键 55 个 topic 一个都没有（POL-5）**——`check_all.py` 的 `catalog` 有校验它的分支
  （L134–136），因键恒缺而从未执行。本 topic 的消费方名单只活在六篇散文里，且两边互不覆盖：
  `depends_on` 含 S02 的 7 个里只有 S07 / S40 提到它，S19 / S21 / S25 / S39 / S46 零命中；
  而 **S08 / S29 / S33 / S34 四个消费方没声明 S02 依赖**。
- **S33 / S34 与 S02 的生效范围表冲突（POL-6）**——两篇消费行都写这个事件「可能导致 take 变
  void」，而 S02 §9.2 行 627 明写「已产出的 artifact 与已接受的指针**不追溯重判**」、行 630
  「历史产物是否重做：默认不重做」。**S33 自己 L507 也说过这个事件「说的是策略，不是『这个素材
  已经不可用』」**——同一篇里两处结论方向相反。
- **§10.1 的 7 张表没有类型与约束（POL-8）**——10 张表里只有 3 张真 DDL，8 行 16 列的 `NOT NULL`
  与唯一 1 处 `CHECK` 全在那 3 张里；本事件的主表 `automation_policy_revision` 是裸列清单之一，
  「只 INSERT 永不 UPDATE」（行 651）没有任何机制承载。`content_hash` 也没有 `char(64)`——
  所以「64 位十六进制」这条约束在 S02 自己的 DDL 里不存在，只借自 `policy_ref` 的形状。
- **`sha256:` 在 4 个示例里与全仓的裸 hex 冲突（POL-10）**——行 338 / 480 / 484 / 520；
  行 326 与行 338 是同一对象、相隔 12 行、两种写法。全仓 `^[0-9a-f]{64}$` 167 处，`sha256:`
  在**任何 `pattern` 的值里命中 0**（13 份提到它的 schema，逐份查了 `pattern`）。本 schema 按裸
  hex 收，让错误在生产者侧暴露，而不是在跨服务比较里静默失败。

另有一条没有升成 conflict、但值得记下：**S02 全篇不提审计**——`审计` 0 次、`E31` 0 次、
`published_by` 0 次。而它是策略权威，S45 要能追溯「这条收紧是谁下的、依据什么」。
本 schema 用 `created_by` 补了这一半（DDL 行 649 的列名），另一半靠 `x-needs-owner`。

### 14.5 探针

`_tmp_probe_s02.py` 的正例/负例对 schema 本体跑过一轮，用 `Draft202012Validator` 直接验：

```
baseline ok: True
REJECTED sha256: prefix
REJECTED revision 0
REJECTED extra key document
REJECTED bad service_id
REJECTED missing created_by
REJECTED extra in policy_ref
```

六条负例全部按预期拒绝，基线通过。第二条（`sha256:` 前缀）是这一轮最要紧的一条：
它证明 `POL-10` 不是纸面观察，而是 schema 真的会拦住。

`python tools/check_all.py` 九项全过，`python tools/validate/validate_examples.py` 报
`63 example(s) valid, 0 failing, 1 without a schema`。

与 S11 / S13 / S14 / S12 同源的一条已知漏检仍在：信封对 `source` / `type` / `traceparent`
只收非空字符串，所以本 schema 的两层校验都放行「`type` 写成别的主题、`source` 写成别的服务」
的实例。本事件多一层具体的暴露：**envelope 的 `aggregate_id` 与 `data.policy_ref.id`
本 schema 要求是同一个 `policy_id`，但没有任何工具能交叉校验同一份文档里的这两个位置**——
`additionalProperties: false` 只管键，不管两处值的关系。这属于 schema 层表达不了的东西
（要 `$data` 或 codegen 校验器），记在这里。

---

## 15. S42 这一轮：两条只有聚合键的事件，以及它们暴露的三处跨服务空洞

S42 方案 §6.3 行 701 发布三条事件，但行 709–711 的聚合键表只给 `mix_job_id` / `render_job_id`
两个键名，**没有字段清单、没有示例、没有字段表**。前面十四轮的 S42 部分只做了
`post.master.rendered`（它有 §6.3 行 713–730 的完整示例）。本轮把另两条补完，并顺手把上一份
schema 里「另两条 payload 仍缺」这句已经过时的话改掉。

### 15.1 修正本轮自己写错的六条数

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `post.mix-completed.v2` 的 `description`、`x-needs-owner[0]` | 「DDL 有 16 列，本 schema 选了 **12 个字段 + `schema_version`**」「本 schema 的 **13 个键**」 | **14 个属性 = `schema_version` + 从 16 列里挑的 13 列** | 我把「去掉 3 列」（`tenant_id` / `created_at` / `finished_at`）写成了「留下 12 列」，又把 13 个 DDL 列 + `schema_version` 写成了 13 个键。**两处都是我自己减错**：16 − 3 = 13，13 + 1 = 14 |
| 2 | `post.render-failed.v2` 的 `description`、`x-needs-owner[0]` | 「本 schema 的 **15 个键**全部从 … 26 列里挑出」「保留其余 **12 列** + `schema_version`」 | **17 个属性 = `schema_version` + 从 26 列里挑的 16 列** | 同一类错，而且这一份更离谱：`required` 就有 15 项，加上非必填的 2 项正好 17。「26 列去掉 10 列 = 16」与「16 + 1 = 17」两次都对不上我写的 15 / 12 |
| 3 | `MIX-2` 的 `why_it_matters` | 「仓内 **52 处**跨服务引用」「与仓内其余 **51 处**不同形」 | **55 处**（本轮之前 52 处，本文件贡献 3 处），其余 **54 处** | 52 是**本轮写 schema 之前**的口径，而本 schema 自己就带 3 处该形状的四键引用。**与第 13、14 轮同一类错**：在一个会随本轮改动的分母上引用了本轮之前的值 |
| 4 | `RJF-1` 的 `repo_reality` | 「**4 篇文档**提到 topic 的其余 48 篇均为 0」 | **全仓 `.md` 只有 2 份提到 `drama.composition.v2`**：`plans/S42-composition.md` 与 `delivery/acceptance/plans-consistency-2026-10-30.md` 行 159 | 「4 篇」是我把另一个 topic 的数抄过来的；而且 48 篇服务方案里提到它的只有 S42 一篇，那另外「3 篇」根本不存在。**「N 篇文档提到 X」这种句子必须整仓扫一遍再写** |
| 5 | `RJF-1` 的 `s42_own_silence` | 「§8 的指标表（行 792 附近）有 `composition_remix_ratio`，**但没有失败率指标**」 | **行 789 就是 `composition_render_failure_total`**（counter，按 `failure_code`），行 790 还有 `composition_orphaned_job_total` | 我只扫了行 792 那一格就下了否定结论。**「没有 X」的断言必须扫完整张表**——这一条如果留着，会让一个真实存在的指标被当成缺口去补 |
| 6 | `RJF-2` 的 `why_it_matters` 与 `s42_prose` | 「行 282 的顺序是「先申请许可再落 job」，所以被拒的作业**不落库**」；行 280 引文里反引号错位；依据写作「行 279–282 第 6/7 步」 | 行 280 原文是「拒绝 → REJECTED，不落 RUNNING」；**行 282 只写「7. 事务写 render_job(QUEUED)…」，顺序的依据是行 290 那句「第 6 步的顺序很关键：先申请许可再落 job」** | 我把行 290 的结论归给了行 282，且行号区间写成「279–282」而实际是「279–281 第 6 步 / 282 第 7 步」。**结论对、出处错**是最难被察觉的一类——引用看起来有行号，读起来也通顺，但回读原文就对不上 |

第 1、2 条值得单独一句：这两份 schema 是本轮刚写的，`properties` 的个数我自己数过、`required`
也逐项对过，却仍然在 prose 里写出了不同的数。**「properties 的长度」与「prose 里说的键数」
是两个数，写完必须用 `len(s['properties'])` 回读。**

### 15.2 本轮脚本输出（不沿用第 14.2 节的中间结果）

`_tmp_probe_s42.py`：

```
=== 1. contract census (after this round) ===
contracts/*.schema.json       = 58
contracts/**/*.schema.json    = 65
contracts/**/*.json           = 136
contracts/examples/*.json     = 66
topics total                  = 55
  with data_schema_ref        = 49
  envelope-only               = 6
  schema_ref + data_schema_ref= 104
  topics having "consumers"   = 0

=== 2. the 6 remaining envelope-only topics ===
   S43  delivery.package.created  (topic drama.delivery.v2)
   S43  delivery.package.sealed  (topic drama.delivery.v2)
   S43  delivery.publish.failed  (topic drama.delivery.v2)
   S44  analytics.metric.snapshot.created  (topic drama.evaluation-analytics.v2)
   S44  analytics.evaluation.completed  (topic drama.evaluation-analytics.v2)
   S44  analytics.cost.projection.updated  (topic drama.evaluation-analytics.v2)

=== 3. event types ending in .failed ===
   n = 2 ['post.render.failed', 'delivery.publish.failed']

=== 4. canonical closed 4-key ref (service_id/id/revision/content_hash) ===
   total                         = 55 in 31 files
   post.mix-completed (this round)= 3
   post.render-failed (this round)= 0
   before this round             = 52

=== 5. hash pattern forms across contracts/**/*.schema.json ===
   "pattern" entries                = 263
   of which == ^[0-9a-f]{64}$       = 138
   "pattern" containing "sha256:"   = 0

=== 6. plans census ===
   plans/*.md                        = 49
   plans/Snn-*.md                    = 48
   drama.composition.v2     repo-wide .md = 2  ['plans-consistency-2026-10-30.md', 'S42-composition.md']
   post.mix.completed       repo-wide .md = 3  ['README.md', 'plans-consistency-2026-10-30.md', 'S42-composition.md']
   post.render.failed       repo-wide .md = 3  ['README.md', 'plans-consistency-2026-10-30.md', 'S42-composition.md']

=== 7. S35 / S39 mention the three S42 events ===
   S35-technical-qc.md {'post.mix.completed': 0, 'post.render.failed': 0, 'post.master.rendered': 0}
   S39-repair-planning.md {'post.mix.completed': 0, 'post.render.failed': 0, 'post.master.rendered': 0}

=== 8. S39 repair_plan columns (L642-663) ===
   columns = 17
   ['plan_id', 'tenant_id', 'series_id', 'branch_id', 'episode_id', 'run_id',
    'manifest_id', 'manifest_revision', 'execution_fence', 'state',
    'aggregation_deadline', 'policy_ref', 'policy_partial', 'snapshot',
    'escalation', 'created_at', 'updated_at']
   has revision           = False
   has content_hash       = False
   has manifest_revision  = True
   has plan_id            = True

=== 9. S42 anchors ===
   file length = 945 lines
   L182  │ QUEUED │──────────────────────────► REJECTED（终态）
   L203  │ ORPHANED │── 对账：查 S28 有无产物
   L282  7. 事务写 render_job(QUEUED) + segments + outbox + command_receipt
   L290  第 6 步的顺序很关键：**先申请许可再落 job**。反过来会造出一堆"排队但永远拿不到许可"的僵尸任务。
   L689  | GET | `/render-jobs/{id}` | 状态、段进度、`stale_inputs`、用量 |
   L701  | 发布 | `post.master.rendered`（topic `drama.composition.v2`）、`post.mix.completed`、`post.render.failed` |
   L710  | `post.mix.completed` | `mix_job_id` | 该混音作业的事件序号 |
   L711  | `post.render.failed` | `render_job_id` | 该渲染作业的事件序号 |
   GET mix-jobs in §6.2? none
   MixStem = 1 | mix_stem = 0 | STEMS_UNAVAILABLE = 2

=== 10. S42 mix_job / render_job column counts ===
   mix_job columns    = 16
   render_job columns = 26

=== 11. S43 gates + package_type CHECK ===
   gate rows = 10 ['G1 接受门', 'G2 素材门', 'G3 完整性门', 'G4 QC 门', 'G5 响度/规格门',
                   'G6 字幕门', 'G7 权利门', 'G8 策略门', 'G9 签名门', 'G10 清单门']
   L384: package_type         text NOT NULL CHECK (package_type IN
   L385: ('missing_material','preview','formal')),

=== 12. examples / SCHEMA_OF ===
   example files        = 66
   SCHEMA_OF entries    = 59
   mapped (not None)    = 58
   deliberately None    = 1
```

第 4 项的计数口径要写明，否则下一个人会数出别的数：只数**同时满足三个条件**的对象——
`properties` 的键集恰好是 `{content_hash, id, revision, service_id}`、`required` 与之一致、
`additionalProperties: false`。只按 `service_id` + `content_hash` 两个键去扫会得到 0，因为
这四键在 schema 里写作 `properties` 的子键，不是对象自身的键（我第一版探针就是这么写错的）。

第 6 项有一个陷阱也要记下：`glob('**/*.md')` 会同时命中仓库根目录的 `README.md` 与
`docs/SERVICE-SPECIFICATIONS.md`，所以 `post.mix.completed` 的「全仓 3 份」里有一份是
`contracts/README.md` 本轮新加的那张表——**它是本轮的产出，不是文档给的事实证据**。

### 15.3 本轮改了哪些文件

| 文件 | 动作 |
|---|---|
| `contracts/post.mix-completed.v2.schema.json` | 新增。14 个属性（13 必填，`repair_plan_ref` 条件必填），`x-needs-owner` 4 条、`x-conflicts` 3 条（MIX-1..MIX-3）；`allOf` 镜像 DDL 的 `mix_remix_needs_plan` |
| `contracts/post.render-failed.v2.schema.json` | 新增。17 个属性（15 必填；`failure_code` / `failure_detail` 可选），`x-needs-owner` 4 条、`x-conflicts` 3 条（RJF-1..RJF-3）；`state` 收窄到 4 个终态值 |
| `contracts/examples/post-mix-completed.json` | 新增。16 个 envelope 键 + 14 个 `data` 键，`data_ref: null` |
| `contracts/examples/post-render-failed.json` | 新增。16 个 envelope 键 + 17 个 `data` 键，`data_ref: null` |
| `contracts/event-topics.v2.json` | `post.mix.completed` / `post.render.failed` 各加 `data_schema_ref`，位置紧随 `schema_ref` |
| `tools/validate/validate_examples.py` | `SCHEMA_OF` 加 `post-mix-completed` / `post-render-failed` 两行 |
| `contracts/README.md` | 线表加两行；L61 47/8 → 49/6；L803、L867、L924 三处「今天剩 N 个」8 → 6；L132–136 那段「S42 三条里只有 `post.master.rendered` 有形状」改写；L1432 图里 47/8 → 49/6；L1446–1450 入口数字（132→136、102→104、47→49、63→66）；L1502「49 个示例」→ 65 |
| `contracts/post.master-rendered.v2.schema.json` | 两处过时话改成「另两条已各有 schema」；`x-needs-owner[0]` 从「另两条 payload 缺」改成「本条的键集本身」 |
| `delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md` | 本节 |

`contracts/README.md` L132–136 那一处值得单看：它原来写「S42 三条里只有 `post.master.rendered`
有字段清单，所以这三份 schema 都只覆盖『有形状的那一条』，其余的在 `x-needs-owner` 里点名」——
本轮之后这句话对 S42 不再成立，对 S43 / S44 仍然成立。改写时保留了后半段的规则（不猜形状），
只把 S42 从「点名」改成「已补，且不是从 master 事件裁出来的」。

### 15.4 本轮的三处跨服务空洞

这三条都不是「写法不统一」，是三个不同的东西缺了来源。

**（1）`repair_plan_ref` 的四键，S39 产不出来（MIX-2）。** 示例（§6.1 行 674）写的是
`{"service_id":"S39","id":"rp_...","revision":2,"content_hash":"..."}`，而 S39 §5 行 642–663 的
`repair_planning.plan` 是 17 列，`plan_id` 为主键、`manifest_revision` 有，**没有 `revision`
也没有 `content_hash`**（脚本逐列核对）。三条出路都不干净：给 S39 加两列（可它的状态机里
`superseded` 说明计划是被替换而非原地改，`revision` 可能恒为 1）；把引用收成两键（与仓内
其余 54 处不同形，且丢掉「哪一版」）；让 S42 自己算 `content_hash`（第二个权威，
AGENTS.md 禁止）。**示例已经写成了 S39 做不到的形状**，照示例收 schema 会在下游拒掉合法数据。

**（2）混音失败的信号没有通道（MIX-1）。** §4.4 行 347 要求「stems 已过期/删除时 `remix`
不可行，**必须显式失败并报告** `STEMS_UNAVAILABLE`」，E24-S42-02（行 836）把它写成验收用例。
但事件目录里没有 `post.mix.failed`（全仓 `.failed` 结尾只有两个，脚本核对），`MixJob` 状态机
（§3.2 行 218）允许 `FAILED` 却没说 `FAILED` 时发什么，而 §6.2 的查询表里**连 `mix-jobs`
的 GET 都没有**。于是 S39 唯一能走的路是轮询一个不存在的接口。要 owner 定：
`post.mix.completed` 是否在 `FAILED` 时也发（本 schema 的宽读法是允许的，`state` 枚举含
`FAILED`），还是新增一条事件。

**（3）C8 的裁决理由查无实据（MIX-3）。** C8（行 934）是**已裁决**条目，理由是「`post.mix.completed`
是 **S35/S39 的独立触发器**」，并据此选了「补登」而不是「塞进 `post.master.rendered`」。
可是 S35 与 S39 两份方案提到这条事件各 **0 次**（脚本核对），`drama.composition.v2` 在全仓
`.md` 里只有 2 份提到，其中一份是本方案的目录登记表。裁决可能仍然对——「独立触发器」可以是
将来的用途——**但今天这条事件没有任何已声明的消费者**，所以它的 payload 写成什么样都不会被
任何真实需求校验。这一点对 `post.render.failed` 同样成立（RJF-1）。

### 15.5 两条「同一张表、两处不一致」的证据

这两条我没有升成新 conflict，而是记进 RJF-3 与 RJF-2，因为它们的修法只需「把已有的列/已有的
结论搬过来」，不需要 owner 发明事实。

**相邻列被两条事件分掉。** `render_job`（§5 行 416–446）同时有 `timeline_id`（行 423）与
`purpose`（行 429）。`post.master.rendered` **两项都不发**（分别是 PMO-5 与 PMO-3），而本轮的
失败事件两项都发。后果具体：S43 的 G3 门（行 140）要校验「S41 的 timeline
`formal_master_eligible=true`」，它消费 `post.master.rendered` 却拿不到 `timeline_id`；要区分
正式包与预览包（`package_type CHECK IN ('missing_material','preview','formal')`，§5 行 384–385）
却拿不到 `purpose`。**一个只盯着候选的消费方，被迫去订阅一条失败事件才能拿到定位维度。**

**状态图与流程步骤对不上。** §3.1 行 181–182 画的是 `QUEUED ─S22 拒绝→ REJECTED（终态）`；
§4.1 第 6 步（行 279–281）申请许可、第 7 步（行 282）才 `事务写 render_job(QUEUED)`，行 290
还强调「先申请许可再落 job」。按 §4.1 被拒时行不存在（`REJECTED` 无处可落），按 §3.1 行存在
（那条跃迁没有事件）。这不是「少发一条事件」，是**两段文字不可能同时为真**，必须先裁决哪段对。

### 15.6 探针

26 条正负例对两份 schema 跑过（`Draft202012Validator` 直接验，正例 3 条、负例 23 条），全部
按预期：

```
=== post.mix.completed negative probes ===
  PASS  baseline                                             -> accepted
  PASS  sha256: prefix on mix_plan_hash                      -> REJECTED
  PASS  reason not in enum                                   -> REJECTED
  PASS  state not in enum                                    -> REJECTED
  PASS  remix_defect without repair_plan_ref                 -> REJECTED
  PASS  repair_plan_ref = null under remix_defect            -> REJECTED
  PASS  extra root key                                       -> REJECTED
  PASS  source_video_ref extra key                           -> REJECTED
  PASS  source_video_ref service_id not S28                  -> REJECTED
  PASS  consumed_cpu_ms negative                             -> REJECTED
  PASS  missing mix_job_id                                   -> REJECTED
  PASS  non-remix reason without repair_plan_ref (ACCEPT)    -> accepted

=== post.render.failed negative probes ===
  PASS  baseline                                             -> accepted
  PASS  state=SUCCEEDED (belongs to another event)           -> REJECTED
  PASS  state=FAILED without failure_code                    -> REJECTED
  PASS  state=FAILED with empty failure_code                 -> REJECTED
  PASS  purpose not in enum                                  -> REJECTED
  PASS  purpose=review (S43 package_type typo)               -> REJECTED
  PASS  render_env_hash with sha256: prefix                  -> REJECTED
  PASS  retry_count negative                                 -> REJECTED
  PASS  timeline_revision 0                                  -> REJECTED
  PASS  extra root key                                       -> REJECTED
  PASS  failure_detail as string                             -> REJECTED
  PASS  missing render_job_id                                -> REJECTED
  PASS  state=CANCELLED without failure_code (ACCEPT)        -> accepted
  PASS  state=ORPHANED (in enum by design) (ACCEPT)          -> accepted

ALL PROBES BEHAVED AS INTENDED
```

`python tools/check_all.py` 九项全过（`payloads` 报 `104 schema ref(s), 49 topic(s) with a payload
schema`；`scaffold` 120 个生成物与 catalog 一致，本轮没有改 catalog 与生成物）。
`python tools/validate/validate_examples.py` 报
`65 example(s) valid, 0 failing, 1 without a schema`。

有一条已知漏检继续存在，与 S11–S14、S02 同源：信封对 `type` / `source` 只收非空字符串，所以
示例里 `type: "post.mix.completed"` 与 `data.mix_job_id`、信封 `aggregate_id` 三者的**值相等**
这一层关系没有工具能校验——`additionalProperties: false` 只管键，不管同一份文档里不同位置的
值之间的关系。本轮的示例靠人工保持一致（`aggregate_id` 与 `data.mix_job_id` 都写成
`mj_01JCC4R2T6V8W0X2Y4Z6A8B0C2`），下一步做 codegen 校验器时应当把这类约束一起表达。

## 16. S43 / S44 这一轮：六条「只有聚合键」或「只有事件名」的事件，以及引用核对暴露的十一处错

第 15 轮把 S42 剩下两条补齐之后，`envelope-only` 归零的最后一格是 S43 与 S44 的六条事件。
这一轮把六条都写成 payload schema，`event-topics.v2.json` 的 `data_schema_ref` 从 49 条涨到
**55 条**，`envelope-only` 从 6 降到 **0**。六份 schema 各 3 条 `x-needs-owner`、各 3 条
`x-conflicts`（前缀 PDC / PDS / PDF / AMS / AEC / ACP），共 36 条新编号。

这一节的 §16.1 是**本轮自己写错的数**——十一处，其中四处在写完这一轮报告时才发现、属于
「引用了不存在的行」，另外三处是我在这一节的初稿里又写错的 ref 计数。§16.2 起是把源表重新数
一遍的脚本输出，不沿用 §15.2 的任何中间结果。

### 16.1 修正本轮自己写错的十一条数

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `delivery.package-created.v2` / `purpose` 与 `x-needs-owner[0]` | 「`package_hash` 在 `DRAFTING` 事务里刚算出来、签名尚未发生（§4.2 行 299–302）」「创建时未定稿」 | **S43 全文没有写出 `package_hash` 的算法**。它只出现 4 次：行 393 的 DDL 列（`char(64) NOT NULL`）、行 408 的唯一索引、行 602 与行 670 的示例；§4.2 行 299 算的是 `manifest_hash`，不是它。因为 DDL 是 `NOT NULL`，它必须在 §4.1 行 278 写 `package(DRAFTING)` 时就已算出——**但「已算出」这件事本身是推断，不是文档给的** | 我把「`package_hash` 此刻已定稿」与「文档写了它的算法」当成同一件事，然后把算式归给了 §4.2 的 `:seal` 流程。**行 299–302 是 `:seal` 的 8 步，算的是 `manifest_hash`**——两个 hash 混了。更要紧的是：这份 schema 原来那句把一个**没写出来的步骤**说成有出处，改完之后它变成了一条真缺口 |
| 2 | `delivery.package-sealed.v2` / `PDS-2` 的 `s43_meaning` | 「§11 C8 区域之外的 §4.2 行 283 还说过签名是为 `formal` 包服务的」 | **行 283 是空行**。「`formal` 包没有有效签名时 `deliverable=false`」在 **§2.1 行 103**；「`formal` 包缺签名 → 409 `SIGNATURE_REQUIRED`」在 **§3.3 行 247** | 行 283 在 §4.1 的创建流程（行 262–281）之后、那段「第 9–10 步拆开是刻意的」解释（行 284）之前——是个空行。**「我记得这里说过」不是出处**，回读一次就能发现 |
| 3 | `delivery.package-sealed.v2` / `manifest_hash` | 「除非再 `GET /delivery-packages/{id}/manifest`（§6.2 行 645）」 | 行 645 的**表行**写的是路径 `/delivery-packages/{id}/manifest`，不带 `GET ` 前缀（方法在行 644 的「方法」列，按表行读是 `\| GET \|`）。两处都对得上，但引用里的字符串不是源文原文 | 我把 markdown 表格的两个单元格拼成了一个字符串。**表行不能当散文引用**——要么引单元格原文，要么引整行 |
| 4 | `analytics.cost-projection-updated.v2` / `ci_low_micro` | 「`ci95` 这个名字出现在 `analytics.metric_snapshot_created.v2` 里（§5 行 416）」 | **`ci95 jsonb,` 在 §5 行 403**；行 416 是 `CREATE INDEX snapshot_lookup_idx ON analytics.metric_snapshot` | 我把「`ci95` 这一列」与「这张表的索引」两行看串了。行号落在 400 附近的同一张 DDL 里，肉眼扫过去很像 |
| 5 | `analytics.cost-projection-updated.v2` / `x-needs-owner[2]` | 「§5 行 412–418（`metric_snapshot` 有 `allocation_policy_version` 列）」 | **`allocation_policy_version text,` 在 §5 行 408**；行 412–414 是同表两条 CHECK（`snapshot_n_nonneg` / `snapshot_value_null_iff_zero_n`），行 416–417 是索引 | 同一处病灶：我用「这张 DDL 的范围」代替了「那一列的行号」。**41X 那一段里没有 `allocation_policy_version`** |
| 6 | `analytics.metric-snapshot-created.v2` / `snapshot_id` | 「与 §6.3 行 677（`aggregate_id`）」 | 行 677 是 `\| \`analytics.metric.snapshot.created\` \| \`snapshot_id\` \|` ——**`aggregate_id` 这个词在行 675 的表头**（`\| event_type \| aggregate_id \|`） | 我引了「那一行」，却在括号里承诺「`aggregate_id` 这个词」。**承诺一个 token 就必须确认那一行里有它** |
| 7 | `analytics.evaluation-completed.v2` / `evaluation_run_id` | 同上，「与 §6.3 行 678（`aggregate_id`）」 | 同上：`aggregate_id` 在行 675 表头，行 678 是本事件那一行 | 同 #6 |
| 8 | `analytics.cost-projection-updated.v2` / `projection_id` | 同上，「与 §6.3 行 679（`aggregate_id`）」 | 同上：`aggregate_id` 在行 675 表头，行 679 是本事件那一行 | 同 #6 |
| 9 | `post.mix-completed.v2` / `repair_plan_ref` 的 `description` | 「与仓内 **52 处**同形引用一致」 | 按四重条件（`type=object` + `properties`/`required` 恰为四键 + `additionalProperties: false`）数 `contracts/*.schema.json`（64 个文件）得 **53 处 / 31 个文件** | 52 是**上一轮写完 `post.mix-completed` 之前**的口径（`git show HEAD~3` 数得 52）。**在一个会随本轮改动的分母上引用了旧值**——与 §15.1 第 3 条同一类错，那一轮修了一次，这一轮又犯 |
| 10 | `post.mix-completed.v2` / `MIX-2` 的 `why_it_matters` | 「仓内 **55 处**严格四键 closed 引用（另有 **2 处**同形但允许 null，**都在本文件**）」「脚本按 `properties`+`required`+`additionalProperties:false` **三重**条件数 `contracts/*.schema.json`，其中 **3 处**就是本文件」 | **55 处 / 32 个文件**（递归 glob）/ **53 处 / 31 个文件**（顶层 glob），差的 2 处都在 `contracts/quality/qc-report.v2.schema.json`；可空同形 **6 处 / 4 个文件**（`episode-manifest.v2` 1、`event-envelope.v2` 的 `data_ref` 1、`generation-command.v2` 2、`post.mix-completed.v2` 2）；**本文件贡献 1 处严格（`source_video_ref`）** | 一处句子里叠了四个错：(a) 宣称的 glob（`contracts/*.schema.json`）数不出 55；(b) 「三重条件」漏了 `type=object`，于是把 `continuity.check-completed.v2` 里一个 `required` 只有两项的开放形状算进了 33 个文件；(c) 「可空都在本文件」漏了另外三个文件；(d) **「3 处就是本文件」把 1 处严格 + 2 处可空加在了一起**，那是两个不相加的数 |
| 11 | `post.mix-completed.v2` / `MIX-2` 的三条出路 (2) | 「与仓内其余 **54 处**不同形」 | 其余 **54 处**（严格 55 − 本文件 1；按递归 glob），若按顶层 glob 则是 52 | 与 #9 同源：分母口径没写死就减 |

第 10 条值得单独一句：**「本文件贡献几处」这种归属计数必须把严格与可空分开数**。我第一版
修完写成「本文件贡献 0 处严格、只贡献 2 处可空」，回读时发现 55 − (其余文件) 对不上，才
数出 `source_video_ref`。**总数与归属两个数要能互相验算**——只对得上一个说明另一个错了。

### 16.2 本轮脚本输出（不沿用第 15.2 节的中间结果）

`_tmp_census16.py`：

```
=== 1. contract census (after this round) ===
contracts/*.schema.json       = 64
contracts/**/*.schema.json    = 71
contracts/**/*.json           = 148
contracts/examples/*.json     = 72
topics total                  = 55
  with data_schema_ref        = 55
  envelope-only               = 0
  schema_ref + data_schema_ref= 110
  topics having "consumers"   = 0

=== 2. the six schemas written this round ===
  delivery.package.created               props=10  req=10  needs=3 conf=3 allOf=False
  delivery.package.sealed                props=10  req=10  needs=3 conf=3 allOf=False
  delivery.publish.failed                props=13  req=10  needs=3 conf=3 allOf=False
  analytics.metric.snapshot.created      props=17  req=17  needs=3 conf=3 allOf=True
  analytics.evaluation.completed         props=13  req=13  needs=3 conf=3 allOf=False
  analytics.cost.projection.updated      props=10  req=10  needs=3 conf=3 allOf=False

=== 3. canonical closed 4-key ref across contracts/**/*.schema.json ===
  strict 4-key closed refs      = 55 in 32 files
  nullable same-shape refs      = 6 in 4 files

=== 4. hash pattern forms across contracts/**/*.schema.json ===
  "pattern" entries            = 264
  of which == ^[0-9a-f]{64}$   = 139
  "pattern" containing sha256: = 0

=== 5. event types ending in .failed ===
  n = 2 ['post.render.failed', 'delivery.publish.failed']

=== 6. conflict-id prefix collisions ===
  conflict ids                 = 303
  distinct prefixes            = 55
  PDC new, used by             = ['PDC-1', 'PDC-2', 'PDC-3']
  PDS new, used by             = ['PDS-1', 'PDS-2', 'PDS-3']
  PDF new, used by             = ['PDF-1', 'PDF-2', 'PDF-3']
  AMS new, used by             = ['AMS-1', 'AMS-2', 'AMS-3']
  AEC new, used by             = ['AEC-1', 'AEC-2', 'AEC-3']
  ACP new, used by             = ['ACP-1', 'ACP-2', 'ACP-3']

=== 7. README table rows vs topics ===
  README payload-table rows    = 55
  topics absent from the table = []

=== 8. examples registered in SCHEMA_OF ===
  SCHEMA_OF rows               = 65
```

第 3 行是这一轮改过两次的那个数。第一版用「`type=object` + `properties` 恰为四键」两个条件，
得 55 处但 **33 个文件**；加上 `required` 恰四项与 `additionalProperties: false` 之后是
**32 个文件**。差的那一个文件是 `continuity.check-completed.v2.schema.json`：它有两个
`properties` 是四键、`additionalProperties: false`，但 `required` 只有 `id` / `revision`
两项——**同一族形状的「宽松版」，不是引用**。附带发现，`contracts/quality/` 下的
`qc-report.v2.schema.json` 内含 **2 处**严格引用，这就是顶层 glob 与递归 glob 相差的那 2 处。

`_tmp_refs17.py`（两个 glob 分开数）：

```
=== current working tree ===
TOP  glob=contracts/*.schema.json
  files matched        = 64
  strict 4-key refs    = 53  in 31 file(s)
  nullable same-shape  = 6  in {'episode-manifest.v2.schema.json': 1, 'event-envelope.v2.schema.json': 1, 'generation-command.v2.schema.json': 2, 'post.mix-completed.v2.schema.json': 2}
RECURSIVE  glob=contracts/**/*.schema.json
  files matched        = 71
  strict 4-key refs    = 55  in 32 file(s)
  nullable same-shape  = 6  in {'episode-manifest.v2.schema.json': 1, 'event-envelope.v2.schema.json': 1, 'generation-command.v2.schema.json': 2, 'post.mix-completed.v2.schema.json': 2}

delta (only under contracts/**):
  contracts\quality\qc-report.v2.schema.json
```

`git show` 逐 commit 回数（证明 52 是旧值）：

```
HEAD~1 strict=54 nullable=4 files=63  postmix(strict=0 nullable=0)
HEAD~2 strict=53 nullable=4 files=62  postmix(strict=0 nullable=0)
HEAD~3 strict=52 nullable=4 files=61  postmix(strict=0 nullable=0)
```

`HEAD~1` 的 54 → 现在 55，只涨 1，因为第 15 轮的两份 schema 里没有四键引用；52 出现于
`HEAD~3`，那正是 `post.mix-completed` 尚未写入时。

`_tmp_cols16.py`（按括号深度解析 DDL，不按行切——多行 `CHECK (... IN ('a','b','c'))` 会
把一行切成几段）：

```
=== plans/S43-delivery.md ===
  delivery.package                           L377 -404  23 cols
  delivery.item                              L410 -430  10 cols
  delivery.manifest                          L433 -443   8 cols
  delivery.integrity_signature               L445 -458  11 cols
  delivery.publish_authorization_ref         L462 -476  11 cols
  delivery.distribution_attempt              L478 -496  13 cols
  delivery.receipt                           L502 -513   9 cols
  delivery.channel                           L516 -529  10 cols
  delivery.command_receipt                   L531 -538   5 cols
  delivery.outbox                            L540 -551  10 cols
  delivery.inbox                             L555 -560   3 cols

=== plans/S44-evaluation-analytics.md ===
  analytics.metric_definition                L366 -378  10 cols
  analytics.cohort                           L380 -389   7 cols
  analytics.metric_snapshot                  L393 -415  17 cols
  analytics.allocation_policy                L419 -426   5 cols
  analytics.cost_allocation                  L428 -440   7 cols
  analytics.benchmark_dataset                L444 -453   7 cols
  analytics.benchmark_item                   L455 -465   8 cols
  analytics.evaluation_run                   L467 -484  13 cols
  analytics.blind_review                     L486 -498  10 cols
  analytics.blind_review_vote                L500 -510   8 cols
  analytics.experiment                       L512 -523   9 cols
  analytics.experiment_assignment            L525 -532   5 cols
  analytics.cost_projection                  L534 -546  10 cols
  analytics.customer_roi                     L548 -558   8 cols
  analytics.outbox                           L560 -571  10 cols
  analytics.inbox                            L575 -580   3 cols
```

六份 schema 里每一处「N 列」的断言都以这张表为准：`delivery.package` 23、
`delivery.distribution_attempt` 13、`analytics.metric_snapshot` 17、`analytics.evaluation_run` 13、
`analytics.cost_projection` 10。**`evaluation_run` 是 13 而不是 15**——`_tmp_verify16.py` 里
那个按逗号切分的临时数法把它数成 15，因为它把 `CHECK (state IN (...))` 里的两个逗号也当成了
列分隔符。这就是为什么最终数字一律取括号深度解析的结果。

### 16.3 本轮改了哪些文件

| 文件 | 状态 | 改了什么 |
|---|---|---|
| `contracts/delivery.package-created.v2.schema.json` | 新增 | 10 属性 / 10 必填 / 3 `x-needs-owner` / 3 `x-conflicts`（PDC-1..3） |
| `contracts/delivery.package-sealed.v2.schema.json` | 新增 | 10 / 10 / 3 / 3（PDS-1..3） |
| `contracts/delivery.publish-failed.v2.schema.json` | 新增 | 13 属性 / 10 必填 / 3 / 3（PDF-1..3） |
| `contracts/analytics.metric-snapshot-created.v2.schema.json` | 新增 | 17 / 17 / 3 / 3（AMS-1..3），含 `allOf` 复刻 `snapshot_value_null_iff_zero_n` |
| `contracts/analytics.evaluation-completed.v2.schema.json` | 新增 | 13 / 13 / 3 / 3（AEC-1..3） |
| `contracts/analytics.cost-projection-updated.v2.schema.json` | 新增 | 10 / 10 / 3 / 3（ACP-1..3） |
| `contracts/examples/*.json`（6 个） | 新增 | 每条事件一个完整信封示例，`aggregate_id` 与 `data` 里的主键逐一对齐 |
| `contracts/event-topics.v2.json` | 改 | 6 条 topic 各加一个 `data_schema_ref`；`git diff --stat` 只 +6 行（其余键不动） |
| `tools/validate/validate_examples.py` | 改 | `SCHEMA_OF` +6 行，全部指向 `event-envelope.v2.schema.json`（不是 payload schema） |
| `contracts/README.md` | 改 | 8 处计数勘误 + 6 行 payload 表新增（表从 49 行涨到 55 行） |
| `contracts/delivery.package-published.v2.schema.json` | 改 | `x-needs-owner[1]` 的 `s43_status` 加一句：那三条「payload schema 仍缺」已由本轮补齐 |
| `contracts/post.mix-completed.v2.schema.json` | 改 | 第 15 轮遗留的 ref 计数（`52 处` / `MIX-2`）按 §16.1 第 9–11 条改写 |

`validate_examples.py` 那 6 行必须是信封而不是 payload：`SCHEMA_OF` 的键是示例文件名，示例
是**完整信封**。写第一版时我把 6 条都指向了各自的 payload schema，`check_all.py` 立刻报
六个 `'<key>' is a required property at <root>`——信封缺 payload 的键。**payload 只能经
`data_schema_ref` 到达，`SCHEMA_OF` 永远指信封**。

### 16.4 六条事件的出处与逐字段理由

六条事件的写法分两类，正好对应两种文档状态：

**（一）只有事件名，没有字段清单（S43 三条 + S44 三条共六条，全部如此）。**
S43 §6.3 行 655 只列四个事件名；S44 §6.3 行 658 只列三个事件名。六条事件在各自方案的
全文里都**没有 `data` 块、没有示例、没有字段表**。因此六份 schema 的键全部取自 §5 的 DDL，
取舍写进各自的 `x-needs-owner` 第 1 条：

- **`delivery.package.created`** — 取自 `delivery.package` 的 23 列。取 `package_id` /
  `package_type` / `episode_id` / `purpose` / `master_candidate_id` / `state` / `gaps` /
  `gate_results` / `created_at`。刻意去掉 `tenant_id`（信封已有）、`manifest_hash` /
  `package_hash`（后者全文无算法）、`canonical_version`、`execution_fence`、
  `created_by`（actor 形状未定义，见 `x-needs-owner[2]`）。
- **`delivery.package.sealed`** — 取自 `delivery.package` + `delivery.manifest`（8 列）+
  `delivery.integrity_signature`（11 列）。取 `manifest_hash` / `canonical_version` /
  `item_count` / `total_bytes` / `sealed_at` 与三个定位键；**`integrity_signature` 的 11 列
  一列都不取**，因为 §6.3 行 678 明说「事件不含签名内容」——这是 PDS-2 的核心张力：
  一条「已签名」的事件不许带签名标识，审计方就必须再查一次 API。
- **`delivery.publish.failed`** — 取自 `delivery.distribution_attempt` 的 13 列。取
  `attempt_no` / `failure_code` / `failure_detail` / `channel_id` / `attempted_at` 等；
  13 属性里 10 必填，`failure_detail` 与两个渠道侧的可空列不强制。
- **`analytics.metric-snapshot-created`** — 取自 `analytics.metric_snapshot` 的 17 列，
  **17 属性全必填**（DDL 里这 17 列也全部 `NOT NULL` 或有 `DEFAULT`），并用 `allOf` 的
  if/then 复刻行 412–414 的 `snapshot_value_null_iff_zero_n`：`n=0` 时 `value` 必须为
  `null`，`n>0` 时必须为数字。这是六份 schema 里唯一带条件约束的一份。
- **`analytics.evaluation-completed`** — 取自 `analytics.evaluation_run` 的 13 列，
  13 属性全必填。`state` 收成闭集（DDL 行 473–475 有 `CHECK (state IN (...))`），
  这是本仓「取值域只在 DDL 有 CHECK 时才收窄」规矩的正面例子。
- **`analytics.cost-projection-updated`** — 取自 `analytics.cost_projection` 的 10 列，
  10 属性全必填。`based_on_rate` 的单位与币种在方案里两处都没写，登记为
  `x-needs-owner[1]`——**同一张表五个金额列里唯一不是 `bigint ... _micro` 的一个**。

**（二）「事件名 + 聚合键一行」以外的信息量。** S44 的三条比 S43 稍多一行：§6.3 行 675–679
给了聚合键表。三条的 `aggregate_id` 分别是 `snapshot_id` / `evaluation_run_id` /
`projection_id`，本批三份 schema 都把它写进 `description` 并与 `data` 里的同名字段对齐。
`cost.projection.updated` 的 `updated` 与「主键 `(tenant_id, projection_id)` 且无版本列」
合起来意味着同一 ID 反复覆盖——这条语义写进了 `projection_id` 的说明，是本轮唯一一处
「从事件名推导语义」的地方，也标了出处（§6.3 行 658 + §5 行 536 / 545）。

**（三）谁在消费。** 六条里只有 `delivery.package.sealed` 有本方案之外的提及者：
`plans/S46-developer-integration.md`（行 129 的订阅示例、行 262 的 webhook 签名）与
`plans/S47-realtime-projection.md`。另外五条**在整个 `plans/` 目录里只出现在自己的方案中**
（`_tmp_verify16.py` 的 C 段）。这意味着这五条 payload 的形状**今天没有任何真实消费者可以
校验**——写成什么样都不会被下游拒掉。这不是本轮能解决的，但它是 `x-conflicts` 里好几条
（尤其 PDC-1、AMS-1）的共同背景：没有消费方，就无法判断某个字段该不该上事件。

### 16.5 两处「上一轮的结论被这一轮推翻」

**（一）`delivery.package-published.v2` 的 `x-needs-owner[1]`。** 原文说那三条事件的
payload 仍是缺口。本轮补齐之后这句就过期了，改成「后三条已由本仓补成 …」并保留
「本条 `published` 仍缺的东西与那三份不同：见各条 `x-conflicts`」。**没有删掉这一条**——
它记的是「方案里没写 payload」这个事实，那个事实不因我们补了 schema 而消失。

**（二）`post.mix-completed.v2` 的 MIX-2。** 第 15 轮修过一次 ref 计数（§15.1 第 3 条），
修法是「52 → 55，本文件贡献 3 处」。这一轮按 §16.1 第 10 条重新数，**那个修法本身是错的**：
本文件贡献 1 处严格引用，不是 3 处；3 是「1 严格 + 2 可空」，两个不相加的数被加在了一起。
现在的写法把 glob 与「可空」两个口径都写死，并给出 53/31 与 55/32 两个数。
**连续两轮在同一句话上出错，说明「一处引用形状的计数」这个说法本身需要模板**：分母（哪个
glob）、条件（哪几个关键字）、严格与可空是否分开——三样缺一样，数出来的东西就不能引用。

### 16.6 探针

`_tmp_probe16.py`：41 条探针对六份 payload schema 直接跑（`Draft202012Validator`，示例的信封
拆出 `data` 后再验），7 条因该 schema 没有合适字段而跳过。全部符合预期：

```
=== delivery.package-created  (payload of 10 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:package_id           REJECTED  required     @ <root>
  SKIP sha_prefix (no suitable field)
  SKIP negative_int (no suitable field)
  PASS null_required:package_id           REJECTED  type         @ package_id
  PASS enum:package_type                  REJECTED  enum         @ package_type

=== delivery.package-sealed  (payload of 10 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:package_id           REJECTED  required     @ <root>
  PASS sha_prefix:manifest_hash           REJECTED  pattern      @ manifest_hash
  PASS negative_int:item_count            REJECTED  minimum      @ item_count
  PASS null_required:package_id           REJECTED  type         @ package_id
  PASS enum:package_type                  REJECTED  enum         @ package_type

=== delivery.publish-failed  (payload of 13 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:package_id           REJECTED  required     @ <root>
  SKIP sha_prefix (no suitable field)
  PASS negative_int:attempt_no            REJECTED  minimum      @ attempt_no
  PASS null_required:package_id           REJECTED  type         @ package_id
  PASS enum:package_type                  REJECTED  enum         @ package_type

=== analytics.metric-snapshot-created  (payload of 17 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:snapshot_id          REJECTED  required     @ <root>
  SKIP sha_prefix (no suitable field)
  PASS negative_int:metric_version        REJECTED  minimum      @ metric_version
  PASS null_required:snapshot_id          REJECTED  type         @ snapshot_id
  SKIP enum (no suitable field)

=== analytics.evaluation-completed  (payload of 13 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:evaluation_run_id    REJECTED  required     @ <root>
  SKIP sha_prefix (no suitable field)
  PASS negative_int:dataset_version       REJECTED  minimum      @ dataset_version
  PASS null_required:evaluation_run_id    REJECTED  type         @ evaluation_run_id
  PASS enum:state                         REJECTED  enum         @ state

=== analytics.cost-projection-updated  (payload of 10 keys) ===
  PASS baseline                           accepted
  PASS schema_version:schema_version      REJECTED  const        @ schema_version
  PASS extra_root:zz_unexpected           REJECTED  additionalProperties @ <root>
  PASS drop_required:projection_id        REJECTED  required     @ <root>
  SKIP sha_prefix (no suitable field)
  PASS negative_int:actual_to_date_micro  REJECTED  minimum      @ actual_to_date_micro
  PASS null_required:projection_id        REJECTED  type         @ projection_id
  PASS enum:window                        REJECTED  enum         @ window

probes: 41 PASS / 41 run, 7 skipped
```

`delivery.package-created` 跳过 `sha_prefix` 与 `negative_int`，是因为它**没有哈希列也没有
计数列**：`gaps` / `gate_results` 是裸 `array` / `object`，`state` 是闭集但没有数值。这不是
漏检，是那份 payload 确实没有可探的数值/哈希字段——第 16.4 节说了它是「消费方只拿到定位
与门结果」的设计。

`_tmp_neg2.py` 单独验 `analytics.metric-snapshot-created` 的 `allOf`（2×2 真值表）与
`format` 是否被强制：

```
=== allOf truth table on metric-snapshot (n vs value) ===
  n=0   value=None   -> accepted  OK
  n=0   value=0.5    -> REJECTED  OK
       keyword=type  path=['value']  msg=0.5 is not of type 'null'
  n=12  value=None   -> REJECTED  OK
       keyword=type  path=['value']  msg=None is not of type 'number'
  n=12  value=0.5    -> accepted  OK

=== format is advisory: rfc3339_validator installed? ===
  rfc3339_validator = NOT INSTALLED -> jsonschema never enforces "format"
  jsonschema version = 4.26.0
```

第二条很重要：本仓 264 处 `"pattern"` 里 139 处是 `^[0-9a-f]{64}$`，这些**是被强制的**
（`pattern` 不是 `format`）；而所有 `"format": "date-time"` 一处都不生效。**六份 schema 里
每一个时间字段写 `format: date-time` 都只是文档约定**，若真要在 CI 里挡住畸形时间戳，需要装
`rfc3339-validator` 或改用 `pattern`。这是全仓既有的约定，不是本轮引入的缺陷，但它解释了
为什么探针里没有一条「坏时间戳」的负例——加了也不会被拒。

`python tools/check_all.py`：九项全过。

```
ok    json       148 file(s)
ok    payloads   110 schema ref(s), 55 topic(s) with a payload schema
ok    checkids   99 check_id(s), 3 known non-conforming (owner decision, see contracts/README.md gap 4)
ok    links      1321 local link(s)
ok    catalog    48 service(s), 55 topic(s)
ok    families   14 family/families
regenerated 48 services
wrote 13 agents
wrote 9 connectors
ok    scaffold   120 generated file(s)

71 example(s) valid, 0 failing, 1 without a schema
ok    examples   contracts/examples/
ok    registry   no check-registry.v2.json yet; nothing to validate

9 check(s) passed
```

`payloads` 从第 15 轮的 `104 schema ref(s), 49 topic(s)` 变成 `110 / 55`，`envelope-only` 归零。
`scaffold` 重新生成 120 个文件后 `git status` 干净——本轮没有改
`contracts/service-catalog.v2.json`，所以生成物不该变，也确实没变。

**一条已知漏检继续存在**：信封对 `type` / `source` 只收非空字符串，所以示例里
`type`、信封 `aggregate_id` 与 `data` 里主键三者的**值相等**这一层关系没有工具能校验。
本轮六个示例靠脚本逐对断言（`aggregate_id == data.<主键字段>`）保持一致，**但那条断言
只在我的临时脚本里，没有进仓库**。与 §15.6 末段记的是同一件事，下一步做 codegen 校验器
时应把它变成常驻检查。
