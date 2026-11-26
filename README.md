# job-agent

让 Claude Code / Codex 帮你求职的 MCP 系统：采集岗位、评分、写招呼语，在 BOSS、智联、猎聘、51job 投递，HR 回复推送到 Telegram。

手动投递先预览、由你确认再发；每日自动投递默认关闭，需要显式开启。只有拿到原站回执才算发送成功。

<img src="docs/images/architecture.zh-CN.svg" alt="MCP、CLI 和计划中的 TG 指令经 service 编排 engine、platforms、browser 与 store，产出岗位、投递回执和回复通知" width="838">

示意图：实线是现有调用关系，虚线是计划中的 TG 指令；评分直接走 service / store，无需浏览器。[架构依据](docs/ARCHITECTURE.md)。

- 拟人输入结合频率控制，遇到验证码或风控暂停平台，由用户处理后显式恢复。
- 投递回执状态机保留原因码，结果不明记为 `unverified`，停止该平台剩余投递，不自动重投。
- 每日任务与监听统一部署到 `runtime/current`，支持回滚，数据库变更通过 `schema_migrations`。

## 安装与启动

需要 Python 3.10+、[uv](https://docs.astral.sh/uv/)；网页操作还需 Ego Lite。统一部署需要 Python 3.12+ 和 macOS launchd。

```sh
git clone https://github.com/bmdy6/job-agent-mcp.git
cd job-agent-mcp
uv sync --project mcp
```

将 [config.example.yaml](config.example.yaml) 首次复制到 `~/.job-agent/config.yaml`，已有配置先备份再对照补充。默认 own 后端、本地 SQLite，真实发送与每日任务关闭；数据库初次打开会迁移，升级已有库前先统一运行时并备份，见 [部署与回滚](docs/deploy.md)。

## 注册到 Codex / Claude Code

从仓库根目录注册 [stdio MCP](docs/usage.md#注册到-codex--claude-code)，重开客户端会话。首次先调用 `setup_check()`，再用 `search_jobs()` 查看已存岗位、`funnel_stats()` 查看漏斗；空库需要先确认采集范围。

## 工具

| 阶段 | 入口与产物 |
| --- | --- |
| 采集与筛选 | `check_collection` → 授权 `start_collection`；岗位 / JD 入库，`jd_requirements` 提取原句 |
| 评分与文案 | `preview_scoring` → 授权付费 `start_scoring`；分数、理由与 run_id；Agent 按 JD 和经历写招呼语 |
| 确认投递 | `preview_greetings` → 展示完整批次 → 用户确认 → `send_greetings`；原站回执与原因码 |
| 回复与统计 | `start_reply_monitor` 需独立授权；新回复去重推 TG，`delivery_status` / `funnel_stats` 查进度与漏斗 |

[工具和配置细节](docs/usage.md) · [Agent 技能](skills/README.md) · [浏览器生命周期](docs/browser.md) · [每日任务](docs/daily/README.md)。仅保留自研后端，旧配置处理见[迁移说明](docs/migrations/BTY-10.md)。

## 当前进度

| 能力 | 状态 |
| --- | --- |
| 采集、评分、经验要求与 HR 活跃度排序 | 四个平台已在真实页面跑通 |
| 投递（拟人输入、频率控制、回执核对） | BOSS、智联已真实投递验证；猎聘、51job 待验收 |
| 回复监听与 Telegram 推送 | BOSS、智联、51job 正常；猎聘修复中 |
| 面试邀约识别、招呼语效果对比 | 已上线，样本积累中 |
| 每日自动运行 | 已实现，默认关闭，验收后再启用 |
| 自动跟进、TG 指令 | 计划中 |

## 可观测性

| 已有证据 | 可以看什么 |
| --- | --- |
| `activity.jsonl`（JSONL） | MCP 调用时间、调用方、参数长度/数量和结果摘要；不记录参数原文 |
| 投递流水 / 回执、步骤诊断、手势日志 | 原因码、原站回执；失败步骤、点数、耗时和可信事件观测；见 [输入与限频](docs/usage.md#46-登录预检频率和输入) |
| `monitor-runtime.json`、TG 告警与汇总 | 各平台轮询状态、连续失败与批次结果；见 [监听](docs/monitor-live/README.md) |
| 评分 `run_id` / token 用量、部署状态、`funnel_stats` | 评分批次与模型用量；部署 commit / schema / runtime；按平台、城市、分数档统计 |

监控状态文件使用 JSON；这些信息目前分散在日志、SQLite 和工具返回中。跨阶段统一 run_id、心跳告警、日志轮转、趋势指标与统一 LLM 追踪仍在计划中。

## 测试

```sh
cd mcp
PATH=$HOME/.local/bin:$PATH uv run python -m unittest discover -s tests -v
```

测试使用临时数据和假浏览器；真实平台验证进度以以上 Issue 为准。代码未声明 LICENSE。
