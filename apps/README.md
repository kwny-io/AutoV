# 应用层

五个独立应用，职责不重叠：[docs/13-api-product.md](../docs/13-api-product.md)。

| 应用 | 职责 |
|---|---|
| `api-gateway` | 入口、租户解析、鉴权、限流、路由 |
| `studio-bff` | 工作台后端聚合，为 12 个工作台页面供数 |
| `studio-web` | 前端工作台；`layout.html` 是静态线框 |
| `developer-api` | 对外开放的开发者 API 面 |
| `mcp-server` | MCP 工具服务（streamable-http），12 个工具 |

## 网关不持有业务逻辑

网关只做入口职责。**它不能成为第二个权威**，也不能绕过服务直接读写数据库。

## 上下文必须由入口派生

客户端不能自填 `actor` / `tenant_id` 取得权限。入口派生后校验所有引用。内部持久任务保存的是**已验证**的上下文，但授权撤销、kill switch、预算仍然要在 dispatch 前重查。

## layout.html

`apps/studio-web/layout.html` 是静态线框：品牌名「镜序 · Enterprise」，所有按钮 `disabled`，无脚本、无网络请求。它是排版参考，不是可用界面。

## MCP scope

docs/13 定义了 read / plan / execute / review / admin 五档 scope。
`contracts/mcp-tools.v2.design.json` 的 12 个工具**都带 `required_scope`**（read 4 / execute 4 /
plan 1 / review 1 / admin 1 / **publish 1**），但两处对不上，实施前要由 owner 定：
`drama.delivery.publish` 用的 `publish` 是五档之外的第六个值；工具清单本身也不一致
（docs/13 具名 13 个，JSON 12 个，交集 8 个）。逐条差异见
[contracts/README.md 缺口 2](../contracts/README.md)。
