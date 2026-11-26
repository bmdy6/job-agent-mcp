# 猎聘 P0 原站取证（recon-b，2026-11-24）

结论：聊天编辑器实测上限 **800 个 BMP 汉字**；默认招呼语是 **45 个预设单选**。已找到网页收件消息的结构，但现有收件样本属于带 AI 标记的顾问推荐，尚无「已投岗位的人工 HR 回复」样本。投递提交链和附件发送确认仍未完成实测。本页不能作为 P1/P2 已验收的依据。

## 范围与证据

读取 #15 全文、#9 及其最新评论、PR #13。外部源码和 PR #325 仅作线索，未复制进本仓库。浏览器仅使用现有 Ego Lite，专属空间按名创建为 `job-agent 取证 B`，所有标签页由本任务创建并关闭。未操作其他任务空间或服务。

以下夹具位于 [`mcp/tests/fixtures/platforms/liepin/`](../../mcp/tests/fixtures/platforms/liepin/)。JSON 是原站 DOM/运行时数据的裁剪、脱敏投影，**不是原始 API 响应**；姓名、公司、正文、ID 均替换，字段类型和 class 保留。消息 ID 是稳定占位符，不能用于线上去重。原始 HTML、截图和哈希对应的原件仅保留在本地任务目录 `evidence-private/`，不提交。

| 问题 | 本次证据 | 覆盖边界 |
| --- | --- | --- |
| 登录、已有申请 | [application-list.json](../../mcp/tests/fixtures/platforms/liepin/application-list.json) | 登录有效；前后均 2 条 |
| 输入长度 | [input-limit.json](../../mcp/tests/fixtures/platforms/liepin/input-limit.json) | 799/800/801 字边界；未发送 |
| 未投岗位控件 | [unapplied-job.json](../../mcp/tests/fixtures/platforms/liepin/unapplied-job.json) | 仅观察按钮 |
| 已有会话状态 | [applied-job.json](../../mcp/tests/fixtures/platforms/liepin/applied-job.json)、[applied-lptjob.json](../../mcp/tests/fixtures/platforms/liepin/applied-lptjob.json) | 两种详情路径均「继续聊」 |
| 简历附件 | [attachment-inventory.json](../../mcp/tests/fixtures/platforms/liepin/attachment-inventory.json) | 文件存在；未选中发送 |
| 发件/系统消息 | [employer-messages.json](../../mcp/tests/fixtures/platforms/liepin/employer-messages.json) | 真实雇主已有会话；没有收件正文 |
| 非空收件结构 | [incoming-recommendations.json](../../mcp/tests/fixtures/platforms/liepin/incoming-recommendations.json) | AI 顾问推荐；不是已投岗位回复 |
| 默认招呼语 | [greeting-presets.json](../../mcp/tests/fixtures/platforms/liepin/greeting-presets.json) | 45 个预设；取消退出 |
| 风控 | [risk-observation.json](../../mcp/tests/fixtures/platforms/liepin/risk-observation.json) | 本次未遇到，不代表无阈值 |

## 输入上限

从 `https://c.liepin.com/job/record/apply` 右下角 `.im-ui-basic-entry` 进入「我的沟通」，打开**已有**会话。输入框为 `textarea.im-ui-textarea`，placeholder 是「请输入文字，按Enter键发送」。没有 `maxlength` 属性，不能据此认为不限长。

实际使用键盘 `insertText` 输入重复的「测」，读取 `textarea.value.length`：799 → 799、800 → 800、801 → 800。较早 1001/2001/5001 字输入也只剩 800 字。因此当前网页编辑器有前端截断。测试全程不按 Enter、不点击「发送」，最后清空并读回为空。证据：[input-limit.json](../../mcp/tests/fixtures/platforms/liepin/input-limit.json)。

这个数适用于**已有会话的文本编辑器**，不是首聊弹窗或服务端限制实测。emoji、组合字符的计数未测。P1 不应静默截断用户文案，也不应把后续消息上限当作首聊入口的已验证上限。

## 投递路径与危险边界

未投岗位详情的 `.apply-box` 内存在：

1. `a[data-selector="apply-job"]`，文案「投简历」。
2. `a[data-selector="chat-chat"]`，文案「聊一聊」，招聘官区域另有同名入口。

两类控件带 `data-jobid`、`data-jobkind`；聊天控件还带 JSON `data-params`。本次样本的 DOM `data-jobkind` 与 `data-params.jobKind` 分别为 `6`、`2`，**不能随意互换或仅按 URL 前缀猜岗位类型**。岗位卡路径含 `/job/19….shtml`，其路径数字也不能直接当作真实 source jobId。证据：[unapplied-job.json](../../mcp/tests/fixtures/platforms/liepin/unapplied-job.json)。

