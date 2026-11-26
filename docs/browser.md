# 浏览器连接验证

2026-11-18（macOS）结论：**Ego Lite 支持标准 CDP，可通过 BossHunter 自带 Browser Runtime 连接。本次连接测试通过，无需退回 Chrome。**

## 实测依据

- 任务给的 `~/Applications/ego lite.app` 不存在；实际安装位置是 `/Applications/ego lite.app`。已有主实例保持运行，验证使用独立用户数据目录和服务名。
- `ego-browser` CLI 能管理 TaskSpace、页面和内部 CDP，但 `help` 未列出对外 CDP 监听开关；`Browser.getBrowserCommandLine` 在其内部接口不可用。因此验证了应用可执行文件的 Chromium 启动参数。
- `--remote-debugging-port=9339 --remote-debugging-address=127.0.0.1` 成功开放监听，`GET /json/version` 返回 `Protocol-Version: 1.3` 和 `webSocketDebuggerUrl`。产品标识为 `Chrome/156.0.8078.96`；这是 Ego Lite 的 Chromium 产品标识。
- BossHunter 原有 `cdp-proxy.mjs` 在 `BOSSHUNTER_CHROME_PORTS=9339` 下连接成功。通过其 `RuntimeClient.new_tab("https://www.zhipin.com/")` 创建页面，再用 `evaluate(..., "document.title")` 读到标题：**BOSS直聘-找工作BOSS直聘直接谈！招聘求职找工作！**
- Runtime 健康状态最终为 `connected: true`、`chromePort: 9339`。上游把 Chromium 产品标识显示成 `browserName: "Google Chrome"`，不能据此认定进程是 Chrome；监听端口实际由上述 Ego Lite 进程持有。

## 复现连接

以下是独立测试实例的启动方式。路径按实际安装修改；用新目录避免复用正在运行的实例吞掉启动参数。

```bash
"/Applications/ego lite.app/Contents/MacOS/ego lite" \
  --user-data-dir="$HOME/.job-agent/ego-cdp-profile" \
  --ego-server-name=job-agent-cdp \
  --remote-debugging-port=9339 \
  --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check about:blank
```

在 job-agent 用户配置中记录：

```yaml
browser:
  name: ego-lite
  cdp_url: http://127.0.0.1:9339
```

BossHunter 的常规配置可以设置：

```yaml
browser:
  chrome_ports: [9339]
```

仅本次连接验证用独立代理端口运行原始脚本，未改上游文件和配置：

```bash
BOSSHUNTER_CHROME_PORTS=9339 \
BOSSHUNTER_BROWSER_PROXY_PORT=13456 \
BOSSHUNTER_ENABLE_PORT_GUARD=false \
node /path/to/BossHunter/src/bosshunter/browser/runtime/cdp-proxy.mjs
```

`RuntimeClient` 以 `{"browser": {"proxy_host": "127.0.0.1", "proxy_port": 13456}}` 连接该测试代理。关闭 port guard 仅用于这次简单连接测试，未更改 BossHunter 的日常默认配置。job-agent 的 YAML 用于记录浏览器偏好；它不会自动改写 BossHunter 配置。

## 验证边界

只创建 BOSS 首页、读取 URL/页面标题/readyState，随后关闭测试页面和专用实例。没有登录、采集、评分、打招呼、发消息或改招聘平台数据；不代表已验证登录态复用或四个平台采集。本次独立空资料目录报告过 macOS Keychain/encryption 不可用，但没有阻断 CDP 和首页标题读取，也未读取用户密码或 cookie。

如果另一台机器的 Ego Lite 版本无法开放 CDP 或无法经上述 Runtime 读取标题，则退回支持 CDP 的 Chrome；不要把 `ego-browser` 内部页面操作成功等同于 BossHunter 已能连接。

## #15 当前连接方式与任务空间生命周期（2026-11-24）

自建引擎按 #15 硬约束，只通过**正在运行的 Ego Lite** 执行 `ego-browser nodejs`，按名称 `taskSpace("job-agent 投递")` 复用或创建。上文 11-18 的独立实例/CDP/Chrome 回退是历史连接实验，不是 #15 的运行方式；当前任务不得启动那些实例或使用 Chrome 回退。P0 A 仅使用「job-agent 取证 A」，没有连接、切换、关闭空间 28「BossHunter 投递」的页面，也没有修改 BossHunter。

### 哪些动作会让空间消失

依据当前 CLI 的 `help("TaskSpace.finish")`、`help("taskSpace")`、`help("claimTaskSpace")` 和自己的空间实测，保存于 生命周期夹具（原始过程材料已移除）：

