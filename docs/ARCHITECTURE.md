# job-agent 自建引擎架构

负责人：Claude（架构与审查）。Codex 按本文开发，改动本文需要先在 PR 里说明理由。

## 一句话

job-agent 是一个本机 Python 进程。它通过 Ego Lite 操作四个招聘平台的网页，数据全部存在 `~/.job-agent/jobs.sqlite3`，对外只暴露 MCP 工具和 Telegram 推送。使用自研后端。

## 分层

```
MCP 工具 (server.py)          ← Agent 调用：preview / send / status / stop / monitor
   │
服务层 (mcp/job_agent/service/)          ← 批次确认、汇总、调度；只调下面两层的接口
   │
 ┌─┴──────────────┐
引擎 (mcp/job_agent/engine/)    监听 (mcp/job_agent/monitor/)
队列·状态机·限额      拉回复·去重·推 TG
   │                     │
平台适配器 (mcp/job_agent/platforms/<平台>/)   ← 一个平台一个目录：apply.py（投递）、replies.py（回复）、pages.py（选择器与解析）
   │
浏览器驱动 (mcp/job_agent/browser/ego.py)      ← 唯一调用 ego-browser 的地方
   │
数据 (mcp/job_agent/store/)                   ← 唯一访问 SQLite 的地方
```

规则：

- 上层只依赖下层的接口，不跨层调用。适配器不碰数据库，引擎不碰 `ego-browser`。
- 页面选择器和解析逻辑只放 `pages.py`，并用 `mcp/tests/fixtures/platforms/` 的离线夹具做单测。平台改版时只改这个文件。
- 所有行为进 `config.yaml` 的 `engine:` 段，默认保守。

## 数据：一个库

`mcp/job_agent/store/` 是运行数据的访问入口；schema v7 将监听状态、通知、审批和 MCP 审计并入 `jobs.sqlite3`。服务传入业务数据，store 负责 SQL、事务、只读快照和旧文件迁移。

| 表 | 用途 | 写入模块（经 store） |
| --- | --- | --- |
| `jobs` | 岗位身份、JD、HR 活跃度；唯一键 `(platform, platform_job_id)` | collect、导入器 |
| `deliveries` | 只追加的投递尝试、结果、招呼语与附件证据；`kind=reconcile` 的原站核验结果 | delivery engine、显式对账应用 |
| `delivery_queue` | 每个岗位的当前五态、原因与批次 | delivery engine |
| `batches` | 不可变预览、确认和执行进度 | deliver、MCP preview/send |
| `approvals` | 已哈希的一次性确认令牌、TTL、消费标记 | mcp_backend |
| `delivery_identities`、`delivery_attempts` | 冷静期身份和尝试时间 | delivery engine |
| `replies`、`replies_legacy_duplicates` | 平台消息去重与迁移时保留的旧重复证据 | monitor ingest、导入器 |
| `source_history` | 导入历史与显式记录的求职事件 | 导入器、read_tools.append_event |
| `scoring_runs`、`scoring_items`、`job_scores` | 评分任务、逐岗位请求证据及结果 | scoring |
| `interview_invitations` | 待本人确认的邀约结构化信息 | reply_classification |
| `monitor_baselines`、`monitor_conversations` | 监听首次基线和逐会话游标 | monitor delta/ingest |
| `monitor_platform_state` | 每个平台（及 space）的当前诊断；runtime 读取时关联此表 | monitor poller/runtime |
| `monitor_settings` | 全局通知 cutoff/backfill 策略和旧聊天链接；单行 | monitor notifications、迁移器 |
| `runs` | daily 报告、监听运行、心跳去重、浏览器启动冷却和后台 worker 运行快照 | daily、monitor runtime、heartbeat、browser、tasks |
| `worker_state` | 每类后台任务指向 `runs` 的当前行；不重复保存快照 | tasks |
| `worker_requests` | 后台任务请求和合作停止标记 | tasks |
| `notifications` | 唯一去重键、目标渠道、正文、pending/sent/failed、尝试和回执（含告警时间与平台） | daily、delivery、monitor notifications、observability、notify |
| `events` | 只追加的脱敏 MCP 审计流水及带 run_id 的对账审计 | activity、显式对账应用 |
| `collection_cursors`、`collection_usage`、`collection_platforms`、`collection_days`、`collection_filters` | 采集分页、配额和过滤理由 | collection repository |
| `platform_pauses`、`operation_rate`、`operation_rate_state` | 持久暂停、限速预留和节奏 | safety、rate limiter |
| `store_mode`、`schema_migrations`、`schema_extensions`、`legacy_imports` | 模式隔离、schema/扩展版本和幂等导入账本 | store |

