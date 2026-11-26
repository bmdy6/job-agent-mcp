# 自研 HR 回复监听（#37，第 1 轮）

监听通过 Ego Lite 按名字复用 `job-agent 监听`，与投递、采集空间分离。只读取已有会话，不发 HR 消息、简历或申请。统一配置 `engine.monitor`：

```yaml
engine:
  monitor:
    task_space_name: job-agent 监听
    interval_minutes: 5
    backfill_hours: 48
    failure_threshold: 3
    platforms: [boss, zhilian, liepin, "51job"]
```

默认首次通知时补推最近 48 小时。`monitor-policy.json` 保存首次激活时间与固定 cutoff；重启、断网重试仍用同一个窗口，不因重启丢失未成功推送的回复。更早或时间不明的回复仍留在库中，不推。修改 backfill_hours 只影响新的首次激活，不会重置已有 policy。

### 会话增量读取

每个平台第一次只把当前已加载列表的会话摘要记入 SQLite（schema v5），即使有未读也不逐个点开。之后只打开未读数大于 0、最后消息 ID/摘要指纹变化，或基线后新增的会话。列表重排不触发读取；读取失败保留旧摘要，回复与成功摘要在同一事务提交，重启仍继续使用原游标。新增会话和未读会话读取已加载消息，源的固定回溯 cutoff 沿用首次建基线时的 `backfill_hours`，通知继续受既有 policy 和回执控制。

BOSS 使用 `uniqueId / lastMsgId / unreadCount / lastTS`。智联列表没有消息 ID，使用 `sessionId` 和发送时间、预览文本、消息类型、发送方类型的 SHA-256 指纹；库中只保存指纹，不保存列表预览正文。猎聘按联系人 ID、最后消息 ID、未读数和最后时间提供摘要；字段不明时停止该平台，不能回退到逐个打开。猎聘摘要字段尚未在当前账号实测（平台保持原有风控暂停），需解除暂停的独立授权后核验。

51job 保留申请状态读取，不新增聊天入口：按岗位、申请日期和状态集合比较，首次仅建基线，之后只返回变化状态；仍需读取完整状态列表来发现变化。`monitor-runtime.json` 的逐平台结果增加 `sessions / selected / baseline`，没有变化时 fetched 为 0。

只有本库已投（delivery.status=sent）的 incoming 回复会推送；输出保留原文、平台、公司、岗位和聊天/岗位链接。共用 `notified.jsonl` 按平台消息 ID、渠道、分段去重，失败下轮从库重试。Telegram 不支持幂等键，传输成功后、回执落盘前进程崩溃仍有很小的重复窗口。

## 安装与控制

从干净的已提交代码运行（分支可临时验收，合并后必须从 main 重装）：

```sh
./scripts/install-monitor.sh --backup-dir /absolute/task/backup
```

安装器先把旧 watch plist、watch.sh、现有 monitor plist 与 config.yaml 备份到带时间戳的私有目录，建立新的不可变 runtime 并验证配置，然后卸载旧 watch、替换配置与 plist、启动新服务。服务名 `com.bmdy6.job-agent-monitor`，开机启动，失败/崩溃后至少 60 秒节流重启。需要本机 `uv`、Python 3 与已登录的 Ego Lite。

凭据从 `~/.config/ai-vault/vault.json` 递归匹配已配置的 `bot_token_env`，只进入子进程环境；不写进仓库/plist/日志。TG chat_id 仍使用现有私有配置。安装不修改 BossHunter 本体、数据库或投递 backend。

```sh
launchctl list | rg 'com.bmdy6.job-agent-(watch|monitor)'
tail -f ~/.job-agent/monitor.log
launchctl kickstart -k gui/$(id -u)/com.bmdy6.job-agent-monitor
# 完全停用（下次开机不启动，直到再次 bootstrap）
launchctl bootout gui/$(id -u)/com.bmdy6.job-agent-monitor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bmdy6.job-agent-monitor.plist
```

MCP `start_reply_monitor(confirm=true)` / `stop_reply_monitor()` 控制同一个 launchd 服务，与投递 backend 无关。`reply_monitor_status()`、`briefing`、`status` 的 monitor 字段显示 launchd PID/在线、最后完成抓取时间、逐平台 fetched/added/status。briefing/status 的其他部分在第 1 轮仍遵循现有 backend，第 2 轮再移除剩余 BossHunter 依赖。

进程锁阻止多个 monitor 实例。登录失效立即告警并暂停该平台，其他平台继续；重新登录后显式重启。连续抓取失败达到阈值后每渠道只告警一次，成功恢复后重置。空间失去控制立即告警、正常退出；`KeepAlive.SuccessfulExit=false` 仅重启错误或崩溃，不会自动抢占用户空间。修复后显式 start/kickstart 恢复。

逐平台结果写 monitor.log，运行状态写 monitor-runtime.json，不记录 HR 正文或凭据。源适配器当前读取已加载消息，不承诺导出完整聊天历史。51job 使用只读申请状态兜底：核对“全部 / 已查阅 / 感兴趣 / 邀面试”，并读取申请行内明确的“不匹配 / 不合适”状态。成功读取且没有 HR 状态时记 `ok`、`detail=无数据`；登录、风控、结构或分页不明仍报错。TG 标注“51job 状态变化，没有正文”，时间是首次观测时间，原站发生时间未知，首次抓取可能是已有状态；不把申请日期当消息时间。按岗位、申请日期、状态去重，同一申请的同一状态仅通知一次，仅关联本库已投岗位。没有接入微信或小程序。

BOSS 读取保留列表明确给出的 `lastMsgId=0`：这是已经建立但没有消息的会话，直接跳过；缺失 ID 仍报结构错误。列表职位名使用当前 `source.title`（兼容 `jobName`），视口外的已加载会话按稳定 ID 滚动后重新定位，不靠数字行号选择。职位已关闭不影响读取已有聊天，也不会因为消息正文提到“职位已关闭”而停止监听。

BOSS 失败日志包含 `stage`、`selector`、`row`、`code` 与错误类型，最新气泡不可用时记录对应跳过原因，正常结束记录 `sessions/empty/missing/fetched`；不记录消息正文、姓名或业务 ID。采集范围仍是本次已加载会话及消息，没有新增全历史或全列表分页。读取继续由 poller 统一预留 `monitor` scope 的频率额度。

### #47 验证记录（2026-11-26，待完成）

466 项离线测试通过，sdist/wheel 构建通过。工作区 CLI 的真实 BOSS 读取连续两轮均为 `status=ok / fetched=73 / missing=0 / sent=0`，每轮 40 个已加载会话、1 个已知空会话；第二轮新增 0，去重正常。验收使用隔离数据库快照并关闭通知，没有发送 HR 或 TG 消息，也没有部署。

第三轮在登录预检查阶段因采集空间已归用户控制而保护性停止，未访问聊天；尚未满足连续 10 次成功。当前 monitor 的共享登录预检查仍使用采集空间，需用户允许恢复后重新验收。Claude 只读审查请求返回认证 401，未产生审查结论。两项均是 draft PR 的待办，不能按已通过处理；实际 TG 发送也未测试，只通过模拟渠道验证解析、入库、通知与去重。
