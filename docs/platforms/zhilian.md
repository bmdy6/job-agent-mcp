# 智联招聘：P0 原站取证

2026-11-24，Ego Lite 已登录 profile，独立空间「job-agent 取证 A」。查看现有投递记录、聊天、岗位详情与招呼语设置。只输入临时测试文字并清空、取消，没有保存设置、投递、发送消息或发简历。首次投递动态流程与附件选择仍需 P1 的授权单岗位验收。

## 输入长度：两种输入框必须分开

**招呼语设置框实测上限为 500 个 BMP 中文字符。** `/im/greeting/setting` →「招呼语」→「添加招呼语」是未保存的弹窗，textarea 有 `maxlength="500"`，计数器 `/500`。系统粘贴 499、500、501、700 字，保留 499、500、500、500；超长自动截断。已清空并点「取消」，没有保存。[长度夹具][Z1]

已有聊天页的 `textarea.im-sender__input[placeholder="从这里开启对话…"]` 没有 `maxlength`。粘贴 500、501、1000、1001、2000 字均完整保留；清空后原有气泡数量不变。**这里只能证明输入容量至少 2000，服务端发送上限未知**，也不能反推首次招呼设置支持 2000 字。现有发送处理会 trim 文本后交给 sendText，未调用它。[聊天输入实验][Z2]、[运行时观察][Z10]

P1 若使用平台默认招呼设置，保守校验 ≤500 UTF-16 单元；若走自定义聊天消息，须先验证实际发送上限，不应沿用旧适配器的 300 字，也不应直接把 2000 写成已验证上限。此次只测试 BMP 中文，没有验证表情、组合字符和服务端计数。[Z1][Z2]

## 从岗位到提交

| 阶段 | 原站证据与动作边界 |
| --- | --- |
| 新岗位详情 | `.job-summary .summary-planes` 有「立即投递」。本次只读，未点击；它是潜在直接提交边界，不能假设先弹确认框。[新岗位][Z3] |
| 点击后的流程 | **未动态验证**。只读当前页面 `startChat` 方法看到它向 job-apply widget 发 `preChat`，参数中 `isShowAttachmentSelect=false`。这只是当前代码分支线索，不能推广为所有投递都不选附件。[Z3] |
| 成功回执 | 页面挂载了隐藏 `.deliver-greeting-modal`，标题为「已向对方发送简历和打招呼语」，含「留在此页 / 继续沟通」。它在本次新岗位页是隐藏的，**不是本次投递成功证据**。原站方法对 `APPLY_SUCCESS` 设置「已投递」，未触发。[Z3] |
| 已投岗位 | 对照已有成功投递记录的详情页，按钮实际显示「继续沟通」。不是所有已投岗位都会长期显示「已投递」。[已有岗位][Z4]、[投递记录][Z5] |
| 聊天消息 | 输入框下提示 Enter 发送，有「发送」按钮。只输入/清空，未触发提交。[发出消息结构][Z6] |

从只读证据可知，“立即投递”之后可能已发送简历与招呼语；**不能把成功弹窗里的「继续沟通」当作最后的申请确认按钮**。首次申请在哪次请求提交、是否还有附件选择/其他拦截，须在明确授权时跟踪完整流程。此次没有越过入口。[Z3]

## 已投识别与防重复

`https://i.zhaopin.com/schedule` 有「投递成功 / 被查看 / 有意向 / 邀面试 / 不合适」分类；记录行有岗位链接、公司、状态「成功投递」及日期和时分秒。本次首页看到 7 条记录并有分页，不能把一页当全部历史。职位链接旧格式为 `jobs.zhaopin.com/<jobNumber>.htm`，详情会转到 `www.zhaopin.com/jobdetail/<jobNumber>.html`；主键取岗位 number，不取跟踪 query。[Z5]

防重优先匹配 jobNumber + 已有投递历史，再结合详情「继续沟通」、已有本人消息和附件卡片。HR 主动开聊不能单独证明本人已投。设置 `APPLY_SUCCESS` 的静态方法或隐藏成功模板都不能作为回执。[Z3][Z4][Z5][Z6]

