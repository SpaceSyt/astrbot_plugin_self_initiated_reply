# Decisions 主动回复 · AstrBot

![版本](https://img.shields.io/badge/version-1.6.0-orange)
![许可证](https://img.shields.io/badge/license-GPL--3.0-blue)

使用 OpenAI Decisions API 从近期群聊中选择接话对象，再由 AstrBot 主模型生成回复。基于 [chengzhi-c/业镜](https://github.com/chengzhi-c/astrbot_plugin_self_initiated_reply) v1.5.1 的实验性 fork，保留上游贡献及 GPL-3.0 许可证。

## 工作方式

消息收集 → 固定窗口 → Decisions 选择 ignore / wait / reply:消息编号 → AstrBot 生成正文 → 校验目标并发送。

- 普通消息从首条开始等待 30 秒，后续消息不重置计时；不要求群聊静默。
- Decisions 选择具体消息，而不只判断最后一条是否值得回复。ignore 和 wait 都保持沉默。
- 回复后为同群同一用户保留 120 秒接续窗口；继续聊天无需反复 @，使用 5 秒固定窗口，仍由 Decisions 判断，成功回复后续期。
- 判断期间的新消息另开下一批；同群串行处理，上一轮未完成时下一批排队。
- 发送前核对目标群、消息身份和有效期。@、唤醒词、命令、停用或移除白名单可以终止旧任务。
- 判断阶段不调用工具；正文生成默认不继承第三方插件工具。

## 安装与启用

需要 Python 3.12+、可正常回复的 AstrBot，以及可调用 Decisions API 的 OpenAI API 密钥。已在 AstrBot 4.28.1 + OneBot v11 上实测普通回复及 Decisions 选消息发送；其他版本和平台不等于已实测。

在 AstrBot 插件管理中通过仓库地址安装：

```text
https://github.com/SpaceSyt/astrbot_plugin_self_initiated_reply
```

本 fork 保留原插件 ID，**不能与上游版本同时加载**。已有用户先备份配置及插件数据，再替换插件代码。

1. 给 AstrBot 进程配置 `OPENAI_API_KEY` 环境变量并重启。Docker、服务管理器或启动脚本必须向实际机器人进程传入该变量。
2. 在 AstrBot 插件配置中将 `decision_backend` 设为 `decisions`，填写 `decisions_interests`，例如“喜欢音乐和游戏，自然接朋友的话，不插入他人的交流，不必每条都回复”。
3. 开启插件、判断模型和消息触发；通过 `/selfreply add` 添加目标会话。如果宿主使用独立配置方案，也需将插件加入对应方案的插件集合。
4. 根据需要设置发送冷却和每日次数上限；这些控制独立于 Decisions 判断。

默认仍使用 chat 后端、空白名单；仅安装不会开始 Decisions 调用。新增 Decisions 字段位于 **AstrBot 插件配置页**，上游独立设置页不展示这些字段。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `decisions_base_url` | `https://api.openai.com/v1` | 必须支持 `/decisions`，普通聊天兼容接口不一定支持 |
| `decisions_api_key_env` | `OPENAI_API_KEY` | 密钥所在环境变量名，不是密钥本身 |
| `decisions_interests` | 空，需填写 | 角色兴趣与接话偏好 |
| `decisions_min_confidence` | 0.7 | 低于阈值保持沉默，需按实际场景校准 |
| `message_delay_sec` | 30 | 普通消息固定窗口；接续窗口为 5 秒 |
| `cooldown_sec` | 900 | 普通主动发送冷却，可按需要调整，例如 180 秒 |
| `max_daily_replies_per_session` | 5 | 每会话每日发送上限，0 表示不限 |

判断固定使用 gpt-6-luna，正文沿用 AstrBot 当前会话的主模型与人设。判断 Provider 和最小静默设置只影响 chat 后端。[Decisions 官方文档](https://developers.openai.com/api/docs/guides/decisions)

## 指令

| 指令 | 用途 |
| --- | --- |
| `/selfreply` | 帮助 |
| `/selfreply status` | 运行状态与最近判断 |
| `/selfreply add` | 添加当前会话 |
| `/selfreply remove` | 移出当前会话 |
| `/selfreply list` | 查看白名单 |
| `/selfreply check [内容]` | 强制检查，会生成并可能实际发送回复 |
| `/selfreply on` | 启用 |
| `/selfreply off` | 暂停 |
| `/selfreply debug` | 会话识别诊断 |

## 限制与数据流

Decisions 首版仅分析文本，最多保留 30 条候选、有效期 10 分钟。判断接口会接收近期文本、昵称、发送者标识和时间。示例均为合成消息。接续窗口仅在内存中保存，重启后重置；正常 @ 回复开启接续窗口目前仅适配 OneBot v11。

30 秒是消息合并窗口，不包含模型、网络和同群上一轮排队耗时。发送仍受免打扰、冷却、每日上限与目标有效性约束；高流量导致目标被挤出窗口时会放弃发送。API 失败或超时保持沉默，不自动重试。

Decisions 和正文模型调用分别产生费用。本插件记录 Decisions token 用量，不提供费用预算管理，也不自动接入其他预算插件。跨群长期记忆由其他插件提供，不属于本插件。

详细配置及回放见 [Decisions 接入说明](docs/DECISIONS_API.md)。

## 开发与验证

```shell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python scripts/replay_decisions.py
```

默认回放不联网、不连接聊天平台。加 `--live` 才调用真实 API，仍不发送聊天消息。自动化测试验证流程边界，不证明所有群聊中的接话效果。开发命令在完整 Git 仓库中执行；安装 ZIP 不包含开发工具。

## English

An experimental GPL-3.0 fork of Yejing for AstrBot. OpenAI Decisions selects ignore, wait, or a specific recent message; AstrBot generates the reply. Fixed 30-second batches avoid indefinitely postponed checks in busy groups. A two-minute per-user continuation window supports follow-up messages without repeated mentions. Decisions is opt-in; credentials come from the bot process environment.

## 许可证与来源

GPL-3.0，见 [LICENSE](LICENSE)。上游作者：chengzhi-c；Decisions 扩展维护：SpaceSyt。改动日期与范围见 [NOTICE](NOTICE) 和 [CHANGELOG](CHANGELOG.md)。本项目不宣称两阶段主动聊天架构或 Decisions API 应用是首创，也不代表上游或 OpenAI 官方项目。
