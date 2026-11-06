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
  schema_ref + data_schema_ref= 104  (= 55 schema_ref + 49 data_schema_ref, a ref count)
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

## 16. S43 / S44 这一轮：六条「只有聚合键」或「只有事件名」的事件，以及引用核对暴露的十四处错

第 15 轮把 S42 剩下两条补齐之后，`envelope-only` 归零的最后一格是 S43 与 S44 的六条事件。
这一轮把六条都写成 payload schema，`event-topics.v2.json` 的 `data_schema_ref` 从 49 条涨到
**55 条**，`envelope-only` 从 6 降到 **0**。六份 schema 各 3 条 `x-needs-owner`、各 3 条
`x-conflicts`（前缀 PDC / PDS / PDF / AMS / AEC / ACP），共 36 条新编号。

这一节的 §16.1 是**本轮自己写错的数**——十四处，其中四处在写完这一轮报告时才发现、属于
「引用了不存在的行」，另外三处是我在这一节的初稿里又写错的 ref 计数，第十二处（#12）与第十三处（#13）是后续定时自检回合才发现的：
散文里的「本仓」没写分母，以及 `schema_ref + data_schema_ref` 那个 ref 数被当成了 topic 数。
第十四处（#14）是再下一轮自检发现的，且是我在 §16.9 里**新写**的——把「同一类漏检被提到 4 次」
说成了「4 处都用了『已知漏检』这个标签、说同一句话」，实际是 3 处带标签。
§16.2 起是把源表重新数一遍的脚本输出，不沿用 §15.2 的任何中间结果。

### 16.1 修正本轮自己写错的十四处

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
| 12 | §16.5 行 1591 的散文 | 「**本仓** 264 处 `"pattern"` 里 139 处是 `^[0-9a-f]{64}$`」 | 264 / 139 是按 `contracts/**/*.schema.json`（71 个文件）数出来的；放宽到 `contracts/**/*.json`（148 个文件）是 274 / 143，多出的 10 处来自 `redaction-policy.v2.json`（6 处，无一是 sha）与 `mcp-tools.v2.design.json`（4 处，全是 sha） | **「本仓」不是分母**。同一个报告的第 4 节括注了 glob，散文里却退回「本仓」——§16.2 刚立的口径规则，自己在 270 行后就违反了 |
| 13 | §15.2、§16.2、§16.7 的 `schema_ref + data_schema_ref` 三行 | 「`schema_ref + data_schema_ref= 104`」「`= 110`」，与同一块里的 `topics total = 55` 排在同一个列表里 | 这两行是 **ref 数**，不是 topic 数：55 个 topic 每个都同时有 `schema_ref` 与（本轮之后）`data_schema_ref`，所以 55 × 2 = 110；本轮之前是 55 + 49 = 104 | **数对了，单位没写。**它紧跟在 `topics total = 55` 下面、又用同一个 `=` 对齐，读起来像「110 个 topic」。这正是本报告反复出事的那一类：分母/单位不写清楚，数本身再对也会被误读 |
| 14 | §16.9 开头两句 | 「`contracts/README.md` 在**四处**把它记成『已知漏检』（S13 行 1215、S14 行 1277、S12 行 1349，以及 S47 行 1087 那条关于 `aggregate_id` 的）。四处都写着同一句话：信封的 `source` 与 `type` 只是……」 | **三处**是「已知漏检」（S13 / S14 / S12），三处写同一句话；**第四处**（S47 行 1087）说的是 `aggregate_id` 与主键不相等，措辞也不同 | `git show 0cccb50^:contracts/README.md` 数得「已知漏检」= 3、「本仓信封不钉生产者与主题的对应关系」= 2、「本仓信封不对任何 topic」= 2。我把「同一类漏检在 README 里被提到 4 次」与「4 次都用了同一个标签、说同一句话」混成一件事——**「同一类」不等于「同一句」**，而我改的正是被标签点名的那三处，第 4 处是顺带改措辞 |

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
  schema_ref + data_schema_ref= 110  (= 55 topics x 2 refs)
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
  of which == ^[0-9a-f]{64}$   = 139  (in 53 files, 0 inside an x- extension)
  "pattern" containing sha256: = 0
  -- widened to contracts/**/*.json (148 files) --
  "pattern" entries            = 274
  of which == ^[0-9a-f]{64}$   = 143
     redaction-policy.v2.json     +6 patterns, +0 sha
     mcp-tools.v2.design.json     +4 patterns, +4 sha

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
| `contracts/examples/*.json`（6 个） | 新增 | 每条事件一个完整信封示例，`aggregate_id` 与 `data` 里的主键逐一对齐（这 6 条现在由 §16.8 的检查守着） |
| `contracts/event-topics.v2.json` | 改 | 6 条 topic 各加一个 `data_schema_ref`；`git diff --stat` 只 +6 行（其余键不动） |
| `tools/validate/validate_examples.py` | 改 | `SCHEMA_OF` +6 行，全部指向 `event-envelope.v2.schema.json`（不是 payload schema）；另加 `aggregate_identity_problems()`（见 §16.8）与 `envelope_identity_problems()`（见 §16.9）两个常驻检查 |
| `contracts/README.md` | 改 | 8 处计数勘误 + 6 行 payload 表新增（表从 49 行涨到 55 行）；另把三处「已知漏检」与 S47 行 1087 一处改为指向 §16.8 / §16.9 的两个检查（见 §16.9） |
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

第二条很重要：`contracts/**/*.schema.json`（71 个文件）264 处 `"pattern"` 里 139 处是
`^[0-9a-f]{64}$`（139 处分布在 53 个文件，0 处落在 `x-` 扩展里，即全部处于强制校验位置），这些**是被强制的**
（`pattern` 不是 `format`）；而所有 `"format": "date-time"` 一处都不生效。**六份 schema 里
每一个时间字段写 `format: date-time` 都只是文档约定**，若真要在 CI 里挡住畸形时间戳，需要装
`rfc3339-validator` 或改用 `pattern`。这是全仓既有的约定，不是本轮引入的缺陷，但它解释了
为什么探针里没有一条「坏时间戳」的负例——加了也不会被拒。

补一句口径：264 / 139 这两个数是按 `contracts/**/*.schema.json` 数出来的。若把范围放宽到
`contracts/**/*.json`（148 个文件），会得到 274 / 143。多出的 10 处 `"pattern"` 全在两个文件里：
`redaction-policy.v2.json` 贡献 6 处（都是描述「哪类字段用什么模式」的文本，无一是 `^[0-9a-f]{64}$`），
`mcp-tools.v2.design.json` 贡献 4 处（都是 `^[0-9a-f]{64}$`，于是 sha 那一栏 139 → 143 的差正好全部来自它）。
这两个文件都不是 JSON Schema，它们的 `"pattern"` 不参与任何示例校验，所以引用这两个数时必须说明分母用的是哪个 glob。

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

**那条「已知漏检」已经不在了**（下面 §16.8 记的是同一件事的收尾）：信封对 `type` / `source`
只收非空字符串，所以「信封 `aggregate_id` 与 `data` 里主键的值相等」这一层关系 schema 表达不了
——但现在由 `tools/validate/validate_examples.py` 的 `aggregate_identity_problems()` 常驻检查。
它已经进了 `check_all.py` 的 `examples` 项，不再是我的临时脚本。

### 16.7 定时自检回合：把第 16 节的每个数重新数一遍

提交之后按定时任务跑了一遍全量回顾。九项检查全过，三个 scaffold 生成器重新跑完
`git status` 干净。然后**不沿用 §16.2 的任何输出**，把第 16 节里的每个数从源表重数：

```
contracts/examples/*.json                 = 72
topics total                              = 55   (event_type 去重 55，无重复)
  with data_schema_ref                    = 55
  envelope-only                           = 0
  schema_ref + data_schema_ref            = 110  (= 55 topics x 2 refs, not 110 topics)
  topics having "consumers"               = 0
event types ending in .failed             = 2 ['delivery.publish.failed', 'post.render.failed']
kafka topics distinct                     = 48
README rows that are event types          = 55   (absent=0, extra=0)

strict 4-key closed refs   top glob       = 53 in 31 files
                           recursive      = 55 in 32 files
nullable same-shape refs                  = 6 in 4 files
partial (4 props + closed, required short) = 2 in 1 file  (continuity.check-completed.v2)

conflict ids                              = 303 | distinct prefixes = 55 | duplicates = 0
pattern entries (contracts/**/*.schema.json, 71 files) = 264 | sha = 139 (53 files, 0 in x-)
pattern entries (contracts/**/*.json, 148 files)       = 274 | sha = 143
   delta: redaction-policy.v2.json +6/+0 ; mcp-tools.v2.design.json +4/+4
```

六份新 schema 的 props / required / x-needs-owner / x-conflicts 也与 §16.2 第 2 节逐份对上
（10/10/3/3、10/10/3/3、13/10/3/3、17/17/3/3、13/13/3/3、10/10/3/3，只有 metric-snapshot 带 `allOf`）。

只有一处不符：§16.5 行 1591 的散文写「**本仓** 264 处」，而 264 是按
`contracts/**/*.schema.json` 数的；放宽到 `contracts/**/*.json` 是 274。这条已补进 §16.1 #12。
顺带记一个新增口径：那 139 处 sha 模式**全部落在强制校验位置**（0 处位于 `x-` 扩展子树内，
即不是「只是注释」），所以「这些是被强制的」这句话本身站得住，缺的只是分母。

**探针口径也要说清**：`nullable` 的 6 处用两种算法都能得到——只认 `type: ["object","null"]`
的窄检测器得 2 处（全在 `post.mix-completed.v2`），加上 `oneOf`/`anyOf` 包 null 的宽检测器得 6 处。
**窄检测器会漏掉 `episode-manifest.v2`、`event-envelope.v2`、`generation-command.v2` 那 4 处**；
反过来，宽检测器若把整个 `oneOf` 节点当成一个可空引用、不再下钻，严格数会从 55 掉到 51。
两个数各自都对，但**必须写清用的是哪个算法**——这与 §16.2 对 glob 的要求是同一类。

### 16.8 把 `aggregate_id == data.<主键>` 做成常驻检查，以及它暴露的六个例外

§15.6 与 §16.6 都记着同一件事：示例里「信封的 `aggregate_id` 与 `data` 里主键的值相等」
这层关系没有工具能校验，只有临时脚本在断言。这一段把它落进仓库。

**先说结论：这条断言在 55 个带 `aggregate_id` 的示例里只对 49 个成立。**若照 §16.6 原话
直接写成「每个示例都必须相等」，会有 6 条误报。这 6 条全部有明文依据，不是示例写错：

| event_type | `aggregate_id` 是什么 | 凭什么 |
|---|---|---|
| `connector.observation.received` | 平台侧 `provider_job_id`（`pj_`） | PCR-6 / PCR-7；S27 §9.2 行 683 写 `aggregate_id = provider_job_id`（可空） |
| `context.pack.created` | 装配作用域 `series:branch:episode` | CCP-4；S18 §6.3 行 469–470 |
| `continuity.check.completed` | 同上作用域；真正指向的实体是 `report_id` | contracts/README.md 行 206、CCP-4；S17 §5.2 行 343–344 |
| `generation.job.state-changed` | `provider_job_id`，S26 明文选择不用 `attempt_id` | S26 §12 行 1016 |
| `memory.canon.committed` | `series:branch`，即 `commit_sequence` 的单调作用域 | MSC-7；S16 DDL 行 98「每 series+branch 单调递增」 |
| `model.capability.changed` | 四段复合键 `{provider}/{channel}/{model_id}/{model_snapshot}` | MCG-2；S24 §3.5 行 596 |

所以检查写成**二选一**，不是「一律相等」：

