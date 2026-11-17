#!/usr/bin/env python3
"""selfcheck.py — 离线自检（不发任何 API 请求）。

检查：题库校验值、基线与题库是否对应、规则表内部一致性、各判定函数的行为。
用法：python scripts/selfcheck.py      全部通过时退出码为 0
"""
import hashlib, json, os, random, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("K", "selfcheck")          # 只为导入模块，不会发请求
import probe_lib as L
import probe_tasks as T
import probe_extra as X
import claude_probe as P
import probe_score as SC
import probe_inject as I

FAIL = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def fp(ts):
    return hashlib.sha256("\n".join(t["id"] + "|" + t["answer"] + "|" + hashlib.sha256(t["prompt"].encode()).hexdigest()
                                    for t in ts).encode()).hexdigest()[:16]


print("[1] 题库")
std = T.make_suite("20260929", 6)
hard = T.make_suite("20260929", 4, True, T.HARD_FAMS)
check("标准档 30 题", len(std) == 30, str(len(std)))
check("困难档 15 题", len(hard) == 15, str(len(hard)))
check("标准档校验值 979373c3c763254d", fp(std) == "979373c3c763254d", fp(std))
check("困难档校验值 b35ed1f4ab16a8ab", fp(hard) == "b35ed1f4ab16a8ab", fp(hard))
check("同种子两次生成逐字相同", fp(T.make_suite("20260929", 6)) == fp(std))
check("不同种子生成不同题目", fp(T.make_suite("r123", 6)) != fp(std))
check("评分：整数", T.score({"kind": "int", "answer": "42"}, "work...\nANSWER: 42")[0])
check("评分：三元组", T.score({"kind": "ints", "answer": "1 2 3"}, "ANSWER: 1 2 3")[0])
check("评分：没有 ANSWER 行判错", not T.score({"kind": "int", "answer": "42"}, "the answer is 42")[0])

print("[2] 基线数据")
bp = os.path.join(HERE, "..", "data", "baseline_verified_20260929.json")
if os.path.exists(bp):
    base = json.load(open(bp, encoding="utf-8"))
    ids = {t["id"] for t in std} | {t["id"] for t in hard}
    b = base.get("claude-opus-5-5@high", {})
    check("Opus 5.5@high 基线覆盖 45 题", len(b) == 45, str(len(b)))
    check("基线题目 ID 都在题库中", set(b) <= ids, str(set(b) - ids))
    check("基线没有站点或密钥信息", "sk-" not in json.dumps(base) and "http" not in json.dumps(base["_meta"]))
else:
    check("找到基线文件", False, bp)

print("[3] 规则表一致性")
for m, v in L.CACHE_MIN.items():
    if m in X.CACHE_MIN_ALL:
        check(f"缓存最小长度一致：{m}", X.CACHE_MIN_ALL[m] == v, f"{v} vs {X.CACHE_MIN_ALL[m]}")
check("Opus 5.5 的 F_ref = 10", L.F_HI_REF.get("claude-opus-5-5") == 10)
check("between_tools 只有 Sonnet 5.5 接受", L.expect_400("between_tools@high", "claude-opus-5-5") and not L.expect_400("between_tools@high", "claude-sonnet-5-5"))
check("非法 effort 对所有模型都应 400", all(L.expect_400("effort=ultra", m) for m in X.CANDIDATES))
# 每个候选在替换指纹上的"期望向量"，相同向量的候选彼此区分不开
vec = {}
for c in X.CANDIDATES:
    v = tuple(c in must for _, (kw, must) in X.SUB_PROBES.items()) + (X.CACHE_MIN_ALL.get(c),)
    vec.setdefault(v, []).append(c)
groups = [g for g in vec.values() if len(g) > 1]
known = [{"claude-opus-5-5", "claude-fable-5-1"}, {"claude-opus-4-8", "claude-sonnet-5"}]
check("S14 中无法互相区分的候选只有已知的两组", all(set(g) in known for g in groups), str(groups))
key_of = lambda m: next(k for k, g in vec.items() if m in g)
check("Opus 5.5 与 Sonnet 5.5 在 S14 上可区分", key_of("claude-opus-5-5") != key_of("claude-sonnet-5-5"))

