# 使用与工具参考

默认 `engine.backend: own`，所有业务数据在 `~/.job-agent/jobs.sqlite3`。MCP、CLI 共用 service；平台页面只由适配器和 Ego Lite 驱动访问。TG 当前是通知出口，指令入口计划中。

## 注册到 Codex / Claude Code

在本仓库根目录执行以下命令，让 `pwd` 展开成绝对路径（不依赖 Agent 的工作目录）。注册后重新打开客户端会话。

```bash
codex mcp add job-agent \
  --env "JOB_AGENT_HOME=${JOB_AGENT_HOME:-$HOME/.job-agent}" \
  --env JOB_AGENT_CALLER=codex \
  -- uv run --project "$PWD/mcp" python "$PWD/mcp/server.py"

claude mcp add job-agent --transport stdio --scope user \
  --env "JOB_AGENT_HOME=${JOB_AGENT_HOME:-$HOME/.job-agent}" \
  --env JOB_AGENT_CALLER=claude-code \
  -- uv run --project "$PWD/mcp" python "$PWD/mcp/server.py"
```

已在本机通过 `codex mcp add --help` / `claude mcp add --help` 核对参数；这里给出注册命令，不会自动改动已有客户端注册。

## 工具

查询用 `search_jobs`、`get_job`、`jd_requirements`、`briefing`、`company_history`、`funnel_stats`；经历与公司缓存用 `experience_*`、`company_get` / `company_save`。`resume_import` 只提取本地文本，不上传或保存文件。

采集先 `check_collection`，授权后 `start_collection(confirm=true)`。评分先预览请求数，再明确授权付费。投递先 `preview_greetings` 展示完整不可变批次，用户确认后用返回的 token 调 `send_greetings`；名单或文案变更须重新预览。投递状态用 `delivery_status`，停止用 `stop_delivery`。

监听需独立授权 `start_reply_monitor(confirm=true)`，状态与停止为 `reply_monitor_status` / `stop_reply_monitor`；own 监听不自动回复或发简历。通知默认关闭，按配置启用自己的 Telegram 渠道。兼容后端工具的不同语义见 [BossHunter](bosshunter.md)。

### own 评分

在 `config.yaml` 的 `scoring:` 配置主简历的 UTF-8 文件路径。经历从 `$JOB_AGENT_HOME/experience/*.md` 读取；没有经历文件时只用主简历，不虚构补齐。key 默认从 ai-vault 的 `personal-infra/openai/OPENAI_PLATFORM_API_KEY` 读取，也可选择 `key_source: env` 和 `api_key_env`；模型、地址和来源均可配置，配置只写名称与路径，不写密钥值。

1. `preview_scoring(job_ids)` 先显示 `estimated_requests`、全库 `pending_count` 和跳过原因；不访问模型、不写库。
2. 向用户展示请求数，取得授权后调用 `start_scoring(job_ids, confirm=true)`，返回后台任务和运行 ID。默认最多 10 个岗位/批，按每个岗位一次请求串行执行，无自动重试。
3. `scoring_runs()` 读取最近 20 批的逐项状态、用量和 `remaining_tokens`；`get_job` 返回分数、理由与 trace。分数参与查询/排序、统计和每日候选排序，不改变投递状态，也不自动发送。

默认使用 `gpt-6.1-sol`、`service_tier: priority`，请求只传 `max_completion_tokens`，不传 `max_tokens` 或 `thinking`。先用单条取得 `x-ratelimit-remaining-tokens` 再扩批；同一批后续请求在上一条余额未知或不足时标为 `blocked/rate_limit_budget`，不等待、不重试。预算采用 UTF-8 字节数上界加输出上限和余量，并非精确 tokenizer 计数。

采集写入的新岗位自动出现在待评分统计中，只有有 JD 且未过滤的岗位可评；每日任务不会自动付费评分。导入的旧分数和理由（含有理由的零分）保留并跳过，不提供自动重评。缺 JD 先补全再评。`failed/invalid_model_result` 不落分，用户可重新预览授权；超时、连接中断或进程意外退出的已发出请求保留未知状态/占位，防止重复收费，须人工查证后处理，不能直接重跑。

