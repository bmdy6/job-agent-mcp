# 配置管理

配置变更统一使用 MCP `update_settings` 或 CLI `job-agent settings`。`config` 保留为兼容别名，使用相同服务。旧版迁移脚本的 `--apply` 也委托该服务，不再自己备份和覆盖 YAML。

```sh
job-agent settings show analytics
job-agent settings set analytics.activity_days 14 --reason '调整统计窗口'
# 检查预览后确认；多字段用 PATH JSON 成对传入
job-agent settings set analytics.activity_days 14 --reason '调整统计窗口' --confirm
job-agent settings history
job-agent settings rollback <id>
job-agent settings rollback <id> --reason '撤销窗口调整' --confirm
```

字符串值使用 JSON 字符串，例如 `mode '"practice"'`；列表、布尔、数字也按 JSON 传入。默认只预览。MCP `update_settings(changes, confirm=false, actor=null, reason=null)` 接受同样的点路径对象，`confirm=true` 才写入。actor 默认当前系统用户，可用 CLI `--actor` 或 MCP `actor` 标注操作者；source 自动标识 cli/mcp/service。未提供原因时记录操作类型。actor 是操作者声明，不是经过认证的身份。

写入前按既有模型校验字段、类型、范围和关联约束；凭据与路径字段不可通过 set 改写。确认后持有与 daily 共用的锁，拒绝与每日任务并发修改。保留既有 YAML 注释、换行和非目标内容；写后重新加载模型核验。不会重启服务、改 launchd 或启动投递。

## 历史和回滚

旧版本放在 `$JOB_AGENT_HOME/config-history/`（默认 home 为 `~/.job-agent`）。文件名包含 UTC 时间、随机后缀、首个变更字段和源文件名。单次变更多个字段时，完整字段清单在 `changes.jsonl`。目录权限 0700，快照和日志权限 0600；快照保存原始字节，包括原文件是否存在、模式和 SHA-256 元数据。快照含私有配置，不应提交或上传。

config.yaml 总是备份；daily.yaml 拥有被修改字段时也备份。两个文件属于同一事务。`changes.jsonl` 追加 pending、applied 或 failed 事件，记录操作者、来源、字段旧值/新值、原因和回滚来源。只有通过写后核验、成功记录 applied 的版本可回滚。预览、无变化、校验拒绝不创建历史。

默认最多保留 50 个 YAML 快照文件，按最旧事务整体清理，不拆开双文件版本，因此混合事务时可能剩 49 份。审计行持续保留，已轮换的版本显示 `available=false`。清理失败会在成功结果中给出 `retention_warning`，配置和 applied 记录仍然有效。

`rollback <id>` 恢复该次变更**之前**的完整文件，包括注释、换行及原文件不存在的状态；同文件中随后发生的其他变更也会被撤销。它会先备份当前版本并记录新的回滚事务，因此可以用新 ID 撤销回滚。回滚后同样执行当前模型校验；历史配置与现有 schema 不兼容时恢复当前文件并报错。回滚读取时校验快照哈希，拒绝软链接、未知 ID 和损坏的快照。

普通失败和 Ctrl-C 会恢复本次修改的所有文件。系统断电/SIGKILL 无法执行恢复，pending 不会被当成成功；应在私下检查快照和配置后处理，勿盲目重试。写入或审计失败会保留快照供排查；历史日志损坏时拒绝继续追加。

审计与历史接口对招呼语、事实文本、收件人 ID、完整连接 URL 记录脱敏标记和摘要；精确值仍在受保护快照中。原因中也不要放 HR 姓名、消息正文、URL 或密钥。CLI 不额外创建运行日志。

## 生产目录盘点

```sh
python3 scripts/home_inventory.py --report /tmp/home-inventory.md \
  --archive-script /tmp/archive-home.py
```

盘点只读文件名和元数据，不打开数据库或日志正文。未知项和运行数据保守保留；runtime、deployments、backups、evidence 等目录整体保留，仅展开 logs、bin。报告与脚本必须写在生产 home 以外。

脚本默认预览。由 Claude 审核脚本、清单、四个临时脚本的调用方及运行情况后，才可以执行 `python3 /tmp/archive-home.py --execute`。所有“可以归档”和“可以删除”的文件均只移动到 `backups/archive-日期/`；检查全部候选的 inode、大小、修改时间和文件类型，变化则在移动前停止。拒绝软链接和已存在的目标目录；不覆盖、不递归删除目录。若执行中断，已移动文件保存在归档中；重新盘点再审查。

## 公开仓库同步

