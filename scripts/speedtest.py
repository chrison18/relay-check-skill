#!/usr/bin/env python3
"""speedtest.py — 测 Claude 中转（或 OpenAI 兼容端点）的 TTFB / TPS / 总耗时。

    python3 speedtest.py --model claude-opus-5-5 --rounds 3
    python3 speedtest.py --base-url https://relay.example --key sk-... --model claude-opus-5-5

不填 --base-url / --key 时读 ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN（或
ANTHROPIC_API_KEY），也就是 Claude Code 正在用的配置。仅 Python 3.8+ 标准库；
密钥只发给你指定的端点，不写入任何输出文件。

指标（每轮一次流式请求）：
  TTFB        首个可见文本 token 的等待时间（思考模型先思考后出字，这项会偏大）
  TPS         usage.output_tokens / 生成窗口时长（含 thinking token，是计费口径）
  TPS-total   usage.output_tokens / 请求总时长（含排队与首字等待）
  chars/s     可见文本字符数 / 生成窗口时长（交叉核对：usage 被灌水时两者会背离）

退出码：0 全部轮次成功；1 部分失败；2 全部失败。
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PROMPT = ("Write a coherent explanation of how prompt caching reduces cost in LLM API usage, "
                  "about 350 words. No preamble, start directly with the explanation.")


def sse_events(response):
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except ValueError:
            pass


def post_stream(url, headers, body, timeout):
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    return urllib.request.urlopen(request, timeout=timeout)


def measure_anthropic(base, headers, args):
    body = {"model": args.model, "max_tokens": args.max_tokens, "stream": True,
            "messages": [{"role": "user", "content": args.prompt}]}
    if args.effort:
        body["output_config"] = {"effort": args.effort}
    url = base.rstrip("/") + "/v1/messages"
    first_any = first_text = last = None
    chars = 0
    out_tokens = in_tokens = None
    stop = None
    t0 = time.perf_counter()
    with post_stream(url, headers, body, args.timeout) as response:
        for event in sse_events(response):
            now = time.perf_counter()
            kind = event.get("type")
            if kind == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                in_tokens = usage.get("input_tokens")
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chars += len(delta.get("text") or "")
                    if first_text is None:
                        first_text = now
                if first_any is None:
                    first_any = now
                last = now
            elif kind == "message_delta":
                usage = event.get("usage") or {}
                if usage.get("output_tokens") is not None:
                    out_tokens = usage["output_tokens"]
                stop = (event.get("delta") or {}).get("stop_reason") or stop
    return finish(t0, first_any, first_text, last, chars, out_tokens, in_tokens, stop)


def measure_openai(base, headers, args):
    body = {"model": args.model, "max_tokens": args.max_tokens, "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": args.prompt}]}
    url = base.rstrip("/") + "/v1/chat/completions"
    first_any = first_text = last = None
    chars = 0
    out_tokens = in_tokens = None
    stop = None
    t0 = time.perf_counter()
    with post_stream(url, headers, body, args.timeout) as response:
        for event in sse_events(response):
            now = time.perf_counter()
            usage = event.get("usage")
            if isinstance(usage, dict):
                out_tokens = usage.get("completion_tokens") or out_tokens
                in_tokens = usage.get("prompt_tokens") or in_tokens
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    chars += len(text)
                    if first_text is None:
                        first_text = now
                if first_any is None:
                    first_any = now
                last = now
                stop = choice.get("finish_reason") or stop
    return finish(t0, first_any, first_text, last, chars, out_tokens, in_tokens, stop)


def finish(t0, first_any, first_text, last, chars, out_tokens, in_tokens, stop):
    end = time.perf_counter()
    start = first_text or first_any
    if start is None:
        raise RuntimeError("empty stream: no content deltas received")
    gen = max(last - start, 1e-6)
    total = end - t0
    return {
        "ok": True,
        "ttfb_s": round((first_text or first_any) - t0, 3),
        "total_s": round(total, 3),
        "gen_s": round(gen, 3),
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "tps": round(out_tokens / gen, 1) if out_tokens else None,
        "tps_total": round(out_tokens / total, 1) if out_tokens else None,
        "chars": chars,
        "chars_per_s": round(chars / gen, 1),
        "stop_reason": stop,
    }


def one_round(index, base, headers, args):
    measure = measure_anthropic if args.protocol == "anthropic" else measure_openai
    try:
        result = measure(base, headers, args)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        result = {"ok": False, "error": f"HTTP {error.code}: {detail}"}
    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as error:
        result = {"ok": False, "error": str(error)}
    result["round"] = index + 1
    return result


def summarize(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"median": round(statistics.median(values), 2),
            "mean": round(statistics.mean(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="测 Claude 中转的 TTFB / TPS / 总耗时（流式）")
    parser.add_argument("--base-url", default=os.environ.get("ANTHROPIC_BASE_URL"))
    parser.add_argument("--key", default=os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--protocol", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--bearer", action="store_true", help="用 Authorization: Bearer 代替 x-api-key")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--conc", type=int, default=1, help="并发数（默认串行；测并发吞吐时才调高）")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                        help="anthropic 协议的推理档位；测纯输出速度可用 low 减少思考")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", metavar="PATH", help="把逐轮结果写成 JSON（不含密钥）")
    args = parser.parse_args(argv)
    if not args.base_url or not args.key:
        parser.error("需要 --base-url 和 --key（或设置 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN）")

    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if args.bearer or args.protocol == "openai":
        headers["authorization"] = "Bearer " + args.key
    else:
        headers["x-api-key"] = args.key

    print(f"{args.base_url}  model={args.model}  protocol={args.protocol}  rounds={args.rounds} conc={args.conc}", flush=True)
    with ThreadPoolExecutor(max_workers=max(args.conc, 1)) as pool:
        results = list(pool.map(lambda i: one_round(i, args.base_url, headers, args), range(args.rounds)))

    print(f"{'轮':<4}{'TTFB(s)':<10}{'总耗时(s)':<11}{'输出tok':<9}{'TPS':<8}{'TPS-total':<11}{'chars/s':<9}结果")
    for r in results:
        if r["ok"]:
            print(f"{r['round']:<4}{r['ttfb_s']:<10}{r['total_s']:<11}{str(r['output_tokens']):<9}"
                  f"{str(r['tps']):<8}{str(r['tps_total']):<11}{r['chars_per_s']:<9}ok")
        else:
            print(f"{r['round']:<4}{'-':<10}{'-':<11}{'-':<9}{'-':<8}{'-':<11}{'-':<9}FAIL {r['error']}")

    good = [r for r in results if r["ok"]]
    summary = {key: summarize([r[key] for r in good]) for key in ("ttfb_s", "total_s", "tps", "tps_total", "chars_per_s")}
    if good:
        print("\n汇总（成功轮次）：")
        for key, stats in summary.items():
            if stats:
                print(f"  {key:<12} median={stats['median']}  mean={stats['mean']}  min={stats['min']}  max={stats['max']}")
        print("注意：TPS 按 usage.output_tokens 计（含 thinking token，计费口径）；chars/s 是可见文本速度。")
        print("      两者明显背离、或 TPS 异常高而 chars/s 很低时，怀疑 usage 灌水或思考被剥离。单次结果受排队/路由影响，多时段复测。")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"base_url": args.base_url, "model": args.model, "protocol": args.protocol,
                       "rounds": results, "summary": summary}, handle, ensure_ascii=False, indent=1)
        print(f"\n逐轮结果已写入 {args.json}")
    if not good:
        return 2
    return 0 if len(good) == args.rounds else 1


if __name__ == "__main__":
    sys.exit(main())