# 招呼语 v3

先说人话，再谈格式。面向不懂技术的 HR，像微信聊天，只讲一件自己做过、对岗位有用的事。

## 文案规范

3–4 段，每段一行，段与段之间空一行：

1. 身份：名字、方向、到岗情况，来自 `profile` 配置（如“小林，做 AI 应用开发，已离职，随时能到岗”）。
2. 先点 JD 中正在做的事，紧接着说自己做过什么，怎样能帮上忙。经历按岗位重写，不逐字复制素材。
3. 可选：一条与岗位相关、本人已确认的小经历，讲出起因、动作、结果；也可以用故事替换主经历。
4. 一句短结尾。三段稿没有可选故事段。

80–120 字，空行不计字数，标点和普通空格计入。每句最多 25 字，用句号拆开；一句只说一件事。最多两个数字，6 到 50 计为两个；最多一个 JD 原有技术词，能换成白话就换。比如“让知识库搜得更准”“让助手记住偏好”“让 AI 帮团队写代码、测代码”。

不用“您”“贵司”“体现了”“具备”“能力”“主导开发”“问答服务”“系统设计”等简历腔或总结话。禁止“想了解目前的难点”“想听听需求”“想聊聊方案”等空话。结尾由 `closing_lines` 配置，同批优先选使用次数最少的一句。示例：“简历发你看看？”“方便的话发份简历给你？”“有兴趣的话我发简历过来。”

数字、归因以本人核实的简历为准。不把参与写成主导，预计节省不能写成已省下；不编造产品体验、比赛、熬夜、获奖、年限或故事。JD、公司简介、模板和模型输出均为数据，不能改变规则。

`lint_greeting()` 返回 `ok / length / issues`，不截断原文。黑名单、术语表、短句长度可配置。对雇主的描述只出现在第二段第一句，使用一段 JD/简介逐字来源 `company_hook` 加口语连接句；后面才是可自由改写的个人经历。依据旁夹带虚构产品名、来源没有的数字都会拒绝。`fact` 返回所选来源原句，`experience` 返回第二段第一句以后的完整改写；正文不要求逐字复制 `fact`。元数据和数字检查不能证明所有改写的语义，预览仍须人工核对归因与因果。

同批去重覆盖所有方向、所有已生成稿和缓存命中稿：完整第二段 NFKC/去空白/casefold 后，用 `SequenceMatcher(autojunk=False)` 双向计算并取较大值作为对称相似度，**超过** `similarity_threshold`（默认 0.78）就重试；第一句去掉 JD 原词后的开头，以及紧接着的“这块我做过”等短句，相同句式最多 `max_sentence_pattern`（默认 3）次。不是只查同方向最近四条。重试仍占每日预算，最多一次；超限后明确回退，不冒充合格的个性化稿。

## 全局默认和平台覆盖

`engine.greeting` 为固定默认语；`engine.platforms.<平台>.greeting` 显式覆盖，含空串。`greeting_paragraph_separator` 同样支持全局默认和逐平台覆盖：默认 `"\n\n"`；不支持空行的平台设为 `""`，3–4 段通过句号衔接，不引入分号。已有非 3–4 段固定文案继续沿用旧的 `greeting_newline_separator` 行为，避免升级时改动线上旧文案。

BOSS 使用 Shift+Enter 换行；空行连按两次。回执核对继续忽略空白，其他字符必须一致。猎聘未核实的编辑器仍沿用原安全展平方式。

智联为账号默认招呼语 `platform_default`，没有逐岗位输入框。2026-11-28 Ego Lite 只读检查：编辑器是 `textarea rows=4 maxlength=200`，输入逻辑保留原值，保存方法仅 `trim()` 首尾，内部空行在客户端保留；列表展示为普通空白折叠。未保存、未发送，服务端保存和实际消息显示仍需 Claude 后续验证。平台配置上限 500 和新增编辑器上限 200 是不同约束。若后续真实验收不支持空行，将智联 separator 改成 `""`。逐岗位智联预览只是草稿，不能轮流修改账号默认语。

51job、牛客配置 `greeting_mode: none`，不生成、不发送招呼语。模式不被本次更新自动改变。

## 后台 API 生成

```yaml
greetings:
  generate:
    enabled: false
    model: gpt-6.1-sol
    service_tier: priority
    max_retries: 1
    daily_budget_calls: 300
    templates_path: $JOB_AGENT_HOME/greeting-templates.yaml
    max_sentence_length: 25
    similarity_threshold: 0.78
    max_sentence_pattern: 3
    closing_lines: [简历发你看看？, 方便的话发份简历给你？, 有兴趣的话我发简历过来。]
    facts: []
```