原站公开页面脚本的只读检查表明，「投简历」点击会进入申请函数，传入 `applyData.jobId/jobKind`；本次没有执行它，**尚未证实是首击即申请，还是还会出现确认步骤**。因此首击就是本轮的停止边界，不能为了看下一屏点击。脚本来源是原站已加载的 [job bootstrap](https://concat.lietou-static.com/fe-www-pc/v6/js/src_pages_job_bootstrap_js-src_components_DoraEntry_edgeLight_module_less-src_components_Dora-d0196f.2898316b.js)。后续实际产生申请的请求、回执和失败分支待授权单岗位验证。

「聊一聊」可能自动发平台招呼语；本次一个也没点。已投页面出现「继续聊」，而且原投递记录仍存在。**「继续聊」只可视为已有会话信号**；是否已申请，需结合投递记录按岗位 ID 核对，不能只看聊天按钮。证据：[applied-job.json](../../mcp/tests/fixtures/platforms/liepin/applied-job.json)、[application-list.json](../../mcp/tests/fixtures/platforms/liepin/application-list.json)。

## 附件简历

`https://c.liepin.com/resume/edit` 的「附件简历」区域可直接读到 `示例候选人_简历.pdf`。文件名节点及祖先 class 见 [attachment-inventory.json](../../mcp/tests/fixtures/platforms/liepin/attachment-inventory.json)。首页「附件简历 / 去同步」实际进入 `/resume/parse-result` 的在线简历同步向导，并不是投递附件选择器；本次未点击「更新并继续」。

已有雇主会话里有「发简历」操作，历史简历卡的 `extType=203`，`bizData` 包含 `attachmentResume` 和 `onlineResume`。这只说明历史卡包含两类简历入口，未证明本轮将发送哪份附件。为避免点击即发送，本次未点击「发简历」，**选择弹窗、选中态及发送前文件名读回仍未验证**。证据：[employer-messages.json](../../mcp/tests/fixtures/platforms/liepin/employer-messages.json)。

## 已投状态

访问 `https://c.liepin.com/job/record/apply`，等列表加载后读取每条岗位链接、公司、状态、投递时间。实际两条均为「已投递」，页面还提供「已查看 / 面试邀约 / 不合适」过滤项。前后数量均 2。`/job/…shtml` 与 `/lptjob/…` 都在真实记录中出现。证据：[application-list.json](../../mcp/tests/fixtures/platforms/liepin/application-list.json)。

## 会话与收件结构

安全入口：投递记录页 → `.im-ui-basic-entry` → `#im-basic-contact`「我的沟通」→ 已有联系人行。不经过岗位首聊入口。会话列表展示招聘者、职务/公司、最近摘要、时间和未读数，并提示「仅支持查看180天以内的会话」。列表摘要可能是我方消息或系统提示，不能直接转成 HR 回复。

消息 DOM 在 `.im-ui-message-list-wrapper` / `.im-ui-msg-list-content` 内，每项 `.im-ui-message-item-wrapper`；正文区域 `.im-ui-message-item-normal-message-wrapper`，时间 `.im-ui-system-tip.format-time`。网页不把稳定消息 ID 放在行属性中。本次只读沿行的 React 内部实例找到上层 `memoizedProps.message`，读到以下字段；这属于当前页面实现细节，不是平台公开接口。

| 所需信息 | 原站字段/节点 | 处理限制 |
| --- | --- | --- |
| 岗位 ID | 详情 `data-jobid` / `data-params.jobId`；收件职位卡 `bizData.communicateJobId` | 联系人 ID、消息 ID 均不是岗位 ID |
| 公司 | 联系人 `contact.company`；职位卡 `bizData.jobCompany` | 顾问公司可能不同于招聘公司 |
| 方向 | `message.direction`；DOM `im-ui-message-item-receive` / `…-send` | 实测 `"1"` 收件、`"0"` 发件；系统消息也可能为 `"0"` |
| 正文 | `message.msg`，结合 `extType` / `bizType` | `msgType="txt"` 也可能是卡片/系统消息 |
| 时间 | `msgTime`（毫秒数）、`formatTime` | 优先原始时间，不把「昨天」当稳定键 |
| 消息 ID | `msgId` | 以平台+会话+原始ID去重；夹具已替换ID |

证据：[employer-messages.json](../../mcp/tests/fixtures/platforms/liepin/employer-messages.json)、[incoming-recommendations.json](../../mcp/tests/fixtures/platforms/liepin/incoming-recommendations.json)。

雇主已有会话仅见我方招呼语、简历卡和系统提示：`extType=1` 文本、`200` 系统提示、`203` 简历卡、`214` 简历代投卡。另一会话读到非空收件文本和 `extType=221` 职位/意向卡，但页面明确写「内容由AI生成」，职位/意向卡 `sourceFlag=3`，其中一条文本的 `bsData.aiMark=true`，职位不在两条已投记录里。只能用来验证接收方向和过滤反例，**不能证明人工 HR 回复，也不能因 `direction="1"` 就通知用户已获回复**。

P2 当前缺口：需要已投岗位的真实人工收件样本；需要建立联系人→所投岗位映射，验证卡片/AI/系统排除和消息去重。没有样本时返回未验证，不能返回「已验证无回复」。

## 默认招呼语、风控

从已有会话系统提示「去修改＞＞」打开「请选择打招呼语」，实测 45 个 radio，值覆盖 `1…45`。没有自定义文本输入框、关闭开关或「不发送」选项。仅确认当前界面只提供预设；没有保存任何修改。证据：[greeting-presets.json](../../mcp/tests/fixtures/platforms/liepin/greeting-presets.json)。

本次未遇验证码、频率限制或账号异常。普通求职安全提示不能误判为风控。未测每日配额、连续发送阈值或服务端消息长度，不能给出平台安全发送速率。证据：[risk-observation.json](../../mcp/tests/fixtures/platforms/liepin/risk-observation.json)。

## 交给 P1/P2 的明确结论

- 可用：800 字编辑器边界、45 预设、已投列表/既有会话的 DOM 和消息字段。
- 必须继续保守：首击申请的具体时序、附件发送前确认、人工 HR 回复样本均未验证。
- 本次只改文档和夹具。未实现适配器、未投递、未发消息/简历、未通知 Telegram。

## 采集

2026-11-24，collect-b 通过项目 `EgoBrowser` 在专属「job-agent 采集 B」只读搜索 `AI应用开发 / 上海`。搜索入口 `https://www.liepin.com/zhaopin/`，参数 `key`、`dqs=020`、`currentPage=0`（对外页码从 1 开始）。实测第二页 `currentPage=1` 返回不同岗位；本次仅访问第一页和第二页，含一次参数校正共 3 次搜索导航，详情 2 次。未点击沟通、申请、收藏，未遇验证码。只关闭临时页，保留锚点，不 finish。

- 列表：`a[data-nick="job-detail-job-info"]`，卡片 `.jobDetailBox`，标题 `.jobTitleBox .ellipsis-1`，公司 `[data-nick="job-detail-company-info"] .companyName`；城市、薪资、经验、学历由该岗位链接的文本分行读取。避开构建时生成的 class。
- 岗位 ID：优先 `data-jobid`；本次列表该属性缺失，读取 `data-tlg-scm` 的 `cid=种类_ID`。详情 `.apply-box [data-jobid]` 已核对与 cid 后半段一致。URL 路径中的数字不能代替该 ID。
- 详情：`.job-intro-container [data-selector="job-intro-content"]` 读取完整 JD，核对真实岗位 ID；不包含招聘者和推荐岗位。
- 页码：`.ant-pagination-item-active` 的 title。明确空结果提示且无岗位才返回 `[]`；空框架、缺字段、页码不符一律 `BrowserError`，不当成已到底。末页/空结果分支是离线构造测试，未在真机访问最后一页。
- 登录：`.header-quick-menu-username` 只转布尔值；登录弹窗、跳转登录页或失效时抛 `login_required`；可见验证/风控时抛 `risk_control`，不重试。

[search-list.json](../../mcp/tests/fixtures/platforms/liepin/search-list.json)、[search-detail.json](../../mcp/tests/fixtures/platforms/liepin/search-detail.json)、[search-page2.json](../../mcp/tests/fixtures/platforms/liepin/search-page2.json) 是原站 DOM 的脱敏投影。列表各保留前两条，ID、公司、标题替换，详情 JD 全文保留；没有姓名、cookie 或追踪参数。第一页投影来自探索读取，第二页和详情由生产表达式读取。

城市映射在 `pages.py`。上海已真机验证；苏州、杭州、南京来自用户允许参考的配置，合肥来自同一工具的城市数据，未逐城实测。不使用参考源码中过时的三位 fallback。未知城市（包括当前未核实的无锡）直接 `ValueError`，不悄悄改成全国。关键词硬过滤与 `salary_min_k` 由调度层处理。

### O 集成补充（2026-11-24）

苏州「AI应用开发」列表同时包含 `/job/<数字>.shtml` 与 `/a/<数字>.shtml`，后者也保留页面给出的 cid 岗位 ID，不用链接数字覆盖。已为 `/a/` 增加严格路径校验及回归测试，其他未知路径继续报错。苏州冒烟累计 3 次第一页访问，最终一次取得 42 条；重复读取间结果有变化，数据库按稳定 ID 去重后共新增 44 条。

每日列表预算为每次搜索 1 页；失败也扣预算。未改浏览器生命周期。