`scripts/job-agent-sync-public.sh` 是可审查的版本化脚本，排除根目录 result*.md、DONE、shots，以及内部 plans/tasks/migrations。`--export-only` 只从本地 origin/main 导出到一个不存在的目标，不 fetch、不提交、不发布；常规同步保持原有 fetch 和本地 public 提交行为，不自动 push。可用 `JOB_AGENT_PRIVATE_REPO`、`JOB_AGENT_PUBLIC_REPO`、`JOB_AGENT_PUBLIC_LICENSE` 指定路径。

## llm：模型、用量和费用

`llm.models.<name>` 定义 `base_url`、`model`、`service_tier` 和 `pricing`。
`scoring.model_ref`、`greetings.generate.model_ref` 分别引用评分和招呼语模型；回复分类使用
`llm.reply_classification.model_ref`。也可以统一在 `llm.scoring.model_ref`、
`llm.greetings.model_ref` 设置引用，这两项优先于旧用途段中的引用。
未设置引用时，旧的 `scoring.model/base_url/service_tier` 和
`greetings.generate.model/service_tier` 继续生效；回复分类沿用评分配置。
凭据、超时和评分输入限制仍由原来的 `scoring` 配置提供。

```yaml
llm:
  models:
    primary:
      base_url: https://api.openai.com/v1
      model: your-model
      service_tier: priority
      pricing: {}  # 未配置价格，不包含任何内置真实价格
  scoring:
    model_ref: primary
  greetings:
    model_ref: primary
  reply_classification:
    model_ref: primary
  budget:
    daily_cost: null
    daily_calls: null
    currency: null
```

配置价格时须同时填写 `input_per_million`、`cached_input_per_million`、
`output_per_million` 和 `currency`，数值来自你实际使用的服务计费规则。
输入 token 已包含缓存输入，费用按 `(输入 - 缓存输入) × 输入单价 + 缓存输入 × 缓存单价 + 输出 × 输出单价`，最后除以一百万。
缺少价格时，只记录 token 和调用次数，显示「费用未知（未配置价格）」；不推算价格，也不回算历史流水。
HTTP 失败、超时、缺少 usage 的费用保持未知；模型输出不合法但返回 usage 时仍记账。

全局金额上限是 `llm.budget.daily_cost`；用途上限分别为
`llm.scoring.daily_cost`、`llm.greetings.daily_cost`、`llm.reply_classification.daily_cost`。
`daily_calls` 同样可在全局和各用途设置，招呼语原有 `daily_budget_calls` 继续取更严格值。
兼容用途字段 `scoring.daily_cost`、`greetings.generate.daily_cost`，统一 llm 段优先。
默认金额上限为 `null`。按 `engine.timezone` 的自然日计算，达到上限后评分停止、招呼语使用配置的兜底文案、回复分类保守降级。
当天只入队一次 TG 预算告警，由正常通知队列发送。

金额上限依据已返回的实际 usage：单次在途请求可能使费用跨过上限；下一次请求会停止。
配置金额限制时，同一个受限范围内只允许一条在途调用，避免并发绕过检查。
中断且无 usage 的调用保留未知结果，不自动重试；需人工核对账本中 `in_progress` 行后处理。
未配置价格时金额限制不执行，仍执行次数限制。
已配置价格但调用的 usage 缺失时，受金额预算约束的后续请求会暂停，避免把未知花费当作零继续调用。
不同币种不相加；配置金额预算时可指定 `currency`，不一致会阻止调用。

`$JOB_AGENT_HOME/llm-usage.sqlite3` 保存不含正文的统一流水：调用 ID、run_id、用途、日期、模型、token、费用和价格快照。
招呼语原流水及 `scoring_items.usage_json` 同步保留费用。回复分类原来没有调用账本，统一账本补齐。
`funnel_stats.llm_usage`、离线 dashboard 和每日 TG 投递汇总提供当天及含当天最近 7 个自然日的分用途指标。
旧版未记录价格的历史不会被标为零费用；报表兼容读取旧招呼语及评分流水，保留旧 token 和未知费用；旧评分仅有运行创建时间，日期按该时间归属。

## profile：身份段

```yaml
profile:
  name: 候选人
  role: 做软件开发的
  availability: ''
  graduation: ''
```

填写你自己的姓名、工作方向和到岗状态。`availability`、`graduation` 可为空。
身份 lint 使用此处的值，非空的字段都必须出现在第一段，不再匹配固定人名。
默认提示词、兜底文案和风格模板都只引用配置中的身份，不携带仓库作者的个人经历。
固定的 `engine.platforms.<platform>.greeting` 仍是用户自行维护的完整文案。