print("[4] 判定函数")
th = 500
mk = lambda st, inp: {"status": st, "msg": {"usage": {"input_tokens": inp}}} if st == 200 else {"status": st, "error": {}}
check("READ：差值约等于 thinking", P.classify(mk(200, 1500), mk(200, 1000), th)[0] == "READ")
check("DROP：差值为 0", P.classify(mk(200, 1000), mk(200, 1000), th)[0] == "DROP")
check("5xx 不判为 READ/DROP", P.classify(mk(502, 0), mk(200, 1000), th)[0] == "ERR502")
M = "claude-opus-5-5"
Rd, Dd, SD = (lambda: {"class": "READ"}), (lambda: {"class": "DROP"}), (lambda: {"class": "DROP", "strong_drop": True})
honest = {M + ">claude-sonnet-5-5": Dd(), M + ">claude-fable-5-1": Rd(), "claude-sonnet-5-5>" + M: Rd(), "claude-opus-5>" + M: Rd()}
ex = P.excluded_impostors(M, honest)
check("规则：Opus 5.5 能读 Sonnet 5.5（官方，Claude API）", P.expected("claude-sonnet-5-5", M)[0] == "READ")
check("规则：Sonnet 5.5 读不了 Opus 5.5", P.expected(M, "claude-sonnet-5-5")[0] == "DROP")
check("排除：诚实矩阵排除全部便宜候选", all(c in ex for c in SC.CHEAPER[M]), str(sorted(set(SC.CHEAPER[M]) - set(ex))))
check("排除：普通 DROP 不参与排除", P.excluded_impostors(M, {M + ">claude-sonnet-5-5": Dd(), M + ">claude-opus-5": Dd()}) == {})
check("排除：出错的格不参与排除", P.excluded_impostors(M, {M + ">claude-fable-5-1": {"class": "ERR502"}}) == {})
check("排除：官方 model_binding_mismatch 的 DROP 是强证据", "claude-sonnet-5" in P.excluded_impostors(M, {M + ">claude-sonnet-5-5": SD()}))
swap = {M + ">claude-sonnet-5-5": Rd(), M + ">claude-fable-5-1": Dd()}
chk = [{"cell": k, "obs": v["class"], "exp": P.expected(*k.split(">"))[0]} for k, v in swap.items()]
strong, weak = P.judge_cells(chk)
check("不符：应 DROP 却 READ 判为强不符", strong == [M + ">claude-sonnet-5-5"], str(strong))
check("不符：应 READ 却普通 DROP 只算弱", weak == [M + ">claude-fable-5-1"], str(weak))
check("偷换为 Sonnet 5.5 时不会排除 Sonnet 5.5", "claude-sonnet-5-5" not in P.excluded_impostors(M, swap))
check("签名拒绝必须是指向 signature 的 400", P.sig_rejected({"status": 400, "error": {"message": "Invalid `signature`"}})
      and not P.sig_rejected({"status": 502, "error": {}}) and not P.sig_rejected({"status": 400, "error": {"message": "bad effort"}}))
q1, q2 = P.think_q(0, True)[0], P.think_q(0, True)[0]
check("thinking 题目默认随机", q1 != q2)
check("固定题目可复现", P.think_q(0, False) == P.think_q(0, False))
check("JSD：相同分布为 0", X.jsd(Counter(a=5, b=5), Counter(a=5, b=5)) == 0)
check("JSD：完全不同为 1", X.jsd(Counter(a=10), Counter(b=10)) == 1.0)
check("偷换比例上界 n=30 约 9.5%", abs((1 - 0.05 ** (1 / 30)) - 0.095) < 0.002)
lo, hi = L.wilson(30, 30)
check("Wilson 区间 30/30 下界约 89%", abs(lo - 0.886) < 0.01, str(lo))
check("精确符号检验 p(5,0)=0.0625", abs(L.p_two(5, 0) - 0.0625) < 1e-9)
check("篡改签名改变中间字符", L.tamper("A" * 40) != "A" * 40 and len(L.tamper("A" * 40)) == 40)
check("答案解析：取最后一个 ANSWER 行", L.answer("ANSWER: 1\nmore\n**ANSWER: 2**") == "2")

