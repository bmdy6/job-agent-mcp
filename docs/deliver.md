# 第二轮集成 CLI

旧 MCP `preview_greetings` / `send_greetings` 保持原实现和默认行为。

新入口在隔离的 `JOB_AGENT_HOME/config.yaml` 配置 engine，然后运行：

```sh
job-agent deliver --database /absolute/path/to/offline.sqlite3 --preview --job-ids 1 2 3 4
job-agent deliver --database /absolute/path/to/offline.sqlite3 --confirm TOKEN
```

`--job-ids` 是自有库 jobs.id；预览显示完整平台岗位列表、URL、各平台招呼语、附件名和配置哈希。
确认只执行此 token 的岗位；配置变化、重复确认、已尝试岗位均拒绝。新建 dry-run 库可直接使用；已有真实库须只读 backup 到独立文件，迁移副本后明确标为 dry-run，绝不能直接用真实库验证。dry-run 只接受内置离线依赖，汇总中的 sent 是模拟结果。

默认 dry_run=true、live_send_enabled=false。真实路径已接具体四平台工厂，但必须显式配置 dry_run=false 和 live_send_enabled=true，再预览并确认新批次；本轮未开启或执行真实路径。不要据离线通过启用日常投递，仍需 Claude 审查及用户指定单岗位授权验收。51job 按架构申请后唯一选择附件并读回；其他平台附件未验证时提前拒绝。不同附件名分批预览。

`--notify` 是真实执行后的 TG 汇总显式开关，还需 notify 和渠道配置启用；preview/dry-run 禁止该开关。本轮未发 TG。

schema v3 增量接续 #21 v2：回复按 (platform,message_id) 唯一，历史重复行原样归档在 replies_legacy_duplicates；补 chat_url 和队列/流水 error_detail。副作用异常保留说明并标 unverified，停同平台本批剩余岗位；登录/风控同样暂停本批该平台。历史其他批次的 unverified 不阻挡当前批准的新岗位，每日限额仍读取整个平台历史。

监听工厂使用无参 ReplySource()；回复链接存入 SQLite，旧 monitor-state 链接只作读兼容。猎聘仍缺真实雇主收件映射验收；51job 正文入口未验证，回复源保持显式失败。

shutdown 仅关当前进程创建的临时页，保留锚点和 Agent 所有权，不 finish。不操作其他服务或空间。