```sh
job-agent settings set profile.name '"候选人姓名"' --reason '设置个人资料' --confirm
job-agent settings set profile.role '"做软件开发的"' --reason '设置工作方向' --confirm
job-agent settings set profile.availability '"下月可以到岗"' --reason '设置到岗状态' --confirm
```

## prompts：提示词和模板覆盖

评分、招呼语、回复分类、兜底文案分别使用 `scoring.md`、`greeting.md`、
`reply_classification.md`、`fallback.md`。读取顺序：

1. `prompts.scoring/greeting/reply_classification/fallback` 显式配置的本地文件路径。
2. `$JOB_AGENT_HOME/prompts/` 下的同名文件。
3. 安装包自带的 `job_agent/prompts/` 文件。

显式路径不存在或文件为空会报错，不悄悄换回默认提示词。
为保留 settings 工具的本地路径权限边界，通过 settings 修改时只允许 `$JOB_AGENT_HOME/prompts/` 下的 Markdown 文件；其他位置由用户直接在 YAML 中配置。
支持 `{name}`、`{role}`、`{availability}`、`{graduation}`；
`{availability_clause}` 和 `{graduation_clause}` 在值非空时自动加前置逗号。
只替换这些占位符一次，JSON 花括号和用户文本中的其他占位符保持原样。

```sh
job-agent settings set prompts.greeting '"$JOB_AGENT_HOME/prompts/greeting.md"' --reason '使用自己的提示词' --confirm
job-agent settings set greetings.generate.hook_prefixes '["岗位里提到", "看你们在做"]' --reason '调整开头词表' --confirm
```

`greetings.generate.templates_path` 继续支持个人 YAML 风格样稿；不存在时使用包内
`prompts/styles.yaml`。词表默认值位于 `prompts/vocabulary.json`；
`greetings.generate.hook_prefixes`、`hook_suffixes`、`closing_lines` 可以覆盖。
个人样稿也支持上述占位符，样稿只用于学习语气，事实仍来自配置的 facts 和确认过的故事。

`deploy.label_prefix` 默认 `com.job-agent`，monitor 标签为 `com.job-agent.monitor`。
其他前缀生成 `<prefix>.job-agent-monitor`；例如旧安装应显式保留原前缀。
日任务标签保持 `com.job-agent.daily`。`deploy.py` 发现同一数据目录已有不同 monitor 标签时，
会在停止服务之前拒绝部署，要求先配置原前缀；不会自动改名、删除或另装 monitor。
升级旧安装前，应先用新版本 settings 命令设置 profile、提示词覆盖和原标签前缀，再启动部署事务。

## followup：HR 未回复跟进

默认关闭：`followup.enabled=false`、`followup.auto=false`。每日任务在新投递之后筛选跟进，汇总单列“跟进”。`auto=false` 每天只向启用的 Telegram 渠道推送一次“公司｜岗位｜话术”；不会入发送队列。只有显式设置 `auto=true` 才发送，且仍需要 `engine.live_send_enabled=true`、`engine.dry_run=false` 和每日投递开关。关闭每日投递时，自动跟进也停止。

```yaml
followup:
  enabled: false
  auto: false
  after_days: [1, 3]  # 每个岗位随机取闭区间内的整数天，首次规划后固定
  max_age_days: 7
  daily_limit: 10
  messages:
    - 你好，我还在关注这个岗位。有需要补充的材料或项目细节，随时找我就好。
  generate:
    enabled: false
```

候选必须有首投 `sent` 记录（对账 `sent_confirmed` 在 v7 中也是 `status=sent`）、`replies` 中完全没有该岗位的记录、达到持久化的等待天数，且距首投不超过 `max_age_days`。精确达到等待天数或最大年龄时包含在窗口内。后续回执或对账不会重置首投年龄；历史无时区时间沿用 Store 的 UTC 约定，旧版已跟进标记也会被排除；已删除、无 URL、禁用或暂停的平台不参与。仅支持 `greeting_mode=send_message` 且适配器明确支持跟进的平台，目前为 BOSS。

每个岗位最多跟进一次；随机等待天数、每日预览和尝试标记均保存在 `jobs.sqlite3`，无 schema 升级。跟进使用现有不可变批次和 `delivery_queue`，原首投历史及额度记录保留；跟进消耗额度，但不计入新投递的成功数；投递事件标为 `kind=followup`。复用投递时段、平台锁、频率控制、当日平台额度和正向回执。发送前再次检查时间窗和回复；BOSS 只打开已有会话，核对岗位与会话身份，若出现对方消息则停止。结果不明保留不重试。副作用前因时段、额度或平台忙而暂缓的同一批次可在后续每日任务续办。修改配置后，已冻结的批次仍要求配置一致。