`setup_check` 在 own 模式检查本地库、评分凭据、主简历和 Ego 配置，不探测 BossHunter。它仍会报告缺少经历、空间未验证等具体缺项。`pending_evaluations` / `submit_evaluations` 是旧 BossHunter 的 Agent 代评接口，在 own 模式明确 unsupported；付费评分使用上面三个工具。本表其他 BossHunter 描述仅适用于兼容后端。

## 数据与日志

`activity.jsonl` 只留参数长度/数量、调用方和结果摘要。`notified.jsonl` 是逐渠道通知去重回执，`monitor-runtime.json` 是监听状态。私密材料、配置、消息与 jobs.sqlite3 都留在 `JOB_AGENT_HOME`，不放入仓库。投递审计会在私有数据库保存完整文案；脱敏调用日志不等于数据库不含私密数据。

## 测试

在 `mcp/` 执行 `PATH=$HOME/.local/bin:$PATH uv run python -m unittest discover -s tests -v`；可用 `uv build` 构建 wheel 和 sdist。[部署说明](deploy.md) 包含 opt-in 部署演练。

### #46 登录预检、频率和输入

自研后端每次采集、投递、监听前，在配置的采集空间只读检查账号。没有正向登录证据时跳过该平台，TG 提示在「job-agent 采集」登录；账号类型仅作只读记录。`engine.platforms.<platform>.expected_account_type` 默认 `any`，只有显式设置 `student` / `professional` 且与检测结果不符才跳过；猎聘学生版通过默认预检。`setup_check` 的 `platform_sessions` 返回登录结果、账号类型和原因码。检查会打开并关闭临时页面，不操作登录或账号切换。

每个平台可以分别配置 `engine.platforms.<平台>.rate`（投递）和 `collect.platforms.<平台>.rate`（采集），字段如下：

```yaml
engine:
  platforms:
    boss:
      daily_limit: 200  # rate.daily_limit 未配置时沿用此值
      interval_seconds: 30  # 旧字段仍可读取；新 rate.interval_seconds 优先
      rate:
        interval_seconds: [45, 120]
        hourly_limit: 20
        rest_every: [8, 15]
        rest_seconds: [300, 900]
        allowed_hours: ['09:00', '21:00']
        timezone: Asia/Shanghai
collect:
  platform_timeout_seconds: 600
  platforms:
    boss:
      rate:
        interval_seconds: [45, 120]
        hourly_limit: 20
        # daily_limit 未指定时继承 daily_page_limit；详情也消耗操作额度。
        rest_every: [8, 15]
        rest_seconds: [300, 900]
        allowed_hours: ['09:00', '21:00']
        timezone: Asia/Shanghai
```

全新配置默认随机间隔 45～120 秒。旧投递 scalar 间隔 x 兼容为 `[x, max(2*x,120)]`；旧采集区间原样继承，显式配置 `rate` 后迁移到新值。旧 `daily_limit` / `daily_page_limit` 仍生效。投递一条算一次操作；采集打开列表、内部翻页（含为后续页重放的列表）、打开详情各算一次。投递与采集各有独立小时/日额度，共享该平台的持久化间隔/连续操作休息计数；并发进程在 SQLite 中原子预约。监听复用间隔、休息和小时上限，不消耗投递每日额度。窗口为左闭右开，支持跨午夜；窗口外、小时/日额度耗尽时保留待处理并继续其他平台，不等待下一小时/次日。预检页面不消耗投递额度。

限频预约发生在外部操作前，失败也消耗本次额度，重启不会清零。旧投递历史会用于首次限额检查。风控暂停持久化在与 daily 相同的 `platform_pauses` 表，不随日期、重启或预检成功解除。显式执行 `job-agent resume-platform boss`（可替换为其他平台）才会先检查登录/账号类型，再解除该平台暂停。该命令不代表回执核验通过，不会修改任何投递历史或自动重投。

