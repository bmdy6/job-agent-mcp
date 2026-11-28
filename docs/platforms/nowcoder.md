# 牛客接入侦察（2026-11-26，Ego Lite）

范围：只读访问首页、校招职位、详情、消息中心和插件介绍；未申请、未发消息。

- 登录：导航有「消息」链接 `/users/notice/privateChat` 与「创作中心」，不是仅凭没有登录弹窗判断。
- 搜索：`/jobs/school/jobs`，`input[placeholder="请输入公司名或职位名搜索"]` 输入后 Enter。`.school-job-box.__vue__` 有 `isLoading/list/page/totalPage/searchValue/city`。实际 URL 出现 `careerJob=11006&city=苏州`；不能依赖共享缓存里的筛选。
- 岗位卡：`a.job-message-boxs[href*="/jobs/detail/"]`；标题 `.job-name`、薪资 `.job-salary`、标签 `.job-info-item`，同父级 `.company-name`。稳定 ID 为详情路径数字。Vue list 的 `jobExpInfo.recReason` 可为「空结果推荐」：输入 AI应用、城市苏州仍返回南京 2027届岗位，必须本地严格过滤。
- 详情：`/jobs/detail/475028` 可读公司、11-15K * 14薪、苏州、本科、岗位职责/要求、HR「14分钟前在线」、反馈率及反馈时长。标题和列表工资都不能替代毕业届数核对。普通按钮「立即申请」，另有外部官网岗位类型；不是所有入口都能统一点击。
- 投递：只读读取按钮组件方法可见 nowpickJobApply → showNowpickDeliverPopup；另有 officialJobApply 分支。具体附件选择、招呼语、确认按钮及原站回执尚未实测，不能宣称投递成功。
- 消息：`/users/notice/privateChat` 是社区私信，不等于招聘会话；「求职沟通」跳转 `/users/notice/jobAssistant`。本账户显示「暂无会话列表 / 暂无会话，快去找HR聊聊吧」。稳定会话/消息 ID、未读字段、岗位关联暂未取得，不伪造事件。社区资源出现 sns/message/.../get-conversation-list，不可误用于招聘监听。
- 风控：本次未出现验证码或频率提示，未探测阈值。每日投递/沟通上限未知；建议本项目初始每日最多 2 次、间隔 120–240 秒、小时最多 2 次；这些是项目预算，非官方额度。出现验证码、安全验证、操作频繁或账号异常立即停止，不自动恢复。

## 网申助手 / 一键海投

