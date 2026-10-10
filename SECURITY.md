# Security Policy

## Supported versions

Security fixes are applied to the latest release only.

| Version | Supported |
| ------- | --------- |
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a vulnerability

Please do not open a public issue. Report vulnerabilities through a
[private security advisory](https://github.com/weizyyy/Agentic-Meeting/security/advisories/new).
Include the affected version, a description of the problem and, if possible, steps to reproduce it.
You can expect an initial response within a week.

## Deployment notes

Agentic-Meeting is designed for a trusted local network.

- The web application has **no authentication by default**. Anyone who can reach the port can read
  meeting records, start meetings and delete them. Set an access password (`server.password_env`,
  see [getting started](docs/getting-started.md#access-password)) on any network that is not fully
  trusted, and serve it over HTTPS. The password is a single shared secret, not per-user accounts;
  even with it, do not expose the application directly to the public internet. Before others can
  reach an instance, go through the checklist in the [deployment guide](docs/deployment.md#2-checklist-before-exposing-an-instance).
- With a password, every `/api` endpoint requires a signed, HTTP-only session cookie, and
  state-changing requests also require a CSRF token. Failed logins are limited per client address.
  The cookie signing secret is stored in `<data_dir>/auth_secret`; protect that directory.
- `/healthz`, `/readyz` and `/metrics` answer without a login. They contain no meeting content, but
  `/metrics` shows load and activity; the reverse proxy examples in the deployment guide allow them
  only from a monitoring network.
- Inference services started by the application listen on `127.0.0.1` only.
- When the realtime LLM or the background agent uses a remote endpoint, meeting transcripts and
  screenshots are sent to that endpoint. `agentic-meeting check` lists where data will go.
- The background agent executes model-written code. Keep `agent.sandbox.kind = "docker"`; the
  `local` sandbox offers no isolation and is meant for development machines only.
- API keys, the access password and TURN credentials are read from environment variables and are
  never written to the configuration file, logs or the database.
- Meeting data stays until it is deleted or a retention period removes it. Deletion is not secure
  erasure: SQLite pages, backups, exported downloads, remote model endpoints and logs may still hold
  copies; see [data governance](docs/data-governance.md#4-complete-deletion-and-recovery-limits).

---

# 安全策略

## 支持的版本

安全修复只针对最新发布的版本。

| 版本  | 是否支持 |
| ----- | -------- |
| 0.2.x | 是       |
| < 0.2 | 否       |

## 报告漏洞

请不要公开提交议题。请通过
[私密安全公告](https://github.com/weizyyy/Agentic-Meeting/security/advisories/new)报告漏洞，
并说明受影响的版本、问题描述，以及（如有可能）复现步骤。通常会在一周内给出初步答复。

## 部署须知

Agentic-Meeting 面向可信的局域网环境设计。

- 网页应用**默认没有身份认证**。任何能够访问该端口的人都可以查看会议记录、开始和删除会议。
  在不完全可信的网络中，请设置访问口令（`server.password_env`，
  见[入门指南](docs/zh-CN/getting-started.md#访问口令)）并通过 HTTPS 访问。口令是所有人共用的一个密码，
  不是个人账号；即使设置了口令，也请勿将应用直接暴露在公网上。让其他人访问之前，请先对照
  [部署指南](docs/zh-CN/deployment.md#2-对外开放前的检查清单)中的检查清单。
- 设置口令后，所有 `/api` 接口都要求签名的 HTTP-only 会话 Cookie，改动数据的请求还要求 CSRF 令牌；
  登录失败次数按客户端地址限制。Cookie 签名密钥保存在 `<data_dir>/auth_secret`，请保护好该目录。
- `/healthz`、`/readyz` 和 `/metrics` 不需要登录。它们不含会议内容，但 `/metrics` 能看出负载和活动情况；
  部署指南中的反向代理示例只允许监控网段访问它们。
- 由应用启动的推理服务只监听 `127.0.0.1`。
- 实时模型或后台 agent 使用远端接口时，会议转录和截图会发送到该接口。`agentic-meeting check` 会列出数据的去向。
- 后台 agent 会执行由模型编写的代码。请保持 `agent.sandbox.kind = "docker"`；`local` 沙箱不提供隔离，
  仅适用于开发环境。
- API 密钥、访问口令和 TURN 凭据从环境变量读取，不会写入配置文件、日志或数据库。
- 会议数据会一直保存，直到被删除或超过保留期限被清理。删除不等于安全擦除：SQLite 页面、备份、导出的文件、
  远端模型接口和日志中仍可能留有副本；详见[数据治理](docs/zh-CN/data-governance.md#4-完整删除与恢复边界)。