print("[5] 证据评分")
full = {
    "thinking": {"verdict": "CONSISTENT_WITH_CLAIMED_MODEL", "excluded_impostors": sorted(ex)},
    "substitution": {"controls_ok": True, "claimed_consistent": True, "consistent_candidates": [M, "claude-fable-5-1"]},
    "tokenizer": {"vectors": {M: dict(SC.TOK_REF[M])}},
    "onetoken": {"jsd_vs_claimed": {"claude-sonnet-5-5": {"p1": 0.7}}},
    "bench_random": {"valid": 10, "correct": 10, "vs_baseline_by_family": {"a": {"ratio": 1.0}}},
    "effort": {"max_clearly_above_low": "3/3", "truncated": 0},
    "baseline": {"injection": {"injected_tokens_est": 0}},
    "cachegate": {"verdict": "NO_HIDDEN_INJECTION", "hidden_injection_range": [0, 3]},
    "injection": {"conditions": {"a": {"ok": True}, "b": {"ok": True}}, "anomalies": []},
    "params": {M: {"x": {"match": True}}},
    "tools": {"description_quoted": True, "tool_name_exact": True, "result_echo": {"a": True}},
    "context": {"30000": {"found": {"a": True, "b": True}}, "history_61": {"x": True}, "system_3points": [True]},
    "stream": {"s": {"status": 200, "timeline": {"has_message_stop": True}}},
    "purity": {"judged": 4, "exact": 4, "with_extra_text": []},
    "audit": {"judged": 30, "passed": 30, "failed": 0, "eps_upper95": 0.095},
    "stability": {"hi_start": 10, "hi_end": 10, "sub_end_consistent": True},
    "_reliability": {"total": 100, "errors": 0},
}
S = SC.score(full, M)
check("评分：全部通过时为 100 分", S["total"] == 100 and S["coverage"] == 100, f"{S['total']} / 覆盖 {S['coverage']}")
check("评分：各维度满分合计 100", sum(d["weight"] for d in S["dimensions"].values()) == 100 and
      all(abs(sum(c["max"] for c in d["checks"]) - d["weight"]) < 1e-9 for d in S["dimensions"].values()))
part = {k: v for k, v in full.items() if k in ("thinking", "substitution", "tokenizer", "baseline", "cachegate", "injection", "params", "purity", "stability", "_reliability")}
Sq = SC.score(part, M)
check("评分：只测了一部分且无否决时等级为 I（覆盖不足）", Sq["grade"].startswith("I") and Sq["covered_rate"] == 100.0, Sq["grade"][:30])
S0 = SC.score({}, M)
check("评分：什么都没测为 0 分、覆盖 0", S0["total"] == 0 and S0["coverage"] == 0)
bad = json.loads(json.dumps(full)); bad["thinking"] = {"verdict": "MATRIX_MISMATCH", "reason": "x"}
check("否决：S3 强不符封顶 40", SC.score(bad, M)["total"] <= 40)
bad = json.loads(json.dumps(full)); bad["cachegate"] = {"verdict": "HIDDEN_INJECTION_SUSPECTED"}
check("否决：S2 隐藏注入封顶 60", SC.score(bad, M)["total"] <= 60)
bad = json.loads(json.dumps(full)); bad["injection"]["anomalies"] = ["tail_canary"]; bad["injection"]["conditions"]["b"]["ok"] = False
check("否决：S18 异常封顶 80", SC.score(bad, M)["total"] <= 80)
bad = json.loads(json.dumps(full)); bad["audit"] = {"judged": 30, "passed": 29, "failed": 1}
check("否决：S11 有一条偷换封顶 40", SC.score(bad, M)["total"] <= 40)
bad = json.loads(json.dumps(full)); bad["tokenizer"]["vectors"][M]["mixed"] = 113
check("否决：tokenizer 不符封顶 50", SC.score(bad, M)["total"] <= 50)
part = json.loads(json.dumps(full)); part["thinking"]["excluded_impostors"] = ["claude-opus-4-8"]
check("评分：S3 没排除 Sonnet 5.5 时扣分", SC.score(part, M)["total"] < 100)
check("评分报告声明不是概率", "不是" in SC.render(S) and "概率" in SC.render(S))

print("[6] 注入检测")
check("尾部金丝雀标记是普通词", all(ch.isalnum() or ch == "-" for ch in L.wcode(random.Random(1))))
check("注入参考值存在", I.REF[M]["multi_turn_step"] == 10 and I.REF[M]["tail_after_last_bp"] == 4)

