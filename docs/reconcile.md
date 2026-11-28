# 原站结果对账与岗位新鲜度

`reconcile` 只读取 schema v7 岗位库、原站投递记录与既有聊天页，不调用申请、开始沟通、输入或发送流程。它在独立 Ego Lite 空间「job-agent 对账」运行；按平台打开数据库同目录中**已有**的 automation.lock，等待其他生产任务释放锁。锁文件不会被创建、修改或删除。缺少锁文件时停止，不能用测试目录中的锁代替生产锁。

```bash
export JOB_AGENT_HOME=$(mktemp -d)
export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH
uv run --project mcp job-agent reconcile --status unverified,error \
  --database "$HOME/.job-agent/jobs.sqlite3" \
  --platform boss --platform zhilian --output /absolute/private/reconciliation.json
```

输出文件必须是新文件，位于生产目录之外，权限为 0600。省略 `--platform` 时会列出其他平台的候选；目前除 BOSS、智联、51job 外，其他平台没有已验证的对账读取器，明确记录为 `still_unknown/unsupported_platform`。`error` 匹配导入的 `legacy_error`（v7 存储为 unverified），`failed` 可单独选择。以每个岗位最新的审计流水为候选真源；已经对账的岗位不再次进入默认候选。

结果包含 `run_id`、岗位 ID、原流水 ID、快照指纹、终态和固定证据码/选择器、回执时间。不会保存招聘者姓名、消息正文、完整 URL、页面 dump 或凭据。页面数据只在内存匹配，BOSS 消息投影在浏览器内就排除正文。

- `sent_confirmed`：原站正向回执匹配岗位 ID、公司、岗位名与原尝试时间（精确时间最多后移 10 分钟；51job 仅支持同日，见下文）。智联读取成功投递记录；BOSS 读取既有会话中已送达/已读的发出消息。
- `not_sent`：同一岗位详情的 ID、公司和岗位名匹配，且原站明确显示未申请控件。不会据此自动重投。
- `still_unknown`：缺失、虚拟列表未加载、分页遗漏、身份/时间不符、关闭的详情、登录/风控或不可读页面均不能证明未投。平台登录/风控/空间控制问题停止该平台后续网页访问。

这三态描述申请或既有发出动作的核验结果；`sent_confirmed` 不补造自定义招呼语或附件送达证据。与原发送回执的区别应在审查时保留。

## 审查后应用（由 Claude/操作员执行）

```bash
# 默认预览：SQLite mode=ro；不创建/迁移数据库
uv run --project mcp python scripts/apply-reconcile.py /absolute/private/reconciliation.json \
  --database "$HOME/.job-agent/jobs.sqlite3"

# 只有负责生产写入的操作员在审查后加 --apply
uv run --project mcp python scripts/apply-reconcile.py /absolute/private/reconciliation.json \
  --database "$HOME/.job-agent/jobs.sqlite3" --apply
```

应用使用 `mode=rw` 和一个 BEGIN IMMEDIATE 事务；先校验整批身份、原最新流水和队列快照，再追加 `deliveries`、`events` 和 `runs(kind=reconcile)`。任何快照冲突均整批拒绝；重跑只补尚未写入的项，相同 run_id/岗位不能替换已经应用的证据。不会改原流水或队列，不增加投递尝试/频率预留，也不创建通知。生产任务继续变化时，应重新生成只读对账文件。

沿用 v7 五态 CHECK 约束，物理状态分别是 `sent/failed/unverified`，三种业务终态保存在 `reason_code`（`kind=reconcile`）。流水 `error_detail` 与 `events.summary` 都带 run_id。新的流水沿用原流水的业务时间，实际核验时间保存在 checked_at / events，避免把历史投递计入今天。原生岗位查询优先展示最新对账终态；漏斗将 `sent_confirmed` 计为发送成功。队列与旧失败/不明历史不被清除，防止自动重新投递。

## 投递前检查

`engine.max_job_age_hours` 默认 48，可配置正数小时。岗位采集时间超过阈值（等于阈值不触发）、时间损坏或异常地位于未来时，在平台锁和会话预检后，只读打开详情，复用适配器的身份与可用性检查，关闭临时页，再进入额度预留/投递。

明确关闭记 `skipped/job_closed`，没有 attempted_at、daily/hourly 预留或发送动作；无法核验记 `pending/freshness_unknown`，等待后续运行复查。登录、风控、用户接管均停止该平台，不绕过。已有暂停、简历能力限制和历史去重规则仍先执行。新鲜岗位保留原流程。

## 历史不明回执补查（#110–#113）

已经应用的 `still_unknown` 默认不会再进入候选。显式指定原对账 run，只读取**最新流水仍属于该 run 且仍为不明**的岗位：

```bash
export JOB_AGENT_HOME=$(mktemp -d)
export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH
uv run --project mcp job-agent reconcile --retry-run <32位原run_id> \
  --database "$HOME/.job-agent/jobs.sqlite3" \
  --platform 51job --platform boss --platform zhilian \
  --output /absolute/private/history-review.json
```

候选使用当前最新流水 ID 和快照指纹；已确认、已判未投或后来有新流水的岗位不会被这个选项强行覆盖。产物继续使用 v1 结构，生产写入仍由 Claude 用经审查的本版本 `scripts/apply-reconcile.py --apply` 执行，旧版证据白名单无法处理新增证据码。

- 51job 复用 `job51.status.read_applications` 的全部申请分页。唯一岗位 ID、公司、岗位名和上海时区的申请日必须全部匹配。`application_record_day` 的 `receipt_at` 是原站申请日的零点编码，**只有日期精度**，不表示实际发送在零点发生；应用时按同日校验。在线简历名不进入产物，也不证明指定附件或招呼语送达。未匹配的申请保持不明。
- BOSS 扫描只缓存与滚动视口相交的会话行及其位置；选择时复用稳定身份投影，等待虚拟行回收完成和几何稳定后才点击既有会话。消息取岗位字段 `jobName`，不使用招聘者职务 `title`。最后消息不可核验时用 `conversation_unavailable` 保留不明，不把其他会话的消息当证据。
- 智联遍历投递成功、被查看、有意向、邀面试、不合适五个可见状态筛选及各自分页；切换后等待记录签名变化并稳定。被查看/有意向的事件时间不作为申请时间：匹配到岗位但缺原始申请时间时保留 `application_time_unavailable`。未发现更早分页或时间筛选不等于历史未投。
- 猎聘本次仅人工只读核对两个指定岗位，通用自动读取器仍未开放。停投设置不在本次修改范围。

浏览器页面清理失败不会删除已经收集的审查证据；不自动重试点击或清理。平台列表读取失败会在该读取器内缓存异常，避免对每个候选重复打开同一失败列表。