鼠标输入统一由 Ego 的 `mouse.move/down/up` 执行，每次完整轨迹只发起一次 `ego-browser nodejs`。BOSS 虚拟会话列表使用稳定身份只读投影取可见框，避免滚动导致索引换位。终点在目标框缩至 60% 后随机选择，起点沿用该页上次位置（首次在视口内随机）；纯函数使用种子生成稀疏贝塞尔路径，共 5～8 个鼠标点（含起点/终点）；冲过头点位于目标之外，再回到目标。输入逐字发送。可在 `browser.human_input` 中覆盖：`shrink: 0.6`、`duration_ms: [250,900]`、`steps: [3,6]`（中间点数，犹豫/冲过头占用其中一点）、`step_ms: [50,200]`、`hold_ms: [50,150]`、`typing_ms: [60,180]`、`noise_px: 1.5`、`overshoot_probability: 0.3`、`overshoot_px: [2,12]`。只记录时间、坐标、平台和限频结果，不记录正文、完整 URL 或凭据。

采集与投递每轮随机打乱可用平台，每平台操作一次（一个列表加载/翻页/详情或一个岗位），冷却中的平台直接让出执行权。暂停、未登录的平台不参与后续轮换。只有整轮都无进展才短暂等待。采集使用 POSIX 主线程的每平台累计时间预算，包含预检、自身频率等待和内部翻页，不计其他平台的 I/O 时间；Ego 子进程超时也受剩余时间限制。超时记录 `collection_timeout` 后继续下一平台；通过现有 daily 调用采集返回后仍可继续投递。MCP 采集本来就在 CLI 子进程中执行；不在不可取消的后台线程里跑采集。第四轮已 rebase 到 `d47fb5c`；daily 将同一阶段、附件配置兼容的跨平台岗位放入同一批次，复用引擎的随机轮换与持久限频。

配置迁移/发布前应备份 `~/.job-agent/config.yaml` 为带时间戳的文件。旧版本监听程序只选择自己的配置段，但仍严格检查 `browser` 字段；它不认识 `browser.human_input`，因此先升级监听运行时，再写这个字段。新字段加入配置后也需要同时升级会读取完整配置的旧 daily 运行时；`daily.yaml` 的 `enabled` 保持 false，等待用户确认后开启。

迁移工具默认只预览限频字段，不输出其他用户配置：`mcp/.venv/bin/python scripts/migrate-rate-config.py`。所有读取完整配置的运行时升级后，再显式加 `--apply`；会先生成时间戳备份，再原子替换并回读校验。迁移保留旧间隔、日额度和其他配置的值，仅补新 `rate` 与平台采集超时；不设置 `browser.human_input`，不接触 `daily.yaml`。

第 2 轮补充：BOSS 默认日投递额度 140，智联 90，其余平台沿用原默认值。显式更大额度保留，`setup_check.platform_limits` 提示 BOSS 超过 150 / 智联超过 100 的配置。

详情页先随机停留并滚动，配置位于 `browser.human_input`：`detail_seconds: [3,12]`、`detail_scrolls: [1,3]`。鼠标 `hesitation_probability: 0.05`，回退已移动距离的 `hesitation_backtrack: [0.1,0.3]`，停顿 `hesitation_ms: [100,400]`；操作额外停顿 `long_pause_probability: 0.03`、`long_pause_seconds: [3,10]`。犹豫时长额外加在基础轨迹时间上。`mouse_sample_window: 20` 保存当前驱动最近 N 次鼠标移动调用的平均耗时；无样本时按 `mouse_point_ms: 1100` 毫秒估算，预算至少使用此保守基线，避免首次快速调用低估下一次限速。手势预算为计划鼠标点数 × 每点估算耗时，加 `gesture_margin_ms: 5000`、计划停顿及按下/松开开销；外层 Ego 运输超时在原页面预算上另加手势、逐字间隔和额外停顿预算，页面等待本身仍从调用时开始。采集总硬截止仍有效。日志包含 `planned_points`、`completed_points`、实测 `duration_ms`、`point_avg_ms`、`rolling_point_ms`、`budget_ms` 和可信事件 `isTrusted`；进度记录可在运输超时时保留已完成点，无法取得的事件仍标为 unknown。纯几何与调度测试支持随机种子。

岗位预检同时等待可操作按钮与明确的关闭/下线/停止招聘状态；关闭岗位记录为 `skipped/job_unavailable`。点击会先居中滚入目标并等待矩形稳定，提交前再次核对实际命中元素；错误区分 `target_geometry`、`target_recheck`、`mouse.down`、`mouse.up` 与 `post_click_observation`，不重放失败点击。智联结果新标签页纳入只读检查，原站申请回执独立于成功弹窗和招呼语回执。

