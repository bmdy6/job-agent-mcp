# job-agent 自建引擎架构

负责人：Claude（架构与审查）。Codex 按本文开发，改动本文需要先在 PR 里说明理由。

## 一句话

job-agent 是一个本机 Python 进程。它通过 Ego Lite 操作四个招聘平台的网页，数据全部存在 `~/.job-agent/jobs.sqlite3`，对外只暴露 MCP 工具和 Telegram 推送。不依赖 BossHunter。

## 分层

```
MCP 工具 (server.py)          ← Agent 调用：preview / send / status / stop / monitor
   │
服务层 (mcp/service/)          ← 批次确认、汇总、调度；只调下面两层的接口
   │
 ┌─┴──────────────┐
引擎 (mcp/engine/)    监听 (mcp/monitor/)
队列·状态机·限额      拉回复·去重·推 TG
   │                     │
平台适配器 (mcp/platforms/<平台>/)   ← 一个平台一个目录：apply.py（投递）、replies.py（回复）、pages.py（选择器与解析）
   │
浏览器驱动 (mcp/browser/ego.py)      ← 唯一调用 ego-browser 的地方
   │
数据 (mcp/store/)                   ← 唯一访问 SQLite 的地方
```

规则：

- 上层只依赖下层的接口，不跨层调用。适配器不碰数据库，引擎不碰 `ego-browser`。
- 页面选择器和解析逻辑只放 `pages.py`，并用 `mcp/tests/fixtures/platforms/` 的离线夹具做单测。平台改版时只改这个文件。
- 所有行为进 `config.yaml` 的 `engine:` 段，默认保守。

## 数据：一个库

`mcp/store/`是唯一的数据层。`mcp/engine/store.py` 里 C 的参考 SQLite 实现要删掉，改成在 `mcp/store/` 上实现 `DeliveryStore` 协议：

- `jobs`：岗位，唯一键 `(platform, platform_job_id)`。
- `delivery_queue`（新表，schema v2）：每个岗位一行，引擎的当前状态 `pending / sent / failed / unverified / skipped` + 原因码 + 尝试时间。
- `deliveries`：每次尝试追加一行（审计流水，含招呼语全文、附件名、批次 ID）。不改不删。
- `replies`：回复事件，唯一键 `(platform, message_id)`。
- `batches`（新表）：用户确认的批次。`preview_greetings` 生成，内容不可变（岗位列表、每平台招呼语全文、附件名、配置快照的哈希）。`send_greetings` 只能执行已确认的批次。
- 跨进程锁：`exclusive()` 用文件锁（flock），锁住整个 run。

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
- 现有 `watch.py` 和 `notify.py` 改成读 `replies` 表，不再读 BossHunter。

## 切换

1. 适配器写完、单测过 → 每平台用 1 个用户确认的岗位真投验证。
2. 验证通过 → `send_greetings` 等 MCP 工具切到新引擎，BossHunter 只留监听。
3. 新监听跑一天，和 BossHunter 一致 → 停 BossHunter，README 去掉依赖。

## 第二轮生命周期修正

进程结束也不调用 `finish`。shutdown 只关闭本实例创建的临时页，锚点页始终保留，任务空间持续由 Agent 持有。不得关最后一页或接管用户空间。旧的“退出 finish + keep”规则已废止，因为 keep 仍会把空间交还用户。

## 采集（P3，接口见 mcp/platforms/base.py）

- 每个平台 `mcp/platforms/<平台>/search.py` 实现 `JobSource`（search 列表页 + detail 详情页），选择器和解析放 `pages.py`，用夹具单测。
- `mcp/collect/`：采集调度。按配置 `collect:` 段（关键词列表、城市列表、每平台页数上限、间隔、每日上限）逐页搜，`(platform, platform_job_id)` 去重写 `jobs`，再补 JD。只做硬过滤（配置里的薪资下限、排除词、屏蔽公司），不做 AI 打分。
- 采集用独立空间 `job-agent 采集`，和投递、监听分开。遇到验证码/风控就停该平台并 TG 通知，不绕过。
- 采集是只读动作，但会产生大量页面访问：默认保守（每页间隔 8–15 秒随机、每平台每天 20 页）。

## 全部 MCP 工具切到自研

`engine.backend: own` 时，所有 MCP 工具都只读写 `~/.job-agent/jobs.sqlite3` 和自研引擎，不再调 `127.0.0.1:8686`：search_jobs、get_job、jd_requirements、briefing、status、funnel_stats、company_history、check_collection/start_collection、preview/send、delivery_status/stop、监听启停。`preview_scoring` / `start_scoring` / `scoring_runs` 已迁入自有评分服务。旧 BossHunter 代评接口 `pending_evaluations` / `submit_evaluations` 保持 unsupported，不访问 BossHunter。


## 评分（#48，11-26）

落实 Issue #1 的 11-26 分层边界：`server.py` 只路由，`service/scoring.py` 编排材料、授权、串行模型请求和结果校验，`store/scoring.py` 管理所有评分 SQL。配置在 `scoring_config.py`，由主配置的 `scoring:` 段加载。评分无需浏览器、平台选择器或 BossHunter HTTP。

- schema migration 4 在唯一 SQLite 内新增 `scoring_runs`、`scoring_items`、`job_scores`，通过现有 `schema_migrations` 事务执行；不继续增加隐式 schema extension。v3 已有 jobs/投递/回复和旧评分字段原样保留。先备份并升级旧监听/每日运行目录，再迁移生产库；旧运行时拒绝 v4 是明确的升级边界。
- preview 是只读快照，列出预计付费请求数、全库待评分数和跳过原因。start 只接受布尔 `confirm=true`，校验材料与凭据后原子占用岗位，复用 `service/tasks.py` 启动 scoring worker；同一个岗位不能被并发重复评分。
- worker 使用确认时的主简历/经历和 JD 快照；执行前复核评分配置哈希。缺 JD、已过滤、已删除和已导入评分的岗位不请求模型。运行完成移除材料快照，保留输入哈希和模型/服务等级/用量；简历、JD、密钥不进入 Git 或任务日志。
- 原生分数通过查询投影参与搜索、详情、briefing、分数分组和每日候选排序；不会覆盖导入的 legacy_score/reason，不会改变投递队列、授权、风控暂停或发送规则。
- 每批上限默认10（配置可调低），串行单次请求，不自动重试。首先单条查看额度；后续余额缺失/不足就阻塞该项。模型 JSON 必须有合法整数分数和非空理由；失败不伪造零分。未知请求结果保留占位，人工核对后再处理，进程重启不自动付费重放。
- `setup_check` 的 own 分支只检查本地资源；不会因没有 BossHunter 进程而失败。`daily.enabled`、回复发送与跟进授权保持现有边界。
