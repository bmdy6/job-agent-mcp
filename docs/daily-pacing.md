# BTY-5 每日任务

`collect.platform_timeout_seconds: null`（默认）按各平台页预算 × 每页操作数 × 平均间隔，加预计休息和余量计算。`operations_per_page` 默认 2，开启 JD 时再加 `details_per_page`（默认 30），`timeout_margin_seconds` 默认 120。平台内分页、实际列表长度会改变实际操作数，预算是估算；仍保留硬超时。显式数字保留手动超时覆盖。现有配置里的 600 也属于显式覆盖，切换自动计算需改为 null。

每日 JSON `summary` 与通知包含采集状态、页面数、可投候选（符合本地资格和活跃度、冷却期过滤后，额度和本轮上限裁剪前）、冷却期排除、实际成功沟通回执数和零投递原因。BOSS 沟通成功不等于简历送达。

采集、投递、监听使用 `$JOB_AGENT_HOME/<platform>.automation.lock`，非阻塞 flock，繁忙时不访问平台。锁文件不能删除，进程退出自动释放。采集与投递各在本轮持有已取得的平台锁，监听按平台读取周期持有。

外部简历同步必须通过同一入口运行整段平台操作（包含读取、上传、保存与回读），例如：

```sh
python3 scripts/platform-lock.py boss -- ego-browser nodejs /absolute/path/boss-resume-sync.mjs
```

繁忙退出码 75。所有工作进程须使用同一 JOB_AGENT_HOME。直接在外部调用浏览器的旧同步脚本不受 Python 锁约束，接入时必须使用该包装入口；本次不部署或改动其他正在运行的 Agent。

BOSS 点击后目标不匹配时只打开已有会话核验，不再点击立即沟通或发送。目标及当前会话均需有唯一身份、加载完整、两次读取没有任何发出消息才能 skipped/not_started；找不到目标、加载不完整、存在默认消息或已提交表单仍保留 unverified。

## 开机补跑与续跑

每日 LaunchAgent 保留配置的 08:00 日历触发，另设 `RunAtLoad=true` 和 `StartInterval=1800`。这是登录用户的 LaunchAgent：Mac 用户登录后启动，依赖 Ego Lite 登录态，不是系统登录前的守护进程。入口加 `--scheduled`，先取 `daily.lock`，检查 Mac 本地日期的 `daily-last-run.json`。`status=completed` 且 `started_at` 是今天时直接退出。其他状态（包括 running 且没有 finished_at）都可续跑；时段外持锁每 30 秒检查，等到下一个活跃时段。

活跃时段复用已启用投递或采集平台的 `rate.allowed_hours/timezone`，任一平台活跃即可开始，其余平台仍由自身限流器拦截。没有启用相关平台时用 Mac 本地 09:00–21:00。已完成的当日采集结果会复用，未完成的采集由已有 SQLite 游标恢复。已发送历史、未确认回执、尝试额度和平台暂停继续由 SQLite 保护。`NotStarted` 仅在持久事件证明未产生副作用时重新入队；任何真实不明回执仍阻断重试。瞬时采集异常或待续投递不会写成 completed；达到每日额度、功能未验收或持久风控暂停仍遵循原规则，不自动解除。

Ego 连接失败时只在解析命名空间的安全边界执行 `open -g -a "ego lite"`，以只读 CLI/CDP 探测等待就绪。默认等待 60 秒，`browser.startup_timeout_seconds` 可设 5–300。失败写 `ego-startup-failure.json` 并经既有 Telegram 通道告警，同一失败跨进程冷却 300 秒。不会自动重放点击、输入或导航，也不会接管用户控制的空间。

心跳使用成功轮询时间，失败轮询不会刷新它。每日任务使用首次开始时间、`last_progress_at` 及只读 SQLite 的投递/采集操作进度；单纯周期重启不会刷新业务进度。告警带环节、步骤、原因码和最后成功时间，仅发 Telegram sendMessage。配置（默认值）：

```yaml
observability:
  heartbeat_stale_polls: 3  # 监听连续超过三个配置轮询周期无成功
  heartbeat_daily_grace_minutes: 30  # 活跃时段内应启动但未启动
  heartbeat_daily_stale_minutes: 120 # 当天未完成且无进展
  heartbeat_cooldown_minutes: 120   # 同类问题持续存在时的重复提醒间隔
```

部署由 Claude 在合并后通过 `scripts/deploy.py` 执行；统一部署器也会重装上述 plist。旧 `install-daily.py` 安装入口已停用，触发器更新统一由 `scripts/deploy.py` 完成。部署期间 daily.lock 保护 schema 切换；如果 RunAtLoad 此时撞锁退出，后续 30 分钟触发会补跑。部署完成后的真实开机、断网恢复、Ego 启动和 TG 收件须单独验收，本地单测不做真实投递。