`approvals` 保留独立表：它的主键是令牌哈希并带 TTL，与 `batches` 的冻结预览契约不同；两者在同一个库里，确认校验和令牌消费共用一个事务。`monitor_settings` 保存全局 cutoff，避免把同一策略复制到每个平台。通知回执复用 `notifications`，不再维护第二份去重文件。

投递引擎在更新队列和追加最终结果的**同一事务**里写入批次汇总通知意图。同一事务用只读渲染器冻结原有汇总正文，独立发送步骤发送该正文并保存回执；监听和心跳也会补发崩溃后留下的意图。持有 flock 发送锁期间不持有 SQLite 写事务，避免网络等待阻塞其他写进程。远端已收件而本地回执尚未提交的崩溃窗口仍可能重复，现有通知供应商没有可复用的幂等键。

已有旧版本库必须显式使用 `Store(migrate=True)` 升级；普通打开会拒绝旧 schema，部署迁移步骤负责显式选择。迁移器只在正式 `jobs.sqlite3` 上导入旧状态；SQL 数据与校验和账本同事务提交，再把源文件改名为 `*.migrated-YYYYMMDD`。归档失败可重试；无效源文件保留原样并报错。升级必须先由 deploy.py 停止旧写入进程。部署快照同时保存旧库和旧状态，`--rollback --restore-database` 恢复原 schema 和旧文件，保留 v7 数据库安全备份及迁移归档。WAL、30 秒 busy timeout 和每 worker 独立连接沿用 #87。

daily 复用 #105 的 `run_id`，完整保留 `last_failure`、中断状态和续跑 checkpoint。结果事务同步保存 daily 报告与单条可更新的汇总意图，正常结束时冻结最终正文；若进程中断，监听或心跳在 daily 锁释放后补发已保存的正文。告警回执导入 `alert_at`、`platform`，按文件顺序应用 `alert_reset`；恢复仅清除对应平台的告警冷却，不清除回复去重和风控暂停。

测试包入口自动把未设置或解析到真实生产目录的 `JOB_AGENT_HOME` 替换为临时目录；测试标记传给子进程。配置和 Store 还会独立拒绝测试进程访问生产目录（含符号链接与显式数据库路径）。真实生产库升级必须在 `deploy.py` 的安装运行时子进程中进入 `deployment_migration()` 上下文，单独传 `migrate=True` 不足以授权；普通正式服务只打开已升级的库。临时副本仍可显式迁移。配置、`config-history/` 审计历史、日志、公司研究文档和进程锁保留文件形式。

## 浏览器：任务空间

依据 `docs/browser.md`「任务空间生命周期」：

- 空间按名字取：投递用 `job-agent 投递`，监听用 `job-agent 监听`。两个空间分开，互不影响。
- 每个空间保留一个锚点标签页（`about:blank`），永远不关。每个岗位开临时标签页，用完关掉。
- 整个进程退出也不 `finish`；仅关闭自己创建的临时页，保留锚点与 Agent 所有权。
- 空间不存在 → 新建同名空间。空间被用户接管、或者同名空间属于用户 → 停下，TG 通知，不抢。
- 平台登录态：Ego 同一个 profile 共用 cookie，新建空间一般不用重新登录。预检发现没登录 → 停该平台，TG 通知用户登录。