| 情况 | 当前证据与结论 |
| --- | --- |
| 关闭临时标签页，另有标签保留 | 关闭自己的 p4、p2 后，空间 29 仍存在。 |
| 关闭最后一个标签页 | 关闭 p1 后立即查询，空间 29 仍在；随即按同名解析仍返回 29。**本次没有复现“关最后一页立刻删除空间”**。观察窗口很短，不能排除异步清理或其他版本行为。 |
| `finish({keep: []})` 且没有受保护标签页 | 内置帮助明确说明会关闭空间。本次返回 `closedSpace=true / preservedUnmanagedCount=0`，随后列表中已无自己的空间；这是本次确认的删除条件。 |
| finish 时保留页面/有用户或 unmanaged 页面 | 帮助说明保留/受保护策略影响是否关闭整个空间；本次没有借用户页面验证这一分支。不要为清理空间强关这些页面。 |
| 用户接管、空间 inactive 或名字被用户拥有 | 是控制权问题，不能等同于已删除；同名 user-owned 空间会报 `EGO_TASK_SPACE_NAME_IN_USE`。帮助要求先有用户批准才能 claim，不能偷偷抢回或绕过另建同名空间。 |
| 用户从界面关闭/删除空间 | 本次没有操作用户界面验证，不能据此判定早先事件由用户触发。 |
| 超时/闲置自动删除 | P0 当时所读技能与 API 帮助未给出 TTL，也未做长时实验。后续 R 的 A/B 实测见下文。 |

**今天空间 14、23 为什么消失仍未知。** 没有它们的删除事件记录或当时 finish 回执；“编号不在了”不足以区分 finish、用户操作、异步清理等原因。不能用上面的单次实验倒推历史因果，也不应继续把旧版“关最后页必销毁”的经验当当前保证。完整事件时间与回执（原始过程材料已移除）

### 引擎应如何管理空间

- 名称是配置；数字 ID 是当前连接结果，不写死为持久配置。开始一轮工作时按名称解析，确实缺失才创建同名 Agent 空间。空间存活但无 Page 时需要 `newPage()`；不能假设 `p1` 一直存在。API 帮助和实测（原始过程材料已移除）
- 投递/监听期间保留一个自己的锚点标签页，每个岗位另开临时 Page，用完关闭该 Page；不要关闭用户的登录页，也进程结束也不 `finish`。历史取证中 finish 移除了无受保护页面的空间；当前实现始终保留锚点及 Agent 所有权。生命周期证据（原始过程材料已移除）
- 用户接管、inactive、同名 user-owned 时停止，等用户明确继续；缺失重建不能绕过控制权。`claimTaskSpace` 与 `takeOverTaskSpace` 不是自动恢复快捷键。内置帮助摘录（原始过程材料已移除）
- 真正缺失后的重建只恢复浏览器容器；投递是否已经提交须从原站和持久队列核对，结果不明保持 `unverified`，不得因换了空间而重发。这是 #15 的实现要求，P0 A 没有实现或验证引擎恢复逻辑。

排查时记录时间、空间名称/当前 ID、临时 Page 标签、关闭/finish 动作及回执、ownership。不要记录 cookie、securityId、聊天正文或其他空间的 URL。取证 A 的生命周期夹具只包含自己的空间状态和“28 仍存在”的布尔检查；实验结束已 finish 自己的空间。

## 第二轮生命周期修正

进程结束也不调用 `finish`。shutdown 只关闭本实例创建的临时页，锚点页始终保留，任务空间持续由 Agent 持有。不得关最后一页或接管用户空间。旧的“退出 finish + keep”规则已废止，因为 keep 仍会把空间交还用户。

## #15 R：闲置回收与保活实测（2026-11-24）

完整观察 60 分钟：B 在第 25 分钟仍在，第 30 分钟已缺失；A 每 5 分钟执行一次轻量调用，13 个采样点均存活且执行成功，始终为同一空间 47。实测支持闲置回收，并支持采用 300 秒保活。

### 方法与证据

- 环境：正在运行的 Ego Lite 0.6.3.17（Chromium 156.0.8078.96、内嵌 Node v24.22.1）。不启动其他浏览器实例。
- 2026-11-24 20:55:48（UTC+8）创建「job-agent 保活测试 A」=47、「job-agent 保活测试 B」=48，均仅有 p1 `about:blank`。
- A 每 300 秒按 ID 解析自己的 TaskSpace，再对 p1 执行 `evaluate("1")`。B 创建后不再解析空间或访问页面。
- 每 300 秒调用 `listTaskSpaces()`，只保存这两个名称和 ID 的存在性与 ownership。中途不重建 B，不 finish，不新增其他实验空间。
- 首次采样在创建后约 24 秒；后续按创建时刻每 300 秒采样。时间戳使用 UTC，下面换算为 UTC+8。
- 原始 13 次采样记录（原始过程材料已移除）、帮助摘录（原始过程材料已移除）、真实驱动轻量检查（原始过程材料已移除） 和 清理回执（原始过程材料已移除） 已归档。

