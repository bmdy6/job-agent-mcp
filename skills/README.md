# 技能

| 技能 | 作用 |
| --- | --- |
| [jd-digest](jd-digest/SKILL.md) | 把多份 JD 汇总成共性要求，每项附原句 |
| [resume-expand](resume-expand/SKILL.md) | 按 JD 汇总扩写主简历，只用已有经历 |
| [platform-sync](platform-sync/SKILL.md) | 主简历写入四个平台并回读 |
| [greetings](greetings/SKILL.md) | top N 公司只加一句 JD 依据；预览后用户确认再发送 |
| [learning-plan](learning-plan/SKILL.md) | 从 JD 提取技能，对照简历和知识库出可验证的学习计划 |
| [company-brief](company-brief/SKILL.md) | 手动触发一页公司简报，research-mcp + 网页搜索，逐项引用并缓存 |

安装：把技能目录复制或链接到 Agent 的技能目录，比如 `~/.claude/skills/` 或 `~/.codex/skills/`。

简历相关的技能只在这里维护。旧的 `skill-resume-platform-sync` 和本机的 `job-jd-learning` 已经合并进来（后者即 learning-plan），不再更新。


## 查看与调整 job-agent 配置

涉及平台开关、频率、每日/星期上限、筛选、关键词/城市、招呼语文本、附件名称或 daily 开关时，先调用 `get_settings(section)` 读取当前值和今天额度，再用 `update_settings(changes)` 展示前后对比；用户明确同意后才传 `confirm:true`。不要手工整文件重写 YAML。工具使用 Pydantic 字段白名单，凭据和本地路径只能查看是否配置，不能读出或修改；连接 URL 不能含凭据或查询串。daily 持锁时等待运行结束后再预览确认，不能删除锁文件或停止任务来绕过它。

例如先读 `get_settings("engine")`，再预览 `update_settings({"engine.day_limits.sat":2,"engine.day_limits.sun":2})`。CLI 等价入口为 `job-agent config show/set`，完整返回结构、时区与兼容规则见 [每日节奏文档](../docs/daily-pacing.md#通用-mcp-配置工具与-cli)。配置修改不替代投递确认；招呼语修改后应重新预览批次，保持 greetings 技能原有授权流程。