## 结果不明（unverified）

默认 `engine.stop_on_unverified: false`：结果不明的岗位直接跳过、永不自动重投，同平台其余岗位照常继续，不暂停、不告警（汇总里计入 unverified_skipped）。

设为 `true` 时使用以下保守策略：

- 某平台出现一条 unverified（结果不明），本轮只停该平台，该岗位永不自动重投。
- 本轮状态记为 deferred；冷却 `daily.unverified_cooldown_minutes`（默认 30 分钟）后，每 30 分钟的周期触发会续投该平台剩余岗位。
- 当天该平台累计 `daily.unverified_daily_stop`（默认 3）条不明，停到第二天。风控、登录、空间错误仍按原规则停止，不自动解除。
- 采集达到当日页数上限（collect `daily_limit`）与采集超时一样，不再阻止投递已入库的岗位。
- 51job 申请按钮在点击前定位失败（resolve / target_geometry）时重试一次第一个可见按钮；仍失败记 skipped/not_started，不算结果不明。


## 星期规则与生效顺序

引擎和各投递平台都接受 `day_limits` 与 `day_allowed_hours`。星期键只接受 `mon/tue/wed/thu/fri/sat/sun`；以 `engine.timezone` 判断今天是星期几。未设置的日期沿用原配置，`0` 上限表示当天不投。示例（合并到已有节点）：

```yaml
engine:
  weekend_daily_limit: 2 # 旧字段继续兼容，null 关闭旧规则
  day_limits: {mon: 10, sat: 2, sun: 2}
  day_allowed_hours: {sat: ['10:00', '16:00'], sun: ['14:00', '18:00']}
  platforms:
    boss:
      daily_limit: 20
      day_limits: {mon: 5, sat: 1}
      day_allowed_hours: {sat: ['11:00', '15:00']}
```

平台当天规则优先于全局同一天规则；最终上限为 `min(平台 daily_limit, 平台或全局当天 day_limits, 周六日的 weekend_daily_limit)`，未设置项不参与取小。显式 `rate.daily_limit` 仍可进一步收紧，不能抬高最终上限。旧 `weekend_daily_limit` 等价于额外的 sat/sun 上限，因此两种写法并存时仍取更严格者。上述 BOSS 周一上限是 5，周六是 1，周日是 2。尝试次数按 `engine.timezone` 的自然日统计，失败和结果不明的尝试也消耗额度；与成功回执数分开。

当天投递时段取平台 `day_allowed_hours`，缺省取全局同一天设置，再缺省取平台 `rate.allowed_hours`。支持跨午夜窗口，例如 `['21:00', '02:00']`，起止不能相同。窗口判断仍使用该平台 `rate.timezone`；星期选择使用 `engine.timezone`。规则参与 daily 候选筛选、实际投递限流和 scheduled 等待；不更改采集平台自身的 rate 配置。

## 通用 MCP 配置工具与 CLI

`get_settings(section=None)` 返回 `load_config()` 合并、校验后的完整用户配置及模型默认值。`section` 是可选点路径，例如 `engine`、`engine.platforms.boss`、`collect.filters`、`daily`；不认识或尚未配置的 section 会报错。无论筛选什么 section，都会附带今天的各平台上限与已用量。用量同时考虑两道限制：`attempts_used` 是引擎时区当天的持久化尝试数；`rate_used` 复用限流器的预留记录与历史去重口径，按平台 `rate.timezone` 统计，包括预留后中断的操作。`used=max(attempts_used, rate_used)`，`remaining=max(0, limit-used)`；采集预留不占投递额度。没有数据库时用量为 0；已有数据库无法安全读取时用量和剩余额度为 `null`，`usage_status` 为 `unavailable`。

```json
{"settings":{"daily_limit":20,"enabled":false,"day_limits":{"sat":1}},"today":{"date":"2026-11-28","timezone":"Asia/Shanghai","usage_status":"available","platforms":{"boss":{"enabled":false,"limit":1,"used":0,"remaining":1,"allowed_hours":["11:00","15:00"],"hours_timezone":"Asia/Shanghai"}}}}
```

上面省略了 `settings` 中其他平台参数和 `today.platforms` 中其他平台；真实返回包含选定 section 的全部字段，以及六个平台的当天状态。禁用平台仍展示配置上限，并标注 `enabled:false`，不代表可以投递。

