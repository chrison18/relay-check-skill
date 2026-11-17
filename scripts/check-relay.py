#!/usr/bin/env python3
"""Check whether a Claude relay is backed by Anthropic's own API.

    python3 check-relay.py --base-url https://relay.example --key sk-...

ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) are used
when the flags are left out. Python 3.8+ and the standard library only; the
key is sent to the base URL and nowhere else.

Three checks, each about something another model or a rewriting proxy cannot
easily fake:

  shape      the response is an Anthropic Messages response with usage
  cache      a long cached prefix is written once and read back on the next call
  signature  Anthropic signs thinking blocks and rejects a tampered signature
             when it is sent back; a genuine one is accepted (platform.claude.com/
             docs/en/build-with-claude/preserved-thinking: a tampered signature
             always returns a 400)

It costs a few cents: about 18,000 cached input tokens twice and four short calls.
Exit status: 0 when nothing failed, 1 when a check failed, 2 when a request
error stopped a check (that says nothing about the upstream).
Docs: https://corecode.live/docs/verify/
"""
import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

VERSION = "2023-06-01"
PASS, FAIL, WARN, SKIP, ERROR = "PASS", "FAIL", "WARN", "SKIP", "ERROR"

ZH = os.environ.get("LANG", "").lower().startswith("zh")


def t(zh, en):
    return zh if ZH else en


class Relay:
    def __init__(self, base_url, key, bearer=False, timeout=120):
        self.url = base_url.rstrip("/") + "/v1/messages"
        self.headers = {"content-type": "application/json", "anthropic-version": VERSION}
        if bearer:
            self.headers["authorization"] = "Bearer " + key
        else:
            self.headers["x-api-key"] = key
        self.timeout = timeout

    def post(self, body):
        """(status, parsed JSON or None, raw text)."""
        request = urllib.request.Request(self.url, json.dumps(body).encode(), self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read().decode("utf-8", "replace")
        try:
            return status, json.loads(raw), raw
        except ValueError:
            return status, None, raw


def error_text(data, raw):
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return str(data["error"].get("message") or data["error"].get("type"))
    return raw[:200]


def check_shape(relay, model):
    status, data, raw = relay.post({"model": model, "max_tokens": 16,
                                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}]})
    if status != 200 or not isinstance(data, dict):
        return ERROR, t(f"请求失败：HTTP {status} {error_text(data, raw)}", f"request failed: HTTP {status} {error_text(data, raw)}")
    usage = data.get("usage") or {}
    problems = []
    if data.get("type") != "message" or data.get("role") != "assistant":
        problems.append("type/role")
    if not str(data.get("id", "")).startswith("msg_"):
        problems.append(f"id={data.get('id')!r}")
    if not isinstance(usage.get("input_tokens"), int) or not isinstance(usage.get("output_tokens"), int):
        problems.append("usage")
    if not isinstance(data.get("content"), list):
        problems.append("content")
    if problems:
        return FAIL, t("不是 Anthropic Messages 格式：", "not an Anthropic Messages response: ") + ", ".join(problems)
    return PASS, t(f"Messages 格式正确，返回模型 {data.get('model')}，输入 {usage['input_tokens']} token",
                   f"Messages response, model {data.get('model')}, {usage['input_tokens']} input tokens")


def long_prefix():
    # About 18,000 tokens on Claude 5: above every Claude model's minimum cacheable length.
    # The nonce makes this run's prefix new, so the first call has to write it.
    nonce = secrets.token_hex(8)
    lines = [f"Reference line {i:04d} of run {nonce}: the relay check repeats this text to fill a cacheable prefix."
             for i in range(400)]
    return "You are a terse assistant. Answer in one word.\n" + "\n".join(lines)


