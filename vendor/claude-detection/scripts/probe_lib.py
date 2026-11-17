"""probe_lib.py — claude_probe.py 的公共库：HTTP（流式/非流式/重试）、原始日志、规则表、题库、统计。
仅用 Python 标准库。环境变量：BASE、K、AUTH(both|xapikey|bearer)、OUT；也可写在当前目录的 .env 文件里（或用 ENV_FILE 指定路径）。
密钥只从环境变量 / .env 读取，不写入日志；响应头写入日志前会脱敏。
"""
import base64, hashlib, json, math, os, random, re, sys, threading, time
import urllib.error, urllib.request

for _s in (sys.stdout, sys.stderr):                   # 中文 Windows 控制台默认 GBK：统一输出 UTF-8，无法编码的字符替换掉
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_env_file():
    """读取 .env（KEY=VALUE，每行一个，# 开头为注释）。已存在的环境变量优先，不会被覆盖。"""
    p = os.environ.get("ENV_FILE") or (".env" if os.path.exists(".env") else None)
    if not p or not os.path.exists(p):
        return
    for ln in open(p, encoding="utf-8-sig"):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


_load_env_file()
BASE = os.environ.get("BASE", "").rstrip("/")
KEY = os.environ.get("K", "")
AUTH = os.environ.get("AUTH", "both")
OUT = os.environ.get("OUT", "probe_out")
NL = "\n"
RETRY = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529, 597, 598, 599}
_lock = threading.Lock()

# ------------------------------------------------------------------ 规则表（来源见 Claude-skills.md 第 9 节）
# 官方文档：最小可缓存前缀（claude-api skill shared/prompt-caching.md，缓存日期 2026-09-25）
CACHE_MIN = {"claude-opus-5-5": 512, "claude-opus-5": 512, "claude-fable-5": 512, "claude-fable-5-1": 512, "claude-sonnet-5-5": 512,
             "claude-opus-4-8": 1024, "claude-sonnet-5": 1024, "claude-sonnet-4-6": 1024, "claude-sonnet-4-5": 1024,
             "claude-opus-4-7": 2048, "claude-opus-4-6": 4096, "claude-opus-4-5": 4096, "claude-haiku-4-5": 4096}
