# BOSS 直聘：P0 原站取证

2026-11-24，Ego Lite 共用登录态，独立空间「job-agent 取证 A」。只打开岗位、设置和已有聊天；没有点击「立即沟通」、发送、发简历或保存设置。所有测试草稿已清空。以下区分实测、页面静态线索和待验证项；不能据此宣告 P1 投递验收通过。

## 输入长度

**正常粘贴上限实测为 1000 个 BMP 中文字符。** 对已有聊天的 `#chat-input[contenteditable=true]` 用系统粘贴分别输入 999、1000、1001 字，保留量为 999、1000、0；1001 字提示「您粘贴的内容过长」，整段拒绝，不是截成 1000。测试后输入框为空，原有 2 条消息数量不变。[输入实验][B1]

这只是粘贴路径的客户端限制。`fill` 曾保留 6000 字，说明编辑 DOM 的容量与粘贴检查不同；**不能把 6000 当可发送长度，也不能把 1000 宣称为服务端消息上限**。只读组件状态 `MAX_PASTE_LENGTH=1000`，粘贴处理用 JavaScript 字符串 `.length` 比较。此次没有测表情、组合字符和服务端校验，也没有打开首次沟通弹窗。[运行时状态][B2]

P1 当前可保守限制正常粘贴文案 ≤1000 UTF-16 单元；最终配置须区分首次招呼框与已有聊天框，发送上限留给授权单岗位验收。超限直接拒绝，不自动截断。[B1][B2]

## 从岗位到提交

| 阶段 | 原站看到的内容 | 是否执行、边界 |
| --- | --- | --- |
| 打开岗位详情 | 路径 `/job_detail/<encryptJobId>.html`，操作区 `.btn-startchat` | 已执行；页面可能有上下两个同类按钮，不能假设选择器唯一。[新岗位][B3] |
| 新岗位入口 | 按钮「立即沟通」 | 未点击；按可能立即建立沟通/发默认招呼的提交入口处理。[B3][B7] |
| 首次沟通中间步骤 | 本次没有触发 | 是否弹招呼框、是否另需确认、哪次请求创建关系，均待授权实测；不从旧自动化代码推断已证实。 |
| 已有会话 | 对照岗位显示「继续沟通」；聊天里有本人消息及「送达」 | 只读已有结果，没有重新点击沟通按钮。[已有岗位][B4]、[已有发出消息][B5] |
| 输入自定义消息 | `#chat-input`；界面提示 Enter 发送、Ctrl+Enter 换行；有「发送」按钮 | 只输入和清空；点击发送或 Enter 是明确的消息提交边界，不执行。[B5] |

因此，本次能定位危险入口和现有回执，**首次申请/首条招呼真正发生在哪一步仍未动态验证**。P1 不应为了探路点击「立即沟通」，也不应把进入聊天页算发送成功。[B3][B4][B5]

## 防重复与聊天字段

岗位详情的「继续沟通」可以作为已沟通过的证据；对应聊天中的本人完整文本和送达/已读状态可作为已有消息证据。列表出现 HR 主动招呼，并不等于本人已经投递，必须结合发出方向和岗位标识。[B4][B5][会话模型][B6]

| 字段 | 原站位置、注意点 |
| --- | --- |
| 岗位 ID | 列表虚拟行 `[role=listitem].__vue__.$props.source` 有 `encryptJobId` 和数字 `jobId`；详情 URL 用加密 ID。不要把二者混成一个键。[B6] |
| 公司、岗位 | `source.brandName`、`jobName`；列表可截断公司名。夹具已改为示例名称。[B6] |
| 列表方向 | `source.lastIsSelf` 是布尔值；`lastText`、`lastMsgId`、`lastTS`、`lastMsgStatus`、`unreadCount` 可读。数字状态码本次不完整枚举。[B6] |
| 消息方向 | `.message-item.item-myself` 是本人；`.item-friend` 可能是 HR，也可能是系统卡片，不能直接当 HR 回复。[B5][B8] |
| 正文、ID | 文本在 `.text-content`；消息行有 `data-mid`。系统卡片另有 `.message-card-wrap`，夹具同时保留两种结构。[B5][B8] |
| 时间 | 消息行 `.item-time .time` 只在部分行出现；例子有 `11:26`、`12:58`，不是每条都有完整日期。列表 `lastTS` 是数值字段，不能用相对时间文本拼造每条消息的精确时间。[B6][B8] |

建议 P2 以平台、会话/岗位、`data-mid` 组合去重；先确认已投集合，再排除本人、系统和非文本卡片。缺失日期/ID时保留未知。Vue 私有属性只是当前页面读取点，未来版本变化必须报结构不匹配。[B5][B6][B8]

## 默认招呼语

路径 `/web/geek/notify-set` →「设置打招呼语」，实际 URL 带 `?type=greetSet`。页面说明「启用后，将在沟通时自动发送招呼语」。开关 `.ui-switch` 的 hidden input 为 `0`，组件 `value=0 / falseValue=0 / trueValue=1 / currentValue=0`，确认目前**关闭**，本次未切换。[设置夹具][B7]

能否关闭：界面与状态支持关闭，当前就是关闭。能否修改：DOM 挂载了「修改打招呼语」输入/保存模板，但关闭状态下未打开编辑流程，**只能确认模板存在，未验证可编辑范围与保存限制**。没有为取证打开开关。[B7]

