"""positive_control.py — 用 fake_relay.py 的各种作弊模式验证检测力（阳性对照）。
用法：UPSTREAM=<真实端点> UPSTREAM_KEY=<key> python positive_control.py [模式...]
      PC_ONLY=injection,cachegate 只运行指定的检测（结果会合并进各模式已有的 results.json）
"""
import json, os, subprocess, sys, time, socket

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN = {
    "none":           ["baseline", "cachegate", "injection", "channel"],
    "prepend_system": ["baseline", "cachegate", "injection"],
    "hidden_system":  ["baseline", "cachegate", "injection"],
    "tail_append":    ["baseline", "cachegate", "injection"],
    "tail_hidden":    ["baseline", "cachegate", "injection"],
    "per_turn":       ["baseline", "injection"],
    "ua_conditional": ["baseline", "injection"],
    "strip_thinking": ["thinking"],
    "swap_model":     ["substitution", "thinking"],
    "drop_params":    ["params", "substitution"],
    # 渠道识别（S20）
    "kiro":           ["baseline", "injection", "channel"],
    "bedrock_style":  ["channel"],
    "vertex_style":   ["channel"],
    "newapi":         ["channel"],
    "official_like":  ["channel"],
}
EXTRA = {"thinking": ["--peers", "claude-sonnet-5-5,claude-opus-5"]}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def summarize(cmd, R):
    if cmd == "baseline":
        ij = R.get("baseline", {}).get("injection", {})
        return f"hi={ij.get('hi_x4')} 估计注入={ij.get('injected_tokens_est')} 复述={ij.get('dump_tokens')}tok"
    if cmd == "cachegate":
        c = R.get("cachegate", {})
        return f"{c.get('verdict')} 区间={c.get('hidden_injection_range') or c.get('hidden_tokens_lower_bound')} 最短探针前缀={c.get('visible_prefix_at_min_probe')}"
    if cmd == "injection":
        i = R.get("injection", {})
        return f"异常={i.get('anomalies')} 每轮增量={i.get('multi_turn_steps')} 尾部={i.get('conditions', {}).get('tail_after_last_bp', {}).get('value')}"
    if cmd == "thinking":
        t = R.get("thinking", {})
        return f"{t.get('verdict')} 排除={t.get('excluded_impostors')}"
    if cmd == "substitution":
        s = R.get("substitution", {})
        return f"控制探针正常={s.get('controls_ok')} 声明模型一致={s.get('claimed_consistent')} 一致候选={s.get('consistent_candidates')}"
    if cmd == "channel":
        c = R.get("channel", {})
        top = [f"{x['name']}({x['confidence']})" for x in c.get("top", [])]
        chain = " → ".join(f"{x['name']}({x['confidence']})" for x in c.get("chain", [])) or ("未知" if c.get("unknown") else "未确定")
        return f"渠道链={chain}  前三={top}"
    if cmd == "params":
        p = R.get("params", {}).get("claude-opus-5-5", {})
        return "不符=" + str([k for k, v in p.items() if v.get("match") is False])
    return ""


modes = sys.argv[1:] or list(PLAN)
out_all = {}
for mode in modes:
    port = free_port()
    env = dict(os.environ, MODE=mode, PYTHONIOENCODING="utf-8")
    proxy = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_relay.py"), str(port)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    outdir = os.path.join("pc_out", mode)
    cenv = dict(os.environ, BASE=f"http://127.0.0.1:{port}", K="dummy", AUTH="xapikey", OUT=outdir, PYTHONIOENCODING="utf-8")
    try:
        only = [c for c in os.environ.get("PC_ONLY", "").split(",") if c]
        for cmd in [c for c in PLAN[mode] if not only or c in only]:
            subprocess.run([sys.executable, os.path.join(HERE, "claude_probe.py"), cmd, "--model", "claude-opus-5-5", "--conc", "2"] + EXTRA.get(cmd, []),
                           env=cenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
            R = json.load(open(os.path.join(outdir, "results.json"), encoding="utf-8")) if os.path.exists(os.path.join(outdir, "results.json")) else {}
            line = summarize(cmd, R)
            out_all.setdefault(mode, {})[cmd] = line
            print(f"[{mode:15s}] {cmd:12s} {line}", flush=True)
    finally:
        proxy.terminate()
json.dump(out_all, open("pc_summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
