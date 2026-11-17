# relay-check

Claude Code skill：AI API 中转站验真 + 测速，三件工具打包。

| 层 | 工具 | 回答的问题 | 成本 |
|---|---|---|---|
| 1 | `scripts/check-relay.py` | 上游是不是 Anthropic 官方行为（响应格式 / 提示缓存 / thinking 签名） | 几美分 |
| 2 | `scripts/speedtest.py` | TTFB / TPS / 出字速度，usage 灌水交叉核对 | 几美分 |
| 3 | `vendor/claude-detection/` | 型号鉴定、注入、偷换比例、渠道识别，100 分制证据评分 | $1 起 |

## 安装

```bash
git clone git@github.com:chrison18/relay-check-skill.git ~/.claude/skills/relay-check
```

## 使用

对话里说「检测中转站掺水 / 测速」或 `/relay-check` 自动触发；执行流程、成本表、
判读边界与密钥安全规则见 [SKILL.md](SKILL.md)。第三方来源与许可见 [SOURCE.md](SOURCE.md)。

所有脚本仅用 Python 3.8+ 标准库；密钥只从环境变量 / `.env` 读取，只发给用户指定的端点。