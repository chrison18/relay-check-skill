"""probe_extra.py — 掺水检测增强（配套 Claude-skills.md 的 S14～S17）。
S14 substitution  模型替换指纹：用"官方规则下各候选模型行为不同"的服务端探针，逐个排除冒充者（不依赖 thinking 回传）
S15 tokenizer     tokenizer 向量：同一组文本在各模型上的 token 数（差分法，抵消固定注入）
S16 onetoken      单 token 分布指纹：一词回答的经验分布，模型之间的 JSD
S17 tools         工具定义 / 工具结果完整性：工具名、描述、结果是否被中转改写或注入额外工具
"""
import json, math, os, random, re, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from probe_lib import *  # noqa: F401,F403

# ------------------------------------------------------------------ S14 候选模型 × 探针的官方预期（True = 应返回 400）
# 来源：官方文档（error-codes / model-migration / prompt-caching，本地缓存 2026-09-25）。换模型版本后必须重新核对。
CANDIDATES = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5", "claude-opus-5",
              "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5"]
_NEWGEN = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5", "claude-opus-5",
           "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5"}
SUB_PROBES = {
    # 名称: (请求参数, 应返回 400 的候选集合)
    "between_tools@high": ({"thinking": {"type": "between_tools"}, "output_config": {"effort": "high"}},
                           set(CANDIDATES) - {"claude-sonnet-5-5"}),
    "thinking=disabled": ({"thinking": {"type": "disabled"}},
                          {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5"}),
    "tool_choice=any": ({"tools": [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
                         "tool_choice": {"type": "any"}},
                        {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}),
    "temperature=0.5": ({"temperature": 0.5}, _NEWGEN),
    "effort=xhigh": ({"output_config": {"effort": "xhigh"}}, {"claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"}),
    "effort=low": ({"output_config": {"effort": "low"}}, {"claude-haiku-4-5"}),
    "max_tokens=64001": ({"max_tokens": 64001}, {"claude-haiku-4-5"}),
}
# 控制探针：对所有候选都必须 400。若返回 200，说明中转在清洗/改写参数，本矩阵不可靠
CONTROL_PROBES = {"effort=ultra(非法)": {"output_config": {"effort": "ultra"}},
                  "thinking.type=bogus(非法)": {"thinking": {"type": "bogus_type"}}}
CACHE_POINTS = [800, 1600, 3000, 6000]      # 可见前缀约多少 token 时探测是否缓存
CACHE_MIN_ALL = {"claude-opus-5-5": 512, "claude-sonnet-5-5": 512, "claude-fable-5-1": 512, "claude-fable-5": 512, "claude-opus-5": 512,
                 "claude-opus-4-8": 1024, "claude-sonnet-5": 1024, "claude-sonnet-4-6": 1024, "claude-opus-4-7": 2048,
                 "claude-opus-4-6": 4096, "claude-haiku-4-5": 4096}


def _probe(model, kw, tag):
    kw = dict(kw)
    mt = kw.pop("max_tokens", 64)
    r = ask(model, [{"role": "user", "content": "Say OK."}], mt, stream=False, retries=3, tag=tag, **kw)
    return r["status"], (etxt(r)[:200] if r["status"] != 200 else None)


def _cache_flag(model, target_tokens, rng):
    words = PROSE.lower().replace(",", "").replace(".", "").split()
    n = max(2, int(target_tokens / 1.75))
    nonce = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(10))
    body = [{"type": "text", "text": f"[{nonce}] " + " ".join(words[i % len(words)] for i in range(n)), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Reply with exactly: OK"}]
    r = ask(model, [{"role": "user", "content": body}], 5, stream=False, tag=f"S14_cache{target_tokens}", **LOW)
    if r["status"] != 200:
        return None, None
    u = r["msg"]["usage"]
    return ((u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) > 0), tin(u)


def s_substitution(a):
    M = a.model
    R = {"model": M, "probes": {}, "controls": {}, "cache": {}}
    for name, kw in CONTROL_PROBES.items():
        st, err = _probe(M, kw, f"S14_ctrl_{name}")
        R["controls"][name] = {"status": st, "error": err}
    reliable = all(v["status"] == 400 for v in R["controls"].values())
    for name, (kw, _) in SUB_PROBES.items():
        st, err = _probe(M, kw, f"S14_{name}")
        R["probes"][name] = {"status": st, "error": err}
    rng = random.Random(time.time())
    for pt in CACHE_POINTS:
        flag, tot = _cache_flag(M, pt, rng)
        R["cache"][str(pt)] = {"cached": flag, "reported_total": tot}
    # 逐个候选核对
    verdicts = {}
    for c in CANDIDATES:
        mism, judged = [], 0
        for name, (kw, must400) in SUB_PROBES.items():
            st = R["probes"][name]["status"]
            if st not in (200, 400):
                continue
            judged += 1
            if (st == 400) != (c in must400):
                mism.append(f"{name}: 观测 {st}，{c} 应为 {'400' if c in must400 else '200'}")
        mn = CACHE_MIN_ALL.get(c)
        for pt, v in R["cache"].items():
            if v["cached"] is None or v["reported_total"] is None or mn is None:
                continue
            visible = v["reported_total"] - 13
            if abs(visible - mn) < 150:          # 太靠近该候选的阈值，不用于判定
                continue
            judged += 1
            exp = visible >= mn
            if v["cached"] != exp:
                mism.append(f"cache@{visible}: 观测 {'缓存' if v['cached'] else '未缓存'}，{c} 的最小值 {mn} 应{'缓存' if exp else '不缓存'}")
        verdicts[c] = {"judged": judged, "mismatches": mism, "consistent": not mism}
    R["controls_ok"] = reliable
    R["candidates"] = verdicts
    R["consistent_candidates"] = [c for c, v in verdicts.items() if v["consistent"]]
    R["claimed_consistent"] = verdicts.get(M, {}).get("consistent")
    save("substitution", R)
    ctrl = ", ".join("%s: %s" % (k, v["status"]) for k, v in R["controls"].items())
    verdict = "可用" if reliable else "中转可能在清洗参数，矩阵不可靠"
    print(f"[S14] 控制探针（应全部 400）: {{{ctrl}}} → {verdict}", flush=True)
    print("[S14] 探针: " + ", ".join(f"{k}={v['status']}" for k, v in R["probes"].items()), flush=True)
    print("[S14] 缓存: " + ", ".join(f"≈{k}tok→{'缓存' if v['cached'] else ('未缓存' if v['cached'] is False else '失败')}({v['reported_total']})" for k, v in R["cache"].items()), flush=True)
    print(f"[S14] 与全部观测一致的候选: {R['consistent_candidates']}；声明模型 {M} {'一致' if R['claimed_consistent'] else '不一致'}", flush=True)
    for c, v in verdicts.items():
        if v["mismatches"]:
            print(f"[S14]   排除 {c}: {v['mismatches'][0]}" + (f"（另有 {len(v['mismatches']) - 1} 项）" if len(v["mismatches"]) > 1 else ""), flush=True)


# ------------------------------------------------------------------ S15 tokenizer 向量
TOK_TEXTS = {
    "en": PROSE[:400],
    "zh": "河流缓缓流过山谷，把山上的泥沙带到大海。春天水位上涨，沿岸的农民怀着希望与担忧注视着水流。孩子们在古老的石桥边打水漂。",
    "ja": "川はゆっくりと谷を流れ、山から海へ土砂を運ぶ。春になると水位が上がり、岸辺の農民たちは希望と不安を抱えて流れを見守る。",
    "code": "def merge(a, b):\n    i = j = 0\n    out = []\n    while i < len(a) and j < len(b):\n        if a[i] <= b[j]:\n            out.append(a[i]); i += 1\n        else:\n            out.append(b[j]); j += 1\n    return out + a[i:] + b[j:]\n",
    "digits": "31415926535897932384626433832795028841971693993751058209749445923078164062862089986280348253421170679",
    "emoji": "🌊🏔️🌾🐟🦢⛪🔔🚣‍♀️🌙✨🍎🧀🥖📚🕯️⚓🌫️🎣",
    "mixed": "Error 0x80070057: 参数错误 (param=“timeout_ms”) → retry #3 @ 2026-10-01T12:00:00Z; see https://example.org/a?b=1&c=2",
}


def tok_vector(model):
    def cnt(user):
        r = ask(model, [{"role": "user", "content": user}], 8, stream=False, tag=f"S15_{model}", **LOW)
        return tin(r["msg"]["usage"]) if r["status"] == 200 else None
    base = cnt("hi")
    if base is None:
        return None
    v = {}
    for k, t in TOK_TEXTS.items():
        x = cnt("hi" + NL + NL + t)
        v[k] = None if x is None else x - base
    return v


def s_tokenizer(a):
    models = [a.model] + [p for p in (a.peers.split(",") if a.peers else []) if p and p != a.model]
    with ThreadPoolExecutor(max(1, a.conc)) as ex:
        V = dict(zip(models, ex.map(tok_vector, models)))
    same = {p: (V.get(p) == V.get(a.model)) for p in models[1:] if V.get(p) and V.get(a.model)}
    R = {"vectors": V, "identical_to_claimed": same}
    if a.ref:
        ref = json.load(open(a.ref, encoding="utf-8")).get(a.model)
        if ref:
            R["ref"] = ref
            R["matches_ref"] = {k: (V[a.model] or {}).get(k) == ref.get(k) for k in ref}
    save("tokenizer", R)
    for m, v in V.items():
        print(f"[S15] {m}: {v}", flush=True)
    print(f"[S15] 与声明模型向量完全相同的对照模型: {same}" + (f"；与参考向量逐项比对: {R.get('matches_ref')}" if "matches_ref" in R else ""), flush=True)


# ------------------------------------------------------------------ S16 单 token 分布指纹
ONE_TOKEN_PROMPTS = {
    "num100": "Pick a random integer between 1 and 100. Reply with only the number, nothing else.",
    "color": "Name one random color. Reply with only one lowercase word, nothing else.",
    "letter": "Pick a random uppercase letter from A to Z. Reply with only the letter, nothing else.",
}


def _norm(t):
    t = (t or "").strip().lower().strip(".!\"'` ")
    return t.split()[0] if t else ""


def jsd(p, q):
    keys = set(p) | set(q)
    sp, sq = sum(p.values()) or 1, sum(q.values()) or 1
    P = {k: p.get(k, 0) / sp for k in keys}; Q = {k: q.get(k, 0) / sq for k in keys}
    Mx = {k: (P[k] + Q[k]) / 2 for k in keys}
    kl = lambda A: sum(A[k] * math.log2(A[k] / Mx[k]) for k in keys if A[k] > 0)
    return round((kl(P) + kl(Q)) / 2, 3)


def s_onetoken(a):
    models = [a.model] + [p for p in (a.peers.split(",") if a.peers else []) if p and p != a.model]
    jobs = [(m, k, i) for m in models for k in ONE_TOKEN_PROMPTS for i in range(a.n)]

    def run(j):
        m, k, i = j
        r = ask(m, [{"role": "user", "content": ONE_TOKEN_PROMPTS[k]}], 2000, stream=False, tag=f"S16_{m}_{k}", **LOW)
        return j, (_norm(text_of(r["msg"])) if r["status"] == 200 and (r["msg"].get("stop_reason") != "refusal") else None)
    with ThreadPoolExecutor(max(1, a.conc)) as ex:
        res = list(ex.map(run, jobs))
    D = {m: {k: Counter() for k in ONE_TOKEN_PROMPTS} for m in models}
    for (m, k, i), ans in res:
        if ans:
            D[m][k][ans] += 1
    R = {"n_per_prompt": a.n, "dist": {m: {k: dict(c.most_common()) for k, c in v.items()} for m, v in D.items()},
         "jsd_vs_claimed": {p: {k: jsd(D[a.model][k], D[p][k]) for k in ONE_TOKEN_PROMPTS} for p in models[1:]}}
    # 自身噪声底：把声明模型的样本对半分，计算两半之间的 JSD
    half = {}
    for k in ONE_TOKEN_PROMPTS:
        xs = [ans for (m, kk, i), ans in res if m == a.model and kk == k and ans]
        h1, h2 = Counter(xs[0::2]), Counter(xs[1::2])
        half[k] = jsd(h1, h2)
    R["self_split_jsd"] = half
    if a.ref:
        ref = json.load(open(a.ref, encoding="utf-8")).get(a.model)
        if ref:
            R["jsd_vs_ref"] = {k: jsd(D[a.model][k], Counter(ref.get(k, {}))) for k in ONE_TOKEN_PROMPTS}
    save("onetoken", R)
    for m in models:
        print(f"[S16] {m}: " + "；".join(f"{k} 前三 {D[m][k].most_common(3)}" for k in ONE_TOKEN_PROMPTS), flush=True)
    print(f"[S16] 声明模型对半拆分的 JSD（噪声底）: {half}", flush=True)
    print(f"[S16] 与声明模型的 JSD: {R['jsd_vs_claimed']}" + (f"；与参考分布的 JSD: {R.get('jsd_vs_ref')}" if "jsd_vs_ref" in R else ""), flush=True)


# ------------------------------------------------------------------ S17 工具定义 / 结果完整性
def s_tools(a):
    M = a.model
    rng = random.Random(time.time())
    c1, c2, c3 = wcode(rng), wcode(rng), wcode(rng)
    tool = {"name": "lookup_inventory_v7",
            "description": f"Look up stock for an item. Internal reference tag: {c1}. Always pass the item name in lowercase.",
            "input_schema": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]}}
    q = ("First, quote verbatim the full description of every tool you have access to, each on its own line prefixed with the tool name. "
         "Then call lookup_inventory_v7 for the item 'Lantern'.")
    r1 = ask(M, [{"role": "user", "content": q}], 3000, stream=False, tag="S17_call", tools=[tool], **LOW)
    R = {"status": r1["status"]}
    if r1["status"] != 200:
        R["error"] = etxt(r1); save("tools", R); print("[S17] 请求失败", R["error"]); return
    m = r1["msg"]
    t = text_of(m)
    uses = [b for b in m["content"] if b.get("type") == "tool_use"]
    names_in_text = set(re.findall(r"\b[a-z][a-z0-9_]{3,}\b", t))
    R["description_quoted"] = c1 in t
    R["tool_use_names"] = [u.get("name") for u in uses]
    R["tool_name_exact"] = bool(uses) and all(u.get("name") == "lookup_inventory_v7" for u in uses)
    R["tool_input"] = [u.get("input") for u in uses]
    R["other_tool_like_names_in_text"] = sorted(n for n in names_in_text if n.endswith(("_tool", "tool")) or n in ("bash", "web_search", "read", "write", "edit"))[:10]
    if uses:
        msgs = [{"role": "user", "content": q}, {"role": "assistant", "content": m["content"]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"],
                                              "content": f"stock=17; warehouse_note={c2}; batch={c3}"}]}]
        r2 = ask(M, msgs, 2000, stream=False, tag="S17_result", tools=[tool], **LOW)
        t2 = text_of(r2.get("msg"))
        R["result_status"] = r2["status"]
        R["result_echo"] = {"stock": "17" in t2, "note": c2 in t2, "batch": c3 in t2} if r2["status"] == 200 else None
        if r2["status"] == 200 and not all(R["result_echo"].values()):
            r3 = ask(M, msgs + [{"role": "assistant", "content": r2["msg"]["content"]},
                                {"role": "user", "content": "Repeat the exact tool result you received, verbatim."}], 1000, stream=False, tag="S17_result2", tools=[tool], **LOW)
            t3 = text_of(r3.get("msg"))
            R["result_echo_retry"] = {"note": c2 in t3, "batch": c3 in t3}
    save("tools", R)
    print(f"[S17] 描述原文复述={R['description_quoted']} 工具名精确={R['tool_name_exact']} {R['tool_use_names']} 参数={R['tool_input']} "
          f"工具结果回显={R.get('result_echo')} {('重试=' + str(R.get('result_echo_retry'))) if 'result_echo_retry' in R else ''} "
          f"文本中其他疑似工具名={R['other_tool_like_names_in_text']}", flush=True)