## 附件简历

个人中心附件管理中存在要求的附件；本地只记录名称匹配布尔值，夹具名称脱敏。它证明附件存在，不证明将来某次操作选中了它。[附件管理][B9]

**不要把「发简历」当作安全的选择器入口。** 只读原站 toolbar 组件的 `handleSendResume` 方法发现：有检查和弹框分支，也有已有 `resumeId` 时直接调用 `handleConfirm` 的分支；后者调用含 `resumeId` 的 request。因此本次未点击，未触发简历请求。[提交边界夹具][B10]

附件如何选择、选中后如何读回：本次未安全打开选择器，仍待 P1 授权单岗位验收。到时须匹配指定附件全名、确认唯一候选及选中状态，再由提交后的附件卡片核实；这些是验收要求，尚无原站选中夹具。[B9][B10]

## 风控与证据边界

本次查看的岗位、聊天、设置页未见验证码、频率限制或沟通额度耗尽提示；见到的「粘贴内容过长」是输入校验。未压测、未提交，无法推断每日额度、间隔或风控阈值。[观察记录][B11]

夹具是原站 DOM/已加载模型的脱敏投影，不是旧适配器代码或其接口回包。正文、HR、公司、标识和附件名已替换，样例之间不承诺同一真实岗位映射；保留 class、方向、状态、节点层次与消息标识位置。没有保存 cookie、token、头像链接或 securityId 的值。`message`/`conversation` 模型是字段投影，不是原站完整响应契约。[B5][B6][B8][B9]

[B1]: ../../mcp/tests/fixtures/platforms/boss/input-limit.json
[B2]: ../../mcp/tests/fixtures/platforms/boss/input-runtime.json
[B3]: ../../mcp/tests/fixtures/platforms/boss/job-new.json
[B4]: ../../mcp/tests/fixtures/platforms/boss/job-existing.json
[B5]: ../../mcp/tests/fixtures/platforms/boss/chat-outgoing.json
[B6]: ../../mcp/tests/fixtures/platforms/boss/conversation-model.json
[B7]: ../../mcp/tests/fixtures/platforms/boss/greeting-settings.json
[B8]: ../../mcp/tests/fixtures/platforms/boss/chat-incoming.json
[B9]: ../../mcp/tests/fixtures/platforms/boss/personal-center.json
[B10]: ../../mcp/tests/fixtures/platforms/boss/resume-boundary.json
[B11]: ../../mcp/tests/fixtures/platforms/boss/risk-observation.json

## 采集

2026-11-24，独立空间「job-agent 采集 A」，经 `EgoBrowser` 搜索「AI应用开发」+ 上海。只读搜索和独立详情页，未点击收藏、立即沟通或任何投递入口。未见验证码、风控或登录失效；停止和异常路径通过离线测试覆盖，未在原站主动制造。

- 搜索入口实际为 `/web/geek/jobs?city=101020100&query=…`；旧 `/web/geek/job` 会跳转。城市映射集中在 `pages.py`，目前覆盖上海、苏州、杭州、南京，未知城市直接拒绝，不能静默搜全国。
- 列表 `.job-card-wrap`，标题与链接 `.job-name`，薪资 `.job-salary`。卡片 `__vue__.$props.data` 只投影 `encryptJobId / brandName / cityName / salaryDesc / jobExperience / jobDegree / brandScaleName`；不读出 HR、securityId、头像等字段。原站 ID 取详情 URL 的 encryptJobId，去掉 query。
- 首批 15 条，无页码按钮。父 PageJobs 的 `hasMore` 是结束标记，`pageVo.pageSize=15`。滚动最后一条并等待 8–15 秒后第二批返回 15 条。每次 `search(page=N)` 新建临时页，从首批滚动到第 N 批并返回切片，用完关闭；终页返回 `[]`，未加载或缺少结束证据报错。**第 N 批会重访前 N−1 批，调度的访问预算应按实际加载批次计数，不能只按 search 调用次数。** 本轮每平台只读取首批及一次回放首批+第二批，共 3 批；未到自然终页。
- 页面薪资为 PUA 字符，例如夹具中的加密原文与模型明文 `18-35K·14薪` 对应。当前采用同一卡片 `salaryDesc` 明文还原，不使用固定字体码表，不下载字体；明文缺失且 DOM 仍加密时抛 `collection_structure`。未来若原站连模型也加密，需要另行实现字体解码。
- 独立详情 `/job_detail/<id>.html`，JD `.job-sec-text`，读取全文并核对 URL ID。实读 1 个详情，原文 1344 字符，夹具正文整体替换，不能用于评价真实岗位内容。
- `risk_control / login_required / collection_structure` 都继承 `BrowserError`；导航、只读轮询、解析的失败都会关闭临时页，不重试导航、不绕过。空间锚点保留，不 finish。

脱敏证据：[列表](../../mcp/tests/fixtures/platforms/boss/search-list.json)、[独立详情](../../mcp/tests/fixtures/platforms/boss/search-detail.json)、[翻页回执](../../mcp/tests/fixtures/platforms/boss/search-pagination.json)。列表只留前两条结构样例，公司、名称、标识、URL 已替换；详情内容整体替换。`ready/has_more` 与薪资对应关系来自实读投影。终页、验证码和登录失效为合成测试，不宣称原站实测。