`update_settings(changes, confirm=false)` 接受点路径到 JSON 值的对象。Pydantic 模型是字段白名单，范围、枚举、未知字段、跨字段约束均需通过；允许一次修改多个关联字段，例如同时配置合法招呼语长度与文本。适用于平台开关、daily_limit、rate、星期规则、公司/标题/薪资过滤、采集关键词/城市、招呼语文本、attachment_name、daily 开关及其他模型参数。非空嵌套对象会展开为叶子更新；空的普通映射可清空星期规则或城市代码。不能用整个模型替换或清空带凭据的父节点绕过限制；不支持删除字段或列表下标更新。

凭据和本地文件路径只返回 `{"configured": true/false}`：`*_env` 根据当前进程是否设置了对应环境变量判断；其他受保护字段表示是否有配置引用，不代表已经解析或验证 vault 中的凭据。不会返回环境变量名、vault 引用或秘密值，也不读取 vault 或修改进程环境。禁止修改 `key_source`、所有 `vault_*`、`*_env`、`*_path`、`*_token`、`*_secret`、`*_key`。这些由本地受信任配置流程管理。模型 API 地址和浏览器连接地址可按模型校验修改，仅接受无用户名、密码、查询串和片段的 HTTP/WS 地址；原有 URL 若含凭据，只显示是否配置，不返回地址。`attachment_name` 是平台上的附件名称，可正常修改。

调用示例：

```json
{"changes":{"engine.day_limits.sat":2,"engine.platforms.boss.daily_limit":10,"daily.deliver_enabled":false}}
```

预览返回结构（示例仅列一个改动）：

```json
{"applied":false,"changes":[{"path":"engine.day_limits.sat","before":null,"after":2,"file":"config.yaml"}],"backups":{}}
```

查看预览后，用相同 `changes` 并设置 `confirm:true` 执行。确认返回同样结构，`applied:true`，`backups` 包含文件名到时间戳备份绝对路径的映射，例如 `config.yaml.backup-20261128-100000-123456`。无实际差异时为 `applied:false, changes:[], backups:{}`。

预览不建锁、不备份、不写配置或数据库；MCP 调用本身仍遵循现有的 `activity.jsonl` 脱敏审计。确认时先取得 daily 同一把非阻塞 `daily.lock`；daily 正在执行或 scheduled 等待时拒绝并提示 `Daily round already running`，不删除锁文件、不打断进程。锁覆盖读取、校验、备份、写入和回读。每次实际写入都备份 `config.yaml`；修改 `daily.yaml` 时也备份它。保留原权限，备份权限为 0600。

修改通过 YAML 节点位置替换目标值或插入目标块，保留目标之外的注释、引号、顺序和空白；多行文本保留块样式及头部注释。被整体替换的列表/映射块内部会按新值生成。遇到重复键、锚点、别名、合并键，拒绝编辑并保留原文件。缺省字段写入 `config.yaml`；如果字段来自 `daily.yaml` 则写回该文件。遵守现有浅合并规则：`config.yaml` 中同一 section 的直接子节点覆盖 `daily.yaml` 中的对应整块。写完必须 `load_config()` 校验并核对目标生效值，失败恢复全部触及文件，原本不存在的文件会移除，备份保留。写入不重启服务；已启动进程继续使用其已加载配置，新调用/下一轮重新加载。daily.hour/minute 的 LaunchAgent 日历触发仍需之后由部署流程重装；不会由本工具运行安装脚本。

工具 annotations：`get_settings` 采用现有 READ（`readOnlyHint=true, openWorldHint=false`）；`update_settings` 采用 WRITE（`readOnlyHint=false, destructiveHint=false, idempotentHint=false, openWorldHint=false`），即使默认调用只是预览也按可写工具声明。校验失败或锁忙通过 MCP 工具错误返回；CLI 错误输出 JSON 到 stderr，退出码 1。

CLI 与 MCP 调用同一服务，值使用 JSON，文本需含 JSON 双引号：

```sh
job-agent config show
job-agent config show engine.platforms.boss
job-agent config set engine.day_limits.sat 2 engine.day_limits.sun 2
job-agent config set engine.day_limits.sat 2 engine.day_limits.sun 2 --confirm
job-agent config set collect.keywords '["Agent开发工程师", "AI应用开发工程师"]' --confirm
job-agent config set engine.platforms.boss.greeting_max_length 100 engine.platforms.boss.greeting '"您好，想了解这个岗位。"' --confirm
job-agent config set daily.deliver_enabled false --confirm
```

调整投递开关或招呼语不等于取得某次投递的用户授权；交互发送仍须重新完整预览并明确确认。引擎配置变化会使旧批次确认失效。测试与演练始终使用临时 `JOB_AGENT_HOME`。
