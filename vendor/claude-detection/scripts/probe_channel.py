"""probe_channel.py — 渠道识别（配套 Claude-skills.md 的 S20）。

回答"这个 Claude 最可能来自哪类渠道"：Anthropic 官方、Bedrock、Vertex、Azure、Kiro、Claude Code 号池、各类 IDE 代理、聚合网关……
做法：从 raw.jsonl（含响应头）与 results.json 中提取多维证据，按 channel_signatures.json 的规则逐条匹配，
按渠道累计得分，给出前 3 名候选、命中与未命中的特征、置信等级，以及可能的渠道链（例如"网关 → Kiro → 上游"）。

置信规则（不靠单一证据）：
- 高：至少一条强特征命中，且至少两个独立维度命中；
- 中：有强特征但只有一个维度，或至少两个维度的弱特征；
- 低：只有单一维度的弱特征；
- 没有任何命中时写"未知"，**不能默认为官方**。
"""
import json, os, re, time
from collections import Counter
from probe_lib import *  # noqa: F401,F403

HERE = os.path.dirname(os.path.abspath(__file__))
SIG_PATH = os.path.join(HERE, "channel_signatures.json")
LAYER_ORDER = {"gateway": 0, "client": 1, "upstream": 2}
LAYER_NAME = {"gateway": "网关层", "client": "客户端 / 转换层", "upstream": "上游"}


def load_signatures(path=SIG_PATH):
    return json.load(open(path, encoding="utf-8"))


