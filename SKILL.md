---
name: relay-check
description: AI API 中转站验真与测速。当用户要求检测中转站是否掺水、掉包、降智、注入、是否满血真 Opus/Sonnet，或要测中转的 TPS、首字延迟、出字速度时使用。打包三件工具：check-relay.py 协议验真（几美分）、Claude-Detection 深度检测（型号/注入/偷换比例，$1 起）、speedtest.py 流式测速（TTFB/TPS）。
---

# Relay Check — 中转站验真 + 测速

三件工具，由浅入深。所有脚本仅用 Python 3.8+ 标准库，密钥只发给用户指定的端点。
来源与许可见 [SOURCE.md](SOURCE.md)。本目录下称 `$SKILL`（即本 SKILL.md 所在目录）。

## 开始之前（必须）

1. **向用户确认**：端点地址、要测的模型名、预算上限。测当前 Claude Code 正在用的网关时，
   可直接复用环境变量 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`，模型名用
   `ANTHROPIC_DEFAULT_OPUS_MODEL` 等别名变量的实际值（网关可能不认裸 `claude-*` 名）。
2. **密钥只放环境变量或 `.env`**，绝不写进对话、命令参数或报告：
   ```bash
   export BASE="$ANTHROPIC_BASE_URL" K="$ANTHROPIC_AUTH_TOKEN" AUTH=both
   ```
   用户提供的第三方 Key 也一样走环境变量。若 Key 已出现在对话里，报告末尾提醒更换。
3. **先报成本再执行**：
   | 工具 | 成本（官方价目） | 耗时 |
   |---|---|---|
   | check-relay.py | 几美分 | ~1 分钟 |
   | speedtest.py | 每轮约 1K 输出 token，3 轮几美分 | 1～3 分钟 |
   | claude-detection quick | $1～2（中转按倍率） | 10～30 分钟 |
   | claude-detection standard | $8～15 | ~1 小时 |
   | claude-detection deep | $30～60 | 更久 |
4. 默认串行/低并发（`--conc 1` 或 `2`）；高并发会让中转成批 5xx，造成无法判定。

## 第一层：协议验真（先跑这个）

```bash
python3 "$SKILL/scripts/check-relay.py" --model <模型名> [--bearer]
```

三项硬信号：Messages 响应格式、提示缓存写入/读回、thinking 签名篡改必被 400 拒绝。
不填 `--base-url/--key` 时自动读 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`。
退出码：0 通过 / 1 有 FAIL / 2 请求错误（ERROR 不说明真假，先解决连通或客户端限制）。

**判读边界**：三项 PASS 只证明上游行为与 Anthropic 官方一致，**证明不了是 Opus 这一档**
（Sonnet 的签名同样合法）。型号交给第二层。

## 第二层：测速

```bash
python3 "$SKILL/scripts/speedtest.py" --model <模型名> --rounds 3 [--bearer] [--effort low] [--json out.json]
```

输出每轮 TTFB / 总耗时 / TPS（usage 口径，含 thinking）/ TPS-total / chars/s（可见文本），
并给 median/mean/min/max 汇总。OpenAI 兼容端点加 `--protocol openai`。

判读：
- **TPS 慢 ≠ 掺水**：拼车排队、逆向通道、路由漂移都会慢；不同时段各测几次再下结论。
- **TPS 很高但 chars/s 很低**：怀疑 usage 灌水（虚报 output_tokens）或思考被剥离。
- 怀疑"降智"（模型没换但推理档位被调低）：用第三层的 `effort` 探针，或对比官方同档基线。

## 第三层：深度检测（Claude-Detection）

完整执行规则、判读措辞、报告模板在 [vendor/claude-detection/SKILL.md](vendor/claude-detection/SKILL.md)
和 [vendor/claude-detection/reference/MANUAL.md](vendor/claude-detection/reference/MANUAL.md)，
**执行前必须先读这两份**，此处只列入口。所有命令在 `vendor/claude-detection/` 目录下运行：

```bash
cd "$SKILL/vendor/claude-detection"
export OUT=./probe_out/<run-name>        # 每次检测新目录
python3 scripts/claude_probe.py run --tier quick --model <模型名>     # 一键执行并评分
python3 scripts/claude_probe.py status                                 # 查看进度
```

- 单步冒烟（便宜）：`channel`（渠道识别）、`params`（参数指纹）、`baseline`（可见注入）。
- 长任务放后台并重定向到日志文件（`> run.log 2>&1`），**不要接 `| tail` 之类管道**；
  过程中读日志或 `status` 向用户汇报进度。
- 结果在 `$OUT/results.json`、`$OUT/SCORE.md`（100 分制证据评分 + 结论画像），
  全部请求响应在 `$OUT/raw.jsonl`（含端点与完整内容，不要公开）。
- 关键判读（详见其 SKILL.md）：READ 才是强证据，普通 DROP 不算造假；签名缺失不等于非官方；
  5xx/超时/空流一律"无法判定"不算通过；结论只适用于测试时段与该 Key/分组；
  禁用"100% 真""绝对 0 注入""保证满血"这类措辞。
- 规则表会随官方模型更新过期：使用前按其 SKILL.md「维护」节核对官方文档。

## 离线自检（不花 API 额度）

```bash
cd "$SKILL/vendor/claude-detection" && python3 scripts/selfcheck.py
```

检测力阳性对照（需要真实上游，消耗额度，先报成本）：
`UPSTREAM=<端点> UPSTREAM_KEY=<key> python3 scripts/positive_control.py [模式...]`，
用本地作弊中转 `fake_relay.py` 模拟注入/偷换/剥 thinking 等 10 种手法，验证探针能抓到。
只用于测试检测工具本身。

**坑：桌面应用自带的本地路由**（如 magpie/Orca 的 127.0.0.1 端口）可能只放行自己进程树内的
连接，孤儿进程会被 403（"must come from the desktop app or a paired device"）。
`fake_relay.py` 必须在**同一条命令内**启动、使用并 `kill $PID` 收尾；命令结束后残留的
后台进程会被系统收养，之后的请求全部被拒，且端口占用会让新实例启动失败、请求继续打到孤儿进程上。

## 报告

- 分层给结论：① 上游是否 Anthropic 行为一致（第一层）② 型号证据（第三层）③ 速度（第二层），
  各自附证据与边界，不合并成一个"真/假"二元结论。
- 单次检测只是一次抽样；中转可分时段/分流量掺水，建议定期复测。
- 报告与日志里不出现完整密钥。