## 各平台投递方式（依据 P0 取证）

| 平台 | 招呼语怎么到 HR | 长度上限（实测） | 附件 |
| --- | --- | --- | --- |
| BOSS | 平台默认招呼已关，我们在聊天框发全文 | 1000 | 不需要 |
| 智联 | 申请时平台自动发默认招呼语（已设成我们的全文，500 上限）；引擎**不再发第二条** | 设置 500、聊天框 2000+ | 待 P1 确认 |
| 猎聘 | 平台先发一句预设，引擎随后在聊天框发全文 | 800 | 待 P1 确认 |
| 51job | 没有招呼语设置；能发首条消息就发，不能就只投简历 | 未测 | **先申请，再用 cvLogId 选附件**，选完读回；读回失败记 `unverified` 并 TG 提醒手动补 |

配置里每个平台有 `greeting_mode`：`send_message`（引擎发）/ `platform_default`（靠平台默认，不发）/ `none`。

## 投递流程

```
preview_greetings → batches（待确认）
用户说「发送」 → send_greetings(token) → batches（已确认）→ 入队 delivery_queue
引擎 run：
  预检（空间、登录、附件）
  每个岗位：开临时页 → 是否已投？是→skipped
          → 落盘 unverified/interrupted（防崩溃重投）
          → 申请 / 发招呼 / 选附件（顺序由适配器决定）
          → 读回回执 → sent 或 unverified
          → 关临时页 → 按间隔等待
结束 → TG 汇总（成功 / 失败原因 / 链接）
```

- 结果不明一律 `unverified`，并停该平台本批剩余岗位。不自动重投。
- 每日上限、间隔按平台配置。默认：每平台每天 200，间隔 20–40 秒随机。
- 出现验证码、风控提示 → 停该平台，TG 通知。

## 回复监听

- 独立进程（launchd），每 N 分钟（默认 10）轮询四个平台的聊天列表。
- 只认已投岗位，排除自己发的和系统卡片，按 `(platform, message_id)` 去重写 `replies`，新消息推 TG（公司、岗位、原文、链接）。
- 不自动回复 HR，不自动发简历。
- `watch.py` 从 `replies` 表通知，`notify.py` 提供用户通知传输。

## 运行与迁移

所有入口直接使用自研服务。历史状态保留，配置迁移与已删除接口见 [BTY-10](migrations/BTY-10.md)。原站验收、服务安装和生产部署仍是独立操作。

## 第二轮生命周期修正

进程结束也不调用 `finish`。shutdown 只关闭本实例创建的临时页，锚点页始终保留，任务空间持续由 Agent 持有。不得关最后一页或接管用户空间。旧的“退出 finish + keep”规则已废止，因为 keep 仍会把空间交还用户。

## 采集（P3，接口见 mcp/job_agent/platforms/base.py）

- 每个平台 `mcp/job_agent/platforms/<平台>/search.py` 实现 `JobSource`（search 列表页 + detail 详情页），选择器和解析放 `pages.py`，用夹具单测。
- `mcp/job_agent/collect/`：采集调度。按配置 `collect:` 段（关键词列表、城市列表、每平台页数上限、间隔、每日上限）逐页搜，`(platform, platform_job_id)` 去重写 `jobs`，再补 JD。只做硬过滤（配置里的薪资下限、排除词、屏蔽公司），不做 AI 打分。
- 采集用独立空间 `job-agent 采集`，和投递、监听分开。遇到验证码/风控就停该平台并 TG 通知，不绕过。
- 采集是只读动作，但会产生大量页面访问：默认保守（每页间隔 8–15 秒随机、每平台每天 20 页）。

## 全部 MCP 工具切到自研

`engine.backend` 只接受 `own`。所有 MCP 工具都只读写 `~/.job-agent/jobs.sqlite3` 和自研引擎，不再调 `127.0.0.1:8686`：search_jobs、get_job、jd_requirements、briefing、status、funnel_stats、company_history、check_collection/start_collection、preview/send、delivery_status/stop、监听启停。`preview_scoring` / `start_scoring` / `scoring_runs` 已迁入自有评分服务。旧代评接口已删除。


