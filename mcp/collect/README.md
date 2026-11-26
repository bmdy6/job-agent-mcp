# 只读采集调度（#15 N）

```sh
job-agent collect --once --platform boss
job-agent collect --once --dry-run --platform boss
```

配置见仓库根 `config.example.yaml` 的 `collect:`。平台默认关闭；显式启用后才会采集。关键词、城市、参考城市编码来自用户 BossHunter 的数据；不读取其数据库或运行接口。适配器自行将 `SearchQuery.city` 映射为平台编码；编码配置仅供对照，不改变公共 SearchQuery。

- `collect.platforms.<平台>.daily_page_limit` 默认 20，按 timezone 日历日统计**搜索请求**，含空页和失败页。请求前预扣，中断不退还。详情请求不占搜索额度，但与搜索共用 8–15 秒随机访问间隔。`fetch_jd` 默认 false。
- 先写全部列表岗位并逐条做硬过滤。过滤结果保存在 `collection_filters`（job_id → 原因码数组），不会删除岗位。已有工具仍消费 `legacy_status`，所以可投递状态另映射为 `filtered` 和 `legacy_score_reason=预筛不通过:采集:...`；已投递状态和外部过滤结果不覆盖。解除本调度产生的过滤时恢复为未评分状态，不恢复 AI 分数/批准。
- 原因码：`salary_below_min`、`title_excluded`、`company_excluded`、`company_blocked`。字面包含匹配忽略英文大小写。薪资比较月薪下限，未知/面议/日薪/时薪放行，年薪折月薪；不做 AI 评分。
- 每页所有条目处理成功才推进游标；详情失败时保留已存岗位和已补 JD，下次显式启动重读这页、按唯一键去重，只补缺少的 JD。一个组合返回空页后完成；未完成的组合跨日继续，整轮完成后次日重新开始。
- `collection_usage / collection_cursors / collection_platforms / collection_filters` 为 store 组件的附加表，全部 SQL 在 `store/collection.py`，不调整共享 schema 版本和投递队列。锁以数据库路径派生，覆盖整个 run。状态查询返回当天页数、游标、过滤数、平台错误和运行锁状态。
- 所有异常停当前平台，BrowserError 同样不重试。空间被接管则停整轮。Telegram 仅使用现有 master/channel 开关，发送失败不影响其余平台；不记录异常原文、页面正文或凭证。未配置通知时状态仍保留错误。
- Browser.space 按配置名称取独立空间 `job-agent 采集`。适配器负责关闭临时页；退出再调用 EgoBrowser.shutdown 清理遗漏页。不会 finish，不访问 BossHunter 空间。
- `service.collection.start_collection(platforms=None)` 在后台子进程启动 CLI，一轮后退出；返回 PID 和平台。`collection_status()` 读持久状态和运行锁。多次快速启动可能产生短暂候选进程，只有拿到采集锁者会访问网页。本 PR 不改 server.py。
- dry-run 使用 FakeBrowser / FakeSource、独立 `collect-dry-run.sqlite3`，不调用真实浏览器或通知，跳过真实等待。Fake 平台允许忽略 live enabled 开关；数据不会进入正式岗位库。

注册契约：L/M 在 `platforms.registry.create_sources()` 返回平台到 JobSource 的字典。当前 main 尚无采集注册时，按 `platforms.<目录>.search.JobSource()` 别名发现；缺失源记录平台错误并继续。51job 目录为 job51。L/M 合并时应合并 SOURCES 的四个平台条目。

验证边界：N 只做离线测试；真实采集由 L/M 与后续集成验收。本分支基于 ab61619，该基线还不支持用户现有 engine.backend 字段，须与 K 配置模型一起集成。用户配置只追加 collect，保留现有 backend，备份路径记录在任务目录。