```
命中（data 里有值等于 aggregate_id） 且 不在豁免表  -> 通过
命中 且 在豁免表                                  -> STALE（豁免已过期，删掉）
未命中 且 在豁免表                                -> 通过
未命中 且 不在豁免表                              -> 失败
```

第三、四行是关键：**豁免表里的一条一旦开始命中就报错**，否则文档修好之后豁免会永远躺着，
把真回归盖住。这与 §16.2「统计口径要能被复核」是同一个思路——豁免也得有失效条件。

实现放在 `tools/validate/validate_examples.py` 的 `aggregate_identity_problems()`，
已由 `check_all.py` 的 `examples` 项自动跑到（它 `import` 那个模块并调用 `main()`）。
`_strings()` 递归取 `data` 下所有字符串叶，所以嵌在 `*_ref.id` 里的也算。这是必须的：
**55 个带 `aggregate_id` 的示例里有 15 个在 `data` 顶层找不到相等的 `*_id`**，其中
9 个靠嵌套命中——`artifact.ingested`（`data.artifact_ref.id`）、`asset.binding-set.created`
（`data.binding_set_ref.id`）、`asset.{character,costume,prop,scene}-revision.accepted`
（`data.*_ref.id`）、`policy.revision.published`（`data.policy_ref.id`）、
`prompt.compiled` 与 `snapshot.manifest.committed`（`data.manifest_ref.id`）。
只看顶层的话这 9 条会全部误报，再加上下面 6 条豁免，一共 15 条假失败。

**这条检查比「主键相等」弱，要说清楚**：它断言的是「`data` 里某处有一个值等于
`aggregate_id`」，不是「`data` 的主键字段等于 `aggregate_id`」。差别在于前者理论上可被
一个恰好相等的非主键字段满足。之所以没写成后者：55 个示例的主键字段名各不相同
（`package_id` / `run_id` / `shot_lineage_id` / `data.package_ref.id`……），而 schema 里没有
「哪一个是主键」的标记，靠命名猜会引入新的猜测。**这条检查能挡住「id 漂移」这类真回归，
挡不住「相等但出现在错误字段」的假通过**——后者要等 schema 有了 `x-primary-key` 之类的
标记才做得动，记在这里不假装做到了。

反向测试（`_tmp_negtest.py`，四种情形，跑完即删）：

```
1. baseline: 71 个示例全部过                       problems: 0
2. 把 delivery.package.created 的 aggregate_id 改坏 -> 报「appears nowhere in data ... not in AGGREGATE_EXEMPT」
3. 给 memory.canon.committed 塞一个相等的 data.aggregate_id -> 报「STALE exemption ... delete the AGGREGATE_EXEMPT entry」
4. 造一个不在 registry 的新 event_type + 不匹配的 id -> 报同一类失败
5. 豁免事件的 aggregate_id 置空（PCR-5 的 UNMAPPED 路径） -> 跳过，不报
6. 六条豁免的 event_type 全在 event-topics.v2.json 里        -> 6/6 True
```

第 5 条是刻意的：`aggregate_id` 为空时这条事件按 PCR-7 本来就发不出去（envelope 要求
`minLength: 1`），检查不该在「本该为空」的地方制造第二重噪音。

`python tools/check_all.py` 仍是九项全过（`examples` 一项的输出不变：71 valid / 0 failing / 1 without a schema）。

### 16.9 把「示例的 `type` / `source` 对不对」也做成常驻检查

§16.8 关掉的是「`aggregate_id` 追不到」那一类。同一批示例里还有另一类一直没人管，
`contracts/README.md` 在**三处**把它记成「已知漏检」（S13 行 1215、S14 行 1277、
S12 行 1349），三处写的是同一句话：信封的 `source` 与 `type` 只是
`{"type":"string","minLength":1}`，所以改坏了也没人拦。**第四处**（S47 行 1087）说的是另一件事
——信封也不把 `aggregate_id` 与 `data` 里的主键钉成相等——它已被 §16.8 的
`aggregate_identity_problems()` 收掉，这一轮只顺带改了措辞。

**先证实它不是空话。**把 `artifact-ingested.json` 的 `type` 改成 `artefact.ingested`
（不存在的主题）、`source` 改成 `S99`（不存在的服务），跑 `python tools/check_all.py`：

```
exit code: 0
  ok    examples   contracts/examples/
  9 check(s) passed
```

改完之后**九项全过**——两个都改坏了，检查器一个都没发现。这条不是理论上的洞。

**普查现有 55 个信封示例**（`_tmp_probe5.py` / `_tmp_probe6.py`，跑完即删）：

```
envelope examples          = 55
type registered as topic   = 55 / 55
source shapes              = {'bare': 54, 'cells-uri': 1}
problems                   = 0

home_cell  {'cell_a1': 55}
time precision  no fractional seconds = 55,  with = 0
specversion / schema_version  {('1.0', '2.0'): 55}
```

`source` 的两种写法都要接受，因为**仓库里本来就两种都有**：54 个裸 `S28`，1 个
`/cells/cell_a1/services/S28`。后者不是笔误——`access.grant-changed.v2.schema.json` 行 216
的 `envelope_drift` 已经把它记成漂移实例。检查器不把一种归一化成另一种，只要求两种都能解析出
同一个 service_id。

**断言的三件事**（`envelope_identity_problems()`，加进 `validate_examples.py`）：

```
1. 带 type 的示例，type 必须在 event-topics.v2.json 里是一个 event_type
2. source 解析出的 service 必须在 service-catalog.v2.json 里，且等于该 topic 的 producer
3. 若 source 是 /cells/<cell>/services/... 写法，<cell> 必须等于 home_cell
```

第三条只有那 1 个示例会走到，但它正是「URI 写法与 `home_cell` 两处各写一遍」的必然产物。

**故意不做的两件事**，写在函数注释里：

- **不钉 `subject` 的形态。**普查出来 39 个示例的 `subject != aggregate_id`，且形态五花八门
  （`shot_3_2`、`series_demo/canon/ep003`、`manifest/manifest_ep3`、`training-job/job_01J...`）。
  仓库没有任何一份文档规定 `subject` 该长什么样，硬钉一种就是**新增猜测**。
- **不校验 `traceparent`。**它是 `00-<32hex>-<16hex>-<2hex>` 的 W3C 格式，但信封只收字符串。
  要做就得先决定「非 W3C 的值该拒还是该放」，那是 owner 的口径问题，不是我能定的。

12 条正负例（`_tmp_negtest2.py`）全部按预期：

```
  PASS  baseline bare source                   -> accepted
  PASS  baseline cell URI                      -> accepted
  PASS  type typo                              -> ["type 'artefact.ingested' is not an event_type ..."]
  PASS  source unknown service                 -> ['source names S99, which is not in service-catalog.v2.json']
  PASS  source wrong producer                  -> ["source names S14 but topic 'artifact.ingested' is produced by 'S28' ..."]
  PASS  source unparseable                     -> ["source 's28.artifact' is neither a catalog service_id nor ..."]
  PASS  source empty                           -> ['source is missing or empty']
  PASS  source absent                          -> ['source is missing or empty']
  PASS  cell disagrees                         -> ["source says cell 'cell_b2' but home_cell is 'cell_a1'"]
  PASS  payload-only doc (no type)             -> accepted
  PASS  payload-only doc (null type)           -> accepted
  PASS  type not a topic skips producer check  -> ["type 'nope.event' is not an event_type ..."]
ALL PASSED
```

最后一条是刻意的：`type` 不是 topic 时**不再报 producer 错**。否则一个类型名笔误会产生两条
错误，第二条（`producer` 对不上）把真正的病灶盖住。

端到端复验同一个洞：

```
exit code: 1
  FAIL artifact-ingested.json: 1 error(s), first at <type>: type 'artefact.ingested' is not an event_type ...
  FAIL  examples   1 problem(s)  [contracts/examples/]
```

**`contracts/README.md` 那三处「已知漏检」现在是假话，一并改了。**三处都拆成「schema 层仍不管 /
示例层已由 `envelope_identity_problems()` 管 / `traceparent` 仍无人管」三段，只保留仍然为真的部分。
另外 S47 行 1087 那处（讲 `aggregate_id` 的）也顺带改了措辞，指向 §16.8 的
`aggregate_identity_problems()`。

九项仍是全过，`examples` 一项输出不变（71 valid / 0 failing / 1 without a schema）。

### 16.10 `catalog` 那条绿只覆盖了一半，而且另一半从没跑过

§16.9 关掉的是示例层的两个洞。顺着「还有哪些检查是绿的但其实没跑」再找一次，
`check_catalog` 里有一处：

```
134        for c in t.get("consumers", []) or []:
135            if c not in known:
136                r.fail(f"topic {t.get('topic')}: consumer {c!r} is not a catalog service_id")
```

`_tmp_consumers.py` 的数：

```
topics                        = 55
topics with a 'consumers' key = 0
total consumer entries        = 0

all keys used by any topic    = ['data_schema_ref', 'delivery', 'event_type',
  'normal_retention_days', 'partition_key', 'pii_payload', 'producer',
  'replay_side_effects', 'schema_ref', 'topic']
```

55 个 topic 没有一个是带 `consumers` 键的，所以上面那个循环**从仓库存在至今 0 次进入**。
`git log -S'"consumers"' -- contracts/event-topics.v2.json` 也是空的——这个键从来没进过库。

问题不在那条循环本身，在它旁边那行绿：

```
ok    catalog    48 service(s), 55 topic(s)
```

`contracts/README.md` 把这一项写成「服务 id / topic 生产消费方 / `docs/02` 三方一致」。
**「生产消费方」是两件事，实际只查了生产方。** 这正好违反 `check_all.py` 文件头自己立的规矩
（行 13–15）：一个没跑成的检查不能读成绿灯。

**没有替 owner 补 `consumers`。** `policy.revision-published.v2.schema.json` 行 163 的
`fix_shape` 写着加这个键「需 owner 定」，加之前还要定每个 topic 的消费方是哪些服务——
那是一次全仓语义变更，不是校验器能自己填的值。

改的是**让绿说实话**：

```
ok    catalog    48 service(s), 55 topic(s)  [producer-only: no topic declares consumers]
```

三个用例验证（`_tmp_consumers_test.py`，改完还原原始字节，`restored: True`）：

| 注入 | 退出码 | `catalog` 那行 |
|---|---|---|
| 不注入（现状） | 0 | `48 service(s), 55 topic(s)  [producer-only: no topic declares consumers]` |
| `topics[0].consumers = ["S01", "S99"]` | 1 | `FAIL catalog 1 problem(s) [...]` / `topic drama.tenant-access.v2: consumer 'S99' is not a catalog service_id` |
| `topics[0].consumers = ["S01"]` | 0 | `48 service(s), 55 topic(s)`（不带 `producer-only`） |

第二行是要紧的：注入之前那条分支**从未执行**，注入之后它真的报错——说明不是循环写错了，
只是没有输入。第三行说明标只贴「一个 topic 都没声明」这个事实上，一旦有 topic 声明了它就消失。

同时把 `contracts/README.md` 那行从「topic 生产消费方」改成「topic `producer`」，并加一段
写清这条分支从没执行、谁来决定加 `consumers`。

九项仍是全过，`scaffold` 仍是 120 个生成物与 catalog 一致（这次没碰生成物）。

### 16.11 上一轮改 README 时把两份 schema 里的引用改成了过期行号

