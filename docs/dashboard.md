# 本地数据看板

`job-agent dashboard [--out PATH.html]` 与 MCP `dashboard(out=None)` 共用
`service.dashboard.generate`。默认写入 `$JOB_AGENT_HOME/dashboard.html`，
返回绝对路径。HTML 是本地离线快照，不会自动刷新、上传或发布。

## 口径和数据路径

- `ReadStore` 以 SQLite URI `mode=ro` 和一个 `BEGIN` 事务读取，不调用 `Store.__init__`，不迁移数据。
- 累计数字 / 平台漏斗复用 `read_tools.funnel_from_store`；公司数使用其 `funnel_population` 的 applied 岗位和现有 `company_key` 规则。
- 招呼语效果直接显示同一次 funnel 的 `greeting_stats`，保留样本门槛、Wilson 区间和归因边界。
- 趋势对同一人口按首次已知阶段时间去重，补齐配置时区的连续 14 天。投递回执、真实历史和显式事件提供日期；旧 `legacy_snapshot.updated_at` 不当作投递日期。日期缺失单独报告。
- 今日额度复用投递限速器的 `operation_times` / `daily_usage`；从 `DailyQueue.attempt_times` 获取旧尝试，按平台 `rate_policy()` 的时区与限额统计预留，失败尝试也可能占额度。
- 平台诊断读取 monitor-runtime / monitor-state 和 durable pauses；心跳只调用既有 `heartbeat.problems`，不会调用发告警的 `run_once`。
- 最近投递取每个岗位最新 delivery 状态；仅显式事件的岗位标为已记录投递。BOSS sent 显示沟通回执。最近回复仅展示 inbound，排除 source_history 合成状态，保留已有分类。

## 文件和隐私

只生成单个内联 CSS / SVG 的 HTML，使用系统字体，无 JS、CDN 或远程资源。
CSP 禁止资源请求；HTTP(S) 岗位与聊天链接须由用户主动点击。
展示文字和链接均转义，不允许 javascript / data / 带凭据的链接。
回复在数据投影时即截为前 40 字；不嵌入完整数据库、正文、JD 或配置。

HTML 通过同目录临时文件以 0600 权限原子替换，拒绝覆盖输入文件、指向输入的软链接 / 硬链接。
CLI 不运行写日志初始化；MCP 保留原有仅含类型和计数的调度审计，不记正文、姓名或完整 URL。
生产数据预览使用独立配置目录和从生产 `mode=ro` 连接 backup 得到的只读副本，产物留在仓库外。

## 本地验收（2026-11-26）

- 临时数据库覆盖数字、同岗位多次投递 / 回复去重、跨日边界、旧快照无日期、历史邀约后回执不明、本地事件、额度预留、持久风控和监听未登录、心跳过期、空库、缺失库、schema 不匹配与路径碰撞。
- HTML 检查无外部资源、仅岗位 / 聊天链接、无完整回复尾部、转义、私有文件权限和指标区闭合。
- CLI 与实际 MCP stdio 均生成文件；新增工具同步更新工具全集契约。
- 全量 611 项测试在隔离 JOB_AGENT_HOME 下运行，610 项通过、1 项跳过；真实部署演练按原套件默认 opt-in 跳过。本任务不运行部署。
- `uv build` 成功，wheel 包含 dashboard.py 与 service/dashboard.py；新建 venv 安装 wheel 后，从仓库外运行安装版 CLI 成功生成空库 HTML。
- Ego Lite 打开真实只读副本生成的 HTML，检查桌面 / 手机截图和最近回复表；320 / 768 / 1024 / 1440px 均无页面横向溢出。键盘 Enter 可展开 / 收起每日精确表，56 行；资源请求数为 0。
- 首轮测试未设置外层临时 JOB_AGENT_HOME 时，3 项平台安全测试碰到生产智联锁而失败；隔离目录后原 14 项平台安全测试及全量测试均通过，未改锁或停止生产服务。

本次额外尝试只读 Claude CLI 审查，因上游 `401 Invalid token` 未完成，不视为审查通过；没有修改认证配置或重试发送生产数据。保持 draft PR，交由 Claude 后续审查、合并、部署。
