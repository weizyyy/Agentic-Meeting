# Security Policy

## Supported versions

Security fixes are applied to the latest release.

## Reporting a vulnerability

Please do not open a public issue. Report vulnerabilities through a
[private security advisory](https://github.com/weizyyy/Agentic-Meeting/security/advisories/new).
Include the affected version, a description of the problem and, if possible, steps to reproduce it.
You can expect an initial response within a week.

## Deployment notes

Agentic-Meeting is designed for a trusted local network.

- The web application has **no authentication**. Anyone who can reach the port can read meeting
  records, start meetings and delete them. Do not expose it to the public internet.
- Inference services started by the application listen on `127.0.0.1` only.
- When the realtime LLM or the background agent uses a remote endpoint, meeting transcripts and
  screenshots are sent to that endpoint. `agentic-meeting check` lists where data will go.
- The background agent executes model-written code. Keep `agent.sandbox.kind = "docker"`; the
  `local` sandbox offers no isolation and is meant for development machines only.
- API keys are read from environment variables and are never written to the configuration file,
  logs or the database.

---

# 安全策略

## 支持的版本

安全修复只针对最新发布的版本。

## 报告漏洞

请不要公开提交议题。请通过
[私密安全公告](https://github.com/weizyyy/Agentic-Meeting/security/advisories/new)报告漏洞，
并说明受影响的版本、问题描述，以及（如有可能）复现步骤。通常会在一周内给出初步答复。

## 部署须知

Agentic-Meeting 面向可信的局域网环境设计。

- 网页应用**没有身份认证**。任何能够访问该端口的人都可以查看会议记录、开始和删除会议。请勿将其暴露在公网上。
- 由应用启动的推理服务只监听 `127.0.0.1`。
- 实时模型或后台 agent 使用远端接口时，会议转录和截图会发送到该接口。`agentic-meeting check` 会列出数据的去向。
- 后台 agent 会执行由模型编写的代码。请保持 `agent.sandbox.kind = "docker"`；`local` 沙箱不提供隔离，
  仅适用于开发环境。
- API 密钥从环境变量读取，不会写入配置文件、日志或数据库。
