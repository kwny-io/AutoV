# 骨架生成器

仓库里的元数据是**生成的**，不是手写的。改内容请改源，再重新生成。

| 脚本 | 源 | 产物 |
|---|---|---|
| `generate_services.py` | `contracts/service-catalog.v2.json` + `contracts/event-topics.v2.json` | `services/*/service.yaml`、`services/*/service.md` |
| `generate_agents.py` | docs/08 §1 的角色表（脚本内） | `agents/*/agent.yaml` |
| `generate_connectors.py` | docs/09 §2 的渠道表（脚本内） | `connectors/*/*/connector.yaml` |

## 用法

在仓库根目录：

```bash
python tools/scaffold/generate_services.py
python tools/scaffold/generate_agents.py
python tools/scaffold/generate_connectors.py
```

生成是幂等的，可以反复运行。

## 为什么服务元数据从 catalog 生成

因为 48 份手写 YAML 一定会漂移。catalog 是机器可读的单一来源，服务目录、事件主题、
依赖关系都从它派生，改一处就全部一致。

`agent.yaml` 和 `connector.yaml` 目前从脚本内的表生成，因为 docs/08 和 docs/09 里的
角色/渠道还没有机器可读的契约。后续应当把这两张表也提取成 `contracts/` 下的 JSON，
让脚本读契约而不是读硬编码。