## 聊天列表、正文与消息 ID

入口 `https://i.zhaopin.com/im`，列表 `.im-session-item`，有全部、未读、新招呼、沟通中、不合适分类。初始异步页面曾短暂显示未登录/空列表，待账号与会话加载完成后才读到数据；不能把加载中当无回复。[Z6][收到消息][Z7]

| 字段 | 当前原站结构 |
| --- | --- |
| 公司、岗位 | `.im-session-item__company-name`、`__job`；列表 `__name` 是 HR，夹具已替换。[Z6][Z7] |
| 岗位/会话标识 | `.im-message` 的 Vue `$props.session` 有 `jobNumber`、`sessionId`、`companyName`、`jobTitle`。jobNumber 是字符串。[模型][Z8][Z7] |
| 方向 | 气泡 `.im-message__bubble--me` 对应 `msg.fromMe=true / flow="out"`；入站样例 `false / "in"`。入站也包含系统/业务卡片。[Z8][Z7] |
| 正文 | 普通文本 `.im-msg-text`，模型 `type="text"`、`body/content` 为字符串；custom 卡片的 content 是对象，不能直接当正文。[Z8][Z7] |
| 时间 | 列表 `__time`、消息时间节点用于显示；模型 `msg.time` 为数值。模型夹具的时间替换为合成数值；DOM 片段部分保留时分。没有验证 epoch 单位和跨日展示规则。[Z8][Z7] |
| 消息 ID | 模型 `id / idClient / idServer / sendMessageId` 均见非空字符串；当前样例 `id==idClient`，其他 ID 不相等。DOM 上没有在此样例发现可直接替代的 `data-mid`。[Z8] |
| 业务卡片 | 出站附件 `type="custom" / cardType="177"`，DOM `.im-msg-177` 和 `__name`；入站另有 131、11、336 等卡片，不是普通 HR 文本。[Z6][Z7][Z8] |

**同一消息在嵌套 `.im-message` Vue 根上会被读到两次。** 模型夹具保留重复的同一替代 ID，可验证先按稳定消息标识去重。P2 建议以平台+会话+idServer 去重；idServer 缺失时保留待处理并验证其他 ID 的稳定性，不用正文哈希宣称永久唯一。只对已投岗位的入站文本推送；卡片另行分类，禁止把所有 `flow=in` 当 HR 自由文本。[Z7][Z8]

## 默认招呼语能否改、能否关

`https://i.zhaopin.com/im/greeting/setting` 的「招呼语」说明：发起沟通时自动向招聘者发送招呼语。界面有自定义/常规/幽默/礼貌/诚恳分类、自定义条目、编辑图标、设为默认，以及添加弹窗的输入/保存/取消，证明支持自定义编辑入口；本次没有保存，也没有改变默认项。[设置][Z9][Z1]

能否关闭：当前所看面板没有独立 switch/checkbox 开关；条目有「设为默认」，没有据此推断当前一定未设默认，也没有点击它试关闭。**关闭能力和当前实际默认条目尚未确认**，不能写死“智联没有关闭能力”。[Z9]

## 附件选择与读回

已有聊天中看到了与指定附件名匹配的 PDF 卡片（夹具只保留匹配布尔值，文件名替换）。出站附件结构 `.im-msg-177__name` 可用于将来提交后读回名称；这证明以前那次会话已有该附件，不能证明本次新投递会自动选中它。[Z6]

聊天工具栏有「发简历」。只读 `onSendResume` 方法先做会话、锁定与 `checkSendResumeIntercept` 检查，然后 `openSendResume`；没点按钮，因此选择器候选、唯一选中标记、确认按钮和保存后的读取路径仍缺少原站夹具。P1 应在授权岗位上验证唯一文件名/版本、选中状态和提交后附件卡片，歧义时不提交。[Z10]

## 风控与取证局限

本次已访问页面未见验证码或频率上限弹窗。招呼语框明示不要填写手机、QQ、微信或广告；简历入口静态方法存在拦截/limitMessage 分支，但本次未触发，无法确认账号当前限额、间隔、敏感内容规则。[观察][Z11][Z10]