§16.10 改了 `contracts/README.md` 行 1477 那一句（「topic 生产消费方」→「topic `producer`」），
但仓里有两份 schema 按行号引用 README 的段落。改完没回头看它们。这一轮回源核对时发现两处已失效，
第三处（`contracts/README.md 行 206`）是对的，一并记下来免得再有人重查。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `policy.revision-published.v2.schema.json` / `why_it_matters` | 「…声称『服务 id / topic 生产消费方 / `docs/02` 三方一致』（**README L1445–1446**），实际只校验了 `producer`」 | 行 1445–1446 是 README 里 `KEYFRAME_MAX_FRAMES` 那段；被引的那句在**行 1477**，且已被 §16.10 改成「topic `producer`」。引用改为不带行号的「曾声称…」，并点明 README 行 1477 现有措辞 | 同 #2：`L1445–1446` 在 `ff99c4e^` 时就已经不对（那时真值也是 1477），不是这一轮改坏的——但这一轮改了被引的原文却没回头核行号，等于把一个旧错留在原处又叠了一层 |
| 2 | `continuity.check-completed.v2.schema.json` / `verdict` | 「与本仓库 S40 `status` 的处理同口径（**README 行 101–102**）」 | 行 101–102 是事件表里 `model.capability.changed` / `model.route.selected` 两行；讲 S40 `status` 收成 `enum` 的是**行 132–136** | 引的是表格行，不是散文行——与本报告 §16.1 #3 同一类：**表行与散文行在同一个行号空间里，不能凭印象给数字** |
| 3 | `policy.revision-published.v2.schema.json` / `dead_check` | 「`check_catalog`（**L134–136**）有：`for c in t.get("consumers", []) or []: ...`」，且同句称 `catalog` 报的是「48 service(s), 55 topic(s)」 | §16.10 那一改把分支挪到了 **L135–140**，形态也变了（`consumers = t.get(...)` / `if consumers:` 两层）；现在的 note 也不再是纯 `48 service(s), 55 topic(s)` | 同一个 PR 里改了代码又留了按行号的引用。**行号引用在同一个提交里就会烂**——要么写符号名（`check_catalog` 的 consumers 分支），要么改完就回头核 |

第 1 与第 2 条是**旧错**，不是这一轮引入的：`git show ff99c4e^:contracts/README.md` 里
「三方一致」也在 1477 行。第 3 条才是这一轮制造的。两者都该在同一张表里，因为对被引的人来说
区别没有意义——他照着行号去翻，翻到的都不是那句话。

顺带确认第三条**本来就是对的**：`contracts/README.md 行 206` 正是讲
`continuity.check.completed` 的 `aggregate_id` 是 `series:branch:episode` 的那一句，
`validate_examples.py:149` 与 §16.8 的表格里的引用都不用改。

**可以推广的一条**：改一份被按行号引用的文件之前，先

```
grep -rn "README L[0-9]\|README 行 [0-9]" contracts/ docs/ plans/ delivery/ tools/
```

十分钟前我刚错过一次。现在这条也进了这一轮的核对清单。

九项仍全过；`scaffold` 仍 120 个生成物一致（这次只改了两份 schema 的描述文字，未碰生成物）。

**这一节自己又犯了一次。**改 #1 的时候我写的是「README 行 1477 已改成…」——在同一节里
刚说完「行号引用在同一个提交里就会烂」，转头又给了一个裸行号。这一轮核验发现后改成不带行号的
「README 里 `check_all` 那一节已改成…」。**立完规矩当轮就用它扫一遍自己写的字**，不是下一轮。

现在全仓只剩一处按行号引 README 的：`continuity.check-completed.v2.schema.json` 的
`README 行 132–136`。它只改行号、不改被引内容就没事，所以留着；复查命令：

```
grep -rn "README 行 [0-9]\|README L[0-9]" contracts/ docs/ plans/ delivery/ tools/
```

预期只剩这一条（外加本报告里引它当证据的两行）。

### 16.12 `format: date-time` 不生效有两层原因，README 只写了一层

§16.11 那条教训练「立完规矩当轮就扫自己写的字」。这次照做了，扫到的不是行号，是**一句只说了
一半的解释**。

`contracts/README.md` 里讲 AEX-5 负向探针那一段说 `expires_at: "tomorrow"` 能过是因为
`validate_examples.py` 不传 `format_checker`。这话不假，但不完整。`FormatChecker()` 在
这台机器上注册的检查器是：

```
['date', 'email', 'idn-email', 'idn-hostname', 'ipv4', 'ipv6', 'regex', 'time', 'uuid']
```

**没有 `date-time`。**`jsonschema/_format.py` 把它包在 `with suppress(ImportError)` 里，
只有装了可选的 `rfc3339_validator` 才注册；本机没装。实测：

| 值 | 不传 `format_checker` | 传 `format_checker` |
|---|---|---|
| `2026-11-06T12:00:00Z` | 0 err | 0 err |
| `tomorrow` | 0 err | **0 err** ← |
| `2026-13-45T99:99:99Z` | 0 err | 0 err |

所以「在 `validate_examples.py` 里加一行 `format_checker=fc`」并不能把 `tomorrow` 拦住。
两层原因（调用方不传 / 依赖没装）同时成立，也同时会让那句解释失效。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` / AEX-5 负向探针那一段 | 「`format: date-time` 在本仓库只是注解，`validate_examples.py` …不传 `format_checker`」（把原因归给调用方一处） | 两层原因：调用方不传 **且** `jsonschema` 的 `date-time` 检查器依赖可选的 `rfc3339_validator`，未安装即未注册。只有 `date`/`time`/`email`/`uuid`/`regex`/`ipv4`/`ipv6`/`idn-*` 这些内置项会生效 | **把「我知道的那个原因」当成「全部原因」。**`format_checker` 是我在 `validate_examples.py` 里亲眼见过的字眼，而 `rfc3339_validator` 从没进过视线——查一次 `FormatChecker().checkers` 就能看见它不在列表里 |
| 2 | `contracts/audit.export-ready.v2.schema.json` / `expires_at` 的描述 | 同 #1 的一句话（「不传 `format_checker`，所以 `tomorrow` 也能过」） | 同上，补一句「即使传了也不管用」并点出 `suppress(ImportError)` | 同 #1，这处是那句解释的原始出处 |

**这次新写的数字也错了一个。**初稿写「分布在 71 个 schema 文件中的 22 个」，回源一数：

```
schema files total            = 71
format: date-time occurrences = 30
files declaring at least one  = 27
time-ish example strings      = 88
  valid RFC3339               = 88
```

**22 是从早先那份 30 条的打印列表里数的**——而那份列表按文件名排序、同一个文件出现多次，
我数的是「看上去不同的前缀行数」。真值 **27**。已改。这是本报告反复记录的那个模式：
**分母/归属数必须脚本算，眼睛数列表一定错。**

顺带确认：全仓 30 处 `format: date-time` 一行未改语义，88 个示例的时间值全是合法 RFC3339。
所以现在启不启用都看不出差别——**差别只在下一次有人写坏的时候**。要不要加依赖、或改用
`pattern` 硬约束，是 owner 决定（`pattern` 是硬校验，`format` 不是），这一轮不改。

### 16.13 `pattern` 是硬校验，所以它拒掉的东西是真拒掉——但有一半的 schema 根本没被加载

§16.12 的结论是 `format` 不算数、`pattern` 算数。顺着「算数的那一半有没有真在看」查下去，
发现算数的那一半**只覆盖了 64 个文件里的内容**，而仓里有 71 个 schema。

`tools/validate/validate_examples.py` 的 `local_registry()`：

```python
for p in glob.glob(os.path.join(CONTRACTS, "*.schema.json")):
```

`glob` 不带 `recursive=True`，所以只收顶层。实测：

```
top-level = 64  recursive = 71  never loaded = 7
    domain-v1\change-plan.v1.schema.json
    domain-v1\event-envelope.v1.schema.json
    domain-v1\generation-request.v1.schema.json
    domain-v1\qc-report.v1.schema.json
    domain-v1\shot-spec.v1.schema.json
    quality\check-registry.v2.schema.json
    quality\qc-report.v2.schema.json
