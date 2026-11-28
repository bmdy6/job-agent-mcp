# 统一部署

`scripts/deploy.py` 从指定的已提交版本构建 `~/.job-agent/runtime/<12位提交>/`，包含独立 `.venv`。默认使用本地 `origin/main`，不自动 fetch、pull 或切换主仓库。每日任务和监听共用 `runtime/current`，用原子 rename 切换软链接。需要 Python 3.10+、Git、uv；原生服务控制需要 macOS 用户 launchd。

11-26 的已部署版本与后续顺序见 Issue #1。本说明描述部署机制；执行部署仍需先审查目标提交与停止其他写入者。

## 生产操作顺序

先停止 MCP/CLI 的写入活动，包括采集、评分和手动投递；部署期间不要重启 job-agent MCP，也不要先 pull 作为 MCP 入口的主仓库。脚本会拒绝活动的 own-tasks，检查 daily、collect、monitor 的运行锁。任意手写 SQLite 脚本或另一份 MCP 不受这些锁约束，须由操作者提前停用。

在审查通过的部署代码目录运行：

```sh
export PATH="$HOME/.local/bin:$PATH"
git fetch origin
python3 scripts/deploy.py --inventory
python3 scripts/deploy.py                       # 默认 origin/main
# 或固定一个已审查提交：python3 scripts/deploy.py --revision <full-commit>
```

一次部署依序：

1. 校验现有 plist 的 Label 和 JOB_AGENT_HOME，备份两份 plist、加载状态、config.yaml、daily.yaml 和旧 current 指向。
2. 停监听并等待原 PID 退出，卸载每日 calendar 防止迁移期间触发。
3. 使用 SQLite backup API 备份 jobs.sqlite3（包含 WAL 中已提交记录），备份权限为 0600。
4. 用 git archive 和 `uv sync --frozen --no-editable` 按已提交的 `mcp/uv.lock` 构建新 runtime（环境放在 runtime/.venv，Python 要求由 pyproject 声明）；完整构建后写入 deployment.json，已有不完整目录拒绝复用，留给人工检查。
5. 通过新 runtime 校验现有配置，并用临时数据库、FakeBrowser 和真实轮询器做冒烟，不访问网页、不发通知或投递。
6. 用新 runtime 的 Store 自动迁移实际部署目标的数据库。schema v4 来自 #52，v5 为 #57 的监听基线与会话摘要表。
7. 原子切换 current，并分别原子写入两份 plist。两项服务停用期间完成切换，失败时恢复整组状态。
8. 重装每日 plist（保留 calendar，增加 RunAtLoad 和 1800 秒周期触发），再启动监听。每日不设置 KeepAlive，也不 kickstart；启动时先检查当日完成标记和活跃时段，部署持有 daily.lock 时不会投递。监听需由新 PID 完成一次新轮询且无失败，默认等待上限 600 秒（可用 `--health-timeout` 修改）。

确认输出 `status: deployed` 和新轮询记录后，Mac 主仓库才可 pull 已部署提交，再恢复 MCP。`setup_check`、MCP `status`、`reply_monitor_status` 的 `deployment` 返回当前完整提交、schema、runtime 和 `executing_commit`；后者用于识别 MCP 是否仍在执行旧代码。工作树代码没有部署清单时 executing_commit 为 null，不冒充当前运行快照。

TG 的指令入口由 #51 实现，此基线没有 `/status` handler。本轮提供 `service.deployment.status()` 作为共享数据接口；#51 的 `/status` 需复用它。这部分尚未经过真实 TG 指令验收。

## 每日任务与凭据

不改 config.yaml、daily.yaml、daily.enabled、投递额度或任何密钥文件。已有 calendar、环境变量及每日任务临时 cap 参数会保留；首次安装才从合并配置读 hour/minute。不会因为重新部署开启每日任务。calendar 到达既定时间后仍按原有授权配置执行。

监听沿用 monitor-launch.py 的 ai-vault 注入；每日沿用 daily-launch.py 的私有 daily.env.json。部署本身不读 vault 或输出凭据。生产启动器、真实 Ego Lite 页面、平台登录和 TG 凭据仍需 Claude 部署时验收。

旧 install-daily.py/install-monitor.py 的可执行安装入口已停用，会提示改用 deploy.py；仅保留被既有测试使用的 plist/bootstrap 辅助函数。旧 watch 仍安装时，统一脚本会在停服务前拒绝执行，需要明确退役旧服务。

## 回滚

```sh
python3 scripts/deploy.py --rollback
```

恢复上一次部署前的 current、两份 plist、加载状态。schema 相同则保留部署后新增的数据库记录。可以连续回滚，撤销后的部署从活动历史移除；回滚本身也保留安全备份。脚本不删除任何运行快照。

