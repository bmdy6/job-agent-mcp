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
