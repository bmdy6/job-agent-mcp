# 51job 原站取证与投递验收

## BTY-4：只读申请对账（2026-11-26）

```bash
cd mcp
PATH=$HOME/.local/bin:$PATH uv run python cli.py reconcile-51job --database ~/.job-agent/jobs.sqlite3
```

读取社会申请的「全部」筛选，复用状态监听的 DOM 投影、登录/风控检查和分页校验。浏览器仅执行导航和读取，临时页在成功或失败时关闭；使用配置的投递空间，不解除平台暂停。SQLite 以 `mode=ro`、`query_only` 和单次事务快照读取，不创建数据库、不迁移、不回填投递、不修改队列。命令输出仅含岗位 ID、本地行 ID、申请日期、分类及状态，不含姓名、正文或完整 URL。

| 分类 | 含义 |
| --- | --- |
| `site_only` | 原站有、本地无对应申请证据。即使采集库已有该岗位，单纯采集或发送前失败也不算申请。 |
| `local_attempt_unverified` | 同日有自研发送尝试，完整附件回执未确认；不重投。未确认记录需有持久化发送边界，不能把发送前读页失败当成提交。 |
| `local_attachment_confirmed` | 同日存在 `kind=attempt`、`status=sent` 且附件名非空的自研回执。附件依据是本地回执，原站申请列表不显示附件。 |
| `legacy_record_only` | 仅找到导入的旧发送/不明记录，无法确认当次附件或准确时间。 |

原站只能证明「已申请」，不能证明招呼语已送达。此桌面投递流程不发送招呼语，汇总会明确说明。匹配以平台岗位 ID 和中国时区申请日期为依据；原站仅保留近 60 天，缺失不能证明从未申请。`site_only` 的来源仍未知，不能直接归因于平台智能投递，也不会自动导入成自研成功记录。

BTY-4 真实验收使用已有 v8.4 附件：原站文件下载后的 SHA-256 与用户指定 PDF 完全一致，因此跳过重复上传，保留全部旧附件。账号原站及数据库的具体岗位、附件名和本机配置备份信息只写本地 `result.md`。验收过程中发现一个候选岗位已下线，提交前替换；共享投递锁会阻止并发确认，不绕过该锁。

本次只提交了两次申请：原站申请数由 5 增至 7，但两条本地回执均为 `unverified/effect_error`，**首次完整附件投递验收未通过**。第二条已完成指定附件的选中态回读和发送点击，失败发生在等待附件成功事件阶段；第一条因旧提示没有阶段信息，不能追认其具体失败点。原站申请列表不显示附件，不能据此补成附件成功。

针对诊断缺口，适配器补充安全的阶段和异常类型，并保留风控/登录/空间错误码，让引擎正确停止或暂停。原站组件在附件发送失败时也会发出 `sendStatus=0` 事件；原代码只等成功标记会把明确失败显示为超时，现在等待任一附件事件，再严格核对 `sendStatus=1`、附件 ID 与申请记录 ID。**没有证据证明本次超时就是 `sendStatus=0`，修正后也没有追加真实申请复验。** 已用完两条授权额度，不重投、不清除不明记录；本项留待 Claude 审查和后续明确授权的验收。

---

## P0 原站取证（recon-b，2026-11-24）

结论：目标附件存在，但当前网页的附件选择器在**初始投递成功之后**才初始化，依赖成功结果里的 `cvLogId`。没有找到可在初次申请提交前选中附件并读回的正常网页入口。已有非空 HR 消息提醒，但桌面端本次只读到提醒元数据，未拿到聊天正文列表。P0/P2 仍有明确缺口，不能宣称完整取证或投递已修复。

## 范围与证据

使用现有 Ego Lite 的 `job-agent 取证 B` 独立空间。没有点击「立即投递 / 立即沟通 / 发送」，没有扫码、登录、上传或更改简历。未操作其他任务空间。

夹具在 [`mcp/tests/fixtures/platforms/job51/`](../../mcp/tests/fixtures/platforms/job51/)。JSON 是原站 DOM、Vue 状态和方法只读检查的脱敏投影，**不是实际投递 HTTP 回包**。姓名、公司、ID、附件姓名替换；class、字段类型和状态保留。隐藏弹窗明确标记为未打开，未伪造成功响应或选择项。