```

而且这 7 个文件**没有被任何 `$ref` 拉进去过**（全仓指向 `quality/`、`domain-v1/`、
`qc-report`、`check-registry` 的 `$ref` 命中数 = **0**）。所以「没被当作根加载」不是
「被当作 `$ref` 目标加载了」——是两边都没有，这 7 个文件里的每一个 `pattern` 都从未执行过。

**第一个后果：`quality/qc-report.v2.schema.json` 里的两个 pattern 和仓里全部 4 个真实值矛盾。**

```
qc-report.v2 /properties/report_id                     ^qc_[a-z0-9]{26}$
qc-report.v2 /properties/supersedes/oneOf[0]/…/report_id  ^qc_[a-z0-9]{26}$
qc-report.v2 /$defs/defect/properties/defect_id        ^df_[a-z0-9]{26}$
```

示例里 `qc_` 出现 10 次、4 个不同值，**全部**是纯大写 ULID：

| 值 | 出现次数 | 主体长度 | 大写 | 小写 | 符合 pattern |
|---|---|---|---|---|---|
| `qc_01J9Z4Q2K7M3N8P5R1S6T0V4W2` | 3 | 26 | 12 | 0 | **否** |
| `qc_01J9Z5A3L8N4P9Q6S2T7U1W5X3` | 3 | 26 | 12 | 0 | **否** |
| `qc_01J9Z6B4M9P5Q1R7S3T8U2V6W4` | 3 | 26 | 12 | 0 | **否** |
| `qc_01J9Z7C5N1Q6R2S8T4U9V3W7X5` | 1 | 26 | 12 | 0 | **否** |

长度全对（26），大小写全反。把同一个值改成小写就能过：

```
'qc_01J9Z4Q2K7M3N8P5R1S6T0V4W2' -> REJECT
'qc_01j9z4q2k7m3n8p5r1s6t0v4w2' -> accept
```

而 `report_id` 自己的 `description` 写的是「**ULID 风格**」——ULID 的 Crockford base32
是**大写**。这就不是「示例写错了」一句话能盖过去的了：

| 来源 | 说的是 | 效果 |
|---|---|---|
| `plans/S35-technical-qc.md` §2.6 那张伪代码块里的 `base32_lower(...)` 一行 | `report_id = "qc_" + base32_lower(derived_key)[0:26]` | 小写 |
| `qc-report.v2.schema.json` `pattern` | `^qc_[a-z0-9]{26}$` | 小写 |
| `qc-report.v2.schema.json` `description` | 同一节点，写着「ULID 风格」 | 大写 |
| 示例（4 个不同值 / 10 处） | `qc_01J9Z...` | 大写 |
| 全仓其他 ULID 值（`evt_`/`cmd_`/`tnt_`/… 共 **425** 处、76 个前缀） | `_01J9...` | 大写 |

**同一个节点的 `pattern` 和 `description` 互相打架**，而且 `pattern` 那一侧有方案撑腰、
`description` 那一侧有全仓 435 处惯例撑腰。按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，
不要自行选边」，这一轮**不改任何一侧**。

**后果的后果：`qc-report.v2` 的 payload 从来没有任何实例被校验过。** 质量主题的
`data_schema_ref` 指向的是 `quality.technical-completed.v2.schema.json` 等**事件** schema，
那几处 `report_id` 是故意不收窄的，`description` 里明写理由：

> S35 只在示例里写成 `qc_…`，没有把前缀写成约束，故此处不收窄为 pattern——收窄会拒掉
> S35 自己没禁止的形式。

也就是说：**事件层故意放宽、报告 schema 收得比示例还紧、报告 schema 又从不加载。** 三层叠起来，
这个矛盾在九项检查里一次都不会显形。

`df_` 更彻底：`defect_id` 的 pattern 是 `^df_[a-z0-9]{26}$`，但**全仓 0 个示例含 `df_` 值**，
方案里也没有 `defect_id` 的派生式（全文只出现两次：`required` 列表里一个名字，
和一段 schema 片段里同一个 pattern）。所以它连「示例对不上」都谈不上——没有示例。

**第二个后果（顺带查到，与上面独立）：两份 schema 对 `check_id` 的段数约定不同。**

```
qc-report.v2      ^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+\.[A-Z0-9_]+$     <- 恰好三段
check-registry.v2 ^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$  <- 至少三段
check_all.py      ^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$  <- 至少三段（与 registry 一致）
```

实测分歧点：

```
TECH.A.B                     qc-report=ok      check-registry=ok
TECH.A.B.C                   qc-report=REJECT  check-registry=ok   <-- 分歧
VIS.SAFE_AREA.VIOLATION.SUB  qc-report=REJECT  check-registry=ok   <-- 分歧
```

也就是说：一个 `check_id` **能在 registry 里登记成功、也能被 `check_all` 的 `checkids`
放行，但写进 QC 报告时会被拒**。今天不炸，因为 99 个表行 check_id 里 96 个是三段、
3 个是两段（那 3 个是已知的 owner 决定项），**四段及以上的一个都没有**。所以这是
「还没有实例走到分歧里」，不是「分歧不存在」。

**做的事：把「7 个文件从没被加载」变成一条常驻提示，不改任何语义。**

改 `check_all.py` 的 `check_examples`，让它自己数一遍递归 glob 与顶层 glob 的差集：

```
ok    examples   contracts/examples/  [7 schema file(s) never loaded by the validator: 5 in domain-v1/, 2 in quality/]
```

反向验证：往 `contracts/quality/` 塞一个空 schema 文件，数字变 8；删掉，回到 7，
`git status --porcelain contracts/quality/` 为空。

**为什么只报告不发难：** 把 glob 改成 `recursive=True` 会让这 7 个文件里的 pattern 第一次真正执行。
那是语义变更——今天是绿的，改完可能立刻红（`qc-report.v2` 的 `^qc_[a-z0-9]{26}$` 对上 4 个示例值
就会红），而且红的根因是上表那个 pattern/description 冲突。**先修根因再扩覆盖**，顺序不能反。

| # | 位置 | 原状 | 现在 | 错因 |
|---|---|---|---|---|
| 1 | `tools/check_all.py` / `check_examples` | `examples` 那条绿写「71 example(s) valid」，看不出 7 个 schema 从未参与 | 追加 `[N schema file(s) never loaded by the validator: …]`，按目录计数 | **把「有文件」当成「有约束」。**`glob` 的 `recursive` 默认 `False`，我没查过默认值就假设子目录也在内 |
| 2 | 本节初稿：表格里「全仓其他 ULID 值 … 共 435 处」 | 435（含 `qc_` 自己那 10 处） | **425**，76 个前缀 | 想写「其他」却填了含自身的总数。`435 = 425 + 10`，差值正好是自己那一格 |
| 3 | 本节初稿：「只有行 504 的 required 列表和行 510 的 schema 片段」 | 两处裸行号 | 改成描述（`df_`/`defect_id` 在 `plans/S35` 全文只出现两次，各是什么） | §16.11 立的规矩是**当轮**扫自己写的字。这次扫到了自己刚写的裸行号 |
| 4 | 上一轮我这处对「孤立」的判断 | 曾以为「仓里没人 `$ref` qc-report」就等于「它完全孤立」 | 两件事都要说：既没被当根加载，也没被 `$ref` 拉进去。**结论一样，但证据链少一半** | 先有结论再找证据，找到一条能支撑的就停了；`$ref` 命中数 = 0 是这一轮才数的 |

**owner 决定（三条，都不自行选边）：**

1. `qc-report.v2` 的 `report_id` / `supersedes.report_id` / `defect_id` 到底是大写还是小写？
   - 选大写 → 改两个 `pattern` 为 `^qc_[A-Za-z0-9]{26}$`（或 `0-9A-HJKMNP-TV-Z` 的 Crockford 严格式），
     同时把 `description` 的「ULID 风格」写实；示例不动。
   - 选小写 → 改 4 个示例值（10 处），`base32_lower` 一侧不动；`description` 的「ULID 风格」要改词，
     否则它继续骗人。
2. `qc-report.v2` 与 `check-registry.v2` 的 `check_id` 段数取哪个？「恰好三段」还是「至少三段」？
   `check_all` 已经选了「至少三段」，所以统一到 QCR 是要改 QCR；统一到「恰好三段」则
   `check_all` 和 registry 都要改。
3. `local_registry()` 要不要改成 `recursive=True`？改之前得先定上面 1，否则 `examples` 会由绿转红。

九项仍全过（`examples` 那条现在带上面那个提示）。

### 16.14 `contracts/README.md` 里 `examples` 那格数从 63 起就没再跟过磁盘：63 → 66，实际 71

§16.13 关掉「有文件但没加载」之后，顺带核 README 九项摘要里每一格的数。**其余七格都对**
（148 / 110+55 / 99+3 / 1321 / 48+55 / 14+5 / 120；第九格 `registry` 报「还没有文件」，
本来就没有数可对），只有 `examples` 那格不对：

```
README 原写：66 个示例对 schema 校验，另 1 个刻意不映射
校验器实打：71 example(s) valid, 0 failing, 1 without a schema
磁盘上的 .json：72
```

`71 + 1 = 72`，闭合。**66 那格少算的 5 个，是这轮之前陆续加进来的示例**——每加一次
示例都改了 `SCHEMA_OF`，就是没人回头改 README 里那句数字。

README 那行的历史（脚本逐 commit 取 `git show`，不是我记的）：

```
7895bba  57
dae385e  58
218b4b3  59
5afb255  60
72aa6f0  61
758bf47  62
785b180  63
3aeeb22  66      <- 之后一直停在 66
a172d89  66
0cccb50  66
ff99c4e  66
b87f0e3  66
```

这正是本报告反复记录的那个模式，但换了张皮：前面几节都是「眼睛数列表数错」，
这次是**「数字曾经对过，之后上游变了它没跟」**。前者靠脚本重数能防，后者不能——
你重数的还是旧口径。要防它，得让这个数**没有地方可以单独存在**。

顺着这个想法把 `capacity-plan.json`（唯一无 schema 的那个）也验了一遍：`docs/03` 里讲
「Little 定律 / 每日生成秒数 / 存储」那几段散文明确说它「可调整后重新计算」，那就照那几段
的公式重算 `derived` 块：

```
field                                     declared            recomputed  ok
video_attempts_per_day                      900000                900000.0  ok
average_requests_per_second        10.416666666666666    10.416666666666666  ok
peak_requests_per_second            52.08333333333333      52.08333333333333  ok
average_inflight                                3125.0                3125.0  ok
peak_inflight                       15624.999999999998    15624.999999999998  ok
planned_inflight_with_margin                     20313                 20313  ok
video_generated_seconds_per_day              4500000              4500000.0  ok
video_terabytes_per_day_decimal                    4.5                   4.5  ok
video_retention_terabytes_decimal                135.0                 135.0  ok
accepted_minutes_per_day            33333.333333333336    33333.333333333336  ok
mismatches = 0
```

今天自洽。但**没有任何东西在跑这个重算**，所以明天有人把 `daily_shots` 改成 800000，
`derived` 会静静留着旧值，而 README 那句「71 个示例全部通过各自 schema 校验」照样是绿的
（`capacity-plan.json` 本来就不映射 schema）。这是同一个病：**一个可以单独存在的数字。**

**做了一件小事，让它不能再单独存在：** `check_examples` 现在把三个数并排打出来，
并把「磁盘上有但 `SCHEMA_OF` 里没有」当失败：

```
ok    examples   contracts/examples/  [7 schema file(s) never loaded …]  72 file(s) on disk, 64 stem(s), 64 mapped
```

三个数一摆，`71 + 1 = 72` 和 `64 stem = 64 mapped` 两个等式都能当场看出来是否成立；
`ok` 那格该填 71 也就有了出处。反向验证过：往 `contracts/examples/` 塞一个
`_probe-unmapped.json`，这行立刻变 `FAIL` 并指名 `['_probe-unmapped'] is on disk but
missing from SCHEMA_OF`；删掉恢复绿。

`72 个文件 / 64 个 stem` 会让人疑惑——9 个 `generation-command.*.json` 共享第一个点之前的
stem `generation-command`，所以它只是一个 schema 下的 9 个实例，不是 9 个没映射的孤儿。
这一点写进了 README 那段说明里，免得下次有人照 `stem` 数。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` 九项摘要里 `examples` 那格 | 「66 个示例对 schema 校验，另 1 个刻意不映射」 | **71** 个校验 + 1 个不映射 = 72 个文件 | **上游变了下游没跟。**数是从某次校验器输出里抄来的，抄完就再没回源；之后每加一个示例，这格就更错一格 |
| 2 | 本节初稿：「按 `docs/03` 第 38–40 行散文里的公式」 | 裸行号 | 改成描述（讲 Little 定律 / 每日生成秒数 / 存储的那几段） | 本节上一句刚在讲「数字不能单独存在」，下一句就用了一个会腐的坐标 |
| 3 | 本节初稿：「八格都对（148 / 110+55 / … / 120）」 | 八格，列了 7 组数 | **七格**都对，第九格 `registry` 报「还没有文件」、本来无值可对 | 把「九项检查」减「`examples` 那格」口算成 8，忘了 `registry` 那格没有数 |
| 4 | 本节初稿：我怀疑 9 个 `generation-command.*` 共享 stem 会导致「1 without a schema」被算小 | 按 `basename.split(".")[0]` 推演，9 个文件会塌成 1 个，`skipped` 应该是 0 而不是 1 | 塌缩确实发生，但**方向相反**：9 个文件都映射到同一个 `generation-command.v2.schema.json` 并各自校验通过，所以 `ok` 吸收 9 个；`skipped` 的 1 是 `capacity-plan.json`。`71 + 1 = 72` 本来就闭合 | 看到「9 个文件 1 个 stem」就先下了「会漏」的结论，没先算 `ok + skipped` 是否等于磁盘文件数。**先验闭合性，再解释构成** |

**owner 决定（新增一条）：**

4. `capacity-plan.json` 的 `derived` 块要不要做成常驻检查？它需要一个「公式的单一出处」——
   今天公式只在 `docs/03` 的散文里，脚本只能照着散文重写一遍，那样又造出第二个权威。
   要么在 `docs/03` 里写成可执行片段并被引用，要么接受它永远不校验（那就得在 README
   里明说「`capacity-plan.json` 的 `derived` 是某次人工计算的快照，不是实时值」）。
   这一轮两样都没做，只把重算结果贴在这里。

九项仍全过。

### 16.15 `action-families.v2.json` 自己的第 5 条 invariant，和另两份文件、和校验器代码都对不上

§16.14 核到一个事实：`check_families` 只覆盖 `action-families.v2.json` 自己声明的 4 条。
那第 5 条去哪了？顺着查下去，发现它不只是「没在我的检查里」，而是**跟同仓另外两份文件矛盾**。

文件 `invariants` 列表里原样写着（5 条，第 5 条）：

```
registry entries MUST NOT name an action outside families[defect_type].chain ∪ {fallback}.
The qc-sdk validator enforces this at load time; a violating registry is rejected, not logged.
```

**`chain ∪ {fallback}`** 是并集：登记项可以用链里的动作，**也可以**用该族的 fallback。

同一个族的另外两份文件说的是**只有链**：

| 出处 | 原文 | 允许集合 |
|---|---|---|
| `action-families.v2.json` 第 5 条 invariant | `outside families[t].chain ∪ {fallback}` | chain **∪ {fallback}** |
| `check-registry.v2.schema.json` → `$defs/escalation/properties/escalate_to/description` | `Must also be in families[defect_type].chain.` | chain |
| `check-registry.v2.schema.json` → `x-cross-checks.rules` 第 2/3/4 条 | `default_action ∈ …chain` / `allowed_actions ⊆ …chain` / `escalate_to ∈ …chain (when non-null)` | chain |
| `validate_registry.py`（`validate()` 里 `allowed_by_family = set(chain)`） | 三个字段都拿 `set(chain)` 判 | chain |