print("[7] 日志、编码与取样")
import re, tempfile
import probe_channel as CH
h = L.redact_headers({"Set-Cookie": "s=1", "Authorization": "Bearer abc", "X-Request-Id": "r1", "X-Echo": "k=" + L.KEY})
check("响应头脱敏：敏感头打码、键转小写", h["set-cookie"] == "<redacted>" and h["authorization"] == "<redacted>" and h["x-request-id"] == "r1")
check("响应头脱敏：值里出现 Key 也会打码", L.KEY not in h["x-echo"] or len(L.KEY) < 8)
with tempfile.TemporaryDirectory() as td:
    envp = os.path.join(td, "x.env")
    open(envp, "w", encoding="utf-8").write("# 注释\nexport SC_TEST_A=\"1\"\nSC_TEST_B=two\n")
    os.environ["ENV_FILE"] = envp
    os.environ["SC_TEST_B"] = "keep"
    L._load_env_file()
    check(".env：支持 export 前缀与引号", os.environ.get("SC_TEST_A") == "1")
    check(".env：已有的环境变量优先", os.environ.get("SC_TEST_B") == "keep")
    del os.environ["ENV_FILE"]
    old_out = L.OUT
    L.OUT = td
    L.save("t", {"中文": "值"})
    raw = open(os.path.join(td, "results.json"), "rb").read()
    check("results.json 是纯 ASCII（GBK 读取也不会报错）", all(b < 128 for b in raw) and L.results()["t"]["中文"] == "值")
    L.OUT = old_out
check("thinking 计数：优先用 usage", L.think_count({"usage": {"output_tokens": 300, "output_tokens_details": {"thinking_tokens": 250}}, "content": []}) == (250, "usage"))
n, src = L.think_count({"usage": {"output_tokens": 300}, "content": [{"type": "text", "text": "x" * 36}]})
check("thinking 计数：缺字段时用 output_tokens 估算", src == "estimated" and n == 290, f"{n} {src}")
check("取样失败分类：始终没有 thinking 块", P.sample_verdict(["no_thinking_block"] * 3)[0] == "NO_THINKING_RETURNED")
check("取样失败分类：5xx 属于可靠性问题", "可靠性" in P.sample_verdict(["http_5xx", "timeout"])[1])

print("[8] 渠道识别（S20）")
SIG = CH.load_signatures()
ok_rules = all(r.get("source") and r.get("checked") and r["channel"] in SIG["channels"] and r["strength"] in ("strong", "weak")
               and (r["where"] == "special" or re.compile(r["pattern"])) for r in SIG["rules"])
check(f"特征库 {len(SIG['rules'])} 条规则都有来源、核对日期与合法正则", ok_rules)
check("特征库 HEUR 规则不会单独给出高置信", all(not (r["source"].startswith("HEUR") and r["weight"] > 4) for r in SIG["rules"]))


def rec(usage, hdr=None, mid="msg_01" + "A" * 22, status=200, error=None, tag="x"):
    return {"tag": tag, "status": status, "resp_headers": hdr or {}, "error": error,
            "response": {"type": "message", "id": mid, "model": M, "usage": usage, "content": [{"type": "text", "text": "hi"}]} if status == 200 else None}


off_usage = {"input_tokens": 10, "output_tokens": 5, "service_tier": "standard", "inference_geo": "us", "output_tokens_details": {}}
kiro_R = {"baseline": {"injection": {"injected_tokens_est": 7230}},
          "injection": {"dump2": {"text": "YES. 1. A system prompt: \"You are Kiro, an AI-powered development environment. You\""}}}
A = CH.analyze([rec({"input_tokens": 400, "output_tokens": 60, "credit_usage": 0.1, "credit_unit": "credit"}) for _ in range(5)], kiro_R, SIG)
check("渠道：Kiro 自述 + credit 字段 + 额外 token → Kiro 第一、置信高", A["top"][0]["channel"] == "kiro" and A["top"][0]["confidence"] == "高",
      str([(c["channel"], c["confidence"]) for c in A["top"]]))
