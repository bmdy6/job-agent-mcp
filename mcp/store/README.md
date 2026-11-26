# 自有岗位库（#15 / D + E）

提供自有数据层与一次性 BossHunter 导入。`engine.backend: own` 的 MCP、采集、评分、投递、统计和监听共用此库。
SQLite 仅用 Python 标准库，无新依赖。路径默认 `$JOB_AGENT_HOME/jobs.sqlite3`，HOME 未设置时为 `~/.job-agent/jobs.sqlite3`。新库权限 0600。

```sh
cd mcp
uv run job-agent import-bosshunter
# 可选参数：--source /path/to/bosshunter.db --database /path/to/jobs.sqlite3 --source-id bosshunter
```

源库默认 `~/Library/Application Support/BossHunter/data/bosshunter.db`，用 URI `mode=ro`、`query_only`、单一读事务打开。不调用 BossHunter 写 API、不读取简历文件、不读取 HR 字段。只输出聚合计数，错误只输出类型。真实源数据和 history.detail 仅写入本机私有库，不可提交到 Git。

## API 给 C / 后续采集接入

```python
from store import Store

with Store() as store:
    job_id = store.upsert_job(platform='boss', platform_job_id='example',
                              title='AI 工程师', company='示例', url='https://example.org/job',
                              city='苏州', salary='面议', jd='岗位说明')
    delivery_id = store.record_delivery(job_id, status='pending', greeting='完整招呼语',
                                        attachment_name='附件文件名')
    # 流水只追加，不修改旧记录。
    store.record_delivery(job_id, status='unverified', reason_code='receipt_missing')
    store.add_reply(job_id, message_id='原站消息ID', direction='inbound', body='消息正文')
    store.get_job(job_id)
    store.get_delivery(delivery_id)
    store.list_jobs(platform='boss')
    store.list_deliveries(job_id)
    store.list_replies(job_id)
    store.counts()
```

岗位唯一身份为 `(platform, platform_job_id)`；使用 `boss / zhilian / liepin / 51job`。原生投递状态严格为 `pending / sent / failed / unverified / skipped`。回复方向 `inbound / outbound / system`；同岗位、同消息 ID、同方向去重。默认每个单条写入自动提交，批量操作可放在 `with store.transaction():` 中（不可嵌套）。每个 worker 自己打开 Store 连接。`schema_migrations` 与 `PRAGMA user_version` 记录版本；迁移在事务中执行，拒绝未来版本。

服务层负责用户授权；本模块提供队列和不可变批次，但不会执行投递。读写接口保留完整招呼语，不设 300 字上限。

## 导入语义与对账

- 所有岗位（含软删除）均保留；默认查询与兼容统计排除软删除。
- `source_history` 保留所有历史动作及原始 detail，按源 namespace + history ID 去重。岗位 ID 使用 source_job_id，老 BOSS 库缺失时回退到旧 id；平台独立，不跨平台合并。
- 每个岗位一个 `legacy_snapshot` 投递记录；发送/错误等历史事件另存为 `legacy_event`。`counts()` 的投递总数包含这两种，不能当已投岗位数。历史事件没有完整招呼语、附件名时留空；当前岗位招呼语只保留在快照上。history.detail 中的旧发送摘要仍可追溯。
- `sent/manual_sent/replied/resume_sent/needs_resume/follow_up_sent` 快照归为 sent；filtered/rejected 归 skipped；approved/ready/scored/pending 归 pending；旧 error 或未知状态归 unverified，避免将不确定结果当成可自动重试的失败。
- 旧库没有平台消息 ID，导入使用 `source_history` 编号生成稳定消息身份，`id_origin=source_history`。`hr_reply_detected` 可能只有 50 字摘要，设 `is_excerpt=1`。JSON `hr_question` 为入站；replied/auto_replied 的 manual_reply 或 ai_reply 为出站；待审批建议不当已发消息。摘要与后续完整记录保留为各自来源证据，不能把回复事件条数当独立会话数。不会从 history 重建完整聊天流水。
- 一次性导入采用 insert-once：相同 source-id 重跑新增缺失的岗位/历史，不刷新已有快照或覆盖引擎状态。源库后续状态更改不会同步。`source-id` 默认 bosshunter，移动同一库保持不变；独立库必须用不同 ID。来源历史孤儿记录会让整批回滚。
- `legacy_stats()` 与 `funnel_stats()` 是导入快照的兼容统计，可与导入前 stats/workbench 核对；它们不是原生引擎分析。保留旧评分元数据和“是否生成简历”布尔值以核对漏斗，不迁移简历内容或路径。`counts()` 另给出按平台及投递五态的统计。


