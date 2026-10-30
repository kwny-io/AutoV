# 基础设施

拓扑见 [docs/03-deployment-capacity.md](../docs/03-deployment-capacity.md)。

## 结构

```text
infra/
  global/                       全局控制面：Tenant Directory、home_region/home_cell、身份联邦配置
  regions/<region>/
    cells/<cell>/               每个 cell：独立服务部署、Temporal namespace、事件 topic 空间、
                                数据库集群、对象存储前缀与密钥、Redis、索引、可观测采集
```

模板在 `regions/_template/cells/_template/`。

## cell 的含义

租户被分配到某个地域 cell。剧本、媒体、音色、记忆、任务与账本都落在 **home cell**。

全球访问先做租户解析再路由。**客户端不能任意指定其他 cell 越权。**

通用 cell 多租户逻辑隔离；高等级企业可要专属 namespace、专属数据集群或整个专属 cell。

## 单写

生产事实按 `tenant / project` 归 home cell。单集 manifest CAS 按 `episode + branch` 串行。

大租户可以把不同 series 分到不同 cell，但**同一 series 的记忆权威写入仍只有一个 home cell**。

## 数据库隔离

- 普通规模服务可共享托管 Postgres 集群，但必须**不同 database、不同 role**
- S08 / S16 / S23 建议物理隔离

## 容量是未验证的规划值

docs/03 里的 1,000 租户 / 5,000 活跃 run / 90 万视频请求/天等数字是用 Little 定律推的**规划口径**，没有实测支撑。任何容量结论都要重新标定。

SLO 同理：控制 API 99.95%、普通读 P95≤500ms、受理命令 P95≤1s、事件投影 P95≤5s —— 均为**目标**，排除 LLM/模型执行时间。