## 评分（#48，11-26）

落实 Issue #1 的 11-26 分层边界：`server.py` 只路由，`service/scoring.py` 编排材料、授权、串行模型请求和结果校验，`store/scoring.py` 管理所有评分 SQL。配置在 `config/scoring.py`，由主配置的 `scoring:` 段加载。评分无需浏览器或平台选择器。

- schema migration 4 在唯一 SQLite 内新增 `scoring_runs`、`scoring_items`、`job_scores`，通过现有 `schema_migrations` 事务执行；不继续增加隐式 schema extension。v3 已有 jobs/投递/回复和旧评分字段原样保留。先备份并升级旧监听/每日运行目录，再迁移生产库；旧运行时拒绝 v4 是明确的升级边界。
- preview 是只读快照，列出预计付费请求数、全库待评分数和跳过原因。start 只接受布尔 `confirm=true`，校验材料与凭据后原子占用岗位，复用 `service/tasks.py` 启动 scoring worker；同一个岗位不能被并发重复评分。
- worker 使用确认时的主简历/经历和 JD 快照；执行前复核评分配置哈希。缺 JD、已过滤、已删除和已导入评分的岗位不请求模型。运行完成移除材料快照，保留输入哈希和模型/服务等级/用量；简历、JD、密钥不进入 Git 或任务日志。
- 原生分数通过查询投影参与搜索、详情、briefing、分数分组和每日候选排序；不会覆盖导入的 legacy_score/reason，不会改变投递队列、授权、风控暂停或发送规则。
- 每批上限默认10（配置可调低），串行单次请求，不自动重试。首先单条查看额度；后续余额缺失/不足就阻塞该项。模型 JSON 必须有合法整数分数和非空理由；失败不伪造零分。未知请求结果保留占位，人工核对后再处理，进程重启不自动付费重放。
- `setup_check` 检查本地资源及配置的 Ego 登录会话。`daily.enabled`、回复发送与跟进授权保持现有边界。


## Python 包与模块边界

运行时代码仅安装到 `job_agent` 命名空间，CLI 为 `job_agent.cli:main`，MCP 以 `python -m job_agent.server` 启动。配置模型集中在 `job_agent/config/`，原有 YAML 和环境文件格式保持兼容。

| 模块 | 责任 |
| --- | --- |
| `context_tools.py` / `delivery_tools.py` | 注册上下文工具与投递/统计工具；工具名称及 schema 保持不变 |
| `dashboard_html.py` | 纯 HTML/SVG 渲染，不读取数据库 |
| `service/dashboard.py` | 只读快照与统计投影，原子生成 HTML |
| `approvals.py` | 单次审批 token 兼容入口，存入 jobs.sqlite3 的 approvals |
| `service/deliver.py` | 批次预览、授权确认、依赖构造和结果汇总 |
| `store/delivery.py` | 持久队列、批次、结果与同事务通知意图 |
| `service/read_tools.py` | 类型化本地读取；MCP 调用方直接调用 search_jobs/get_job |

部署从已提交的锁文件安装非 editable 包；启动脚本导入安装的 `job_agent`，不依赖平铺的通用包名。

## 运行编号与错误记录

`observability.run_scope` 用 contextvar 保存本次 `run_id`（32 位 UUID hex）。每日任务、独立投递批次、独立评分各创建编号；嵌套采集、评分、投递和通知继承当前编号。一轮监听强制创建新编号，长期监听进程不会把所有轮次混成一次运行。评分子进程恢复数据库 `scoring_runs.id`，不使用 worker 进程编号代替评分编号。daily 当前仍不自动发起付费评分；离线夹具显式插入评分步骤验证上下文传播，不增加生产付费行为。

