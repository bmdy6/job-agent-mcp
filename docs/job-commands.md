# 私有求职指令（Issue #51 Part 2）

job-agent 不接收 Telegram updates，不设置 webhook。消息入口始终是息壤 Hermes 网关；只有团队仓库的原生 `job-commands` 插件负责检查 Telegram owner 私聊身份，然后调用已安装 CLI。

```sh
~/.job-agent/runtime/current/.venv/bin/job-agent command status
~/.job-agent/runtime/current/.venv/bin/job-agent command today --json
~/.job-agent/runtime/current/.venv/bin/job-agent command pause
~/.job-agent/runtime/current/.venv/bin/job-agent command resume
~/.job-agent/runtime/current/.venv/bin/job-agent command only_reply
```

MCP 工具 `command(action)` 接受同样五个值。运行模式变更需要用户要求；它不会发送消息或投递。CLI 的可读输出为简短中文；`--json` 增加结构字段，仍不含 HR 姓名、消息正文或 URL。today 用平台名、数据库岗位编号及截短脱敏的公司/岗位名列出至多 20 项，完整数量单独显示。公司/岗位名会移除 URL 及数据库中已知的 HR 姓名；可在本地用原有查询工具查看岗位详情。

| 模式 | daily / 采集 / 投递 / 跟进 | monitor |
|---|---|---|
| running | 按原配置和调度执行 | 按原配置执行 |
| paused | 暂停 | 保持进程空闲，恢复后继续轮询 |
| only_reply | 暂停 | 保留监听 |

运行模式复用 schema v7 的 `runs` 表，固定 `run_id=automation:control`，`kind=automation_control`。无需迁移，无配置文件改写。缺省 running；非法持久化模式保守按 paused 处理。`resume` 只切换这个状态，不删除 `platform_pauses`；风控恢复仍需原有 `resume-platform` 的人工检查流程。

暂停在 worker 启动、采集步骤间、投递批次间和投递引擎的协作停止检查点生效。已经发出的网络请求不强行中断或重放。长期运行的 monitor 在 paused 时保留进程，`resume` 不依赖 launchd 重启；单轮 `monitor --once` 和 daily 的暂停跳过正常退出。daily 等待活跃时段时也检查暂停。

status 按 `engine.timezone`（默认中国时区）统计当天去重岗位数：真实 `attempt/followup` 的 sent，平台原生 inbound 回复，以及回复发生当天的面试邀请。历史导入、reconcile 和只读回执补查不会算成当天新投递。面试数是邀请数，不代表用户已确认的安排。today 复用 daily 候选筛选及简历能力、额度、去重和风控保护，显示当前岗位池的计划，后续采集和运行时检查仍可能改变名单。

## 与并行跟进分支衔接

本分支基线没有跟进计划接口，today 明确显示「自动跟进：未启用」。合并 `feat/followup-qa` 后，由其入口在规划及发送前调用：

```python
from job_agent.service.automation import allowed, hold
if deferred := hold('followup'):
    return deferred
```

跟进发送必须继续走共享 `service.deliver.confirm`，其中会在确认前检查状态，共享 DeliveryEngine 也会在启动和协作停止检查点读取运行状态，覆盖后续的 followup kind。新入口仍须检查自身的规划与非发送副作用。不得仅修改配置开关，或清除平台风控暂停。集成后补 `today` 的跟进只读摘要；不在此分支猜测另一个 worktree 的接口。

## 离线验证

```sh
export JOB_AGENT_HOME=$(mktemp -d)
export PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH
JOB_AGENT_DEPLOY_REHEARSE=1 uv run --project mcp python -m unittest discover -s mcp/tests -t mcp -v
```

部署演练从 Git 提交构建归档，所以最终提交后必须重跑。禁止对生产 home 进行测试。Hermes 的安装、身份验证和回滚见团队仓库 `docs/job-commands.md`。