首页入口指向 [介绍页](https://www.nowcoder.com/my/resume-plugin-intro?channel=pc_jgw)。页面描述 Chrome/Edge 插件、AI 匹配招聘官网字段、预填一次简历、支持批量投递；另有 Agent 求职助手和 Skills 标签。以上是页面说明，未安装或验证其权限、准确率、批量发送和逐站回执。

本次不使用批量投递：它跨多个招聘官网，无法在现有单岗位队列内保证预算、附件、字段和原站回执逐条核验。可把人工检查后的单站表单辅助填充作为另行评估方向，不能直接认定安全。job-agent 先接单岗位只读采集。

## 采集/监听实现边界

采集保存 graduation_year/experience 原文，不把当前候选人的年份硬编码进公用 JobSource；真实投递选岗必须另行核对 2026 届资格。首次只实现校招第一页，第二页明确报未核验，不重复第一页或假装全量采集。

进一步只读查看官网加载的 notice/main.entry.js（1.0.501）发现 JobAssistant.mounted 会自动 selectUser(首个会话)，因此监听不能直接导航该页。相同源码的只读 POST `/np-api/u/msg/get-conversations` 参数 page=1&pageSize=15 已在岗位页实测，返回 HTTP 200/code 0/data.datas=[]/totalCount=0/totalPage=0。replies.py 只支持这个已验证空列表探针；有会话时明确失败，不打开任何会话、不错误 acknowledge。完整的变化会话监听仍未实现，不可部署启用。源码有 latestMsg/createTime/unreadCount 字段，但尚无真实招聘会话可验证 ID、方向与岗位关联。

## 第二阶段侦察与实现（2026-11-26）

本节覆盖上文“仅空列表探针 / 投递未实现”的代码状态。**仍未完成真实投递与非空会话验收，默认关闭。**

### 申请入口

在专属 Ego Lite 空间「job-agent 新平台」打开岗位 475028，读取官方 jobDetail bundle 3.2.699 确认 `立即申请` 仅调用简历弹窗后，实际点击打开；没有点「投递简历」、没有选志愿、没有上传文件或发消息。弹窗有「完善在线简历 / 上传附件简历」，并出现「该职位为专场招聘职位，请选择志愿」（第一至第三志愿）。不能当作统一的一键投递。

账户只读 `/np-api/u/resume/job-resume` 返回：在线简历 `resumeDone:false`、`completeness:0`，没有附件。实现遇到该条件会在申请入口点击前抛 NotStarted，由队列记 skipped/not_started。额外问卷、志愿、协议、未知弹窗、附件配置均拒绝自动提交。当前仅实现已完善的唯一默认简历、无额外表单的可见单岗位申请分支；成功分支仍只经过合成测试。未实现上传、附件切换、填写问卷或官网闪投。

475028 的毕业年份字段虽然是「不限」，却关联 2027 届招聘活动；不能据此认定适合 2026 届。该岗位只用于只读侦察，未加入投递队列。线上库当前没有牛客岗位，未伪造候选补足投递数。

### 原站回执与额度

通过头像菜单的「投递记录」进入用户自己的 `/users/<uid>/deliver`；「牛客投递」显示暂无记录。「官网投递」是另一标签，不能混用。页面官方 users bundle 1.0.501 使用 GET `/v2/resume/deliver-list?page=1&stage=0`，实测 `deliverData:[]`、`pageInfo.totalCount:0`。

源码里的记录字段包括 `deliverId`、`jobId`、`resumeUUID`、`transferList`；卡片显示的 `latestUpdateTime` 是进度更新时间，不能充当新申请时间。适配器仅在弹窗有 serverAcknowledged / deliverySucceeded、成功简历 ID 匹配，并在原站记录中找到相同岗位、相同 deliverId、提交时间附近的「投递成功」转移时间时返回成功。没有记录或字段不合则 unverified，不重复点击。上述非空字段仍待真实回执验证，源码字段并不等于真实验收。

官方每日投递上限未查明。`get/user-resume-count-restrict` 返回的 `cnMaxNum:2/enMaxNum:2` 是中英文简历数量限制，**不是每日投递额度**；岗位 `allowDeliverCount:3` 也不能解释为全站每日额度。项目继续使用每日最多 2 条、小时最多 2 次、120–240 秒间隔，未探测风控阈值。

### 招聘监听

官方 notice bundle 1.0.501：摘要 POST `/np-api/u/msg/get-conversations`，变化会话消息 POST `/np-api/u/msg/common/get-message-list`（conversationId/pageSize=20/preId=0）。消息的 sender 与当前用户 ID 比较判定方向；不能把所有招聘消息都当 HR 回复。

实现先收集完整分页摘要（至多 20 页，每页 15 条，数量不一致/重复 ID/列表变化报错），再调用现有 ConversationCursor，仅对 select 返回 true 的会话读取最新 20 条消息。不导航 jobAssistant，不调用 read-conversation，不点联系人，不发消息。首次已读基线与无变化会话不读取消息；首次未读沿用现有项目规则读取。消息会话/最新 ID/发送方/时间/正文类型无法确认就报错，不 acknowledge；持久化游标由监听框架成功入库后提交。

真实运行新的 ReplySource 仍返回空列表；非空会话及消息类型仅有合成离线测试，正文暂只接受字符串，未知富媒体结构保留错误，不伪造事件。尚不能宣称已完成非空招聘监听真实验收。

### 后续上线配置（Claude 审查与验收后操作）

- 采集：`collect.platforms.nowcoder.enabled:true`，`daily_page_limit:2`，rate.interval_seconds `[120,240]` / hourly_limit `2`；关键词、城市和 `collect.filters.salary_min_k` 另按用户筛选，第一页之外仍未验收。
- 投递：`engine.dry_run:false`、`live_send_enabled:true`；`engine.platforms.nowcoder.enabled:true`、daily_limit `2`、上述 rate；`greeting_mode:none`、`greeting:""`、`attachment_name:""`。现有通用配置要求 enabled 时 greeting_max_length 为正，可设 `1` 通过校验，它不表示支持发送 1 字招呼语，适配器拒绝所有非空招呼语。先在原站完善唯一默认简历，并补齐真实回执验收；附件选择未验收前不可配置附件名称。
- 监听：在 `engine.monitor.platforms` 原列表追加 `nowcoder`；保持监听独立空间。monitor 日额度不沿用投递 daily_limit，小时预算仍是 2 次轮询；共享 pacing 间隔仍生效，默认 5 分钟调度不意味着每 5 分钟都能访问牛客。
- 临时 JOB_AGENT_HOME 验收时，`nowcoder.automation.lock` 必须与生产共用同一锁文件（临时目录可放指向生产锁文件的软链），避免临时目录隔离掉跨进程互斥。数据库显式指向生产 jobs.sqlite3，不复制/重置投递历史。

本轮没有修改生产 config.yaml / daily.yaml，没有停止服务。未出现验证码或账号异常；发生时仍立即停止，不绕过或自动解锁。

## 第二轮修正（2026-11-27）

默认简历预检仅要求唯一的 `attachment:0 / defaultResume:true / resumeDone:true` 在线简历。附件不需要设为默认，附件上的默认标记也不参与在线简历唯一性判断；非空 `attachment_name` 仍不支持。

申请弹窗回读实际选中简历的 ID、UUID、名称和在线/附件类型，与预期在线简历比较。通过引擎绑定的记录器，在提交或拒绝之前将两份身份与 `matches_expected` 写进 append-only `deliveries.error_detail` 的 `resume_selection` JSON；后续成功、不明、异常记录继续保留证据，`attachment_name` 反映实际附件名称（选中在线简历则为空）。不新增数据库 schema，不记录附件下载地址。记录器落盘失败不提交。

实际选择不一致或无法确认时保留 `unverified`，不点击提交、不切换简历、不重投；问卷、志愿等原有限制继续生效。离线测试覆盖默认在线简历与非默认附件并存、弹窗不匹配、落盘失败、提交超时、成功记录以及重开数据库后的禁重投。真实投递结果另见 PR #82 的第二轮验收记录。

真实 CLI 首轮还暴露了通用 HR 信息补充仅覆盖原四个平台的问题：牛客在副作用前因 `HR_JS['nowcoder']` 抛错。该可选补充现在对未取证的平台不读取 HR 字段，沿用现有公司/岗位去重且不覆盖已有身份；新增离线测试直接覆盖 live-only 补充路径和完整的牛客提交分支。

入口与提交按钮使用原生 CSS：自有输入驱动在计算坐标时会调用 `document.querySelectorAll`，不能透传 Ego Lite 高层 API 的 `:has-text(...)` 语法。已在真实岗位只读复用驱动的原样 geometry callback，确认阅读滚动后入口和弹窗提交按钮均可唯一定位；此检查只打开弹窗，没有点击提交。回归中的 live 分支也拒绝非原生文本选择器，避免 FakePage 掩盖这一契约错误。

成功页会用 `DeliverySuccess` 替换 `.delivery-resume-popup` 表单节点，回执投影因此按可见 Dialog 的 `DeliveryResumePopup` 组件归属取状态，不再依赖已被移除的表单节点。合成 DOM 回归直接执行这份 JavaScript 投影，覆盖表单消失后的服务端确认、岗位 ID 和成功简历 ID；仍须原站投递记录满足岗位、deliverId 与新鲜成功转移时间才算成功。
