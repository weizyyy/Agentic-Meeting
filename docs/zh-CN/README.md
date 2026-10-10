# 文档

[English](../README.md) · **简体中文**

## 使用

| 文档                           | 内容                                                 |
| ------------------------------ | ---------------------------------------------------- |
| [入门指南](getting-started.md) | 安装、模型文件、首次运行、HTTPS                      |
| [数据治理](data-governance.md) | 数据存储与外发、期限、保留标记与删除恢复边界         |
| [使用指南](user-guide.md)      | 会中与会后如何使用网页                               |
| [配置说明](configuration.md)   | `config.toml` 的各个小节                             |
| [故障排查](troubleshooting.md) | 麦克风电平、唤醒、服务降级                           |
| [部署](deployment.md)          | 反向代理与 HTTPS、TURN、对外开放前的检查清单         |
| [容器](containers.md)          | Docker Compose、镜像、组合推理服务、容器里的代码沙箱 |
| [运行时与模型](runtimes.md)    | 推理运行时的获取与构建、模型文件、多显卡分配         |
| [性能实测](benchmarks.md)      | 在真实硬件上测得的延迟、内存与准确率                 |

## 理解与修改代码

| 文档                                       | 内容                                                  |
| ------------------------------------------ | ----------------------------------------------------- |
| [架构](architecture.md)                    | 进程、管线、数据流、上下文管理、故障处理              |
| [接口参考](interfaces.md)                  | 配置结构、数据库、算法、HTTP 接口、数据通道消息、工具 |
| [开发指南](development.md)                 | 约定、测试、项目规则                                  |
| [Pipecat 集成说明](pipecat-notes.md)       | 本项目对 Pipecat 1.12 的用法                          |
| [Agents SDK 集成说明](agents-sdk-notes.md) | 后台 agent 对 OpenAI Agents SDK 的用法                |

中英文文档的文件名和章节编号保持一致。