日志通过 LogRecord factory 自动附带 `run_id`、`platform`、`stage`，轮转格式直接输出这些字段。异常记录含 `error_type`、最多 300 字的安全消息、原因码、平台、阶段和最内层三个 `文件名:行号`，不输出源码行、变量或完整堆栈路径。平台/阶段尚未确定时为 `all` / `startup`；不能凭异常类型推断失败平台。原始异常可能包含 YAML 值、HR 姓名、消息正文或凭证，因此只保留已知基础设施消息和结构化错误码，其他正文隐藏并通过行号定位。不会记录模型 prompt、返回正文或 token 追踪。

公共异常告警复用 `notified.lock` / `notified.jsonl` 的每渠道回执；去重身份不含 run_id，发送成功后才写回执。无法识别的消息还按最内层来源行区分，避免把两个隐藏原因误当同一事件；同原因在 `observability.heartbeat_cooldown_minutes` 冷却期内只告警一次（默认 120 分钟），到期可再次告警；平台检查通过后追加恢复标记，清除该平台的告警去重状态，下次故障立即重新告警。恢复标记只影响告警，不自动解除风控暂停。旧告警回执没有时间戳，升级后允许重新告警一次。`notified.lock` 竞争最多等待 5 秒；超时只入现有 outbox 等待后续同渠道通知触发补发。告警发送失败只记日志并保留 outbox，不抛错、不改变平台检查或 daily 结果。当前仍扫描 JSONL，迁库由 #100 处理。监听保留已有阶段/原因回执和失败阈值。告警用中文说明阶段、原因并提供处理建议。Telegram 的末尾保留 `运行编号：xxxxxxxx`；长消息裁剪会预留编号空间，持久重试保留原消息编号。

投递审计沿用 `deliveries.error_detail`，不添加列、表或 schema 版本。运行期间写入的值为 JSON：`{"run_id":"...","detail":"原有文本"}`；既有 JSON（例如 `resume_selection`）在顶层追加 `run_id`，其余键保留。无运行上下文的旧记录保持原格式。读取方需兼容历史文本及 JSON，不能把 JSON 字符串直接当错误消息。

每日任务在获得 `daily.lock` 后、配置校验和数据库初始化前写 `running`；结束写 `completed`、`deferred` 或 `failed`、原因码和 run_id；`KeyboardInterrupt` / `SystemExit` 保留 `interrupted`，同时保存 `last_failure`，断点续跑和心跳继续按未完成处理。异常保留脱敏诊断及 `finished_at`；后续运行在 `last_failure` 保留最近失败，`status.daily` 和 `setup_check.daily_last_run` 可见。已有当日完成凭据直接返回；锁竞争的第二个调用记录异常，不覆盖正在运行者的状态。状态文件仍为 JSON，由 #100 单独处理迁库。强制杀进程无法执行 finally，仍由现有心跳检测无进展。

`funnel_stats.daily_trends.rows` 从现有表按配置时区的日期和平台汇总，每个岗位每天只取最后一条非 pending 结果（成功、不明、跳过、失败），排除旧导入快照，避免把副作用前的 `interrupted` 和最终回执计成两次结果。`top_reasons` 为这组最终结果的前三个原因，`quota_used` 合并 `operation_rate` 投递预留与预留机制启用前的 `delivery_attempts`，口径与限流器一致；同时给出当前配置的额度上限、剩余额度及 `collection_usage` 已用页数。历史日额度上限使用当前配置估算，不伪造历史配置；未消耗额度的副作用前失败仍可计入失败结果。无需额外存储。

离线联调：`uv run --project mcp python mcp/tests/fixtures/observability_daily.py /tmp/job-agent-observability-smoke`（使用新的空目录）。采集使用 FakeSource/FakeBrowser，评分响应、发送动作与 Telegram 网络均替换为夹具；SQLite、daily 编排、评分解析、审计写入和通知截断逻辑保持真实。通过输出编号在该目录 `logs/daily.log` 中 grep，核对整条链路和数据库审计；该夹具不会打开浏览器或访问招聘平台。
