# 契约

这一层是**设计稿**，不是已发布的 API。它定义服务之间能说什么、事件长什么样、schema 长什么样。

原始契约说明已存档在
[docs/design-package/contracts/README.md](../docs/design-package/contracts/README.md)；
`docs/02-service-catalog.md` 是 48 服务全表。

## 布局

```text
contracts/
  openapi.v2.yaml              OpenAPI 3.1，21 个核心操作
  service-catalog.v2.json      48 服务元数据（services/*/service.yaml 的源）
  event-topics.v2.json         48 个事件主题
  mcp-tools.v2.design.json     12 个 MCP 工具（streamable-http）
  *.v2.schema.json             9 个企业 schema（JSON Schema 2020-12）
  domain-v1/                   v1 领域 payload 基线，保留作字段参考
  examples/                    结构与示例（占位 hash / ID，不能当 fixture）
```

## 三层文件的关系

```
service-catalog.v2.json  ──生成──▶  services/<slug>/service.yaml
        │
        └──引用──▶ event-topics.v2.json ──引用──▶ event-envelope.v2.schema.json
```

改服务元数据请改 catalog，然后重新生成 `service.yaml`，不要手改生成物。

## 设计时留下的缺口

实现前需要先补齐，否则代码生成或归属会出问题：

1. **OpenAPI 21 个操作没有 `x-owner` 扩展字段** —— 所有权映射只存在于散文里，工具无法自动归属到服务。
2. **MCP 工具 JSON 里没有 scope 字段** —— 只有 annotations 和 input schema，而 docs/13 描述了 read/plan/execute/review/admin 五档 scope。契约与文档不一致。
3. **`examples/` 里所有 hash 和金额都是占位符**（`aaaa…`、`bbbb…`）—— 结构示意，不能直接当测试 fixture。

## schema 管不到什么

Schema 能证明结构合法，**证明不了**：

- 时间区间的先后关系
- 引用是否真实存在
- 金额守恒
- 状态转移是否合法
- 策略是否被降级

这些只能靠测试和分布式验证。**schema 合法 ≠ 行为正确。**

## v1 为什么还在

`domain-v1/` 只作为 payload 细化基线（ShotSpec / GenerationRequest / QCReport / ChangePlan / EventEnvelope v1）。v2 显式补了上下文与权威边界，但字段定义仍然有参考价值。
