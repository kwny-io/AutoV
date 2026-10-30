# v2契约边界

本目录包含48服务机器目录、9个企业JSON Schema、21个核心OpenAPI操作、48种服务事件Topic设计及MCP工具清单。全部为设计契约，尚无运行服务。每服务的完整资源/迁移/错误字段由后续实施按service spec细化；本包不声称已提供48服务的全部生产OpenAPI。

execution-context包含受信身份、home-cell、branch、manifest、node_epoch、execution_fence与memory checkpoint。外部客户端不能自填actor/tenant取得权限；入口派生后校验所有引用。内部持久任务保存验证后的上下文，但授权撤销/kill switch/预算仍在dispatch前重查。

generation-command为text、understanding、image、video、speech、lipsync、sound_effect、music、training九种输入分别定义结构。image/video/speech延用v1的细化输入结构；v2不再用video Prompt字段装其他模态。自托管训练可用资源调度专用实现，但仍受预算/配额与来源约束。

enterprise-change-plan中的path是候选补丁描述，服务端必须验证各领域的字段白名单，不允许任意JSON Patch执行。准备revision、字段影响、记忆影响、派生动作、报价、政策与版本需要一起验证。相同manifest不同节点可保留各自node_epoch；只有受影响子图递增。

Schema只验证形状和部分常量；以下需要领域验证：跨字段时间区间、引用存在/权限、accepted记忆来源、分支/知情范围、预算守恒、状态转换、input hash与路由/dispatch绑定、角色/音色授权、前后节点时间可行性、政策不可降权。示例hash/ID/金额/日期均为占位，不能直接用于真实调用。

EventEnvelope扩展CloudEvents字段，但不是CloudEvents官方认证。OpenAPI为3.1设计。MCP JSON是工具清单设计，实施时解析相对schema引用、填SDK outputSchema、scope与错误。domain-v1只保留历史负载schema，旧接口和事件格式不用于新生产。

Schema的`.invalid`域名只作标识，解析使用本地注册表；不允许联网拉取未知schema。新major版本兼容迁移与事件规范见../docs/04-data-consistency.md。