`x-cross-checks` 的 `why` 明说这些规则「enforced by the qc-sdk registry validator AT LOAD
TIME」——也就是那份文件自己认定 `validate_registry.py` 是这些规则的实现。数一下**有规范效力
的出处**（界定一个字段可以取哪些值的语句，不是引用它的行文）：说「只有 `chain`」的有
**5 条、分布在 2 份文件**（`escalate_to.description` 1 条、`x-cross-checks.rules` 第 2/3/4
条共 3 条、`validate_registry.py` 的 `allowed_by_family` 1 条）；说「`chain ∪ {fallback}`」的
只有 **1 条、1 份文件**。所以那份文件的第 5 条是孤证。

（口径说明：`grep 'chain ∪ {fallback}'` 在去掉 `docs/design-package/` 与 worktree 之后会命中
**8** 行，其中只有 `action-families.v2.json` 那一行是规范语句，另外 7 行都是本节和
`contracts/README.md` 里**引用**这条规则的说明文字。数「谁规定了什么」要按规范语句数，
不能按字符串出现次数数——否则我上一条更正本身就会被算成 7 票。）

按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，这一轮不改任何一份，
只把它测出来。

#### 冲突有多宽：24 个 (族, 字段) 对

并集和 `set(chain)` 只有在 `fallback ∈ chain` 或 `fallback == human_review 且 human_review ∈ chain`
时才相同。这条文件自己又有第 4 条 invariant 规定 `human_review MUST NOT appear in any chain`，
所以：

```
=== where the two rules agree (chain U {fallback} == chain) ===
  6 family/families: ['technical', 'identity', 'anatomy', 'hands', 'temporal', 'audio_mix']
=== where they contradict ===
  8 family/families: ['costume', 'prop', 'continuity', 'camera_adherence', 'action_adherence', 'subtitle', 'lipsync', 'voice']
  affected (family, field) pairs = 8 x 3 = 24
```

6 个族两说一致（它们的 fallback 是链成员，如 `technical` 的 fallback 是 `regenerate_video`）。
**8 个族的 fallback 正是 `human_review`**，而这 8 个族的链里都没有 `human_review`——
于是并集比 `set(chain)` 多出恰好一个值 `human_review`，三个字段各一处，共 24 对。

#### 实测：并集允许的登记项，校验器拒收

建一条 `prop` 族（chain `['regenerate_video','change_spec']`、fallback `human_review`）的登记项，
只改一个字段填 `human_review`。两次运行之间**只有 `escalate_to` 不同**，所以差出来的那条错误
只能是这个字段引起的（`_tmp_esc.py` 里的成对探针）：

```
=== what validate_registry.py says ===
  exit 1
  REJECTED: 1 problem(s) in 1 check(s)
    AUD.LIPSYNC.DRIFT: escalation_rule.escalate_to 'human_review' is outside the lipsync family

=== control: same entry, escalate_to = 'redo_lipsync' (a chain member) ===
  exit 0
  OK: 1 check(s) loaded, all within their action families
```

三个字段都试一遍（`_tmp_esc2.py`），并带一个什么都不填的对照：

```
field set to human_review  exit  verdict
default_action             1     REJECTED: 1 problem(s) in 1 check(s)
allowed_actions            1     VIS.COSTUME.UNSPECIFIED: allowed_actions contains 'human_review', outside the
escalation_rule            1     VIS.COSTUME.UNSPECIFIED: escalation_rule.escalate_to 'human_review' is outside
(none - control)           0     OK: 1 check(s) loaded, all within their action families
```

三条完整错误信息（`prop` 族，不截断）：

```
VIS.PROP.WRONG_OBJECT: default_action 'human_review' is not in the prop family chain ['regenerate_video', 'change_spec'] (fallback 'human_review' is a fallback, not a start point)
VIS.PROP.WRONG_OBJECT: allowed_actions contains 'human_review', outside the prop family ['change_spec', 'regenerate_video']. A registry entry may narrow the family, never widen it.
VIS.PROP.WRONG_OBJECT: escalation_rule.escalate_to 'human_review' is outside the prop family
```

第一条的括号里那句「fallback … is a fallback, not a start point」其实是**第 5 条应有的含义**：
fallback 不是起点。那它是不是也不该出现在 `allowed_actions` 里？两种读法都讲得通，而
**这正是不能自行选边的理由**——代码把一个语义判断（fallback = 终点，不是候选集成员）
实现成了与契约文字相反的东西，两边都有道理。

#### 不是新发现的重复，是没人跑的代码

`validate_registry.py` 里这段判断**从来没在真实数据上跑过**：`check-registry.v2.json`
不存在，`registry` 那格一直打印「no check-registry.v2.json yet」。上面那些 `exit 1`
是我手工喂进去的探针，不是仓库里的既有数据。也就是说，这个矛盾今天**没有任何生产影响**，
但它是下一次有人真的转录 99 条 `check_id` 时会撞上的第一堵墙——而且撞的时候会看到
`validate_registry.py` 报「outside the family」，指向的却是那份写着并集的契约文件。

#### 顺带修正我自己的一处过度声称

§16.14 和 `contracts/README.md` 都把 `families` 那格写成「动作族文件自己声明的 **5** 条
invariant」。实际是 **4** 条：第 5 条讲的是**登记项**，只能对着 `check-registry.v2.json` 判，
归 `registry` 那格，而那个文件不存在。已改：

- `contracts/README.md` 九项摘要 `families` 格：「5 条 invariant」→「5 条 invariant 里的 **4 条**」
- 同文件加一段说明第 5 条归 `registry`、今天一次都没执行过
- `check_families` docstring：「The five invariants」→「Four of the five invariants」，并写明
  第 5 条为什么不在这个检查里
- `check_families` 的输出行补一句覆盖范围：

```
ok    families   14 family/families  [4 of the 5 stated invariants; the one about registry entries is check_registry's]
```

