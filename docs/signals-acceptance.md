# #50 验证记录（2026-11-26）

交付：Draft PR #64，Closes #50；Claude 审查待完成。

基线 main b60a78d / schema v5；工作分支 feat/50-signals，迁移目标 v6。

## 自动化

- 基线：`PATH=$HOME/.local/bin:$PATH uv run python -m unittest discover -s tests -q`，535 项通过，1 项 opt-in 演练跳过。
- 当前：同一命令，554 项通过，1 项 opt-in 演练跳过；新增 19 项涵盖四平台解析、排序、存储、规则与模型校验、模型预算、持久分类队列/崩溃恢复、通知去重、cohort/区间、MCP 只读和 schema6 事务回滚。
- `uv run python -m compileall -q .`、`git diff --check` 通过。
- 完整 archive/venv 隔离演练已通过：`PATH=$HOME/.local/bin:$PATH JOB_AGENT_DEPLOY_REHEARSE=1 uv run python -m unittest discover -s tests -p test_deploy.py -q`，23 项通过、无跳过。以 b60a78d/v5 建旧运行时，迁到 9596ebb/v6，重启假监听进程、拒绝不恢复数据库的回滚、恢复备份回到 v5，并再次部署/回滚均通过。仅操作临时 home 和 FakeLaunchd，未操作真实服务。

## 四平台真实只读

仅使用 Ego Lite 的“job-agent 采集”（72），各平台一个搜索页，关键词 AI应用开发 / 上海 / 第1页。沿用生产 v5 rate limiter 预扣 collect 操作，只写频率记账，不迁移/更新生产岗位数据。页面渲染后在同一页复读，未翻页、未访问详情、未发消息/投递。原始页面投影和私有 SQLite 放在本机临时目录，不进入仓库。

| 平台 | 条数 | 活跃度 | 经验/学历有值 |
| --- | --- | --- | --- |
| BOSS | 15 | now 10，unknown 5 | 11 / 15；实习岗位未暴露经验时保留空值 |
| 智联 | 20 | unknown 20 | 20 / 20 |
| 猎聘 | 40 | unknown 40 | 40 / 40 |
| 51job | 20 | now 9，today 10，week 1 | 20 / 20 |

智联、猎聘此列表页没有明确活跃文本，unknown 是缺失标记，不是不活跃。51job 的“今日回复/查看简历”“本周人才搜索”作为明确 HR 行为证据。没有用职位更新时间推测 HR 活跃度。未遇验证码，未操作“job-agent 简历同步”空间。结束后关闭本轮临时页，保留采集引擎原有空白锚点。

## 历史回复回放

从生产库只读备份到隔离数据库，抽查 20 条（15 条完整原站回复、5 条历史导入，后者含节选与重复）。人工参考标签与分类逐条核对，原文、ID、企业和模型原始结果仅存本地私有证据，不进入公开仓库。

模型复用 scoring 的 gpt-6.1-sol / priority / ai-vault 配置。首轮 13 次 API；诊断 1 次；明确离线再回放失败样本 6 次，共 **20 次实际调用**，未超过 30。后两轮连接均被重置，生产分类器没有自动重试。provider_unavailable / ConnectionResetError 以置信度 0 保守保存。

最终规则修正后，15 条完整原站回复中 13 条与人工参考一致，2 条仍因 provider 连接失败待验证；5 条历史导入记录被生产分类器排除。历史样本没有真实明确邀约，因此邀约时间/形式、待确认列表和通知的验收来自合成边界夹具，不声称有真实邀约成功识别。没有把重复导入凑成 20 条独立完整 HR 回复，也不把网络兜底算识别成功。

## 审查与边界

已运行受限的 Claude CLI 只读审查，但以 `401 Invalid token` 失败，未产生审查结论；Claude 审查仍待完成。只可提交 draft PR，不能据此标记 ready/合并。

不部署、不启用模型自动分类、不发任何实际通知或消息、不投递。不修改 platforms/liepin/replies.py 和 monitor/delta.py。招呼语版本仅复用 #59 greeting_version，不新建模板版本体系；缺失历史正文归 unknown。
