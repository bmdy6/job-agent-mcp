# 51job 申请按钮定位诊断（2026-11-27）

修复阅读 JD 后吸顶栏按钮落在窄视口外、`click.target_geometry` 返回 null 的情况。申请前先回到正文页首，唯一选择非吸顶按钮，等待布局稳定并检查遮挡；不再用 `nth=0` 任取第一个匹配。

## 页面证据

只读查询本地 deliveries：2026-11-27 有 12 条 51job `skipped/not_started`，错误均为 `Ego click.target_geometry failed: browser_error`。仅在新 Ego Lite 空间「job-agent 51job诊断」（id 6）打开其中三个岗位；未进入每日投递空间。

| 岗位 | 原选择器匹配数 | 稳定登录态 | 已申请 | 可见弹窗 |
| --- | --- | --- | --- | --- |
| [172295763](https://jobs.51job.com/kunshan/172295763.html) | 1 | true | false | 0 |
| [173985523](https://jobs.51job.com/suzhou-gxq/173985523.html) | 1 | true | false | 0 |
| [174214817](https://jobs.51job.com/taicang/174214817.html) | 1 | true | false | 0 |

- `.apply-btn-new.big` 仍有效；本次没有观察到两个按钮。页面复用同一个 `.tHeader.tHjob`，滚动后加 `.fix` 并变成 `position:fixed`。
- 页首布局宽约 1200px，按钮原始 x=988、宽 203px。滚动 600px 后，吸顶按钮 x=980。默认诊断视口为 1057×874，按钮部分被裁；仅对诊断标签页临时设为 900×874 后，按钮完全在右侧视口外。
- 使用当前 `mcp/job_agent/browser/ego.py` 原样提取的 geometry callback（不执行 click）测试三个岗位：吸顶时均返回 `null`，`scrollIntoView` 无法横向滚动 fixed 元素。
- 执行此次真实 `PREPARE_APPLY_JS` 后再运行同一 geometry callback，三个岗位均得到 x=682、y=107、width=203、height=48；按钮完整进入视口。
- 登录字段会异步更新，首帧可能为 false；上表通过现有 `JOB_JS` 稳定等待后读取。原登录/岗位/已投状态护栏保留。
- 未保存每日任务失败瞬间的 viewport/scroll 状态，故上述是可复现的故障机制，不能断言 12 次失败都具有完全相同的历史布局。

原始截图与几何 JSON 已随运维清理转存任务归档，仓库不再跟踪根目录 shots/。上述数值保留了复现结果，原文件仍可从 Git 历史查看。

## 改动与安全边界

上游 PR #89 已将模块迁至 `mcp/job_agent/`，本分支同步该重组并保留新命名空间；适配器实际路径为 `mcp/job_agent/platforms/job51/`。

- `APPLY` 限定 `.tHeader:not(.fix) .apply-btn-new.big`。先回页首，最多等待 3 秒；只接受唯一、可见、启用、文字为「立即投递」且未被覆盖的正文按钮。若未来出现吸顶副本，排除副本；多个正文候选仍拒绝。
- 点击前再次检查岗位已投状态；仅在明确的 resolve/geometry、鼠标尚未按下失败后允许一次重新准备并重试。准备失败/两次定位失败均为 `not_started`；鼠标阶段等不明失败保留 attempted，禁止自动重放。
- 风控、登录、任务空间等安全错误保留原错误码，不因发生于 geometry 阶段而重试。
- 无弹窗关闭逻辑：三页未观察到需要关闭的弹窗；遮挡检查失败则不点击。未改通用 Ego 驱动、数据库、配置、附件选择与附件事件回执校验。

## 验证与待审查

- 先补回归：旧实现下 7 项点击测试出现 6 处失败；修复后通过。
- 全量 unittest：602 项，601 通过、1 项原有 opt-in 部署演练跳过；独立临时 JOB_AGENT_HOME。
- 新增离线布局夹具与 Node DOM double，执行真实准备脚本和真实 driver geometry callback，覆盖窄视口、吸顶副本、正文多匹配、缺失、隐藏、遮挡、禁用、已投与吸顶无法解除。
- 适配器测试覆盖重查已投状态、准备失败、有限定位重试、安全错误保留、鼠标按下后未知结果禁止重试；原附件回执测试通过。
- sdist/wheel 构建、compileall、git diff --check 通过。
- 只读真实页面 smoke 通过；没有运行 send_greeting/click，没有真实申请，也未验证点击后的附件弹窗流程。本次不合并、不部署、不改生产配置；Claude 审查待完成，未启动 Mac Claude CLI。