这是 §16.10 / §16.13 / §16.14 同一个模式的第四次：**绿线读起来比它实际覆盖的多。**

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` 九项摘要 `families` 格 | 「动作族文件自己声明的 5 条 invariant」 | **4 条**（第 5 条归 `registry`，且从未执行） | 把「文件里声明了几条」当成「这个检查覆盖了几条」；两者差的那一条恰好是唯一没跑过的那条 |
| 2 | `tools/check_all.py` 里 `check_families` 的 docstring | 「The five invariants this file states about itself, checked literally」 | 「Four of the five…」，并说明第 5 条为何不属于这里 | 同一个混淆写进了代码注释，比写在 README 里更难被发现 |
| 3 | 本节初稿：把 `escalate_to = human_review` 的拒收直接当成「校验器的 bug」 | 打算改 `validate_registry.py` 使并集成立 | 如实报告为**三份文件 vs 一份文件**的冲突，不改任何一份 | AGENTS.md 明写契约与文档冲突要停；而且代码那一侧的括号理由（fallback 不是起点）本身站得住，我差点按「少数服从多数」选边 |
| 4 | 本节初稿：探针第一次跑出 `'registry_version' is a required property`，我差点把它读成字段相关 | 错误信息里混了 schema 的形状错误，和字段相关的拒收分不开 | 补上 `registry_version` 重跑，让两次运行的**唯一差异**是 `escalate_to` | 探针要能证明因果关系，就必须只留一个变量；带噪的对照不算对照 |
| 5 | 本节初稿：「四票对一票」 | 四票对一票 | **5 条语句、2 份文件** 对 **1 条语句、1 份文件** | 把「文件数」和「语句数」混成同一个计数；`x-cross-checks` 那 3 条规则是三条独立语句，塌成一份文件的一票会少算 |
| 6 | 本节初稿：「6 个族两说一致（它们的 fallback 是链成员）」 | 用「fallback 是链成员」解释这 6 个 | 直接对每个族算 `(set(chain) \| {fallback}) == set(chain)`，得 6 相等、8 不等；另验「不等时多出来的值恰好都是 `human_review`」= True | 条件写对了不等于等价关系成立过；这一句是在解释一个我还没直接数过的相等性 |
| 7 | 本节初稿：「说 `chain ∪ {fallback}` 的只有 1 条语句」没写口径 | 不带限定地写「1 条语句」 | 加上「有规范效力的」这一限定，并补上 `grep` 实得 8 行的口径说明 | 字符串出现次数 ≠ 规范语句数；本节和 README 引用这条规则的文字也会被 grep 到，不写口径就会被自己的引文重复计数 |

**owner 决定（新增三条）：**

5. `action-families.v2.json` 第 5 条要不要改成 `chain`（跟着另外三份文件走），还是反过来把
   `x-cross-checks` 规则 2/3/4 和 `validate_registry.py` 改成 `chain ∪ {fallback}`？
   改了会影响 S39 的路由候选集公式（`candidates = chain ∩ allowed_actions ∩ policy`），
   所以不是纯文字改动。这一轮没动。
6. 若采纳并集：`allowed_actions` 含 `human_review` 是否等于「这个检查允许自动放弃」？
   今天没有字段表达这个意思，`never_auto_repair`/`no_auto_reason` 是另一回事。
7. 若维持交集：那第 5 条应删掉并集写法，同时把「fallback 不是候选集成员」这条**语义**写进
   契约（现在它只活在错误信息的括号里），否则下一个人还会照第 5 条的原文理解。

九项仍全过（`families` 与 `registry` 两格的新措辞见上）。

### 16.16 加第十项检查 `maturity`：README 说「全是 design_only」，此前没有任何东西在核

前十五节做的都是「把已有的绿线读准」。这一节换一件事：**补一个此前完全没有的检查**，
因为那条陈述——README 开头「所有服务的 `maturity` 均为 `design_only`」——是可机械验证的，
却一直没人验证。

#### 先量清楚实际情况，不凭印象

```
service dirs: 48
non-gitkeep, non-generated files under services/: 0
.gitkeep files: 336
services with any extra file: 0
maturity values across services/*/service.yaml: {'design_only': 48}
```

48 个服务目录下，除 `service.yaml` / `service.md` 两个生成物之外**一个文件都没有**，336 个
`.gitkeep` 撑起 `src/ api/ events/ migrations/ tests/ deploy/ runbooks/` 七个子目录。
catalog 里 48 条也**全部**是 `design_only`。所以今天这两句话是**对的**——问题不在于它是错的，
而在于**它错的时候没有任何东西会响**。

这与 `docs/16` 的要求直接相关：E01–E40 全部是分布式行为验收（跨租户拒绝、重复投递幂等、
乱序事件、重启恢复……），**没有任何一条能靠静态文件检查通过**，该文件末尾自己也写着
「不能把设计schema合法等同于分布式行为已正确」。所以九项检查再全，也不可能让 E01–E40 变绿。
能做的只有一件：保证「还没实现」这件事本身别被说假。

#### 为什么这不是多余的检查

`maturity` 的值是 `scaffold` 从 catalog **照抄**进 `service.yaml` 的（`generate_services.py`
的 `maturity: %s` 那一行）。也就是说它写的是**编 catalog 的人当时的意图**，不是磁盘上的事实。
两个方向都会歪，且后果不同：

| 方向 | 现象 | 后果 |
|---|---|---|
| 代码先到，catalog 没跟 | `src/` 里有文件，`maturity` 仍是 `design_only` | 仓库看起来**比实际更没做**；没人会去跑它的测试，也没人知道它已经能跑 |
| catalog 先改，`src/` 还空 | `maturity=implemented`，`src/` 仍是 `.gitkeep` | **报假进度**。与 AGENTS.md「不能把 schema 校验通过当作分布式正确性证明」是同一种错，只低一层 |

所以两个方向都报 FAIL，不设「只报一个方向」的宽松处理——上面那个表格就是理由。

#### 检查做什么

`check_maturity`：对 catalog 每条服务，走 `services/<slug>/src/`，数**非 `.gitkeep`** 的文件。

- 有文件而 `maturity == design_only` → FAIL，提示「bump the catalog」
- 无文件而 `maturity != design_only` → FAIL，提示「the catalog is ahead of the tree」
- 另外单独核 README 那句话：全 48 条都是 `design_only` 时，README 必须仍写着那句；
  只要有一条不是，README 就不能再写「所有」。这一条是为了防「catalog 改了、README 忘了」，
  也就是 §16.14 那个「上游变了下游没跟」的同一个形状。

输出：

```
ok    maturity   48 service(s): 48 design_only; 0 with code in src/
```

#### 反向测试（跑完已还原）

```
=== 1. baseline ===
  exit 0
  ok    maturity   48 service(s): 48 design_only; 0 with code in src/

=== 2. code planted, catalog still design_only ===
  exit 1
  FAIL  maturity   1 problem(s)  [48 service(s): 48 design_only; 1 with code in src/]
  services/budget-ledger/src/ holds 1 file(s) but the catalog still says maturity=design_only; bump the catalog

=== 3. catalog says implemented, src/ empty ===
  exit 1
  FAIL  maturity   2 problem(s)  [48 service(s): 47 design_only, 1 implemented; 0 with code in src/]
  services/budget-ledger/src/ is empty but the catalog says maturity='implemented'; the catalog is ahead of the tree
  README.md still says every service is design_only, but 1 of 48 are not

=== 4. restore and confirm clean ===
  exit 0
  ok    maturity   48 service(s): 48 design_only; 0 with code in src/
  tree after restore: CLEAN
```

第 3 组一次报出**两条**，说明 README 那条独立于目录那条，不是同一个断言换句话。

#### 由此带来的两个变化

1. `tools/check_all.py` 从九项变**十项**（`--list` 也是 10 行，已核对）。
2. `contracts/README.md` 的「九项检查」改为「十项检查」，并补了 `maturity` 那一格及上面
   这个两方向表格。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` 检查总览 | 「九项检查」 | **十项** | 加了检查却没改数；这正是 §16.14 记的「上游变了下游没跟」，这次发生在同一个文件里、隔了不到十行 |
| 2 | 本节初稿：「防『catalog 改了、README 忘了』,」 | 句中出现半角逗号 `,` | 全角 `，` | 中英标点混用；同一个句子里其余逗号都是全角，只有这一个是从键盘直接敲出来的 |

**owner 决定（新增一条）：**

8. `maturity` 的取值域要不要收紧成枚举？`service-catalog.v2.json` **没有配 schema**
   （顶层只有 `schema_version` 与 `services` 两个键，`contracts/*catalog*.schema.json` 一个都
   不存在），所以 `maturity` 是自由字符串，没有任何东西限制它能取哪些值。检查只能比较
   「是不是 `design_only`」，无法判断 `implemented` / `beta` / `ga` 里哪个算「已经有代码」。
   这一轮没有替它定，只用了「非 `design_only` 即需要有代码」这个最小判据。

十项全过。

### 16.17 §16.13 的 owner 决定 #3 前提是错的：改 `recursive=True` 不会让 `examples` 转红

§16.13 把「7 个 schema 从未被加载」变成了常驻提示，然后把要不要修这件事留成决定 #3，
理由原文是「改之前得先定上面 1，**否则 `examples` 会由绿转红**」。这句话没验过。这一轮验了。

#### 一、`local_registry()` 的 docstring 是硬错

`tools/validate/validate_examples.py` 开头写着：

```
Only execution-context.v2.schema.json is referenced from other files today, but the
registry loads all nine so the next cross-file reference needs no change here.
```

「nine」是 `65791d8` 写下这句话时的规模，可那时仓里已经有 **16** 个 schema。现在的数字：

```
local_registry() glob contracts/*.schema.json -> 64 file(s)
docstring claims: 'the registry loads all nine'
```

六个字里两个数字都对不上，而且方向是**高估覆盖面**——它让下一个要加交叉引用的人以为
「注册表已经把该装的都装了，我直接写 `$ref` 就行」。实测这个假设会炸：

```
local_registry() can resolve:
  OK   execution-context.v2       -> $id https://drama.example.invalid/contracts/execution-context.v2.schema.json
  FAIL quality/qc-report.v2       -> NoSuchResource: 'https://drama.example.invalid/contracts/quality/qc-report.v2.schema.json'
  FAIL quality/check-registry.v2  -> NoSuchResource: 'https://drama.example.invalid/contracts/quality/check-registry.v2.schema.json'

what happens when an example's schema $refs a subdirectory schema:
  _WrappedReferencingError: Unresolvable: quality/qc-report.v2.schema.json
```

两句都要改：`execution-context` 那句（今天确实是唯一一个跨文件引用，实测 3 处 `$ref` 全指向它，
吻合）可以留；「loads all nine」必须删，改成它真实覆盖的范围。

#### 二、决定 #3 的前提不成立

§16.13 说改成 `recursive=True` 会让 `examples` 转红，依据是「`qc-report.v2` 的
`^qc_[a-z0-9]{26}$` 对上 4 个示例值就会红」。前半句对，后半句缺一个环节：**没有示例会被
`qc-report.v2` 校验**。三道门全部拦死：

1. `SCHEMA_OF` 的目标全是顶层文件（`event-envelope.v2` 55 次，其余 8 个各 1–2 次），
   没有一个是子目录里的；
2. `event-topics.v2.json` 的 `schema_ref` / `data_schema_ref` 里，指向子目录的 **0 个**；
3. 全仓 `$ref` 的跨文件目标只有 `execution-context.v2` 一个（3 处），没有任何 schema
   把 `qc-report.v2` 拉进来。

也就是说 `qc-report.v2` 装不装进 registry，与示例能不能过完全无关。直接跑两遍对照：

```
top-level glob (today): ok=71 fail=0 skipped=1
recursive  glob (proposed): ok=71 fail=0 skipped=1
```

**一模一样。** 改 `recursive=True` 的作用只是让那两个 `$id` 能被解析，它不会新增任何一条
校验——因为没有任何校验点在用它们。§16.13 担心的「先修根因再扩覆盖，顺序不能反」这件事
**在这个方向上不存在**。

那 `^qc_[a-z0-9]{26}$` 和 4 个示例值（10 处）的大小写冲突当然还是真的，它只是要在
`qc-report.v2` 真的开始校验之后才会咬人。今天咬不到。

```
all `qc_...` string literals under contracts/ (value position only):
contracts/examples/post-subtitle-ready.json         1  qc_01J9Z7C5N1Q6R2S8T4U9V3W7X5
contracts/examples/quality-alignment-completed.json 3  qc_01J9Z6B4M9P5Q1R7S3T8U2V6W4
contracts/examples/quality-semantic-completed.json  3  qc_01J9Z5A3L8N4P9Q6S2T7U1W5X3
contracts/examples/quality-technical-completed.json 3  qc_01J9Z4Q2K7M3N8P5R1S6T0V4W2
                                                     —— 4 个示例文件、10 处，全部大写
```

（`qc_trigger_requested` / `qc_report_ref` / `qc_decision` / `qc_layer` 是字段名不是 id 值，
按词边界排除；上面 10 处是真正的 id。）

#### 三、由此带来的变化

只改 docstring 那句错话。`recursive=True` 仍然不自己改——但决定的**理由**要换掉，
从「改了会红」换成「改了没用」。这仍然是一个 owner 决定：

9. `local_registry()` 要不要改成 `recursive=True`？**实测表明它不会让任何检查变红也不会变绿**
   ——今天没有任何校验点引用子目录 schema。所以这不是「现在改不改」的问题，而是
   「要不要让 64 → 71 的覆盖面在**将来**第一次被引用时自动生效」。选不加：下一个写
   `$ref` 到子目录的人会遇到 `Unresolvable`，报错很直白，改一行就能修；选加：覆盖面
   悄悄扩大到 7 个从未执行过的 schema，而它们的 `pattern` 里还有已知冲突（见决定 #1）。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `tools/validate/validate_examples.py` 模块 docstring | "the registry loads all nine" | 实际 64 个；改成如实描述覆盖面 | 写下这句话时仓里已有 16 个 schema，「nine」从一开始就不对，之后只涨不修 |
| 2 | §16.13 owner 决定 #3 的理由 | 「否则 `examples` 会由绿转红」 | 实测 **不会**：71 ok / 0 fail，与今天逐条一致 | **把「文件没被加载」当成「一加载就会校验它」。** 装载（registry 里有）和校验（有校验点引用它）是两件事，中间还差一个 `$ref` 或一条映射；我把前者当成了后者，于是一个不会发生的后果被写成了不敢改的理由 |
| 3 | §16.13 owner 决定 #3 的括号 | 「对上 4 个示例值就会红」 | 前半句对（4 个示例、10 处确实超大写字面），但缺「且它们会被 qc-report.v2 校验」这个前提 | 同 #2；数字对、结论跳步 |

十项仍全过。

### 16.18 `registry` 那格什么都没跑，却和其他九格一起算进「十项全过」

§16.16 加了第十项检查后，我一路写「十项全过」。这一轮重读输出才发现第十项是这么打印的：

```
ok    registry   no check-registry.v2.json yet; nothing to validate
...
10 check(s) passed
```

`ok` 加上「计进 passed」，而那一格**一个断言都没执行**。`tools/check_all.py` 自己的模块
docstring 第 13–15 行写的就是反面：

```
Exit 0 = every check passed. Exit 1 = at least one failed. Exit 2 = a check
could not run (missing dependency, missing file), distinct from "found a
problem", because a check that did not run must never read as green.
```

这是同一条原则的第三种形态，前两种都处理了：跑挂了 → `ERROR` + exit 2，
发现问题 → `FAIL` + exit 1。**没有东西可跑**这一种没处理，默认落进了 `else` 分支的 `ok`。

#### 为什么这不是措辞问题

`contracts/README.md` 同一节里有一段专门讲这件事，讲的是 `families`：

> 那第 5 条…归 `registry` 那格；而那个数据文件至今不存在，`registry` 只打印
> 「no check-registry.v2.json yet」。也就是说**第 5 条今天一次都没执行过**，
> 写「5 条 invariant」会让这一格读起来比实际覆盖得更多。

README 用一整段说明「别把没执行的算成覆盖」，而它上面十行的摘要行正把没执行的那格
算成通过。同一个文件，隔了不到二十行。

#### 改法与负向测试

`Result` 新增 `skipped` 标志和一个 `skip(why)` 方法；`check_registry` 在没有数据文件时
调 `skip`；`main()` 分开统计并单独打印。现在的结尾是：

```
skip  registry   no check-registry.v2.json yet; nothing to validate

9 check(s) passed, 1 skipped (registry): nothing was verified there
```

退出码仍是 **0**——仓库没坏，这是「没东西可跑」，不是「没跑成」（后者是 exit 2）。
三态区分保持：0 = 没发现问题，1 = 有问题，2 = 某项没跑成。

负向测试（写了临时 registry 文件、跑 `--only registry`、删掉）：

```
--- A. no data file ---
    skip  registry   no check-registry.v2.json yet; nothing to validate
    0 check(s) passed, 1 skipped (registry): nothing was verified there

--- B. one conforming entry ---
    ok    registry   contracts/quality/check-registry.v2.json
    1 check(s) passed                  ← skip 转成了真 ok，不再是 skip

--- D. allowed_actions 里塞 human_review ---
    FAIL  registry   1 problem(s)
            registry rejected at load time
    1 problem(s)                       ← 真错误仍然是 FAIL，没被 skip 吞掉
```

B 关键：文件一存在，`skip` 就变成会计数的 `ok`，说明这个标志是**状态**不是**豁免**。

（探针自己前几版不合法，被 schema 拒掉：`check_id` 写成 `TECH.PROBE_OK`，而 pattern
`^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$` 要求**至少三段**（`TECH.A` 不匹配，
`TECH.A.B.C` 匹配）；`window_scope` 写成了对象，schema 里它是 `type: string` 加 5 个枚举
（`frame` / `window` / `shot` / `track` / `all`）；`window_max_frames` 不是无条件的 required
字段，而是一条**条件**约束——`allOf` 里 `if window_scope ∈ {frame, window} then required
["window_max_frames"]`，我第一版把 `window_scope` 给了 `"window"` 却没给这个字段，才被拒。
改对之后才有上面 B。这几次失败恰好说明 schema 层在真干活。）

两处数字是我写错的，见下方更正表第 3、4 行。

#### 由此带来的变化

`contracts/README.md` 三处：摘要行「十项检查」那段补了 `9 passed / 1 skipped` 的说明、
`families` 那段的「只打印 no check-registry.v2.json yet」改成「打的是 `skip` 不是 `ok`」、
末段「报还没有文件而不是失败」改成「报 `skip`（不是失败，也不是通过）」。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | §16.16 结尾、§16.17 结尾 | 「十项全过」 | **9 过 1 跳过**：`registry` 没有数据文件，一格都没校验 | **把「这个检查没报错」当成「这个检查通过了」。**`check_all` 的三态里原本只区分了「失败」和「没跑成」，`main()` 的 `else` 分支把「没东西可跑」也收进了 `ok`；我读的是那行绿字，没看它在检查什么 |
| 2 | `tools/check_all.py` / `check_registry` docstring | 「reports "not present" rather than failing」 | 保留这半句，并说明它现在通过 `skip()` 表达、不计入 passed | 原 docstring 描述的是行为，但没提这个行为会让摘要行多算一票 |
| 3 | 本节的探针注「`window_scope` … schema 里是 7 个枚举字符串之一」 | 7 | **5**（`frame` / `window` / `shot` / `track` / `all`） | 把同一份 schema 里 `recheck_scope` 的 7 个枚举数串到了 `window_scope` 头上。两个字段相邻、都是 `type: string` + `enum`，我数了后者的长度写给前者 |
| 4 | 本节的探针注「`window_max_frames` 是同级另一个 required 字段」 | 无条件 required | **条件** required：`allOf` 里 `if window_scope ∈ {frame, window} then required ["window_max_frames"]` | 看到 `required` 三个字就当成无条件。它出现在 `then` 分支里，条件没写进句子；这也解释了为什么我第一版给 `"window"` 时被拒、给 `"shot"` 时其实不会被拒 |

十项里 9 项真跑过、1 项没有。
### 16.19 把「README 里那几个数」变成一项检查——它当场抓到 `scaffold` 多算了 2 个

§16.16 起 `contracts/README.md` 的检查摘要段就是手写的：十行以外的一段话，逐项报出
每个检查看了多少东西。它长得像输出，读起来像输出，但**没有任何东西在核它**。§16.17
已经证明这类数字会歪——里面 `examples` 那格从 53 漂到 63 再到 66，而目录里已经有 72 个文件。

这一轮把它变成第十一项检查 `readme`。

#### 它核什么

两件事，来源不同：

1. **开头的「N 项检查」** —— 和 `len(CHECKS)` 比。这个数在加检查的那一刻就会过期，
   我第一版把锚点写成 `readme.find("十项检查：")`，改 README 加这一格的时候
   **把自己写的检查弄挂了**。所以它不 pin 任何字面量，直接读注册表。
2. **段落里 9 个手敲的数字** —— 逐条和产生它的那个检查的 `note` 比：

| 数字 | README 里的话 | 由哪个检查算 |
|---|---|---|
| 148 | `json`（148 个 JSON 解析） | `json` |
| 110 | `payloads`（110 条 `schema_ref`…） | `payloads` |
| 55 | 其中 55 个 topic 带 payload schema | `payloads` |
| 99 | 四份 QC 方案里 99 个 `check_id` | `checkids` |
| 1321 | `links`（1321 条相对链接全部可达） | `links` |
| 5 | 动作族文件自己声明的 5 条 invariant | `families` |
| 118 | `scaffold`（118 个生成物与 catalog 一致） | `scaffold` |
| 71 | `examples`（**71** 个示例对 schema 校验） | `examples` |
| 1 | 另 1 个刻意不映射 | `examples` |

锚点也核：段落起点是从节标题 `## 一个入口跑完所有仓库级检查` 往下找 `N 项检查：`，
找不到就报「这一节的摘要段不见了」，而不是去全文里捞一个碰巧匹配的数——
§16.17 那个「数串到隔壁字段头上」的错就是全文乱捞的产物。

#### 当场抓到的那个

第一次跑通之后，我去回源表重数（cron 回合的「数值：任意新写入的统计数字，回到源表重新数」），
自己从文件数出来是：

```
json            148   (files ending .json under contracts/: 148)
schema_ref      110
payload topic   55
check_id        99
stated invariant 5  (families: 14)
scaffold        70  (svc 48, agent 13, conn 9)
examples files  72
```

`scaffold` 我数出 **70**，检查器报 **120**，README 也写着 120。三边不一致，于是去读
`check_scaffold`：

```python
    # Compare every rendered file against the working tree.
    compared = 0
    for p in sorted(glob.glob(os.path.join(tmp, "**", "*"), recursive=True)):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, tmp)
        tree = os.path.join(ROOT, rel)
        compared += 1
```

`tmp` 是临时根，函数自己往里拷了两个输入文件：

```python
    os.makedirs(os.path.join(tmp, "contracts"), exist_ok=True)
    for f in ("service-catalog.v2.json", "event-topics.v2.json"):
```

这两个文件被下面的 glob 一起数进了 `compared`。真实生成物是：

```
  48  services/*/service.yaml
  48  services/*/service.md
  13  agents/*/agent.yaml
   0  agents/*/agent.md
   9  connectors/*/*/connector.yaml
   0  connectors/*/*/connector.md