## schema v2：统一队列和确认批次（E）

`Store` 默认打开 live 数据库；`SQLiteDeliveryStore` 在相同 schema 上实现
`engine.store.DeliveryStore`，实现只在 `store/delivery.py`，引擎只保留协议。
引擎当前仍只允许运行内置 Fake 的 dry-run；本次不接通真实发送或 MCP 服务层。

```python
from engine.models import Job
from store import SQLiteDeliveryStore

with SQLiteDeliveryStore('/private/live-jobs.sqlite3', dry_run=False) as db:
    config = {'example': '实际执行时应传完整 engine settings.model_dump()'}
    token = db.create_batch([Job('boss', 'example', 'https://example.org/job')],
                            {'boss': '完整招呼语'}, '附件文件名', config)
    db.get_batch(token)  # 供服务层渲染预览，返回独立副本
    db.confirm_batch(token, config=config)  # 只能在用户明确授权后调用
    db.batch_jobs(token)  # 预览时的 Job 快照
```

- `delivery_queue` 每岗位一行，五态、原因码、Unix 秒尝试时间、batch_id；
  `deliveries` 只追加（SQL trigger 禁止 UPDATE/DELETE），增加 batch_id。
  `update_delivery` 旧入口明确报错，调用方应追加事件；`update` 原子更新队列并追加审计。
- v1 事务升级，保留原流水所有字段；当前态优先原生 attempt，其次 legacy_snapshot，
  再其次 legacy_event（组内取最后 ID），避免旧历史事件覆盖导入快照。
  v2 上再次导入也只初始化缺失队列行，绝不覆盖已有队列。
- `create_batch(jobs, greetings, attachment_name, config) -> token` 使用随机 token，
  复制岗位列表和 URL、各平台招呼语全文、附件名，并保存 canonical JSON 配置 SHA-256。
  输入岗位不能为空或重复，平台必须有招呼语。创建后内容即不可改，任何编辑都要创建新 token。
- 状态 `pending / confirmed / executed` 分别代表待确认 / 已确认 / 已执行。
  `confirm_batch(token, config=None)` 在 flock + 单事务内确认并入队，拒绝已尝试或已绑定批次的岗位；
  任一冲突整批回滚。重复确认报错，不重复入队。可传当前配置提早检测预览过期。
- `prepare_run(config)` 在整个 run 的 flock 内核对队列批次状态和配置哈希；
  `records()` 使用已确认快照的 URL，`update()` 从批次快照取全文和附件写审计。
  未知的导入原因码在数据库原样保留，转换为引擎 Record 时用 PRECHECK_ERROR 表示。
  软删除或没有 URL 的岗位不进入可执行 records（仍保留队列表和流水）。
- live 的 `add()` 拒绝无确认入队；dry-run 可直接 add 供 Fake 测试。
  `complete_batch(token)` 要求已确认且无 pending，才标记已执行；有 unverified 也只是本批执行结束，
  不能理解为全部投递成功。调用方必须另外汇总五态。
- dry-run 必须指定独立路径；数据库记录模式，拒绝以另一模式重开。
  对 v1 live 库的 dry-run 拒绝发生在迁移前。新建数据库 0600，锁文件 0600。
  `exclusive()` 使用同数据库路径旁的 flock 文件，非阻塞拒绝第二 runner，进程退出自动释放。
  dry-run 执行审计也记录当次完整招呼语与附件，但不产生真实对外行为。


## schema v4：自有评分

`store/scoring.py` 的 migration 4 由 `database.MIGRATIONS` 按顺序执行，写入
`schema_migrations`，`user_version=4`。新增 scoring_runs（批次与材料快照）、
scoring_items（逐项状态/限流/用量）和 job_scores（原生结果/输入哈希）。旧评分字段
原样保留；活动岗位的原生分数经 service 的读取投影显示，不写回 legacy 字段。

待处理、执行中、完成及未知结果的岗位有唯一占位，防止跨进程重复付费；失败和未发出
请求的阻塞项需重新预览/授权才能重试。外部请求期间不持有数据库事务，成功时原子
写入分数与 item 状态。批次收尾清除简历/经历快照；JD 快照与理由只留私有数据库。
先备份并升级所有旧运行时，再迁移生产库；旧 v3 监听拒绝 v4。
