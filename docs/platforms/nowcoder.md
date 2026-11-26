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