```

48 + 48 + 13 + 9 = **118**。120 − 118 = 2，正好是那两个输入。

我头一遍直数出 70 也是错的：只数了三种 `yaml`，漏了 48 个 `service.md`。
两次错法不一样——检查器把输入当输出，我把一种输出整个漏掉——但方向相同：
**都在数「我以为它在数什么」，而不是去读它实际遍历的路径。**

#### 改法与负向测试

`check_scaffold` 的循环里跳过 `contracts/` 前缀；`r.note` 从 120 改成真数。
改完立刻被自己刚写的检查抓到：

```
FAIL  readme     1 problem(s)  [9 count(s) in contracts/README.md's summary]
        README says 120 for scaffold, scaffold says 118
```

改 README 为 118 之后转绿。这就是这一格该有的行为：**检查器改了，README 必须跟着改，
不跟着就红。**

十向负向测试（9 个数字各 +1/−1，再加开头「十一」改「十」）：

```
json 148->149              CAUGHT  ['README says 149 for JSON 解析, json says 148']
payload refs 110->109      CAUGHT  ['README says 109 for schema_ref, payloads says 110']
payload topic 55->54       CAUGHT  ['README says 54 for payload topic, payloads says 55']
check_id 99->100           CAUGHT  ['README says 100 for check_id, checkids says 99']
links 1321->1320           CAUGHT  ['README says 1320 for links, links says 1321']
invariant 5->6             CAUGHT  ['README says 6 for stated invariant, families says 5']
scaffold 118->119          CAUGHT  ['README says 119 for scaffold, scaffold says 118']
examples 71->70            CAUGHT  ['README says 70 for example, examples says 71']
unmapped 1->2              CAUGHT  ['README says 2 for unmapped example, examples says 1']
opener 十一->十               CAUGHT  ['README says 十项检查, but 11 checks are registered; --list is the authority']
restored identical: True
```

另外两种锚点失效：

```
[opener dropped]         ["contracts/README.md's check section no longer opens with
                          'N 项检查：'; this check cannot find the summary paragraph"]
[section heading gone]   ["contracts/README.md no longer has the
                          '一个入口跑完所有仓库级检查' section"]
```

#### 刻意不 pin 的

- **`families` 的「5 条里 4 条」那个 4**：4 of 5 是语义判断（第 5 条归 `registry`），
  不是计数结果。5 是计数，pin；4 不是。
- **`checkids` 的不合规条数**：白名单本来就该缩短，写死等于要求它别缩短。
- **passed/skipped 的拆分**：数据文件一落地就从 `10 passed, 1 skipped` 变成 11 passed，
  写死会让落地那一刻红，而那是**预期内的**变化。
- **`catalog` 那格的服务数**：README 这一节里根本没写服务数（写的是「服务 id / topic
  `producer` / `docs/02` 三方一致」）。我第一版硬加了一条 `catalog` 的 claim，
  正则里连捕获组都没有，跑起来直接 `IndexError: no such group`——
  那不是写错一个数，是给一段话编了一个它没说的话。

#### 由此带来的变化

- `tools/check_all.py`：新增 `README_CLAIMS`、`_cn_numeral()`、`check_readme_counts()`，
  注册成第十一项，排在 `maturity` 与 `registry` 之间（`registry` 要最后，因为它可能 skip）。
- `tools/check_all.py` `check_scaffold`：`compared` 跳过 `contracts/` 前缀。
- `contracts/README.md`：标题数改成「十一项检查」，摘要段补 `readme` 一格，
  另起一段说明这一格核什么、反向测过什么、哪些不 pin，`scaffold` 120 → 118。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` 摘要行，以及 §16.16–§16.18 各节结尾 | `scaffold` 120 个生成物 | **118**：`check_scaffold` 把自己拷进临时根的 2 个输入文件（`service-catalog.v2.json`、`event-topics.v2.json`）也算成了生成物 | **把「glob 到了」当成「生成出来了」。**循环遍历的是临时根下的所有文件，输入和输出混在同一个树里；我没区分，README 就跟着抄了一个多 2 的数 |
| 2 | 本节的直数脚本 | `scaffold 70`（svc 48 + agent 13 + conn 9） | **118**，漏了 48 个 `service.md` | 只数了 `*.yaml` 三种，`generate_services.py` 同时产出 `service.yaml` 和 `service.md`；两次数错方向不同，但都是没读生成器实际写哪些文件 |
| 3 | 本节初稿：`readme` 这一格的锚点 | `readme.find("十项检查：")` | 不 pin 字面量，从节标题往下找 `N 项检查：` 再和 `len(CHECKS)` 比 | **把「现在的数字」写成了「结构」。**加第十一项检查时我改 README 为「十一项」，锚点还写着「十项」，于是这一格把自己弄挂了 |
| 4 | 本节初稿：`README_CLAIMS` 第一条 | 我加了 `("catalog", r"服务 id.", ...)` 之类一条 `catalog` claim | 删掉——README 这一段没有服务数 | 凭印象以为这一节写了服务数。跑起来 `IndexError: no such group` 才回去读原文，发现那半句讲的是三方一致性，不是计数 |
| 5 | 本节初稿：`family-invariant` 只说「4 of 5 不 pin」 | 只说 4 不 pin，没说 5 该 pin | 5 是计数、pin；4 是语义判断、不 pin；两者在同一句话里 | 把「这半句是判断」扩大成「整句都是判断」，连带放过了一个真能核的数 |

十项里 9 项真跑过、1 项没有——现在是十一项里 10 项真跑过、1 项没有。

### 16.20 布局表写「48 个 topic」，而同一份文件另外 11 处写 55

§16.19 把检查摘要段那 9 个数钉住了，但同一份 README 开头那张「布局」表没被覆盖——
它不是摘要段的一部分，也不是任何检查的 `note` 产生的，所以`readme` 这一格当时看不见它。

#### 错在哪

`contracts/README.md` 第 15 行：

```
  event-topics.v2.json         48 个 topic，注册 55 个 event_type
```

改正前，同一个文件里「55 个 topic」出现在第 44、57、58、61、437、787、863、1487、1543
共 **9 行**，而写「48 个 topic」的只有第 15 行这一处。
（改正后全文 11 处，多出的 2 处是本节的说明文字和检查器报错原文里的引用，不算正文。）
`event-topics.v2.json` 里是 **55** 个 topic：

```
services            48
topics              55
topic.event_type    55   distinct=55
openapi operations  21  paths=21
mcp tools           12  (key=tools)
```

48 是**服务**数，55 是 **topic** 数。第 14 行正好是「service-catalog.v2.json 48 服务元数据」，
下一行就把 48 抄了过去——**相邻两行，上一行的主语换成了下一行的宾语。**

这和 §16.18 的更正表第 3 行是同一个形状（把隔壁字段的 7 个枚举数串到 `window_scope` 头上），
也和 §16.19 的 `scaffold` 120 同源：**数字在文件里出现过一次，就被当成可以复用的常量。**

#### 改法与覆盖

布局块那 5 个数（48 服务 / 55 topic / 55 event_type / 12 MCP 工具 / 21 个 OpenAPI 操作）
现在纳入 `check_readme_counts`。它们不来自任何检查的 `note`，所以直接读源文件：
`service-catalog.v2.json`、`event-topics.v2.json`、`mcp-tools.v2.design.json`、
`openapi.v2.yaml`（后者用 `^\s{4}(get|post|...):` 数路径下的操作键）。

反向测过六个方向：

```
original bug: 55 topics -> 48    CAUGHT  ['README layout block says 48 for topics, the source file has 55']
services 48 -> 47                CAUGHT  ['README layout block says 47 for services, the source file has 48']
event_type 55 -> 54              CAUGHT  ['README layout block says 54 for event_type, the source file has 55']
mcp 12 -> 11                     CAUGHT  ['README layout block says 11 for mcp tools, the source file has 12']
openapi 21 -> 20                 CAUGHT  ['README layout block says 20 for openapi operations, the source file has 21']
block heading renamed            CAUGHT  ["contracts/README.md no longer has the '## 布局' block"]
restored identical: True
```

第一条就是把这个错原样放回去，它被抓到了——这是这一格存在的理由。

#### 由此带来的变化

- `contracts/README.md` 第 15 行：`48 个 topic` → `55 个 topic`。
- `tools/check_all.py` `check_readme_counts`：新增布局块 5 个数的校验，`note` 变成
  `9 count(s) in contracts/README.md's summary; layout block 5 count(s)`。
- `contracts/README.md` 说明段补了布局块这一部分以及它当场抓到的那个错。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | `contracts/README.md` 第 15 行（布局表） | `event-topics.v2.json 48 个 topic，注册 55 个 event_type` | **55 个 topic** | **把上一行的数抄进了下一行。**第 14 行是「48 服务元数据」，第 15 行开头换成 topic，48 却留了下来；同一文件其余 9 处都写 55 |
| 2 | §16.19 的说明段 | 「它做两件事：一是…二是…」 | 三件事，加了布局块 | 写那一格时只盯着摘要段，没往文件开头看；README 里同一类手敲数字的另一处就在第 15 行 |
| 3 | 本节初稿 `check_readme_counts` 的 `r.note` | 先 `+=` 再被 `r.note =` 覆盖，布局那半句永远不显示 | 用一个变量接住，最后一次性拼 | 复制粘贴了 `check_examples` 的 `+=` 写法，但这个函数的 `r.note` 是在末尾赋值的，顺序反了 |

全量仍是 10 项通过、1 项 skip（`registry`），exit 0。

---

### 16.21 把「正文引用的 `check_id` 必须在注册表里存在」也变成一项检查——它当场抓到第三处

§16.19 和 §16.20 钉的是「文件里手敲的数字」。这一节钉的是**名字**：一份计划正文里
引用的 `check_id`，有没有登记在同一服务 §5 那张表里。

#### 错在哪

`check_check_ids` 原来只做一件事：把四份 QC 计划表格行里的 id 拿去匹配
`^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$`。**它只看形状，不看有没有登记。**
形状合法的瞎编 id 一样过。

于是这类引用一直是盲区——它们在正文里，不在表里，形状也对，所以既没进检查，也不影响
任何一格的 `note`：

```
('S35-technical-qc', 1056, 'SUB.CUE.VERSION')          <- 新发现，S38 连 CUE 组都没有
('S38-subtitles',     241, 'SUB.TIMING.CPS')           <- 两段缩写，见下
('S38-subtitles',     278, 'SUB.LAYOUT.CPS_EXCEEDED')  <- S38 §4.3 第 3 步
```

第三条是 `check-registry-missing-fields-2026-11-05.md` §5.2 已经记过的老问题
（S38:278 的 `SUB.LAYOUT.` vs 注册表 §5.1 的 `SUB.TIMING.CPS_EXCEEDED`）。
**第一条是这一轮新发现的**，之前没人记过：S35 第 1056 行写「把 `SUB.CUE.VERSION` 的
`default_action` 写成 `regenerate_video`」，用这个 id 当校验器的实测样例，而 S38 的
注册表里 24 条 id 中一个 `CUE` 组的都没有。

#### 口径：只认完整 id

`VIS.SAFE_AREA`（S35:65）和 `SUB.TIMING.CPS`（S38:241）也是悬空的，但它们**不在这项检查
的范围里**，有意为之：

- `VIS.SAFE_AREA` 是**组引用**——S36 有 `VIS.SAFE_AREA.TEXT_OVERLAP` 等。写组名是在说
  「这一类里某一个」，不是引用某条 id。
- `SUB.TIMING.CPS` 是**正文缩写**——同一句话后半截就点了 `SUB.TIMING.OVERLAP`。

schema 要求 id 至少三段（`PREFIX.<GROUP>.<NAME>`），所以只有 **3 段及以上**的反引号提及
才算「声称有一条 id」。按这个口径量：

```
registered per prefix: {'TECH': 23, 'VIS': 32, 'AUD': 20, 'SUB': 24}
3+-segment id mentions across the four plans: 146
dangling: 3
```

146 处完整引用里 3 处悬空，漏网面很小，而且是已知的两处加新发现的一处。

#### 改法：沿用同一份文件既有的 allowlist 模式

`KNOWN_BAD_CHECK_IDS` 已经是这个形状——把 owner 待决项列出来，让套件对**其他所有**东西
保持绿，同时用一条陈化测试逼它在修好之后消失。新加的 `KNOWN_DANGLING_REFS` 照抄：

```python
for i in sorted(KNOWN_DANGLING_REFS - dangling_seen):
    r.fail(f"{i} is in KNOWN_DANGLING_REFS but no plan cites it unregistered "
           f"any more; remove the entry")
```

**这不是把冲突判给任何一方。**三处正文一个字没改；改的是「一旦有人改了，套件必须提醒」。
按 AGENTS.md「契约与文档冲突 → 停下，报告冲突，不要自行选边」，S38:278 是改 §4.3 的文案
还是给 §5.1 加一行，由 S38 的 owner 定；S35:1056 是换成真 id 还是给 S38 补 `CUE` 组，
由 S35 / S38 的 owner 定。这一节只保证：**这个悬而未决的状态不会再因为没人记得而静默。**
无论 owner 选哪边，陈化测试都会抓到并提示删掉对应条目。

#### 反向测试

六个方向，包括把 owner 可能选的每一种修法都试出来：

```
baseline                                   MISSED  []
unknown id disclosed (no allowlist)        CAUGHT  ['plans/S38-subtitles.md cites SUB.LAYOUT.CPS_OVERFLOW, but plans/S38-subtitles.md does not register it', 'SUB.LAYOUT.CPS_EXCEEDED is in KNOWN_DANGLING_REFS but no plan cites it unregistered any more; remove the entry']
§4 fixed to the registered name            CAUGHT  ['SUB.LAYOUT.CPS_EXCEEDED is in KNOWN_DANGLING_REFS but no plan cites it unregistered any more; remove the entry']
registry gains the LAYOUT row              CAUGHT  ['SUB.LAYOUT.CPS_EXCEEDED is in KNOWN_DANGLING_REFS but no plan cites it unregistered any more; remove the entry']
S35 citation points at a real id           CAUGHT  ['SUB.CUE.VERSION is in KNOWN_DANGLING_REFS but no plan cites it unregistered any more; remove the entry']
registered id misspelled in prose          CAUGHT  ['plans/S38-subtitles.md cites SUB.TIMING.OVERLAPS, but plans/S38-subtitles.md does not register it']
restored identical: True
```

第 2、3、4 条很重要：**owner 按两种修法里任何一种修好，套件都会要求删掉那条 allowlist。**
如果不删，条目就是陈的，将来同一个 id 再被误引用就抓不到了。

#### 同时也量了两格的覆盖面

同一轮里核实了另外两格是不是也「看着绿、其实没看」：

- `links`：1041 个 `.md`、3903 条行内链接里，带 `#` 片段 0 条、图片 0 条、
  reference 式定义 0 条、HTML `<a href>` 0 条。`split("#")[0]` 和「只认 `](...)`」这两种
  写法今天确实没藏住任何东西——但这个结论是**量出来的**，不是假定的。
- `maturity`：48 个服务在 `src/` 之外共有 384 个文件，全是生成的 `service.md` /
  `service.yaml` 和空 `.gitkeep`，**真实代码文件 0 个**。所以「`src/` 之外没有代码要看」
  这个前提成立，该格的口径够用。

#### 一次自我更正

本轮中间记录里我写过「99 行 id 对 109 处提及，多出的 10 处是跨服务交叉引用，所以按表格行
计数是对的」。重数后是 **99 对 101，只多 2 处**，而且两处都在 S38、正是上面那个 CPS 命名
问题本身——没有任何跨服务交叉引用。错误的 109 / 10 没有写进任何文件（写本节时才发现），
但结论「按表格行计数是对的」恰好仍然成立，理由则完全不同：正文提及按定义就不是 id 声明，
这一轮干脆把它们单独做成了一项检查。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | 中间记录（未落盘） | 99 表格行 id 对 109 处提及，多出 10 处是跨服务交叉引用 | **99 对 101，多 2 处，都在 S38，是 CPS 命名冲突本身** | **凭上一轮的印象报数，没重数。**而且「多出的是跨服务交叉引用」本身就是错的：那 2 处不在任何表格里，是正文提及（S38:278 的 §4 步骤、S38:241 的缩写），跟「哪个服务引用谁」无关 |
| 2 | `check_check_ids` 的覆盖面 | 只校验 id 形状，`note` 只报「N 个 check_id」 | 加第二项：正文 3+ 段 id 引用必须在所属前缀的注册表里 | **把「形状对」当成「存在」。**同一个函数里做两件事最省事，但一字不提地只做一半，读起来像全做了 |
| 3 | §16.20 的 `checkids` 口径描述 | 未说明表格行与正文提及的区别 | 正文提及按 3+ 段单独成项，两段是组引用/缩写 | 写 §16.20 时还没意识到正文提及是另一类问题，所以只描述了表格行 |
| 4 | 本节更正表第 1 行初稿 | 「它们各自只引用本前缀的 id，不存在跨服务交叉引用」 | 删掉这句，改为说明那 2 处是正文提及 | **同一次凭印象。**S35:65 引 `VIS.SAFE_AREA`、S35:1056 引 `SUB.CUE.VERSION`，跨前缀引用是有的；只是它们要么是组引用、要么本来就在 allowlist 里 |

全量：10 项通过、1 项 skip（`registry`），exit 0。`checkids` 的 `note` 现在是
`99 check_id(s), 3 known non-conforming ...; 146 citation(s), 3 unresolved ...`。

