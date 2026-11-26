> 历史离线骨架说明；当前已接入真实适配器和 own 数据层，入口与安全规则以 [架构](../ARCHITECTURE.md) 为准。以下示例用于离线理解。

# #15 C：投递引擎离线骨架

本轮只提供可恢复队列和假件。不连接 Ego Lite，不投递，不读取 BossHunter 源码或数据库。不改变 `preview_greetings`、`send_greetings`、`delivery_status`、`stop_delivery` 及现有通知链路。真实入口即使配置 `dry_run: false` 也会拒绝，等待 P0 适配器及既有一次性确认批次绑定后再接通。

## 离线运行

在 `mcp/` 中执行：

```python
from engine.adapters import FakeAdapter
from engine.config import EngineSettings
from engine.models import Job
from engine.preflight import FakePreflight
from engine.queue import DeliveryEngine
from engine.spaces import FakeTaskSpaces
from engine.store import SQLiteDeliveryStore

# 使用单独的模拟库；参考实现不会自动创建或接入 D 的岗位库。
store = SQLiteDeliveryStore('/tmp/job-agent-engine-demo.sqlite3')
try:
    config = EngineSettings(platforms={'boss': {
        'enabled': True, 'greeting': '您好', 'greeting_max_length': 10,
        'daily_limit': 1, 'interval_seconds': 0, 'attachment_name': 'resume',
    }})
    engine = DeliveryEngine(store, config, {'boss': FakeAdapter()},
                            FakePreflight(), FakeTaskSpaces())
    engine.enqueue([Job('boss', 'synthetic-1', 'https://example.invalid/job')])
    print(engine.run())
finally:
    store.close()
```

上例长度值仅用于模拟。`config.example.yaml` 四个平台默认禁用，长度上限 0 表示尚未取证。填入 P0 真实上限才能启用；字符串长度暂按 Python `len` 计，不截断、不规范化文案，平台真实计数单位仍待取证。空附件名表示无需附件，非空时预检必须确认唯一精确匹配，选择后还要读回。假件的 `sent` 只是模拟结果。

## D 对接的存储接口

协议：`mcp/engine/store.py:DeliveryStore`。模型：`mcp/engine/models.py`。不要求 D 采用参考实现的表结构。

```python
class DeliveryStore(Protocol):
    dry_run: bool
    def exclusive(self) -> ContextManager[None]: ...
    def add(self, job: Job) -> bool: ...
    def records(self) -> list[Record]: ...
    def update(self, job: Job, status: Status, reason: Reason,
               attempted_at: float | None = None) -> None: ...
```

- `Job(platform, job_id, url)`：job_id 必须是稳定的原站岗位 ID。唯一键 `(platform, job_id)`，不能仅按 URL 或本地导入行号去重。四个平台值为 `boss / zhilian / liepin / 51job`。
- `Record(job, status, reason, attempted_at)`：每个岗位一行。attempted_at 为 UTC epoch 秒，发送尝试开始时间；`None` 表示未尝试。
- `exclusive()`：同一数据库跨实例/跨进程互斥，锁覆盖整个 run（包括等待及适配器副作用），崩溃由 OS 释放。竞争者拒绝运行。引擎的 enqueue 也持此锁；调用者不得重入。D 的锁必须提供相同语义，不能只锁单个 SQL。
- `add()`：原子 insert-if-absent；返回是否新增，重复不能覆盖 URL/状态。引擎 enqueue 返回新增条数；被拦的重复不创建第二条记录。
- `records()`：返回稳定入队顺序的快照，包含所有状态及时间戳，供恢复、平台阻断、日限额和间隔检查。
- `update()`：在返回前提交持久化；找不到键报错，传 `attempted_at=None` 时保留原时间戳。不删除旧记录、不重置为 pending。运行前所有读改写均在 exclusive 内。
- dry-run 与真实库必须隔离，参考库初始化写模式标记，混用会报错。参考实现用 `engine_meta / engine_deliveries`，不访问 D 的表。仅适用于本机文件系统（Mac/Linux flock），非分布式调度器。

没有自动重试或解除 unverified 的接口。后续人工核实流程须保留审计记录，再决定如何解除平台阻断。本轮 failed/skipped/sent 也都不可自动重排。D 导入历史时须按相同岗位键映射，导入及状态映射由 D 负责。

## 流程与状态

`ensure(name)` → 只读预检（空间、登录、附件）→ `open_job` → `check_applied` → **持久化 unverified/interrupted** → `send_greeting` → `choose_attachment`（配置非空才调用）→ `confirm_receipt` → `close`。

`open_job` 必须只读。`send_greeting` 视为最早可能产生申请的边界；实际平台若选择附件先于提交，由后续适配器封装处理并保证回执覆盖全文和附件，不能把本轮抽象顺序直接套到未取证页面。`check_applied` 只有明确 False 才能继续，未知值阻断。close 只能关 adapter 自己创建的临时标签页，不能关登录标签页；部分打开失败也调用清理。清理失败停止本次队列，已确认 sent 不改为失败。

| 状态 | 语义 |
| --- | --- |
| pending | 未尝试，禁用或触达日限额仍留队列 |
| sent | 明确收到完整投递回执（本轮仅模拟） |
| failed | 副作用前明确失败，无自动重试 |
| unverified | 已有申请状态/发送结果不明，持久阻断该平台 |
| skipped | 原站明确已投，跳过 |

完整原因枚举及含义在 `Reason`：空值=无异常；`disabled`=平台禁用；`daily_limit`=当日尝试额度耗尽；`already_applied`=原站已投；`login_required`=登录未确认；`task_space`=空间未确认；`attachment`=附件不唯一/不匹配/选后读回失败；`precheck_error`=副作用前异常；`not_implemented`=等待 P0；`interrupted`=进入发送前已落盘，进程可能中断；`receipt_unknown`=无明确回执/已投状态不明；`effect_error`=副作用阶段异常。

发送阶段抛错一律 unverified，其他平台继续。重启看到 unverified 仍停该平台。进程若死在回执落盘前，即使页面实际成功也不能自动重投。日限额按配置时区的日历日，保守计入所有发送尝试（含未知）；间隔按开始时间计算并持久化，等待每 0.25 秒检查停止信号。`run(should_stop=...)` 在岗位边界及等待中停止；不从正在执行的同步适配器中途撤销副作用，未来适配器需有超时。配置在 run 开头重验并快照。

## 待 P0 和后续集成

四个空实现：`BossAdapter / ZhilianAdapter / LiepinAdapter / Job51Adapter`，继承的六个方法均抛 `NotImplementedError`。`TaskSpaces.ensure(name)` 的真实调用留在接口 TODO，当前只用内存 fake 验证同名复用和丢失后重建。没有浏览器导入，也没有真实 preflight。

仍需：P0 页面夹具与真实长度规则、适配器/真实预检/真实空间管理、D 的存储接入、既有确认码绑定不可变批次（含全文、附件和配置）、真实发送及单岗用户验收、汇总通知接入。本轮不等于 #15 的 P1 原站投递验收完成。

测试：`cd mcp && uv run python -m unittest discover -s tests`。包含实际子进程在发送边界 `os._exit` 后重启读取参考 SQLite，确认不会重投。
