# Decisions 兴趣接话

使用 OpenAI `POST /v1/decisions`、`gpt-6-luna` 判断是否参与聊天，选中消息后由 AstrBot 主 Agent 生成正文。判断阶段不调用 AstrBot 模型、记忆钩子或工具。

## 连续聊天

普通主动回复默认使用 30 秒固定窗口，从首条待处理消息开始计时，后续消息合并但不重置计时，也不等待群聊静默。到点对当前快照调用 Decisions，忽略或等待都结束本批；只有新的消息才开启下一批。判断或生成期间收到的新消息另开窗口，同群仍串行处理，上一轮未结束时下一批需等它释放。发送冷却在判断之后、生成之前及发送之前检查，故到点判断不保证发送。

Decisions 模式下，成功主动回复后为被回复的用户保留 120 秒接续窗口。QQ 的正常 @ 或唤醒词 LLM 回复在适配器确认发送后也开启窗口，并将问答加入判断上下文。窗口只在当前群内生效，不持久化；重启后需重新开始对话。

同一用户在窗口内继续发言时，等待 5 秒合并消息，再让 Decisions 判断是否自然延续刚才的对话。接续消息可绕过主动回复冷却，但仍受白名单、免打扰、每日次数上限和发送前有效性检查约束。窗口内仅该用户的新消息可成为接续目标，无关话题可被忽略；发送成功后窗口续期。其他用户和窗口过期后的消息沿用普通主动回复节奏。

## 独立回放

在仓库目录使用 Python 3.12 或更新版本：

```powershell
uv venv --python 3.12
uv pip install --python .venv\Scripts\python.exe httpx pytest pytest-asyncio ruff
.venv\Scripts\python.exe scripts\replay_decisions.py
```

默认使用 `examples/decisions_replay.json` 的合成聊天和录制响应，不联网、不导入 AstrBot、不发送消息，也不接触生产记忆。结果包含 `action`、`target_id`、`confidence`、`usage`、`elapsed_sec` 和 `reason_code`。

可指定自己的聊天片段和录制响应：

```powershell
.venv\Scripts\python.exe scripts\replay_decisions.py examples\decisions_replay.json --response response.json
```

`--live` 使用当前进程环境中的 `OPENAI_API_KEY` 发起一次真实 API 请求；`OPENAI_BASE_URL` 可指定支持 Decisions 的 HTTPS 基础地址，默认 `https://api.openai.com/v1`。不要将密钥写入示例、命令参数或提交到仓库。

```powershell
.venv\Scripts\python.exe scripts\replay_decisions.py --live --timeout 15
```

即使使用 `--live`，此工具也只判断输入片段，不连接聊天平台。调用失败返回非零退出码；错误结果不包含密钥、原始响应正文或请求头。

## 插件配置

扩展默认仍使用原来的 `chat` 判断方式。若加载此副本，需要在 AstrBot 插件配置中选择 `decisions`；新增字段位于宿主配置页，原插件自带独立设置页不展示这些字段。

| 字段 | 值 / 行为 |
| --- | --- |
| `decision_backend` | `decisions` 启用 Decisions；`chat` 使用原判断方式 |
| `decisions_base_url` | 默认 `https://api.openai.com/v1`，客户端追加 `/decisions` |
| `decisions_api_key_env` | 默认 `OPENAI_API_KEY`，填写环境变量名，不是密钥 |
| `decisions_interests` | 必填，最多 4000 字符；填写角色兴趣与接话偏好 |
| `decisions_min_confidence` | 默认 0.7，低于阈值不回应；此起点需用实际片段校准 |
| `decision_timeout_sec` | 复用原判断超时；失败时保持沉默，不自动重试 |

仍须开启插件和判断模型，并配置会话白名单。`judge_provider_id`、普通判断提示词、温度及判断识图设置只用于原 `chat` 判断链；Decisions 首版只分析文本，正文生成仍沿用宿主人设。缺少密钥或兴趣设置会记录跳过原因，不回退为随机回复。

为避免误把观察状态当接入授权，独立回放和把代码放在项目目录都不会安装或启用插件。不要在生产插件目录内执行离线生成预览：宿主生成钩子可能写记忆或发送消息。

## 消息选择与节奏

- 候选最多保留 30 条普通消息，最长 10 分钟；事件引用仅存在内存中，不写入历史文件。API 使用本轮 `m0`、`m1` 等编号，本地映射至完整会话和真实消息 ID（包括负数）。
- 判断窗口包含近期背景消息；每条发送至判断接口的正文最多 600 字符。选择范围严格限制为同一会话的未处理候选。
- `ignore`、`wait` 均结束本轮，等待新消息或原有巡检；不会因 `wait` 自动紧密重试。
- 固定窗口到点判断，不等待群聊静默；保留发送冷却、免打扰及每日次数上限。同群前一轮尚未完成时，下一批串行排队。
- 正常 @ /唤醒调用清空旧候选；选中目标在判断、生成、装饰和发送阶段复查，避免回复旧消息时引用或 @ 最新发送者。
- 是否引用和 @ 仍服从 `quote_mode`、`mention_mode`。`quote_mode=model` 时，Decisions 选中的目标建议引用。
- 请求记录 API token 用量，不接入 `luna_budget`，也不实施费用额度限制。频率、超时和重复发送控制独立保留。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
```

测试覆盖 API 响应校验、拒绝与超时、离线零网络、较早消息选择、跨群隔离、负数 ID、生成期间新消息到达以及正常唤醒后的取消。模拟测试不代表真实群聊效果；真实 API 连通也不代表生产钩子已经验证。

协议来源：[OpenAI Decisions API](https://developers.openai.com/api/reference/resources/decisions/methods/create)。
