# BossHunter 兼容后端

默认 `engine.backend: own`，不依赖 BossHunter。仅主动选择 `bosshunter` 后端时，需要安装 [BossHunter](https://github.com/shengjidaguai-china/BossHunter) 并启动本地工作台：

```sh
bosshunter web --no-open
```

默认地址 `127.0.0.1:8686`；改端口须同时设置上游 `web --port` 与本项目 `bosshunter.port`，环境变量 `BOSSHUNTER_PORT` 优先。工作台启动可能恢复持续采集会话，启动前核对既有授权。

兼容工具、一次性确认码、统计口径和通知配置见 [工具参考](usage.md)。旧的 `pending_evaluations` / `submit_evaluations` 在 own 模式 unsupported；own 评分使用 `preview_scoring` / `start_scoring` / `scoring_runs`。

导入已有岗位用 `job-agent import-bosshunter --source <源库路径>`；源库以只读事务打开，目标仍为 job-agent 的唯一 SQLite。默认源路径见 [数据层说明](../mcp/store/README.md)，自定义安装请显式传入 `--source`。过渡保活模板见 [examples](../examples/bosshunter-keepalive.md)。

## 兼容工具完整参考

以下表格和统计口径保留兼容后端契约；提到 BossHunter 的 API、direct_send 和可能发送简历的监听均只适用于兼容后端。own 模式以 [使用参考](usage.md) 和 [架构](ARCHITECTURE.md) 为准。

## 工具

| 工具 | 用途 |
| --- | --- |
| `status()` | 当前后端偏好、评分配置就绪状态、当前/最近任务和 job-agent 配置 |
| `search_jobs(query?, cities?, platforms?, statuses?, salary_min?, salary_max?, min_score?, sort_by?, limit?, offset?)` | 搜索已保存岗位，隐藏 JD 和 HR 私密字段 |
| `get_job(job_id)` | 岗位详情、完整 JD 和最近评分解释 |
| `jd_requirements(cities?, platforms?, since?, limit?)` | 批量提取要求原句及来源，不评分、不归类 |
| `check_collection(platforms)` | 检查采集就绪状态 |
| `start_collection(platforms, confirm=false)` | 用户授权后，以 `confirm=true` 启动采集；关闭自动评分 |
| `preview_scoring(job_ids)` | 预览选定岗位的评分范围与调用数量 |
| `start_scoring(job_ids, confirm=false)` | 用户授权后，以 `confirm=true` 评分，可能产生模型费用 |
| `scoring_runs()` | 最近评分批次及进度 |
| `profile()` | BossHunter preferences + mode、每周练习面试上限和冲刺薪资线 |
| `experience_list()` / `experience_get(id)` / `experience_add(title, md)` | 列出、读取和新增经历 Markdown；标题生成唯一 ID，不覆盖 |
| `company_get(company)` / `company_save(company, md, source_url)` | 读写本地公司缓存，记录来源和更新时间，不访问来源链接 |
| `log_event(job_id, event, resume_version?, variant?, chat_url?, note?)` | 追加手动投递、约面、面试、offer 等事件，公司、平台、城市、分数和链接来自 BossHunter |
| `company_history(company)` | 归一化后该公司的全部本地事件与 BossHunter 岗位状态，附岗位及已知聊天链接 |
| `funnel(group_by?)` | 按岗位去重的打招呼、回复、约面、面试及 offer 漏斗；可按 resume_version / platform / variant 分组 |
| `pending_evaluations(limit=5)` | 原样读取待评分结构和简历（include_resume=true，limit 1–10） |
| `submit_evaluations(evaluations)` | 原样转发 evaluations 列表；BossHunter 校验并准备岗位，不发送 |
| `set_greeting(job_id, greeting, confirm=false)` | confirm 必须为布尔 true，才写入 confirmed=true 的招呼语；不发送 |
| `setup_check()` | 检查配置、BossHunter、简历路径、经历库和本机 CDP（仅 GET /json/version） |
| `resume_import(path)` | 从本地 PDF / docx / md 提取纯文本，不保存导入文件 |
| `briefing(job_id?)` | 总入口：个人条件、mode、漏斗、带链接队列和公司简报提醒；指定岗位时返回 JD、经历、公司缓存、历史和排除证据 |
| `funnel_stats(group_by="platform", days?)` | 复用 BossHunter stats/activity/workbench，合并手动投递和面试事件；输出漏斗表和 platform/city/score_band 分组 |
| `greeting_targets()` | 按配置选分数最高的前 N 家公司，每家保留最高分岗位，返回 JD 和现有文案；只读 |
| `preview_greetings(job_ids?, limit?)` | 列出四家平台、动作、待发文案及能力状态，生成持久化一次性确认码；不发送 |
| `send_greetings(confirm_token, exclude_ids?)` | 仅在用户明确确认后，复核名单及文案并调用 BossHunter direct_send；真实对外发送 |
| `delivery_status()` | 当前/最近任务进度、成功数、日志和失败原因，读取 workbench |
| `stop_delivery()` | 仅停止活跃的独立 deliver 任务，拒绝停止其他模式 |
| `start_reply_monitor(confirm=false)` | 用户单独授权后传布尔 true，启动 BossHunter 独立监听；可能发简历 |
| `stop_reply_monitor()` | 仅停止活跃的独立 monitor 任务 |
| `notify_test(channel)` | 用户明确要求后向自己发送一条渠道测试；可绕过 disabled，无重试，不联系 HR |

工具按 `engine.backend` 路由，确认约束保持不变；`search_jobs` 的筛选仍由参数指定，BossHunter 的采集/评分仍使用其自身设置。`status.job_agent` 可供 Agent 对照本地偏好。


### own 评分

在 `config.yaml` 的 `scoring:` 配置主简历的 UTF-8 文件路径。经历从 `$JOB_AGENT_HOME/experience/*.md` 读取；没有经历文件时只用主简历，不虚构补齐。key 默认从 ai-vault 的 `personal-infra/openai/OPENAI_PLATFORM_API_KEY` 读取，也可选择 `key_source: env` 和 `api_key_env`；模型、地址和来源均可配置，配置只写名称与路径，不写密钥值。

1. `preview_scoring(job_ids)` 先显示 `estimated_requests`、全库 `pending_count` 和跳过原因；不访问模型、不写库。
2. 向用户展示请求数，取得授权后调用 `start_scoring(job_ids, confirm=true)`，返回后台任务和运行 ID。默认最多 10 个岗位/批，按每个岗位一次请求串行执行，无自动重试。
3. `scoring_runs()` 读取最近 20 批的逐项状态、用量和 `remaining_tokens`；`get_job` 返回分数、理由与 trace。分数参与查询/排序、统计和每日候选排序，不改变投递状态，也不自动发送。

默认使用 `gpt-6.1-sol`、`service_tier: priority`，请求只传 `max_completion_tokens`，不传 `max_tokens` 或 `thinking`。先用单条取得 `x-ratelimit-remaining-tokens` 再扩批；同一批后续请求在上一条余额未知或不足时标为 `blocked/rate_limit_budget`，不等待、不重试。预算采用 UTF-8 字节数上界加输出上限和余量，并非精确 tokenizer 计数。

采集写入的新岗位自动出现在待评分统计中，只有有 JD 且未过滤的岗位可评；每日任务不会自动付费评分。导入的旧分数和理由（含有理由的零分）保留并跳过，不提供自动重评。缺 JD 先补全再评。`failed/invalid_model_result` 不落分，用户可重新预览授权；超时、连接中断或进程意外退出的已发出请求保留未知状态/占位，防止重复收费，须人工查证后处理，不能直接重跑。

`setup_check` 在 own 模式检查本地库、评分凭据、主简历和 Ego 配置，不探测 BossHunter。它仍会报告缺少经历、空间未验证等具体缺项。`pending_evaluations` / `submit_evaluations` 是旧 BossHunter 的 Agent 代评接口，在 own 模式明确 unsupported；付费评分使用上面三个工具。本表其他 BossHunter 描述仅适用于兼容后端。

### jd_requirements

```json
{"cities": ["杭州"], "platforms": ["boss", "liepin"], "since": "2026-11-01", "limit": 20}
```

- `cities` / `platforms` 省略时读 YAML；传 `[]` 表示不限。平台值为 `boss`、`liepin`、`51job`、`zhilian`。
- YAML `directions` 为标题/JD 的字面关键词列表，任意一个匹配即可，空列表不限方向；这不是技能归类。
- `since` 按岗位入库时间 `created_at`（非招聘发布时间）作包含边界的筛选。接受 ISO 日期/时间；日期和无时区时间按 UTC，有时区时间转 UTC。
- `limit` 是符合筛选的**岗位数**（默认 20，1–100），句子数可能更多；按入库时间倒序分页查询，每个候选取详情。只读本机 HTTP，无平台请求。
- 从「任职要求 / 岗位要求 / 职位要求 / 任职资格」等标题到下一个已识别章节取原句；支持常见编号、Markdown、HTML 和英文标题，按换行及句末标点拆分，去列表编号，不改写、不归类。
- 找不到非空要求段时拆整段 JD，标记 `section: "full"`；每句带 `job_id/title/company/city/platform/url/section/text`。JD 缺失不会编造内容，会列入 `missing_jd_job_ids`。
- 返回 `jobs`、`requirements_jobs`、`full_jobs`、`missing_jd_job_ids`、`section_success_rate` 和 `items`。成功率为要求段岗位数 / 符合筛选岗位数；空岗位池时为 `null`。规则抽取可能漏标题或混入未识别的后续章节，技能应结合来源核对。
- 当前 BossHunter 的 `GET /api/jobs/{id}` 返回完整 `jd`，无需 SQLite 兜底。`get_job` 继续保留评分解释，而此工具不会请求评分接口。

合成返回样例：

```json
{
  "jobs": 1, "requirements_jobs": 1, "full_jobs": 0,
  "missing_jd_job_ids": [], "section_success_rate": 1.0,
  "items": [{
    "job_id": "example:1", "title": "测试职位", "company": "合成公司",
    "city": "杭州", "platform": "boss", "url": "https://example.invalid/job",
    "section": "requirements", "text": "熟悉 SQL"
  }]
}
```

### v1 上下文与统计口径

- `profile.preferences` 完整来自 `/api/agent/state`，不复制采集条件；`mode` 默认 `job`，`practice.max_interviews_per_week` 默认 3，`stretch_salary_min` 默认 `null`，薪资单位为月薪 K。它们只供调用方决策，不会自动投递或限制发送。
- `experience_list.items` 只含 ID/标题；`experience_get.item`、`company_get.item` 缺失时为 `null`。经历写成 `# 标题` + 空行 + 原始 md，读取时去掉添加的标题封装。ID 为规范化标题 + 随机后缀，以独占创建避免覆盖。
- 公司键使用 NFKC、大小写折叠、去空白和括号段，再反复去掉末尾的「有限责任公司 / 股份有限公司 / 股份公司 / 有限公司 / 公司 / 股份 / 科技」。键的 SHA-256 作为安全文件名，JSON 保留原名、key、md、source_url、UTC updated_at。规范化可能合并同名公司；调用方需结合来源和城市判断法人身份。
- `log_event.event`：`manual_applied / greeted / replied / resume_requested / interview_scheduled / interview_done / interviewed / offer / rejected / withdrawn`（`interviewed` 是兼容旧名）。重复事件保留原始行；漏斗每个阶段按 job_id 只计一次，并合并 BossHunter 的 `sent / replied / resume_sent / needs_resume / follow_up_sent`。后几种是对应的已发送/已回复状态；不推断具体简历版本或招呼语写法。
- 漏斗返回 `total` 和可选的 `groups`：`greeted`、`replied`、`reply_rate`、`interview_scheduled`、`interview_rate`、`interviewed`、`offers`。为兼容旧工具，manual_applied 映射到 greeted；回复率和约面率均以该计数为分母；分母为 0 时返回 `null`。后续阶段包含前序阶段（offer → 面试 → 约面 → 回复 → 打招呼），单独的 rejected/withdrawn 不推断已打招呼。面试数也是岗位数，不是面试轮次数。
- 分组采用每个岗位最早记录的非空简历版本/variant，缺失为 `unknown`；平台优先采用 BossHunter 保存值。后续更换版本不会把同一岗位拆入多个组。查询会读取完整岗位池，不应用 `jd_requirements` 的方向/城市过滤，分页异常会报错，避免给出部分统计。
- `briefing.todos`：待确认为 `ready / scored / needs_review`，已发送未回复为 `sent / follow_up_sent`，回复队列为 `replied / resume_sent / needs_resume`，各返回 `count/items`。这是 BossHunter 岗位状态快照，**不表示平台未读消息**，也不等于 BossHunter 的待发送确认队列。每项都含 `url`；`chat_url` 取上游已有值或最近一次本地事件，不猜造聊天链接。
- 指定岗位的 briefing 返回全部经历素材，由 Agent 判断关联性；MCP 不做语义匹配或评分。排除证据仅检查公司屏蔽名、标题/公司排除词、JD 排除词和可解析月薪 K 区间，不替代 BossHunter 的完整预筛规则；`salary_checked=false` 表示薪资无法按该口径检查。
- `setup_check` 即使配置无效或服务离线，也逐项列出缺失项；无效配置下的网络检查标为无法检查，不使用猜测地址。绝对简历路径读本机文件状态；相对路径交给 BossHunter onboarding 按它自己的目录解析。CDP 仅接受本机 HTTP/WS 地址，用 HTTP GET 检查，不连接 WebSocket、不打开或启动浏览器、不跟随重定向。
- `resume_import.text` 是提取结果，docx 包括正文段落和表格；PDF 无文字层时返回空文本和 OCR 提示，不做 OCR。页眉、脚注、文本框及复杂版式的还原不作保证。导入不复制文件，也不创建简历版本。
- v1 上下文工具不调用模型、不抓取招聘平台。`submit_evaluations` 和 `set_greeting` 会写 BossHunter 的准备数据，但不会发送、启动采集或评分流程；原有 v0 的显式采集/付费评分入口仍保留各自授权约束。

### Issue #6：统计、定制与确认发送

问“我投递情况怎么样”调用一次 `funnel_stats()`。总览保留工作台原始 `bosshunter.funnel`，`total.applied` 为工作台发送数加去重后的本地补充投递；`total.replied / applied` 是回复率，空分母为 null。`manual_applied` 计入总投递，`interview_done` 兼容 `interviewed`，offer 推导前序面试/约面/回复/投递，拒绝本身不推导投递。同一岗位、同一阶段只计一次；上游已发送/回复与本地同类记录不重复计数。按岗位数统计，不是公司数或面试轮数。

- 分组默认 platform，可选 city / score_band；默认分数档 0–59、60–79、80–100，缺少元数据为 unknown。统计通过 `/api/jobs` 完整读取未删除岗位（包括搜索界面隐藏的屏蔽公司），不用待发队列充当全部岗位；招呼语与发送仍走会过滤屏蔽公司的搜索接口。`group_by=null` 且本地无事件时，只需 GET stats/activity/workbench；有本地事件时也会读取岗位池做去重。
- `days`（默认配置 7）仅影响原样返回的 history 活动，漏斗始终累计；编辑 116 次文案不代表投递 116 个岗位。读上游多个接口不是原子快照；运行中的任务可能造成分组与总览暂时不一致，工具会给 warnings，不修饰上游数字。
- `briefing.todos.company_briefs` 对已回复和本地 interview_scheduled 岗位给出公司、链接和提醒。它是状态快照，每次 briefing 都可能重现，按公司去重；不表示新未读、不自动搜索。见 [company-brief](../skills/company-brief/SKILL.md)，缓存复用现有 companies JSON 的 Markdown 字段，每项附链接。
- [greetings](../skills/greetings/SKILL.md) 只为 top N（默认 20，同公司去重）加一句 JD 公司介绍中的业务/产品/行业话；缺依据就跳过，完整文案含标点不超过 120 字。工具不会生成或修改旧文案，获授权后才走现有 `set_greeting` 保存，不发送。

发送流程：`preview_greetings` → 展示完整批次 → **用户明确说确认/发送** → `send_greetings`。默认预览 5 个 ready/approved 岗位，支持 BOSS、猎聘、智联、51job，显示平台及「发招呼语 / 投递简历」动作。招呼语动作必须有已审阅文案；在线简历投递无需文案。显式 job_ids 最多 30 个并全部绑定，此时 limit 不截断。不隐式重试 error 岗位。三家能力默认关闭，预览仍可查看，发送前须在本机 BossHunter 配置启用并重新预览；首次真投由用户挑 1 个岗位，明确确认后执行。

确认码绑定 BossHunter 端口、岗位 ID、文案和展示身份字段、准备/审阅状态，默认 30 分钟有效（可缩短），重启 MCP 后仍有效。发送前重新核对整批；隐式选择时重新计算 top limit，名单变化就拒绝。exclude_ids 只能排除原批次内岗位，被排除的岗位也必须通过原批核验。校验通过后先原子消耗确认码，再发一次 `POST /api/workbench/deliver`（direct_send=true），由 BossHunter 控制时间窗、限速和每日上限。超时或上游失败也不恢复确认码；先用 delivery_status 查结果，不能自动重试。

BossHunter 当前没有带文案哈希的条件发送接口，复核与上游执行之间仍有其他客户端编辑的竞争窗口；发前避免并发改同一批。一次确认只授权该批发送，不授权监听。监听使用现有 `POST /api/agent/tasks` mode=monitor；它可能发送简历并受上游配置影响，需单独说明并取得授权。停止工具只处理对应的独立模式，不中断 collect/full 等其他任务。

### Issue #8：HR 回复通知到 Telegram / 微信

通知默认全部关闭。`job-agent watch` 只读取 BossHunter 已保存的岗位、历史和工作台任务，不启动采集、监听或发送简历，也不打开招聘网站。它依赖 BossHunter 已经发现回复；不替代 BossHunter 的回复采集。通知包含平台、公司、岗位、薪资、可用的回复摘要和岗位链接。有具体 `chat_url` 时使用该链接；BOSS 缺链接时使用聊天首页，其他平台保留原岗位链接，提示在原站查看。

先对照 [config.example.yaml](../config.example.yaml) 给 `$JOB_AGENT_HOME/config.yaml` 添加 `notify`。示例中的所有渠道和总开关均为 `enabled: false`；审核并准备好后，才手动把总开关和目标渠道各自改为 `true`。渠道名可自定义，可以同时开启多个渠道。密钥只通过环境变量注入，配置只存变量名，不接受 `bot_token` / `url` 等明文密钥字段。

**Telegram 私聊**：通过 BotFather 建 Bot，本人在该 Bot 私聊发送 `/start`；在 `channels.telegram` 配置 `type: telegram`、`bot_token_env: JOB_AGENT_TG_BOT_TOKEN` 和自己的 `chat_id: "数字 ID"`。将 Bot token 注入 `JOB_AGENT_TG_BOT_TOKEN`。本模块只用 [Bot API sendMessage](https://core.telegram.org/bots/api#sendmessage)，从不调用 getUpdates，不抢其他服务的轮询；私聊失败不会回退到群聊。

**企业微信群机器人**：在自己选择的企业微信群添加机器人，把完整 webhook URL 注入 `JOB_AGENT_WECOM_WEBHOOK_URL`，配置 `type: webhook`、`format: wecom`、`url_env: JOB_AGENT_WECOM_WEBHOOK_URL`。POST 格式是 `{"msgtype":"markdown","markdown":{"content":"..."}}`；检查 `errcode` 是否为 0。参考[企业微信官方文档](https://developer.work.weixin.qq.com/document/path/91770)。此渠道推到所配群，配置前确认接收人范围。

**Server酱 Turbo（微信）**：按[官方 SendKey 文档](https://sct.ftqq.com/docs/getting-started/sendkey/)获取 SCT SendKey，注入 `JOB_AGENT_SERVERCHAN_SENDKEY`；配置 `type: webhook`、`format: serverchan`、`sendkey_env: JOB_AGENT_SERVERCHAN_SENDKEY`。向 `https://sctapi.ftqq.com/{SendKey}.send` POST `title` / `desp`，检查 `code` 是否为 0。此适配器使用 Turbo，不支持 Server酱³ 的 sctp 密钥。

**通用 HTTP webhook**：配置 `type: webhook`、`format: generic`（默认）和 `url_env`，接收 JSON `{title, text, url}`；任意 2xx JSON 对象响应视为成功。生产 webhook 使用 HTTPS，本机模型/测试服务允许 loopback HTTP。不跟随重定向，防止凭据被转发。

密钥应由自己的密钥工具加载至 MCP / CLI 进程环境，不要写到仓库、命令历史、日志或 launchd 公开示例中。不同进程的环境相互独立，MCP 客户端和 launchd 不会自动读取交互 shell 的变量。

在 MCP 中显式调用 `notify_test`（参数 `{"channel":"telegram"}`）会向该渠道发一条「job-agent 通知测试」，**即使该渠道或总开关关闭也会发**。只试一次，不重试；返回 `success` 和失败时的脱敏 `error_code`。服务端确认成功不代表用户已阅读。请只在本人要求测试时调用。

```bash
# 命令入口由 uv sync --project mcp 安装。以下命令需手动运行。
uv run --project mcp job-agent watch --once # 一轮，输出 sent/failed/skipped
uv run --project mcp job-agent watch        # 前台运行，默认每轮完成后等 5 分钟
```

常驻模式每轮重读配置；修改 `interval_minutes` 可调整间隔（1–1440 分钟），关闭总开关或所有渠道后退出；Ctrl-C 停止的是本通知进程。`--once` 在发送失败或读取失败时返回非零状态，禁用时返回成功且不访问 BossHunter。

去重使用 `$JOB_AGENT_HOME/notified.jsonl`（新文件权限 0600），按 BossHunter 端口、岗位、`hr_reply_detected` 历史 ID 和渠道分别记录成功。**第一次启用会推送库里已有的回复**；读取历史会扩大 limit 直到完整，避免固定窗口漏回复，历史很大时读取成本会上升。只有状态 `replied` / `needs_resume`、没有该类历史时，同一岗位只发一次；普通 JD 编辑、状态切换不重复触发。状态兜底与同岗位第一条历史共享去重标识，避免先看见状态、稍后看见历史时重复推送；后续历史 ID 当作新回复。没有逐条历史的上游无法识别同一岗位后续不同回复；第一条历史被删除或迟到时无法精确还原回复轮次。

每个渠道失败后最多再试 2 次，之后只记错误码；失败渠道下一轮再尝试，成功渠道不会陪着重发。文件锁避免多个 watch 并发推送；回执文件损坏时拒绝发送，先从备份修复。不同岗位的外部通知没有 HR 副作用；可选模型草稿也只随通知发给用户。HTTP 接口没有幂等键：超时重试、或发送成功后回执落盘前崩溃，仍可能重复；本地去重不承诺跨网络的严格 exactly-once。

可选 AI 草稿：将 `notify.draft_reply` 改为 `true`，配置 `notify.ai.base_url`（包含 `/v1`）、`model`、`api_key_env` 和 `resume_path`（本人主简历，UTF-8 Markdown / 文本；PDF/docx 请先用 `resume_import` 提取并自行保存）。默认不调用模型；开启后会把裁剪后的 JD、主简历与回复摘要发送到所配置的 OpenAI 兼容服务，可能产生费用。草稿会标注「仅供本人审阅，未发送」，不自动发给 HR；简历不可读、模型未配置或生成失败时照常推送纯通知。每条事件至多调用一次模型，再分发给待推渠道。

### 投递任务完成汇总

`notify.delivery_summary` 默认 `true`，仍受 `notify.enabled` 和渠道 `enabled` 控制；只发给已有 `type: telegram` 渠道，复用同一套 Bot/接收人配置。设为 `false` 只关闭投递汇总，不影响 HR 回复通知。

watch 检查 `GET /api/workbench` 的当前/最后任务，仅在 `mode=deliver` 且状态为 `completed` 或 `failed` 时汇总。任务 ID + BossHunter 端口 + 渠道组成去重键，成功发送后在现有文件锁内写入并 fsync `notified.jsonl`，重启后继续去重；发送失败下轮重试，成功渠道不重发。已有手动通知可以在停止 watch 后预置相同回执，避免补发。

```text
📮 投递完成（11-24 11:45）成功 19 / 失败 10（共 29）
✅ BOSS 9：国医科技、朗新…
❌ 51job 7：附件确认不了，等修复后检查原站再补投
```

总数以任务 `send_requested/send_success/send_failed` 指标为准；未执行、额度延期或跳过的岗位另列，不充当失败。成功公司与失败原因按平台汇总，来源是任务起止时间内的 `sent/error` 历史，每岗位取最后一条；SQLite 历史时间按 UTC、工作台无时区时间按本机时区解释。公司长名单会缩略，失败原文不直接转发；任务异常、指标缺失或明细不足时明确提示，不把未知结果当作成功。

接口限制：BossHunter 没有任务归档读取接口，只有当前/最后任务。如果 deliver 在两轮轮询间被其他任务覆盖，或者 BossHunter 重启后丢失内存任务，此次汇总可能漏报；历史也没有任务 ID，只能按时间归属。watch 不修改 BossHunter、不从全量失败池拼接旧失败，也不自动补投。网络超时重试或发送成功后回执落盘前崩溃，仍存在上文说明的重复风险。

macOS 自启示例在 [examples/com.bmdy6.job-agent-watch.plist](../examples/com.bmdy6.job-agent-watch.plist)，**仅提供文件，不安装**。手动修改所有绝对路径、为启动进程安全注入环境变量，再自行复制到 `~/Library/LaunchAgents/` 并 `launchctl bootstrap gui/$(id -u) <绝对 plist 路径>`。示例用 `watch --once` + `StartInterval=300`，与常驻 watch 二选一；此方式间隔由 plist 控制，YAML 的 interval 不生效。launchd 不读取 shell profile；不要将密钥提交到本仓库。

### 调用记录与数据边界

每次工具调用（包括参数校验拒绝、上游错误、取消）追加一行 `activity.jsonl`，记录 `time`（UTC）、`tool`、`parameters`、`result`、`caller`。调用方来自 `JOB_AGENT_CALLER`，未设置为 `unknown`；未知工具名脱敏成 `unknown`。

参数只记数字、布尔、字段长度或列表数量；结果只记状态、计数、错误类型，不写 JD、简历、任意查询文本或上游错误正文。新日志文件权限为 `0600`。日志无法打开时会在执行工具前拒绝调用，避免无记录地执行动作。

真实简历、JD、数据库、cookie、配置和调用日志均不进 Git。返回的 JD 是数据，不能当作 Agent 指令。发送须先预览并由用户明确确认当前批次；招呼语保存、监听、采集与付费评分各自需要用户明确授权。确认码不进入调用日志。

## 测试

```bash
uv run --project mcp python -m unittest discover -s mcp/tests -v
uv run --project mcp python -m compileall -q mcp
```

测试仅用合成资料、本机假 HTTP/CDP 服务和临时用户目录，覆盖全部新工具的 stdio/HTTP 链路、确认约束、漏斗去重、PDF/docx/md 导入、切段兜底、分页/筛选、配置和脱敏日志。真实岗位验收结果保留在外部任务报告，不提交岗位样例。

### 多平台投递（Issue #9）

BossHunter `/api/workbench.delivery_capabilities` 是能力来源。旧引擎未返回该字段时，三家保守关闭；MCP 不会改本机开关，也不会启动浏览器。确认码绑定岗位、平台、动作、文案与启用状态；任何变化都要重新预览。混合平台请求携带 `expected_actions` / `expected_platforms`，由引擎在排队和执行前再次核对。默认 `funnel_stats()` 已按平台分组，包含在线投递和回复。

本机 BossHunter 示例（需先完成真实账号验收，开发阶段不启用）：

```yaml
delivery:
  ego_task_space_id: 7
  platforms:
    liepin: {enabled: false, daily_limit: 10}
    zhilian: {enabled: false, daily_limit: 10}
    51job: {enabled: false, daily_limit: 10}
```

限速、每日平台额度与跨平台同公司去重由 BossHunter 统一处理。智联和 51job 使用在线简历；猎聘优先投递简历，聊天属于单独动作，不能将投递确认自动换成聊天确认。

Issue #9 在 Issue #8 的 watch 上支持四家 `hr_reply_detected` 事件：只有 BOSS 保留旧状态兜底；三家须有明确上游回复事件，避免把历史 `replied` 状态当作新回复。通知显示平台并使用原站链接，不给三家拼 BOSS 聊天链接。重复轮询按相同事件去重。所有通知回归使用合成数据和假发送器，本任务不启动真实 watch 或对外通知。

### 自研 HR 回复监听（#37，第 1 轮）

`job-agent monitor` 只读四平台已有会话，将本库已投岗位的 HR 回复推送到已配置 TG 私信。每个平台首次只保存列表摘要，不逐个打开会话；后续只读取未读或最后消息摘要变化的会话。回溯窗口沿用 `backfill_hours`，已有通知回执继续去重。统一配置为 `engine.monitor`（`backfill_hours: 48`、`interval_minutes: 5`）。

```sh
# 统一部署每日任务和监听；先按 deploy.md 停止其他写入者
PATH=$HOME/.local/bin:$PATH python3 scripts/deploy.py
launchctl list | rg 'com.bmdy6.job-agent-(watch|monitor)'
tail -f ~/.job-agent/monitor.log
launchctl kickstart -k gui/$(id -u)/com.bmdy6.job-agent-monitor
```

服务开机自启、崩溃后节流重启；空间被占时告警并退出，修复后显式重启。MCP 使用 `start_reply_monitor(confirm=true)`、`stop_reply_monitor()`、`reply_monitor_status()` 控制同一服务；`briefing` / `status` 包含监听状态与最后抓取时间。凭据仅在运行时从 ai-vault 读取。完整配置、停用/恢复命令和平台边界见 [examples/monitor.md](../examples/monitor.md)。第 2 轮的剩余 BossHunter 清理不在本轮范围。

### 每天自动采集和投递

自研后端支持每天 `08:00` 自动执行「采集 → TG 采集结果 → 投递 → TG delivery_summary」。需明确开启。升级后的运行时支持以下配置；与旧监听部署并存时，将新增的 `daily`、`engine.cooldown`、`collect.refresh_daily` 放在 `~/.job-agent/daily.yaml`，其余原有字段放在 `config.yaml`，避免旧运行时因未知键拒绝配置。两个文件合并时 `config.yaml` 优先：

```yaml
daily:
  enabled: true
  collect_enabled: true  # false：保留每日投递，只关采集
  deliver_enabled: true  # false：只采集，不投递
  hour: 8
  minute: 0
collect:
  fetch_jd: true
  refresh_daily: true
  # platforms.<boss|zhilian|liepin|51job>.enabled 都需为 true
engine:
  backend: own
  live_send_enabled: true
  dry_run: false
  cooldown:
    days: 30
    company_patterns:
      - '[（(][^()（）]*[)）]'
      - '(?:股份有限责任公司|股份有限公司|有限责任公司|有限公司)$'
    title_patterns:
      - '急招|诚聘|高薪|招聘'
      - '\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*[kK千万](?:[/·].*)?'
```

采集使用 `job-agent 采集`，投递沿用 `engine.task_space_name`，与 `job-agent 监听` 分离。`refresh_daily` 每天重新从各关键词/城市首屏开始，每组合每天一页；仍遵守各平台 `daily_page_limit` 和 8–15 秒随机访问间隔。城市编码使用 `collect.platforms.<平台>.city_codes`。新岗位补抓完整 JD、HR 姓名/职位/可取得的平台 HR ID；平台未公开 HR 的字段留空。过滤统计仅保留采集诊断，自动投递不按分数或预筛标签排除岗位。

**共享平台暂停**：采集、投递和 HR 监听均遵守同一持久暂停记录。原站风险限制与登录过期分开；暂停平台在监听轮次中直接显示未访问，重启不自动解除限制。

**正式投递与沟通回执分开**：BOSS 成功表示沟通动作回执；猎聘的现有首聊适配器只证实文本招呼，正式投递/附件流程尚未独立验收，因此真实投递时保留待处理，不发送首聊冒充简历成功。51job 缺少明确附件配置时保留待处理；有配置也必须通过平台原有暂停检查和附件正向回执。历史记录不改写、不直接重投。

通过只读 CLI job-agent resume-status --before YYYY-MM-DD --include-jobs 查看猎聘和 51job 的旧岗位，或 MCP 工具 resume_delivery_status 查看摘要；分别显示本地附件回执、仅沟通、历史发送未证实、未知回执、预检失败、未处理和跳过。统计仅基于本地记录，旧岗位须核对原站真实申请/附件状态；本查询不访问平台、不发送、不修改数据或解除暂停。

原站人工取得的申请记录也可通过只读 CLI job-agent review-resume-record --job-id ID --record-file FILE 或 MCP review_resume_application_record 匹配本地岗位。猎聘“继续聊”不是已投状态；前程无忧“申请简历”记录不是附件送达回执。该审计仅核对所提供记录的岗位身份和历史申请字段，不验证来源、当前尝试或附件，不触发原站请求、不改写历史。

每轮先记下采集前的旧岗位边界。旧岗位按评分降序，同分或无评分按采集时间升序；旧队列还有可处理岗位时，新岗位不先投。旧队列处理完才进入本轮新岗位，各平台仍受 `daily_limit` 和原投递间隔限制。当天没投完的留到第二天。结果不明的岗位不自动重投，本轮该平台停止；风控、登录失效同样停止该平台并 TG 告警。风控暂停另存数据库，进程重启和第二天定时触发也不会自动恢复。本人处理原站验证码/风控后，显式运行 `job-agent resume-platform liepin`（换成对应平台）才解除；这个命令只解除暂停，不采集、不投递。

重复拦截比较规范化后的「公司 + HR + 岗位」。同平台优先比较 HR ID；跨平台比较 HR 姓名，平台 ID 不跨站比较。双方有可比较的不同 HR，或岗位不同，可继续投；任一方没有可比较 HR 时，退回公司加岗位。发送前在已打开的旧岗位详情补 HR，再检查一次冷静期；成功记录冻结当时的身份，后续岗位刷新不改历史。冷静期满允许再进入自动队列，原站若仍显示已投/已沟通则跳过，绝不强制重复沟通。

部署统一使用不可变提交快照，每日任务与监听都指向 `~/.job-agent/runtime/current`。完整迁移、回滚和隔离演练步骤见 [统一部署](deploy.md)，旧快照只读清单见 11-26 盘点（原始过程材料已移除）。schema v5 新增监听摘要表；升级必须先停监听再备份、构建和迁移，跨 schema 回滚需恢复备份数据库。MCP 主仓库须在部署完成后再 pull，期间不重启 MCP。

```sh
PATH=$HOME/.local/bin:$PATH python3 scripts/deploy.py --inventory  # 只读清单
# 按 deploy.md 停止其他写入者后，由部署负责人执行：
PATH=$HOME/.local/bin:$PATH python3 scripts/deploy.py              # origin/main
```

统一脚本保留已有 calendar 和 cap 参数，不改 `daily.enabled`。以下时间覆盖、有限验收与卸载参数属于旧 install-daily.py，保留作历史兼容；它们不是 deploy.py 参数，统一部署后不要再分别切换两项服务到不同快照。

安装器只注册 calendar 触发，**不会立即投递**。TG 使用现有配置指向的环境变量；launchd 不继承终端变量，可在 `~/.job-agent/daily.env.json` 写入环境键值对象并 `chmod 600`；启动器只读此文件，绝不把值放进 plist、命令参数或日志。

- 改时间：修改 `daily.yaml`（或升级后的 `config.yaml`）中的 `daily.hour/minute` 后重新运行安装命令；launchd 使用 Mac 本地时区（本地时间需与预期一致）。`--hour H --minute M` 可临时覆盖触发时间。
- 真实有限验收：安装时附加 `--max-per-platform 3 --collection-page-limit 1`，五分钟后触发；验收后重新安装，不带这两个临时参数，并指定 `--hour 8 --minute 0`。
- 立即手动运行：`python scripts/daily-launch.py --max-per-platform 3 --collection-page-limit 1`。这是**真实投递**，会消费额度，不是 dry-run。
- 关闭：`daily.enabled: false` 关闭整轮；两个子开关可独立关闭采集/投递。彻底移除定时器可运行安装器加 `--uninstall`（仍需提供 `--runtime`）。
- 看日志：`tail -f ~/.job-agent/daily.log`；看逐平台数字、原因、批次与选中岗位：`cat ~/.job-agent/daily-last-run.json`；看服务：`launchctl print gui/$(id -u)/com.job-agent.daily`。
- 定时任务不会绕验证码、换号、改简历或生成新招呼语。需要本人处理时，修复原站状态后再启动下一轮；不把 `unverified` 批次当失败重发。

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