def check_cache(relay, model):
    body = {"model": model, "max_tokens": 8,
            "system": [{"type": "text", "text": long_prefix(), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Reply with: ok"}]}
    usages = []
    for attempt in range(3):
        status, data, raw = relay.post(body)
        if status != 200 or not isinstance(data, dict):
            return ERROR, t(f"请求失败：HTTP {status} {error_text(data, raw)}", f"request failed: HTTP {status} {error_text(data, raw)}")
        usages.append(data.get("usage") or {})
        if attempt and usages[-1].get("cache_read_input_tokens"):
            break
        time.sleep(2)
    first, last = usages[0], usages[-1]
    written = first.get("cache_creation_input_tokens") or 0
    read = last.get("cache_read_input_tokens") or 0
    if not written and not read:
        return FAIL, t("没有任何缓存写入或读取：上游不支持 Anthropic 提示缓存，或用量是代理自己算的",
                       "no cache write or read at all: the upstream does not do Anthropic prompt caching, or the proxy computes usage itself")
    if not read:
        return WARN, t(f"写入了 {written} token 缓存，但 {len(usages) - 1} 次重试都没读到：可能每次落在不同账号上，缓存会经常失效",
                       f"wrote {written} cached tokens but read none in {len(usages) - 1} retries: requests may land on different accounts, so the cache keeps missing")
    if written and abs(read - written) > written * 0.05:
        return WARN, t(f"写入 {written}、读取 {read} token，两者对不上", f"wrote {written} but read {read} cached tokens; they should match")
    return PASS, t(f"首次写入 {written} token，第 {len(usages)} 次读取 {read} token", f"wrote {written} tokens, read {read} on call {len(usages)}")


TOOL = {"name": "get_weather", "description": "Current weather for a city.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}


# Adaptive thinking is what Claude 4.6 and later accept (and the 5 models use
# by default); Claude 4.7 and later reject the older `enabled` + budget_tokens
# with a 400, which Claude 4.5 and earlier still need. The first one a model
# accepts is used for the replays too.
THINKING = [{"type": "adaptive"}, {"type": "enabled", "budget_tokens": 1024}]


def check_signature(relay, model):
    question = {"role": "user", "content": "Think about which unit a traveller expects, then use the tool: what is the weather in Paris?"}
    for thinking_config in THINKING:
        base = {"model": model, "max_tokens": 2048, "thinking": thinking_config, "tools": [TOOL]}
        status, data, raw = relay.post({**base, "messages": [question]})
        if status != 400:
            break
    if status == 400:
        return SKIP, t(f"这个模型不接受思考参数：HTTP 400 {error_text(data, raw)}",
                       f"this model refused the thinking request: HTTP 400 {error_text(data, raw)}")
    if status != 200 or not isinstance(data, dict):
        return ERROR, t(f"请求失败：HTTP {status} {error_text(data, raw)}", f"request failed: HTTP {status} {error_text(data, raw)}")
    content = data.get("content") or []
    thinking = next((b for b in content if b.get("type") == "thinking"), None)
    tool_use = next((b for b in content if b.get("type") == "tool_use"), None)
    if not thinking or not tool_use:
        return SKIP, t("这次回复没有同时带思考块和工具调用（模型可以自己决定不思考），再跑一次",
                       "the reply had no thinking block with a tool call (the model may choose not to think); run it again")
    signature = thinking.get("signature") or ""
    if len(signature) < 32:
        return FAIL, t("思考块没有签名：不是 Anthropic 生成的", "the thinking block carries no signature: Anthropic did not produce it")

    def replay(sig):
        block = {**thinking, "signature": sig}
        assistant = {"role": "assistant", "content": [block, tool_use]}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use["id"], "content": "18°C, clear"}]}
        status, data, raw = relay.post({**base, "messages": [question, assistant, result]})
        return status, error_text(data, raw) if status != 200 else ""

    middle = len(signature) // 2
    forged = signature[:middle] + ("A" if signature[middle] != "A" else "B") + signature[middle + 1:]
    forged_status, forged_error = replay(forged)
    genuine_status, genuine_error = replay(signature)
    if forged_status == 200:
        return FAIL, t("篡改过的签名也被接受了：没有人在校验签名，上游不是 Anthropic",
                       "a tampered signature was accepted: nothing verifies it, so the upstream is not Anthropic")
    if genuine_status != 200:
        return WARN, t(f"篡改签名被拒（HTTP {forged_status}），但原样回传也失败：HTTP {genuine_status} {genuine_error}",
                       f"the tampered signature was rejected (HTTP {forged_status}) but the genuine one failed too: HTTP {genuine_status} {genuine_error}")
    return PASS, t(f"篡改签名被拒（HTTP {forged_status}），原样回传通过", f"tampered signature rejected (HTTP {forged_status}), genuine one accepted")


RANK = {PASS: 0, SKIP: 0, WARN: 1, ERROR: 2, FAIL: 3}
CHECKS = [("shape", check_shape), ("cache", check_cache), ("signature", check_signature)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=t("检测 Claude 中转是否直连 Anthropic 官方", "Check whether a Claude relay is backed by Anthropic"))
    parser.add_argument("--base-url", default=os.environ.get("ANTHROPIC_BASE_URL"))
    parser.add_argument("--key", default=os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--bearer", action="store_true", help=t("用 Authorization: Bearer 代替 x-api-key", "send Authorization: Bearer instead of x-api-key"))
    parser.add_argument("--only", choices=[name for name, _ in CHECKS], action="append")
    args = parser.parse_args(argv)
    if not args.base_url or not args.key:
        parser.error(t("需要 --base-url 和 --key", "--base-url and --key are required"))

    relay = Relay(args.base_url, args.key, args.bearer)
    print(f"{relay.url}  model={args.model}")
    worst = PASS
    for name, check in CHECKS:
        if args.only and name not in args.only:
            continue
        try:
            verdict, detail = check(relay, args.model)
        except (urllib.error.URLError, OSError) as error:
            verdict, detail = ERROR, t(f"连不上：{error}", f"cannot connect: {error}")
        print(f"[{verdict}] {name:<9} {detail}", flush=True)
        if RANK[verdict] > RANK[worst]:
            worst = verdict
    print(t("结论：", "Result: ") + {PASS: t("三项都像 Anthropic 官方", "all checks look like Anthropic"),
                                     WARN: t("有疑点，见上面的 WARN", "doubtful, see WARN above"),
                                     ERROR: t("检测没有完成，先解决上面的请求错误", "the check did not finish; fix the request error above"),
                                     FAIL: t("至少一项说明上游不是 Anthropic 官方，或用量不可信", "at least one check says the upstream is not Anthropic, or its usage is not real")}[worst])
    return {FAIL: 1, ERROR: 2}.get(worst, 0)


if __name__ == "__main__":
    sys.exit(main())