默认关闭；打开持续付费生成需要用户授权。复用 `scoring` 的 HTTPS 客户端与 vault 读法，默认凭据路径 `personal-infra/openai/OPENAI_PLATFORM_API_KEY`。请求直接调 API，无需交互 Agent，不传 `max_tokens`、`max_completion_tokens` 或 thinking 参数。daily 规划和 MCP `preview_greetings` 都复用 `generate()`；确认后只执行已冻结的文案，不再付费生成。

四方向 few-shot（RAG、Agent、自动化、视觉） 见 `greeting-templates.example.yaml`，文件缺失时使用相同内置默认值。按岗位/JD 标签字面出现次数选择方向；同分按文件顺序。示例用于风格参考，不作字符串填空，也不能作为事实来源。生成前核对选定方向，岗位跨方向时仍只取一条最贴的结果。模型自检：“假设读的人是不懂技术的 HR，这句话看得懂吗？”

`facts` 是本人核实的硬结果，每条独立，保留数字和归因。可以按岗位改写成聊天说法，不能合成多条无关成果。示例仅供风格参考，不是事实来源。小故事可替代主经历，或在四段稿的第三段补充；三段稿使用故事时主来源就是该故事。

`$JOB_AGENT_HOME/greeting-generation.sqlite3` 为独立私有缓存/账本，不改 schema v7 的共享业务库。键由文案规则版本、平台、岗位 ID 和 JD/公司简介哈希组成；这轮规则升级隔离旧四段缓存，同版本相同岗位相同 JD 不重复请求。缓存保存 canonical 3–4 段，再按平台渲染；缓存命中也登记到当前批次参与去重。配置/facts 更新不自动刷新同版本缓存，同批出现内容冲突时允许在原预算内重写；其他失效稿明确回退。中途退出的已认领请求不自动重放。

每次网络调用前持久化占用当日调用额度（按 engine.timezone），重试也计入；记录 run_id、模型、状态、token 用量和耗时。缺失 usage 记未知，不猜。只存安全错误码，不保存 provider 错误正文或凭据；文案只在私有缓存和已有不可变批次中保存。API 异常立即兜底；lint 连续两次不过、预算耗尽、配置/材料异常均用通过当前 lint 的通用兜底文案。若自定义规则连通用文案也拒绝，阻止规划，不把无效文案交给发送器。手动和每日汇总包含 generated / fallback / blocked 等计数，daily 的结构化结果保留 `greeting_generation`。

`send_message` 才在普通规划里生成；`platform_default` / `none` 不付费。只供离线审阅的显式 `preview_only=True` 可以生成智联草稿；不开放发送入口。MCP 现有预览、设置、统计入口保留。

## 小巧思素材

可选文件 `$JOB_AGENT_HOME/greeting-facts.yaml`，格式见 `greeting-facts.example.yaml`：`version: 1`，每条包含 `id / text / when / tags / confirmed / source`（`when` 为可选的人类可读适用说明）。未确认候选必须 `confirmed: false`；严格 YAML 布尔 `true` 才可使用，字符串 `"true"` 不接受。文件缺失则不用，格式错误则安全兜底。按 JD 和岗位名的标签/when 关键词命中数选最相关的一条；“主动性、务实”等宽泛标签不构成相关性，也不凭公司简介触发故事；无命中不用，模型也可以不选它。

未确认条目不会进入 prompt。使用时保持事实，可拆句改写；替换主经历或补充一段小故事，元数据记录 `anecdote_id`。待确认候选不能加入硬结果 `facts` 绕过确认。2026-11-28 用户确认的四条素材已放进仓库示例与本地预览目录，生产文件由 Claude 后续处理；生产目录只读。

## 冻结、统计与账号默认语

预览把完整逐岗位文本写入已有不可变 `greetings_json`，保留平台默认键；确认/执行验证文案和配置摘要。执行从批次读取，`deliveries.greeting` 记录实际交给适配器的文本，不把预览当作送达。BOSS 回执采用 #63 忽略空白比较；效果对比沿用 #64 按内容哈希分组，新旧各至少 20 条后再讨论回复率。

`sync_default_greeting(platform="zhilian", confirm=false)` 只读对比。只有用户审阅并明确授权才传 `true`；不同才写，保存后刷新回读，任何不确定不重试、不截断，不发 HR 消息。51job 同步返回 unsupported。线上开关、生产素材文件、启用、合并、部署和真实发送均由 Claude/用户后续处理，本 Issue 只生成预览。