全部夹具来自当前原站 DOM 或已加载模型的脱敏投影。HR、公司、正文、附件名、链接标识、时间都作替换；未保存 cookie、token、头像或完整 raw 对象。JSON 保留方向/类型/状态及嵌套重复 ID；卡片 content 内值统一脱敏，不保留内部值类型，不可当完整 API schema。HTML 删去脚本、样式、媒体链接、框架散列属性与动态原文；隐藏模板须结合额外 `visible=false` 判断。[Z3][Z6][Z7][Z8]

[Z1]: ../../mcp/tests/fixtures/platforms/zhilian/greeting-limit.json
[Z2]: ../../mcp/tests/fixtures/platforms/zhilian/chat-input-limit.json
[Z3]: ../../mcp/tests/fixtures/platforms/zhilian/job-new.json
[Z4]: ../../mcp/tests/fixtures/platforms/zhilian/job-existing.json
[Z5]: ../../mcp/tests/fixtures/platforms/zhilian/applications.json
[Z6]: ../../mcp/tests/fixtures/platforms/zhilian/chat-outgoing.json
[Z7]: ../../mcp/tests/fixtures/platforms/zhilian/chat-incoming.json
[Z8]: ../../mcp/tests/fixtures/platforms/zhilian/message-model-outgoing.json
[Z9]: ../../mcp/tests/fixtures/platforms/zhilian/greeting-settings.json
[Z10]: ../../mcp/tests/fixtures/platforms/zhilian/resume-boundary.json
[Z11]: ../../mcp/tests/fixtures/platforms/zhilian/risk-observation.json

## 采集

2026-11-24，独立空间「job-agent 采集 A」，通过 `EgoBrowser` 搜索「AI应用开发」+ 上海。只读取搜索和 1 个独立详情，未点击投递、沟通、收藏；未见验证码、风控或登录失效。

- 旧 `/sou/jl538/?kw=…` 跳到 `/jobs/?jl=538&kw=…`。城市名到编码由 `pages.py` 映射，已配置上海 538、苏州 639、杭州 653、南京 635，未知城市报错。页码 query 在新界面不生效。
- 新列表 `.job-card`，卡片 `__vue__.$props.job` 白名单读取 `number / positionURL / positionUrl / name / companyName / workingExp / education / companySize`；薪资 `.job-card__salary`，地区 `.job-card__location span`。标题虽在部分 innerText 快照缺失，模型 name 可读；不读取 HR 和追踪字段。详情链接去 query，岗位 ID 使用 number。
- 首批 20 条，列表面板 `.job-list-panel` 的 `hasMore` 标识结束。滚动最后一条、等待 8–15 秒后第二批返回 20 条。每次 search 新建临时页并从首批滚动到请求的批次，结束时关闭。**请求第 N 批实际重访前 N−1 批，调度应按底层加载批次核算预算。** 本轮首批及一次回放首批+第二批，共 3 批；自然终页未实测，终页 `[]` 通过合成测试验证。
- 独立详情 `/jobdetail/<number>.htm`，同时支持既有 `.html` 格式；JD 当前 `.job-description__content`，兼容旧 `.describtion__detail-content / .describtion-card__detail-content`。核对详情 URL ID，返回 JD 全文。本轮 1 个详情原文 594 字符；夹具已整体替换正文。
- 验证码、登录失效、结构或 JD 不匹配抛 `BrowserError` 子类（`risk_control / login_required / collection_structure`）。只轮询页面加载，不重试导航、不绕过。风险与登录异常只做离线合成验证；不主动触发真实风控。

脱敏证据：[列表](../../mcp/tests/fixtures/platforms/zhilian/search-list.json)、[独立详情](../../mcp/tests/fixtures/platforms/zhilian/search-detail.json)、[翻页回执](../../mcp/tests/fixtures/platforms/zhilian/search-pagination.json)。保留前两条列表的结构，公司、岗位名称、标识、URL 已替换，详情文本整体替换，不保存 cookie、HR 姓名或完整模型。空间锚点保留，不 finish。
