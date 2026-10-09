# Roadmap

**English** · [简体中文](ROADMAP.zh-CN.md)

This page describes where Agentic-Meeting is heading. Each stage is a release with a
[milestone](https://github.com/weizyyy/Agentic-Meeting/milestones) on GitHub; the items link to the issues where the details are
discussed. Progress is tracked in [#27](https://github.com/weizyyy/Agentic-Meeting/issues/27).

The order follows three rules: safety before features, verification before expansion, and
architectural changes in the order in which they depend on each other. Dates are not promised.

## [v0.2 – Secure access](https://github.com/weizyyy/Agentic-Meeting/milestone/1)

Make it safe to let other people reach an instance.

| Item | Issue |
|---|---|
| Access password for the web app: login page, session cookie, rate limiting | [#8](https://github.com/weizyyy/Agentic-Meeting/issues/8) |
| Deployment guide: HTTPS, reverse proxy and TURN | [#9](https://github.com/weizyyy/Agentic-Meeting/issues/9) |
| Health check and basic metrics endpoints | [#10](https://github.com/weizyyy/Agentic-Meeting/issues/10) |
| Data governance: retention, automatic cleanup and a recording notice | [#11](https://github.com/weizyyy/Agentic-Meeting/issues/11) |

## [v0.3 – Runs everywhere](https://github.com/weizyyy/Agentic-Meeting/milestone/2)

Verify the system beyond the platform it was developed on and make it easier to deploy.

| Item | Issue |
|---|---|
| Full-system verification on Linux and macOS, long runs, the llama.cpp realtime mode | [#12](https://github.com/weizyyy/Agentic-Meeting/issues/12) |
| Desktop browser compatibility: Chrome, Edge, Safari and Firefox | [#13](https://github.com/weizyyy/Agentic-Meeting/issues/13) |
| Browser end-to-end tests in CI | [#14](https://github.com/weizyyy/Agentic-Meeting/issues/14) |
| Containers for the application and all inference services | [#15](https://github.com/weizyyy/Agentic-Meeting/issues/15) |
| Lower GPU memory requirements while keeping most of the quality | [#16](https://github.com/weizyyy/Agentic-Meeting/issues/16) |
| Condition-based waits in the test suite | [#17](https://github.com/weizyyy/Agentic-Meeting/issues/17) |

## [v0.4 – Frontend redesign](https://github.com/weizyyy/Agentic-Meeting/milestone/3)

Rebuild the web client and open it to more languages and devices.

| Item | Issue |
|---|---|
| Redesign on a design system: themes, responsive layouts, accessibility | [#18](https://github.com/weizyyy/Agentic-Meeting/issues/18) |
| Interface translations, starting with Chinese and English | [#19](https://github.com/weizyyy/Agentic-Meeting/issues/19) |
| Phones as companion devices on Android and iOS | [#20](https://github.com/weizyyy/Agentic-Meeting/issues/20) |

## [v0.5 – Multi-user and parallel meetings](https://github.com/weizyyy/Agentic-Meeting/milestone/4)

Move beyond one device and one meeting at a time.

| Item | Issue |
|---|---|
| Live updates and typed questions for people watching a meeting | [#21](https://github.com/weizyyy/Agentic-Meeting/issues/21) |
| Per-meeting access: passwords, share links and roles | [#22](https://github.com/weizyyy/Agentic-Meeting/issues/22) |
| Several meetings at once on one machine | [#23](https://github.com/weizyyy/Agentic-Meeting/issues/23) |
| Single sign-on with OpenID Connect | [#24](https://github.com/weizyyy/Agentic-Meeting/issues/24) |
| Speakers remembered across meetings | [#25](https://github.com/weizyyy/Agentic-Meeting/issues/25) |

## Not scheduled

| Item | Issue |
|---|---|
| Meetings held in languages other than Mandarin (prompts, wake word, ASR) | [#26](https://github.com/weizyyy/Agentic-Meeting/issues/26) |

Running across several machines is not planned: parallel meetings on one well-equipped machine
cover the intended use.

## Suggesting changes

Ideas and questions are welcome in [Discussions](https://github.com/weizyyy/Agentic-Meeting/discussions). To propose a concrete
feature, open a [feature request](https://github.com/weizyyy/Agentic-Meeting/issues/new/choose).
