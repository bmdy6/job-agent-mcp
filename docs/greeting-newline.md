# 多行招呼语

`browser/humanize.js` 把全文预先展开为输入步骤。`fill` 默认拒绝多行，调用方必须显式给安全换行规则；普通字符保持逐字随机间隔。换行不交给 `keyboard.type()`，也不使用普通 Enter。

| 编辑入口 | 平台声明 | 当前证据与行为 |
| --- | --- | --- |
| BOSS 已有聊天框 | `shift_enter` | 实测 Shift+Enter 输入，219 字、5 行全文相同，最后清空；未发送 |
| BOSS 首次沟通 textarea | `insert_text` | 原生 textarea 用 `insertText("\n")`，不产生 Enter 键事件；离线覆盖，未触发真实首次沟通 |
| 猎聘 | `reject` | 尚未实测换行键，先按配置分隔符替换；不宣称平台不支持换行 |
| 智联 | `reject` | 维持 `platform_default`，本次未修改原站设置或配置文案 |
| 51job | `reject` | 桌面无已验证招呼编辑器，继续申请/附件流程，不新增聊天动作 |

`engine.platforms.<平台>.greeting_newline_separator` 默认 `"；"`，也可设 `" "`。只有未验证/不支持换行的入口使用它。`platforms/greeting.py` 统一把 CRLF、CR、Unicode 行/段分隔符转成 LF，再按平台规则保留或替换。浏览器层不偷偷规范化，非 LF 行分隔符直接拒绝。

预览展示经过转换的有效全文；引擎输入和 `deliveries.greeting` 记录同一份全文，`prepare_run(..., greetings=...)` 在锁内检查它与不可变批次完全相同。修改原文或分隔符后必须重新预览/确认。BOSS 聊天框与首次沟通弹窗、猎聘聊天框都会全文读回，含换行不一致即抛错、停止提交；引擎保留既有不明副作用状态，防止自动重投。

预览新增 `greeting_versions`，MCP 条目新增 `greeting_version`，格式为 `sha256:<有效全文的完整摘要>`。#50 可直接调用 `platforms.greeting.greeting_version(delivery['greeting'])` 计算历史版本，不需迁移或重写已有记录。保留换行与替换分隔符后的文案属于不同版本；没有全文的历史记录不能据此推断文案。这里只提供文案标识，不新增效果统计。

## 受限真实验收

2026-11-26，在 Ego Lite「job-agent 采集」的一个已有沟通会话，用本分支 `EgoPage.fill` 完成输入 → 读回 → 清空。结果见 [脱敏证据](greeting-newline-evidence.json)：4 次 Enter 事件均带 Shift；普通 Enter 为 0；全文 219 字、5 行相同；清空回读为空；输入及清空前后消息投影不变。没有调用适配器发送方法、点击发送按钮或首次沟通入口。截图包含私人聊天，仅存本机，路径记录于本地 `result.md`。

未验证真实发送、首次沟通多行提交、猎聘换行和服务端接收效果。本次不为了验收发送任何消息。

## 配置与部署

本机配置修改前已备份，只替换 boss、liepin、51job 的 `greeting` 为 #59 原文（YAML `|-`，无尾换行）；智联和其余字段保持一致。未添加新配置字段，旧生产模型仍能解析。`daily.enabled` 保持 false。

**代码仍需 Claude 审查、合并后通过 `scripts/deploy.py` 部署。部署前不要使用旧 runtime 发送新版多行文案：旧输入代码仍有误发送风险。** 配置更新不等于新代码已生效，本次未重启生产服务或切换 `runtime/current`。

全量测试（`cd mcp && PATH=$HOME/.local/bin:$PATH uv run python -m unittest discover -s tests -v`）：535 项，534 通过，1 项显式 opt-in 部署演练跳过。另通过 compileall 和 sdist/wheel 构建。