# 官方框架开销参考值："hi"（单条 user、无 system）的 input token。
# 只收录在零注入端点上直接测得的值（两个独立端点、共 14 次一致）；其他模型没有直接测量，留空，
# 有官方 Key 时请用官方端点的实测值补充。
F_HI_REF = {"claude-opus-5-5": 10}
# thinking 读取规则：READ_MAP[产生者] = (能读取其 block 的接收方集合 或 "ALL", 证据来源)
# DOC = 官方文档明确规定（preserved-thinking#switching-models，2026-10-01 核对）；OBS = 实测，换时间或端点后要重新核对
# 注意：Sonnet 5.5 的 block 还与产生它的账号绑定，换账号会被静默丢弃（号池中转上同模型自读也会偶发 DROP），
# 所以只有 READ 是强证据；DROP 只有在 input_transformations 给出 model_binding_mismatch 时才是强证据。
READ_MAP = {
    "claude-opus-5-5": ({"claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"}, "DOC"),
    "claude-sonnet-5-5": ({"claude-sonnet-5-5", "claude-opus-5-5"}, "DOC"),
    "claude-fable-5-1": ({"claude-fable-5-1", "claude-mythos-5-1"}, "DOC"),
    "claude-fable-5": ({"claude-fable-5", "claude-fable-5-1", "claude-mythos-5-1"}, "OBS"),
    "claude-opus-5": ({"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1", "claude-mythos-5-1"}, "OBS"),
    "claude-opus-4-8": ("ALL", "OBS"),
    "claude-sonnet-5": ("ALL", "OBS"),
}
# 官方：Sonnet 5.5 的 block 只在产生它的账号（或关联账号）内有效，换账号回传会被静默丢弃
ACCOUNT_BOUND = {"claude-sonnet-5-5"}
BINDING_BETA = {"anthropic-beta": "thinking-binding-controls-2026-08-01"}   # 官方：响应的 input_transformations 列出被丢弃的 block 与原因
BIND_EXPECT = {"claude-opus-5-5": "DROP", "claude-fable-5-1": "DROP", "claude-sonnet-5-5": "DROP", "claude-opus-4-8": "READ"}
NEW_GEN = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5"}
# 官方应返回 400 的参数组合（官方文档 error-codes.md / model-migration.md）
PARAM_EXPECT_400 = {
    "temperature=0.5": NEW_GEN,
    "thinking=disabled": {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5"},
    "budget_tokens": NEW_GEN,
    "tool_choice=any": {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"},
    "prefill": NEW_GEN | {"claude-opus-4-6", "claude-sonnet-4-6"},
    "effort=xhigh": {"claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-5", "claude-sonnet-4-5"},
    "effort=ultra": "ALL",
    "max_tokens=128001": NEW_GEN | {"claude-opus-4-6", "claude-sonnet-4-6"},
    "between_tools@high": "ALL_EXCEPT_SONNET_5_5",
}


def expect_400(probe, model):
    s = PARAM_EXPECT_400[probe]
    if s == "ALL": return True
    if s == "ALL_EXCEPT_SONNET_5_5": return model != "claude-sonnet-5-5"
    return model in s


# ------------------------------------------------------------------ HTTP
def headers(extra=None):
    h = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if AUTH in ("both", "xapikey"): h["x-api-key"] = KEY
    if AUTH in ("both", "bearer"): h["Authorization"] = "Bearer " + KEY
    h.update(extra or {})
    return h


def _short(o, n=2500):
    if isinstance(o, str):
        return o if len(o) <= n else f"<{len(o)} chars sha256={hashlib.sha256(o.encode()).hexdigest()[:16]} head={o[:100]!r}>"
    if isinstance(o, list): return [_short(x, n) for x in o]
    if isinstance(o, dict): return {k: _short(v, n) for k, v in o.items()}
    return o


SECRET_HEADERS = {"authorization", "x-api-key", "proxy-authorization", "cookie", "set-cookie", "x-goog-api-key", "api-key"}


def redact_headers(h):
    """响应头脱敏：键统一小写；敏感头只保留"存在"这一事实；任何值里出现 Key 都打码。"""
    out = {}
    for k, v in (h or {}).items():
        k2 = str(k).lower()
        v = str(v)
        if k2 in SECRET_HEADERS:
            v = "<redacted>"
        elif KEY and len(KEY) >= 8 and KEY in v:
            v = v.replace(KEY, "<redacted-key>")
        out[k2] = v[:300]
    return out


def log(**rec):
    """追加一条原始记录到 raw.jsonl。写成纯 ASCII（中文转义），任何编码的读取方式都不会报错。"""
    os.makedirs(OUT, exist_ok=True)
    with _lock, open(os.path.join(OUT, "raw.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(_short({"ts": round(time.time(), 2), "base": BASE, **rec}), ensure_ascii=True) + NL)


def timeline(ev, total):
    data_t = [t for k, t in ev if k != "ping"]
    gaps = [b - a for a, b in zip(data_t, data_t[1:])]
    return {"n_events": len(ev), "n_ping": sum(1 for k, _ in ev if k == "ping"),
            "has_message_stop": any(k == "message_stop" for k, _ in ev), "last_event_t": ev[-1][1] if ev else None,
            "total_t": round(total, 1), "max_data_gap_s": round(max(gaps), 1) if gaps else None,
            "tail_silence_s": round(total - (data_t[-1] if data_t else 0), 1)}


def _sse(r, t0):
    msg, blocks, usage, ttft, err, done, ev = None, {}, {}, None, None, False, []
    try:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"): continue
            now = round(time.time() - t0, 2)
            ttft = ttft or now
            try: e = json.loads(line[5:].strip())
            except Exception: continue
            t = e.get("type"); ev.append((t, now))
            if t == "message_start":
                msg = e["message"]; usage.update(msg.get("usage") or {})
            elif t == "content_block_start":
                blocks[e["index"]] = dict(e["content_block"])
            elif t == "content_block_delta":
                b, d = blocks[e["index"]], e["delta"]; k = d.get("type")
                if k == "text_delta": b["text"] = b.get("text", "") + d["text"]
                elif k == "thinking_delta": b["thinking"] = b.get("thinking", "") + d["thinking"]
                elif k == "signature_delta": b["signature"] = b.get("signature", "") + d["signature"]
                elif k == "input_json_delta": b["_j"] = b.get("_j", "") + d["partial_json"]
            elif t == "message_delta":
                usage.update(e.get("usage") or {})
                if msg is not None:
                    msg["stop_reason"] = e.get("delta", {}).get("stop_reason")
                    msg["stop_details"] = e.get("delta", {}).get("stop_details")
            elif t == "message_stop": done = True
            elif t == "error": err = e
    except Exception as ex:
        err = err or {"exception": repr(ex)}
    tl = timeline(ev, time.time() - t0)
    if err or msg is None or not done:   # 没有 message_stop 的流一律视为失败（可能是静默断流）
        return {"status": 599 if err else 598, "error": err or {"incomplete_stream": True}, "dt": time.time() - t0, "timeline": tl}
    for b in blocks.values():
        if "_j" in b:
            try: b["input"] = json.loads(b.pop("_j") or "{}")
            except Exception: b["input"] = None
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    msg["usage"] = usage
    return {"status": 200, "msg": msg, "dt": time.time() - t0, "ttft": ttft, "timeline": tl}


def _once(path, body, stream, timeout, extra=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers(extra))
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try: j = json.loads(raw)
        except Exception: j = {"_raw": raw[:1500]}
        return {"status": e.code, "error": j, "dt": time.time() - t0, "headers": dict(e.headers)}
    except Exception as e:
        return {"status": 597, "error": {"exception": repr(e)}, "dt": time.time() - t0}
    with r:
        hd = dict(r.headers)
        if stream:
            res = _sse(r, t0); res["headers"] = hd
            return res
        raw = r.read().decode("utf-8", "replace")
        try: return {"status": r.status, "msg": json.loads(raw), "dt": time.time() - t0, "headers": hd}
        except Exception: return {"status": 597, "error": {"_raw": raw[:1500]}, "dt": time.time() - t0}


def ask(model, messages, max_tokens=4000, stream=True, tag=None, retries=3, extra_headers=None, timeout=900, **kw):
    """发送 /v1/messages。长请求默认流式（避免网关非流式超时）；可重试状态码自动重试。"""
    body = {"model": model, "max_tokens": max_tokens, "messages": messages, **kw}
    if stream: body["stream"] = True
    for a in range(1, retries + 1):
        res = _once("/v1/messages", body, stream, timeout, extra_headers)
        res["attempts"] = a
        if res["status"] == 200 or res["status"] not in RETRY or a == retries: break
        time.sleep(6 * a)
    log(tag=tag, request=body, status=res["status"], dt=round(res["dt"], 1), attempts=res["attempts"],
        response=res.get("msg"), error=res.get("error"), timeline=res.get("timeline"), resp_headers=redact_headers(res.get("headers")))
    return res


def get(path, tag=None):
    r = _once(path, None, False, 60, method="GET")
    if tag:
        log(tag=tag, request={"GET": path}, status=r["status"], dt=round(r["dt"], 1), response=r.get("msg"), error=r.get("error"),
            resp_headers=redact_headers(r.get("headers")))
    return r["status"], r.get("msg") or r.get("error")


# ------------------------------------------------------------------ 取证辅助
LOW = {"output_config": {"effort": "low"}}
def etxt(r): return json.dumps(r.get("error"), ensure_ascii=False)[:500]
def text_of(m): return "".join(b.get("text", "") for b in (m or {}).get("content", []) if b.get("type") == "text")
def tblocks(m): return [b for b in (m or {}).get("content", []) if b.get("type") == "thinking"]
def tin(u): return sum((u or {}).get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
def tthink(u): return ((u or {}).get("output_tokens_details") or {}).get("thinking_tokens") or 0


def think_count(m):
    """thinking token 数及其来源。usage 有 output_tokens_details.thinking_tokens 时直接用；
    没有时（部分协议转换层会重建 usage、丢掉这个字段）用 output_tokens − 正文 token 估算（正文按约 3.6 字符 / token）。"""
    u = (m or {}).get("usage") or {}
    det = (u.get("output_tokens_details") or {}).get("thinking_tokens")
    if det:
        return det, "usage"
    out = u.get("output_tokens") or 0
    est = max(0, out - math.ceil(len(text_of(m)) / 3.6))
    return est, ("estimated" if est else "missing")
def sig_prefix(s): return base64.b64decode(s + "=" * (-len(s) % 4))[:3].hex() if s else "none"


def tamper(s):
    x = list(s)
    for i in range(len(x) // 2, min(len(x), len(x) // 2 + 8)): x[i] = "A" if x[i] != "A" else "B"
    return "".join(x)


def answer(t):
    for ln in reversed([x.strip().strip("*").strip() for x in (t or "").strip().splitlines()]):
        m = re.search(r"ANSWER\s*[:：]\s*(.+)", ln.replace("**", ""), re.I)
        if m: return m.group(1).strip()
    return None


def ints(s): return [int(x) for x in re.findall(r"-?\d+", (s or "").replace(",", "").replace("_", ""))]
def p_one(w, l): n = w + l; return 1.0 if n == 0 else sum(math.comb(n, i) for i in range(w, n + 1)) / 2 ** n
def p_two(w, l): n = w + l; return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(min(w, l) + 1)) / 2 ** n)


def wilson(k, n, z=1.96):
    if not n: return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def results():
    p = os.path.join(OUT, "results.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def save(key, val):
    """跨进程安全地写入 results.json（多个子命令可并行）。"""
    os.makedirs(OUT, exist_ok=True)
    lock = os.path.join(OUT, ".lock")
    fd = None
    for _ in range(600):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY); break
        except FileExistsError:
            time.sleep(0.1)
    try:
        R = results(); R[key] = val
        tmp = os.path.join(OUT, "results.tmp")
        json.dump(R, open(tmp, "w", encoding="utf-8"), ensure_ascii=True, indent=1)   # 纯 ASCII：GBK 等编码读取也不会报错
        os.replace(tmp, os.path.join(OUT, "results.json"))
    finally:
        if fd is not None:
            os.close(fd); os.remove(lock)


# ------------------------------------------------------------------ 中性文本
PROSE = ("The river bends slowly through the valley, carrying silt from the mountains to the sea. In spring the water rises, "
         "and farmers along its banks watch the currents with a mixture of hope and caution. Children skip stones near the old "
         "stone bridge, while herons stand motionless in the shallows, waiting for fish. By late summer the flow is gentle, and "
         "the long evenings fill with the sound of insects and distant bells from the village church. Travelers who follow the "
         "towpath often stop at the inn beside the lock, where the keeper tells stories about floods, droughts, and the barges "
         "that once carried coal, grain, and timber downstream to the busy harbor towns.")
PARAS = [PROSE,
         "Market day begins before dawn. Carts roll in from the farms loaded with cabbages, apples, and wheels of cheese, and the "
         "baker opens his shutters to the smell of warm bread. By noon the square is crowded with neighbors trading news.",
         "The library stays open late in winter. Students gather around the long oak tables, and the librarian walks the aisles "
         "with a small lamp, returning books to their shelves and reminding everyone that the doors close at nine.",
         "Along the coast the wind shifts every afternoon. Fishermen read the clouds, mend their nets on the pier, and wait for the "
         "tide to turn before they push their boats back out toward the grey horizon."]
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima"]
def filler(n, rng): return " ".join(rng.choice(WORDS) + str(rng.randint(0, 999)) for _ in range(n))
def code(rng, n=10): return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(n))
def wcode(rng):
    """普通词组合的随机口令（随机字母串容易触发安全分类器误拒）。"""
    return "-".join([rng.choice(["amber", "cobalt", "ivory", "olive", "coral", "maple", "silver", "violet"]),
                     rng.choice(["fox", "owl", "elk", "yak", "emu", "heron", "otter", "lynx"]), str(rng.randint(1000, 9999))])