| 时刻（UTC+8） | 创建后分钟 | A=47 | B=48 | A evaluate |
| --- | ---: | --- | --- | --- |
| 20:56:12 | 0.4 | 在，agent | 在，agent | 1 |
| 21:00:48 | 5 | 在，agent | 在，agent | 1 |
| 21:05:48 | 10 | 在，agent | 在，agent | 1 |
| 21:10:48 | 15 | 在，agent | 在，agent | 1 |
| 21:15:48 | 20 | 在，agent | 在，agent | 1 |
| 21:20:48 | 25 | 在，agent | 在，agent | 1 |
| 21:25:48 | 30 | 在，agent | 缺失 | 1 |
| 21:30:48 | 35 | 在，agent | 缺失 | 1 |
| 21:35:48 | 40 | 在，agent | 缺失 | 1 |
| 21:40:48 | 45 | 在，agent | 缺失 | 1 |
| 21:45:48 | 50 | 在，agent | 缺失 | 1 |
| 21:50:48 | 55 | 在，agent | 缺失 | 1 |
| 21:55:48 | 60 | 在，agent | 缺失 | 1 |

### 能确认到哪一层

本轮支持“Agent 空间长时间无操作会被清理，周期轻量调用可保活”的判断；没有持续新建空间，B 仍消失，因此“每次新建挤掉旧空间”不能作为唯一解释。空间列表查询本身没有保住 B。A 的操作包含 TaskSpace 解析与 evaluate，本轮不能单独区分哪一个动作续期。

5 分钟采样只能把 B 的消失限制在第 25–30 分钟之间，不能证明固定 30 分钟 TTL，也不能由一组样本保证所有版本、窗口状态或系统休眠下都一样。没有 Ego 的删除事件与内部清理实现，历史空间 14、23、28、43、45、46 的个别删除原因仍不能倒推成已证实。

当前 CLI `--help`、`ego-browser skill`、TaskSpace/Page 帮助和本地 API 参考均未说明空间 TTL、idle threshold 或 lease。查询 `help("ttl")` / `help("idle")` / `help("lease")` 都返回 Unknown helper；`finish({keep: []})` 仍是帮助明确给出的主动关闭条件。

### 新引擎保活

```yaml
browser:
  keepalive_seconds: 300 # 默认 5 分钟；0 表示关闭
```

默认值选本轮 A 使用的 300 秒，明显短于 B 的 25–30 分钟消失窗口。只有进程取得自己的空间后才启动驱动线程；监听、投递、采集都传入该配置，在轮询间隔或投递等待期间仍能轻触锚点。保活与前台命令串行，不并发操控同一驱动。

- 名字存在且由 Agent 拥有：保留自己的 `about:blank` 锚点，只在锚点 evaluate `1`；缺少锚点时补自己的页面，不导航用户页。
- 名字确实缺失：按同名创建空间和锚点。进程已知 ID 变化时，走现有启用的 Telegram 通道通知重建；初次取得空间不当作丢失告警。
- 长驻调用方保留的 Space 可为后续读取新开临时页；旧 Page 仍绑定旧空间 ID，点击、发送、导航均不迁移或重放。结果不明仍由原队列记 `unverified` 并停该平台。
- 用户接管、inactive、同名属于用户或存在重名歧义：停止该名字的保活并告警，当前实例不再解析它，不 claim/takeOver。需用户明确检查并重启。
- CLI 超时或普通保活失败：不重放页面动作，不将错误当作“空间缺失”绕过控制权。连续同类保活错误仅首次告警，恢复后重置；TG 失败只记日志，不覆盖业务结果。
- shutdown 先停止相应保活，再清理自己的临时页，不 finish；锚点保留。**进程退出后不再保活，锚点仍可能闲置回收**，下次运行按名称重新解析。系统休眠和很长的前台命令也可能延迟保活，不能保证永久存活。

TG 沿用 `notify.enabled`、启用的 Telegram channel 和环境变量；关闭通知时只留日志。本任务不真实发 TG、不投递、不发 HR 消息。

### BossHunter 过渡模板

见 [脚本、plist 和安装/停用说明](../examples/bosshunter-keepalive.md)。固定处理「BossHunter 投递」；空间缺失才创建同名空间并打开聊天页，先备份原配置，再只更新实际读取的 `delivery.ego_task_space_id`。不改 `collection` 或顶层同名字段。

本任务只交付模板，不运行它，不安装或加载到 `~/Library/LaunchAgents`，不修改真实 BossHunter 配置。模板恢复及写配置只用离线假 API 和临时文件测试；是否安装、BossHunter 是否需要重启加载配置由用户决定。

观察结束后仅关闭 A 的自建 p1，B 已缺失无需操作；未 finish、未操作其他空间。新驱动轻量检查通过，TG 与真实投递未运行。

## 离线取证边界

平台夹具是脱敏 DOM 片段，保留 class、层次和通用界面文案，替换身份、岗位、消息、附件与 ID；可用于解析和去重测试，不是完整网页或真实发送回执。时间和 ID 是合成值，不同文件之间不保证关联。保活观察只支持该版本下的闲置回收与周期调用续期，不证明固定 TTL；原始过程证据已移除。