v4 → v3（或第一次安装 → 无数据库）需要显式恢复数据库：

```sh
python3 scripts/deploy.py --rollback --restore-database
```

先停服务，保存回滚前最新数据库到输出的 safety_backup，再恢复部署前的备份及服务。**恢复数据库会撤销备份之后的记录，也会撤销相应的投递去重/暂停状态**；操作者需先核对新增投递与风险暂停记录，避免旧定时器恢复后重复处理。新版数据仍保存在安全备份，脚本不尝试把 v4 数据逆迁移回 v3，也不自动合并。

正常异常（构建、冒烟、迁移、plist、bootstrap、健康检查、Ctrl-C）会尝试停止新服务、归档失败时数据库并恢复部署前状态。恢复本身失败会报错并保留备份，不声称完成。SIGKILL、断电等不能执行 Python 恢复流程；需先停全部写入者和两项服务，检查 `deployments/<id>/before.json`、数据库备份、plist 与 current 后人工恢复，不能盲目重复部署。备份目录位于 JOB_AGENT_HOME/deployments/，不自动清理。

## 快照清理

```sh
python3 scripts/deploy.py --inventory
```

此命令只读，不调用 launchctl、不创建目录、不删除文件。输出每个目录、完整提交线索、所有 plist 引用、current、保留理由。保留最近三个版本、被引用快照、最近回滚所需快照以及身份不明目录。老快照只按目录名解析 Git 哈希，内容未逐文件核验。`candidate` 仅为人工清理候选，实际删除须另行确认。

旧快照的单次盘点文档已移除；重新运行 inventory 获取现状。

## 隔离演练与全量测试

```sh
cd mcp
PATH=$HOME/.local/bin:$PATH JOB_AGENT_DEPLOY_REHEARSE=1 \
  uv run python -m unittest discover -s tests -v
```

测试创建临时 JOB_AGENT_HOME 和 LaunchAgent 目录，把 LocalLaunchd 注入 Deployer。它不调用 launchctl：每日只注册内存状态，监听启动真实独立 Python 子进程、原有 monitor.runtime 循环和 FakeBrowser；采集源为空、通知关闭，不读取 vault。

演练实际归档/安装历史 v3 和当前 v4 提交，迁移、切换、观察不同 PID 的新轮询，拒绝无显式数据库恢复的跨 schema 回滚，再回滚并验证新轮询。没有设置 opt-in 时跳过这项较重测试，其余部署事务测试仍执行。可设置 `JOB_AGENT_REHEARSAL_DIR=/tmp/<new-directory>` 保留完整回执；`JOB_AGENT_DEPLOY_REVISION=<commit>` 固定 v4 演练版本。新建目录必须不存在，测试不会复用或覆盖已有演练。

Deployer 的 home、agents、launchd 可注入；自定义路径若没有注入控制器会被拒绝，以免临时 plist 意外操作生产服务标签。原生 launchd 的 bootout/PID 等待、bootstrap 重试和新鲜轮询条件另有合成测试；真实 macOS bootstrap 与真实网站轮询保留给生产部署验收。


## 临时脚本收口

生产目录中的四个临时脚本仅列为归档候选，由 Claude 审核后移动，不由本次代码执行：

| 原脚本 | 实际作用 | 替代方式 |
| --- | --- | --- |
| wk.sh | 拉取版本、停止 daily、部署、手工覆盖周末额度配置后再次启动 | fetch 后用 deploy.py；额度通过 settings set engine.weekend_daily_limit 修改 |
| dep.sh | 拉取版本、手工停止/启动 daily、执行 deploy.py | 直接使用统一部署事务；不再额外 kill/kickstart |
| night-deploy.sh | 轮询 daily 运行状态，部署并记录结果 | deploy.py --wait-idle 7200 --health-timeout 900；超时退出，不停止 daily |
| apply-smallco.sh | 覆盖整份暂存配置，手改 daily-last-run 状态，再启动 daily | 需要保留的业务字段使用 settings set；不提供手改状态标记的入口 |

```sh
# 由部署负责人审查目标提交后执行；本任务不执行
python3 scripts/deploy.py --revision origin/main --wait-idle 7200 --health-timeout 900
```

`--wait-idle` 是有上限的只读 daily.lock 等待，不创建锁、不发信号。它是部署前的辅助等待，随后仍按原事务获取全部必要锁；等待结束与正式取锁之间若出现新的任务，部署按原有行为拒绝并恢复服务状态。配置管理和目录盘点见 [settings.md](settings.md)。独立 heartbeat 安装器仅管理附加心跳服务，不构建或切换运行快照。
