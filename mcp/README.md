# job-agent MCP

Local stdio job-agent server. `engine.backend: own` uses the owned SQLite, collection, scoring, delivery and monitor services; BossHunter is an optional legacy backend.

From the repository root:

```bash
uv sync --project mcp
uv run --project mcp python mcp/server.py
PATH=$HOME/.local/bin:$PATH uv run --project mcp python -m unittest discover -s mcp/tests -v
```

Configuration, tool reference and client registration: [repository README](../README.md).

The v1 server adds profile/briefing, experience and company storage, application
events/funnels, evaluation forwarding, confirmed greeting preparation, setup
checks and PDF/docx/md extraction. Private state stays under JOB_AGENT_HOME.
The existing AuditedMCP dispatch and redaction rules apply to every tool.

Issue #6 adds `funnel_stats`, `greeting_targets`, `preview_greetings`,
`send_greetings`, `delivery_status`, `stop_delivery`, `start_reply_monitor`
and `stop_reply_monitor` (32 tools total). Preview creates only a private local
confirmation; send requires explicit user confirmation of the shown batch.
Tokens survive MCP restarts, expire within 30 minutes and are consumed once
before the upstream effect, including uncertain HTTP outcomes. BossHunter owns
throttling. Live acceptance uses only stats/activity/workbench GET endpoints;
all sending and monitor tests use a fake HTTP service.

Issue #8 adds audited `notify_test(channel)` (33 tools total) and the
`job-agent watch [--once]` CLI. Notify switches and channels default to disabled.
Watch only reads saved jobs/history and never starts BossHunter monitor or
sends HR messages. See the repository README for channels, deduplication,
optional OpenAI-compatible drafts and the uninstalled launchd sample.

Issue #9 extends the existing approval workflow to BOSS, Liepin, Zhilian and 51job.
Preview items include `source_platform`, `action_id`, `action_label` and `delivery_enabled`.
Non-BOSS send requires enabled capabilities from the local BossHunter workbench and a fresh approval.
`apply_resume` does not require greeting text. No local browser or sender is added.
Platform grouping is the default for `funnel_stats`; the four-platform regression uses synthetic replies only.

Delivery summaries: `notify.delivery_summary` defaults true, under the existing
master/channel switches, and targets enabled Telegram channels only. Watch also
GETs `/api/workbench` and summarizes visible completed/failed deliver tasks once
per task/port/channel using the existing locked, fsynced receipt file. Counts
come from task metrics; platform/company/reason details come from time-bounded
UTC history. Deferred/unknown outcomes are explicit. Replaced in-memory tasks
can be missed because BossHunter exposes no task archive or task IDs on history.
See the main README for count semantics and transport duplication limits.

### #15 MCP backend（任务 K）

`engine.backend` 默认 `bosshunter`。设为 `own` 后，preview_greetings、send_greetings、delivery_status、stop_delivery、start_reply_monitor、stop_reply_monitor 走自建服务。岗位来自 `$JOB_AGENT_HOME/jobs.sqlite3`，MCP job_ids 是此库行 ID 的字符串。

```sh
job-agent sync-bosshunter
# 可重复执行；BossHunter 源以 mode=ro 打开，仅新增岗位/历史，不覆盖旧快照。
```

用户先看完整预览，明确确认/发送后才调用 send_greetings；token 一次性且默认 30 分钟过期。own MCP 真实发送还要求 `dry_run=false` 和 `live_send_enabled=true`，配置变更须重新预览。关闭开关仍允许创建预览。exclude_ids 只能排除预览中的部分岗位，不改变原批次内容，不执行被排除岗位。

自有后台任务状态和停止标记保存在 `$JOB_AGENT_HOME/own-tasks/`，stop 在当前操作完成后停止后续操作。退出保留 Ego 空间锚点，不 finish；不停止 BossHunter。进程崩溃后先核对 status 和原站，不自动重试。

BOSS/智联/猎聘长度校验不超过 P0 实测的 1000/500/800（UTF-16 单位）。51job 尚无实测消息入口或上限；目前仅申请和选附件，配置数值不能视为平台能力证明。其他 MCP 工具的全量切换和采集属于 ARCHITECTURE.md 后续阶段，本任务 K 只接上述六项。


### Owned scoring (#48)

`preview_scoring` estimates paid requests without writes/network; after showing
that estimate and obtaining user approval, `start_scoring(confirm=true)` starts a
private worker. `scoring_runs` reads persisted item outcomes and rate-limit data.
Configure `scoring.resume_path` and the vault/env credential reference; no key
value belongs in YAML. Defaults: gpt-6.1-sol, priority, max_completion_tokens=1200,
10 selected jobs/run, serial requests, no automatic retry. New collected jobs
with JD become pending; imported scores/reasons are preserved. Native scores
feed search/detail/stats/daily ordering without changing delivery state.

Migration 4 is recorded by schema_migrations. Back up the database and upgrade
all monitor/daily runtimes before migrating production; older v3 runtimes reject
v4. Own setup_check never contacts BossHunter. Unknown provider outcomes keep a
job claim for manual reconciliation instead of silently retrying a paid request.