话术开启生成时使用招呼语同一套 `llm.greetings` 模型、计费和预算、`profile` 与提示词覆盖机制；`prompts.followup` 指向自定义文件，也可放 `$JOB_AGENT_HOME/prompts/followup.md`。默认提示词随包分发且不含个人信息。文案 20–50 字，复用招呼语黑名单、句长、术语和数字检查，额外拒绝联系方式、身份信息、催促及重复原招呼的句子。生成失败、预算耗尽或校验失败时随机选择 `followup.messages` 中的有效文案；配置文案也需通过检查，没有有效文案则不发送。语义是否自然、是否有未被词法检查识别的事实，仍需在默认的 TG 预览阶段检查。

部署后由 Claude 执行以下 settings 命令，先预览、再确认。保持 auto=false，不会开始给 HR 发跟进：

```sh
job-agent settings show followup
job-agent settings set followup.enabled true followup.auto false followup.after_days '[1,3]' followup.max_age_days 7 followup.daily_limit 10 --reason '启用每日跟进预览'
job-agent settings set followup.enabled true followup.auto false followup.after_days '[1,3]' followup.max_age_days 7 followup.daily_limit 10 --reason '启用每日跟进预览' --confirm
job-agent settings show followup
# 如需按岗位生成文案，单独预览并确认；会使用模型预算：
job-agent settings set followup.generate.enabled true --reason '生成跟进预览文案'
job-agent settings set followup.generate.enabled true --reason '生成跟进预览文案' --confirm
# 安全停用：
job-agent settings set followup.enabled false --reason '停用跟进' --confirm
```

`followup.messages` 的精确内容只保存在私有配置快照；settings 历史日志使用脱敏标记与摘要。预览话术仅在私有数据库和 TG 中保留，不写入每日结果报告。

## qa：常见问题标准答案与建议回复

私有文件默认路径 `$JOB_AGENT_HOME/qa.yaml`，格式为“问题类别 → 标准答案”，见仓库根目录 `qa.example.yaml`。示例不会自动装入运行环境。先填写自己的真实答案；支持期望薪资、离职原因、到岗时间、现居城市、是否接受外包、能否加班等类别。自定义类别以类别名称在消息中直接命中；一条消息命中多个类别时一并供模型参考。文件缺失、格式不合法、重复键、别名或超出大小限制时不给建议，原回复通知仍保留。

配置字段 `qa.path` 可指定其他路径（支持 `$JOB_AGENT_HOME` 和 `~`）。变更仍通过 settings：

```sh
job-agent settings show qa
job-agent settings set qa.path '"$JOB_AGENT_HOME/qa.yaml"' notify.draft_reply true --reason '启用问答库建议回复'
job-agent settings set qa.path '"$JOB_AGENT_HOME/qa.yaml"' notify.draft_reply true --reason '启用问答库建议回复' --confirm
# 可选：引用已配置的命名模型，MODEL_NAME 替换为 llm.models 中已有的名称
job-agent settings set llm.draft_reply.model_ref '"MODEL_NAME"' --reason '选择回复建议模型'
job-agent settings set llm.draft_reply.model_ref '"MODEL_NAME"' --reason '选择回复建议模型' --confirm
```

没命中问答库的消息，也可以按分类起草（用 profile、`greetings.generate.facts` 和已确认的 `greeting-facts.yaml`）。默认空，只对问答库命中起草：

```bash
job-agent settings set notify.draft_reply_categories '["question","resume_request","interview"]' --reason '按经历起草回复'
```

`notify.draft_reply=true` 时，HR 回复推到 TG 之前先匹配标准答案，再按消息改写为建议，附“建议回复（未发送）”和原聊天链接。状态变化记录不生成建议；不命中类别就保持原通知。模型失败、预算耗尽或输出校验失败时附标准答案原文。已起草的同一回复保存在 `jobs.sqlite3`，重复推送不重复调用模型。**回复永远不自动发给 HR**。

`llm.draft_reply` 支持 `model_ref`、`daily_calls`、`daily_cost`、`currency`；共用 `llm.budget` 总预算及计费记录。未设置命名模型时沿用评分模型（旧配置显式设置 `notify.ai.model` 时兼容该模型与地址）；凭据与超时使用共享 scoring 设置。`prompts.draft_reply` 或 `$JOB_AGENT_HOME/prompts/draft_reply.md` 可覆盖提示词。不要在 settings 的 reason 或公开日志中填写薪资答案、HR 姓名、消息正文和聊天 URL。
