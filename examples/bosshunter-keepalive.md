# BossHunter 过渡保活模板

这两个文件仅供用户审查后手动安装。本任务没有安装、加载 LaunchAgent，也没有运行模板或修改真实 BossHunter 配置。

- `bosshunter_keepalive.py`：单次运行，固定处理名称「BossHunter 投递」。
- `com.bmdy6.bosshunter-keepalive.plist`：launchd 每 N 秒调用一次，`StartInterval` 暂设 300 秒（5 分钟）。实测结论见 [浏览器文档](../docs/browser.md)。这个频率是实验频率，不是已证明的 TTL。

现有空间只选择 Agent 创建并管理的页面执行 `evaluate("1")`，优先 `about:blank`，不会导航已有页面。空间不存在时，才新建同名空间并将初始 p1 导航至 `https://www.zhipin.com/web/geek/chat`，再执行轻量 evaluate。不会投递、发送消息、关闭页面或 finish。

同名空间属于用户、已交还用户、inactive、重名不明确或没有 Agent 页时，单次任务失败退出，不 claim/takeOver。launchd 下一周期还会检查，但控制权没有变化就不会操作。浏览器命令超时或返回失败不重试，不把结果不明当作成功。

配置位置默认为 `~/Library/Application Support/BossHunter-示例用户/config.yaml`。BossHunter 的实际读取路径是 **`delivery.ego_task_space_id`**；脚本只更新这一处，不改 `collection` 或顶层的同名字段。只有 ID 需要更新时才生成同目录 `config.yaml.bak-<随机编号>`，权限 0600，内容为原始字节；再利用 YAML 节点位置仅替换这个整数，保留其他字段、注释和换行，通过临时文件原子替换并回读。已有 `delivery` 段缺少该字段时，在段内追加；原值为 `null` 时也能更新。缺少 `delivery` 段、复杂 YAML 写法、重复字段或符号链接拒绝处理；请手动核对。备份可能含密钥，留在本机，不上传。

浏览器已经创建成功而配置写入失败时，下次按名称复用已有空间，并再次更新 ID。检测到配置被其他进程修改会退出；独立锁防止本模板多实例重叠，不能锁住不使用该锁的 BossHunter。原子替换前会再次比较原文，但没有跨程序事务保证。建议先暂停会写配置的操作，并确认 BossHunter 是否需要重启来加载新 ID。

## 用户自行安装

1. 在准备长期保留的仓库检出中运行 `uv sync --project mcp`，确认该 Python 能 `import yaml`，Ego Lite 已启动。不要将 plist 指向会被删除的临时 worktree。
2. 复制 plist 到自己准备的临时文件，替换所有 `/ABSOLUTE/...`。launchd 不展开 `~` 或 `$HOME`。日志目录需先创建，使用私有目录；建议权限 0700。
3. 审查脚本和 [实验结果](../docs/browser.md)。确认接受它打开聊天页、备份并修改 BossHunter 的 `ego_task_space_id` 后，再手动执行下面的安装命令。不要把模板的测试命令当成只读操作。

```sh
# /path/to/reviewed.plist 是你完成绝对路径替换的文件。
plutil -lint /path/to/reviewed.plist
mkdir -p "$HOME/Library/LaunchAgents"
cp /path/to/reviewed.plist "$HOME/Library/LaunchAgents/com.bmdy6.bosshunter-keepalive.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.bmdy6.bosshunter-keepalive.plist"
launchctl print "gui/$(id -u)/com.bmdy6.bosshunter-keepalive"
```

`RunAtLoad=false`，加载后等下一个调度周期。macOS 休眠期间不能保证执行，不建议用它推断保活间隔或回收阈值；本模板不修改系统睡眠设置。

## 停用和恢复（用户自行执行）

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.bmdy6.bosshunter-keepalive.plist"
```

停用不会删空间、页面或配置。需要恢复旧 ID 时，先停用，再备份当前配置，手动比较目标 `.bak-*` 并只恢复 `delivery.ego_task_space_id`；不要覆盖 BossHunter 后续改过的其他配置。删除 plist 由用户决定。

## 离线验证

```sh
uv run --project mcp python -m unittest discover -s mcp/tests -p test_bosshunter_keepalive.py -v
plutil -lint examples/com.bmdy6.bosshunter-keepalive.plist
```

单测仅使用临时配置和假的 Ego API；Node 用于执行假 API 脚本，不连接浏览器。真实 BossHunter 空间与真实配置未验收。