| 问题 | 证据 | 本次结果 |
| --- | --- | --- |
| 登录/已申请 | [application-list.json](../../mcp/tests/fixtures/platforms/job51/application-list.json)、[applied-job.json](../../mcp/tests/fixtures/platforms/job51/applied-job.json) | 登录有效；前后 1 条申请 |
| 目标附件 | [attachment-inventory.json](../../mcp/tests/fixtures/platforms/job51/attachment-inventory.json) | 存在；不等于投递选中 |
| 选择时序 | [attachment-flow.json](../../mcp/tests/fixtures/platforms/job51/attachment-flow.json) | 原站方法显示先申请成功再选附件 |
| 弹窗结构 | [attachment-dialog-hidden.html](../../mcp/tests/fixtures/platforms/job51/attachment-dialog-hidden.html) | 已挂载但隐藏的空壳，不是实际打开 |
| 非空消息提醒 | [hr-notification.json](../../mcp/tests/fixtures/platforms/job51/hr-notification.json) | 有提醒，无正文/消息ID/时间 |
| 招呼语/聊天 | [desktop-chat-availability.json](../../mcp/tests/fixtures/platforms/job51/desktop-chat-availability.json) | 桌面页未找到设置/编辑器 |
| 风控 | [risk-observation.json](../../mcp/tests/fixtures/platforms/job51/risk-observation.json) | 未遇验证码/限频；没有测阈值 |

原始截图、HTML、运行时方法检查原件及相应哈希，仅留本地任务目录 `evidence-private/`，不提交账号信息或完整第三方前端源码。外部实现仅作只读参考，本仓库没有其实现代码。

## 招呼语/首条消息上限

本次查看首页、个人中心、简历中心、简历设置、岗位详情和申请记录。没有可安全打开的桌面聊天编辑器，也没有招呼语设置。岗位「立即沟通」及招聘官区域引导微信扫码，本次未触碰可能产生自动招呼的入口。

**实测上限未取得，应为 unknown/null，不能填 0、300 或从猎聘推导。** 这不代表平台没有聊天功能；只是当前受限的桌面取证没有可用编辑器。要得到真实上限，还需在已授权、已登录且不会自动首聊的聊天界面继续测。证据：[desktop-chat-availability.json](../../mcp/tests/fixtures/platforms/job51/desktop-chat-availability.json)。

## 投递流程与真正的对外动作

当前岗位详情的控件为 `div.apply-btn-new.big`。未投时文案「立即投递」，已有申请加载登录状态后为 `div.apply-btn-new.big.disabled`「已投递」。**初始渲染一度显示「立即投递」再变「已投递」**，必须等账号及岗位状态就绪，不能见按钮就点。证据：[applied-job.json](../../mcp/tests/fixtures/platforms/job51/applied-job.json)。

原站当前加载的 `PCApplyJob` Vue 方法只读检查得到如下顺序（没有执行这些方法）：

1. 申请拦截检查：`GET /open/user/apply-chat/check`。
2. 初始投递：`POST /open/user-apply/open/user-apply/light-apply-job`。参数含岗位列表、可选在线 `resumeId`、`multiAttachmentResumeSupported=true`，**没有本次所选 `attachResumeId`**。
3. 成功 `status == 1` 后，读取 `resultbody.applyJobResult.applySuccessJobList[].cvLogId`。
4. 若 `resultbody.popupAttachResumeList.attachResumeList` 非空，初始化附件选择器，传入上述 `cvLogIdList` 和附件列表。
5. 用户选附件，点击「发送」，再请求 `POST /open/user-apply/open/user-apply/modify-attach-resume`。
6. 子组件回调后，父组件显示成功/招聘官二维码，并发出 `applyCallback`。

