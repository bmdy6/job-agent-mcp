# job-agent MCP

Local stdio server backed by the owned SQLite, collection, scoring, delivery and monitor services. `engine.backend` defaults to `own` and accepts only `own`.

From the repository root:

```bash
uv sync --project mcp
uv run --project mcp python -m job_agent.server
PATH=$HOME/.local/bin:$PATH uv run --project mcp python -m unittest discover -s mcp/tests -t mcp -v
```

Configuration, tool reference and client registration: [usage](../docs/usage.md). Browser-free examples: [offline usage](../docs/offline.md).

MCP exposes saved-job search and JD extraction, profile/briefing, experience and company files, application events/funnels, local resume text extraction, scoring, delivery, monitor control and notification testing. Every tool uses the audited dispatcher; private data stays under `$JOB_AGENT_HOME` (default `~/.job-agent`). Job IDs are string representations of owned SQLite row IDs.

Preview creates a private immutable batch. Show its complete platforms, actions, companies, jobs, greetings and attachment settings, then wait for explicit user confirmation before `send_greetings`. A token alone is never consent. Tokens persist across MCP restarts, expire within 30 minutes and are consumed once before starting the worker. Changed configuration or jobs require another preview. Real delivery also requires `engine.dry_run=false`, `engine.live_send_enabled=true`, enabled platforms and their preflight checks. Uncertain outcomes never retry automatically.

The engine owns rate limits, cooldowns, platform pauses and receipts. Background task state and stop markers live under `$JOB_AGENT_HOME/own-tasks/`. Stop applies to the current workflow; it does not stop unrelated collection or monitoring. Browser access uses the running Ego Lite with dedicated named spaces; shutdown retains the anchor and Agent ownership.

`preview_scoring` estimates paid requests locally. `start_scoring(confirm=true)` requires explicit authorization, then starts the configured provider worker. `scoring_runs` reads persisted results. Credentials use vault/env references; key values do not belong in YAML. Requests run serially without automatic retries. Native scores do not overwrite historic imported scores.

`job-agent monitor` reads the original platform conversations after separate authorization. `job-agent watch [--once] [--source replies]` only notifies from saved SQLite replies; it does not visit platform pages. Notifications default to disabled. Enabled Telegram channels receive deduplicated reply notifications and daily delivery summaries under the existing switches. Transport success followed by a crash before saving the receipt can still duplicate a notification.

Existing `kind=legacy_*` records, imported score metadata and source history remain in the owned store and keep participating in statistics. This change adds no database migration. Store-level historic funnels and service-level combined funnels retain their separate meanings.

Tests bootstrap a temporary `JOB_AGENT_HOME` when unset or pointing at production. Run a single module from `mcp/` with `python -m unittest tests.test_integration_service`; the installed MCP SDK owns the `mcp` package name. Test processes refuse production paths even if a test changes the environment later.

私有 Telegram 指令通过现有息壤 Hermes 网关接入。CLI `job-agent command status|today|pause|resume|only_reply [--json]` 与 MCP `command(action)` 共用数据库运行模式，不接收 Telegram updates，不清除平台风控暂停。详见 [指令与集成契约](../docs/job-commands.md)。