gw = [rec(off_usage, {"x-oneapi-request-id": "abc", "x-new-api-version": "v1"}),
      rec(None, status=503, error={"error": {"type": "new_api_error", "message": "No available channel for model x under group G (distributor)"}})]
A = CH.analyze(gw, {}, SIG)
check("渠道：网关响应头 + 错误体 → new-api 置信高", A["top"][0]["channel"] == "newapi" and A["top"][0]["confidence"] == "高")
check("渠道：证据中的分销商分组名会被打码", all("under group G" not in h["evidence"] for h in A["top"][0]["hits"]))
A = CH.analyze([rec(off_usage, {"request-id": "req_011ABC", "content-type": "application/json"}) for _ in range(5)], {}, SIG)
check("渠道：官方形态的响应不会被识别为任何反代或网关", not [c for c in A["chain"] if c["layer"] != "upstream"], str(A["chain"]))
A = CH.analyze([], {}, SIG)
check("渠道：没有证据时为未知，不默认官方", A["unknown"] and "不能" in CH.chain_text(A))

print("[9] 结论画像")
Pf = SC.portrait(full, M)
check("画像：全部通过时模型型号为已验证", Pf["model"]["conclusion"].startswith("已验证"))
bad = json.loads(json.dumps(full)); bad["thinking"] = {"verdict": "MATRIX_MISMATCH"}
check("画像：S3 强不符时模型型号为不一致", SC.portrait(bad, M)["model"]["conclusion"].startswith("与声明模型不一致"))
kiro_full = dict(full, **kiro_R, channel=CH.analyze([rec({"input_tokens": 400, "output_tokens": 60, "credit_usage": 0.1})] * 5, kiro_R, SIG))
mid = SC.portrait(kiro_full, M)["middle"]
check("画像：识别出 Kiro 时中间层结论写明转换层与额外 token", "Kiro" in mid["conclusion"] and any("7230" in e for e in mid["evidence"]))
check("评分报告包含结论画像与上游画像", "## 结论画像" in SC.render(SC.score(kiro_full, M)) and "## 上游画像" in SC.render(SC.score(kiro_full, M)))

print("[10] 转换层引起的误判防护")
check("READ 解释：只有 Sonnet 读到 → 指向 Sonnet 5.5", P.explain_reads(M, ["claude-sonnet-5-5"]) == (["claude-sonnet-5-5"], False))
check("READ 解释：Fable 与 Sonnet 都读到 → 只能是谁都能读的模型或 thinking 被压平", P.explain_reads(M, ["claude-fable-5-1", "claude-sonnet-5-5"])[1])
amb = json.loads(json.dumps(full)); amb["audit"] = {"samples": 6, "judged": 0, "passed": 0, "failed": 0, "all_read": 5, "undetermined": 0}
amb["substitution"] = {"controls_ok": False, "claimed_consistent": False, "consistent_candidates": []}
Sa = SC.score(amb, M)
check("ALL_READ 且 S14 无法排除旧模型：封顶 60 而不是按偷换封顶 40", Sa["cap"] == 60, str(Sa["vetoes"]))
flat = json.loads(json.dumps(amb)); flat["substitution"] = {"controls_ok": True, "claimed_consistent": True, "consistent_candidates": [M, "claude-fable-5-1"]}
check("ALL_READ 且 S14 已排除旧模型：判为 thinking 被压平，不封顶", SC.score(flat, M)["cap"] == 100 and SC.flattened_thinking(flat, M))
neg = json.loads(json.dumps(full)); neg["tokenizer"]["vectors"][M]["en"] = -6612
Sn = SC.score(neg, M)
check("tokenizer 计数出现负值：判为计数不可靠，不按换模型封顶", Sn["cap"] == 100 and any(c["state"] == "无法判定" for c in Sn["dimensions"]["A 模型身份"]["checks"]))
st = json.loads(json.dumps(full)); st["stability"] = {"hi_start": 7240, "hi_end": 7240, "sub_start_consistent": False, "sub_end_consistent": False}
check("稳定性：首尾一致（即使 S14 两次都不一致）不算线路切换", not any("线路切换" in v for v in SC.score(st, M)["vetoes"]))

print()
if FAIL:
    print(f"自检失败 {len(FAIL)} 项：{FAIL}")
    sys.exit(1)
print("自检全部通过")