def read_records(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    for ln in open(path, encoding="utf-8"):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ 证据提取
def collect(records, R):
    """把原始记录和结果整理成按位置索引的证据。"""
    ok = [r for r in records if r.get("status") == 200 and isinstance(r.get("response"), dict)]
    msgs = [r["response"] for r in ok if r["response"].get("type") == "message" or "usage" in r["response"]]
    header_lines, n_with_headers = Counter(), 0
    for r in records:
        h = r.get("resp_headers")
        if h:
            n_with_headers += 1
            for k, v in h.items():
                header_lines[f"{k}: {v}"] += 1
    errors = [json.dumps(r.get("error"), ensure_ascii=False) for r in records if r.get("status") not in (200, None) and r.get("error")]
    usage_keys = [set((m.get("usage") or {}).keys()) for m in msgs]
    ids = [str(m.get("id", "")) for m in msgs if m.get("id")]
    models = [str(m.get("model")) for m in msgs if m.get("model")]
    models_list = []
    for r in records:
        if (r.get("request") or {}).get("GET") == "/v1/models" and isinstance(r.get("response"), dict):
            models_list = (r["response"].get("data") or [])
    # 模型复述出的前置内容
    self_texts = []
    bl = (R.get("baseline") or {}).get("injection") or {}
    if bl.get("dump_text"):
        self_texts.append(("S1 复述", bl["dump_text"]))
    ij = R.get("injection") or {}
    if (ij.get("dump2") or {}).get("text"):
        self_texts.append(("S18 前置内容自报", ij["dump2"]["text"]))
    for c in (ij.get("conditions") or {}).values():
        for t in c.get("replies") or []:
            self_texts.append(("S18 金丝雀 / 自报", t))
    for r in records:                                    # 结果文件缺失时，直接从原始记录里取复述类探针的回复
        if r.get("tag") in ("S1_dump", "S18_dump2") and isinstance(r.get("response"), dict) and not R:
            self_texts.append((r["tag"], text_of(r["response"])))
    sigs = []
    for m in msgs:
        for b in m.get("content") or []:
            if b.get("type") == "thinking":
                sigs.append((len(b.get("signature") or ""), len(b.get("thinking") or "")))
    return {"n_records": len(records), "n_ok": len(msgs), "n_with_headers": n_with_headers, "header_lines": header_lines,
            "errors": errors, "usage_keys": usage_keys, "ids": ids, "models": models, "models_list": models_list,
            "self_texts": self_texts, "sigs": sigs, "results": R}


def _snip(s, m, w=90):
    a = max(0, m.start() - 20)
    return _mask(("…" if a else "") + s[a:m.start() + w].replace("\n", " ").strip() + ("…" if m.start() + w < len(s) else ""))


def _mask(s):
    """证据里不保留分销商的分组名等可识别信息。"""
    s = re.sub(r"(under group )[^()\"]+", r"\1<group> ", s)
    return re.sub(r"(当前分组 ?)\S+", r"\1<group>", s)


def match_rule(rule, E):
    """返回 None（未命中）或 {"evidence": 原文片段, "count": 次数, "frac": 比例}。"""
    w, pat = rule["where"], re.compile(rule.get("pattern", "x^"), re.I) if rule.get("pattern") else None
    if w == "header":
        hits = [(ln, n) for ln, n in E["header_lines"].items() if pat.search(ln)]
        if hits:
            return {"evidence": hits[0][0][:160], "count": sum(n for _, n in hits)}
    elif w == "error":
        for e in E["errors"]:
            m = pat.search(e)
            if m:
                return {"evidence": _snip(e, m), "count": sum(1 for x in E["errors"] if pat.search(x))}
    elif w == "selfreport":
        for src, t in E["self_texts"]:
            m = pat.search(t)
            if m:
                return {"evidence": f"{src}：" + _snip(t, m, 120), "count": sum(1 for _, x in E["self_texts"] if pat.search(x))}
    elif w in ("usage_key", "usage_missing"):
        n = len(E["usage_keys"])
        if not n:
            return None
        has = sum(1 for ks in E["usage_keys"] if any(pat.search(k) for k in ks))
        cnt = has if w == "usage_key" else n - has
        if cnt and cnt / n >= rule.get("min_frac", 0):
            what = "出现" if w == "usage_key" else "缺少"
            sample = next((k for ks in E["usage_keys"] for k in ks if pat.search(k)), rule["pattern"])
            return {"evidence": f"{cnt}/{n} 条响应的 usage {what} {sample}", "count": cnt, "frac": round(cnt / n, 3)}
    elif w == "msg_id":
        n = len(E["ids"])
        cnt = sum(1 for i in E["ids"] if pat.search(i))
        if n and cnt and cnt / n >= rule.get("min_frac", 0):
            ex = next(i for i in E["ids"] if pat.search(i))
            return {"evidence": f"{cnt}/{n} 条响应的 id 形如 {ex[:12]}…", "count": cnt, "frac": round(cnt / n, 3)}
    elif w == "model_field":
        pool = E["models"] + [str(x.get("id")) for x in E["models_list"]] + E["errors"]
        for s in pool:
            m = pat.search(s)
            if m:
                return {"evidence": _snip(s, m, 60), "count": sum(1 for x in pool if pat.search(x))}
    elif w == "special":
        return special(rule["check"], E)
    return None


def special(check, E):
    if check == "models_list_official_shape":
        L = E["models_list"]
        if L and all(isinstance(x, dict) and {"created_at", "display_name", "id", "type"} <= set(x) and x.get("type") == "model" for x in L):
            return {"evidence": f"/v1/models 的 {len(L)} 个条目都是官方字段（created_at、display_name、id、type=model）", "count": len(L)}
    elif check == "fixed_signature_length":
        S = E["sigs"]
        if len(S) >= 5:
            ln, n = Counter(s for s, _ in S).most_common(1)[0]
            spread = len({t for s, t in S if s == ln})
            if n / len(S) >= 0.4 and spread >= 3:
                return {"evidence": f"{n}/{len(S)} 个 thinking 签名长度都是 {ln}，而对应的思考长度各不相同", "count": n, "frac": round(n / len(S), 3)}
    elif check == "signature_not_validated":
        th = E["results"].get("thinking") or {}
        if th.get("verdict") == "SIGNATURE_NOT_VALIDATED":
            return {"evidence": f"S3：伪造签名得到 {th.get('garbage_signature', {}).get('status')}，篡改签名得到 {th.get('tampered_signature', {}).get('status')}", "count": 1}
    return None


# ------------------------------------------------------------------ 打分
def confidence(hits):
    dims = {h["dim"] for h in hits if h["weight"] >= 1}       # 权重 < 1 的特征（如 msg_01 格式的 ID）太容易伪造，不算独立维度
    strong = [h for h in hits if h["strength"] == "strong"]
    if strong and len(dims) >= 2:
        return "高"
    if strong or len(dims) >= 2:
        return "中"
    return "低" if hits else "未知"


def analyze(records, R, sig=None):
    sig = sig or load_signatures()
    E = collect(records, R)
    C = {k: {"channel": k, "name": v["name"], "layer": v["layer"], "hits": [], "missed": []} for k, v in sig["channels"].items()}
    for rule in sig["rules"]:
        h = match_rule(rule, E)
        rec = {"rule": rule["id"], "dim": rule["dim"], "strength": rule["strength"], "weight": rule["weight"], "source": rule["source"]}
        if h:
            C[rule["channel"]]["hits"].append({**rec, **h})
        else:
            C[rule["channel"]]["missed"].append(rule["id"])
    # 额外 token 量与注入的系统提示互相印证：给"系统提示类"命中的渠道加一个独立维度
    est = ((R.get("baseline") or {}).get("injection") or {}).get("injected_tokens_est")
    if isinstance(est, (int, float)) and est >= 200:
        for c in C.values():
            if any(h["dim"] == "prompt" for h in c["hits"]):
                c["hits"].append({"rule": "corroborate.extra_tokens", "dim": "tokens", "strength": "weak", "weight": 2,
                                  "source": "S1：每个请求额外约 %d token，与注入系统提示的存在相互印证" % est,
                                  "evidence": f"S1 估计每请求额外 {est} token", "count": 1})
    for c in C.values():
        c["score"] = round(sum(h["weight"] for h in c["hits"]), 1)
        c["confidence"] = confidence(c["hits"])
        c["dims"] = sorted({h["dim"] for h in c["hits"]})
    hit = sorted([c for c in C.values() if c["hits"]], key=lambda c: (-c["score"], c["channel"]))
    chain = [c for c in hit if c["confidence"] in ("高", "中")]
    if any(c["layer"] == "client" and c["channel"] != "converter" and c["confidence"] == "高" for c in chain):
        chain = [c for c in chain if c["channel"] != "converter"]      # 已识别出具体的转换产品时，通用转换层特征只作佐证
    chain = sorted(chain, key=lambda c: (LAYER_ORDER[c["layer"]], -c["score"]))
    # usage 形态分布：同一端点混用多条线路时，不同形态会同时出现
    n = len(E["usage_keys"])
    shapes = Counter()
    for ks in E["usage_keys"]:
        shapes["credit 计费字段" if any(k.startswith("credit_") for k in ks) else
               ("官方形态（含 service_tier）" if "service_tier" in ks else "其他")] += 1
        if "output_tokens_details" not in ks:
            shapes["缺少 output_tokens_details"] += 1
    mixed = n > 0 and sum(1 for k in ("credit 计费字段", "官方形态（含 service_tier）", "其他") if shapes[k]) >= 2
    notes = []
    if not E["n_with_headers"]:
        notes.append("原始记录里没有响应头（旧版本日志或请求全部失败），网络层证据缺失")
    if mixed:
        notes.append("usage 同时出现多种形态，端点可能把请求分配到不同线路（混合路由）")
    return {"top": [_brief(c) for c in hit[:3]], "chain": [_brief(c) for c in chain], "all_hits": [_brief(c) for c in hit],
            "unknown": not hit, "usage_shapes": {k: f"{v}/{n}" for k, v in shapes.items()}, "mixed_routing": mixed,
            "evidence_base": {"records": E["n_records"], "ok_messages": E["n_ok"], "records_with_headers": E["n_with_headers"],
                              "errors": len(E["errors"]), "self_reports": len(E["self_texts"]), "models_list": len(E["models_list"])},
            "notes": notes, "signatures_version": sig["_meta"]["version"]}


def _brief(c):
    return {"channel": c["channel"], "name": c["name"], "layer": c["layer"], "score": c["score"], "confidence": c["confidence"],
            "dims": c["dims"], "hits": c["hits"], "missed": c["missed"]}


def chain_text(A):
    if A["unknown"]:
        return "未知：没有命中任何渠道特征（不能据此认定为官方）"
    if not A["chain"]:
        return "未能确定：只有低置信的零星特征"
    return " → ".join(f"{c['name']}（{c['confidence']}）" for c in A["chain"])


# ------------------------------------------------------------------ 子命令
def probes(model):
    """4 个低成本主动探针：正常请求、流式请求、不存在的模型、模型列表。用来收集响应头、错误体格式和模型 ID 风格。"""
    ask(model, [{"role": "user", "content": "hi"}], 8, stream=False, tag="CH_hi", **LOW)
    ask(model, [{"role": "user", "content": "hi"}], 8, stream=True, tag="CH_stream", **LOW)
    ask("claude-nonexistent-model-0", [{"role": "user", "content": "hi"}], 8, stream=False, tag="CH_badmodel", retries=1)
    get("/v1/models", tag="CH_models")


def s_channel(a):
    rp = getattr(a, "replay", "")
    if rp:
        raw = rp if rp.endswith(".jsonl") else os.path.join(rp, "raw.jsonl")
        rj = os.path.join(os.path.dirname(raw), "results.json")
        R = json.load(open(rj, encoding="utf-8")) if os.path.exists(rj) else {}
        print(f"[S20] 重放 {raw}（{'含' if R else '不含'} results.json），不发请求", flush=True)
    else:
        if BASE and not getattr(a, "no_probe", False):
            probes(a.model)
        raw, R = os.path.join(OUT, "raw.jsonl"), results()
    A = analyze(read_records(raw), R)
    A["source"] = "replay" if rp else "live"
    A["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save("channel", A)
    print(f"[S20] 证据基础：{A['evidence_base']}", flush=True)
    print(f"[S20] 渠道链：{chain_text(A)}", flush=True)
    for c in A["top"]:
        print(f"[S20]   {c['name']}  得分 {c['score']}  置信 {c['confidence']}  维度 {c['dims']}", flush=True)
        for h in c["hits"]:
            print(f"[S20]     {'强' if h['strength'] == 'strong' else '弱'} {h['rule']}：{h['evidence']}", flush=True)
    if A["usage_shapes"]:
        print(f"[S20] usage 形态：{A['usage_shapes']}", flush=True)
    for n in A["notes"]:
        print(f"[S20] 注意：{n}", flush=True)
    print("[S20] 渠道识别只代表本次测试时段与该 Key / 分组；特征库会过期，见 channel_signatures.json 的核对日期", flush=True)
