# 51job 招呼语调研（2026-11-27）

独立 Ego Lite 任务空间「job-agent 51job招呼语」，调研结束 `finish({keep:[]})` 关闭。仅只读浏览，没有申请或发送消息（0 条）。

- `https://we.51job.com/pc/my/myjob` 已登录；推荐岗位的 `.chat` / “立即沟通”展开内容为 `.chat-detail .qrcode-tip`：「微信扫码与HR沟通」。没有取得桌面消息输入框和成功发送回执。
- `https://we.51job.com/pc/my/message` 未出现聊天正文；不能以此断言移动端也不支持。
- `https://i.51job.com/userset/security_center.php` 账号设置展示密码、手机、邮箱、第三方账号；未发现默认招呼语。
- `https://we.51job.com/pc/my/privacysetting` 展示个性化推荐、姓名隐私和屏蔽公司；未发现默认招呼语。

结论：本次没有取得桌面自定义消息或默认语同步能力的证据。保持 `greeting_mode: none`，不实现猜测的 `send_message`；配置其他模式会在申请前拒绝。同步命令对51job返回 unsupported。若后续通过移动端或新桌面入口取证，应重新验收输入回读、同岗位新消息ID、发送状态和完整文本，再启用。

真实发送为0条，因此未消耗最多2条的授权额度，未执行任何需 daily 停止的发送。附件回执继续要求 sendStatus=1、附件ID和cvLogIdList完全匹配；失败仍为 unverified，不重投。已有申请流程见 [job51.md](job51.md)。