证据：[attachment-flow.json](../../mcp/tests/fixtures/platforms/job51/attachment-flow.json)。可重新核对原站已加载的 [51job 岗位页组件包](https://fccdn.51jobcdn.com/51job-jobs/static/js/6.1793270085109.js)。这是**运行时方法静态检查**，并未捕获新申请的请求响应。

因此，初始 `light-apply-job` 请求已经是申请动作；不能点击「立即投递」仅为查看附件选择器。在线简历选择/完整性拦截可能在部分账号条件下出现，但不能假设每次都有提交前弹窗。当前账号只读检查未触发这些分支。

## 重点：附件选择和读回

### 本次已确认

`https://www.51job.com/resume/center` 的「附件管理 → 附件简历」有唯一目标 `示例候选人_简历`，附件数量 `1/3`。文件更多菜单只有「重命名 / 同步更新在线简历 / 删除」，没有提交前选择或默认投递开关。「简历设置」只见简历名称和公开状态。都没有保存更改。证据：[attachment-inventory.json](../../mcp/tests/fixtures/platforms/job51/attachment-inventory.json)、[desktop-chat-availability.json](../../mcp/tests/fixtures/platforms/job51/desktop-chat-availability.json)。

岗位页在提交前已挂载一个 `display:none` 的 `.attachment_resume_dialog`：标题「选择需要同步发送的附件简历」，底部按钮「发送」，没有选项行；Vue 状态 `isVisible=false`、`attachmentResumeList=[]`、`selectedAttachmentResumeId=""`、`cvLogIdList=[]`。**空壳不能通过强制显示来冒充正常选择器**。证据：[attachment-dialog-hidden.html](../../mcp/tests/fixtures/platforms/job51/attachment-dialog-hidden.html)、[attachment-flow.json](../../mcp/tests/fixtures/platforms/job51/attachment-flow.json)。

### 真正弹出后应核对的字段（尚未实点验证）

原站 `onSelect(row)` 把 `row.attachResumeId` 存入 `selectedAttachmentResumeId`，并清空 `defaultApplyResumeId`。确认方法发送：

- `cvLogIdList`：已成功申请的记录 ID。
- `attachResumeId`：`selectedAttachmentResumeId`。
- `setDefaultFlag`：所选 ID 是否等于 `defaultApplyResumeId`。
- `modifyType`、`attachmentScene`。

「设为默认」与本次选择是不同状态，不能看到默认标签就当本次已选中。后续实现应先确认所选 ID 在 `attachmentResumeList` 中恰好命中一行，再核对该行的真实附件名和 DOM 选中态；名字缺失、多个候选或不匹配时停止。**本次没有真实选项列表，文件名字段名、选中 class 和具体行结构仍待抓取，不能猜 `resumeName`。** 证据：[attachment-flow.json](../../mcp/tests/fixtures/platforms/job51/attachment-flow.json)。

确认方法即使附件发送失败，仍会触发父回调；父组件再展示申请成功。所以不能以成功面板证明附件已发。必须分别保留初次申请结果、附件结果和原站回读。

### 对 `attachment_selection_unverified` 的判断

本次证据解释了前置验证为何会失败：初始申请前，正常页面没有本次选项和选中 ID；它们等申请成功后才填入。**没有证明今日 7 次失败的每一次运行细节**，也没有验证其他端或历史默认附件机制。

当前首选是保持自动申请停用，把「附件只能在成功申请后补选」作为明确平台限制交给 #15 的实施者。若仍要求「附件名必须在初次提交前可验证」，这条已观测路径暂不满足。后续只能在用户指定、明确授权的一条真实岗位上验证实际弹窗，且一旦初始申请已成功、附件结果不明，须记 `unverified`，不能重投。不要把附件选择器的「发送」当成第一次申请的安全确认按钮。

## 已投记录与反馈列表

`https://i.51job.com/userset/my_apply.php?lang=c` 提示「仅保留近60天的社会申请记录」。本次非空列表有 1 条：岗位、公司、城市、申请日期和「申请简历：我的简历1」。它是在线简历标签，不能证明附件版本。前后申请数都是 1。证据：[application-list.json](../../mcp/tests/fixtures/platforms/job51/application-list.json)。

| 信息 | DOM/字段 | 注意 |
| --- | --- | --- |
| 列表项 | `.apox > .e` | 非空申请记录，不是聊天记录 |
| 岗位 | `.li.l1 a.zhn.gt` 的 href/title 文本 | href 的 `/数字.html` 可取岗位 ID |
| 公司 | 同一 `.e` 内公司链接 `/co数字.html` 和文本 | 不拿其他推荐职位的公司混入 |
| 申请时间 | 同一行「申请于」后的日期 | 日期不等于 HR 回复时间 |
| 简历 | 「申请简历：我的简历1」 | 未显示附件名 |
| 反馈筛选 | 全部 / 已查阅 / 感兴趣 / 邀面试 | 状态与人发聊天正文分开 |
| 消息正文/方向/消息ID | 本次申请列表无这些字段 | 不可制造回复事件 |

原站保留期只有近 60 天；缺少更早记录不能当作从未投递，应结合引擎持久化历史防重。

## 已有 HR 消息：实际拿到了什么

个人中心 `https://we.51job.com/pc/my/myjob` 的 Vuex getter `commonStore/userInfo` 读到非空提醒：`news="1"`，同时有 `hrName`、`coName`、`jobid`、`hruid`、`ctmid` 和 `qrCodeUrl`。提醒对应岗位**不同于唯一已申请岗位**。证据：[hr-notification.json](../../mcp/tests/fixtures/platforms/job51/hr-notification.json)。

该对象没有消息正文、消息时间、消息 ID 或方向。页面可见的是「扫码关注服务号，打开微信通知」。公开前端提醒逻辑生成 `we.51job.com/wxbridge?page=chat` 的微信桥接二维码，包含 HR/公司/岗位信息；不是桌面聊天列表。取证时提醒组件本身未实际渲染出来，故夹具明确写 `renderedReminderObserved=false`，不把状态对象冒充可见弹窗。证据：[desktop-chat-availability.json](../../mcp/tests/fixtures/platforms/job51/desktop-chat-availability.json)；逻辑可对照原站 [个人中心组件包](https://fccdn.51jobcdn.com/we/static/js/app-792f8fa3-1747d902.js)。

**非空聊天回复列表仍未取到。** 不能以「申请列表非空」或 `news=1` 满足 P2 的正文监听验收；也不能返回「无回复」掩盖此缺口。本轮没有扫码、没有登录其他端、没有打开可能自动招呼的入口。下一步需提供已登录且可只读访问的实际聊天界面，继续抓消息字段和已投岗位映射。

## 默认招呼语与风控

本次检查的桌面简历设置、附件菜单、个人菜单均未见默认招呼语设置，与「51job 没有设置」线索一致；仅能确认已观察的界面，不能推断微信/App 全部设置。证据：[desktop-chat-availability.json](../../mcp/tests/fixtures/platforms/job51/desktop-chat-availability.json)。

未遇验证码、异常登录或限频提示。个人中心有「邮箱尚未验证」，未处理。未做发送和压力测试，因此每日限额/间隔/风控阈值均未知。证据：[risk-observation.json](../../mcp/tests/fixtures/platforms/job51/risk-observation.json)。

## 交给 P1/P2 的明确结论

- 可用：申请列表非空结构、加载后已投状态、目标附件库存、附件选择器的提交后时序和运行时读回字段。
- 必须继续保守：提交前附件选择未找到；实际有行弹窗未抓；首条消息上限与完整回复列表未验证。
- 本次仅交付文档和夹具；未改投递引擎、数据库、导入或其他平台。

## 采集

2026-11-24，collect-b 通过项目 `EgoBrowser` 在「job-agent 采集 B」只读搜索 `AI应用开发 / 上海`。入口 `https://we.51job.com/pc/search?jobArea=020000&keyword=AI应用开发`；实测第一页、第二页和一条详情，含 URL 页码校正共 3 次列表加载，详情 1 次。未点击沟通、投递、收藏，未遇验证码；临时页关闭，锚点保留，不 finish。

- 列表 DOM：`.joblist-item`；页面 `PCSearch` Vue 组件已加载的 `joblist` 提供 `jobId/jobHref/jobName/fullCompanyName/jobAreaString/provideSalaryString/workYearString/degreeString`。只投影这八个字段，不导出 HR、用户信息或整个组件；没有另调搜索 API。
- **URL 的 pageNum 被页面忽略。** 本次读到的初始化函数将页码固定为 1。后续页走页面原有只读分页处理 `PCSearch.getJobList(n)`，等待 `isLoading=false` 并核对 `searchParams.pageNum`。该处理与网页翻页同一路径，不调用投递方法。
- 每次调用都关闭临时页，因此第 n>1 页需要先加载第一页，再翻到目标页，两次列表访问之间随机等 8–15 秒（构造参数 `interval_seconds` 可调，最低 8 秒）。**调度层统计浏览器访问预算时须计入这次额外第一页访问**；本 PR 不修改 N 的预算实现。超过 `ceil(total/20)` 时返回明确空结果，不触发额外翻页。末页逻辑离线验证，未真机访问末页。
- 详情：`.job_msg > div` 读 JD 正文；从现有 `ApplyjobBtn.detailData.jobId` 只读取 ID，与 listing 匹配。账号状态仅输出布尔值，等待异步登录态就绪。
- `isEmpty=true` 且已加载、页码匹配才返回 `[]`。未就绪、页码不符、缺少稳定 ID/JD 抛 `BrowserError`；登录失效抛 `login_required`，可见验证码/风控抛 `risk_control`，不绕过或重试。

[search-list.json](../../mcp/tests/fixtures/platforms/job51/search-list.json)、[search-detail.json](../../mcp/tests/fixtures/platforms/job51/search-detail.json)、[search-page2.json](../../mcp/tests/fixtures/platforms/job51/search-page2.json) 是脱敏 DOM/运行时投影，不是原始 API 响应。列表各保留两条，ID、公司和标题替换，详情保留全文 JD，不带姓名、cookie、追踪参数。第一页来自探索读取，第二页和详情使用生产表达式。

城市映射在 `pages.py`：上海已实测，苏州、杭州、南京参照配置，其他城市暂不猜编码。未知城市抛 `ValueError`。薪资下限不拼平台筛选条件，由调度层统一做硬过滤；本源不打分、不写数据库。

### O 集成补充（2026-11-24）

苏州「AI应用开发」第一页混有 `mahle.51job.com/jobdetail.html` 企业专页；其详情结构不属于当前适配器。采集跳过这个已识别的专页，汇总到 `skipped_rows`，普通岗位继续入库；全页只有不支持的专页仍报错，不能当作搜索结束。其他未知域名仍停止，未扩大域名信任范围。此处是暂时的覆盖限制，企业专页详情待另行实现。

每日列表预算在搜索前预扣：第一页 1 次，后续页 2 次（含重新加载第一页）。失败不退预算。苏州冒烟累计 3 次第一页访问，最终一次成功取得 19 条普通岗位、跳过 1 条企业专页；不访问第 2 页。
