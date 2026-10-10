# 路线图

[English](ROADMAP.md) · **简体中文**

本页说明 Agentic-Meeting 接下来的方向。每个阶段对应一个版本，在 GitHub 上各有一个
[里程碑](https://github.com/weizyyy/Agentic-Meeting/milestones)；各事项链接到对应的议题，细节在议题中讨论。进度汇总在 [#27](https://github.com/weizyyy/Agentic-Meeting/issues/27)。

排序遵循三条原则：安全先于功能，先验证再扩展，架构上的改动按相互依赖的顺序进行。不承诺具体日期。

## [v0.2 – 安全访问](https://github.com/weizyyy/Agentic-Meeting/milestone/1)

让实例可以放心地开放给其他人使用。**已在 [0.2.0](CHANGELOG.md#020---2026-10-10) 发布。**

| 事项                                              | 议题                                                        |
| ------------------------------------------------- | ----------------------------------------------------------- |
| 网页应用的访问口令：登录页、会话 Cookie、登录限速 | [#8](https://github.com/weizyyy/Agentic-Meeting/issues/8)   |
| 部署指南：HTTPS、反向代理与 TURN                  | [#9](https://github.com/weizyyy/Agentic-Meeting/issues/9)   |
| 健康检查与基础指标接口                            | [#10](https://github.com/weizyyy/Agentic-Meeting/issues/10) |
| 数据治理：保留期限、到期自动清理、录制提示        | [#11](https://github.com/weizyyy/Agentic-Meeting/issues/11) |

## [v0.3 – 全平台](https://github.com/weizyyy/Agentic-Meeting/milestone/2)

在开发所用平台之外完成验证，并降低部署难度。

| 事项                                                                  | 议题                                                        |
| --------------------------------------------------------------------- | ----------------------------------------------------------- |
| Linux 与 macOS 上的完整系统验证、长时间运行、llama.cpp 方式的实时模型 | [#12](https://github.com/weizyyy/Agentic-Meeting/issues/12) |
| 桌面浏览器兼容：Chrome、Edge、Safari、Firefox                         | [#13](https://github.com/weizyyy/Agentic-Meeting/issues/13) |
| 接入 CI 的浏览器端到端测试（已在 0.2.0 完成）                         | [#14](https://github.com/weizyyy/Agentic-Meeting/issues/14) |
| 应用与全部推理服务的容器化                                            | [#15](https://github.com/weizyyy/Agentic-Meeting/issues/15) |
| 在保留大部分效果的前提下降低显存需求                                  | [#16](https://github.com/weizyyy/Agentic-Meeting/issues/16) |
| 测试中的固定等待改为等待条件成立（已在 0.2.0 完成）                   | [#17](https://github.com/weizyyy/Agentic-Meeting/issues/17) |

## [v0.4 – 前端重构](https://github.com/weizyyy/Agentic-Meeting/milestone/3)

重做网页客户端，并支持更多语言和设备。

| 事项                                           | 议题                                                        |
| ---------------------------------------------- | ----------------------------------------------------------- |
| 基于设计规范重新设计：主题、响应式布局、无障碍 | [#18](https://github.com/weizyyy/Agentic-Meeting/issues/18) |
| 界面多语言，首批为中文和英文                   | [#19](https://github.com/weizyyy/Agentic-Meeting/issues/19) |
| Android 与 iOS 手机作为辅助设备                | [#20](https://github.com/weizyyy/Agentic-Meeting/issues/20) |

## [v0.5 – 多用户与并行会议](https://github.com/weizyyy/Agentic-Meeting/milestone/4)

不再局限于一台设备、一场会议。

| 事项                             | 议题                                                        |
| -------------------------------- | ----------------------------------------------------------- |
| 旁听者的实时更新与打字提问       | [#21](https://github.com/weizyyy/Agentic-Meeting/issues/21) |
| 按会议授权：口令、分享链接与角色 | [#22](https://github.com/weizyyy/Agentic-Meeting/issues/22) |
| 单机同时进行多场会议             | [#23](https://github.com/weizyyy/Agentic-Meeting/issues/23) |
| 通过 OpenID Connect 单点登录     | [#24](https://github.com/weizyyy/Agentic-Meeting/issues/24) |
| 跨会议记住说话人                 | [#25](https://github.com/weizyyy/Agentic-Meeting/issues/25) |

## 未排期

| 事项                                         | 议题                                                        |
| -------------------------------------------- | ----------------------------------------------------------- |
| 以其他语言进行的会议（提示词、唤醒词、识别） | [#26](https://github.com/weizyyy/Agentic-Meeting/issues/26) |

暂不考虑跨多台机器运行：在一台配置足够的机器上并行多场会议，已能覆盖预期的使用场景。

## 提出建议

欢迎在 [Discussions](https://github.com/weizyyy/Agentic-Meeting/discussions) 中提出想法和疑问。如需提议具体的功能，
请提交[功能请求](https://github.com/weizyyy/Agentic-Meeting/issues/new/choose)。