`verify-receipt` 对智联同时检查原站申请记录和准确岗位的「立即投递」状态，只有明确 `not_applied` 才能由显式操作者调用 `requeue_verified_not_applied` 追加审计并回退待投；核验证据绑定岗位、尝试、最新历史 ID，五分钟有效。不改写历史，新一轮不明结果继续拦截重投。通知入口统一从进程环境读取密钥，缺失时读取权限为 600 的 `daily.env.json`，因此 `deliver --notify` 与 daily 使用同一来源。

### HR 活跃度、面试邀约与招呼语效果

schema v6 在自己的 jobs 库保存 `hr_activity`（now / today / three_days / week / unknown / inactive）、`hr_activity_raw`、`experience`、`education`。仅 HR 的在线/活跃标记或明确的招聘行为标签作为活跃证据；职位发布时间、回复率、没有在线标记都不代表不活跃。平台页面未提供时保存 unknown。BOSS 的在线布尔标记、智联 staffCard 的活跃文字、51job 的在线/招聘行为标签分别在平台层解析；当前猎聘列表没有 HR 标记时为 unknown。

自动预览和每日投递在既有新旧岗位批次内部按活跃度排序，再按可选经验偏好、分数、采集时间排序。默认 `engine.minimum_hr_activity: null` 不过滤；可设 now / today / three_days / week，启用后 unknown 也不通过，显式预览同样校验。`engine.experience_priority: [应届, 经验不限, 1-3年]` 可以优先这些经验要求，默认空列表不偏好。每日旧岗位优先策略、冷却期和投递配额继续生效。

监听只分类新收到的完整原站消息：面试邀约、索要简历、拒绝、询问、其他。明确规则先判；默认 `engine.monitor.classification_model_enabled: false`，开启后才为规则未覆盖的消息调用 `scoring` 的同一模型、service_tier、key 来源和超时。`classification_model_limit` 默认每轮全平台最多 5 次、上限 30；调用失败或无效输出保存 other、置信度 0 和错误标识，不重试。导入历史、节选与仅状态记录不会被当作模型邀约证据。分类、来源、置信度和模型名写入 replies，分类队列随回复持久化；落库后崩溃可继续，模型派发后崩溃则保守记为 worker_interrupted，不重复付费。

`interview_schedule()` 返回待用户确认的邀约列表，包含岗位/聊天链接、消息时间、时间原文和形式（online / onsite / phone / unknown）。相对时间保持原文，不猜日期；邀约不会自动被接受或创建日历事件。TG 使用“面试邀约（待确认）”单独标题，沿用原回复通知的分块回执，避免重复推送。

`greeting_stats()` 复用 #59 的 `greeting_version()` 内容哈希。每个岗位归于首次成功投递的版本，重复投递/回复不增加分母；仅统计该投递之后的完整原站回复与面试邀约。历史缺失正文单列 unknown，不猜模板 ID。返回版本汇总与平台×版本分组、回复率、邀约率、回复→邀约率和 Wilson 95% 置信区间。默认 `analytics.greeting_min_samples: 20`，不足则标注“样本不足”。平台、岗位和观察时长会影响结果，区间不能证明文案的因果效果。

`funnel_stats(group_by='greeting_version')` 支持按版本分组；原有平台分组也增加 `interview_invited`、`invitation_rate`、`reply_to_invitation_rate`。已确认约面仍单列 `interview_scheduled`，不把邀约当作确认。`observed_conversion` 提供严格按首次成功投递归因的转化率，`greeting_stats` 提供分版本区间。`briefing()` 包含邀约待办和基于样本量的文案建议。以上查询工具只读，不迁移数据库、不调用模型、不访问原站。

部署/回滚仍使用 `scripts/deploy.py`：v5 → v6 迁移事务化；返回 v5 前必须恢复部署前数据库备份。开发验证可用 `JOB_AGENT_DEPLOY_REHEARSE=1 uv run python -m unittest discover -s tests -p test_deploy.py -q`，该演练只在临时目录运行 FakeLaunchd 和假浏览器，不操作真实 launchd 或生产服务。
