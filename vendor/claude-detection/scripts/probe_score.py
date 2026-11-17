"""probe_score.py — 证据评分（100 分制）与一键执行（配套 Claude-skills.md 第 4.4 节）。

评分衡量的是"在已完成的检测中，支持'没有掺水'的证据有多充分"，不是"为真的概率"。
- 每一分都对应一项具体检测；没测的项得 0 分并计入"未覆盖"，不会被当作满分。
- 一票否决：发现替换迹象、隐藏注入、偷换等，会把总分封顶。
- 报告时必须同时给出分项明细、覆盖率和否决原因。
"""
import copy, json, os, time
from probe_lib import *  # noqa: F401,F403

# 同一端点、零注入、签名正常时实测的参考数据（OBS，2026-10-01）；其他端点或模型更新后需要重测
TOK_REF = {
    "claude-opus-5-5": {"en": 125, "zh": 73, "ja": 68, "code": 102, "digits": 35, "emoji": 61, "mixed": 68},
    "claude-sonnet-5-5": {"en": 125, "zh": 73, "ja": 68, "code": 102, "digits": 35, "emoji": 61, "mixed": 68},
    "claude-fable-5-1": {"en": 125, "zh": 73, "ja": 68, "code": 102, "digits": 35, "emoji": 61, "mixed": 113},
    "claude-opus-4-6": {"en": 90, "zh": 73, "ja": 68, "code": 98, "digits": 35, "emoji": 61, "mixed": 59},
}
CHEAPER = {"claude-opus-5-5": ["claude-sonnet-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-haiku-4-5"]}


def _chk(name, pts, got, state, detail):
    return {"name": name, "max": pts, "got": round(got, 1), "state": state, "detail": detail}


def tokens_unreliable(R, model):
    """S15 的计数向量出现负值、或某项超过参考值的 3 倍：usage 很可能是估算值或混入了缓存，计数类方法不可用。"""
    vec = ((R.get("tokenizer") or {}).get("vectors") or {}).get(model) or {}
    ref = TOK_REF.get(model) or {}
    return any(isinstance(v, (int, float)) and (v <= 0 or (ref.get(k) and v > 3 * ref[k])) for k, v in vec.items())


def flattened_thinking(R, model):
    """S11 出现"所有对照都能读"时，判断是否为转换层把 thinking 压平成文本：
    S14 已排除 Opus 4.8 / Sonnet 5 这类谁都能读的旧模型，或 S3 显示签名没有被校验。"""
    au = R.get("audit") or {}
    if not au.get("all_read"):
        return False
    sb, th = R.get("substitution") or {}, R.get("thinking") or {}
    all_readers = {"claude-opus-4-8", "claude-sonnet-5"}
    s14_excl = bool(sb.get("controls_ok")) and not (all_readers & set(sb.get("consistent_candidates", [])))
    return s14_excl or th.get("verdict") in ("SIGNATURE_NOT_VALIDATED", "THINKING_NOT_FORWARDED")


def score(R, model):
    D = {}            # 维度 -> 检查列表
    veto = []         # (封顶分, 原因)

    # ---------------- A 模型身份（35）
    A = []
    th = R.get("thinking")
    if not th:
        A.append(_chk("S3 签名与读取矩阵", 15, 0, "未测", ""))
    else:
        v = th.get("verdict")
        excl = th.get("excluded_impostors", [])
        cheaper = [c for c in CHEAPER.get(model, []) if c in excl]
        if v == "CONSISTENT_WITH_CLAIMED_MODEL":
            left = [c for c in CHEAPER.get(model, []) if c not in excl]
            g = 15 if not left else (12 if "claude-sonnet-5-5" not in left else 8)
            A.append(_chk("S3 签名与读取矩阵", 15, g, "通过" if not left else "部分", f"强证据排除 {excl}；未被排除的便宜候选 {left}"))
        elif v == "SIGNATURE_BEHAVIOR_ONLY":
            A.append(_chk("S3 签名与读取矩阵", 15, 6, "部分", "签名行为一致，但没有排除任何冒充者"))
        elif v in ("THINKING_NOT_FORWARDED", "SIGNATURE_NOT_VALIDATED", "NO_THINKING_RETURNED"):
            A.append(_chk("S3 签名与读取矩阵", 15, 0, "证据不可用", v + "：签名类身份证据不可用（不等于造假）"))
        elif v == "MATRIX_MISMATCH":
            A.append(_chk("S3 签名与读取矩阵", 15, 0, "不通过", th.get("reason", "")))
            veto.append((40, "S3 读取矩阵与声明模型的规则不符"))
        else:
            A.append(_chk("S3 签名与读取矩阵", 15, 0, "无法判定", (v or "") + "：" + str(th.get("reason", ""))[:120]))
    sb = R.get("substitution")
    if not sb:
        A.append(_chk("S14 替换指纹", 12, 0, "未测", ""))
    elif not sb.get("controls_ok"):
        A.append(_chk("S14 替换指纹", 12, 0, "无法判定", "控制探针没有全部返回 400，中转可能在清洗参数"))
    elif sb.get("claimed_consistent"):
        others = [c for c in sb.get("consistent_candidates", []) if c != model]
        cheap_left = [c for c in others if c in CHEAPER.get(model, [])]
        g = 12 if not cheap_left else 6
        A.append(_chk("S14 替换指纹", 12, g, "通过" if g == 12 else "部分", f"仍一致的其他候选 {others}"))
    else:
        first = (sb.get("candidates", {}).get(model, {}).get("mismatches") or [""])[0]
        A.append(_chk("S14 替换指纹", 12, 0, "不通过", "声明模型被排除：" + first))
        veto.append((40, "S14：服务端规则行为与声明模型不符（" + first + "）"))
    tk = R.get("tokenizer")
    ref = TOK_REF.get(model)
    if not tk or not (tk.get("vectors") or {}).get(model):
        A.append(_chk("S15 tokenizer 向量", 5, 0, "未测", ""))
    elif not ref:
        A.append(_chk("S15 tokenizer 向量", 5, 0, "无参考", "该模型没有内置参考向量"))
    else:
        vec = tk["vectors"][model]
        diff = {k: (vec.get(k), ref[k]) for k in ref if vec.get(k) != ref[k]}
        if tokens_unreliable(R, model):
            A.append(_chk("S15 tokenizer 向量", 5, 0, "无法判定", f"token 计数不可靠（出现负值或远超参考值）：{vec}；usage 可能是估算值或混入了缓存"))
        elif not diff:
            A.append(_chk("S15 tokenizer 向量", 5, 5, "通过", "与参考向量逐项一致"))
        else:
            A.append(_chk("S15 tokenizer 向量", 5, 0, "不通过", f"与参考不同 {diff}"))
            veto.append((50, f"S15：tokenizer 向量与 {model} 的参考不同"))
    ot = R.get("onetoken")
    if not ot:
        A.append(_chk("S16 单 token 分布", 3, 0, "未测", ""))
    else:
        far = {p: v for p, v in (ot.get("jsd_vs_claimed") or {}).items() if p in CHEAPER.get(model, []) and max(v.values()) >= 0.5}
        A.append(_chk("S16 单 token 分布", 3, 3 if far else 1, "通过" if far else "部分",
                      f"与以下便宜模型明显不同：{list(far)}" if far else "与对照模型区分不明显（这个方法本身较弱）"))
    D["A 模型身份"] = (35, A)

    # ---------------- B 能力与推理强度（20）
    B = []
    bn = R.get("bench_random") or R.get("bench")
    if not bn or not bn.get("valid"):
        B.append(_chk("S7 能力基准", 12, 0, "未测", ""))
    else:
        acc = bn["correct"] / bn["valid"]
        g = 8 * min(1.0, acc / 0.9)
        ratio = (bn.get("vs_baseline") or {}).get("out_ratio_geomean")
        fam = bn.get("vs_baseline_by_family")
        if ratio is None and fam:
            rs = [v["ratio"] for v in fam.values() if v.get("ratio")]
            ratio = (sorted(rs)[len(rs) // 2]) if rs else None
        rg = 4 if ratio is not None and 0.7 <= ratio <= 1.5 else 0
        B.append(_chk("S7 能力基准", 12, g + rg, "通过" if g + rg >= 11 else "部分",
                      f"{bn['correct']}/{bn['valid']} 正确；与基线的 token 比值 {ratio}" + ("" if bn["valid"] >= 5 else "（样本少于 5 题）")))
        if acc < 0.6 and bn["valid"] >= 5:
            veto.append((60, f"S7：正确率仅 {acc:.0%}，明显低于已验证基线"))
    ef = R.get("effort")
    if not ef:
        B.append(_chk("S8 effort", 8, 0, "未测", ""))
    else:
        k, n = map(int, ef["max_clearly_above_low"].split("/"))
        B.append(_chk("S8 effort", 8, 8 * k / n if n else 0, "通过" if k == n and n else "部分", f"max 明显高于 low：{k}/{n}；触顶 {ef.get('truncated')}"))
        if n and k == 0:
            veto.append((70, "S8：各档 effort 的思考量没有拉开，推理强度可能被压低"))
    D["B 能力与推理强度"] = (20, B)

    # ---------------- C 注入与请求完整性（20）
    C = []
    bl = R.get("baseline")
    if not bl:
        C.append(_chk("S1 可见注入", 6, 0, "未测", ""))
    else:
        ij = bl.get("injection", {})
        est = ij.get("injected_tokens_est")
        if est is None:
            ok = (not ij.get("dump_text")) and (ij.get("extra_when_custom_system") or 0) <= 2
            C.append(_chk("S1 可见注入", 6, 3 if ok else 0, "部分" if ok else "不通过", "该模型没有官方参考计数；复述为空且自带 system 无额外" if ok else "复述出前置内容或自带 system 有额外 token"))
        elif est <= 3:
            C.append(_chk("S1 可见注入", 6, 6, "通过", f"usage 显示额外 {est} token"))
        else:
            C.append(_chk("S1 可见注入", 6, 3 if est <= 30 else 0, "不通过", f"每个请求额外约 {est} token；复述内容 {ij.get('dump_tokens')} token"))
            veto.append((85 if est <= 30 else 75, f"S1：存在可见注入（约 {est} token/请求）"))
    cg = R.get("cachegate")
    if not cg:
        C.append(_chk("S2 隐藏注入", 6, 0, "未测", ""))
    else:
        v = cg.get("verdict")
        if v == "NO_HIDDEN_INJECTION":
            C.append(_chk("S2 隐藏注入", 6, 6, "通过", f"隐藏注入区间 {cg.get('hidden_injection_range')}"))
        elif v == "HIDDEN_INJECTION_SUSPECTED":
            C.append(_chk("S2 隐藏注入", 6, 0, "不通过", f"临界点 {cg.get('first_cached_visible_prefix')}，低于官方最小值"))
            veto.append((60, "S2：发现 usage 未显示的隐藏注入迹象"))
        elif v == "CACHED_AT_MIN_PROBE":
            C.append(_chk("S2 隐藏注入", 6, 3, "部分", "最短探针就已缓存，前缀里已有大量内容（与 S1 的可见注入对照）"))
        else:
            C.append(_chk("S2 隐藏注入", 6, 0, "无法判定", v or ""))
    inj = R.get("injection")
    if not inj:
        C.append(_chk("S18 注入条件矩阵", 4, 0, "未测", ""))
    else:
        conds = inj.get("conditions", {})
        judged = [c for c in conds.values() if c.get("ok") is not None]
        good = sum(1 for c in judged if c["ok"])
        badtxt = "；".join(f"{k} 实测 {conds.get(k, {}).get('value')} / 期望 {conds.get(k, {}).get('expect')}" for k in inj.get("anomalies") or [])
        C.append(_chk("S18 注入条件矩阵", 4, 4 * good / len(judged) if judged else 0, "通过" if judged and good == len(judged) else ("部分" if judged else "无法判定"),
                      f"可判定 {len(judged)} 项" + (f"，异常：{badtxt}" if badtxt else "，无异常")))
        if inj.get("anomalies"):
            veto.append((80, f"S18：存在条件注入或改写迹象 {inj.get('anomalies')}"))
    pm = R.get("params")
    if not pm or model not in pm:
        C.append(_chk("S4 参数指纹", 2, 0, "未测", ""))
    else:
        v = pm[model]
        judged = [x for x in v.values() if x.get("match") is not None]
        bad = [k for k, x in v.items() if x.get("match") is False]
        C.append(_chk("S4 参数指纹", 2, 2 * (1 - len(bad) / len(judged)) if judged else 0, "通过" if judged and not bad else ("部分" if judged else "无法判定"),
                      f"可判定 {len(judged)}/{len(v)}，不符 {bad}"))
    tl = R.get("tools")
    if not tl:
        C.append(_chk("S17 工具完整性", 2, 0, "未测", ""))
    else:
        ok = tl.get("description_quoted") and tl.get("tool_name_exact") and all((tl.get("result_echo") or {}).values()) and tl.get("result_echo")
        C.append(_chk("S17 工具完整性", 2, 2 if ok else 0, "通过" if ok else "不通过", f"描述 {tl.get('description_quoted')}，工具名 {tl.get('tool_name_exact')}，结果 {tl.get('result_echo')}"))
    D["C 注入与请求完整性"] = (20, C)

    # ---------------- D 上下文与输出完整性（10）
    Dd = []
    cx = R.get("context")
    if not cx:
        Dd.append(_chk("S5 上下文", 6, 0, "未测", ""))
    else:
        sizes = {k: v for k, v in cx.items() if isinstance(v, dict) and "found" in v}
        found = sum(sum(v["found"].values()) for v in sizes.values())
        total = sum(len(v["found"]) for v in sizes.values())
        hist = cx.get("history_61", {})
        sysok = cx.get("system_3points", [])
        g = (4 * found / total if total else 0) + (1 if hist and all(hist.values()) else 0) + (1 if sysok and all(sysok) else 0)
        Dd.append(_chk("S5 上下文", 6, g, "通过" if g >= 5.9 else "部分", f"召回 {found}/{total}（档位 {list(sizes)}），历史 {hist}，system {sysok}"))
        if total and found / total < 0.8:
            veto.append((60, f"S5：长上下文召回 {found}/{total}，疑似截断"))
    sm = R.get("stream")
    if not sm:
        Dd.append(_chk("S6 流式完整性", 2, 0, "未测", ""))
    else:
        ok = all(v.get("status") == 200 and (v.get("timeline") or {}).get("has_message_stop") for v in sm.values())
        Dd.append(_chk("S6 流式完整性", 2, 2 if ok else 0, "通过" if ok else "不通过", f"{ {k: v.get('status') for k, v in sm.items()} }"))
    pu = R.get("purity")
    if not pu or not pu.get("judged"):
        Dd.append(_chk("S19 输出纯净度", 2, 0, "未测", ""))
    else:
        Dd.append(_chk("S19 输出纯净度", 2, 2 * pu["exact"] / pu["judged"], "通过" if pu["exact"] == pu["judged"] else "部分",
                       f"逐字一致 {pu['exact']}/{pu['judged']}，多余文字 {pu.get('with_extra_text')[:1]}"))
        if pu.get("with_extra_text"):
            veto.append((80, "S19：回复中出现了要求之外的文字（可能被加前后缀）"))
    D["D 上下文与输出完整性"] = (10, Dd)

    # ---------------- E 路由稳定性（10）
    E = []
    au = R.get("audit")
    if au and au.get("samples") and au.get("undetermined", 0) / au["samples"] >= 0.5:
        veto.append((70, f"S11：{au['undetermined']}/{au['samples']} 条抽检没有出现应有的 READ（声明模型的 block 本不绑定账号，诚实端点上应几乎总能被读取）"))
    if au and au.get("all_read"):
        if flattened_thinking(R, model):
            pass                                              # 转换层压平 thinking：记入画像的中间层，不按偷换处理
        else:
            veto.append((60, f"S11：{au['all_read']} 条样本的 thinking 被所有对照模型读取，可能是转换层把 thinking 压平成了文本，"
                             "也可能被换成了谁都能读的旧模型（S14 没有排除后者）"))
    if not au or not (au.get("judged") or au.get("all_read")):
        E.append(_chk("S11 多样本身份抽检", 7, 0, "未测", ""))
    elif not au.get("judged"):
        E.append(_chk("S11 多样本身份抽检", 7, 0, "无法判定", f"{au['all_read']} 条样本的 thinking 被所有对照模型读取，读取矩阵不可用（见画像）"))
    elif au.get("failed", au["judged"] - au["passed"]):
        nf = au.get("failed", au["judged"] - au["passed"])
        E.append(_chk("S11 多样本身份抽检", 7, 0, "不通过", f"{nf} 条出现应 DROP 却 READ"))
        veto.append((40, f"S11：{nf}/{au['judged']} 条抽检出现应 DROP 却 READ，疑似部分请求被偷换"))
    else:
        E.append(_chk("S11 多样本身份抽检", 7, 7 * min(1.0, au["judged"] / 30), "通过" if au["judged"] >= 30 else "部分",
                      f"{au['passed']}/{au['judged']} 通过（{au.get('samples', au['judged'])} 个样本中 {au.get('sample_failed', 0)} 个取样失败、{au.get('undetermined', 0)} 个无法判定），偷换比例 95% 上界 {au.get('eps_upper95')}"))
    stb = R.get("stability")
    if not stb:
        E.append(_chk("时段稳定性", 3, 0, "未测", "一键执行时会在开头和结尾各测一次"))
    else:
        s0, s1 = stb.get("sub_start_consistent"), stb.get("sub_end_consistent")
        ok = stb.get("hi_start") == stb.get("hi_end") and (s0 is None or s0 == s1)          # 只看首尾是否变化
        E.append(_chk("时段稳定性", 3, 3 if ok else 0, "通过" if ok else "不通过", json.dumps(stb, ensure_ascii=False)))
        if not ok:
            veto.append((70, "检测期间端点行为发生变化（线路切换）"))
    D["E 路由稳定性"] = (10, E)

    # ---------------- F 服务可靠性（5）
    F = []
    rel = R.get("_reliability")
    if not rel or not rel.get("total"):
        F.append(_chk("错误率", 5, 0, "未测", ""))
    else:
        er = rel["errors"] / rel["total"]
        F.append(_chk("错误率", 5, 5 if er <= 0.02 else (3 if er <= 0.1 else 0), "通过" if er <= 0.02 else "部分",
                      f"{rel['errors']}/{rel['total']} 次 5xx / 超时 / 空流（{er:.1%}）"))
    D["F 服务可靠性"] = (5, F)

    raw = sum(c["got"] for _, (w, cs) in D.items() for c in cs)
    covered = sum(c["max"] for _, (w, cs) in D.items() for c in cs if c["state"] not in ("未测", "无法判定", "无参考"))
    cap = min([v[0] for v in veto], default=100)
    total = round(min(raw, cap), 1)
    rate = round(100 * raw / covered, 1) if covered else None          # 已覆盖检测项的得分率
    if not veto and covered < 80:
        grade = (f"I 覆盖不足：只完成了 {covered} 分的检测项，已测项得分率 {rate}%；"
                 "总分不代表发现了问题，需运行 standard 档（或补齐未测项）才能给出完整等级")
    else:
        grade = ("A 高度一致：在已完成的检测中没有发现掺水证据" if total >= 90 else
                 "B 基本一致：有轻微问题或覆盖不全" if total >= 75 else
                 "C 存在明显问题" if total >= 60 else "D 高风险：发现掺水证据或证据严重不足")
    chn = R.get("channel")
    chs = None
    if chn:
        import probe_channel as CH
        chs = {"chain_text": CH.chain_text(chn), "top": chn.get("top", []), "usage_shapes": chn.get("usage_shapes"),
               "notes": chn.get("notes", []), "signatures_version": chn.get("signatures_version")}
    return {"model": model, "total": total, "raw": round(raw, 1), "cap": cap, "coverage": covered, "covered_rate": rate, "grade": grade,
            "portrait": portrait(R, model), "channel": chs,
            "vetoes": [r for _, r in sorted(veto)], "dimensions": {k: {"weight": w, "got": round(sum(c["got"] for c in cs), 1), "checks": cs} for k, (w, cs) in D.items()}}


def reliability_from_log():
    p = os.path.join(OUT, "raw.jsonl")
    if not os.path.exists(p):
        return None
    tot = err = 0
    for ln in open(p, encoding="utf-8"):
        r = json.loads(ln)
        tot += 1
        if r.get("status") not in (200, 400, 404, 413):     # 400 等是探针预期内的拒绝，不算可靠性问题
            err += 1
    return {"total": tot, "errors": err}


def portrait(R, model):
    """三个独立结论：上游是否 Anthropic 原生、中间是否有转换 / 注入层、模型型号是否被验证。每个都给出证据强度与未排除项。"""
    th, sb, tk, au = R.get("thinking") or {}, R.get("substitution") or {}, R.get("tokenizer") or {}, R.get("audit") or {}
    ch = R.get("channel") or {}
    chain = ch.get("chain") or []
    P = {}
    # 1) 上游
    sig_ok = (th.get("garbage_signature") or {}).get("rejected") and (th.get("tampered_signature") or {}).get("rejected")
    ups = [c for c in chain if c["layer"] == "upstream"]
    up_txt = "、".join(f"{c['name']}（{c['confidence']}）" for c in ups)
    if sig_ok:
        P["upstream"] = {"conclusion": "Anthropic 原生后端（官方 API、Bedrock、Vertex、Azure 之一）" + (f"；渠道特征指向 {up_txt}" if up_txt else ""),
                         "strength": "E1（签名校验行为）", "evidence": [f"伪造与篡改签名都得到指向 signature 的 400（S3：{th.get('verdict')}）"],
                         "not_excluded": [] if any(c["confidence"] == "高" for c in ups) else ["官方 API、Bedrock、Vertex、Azure 之间无法区分（没有高置信的上游渠道特征）"]}
    elif th.get("verdict") == "SIGNATURE_NOT_VALIDATED":
        P["upstream"] = {"conclusion": "存在疑点：伪造或篡改的签名被接受", "strength": "E2", "evidence": [str(th.get("reason", ""))],
                         "not_excluded": ["上游不是 Anthropic", "中转在上游 400 后把 thinking 转成 text 重试"]}
    elif sb.get("controls_ok") and sb.get("claimed_consistent"):
        P["upstream"] = {"conclusion": "参数规则行为与 Anthropic 后端一致，但签名类证据不可用" + (f"；渠道特征指向 {up_txt}" if up_txt else ""),
                         "strength": "E2（参数规则）", "evidence": [f"S14 控制探针正常，声明模型在一致候选中；S3：{th.get('verdict', '未测')}"],
                         "not_excluded": ["协议转换层复刻了参数规则（成本较高）", "官方 API 与云平台之间无法区分"]}
    else:
        s14 = "未测" if not sb else ("控制探针异常" if not sb.get("controls_ok") else "声明模型被排除")
        P["upstream"] = {"conclusion": "无法判定", "strength": "—", "evidence": [f"S3：{th.get('verdict', '未测')}；S14：{s14}"],
                         "not_excluded": ["所有可能"]}
    # 2) 中间层：网关（通常只转发）与转换层 / 注入层分开写
    gws = [c for c in chain if c["layer"] == "gateway"]
    cls = [c for c in chain if c["layer"] == "client"]
    gw_txt = "、".join(f"{c['name']}（{c['confidence']}）" for c in gws)
    ev = [f"{c['name']}（{c['confidence']}）：" + "；".join(h["evidence"] for h in c["hits"][:2]) for c in cls]
    est = ((R.get("baseline") or {}).get("injection") or {}).get("injected_tokens_est")
    if isinstance(est, (int, float)) and est > 3:
        ev.append(f"S1：每个请求额外约 {est} token")
    cg = R.get("cachegate") or {}
    if cg.get("verdict") == "HIDDEN_INJECTION_SUSPECTED":
        ev.append(f"S2：发现 usage 未显示的隐藏注入，区间 {cg.get('hidden_injection_range')}")
    an = (R.get("injection") or {}).get("anomalies") or []
    if an:
        ev.append(f"S18 异常条件：{an}")
    if tokens_unreliable(R, model):
        ev.append("S15：token 计数出现负值或远超参考值，usage 很可能是转换层估算的，计数类结论（S1、S2、S18）的精确数值要谨慎看待")
    if flattened_thinking(R, model):
        ev.append(f"S11：{(R.get('audit') or {}).get('all_read')} 条样本的 thinking 被所有对照模型读取，且 S14 已排除谁都能读的旧模型："
                  "转换层很可能把历史 thinking 压平成了普通文本")
    pm = (R.get("params") or {}).get(model) or {}
    badp = [k for k, v in pm.items() if v.get("match") is False]
    if badp:
        ev.append(f"S4：与官方规则不符（可能被忽略或改写）的参数 {badp}")
    if ev:
        strong = (any(c["confidence"] == "高" for c in cls) or cg.get("verdict") == "HIDDEN_INJECTION_SUSPECTED"
                  or (isinstance(est, (int, float)) and est > 30))
        names = "、".join(c["name"] for c in cls)
        P["middle"] = {"conclusion": "存在转换层或注入层" + (f"：{names}" if names else "") + (f"；另经过网关 {gw_txt}" if gw_txt else ""),
                       "strength": "强" if strong else "中", "evidence": ev, "not_excluded": ["还有其他未被识别的中间层"]}
    else:
        tested = [k for k in ("baseline", "cachegate", "injection", "channel") if R.get(k)]
        if not tested:
            concl = "未测"
        elif gw_txt:
            concl = f"经过网关 {gw_txt}；在可观测范围内未发现转换层、注入或改写"
        else:
            concl = "在可观测范围内未发现转换层或注入层"
        P["middle"] = {"conclusion": concl, "strength": "E2" if len(tested) >= 3 else "弱",
                       "evidence": [f"已完成：{tested}"] + [f"{c['name']}（{c['confidence']}）：" + "；".join(h["evidence"] for h in c["hits"][:2]) for c in gws],
                       "not_excluded": ["只对检测者发出的请求成立", "不在检测条件内的注入方式"]}
    # 3) 模型型号
    ev = []
    s3ok = th.get("verdict") == "CONSISTENT_WITH_CLAIMED_MODEL"
    if th:
        ev.append(f"S3：{th.get('verdict')}" + (f"，强证据排除 {len(th.get('excluded_impostors', []))} 个候选" if s3ok else ""))
    s14ok = bool(sb.get("controls_ok") and sb.get("claimed_consistent"))
    if sb:
        others = [c for c in sb.get("consistent_candidates", []) if c != model]
        ev.append("S14：" + (f"声明模型一致，仍一致的其他候选 {others}" if s14ok else ("控制探针异常，无法判定" if not sb.get("controls_ok") else "声明模型被排除")))
    if (tk.get("vectors") or {}).get(model):
        ev.append("S15：" + ("token 计数不可靠，无法判定" if tokens_unreliable(R, model) else "tokenizer 向量已采集"))
    if au.get("judged"):
        ev.append(f"S11：{au.get('passed')}/{au.get('judged')} 条强判定通过，偷换比例 95% 上界 {au.get('eps_upper95')}")
    bad = th.get("verdict") == "MATRIX_MISMATCH" or (sb.get("controls_ok") and sb.get("claimed_consistent") is False) or au.get("failed")
    if bad:
        P["model"] = {"conclusion": "与声明模型不一致", "strength": "强", "evidence": ev, "not_excluded": ["官方规则已更新（先核对官方文档）"]}
    elif s3ok and s14ok:
        left = sorted(set(c for c in sb.get("consistent_candidates", []) if c != model) - set(th.get("excluded_impostors", [])))
        P["model"] = {"conclusion": "已验证（服务端行为与声明模型一致）", "strength": "E1 + E2", "evidence": ev,
                      "not_excluded": [f"两种方法都没有排除：{left}"] if left else ["部分请求被偷换（见 S11 的比例上界）"]}
    elif s3ok or s14ok:
        P["model"] = {"conclusion": "部分验证：只有一种身份方法给出了一致结论", "strength": "E1" if s3ok else "E2", "evidence": ev,
                      "not_excluded": ["另一种方法无法判定时，对应的冒充者没有被排除"]}
    else:
        P["model"] = {"conclusion": "未验证：没有任何身份方法给出可用结论", "strength": "—", "evidence": ev or ["S3、S14 都未测或无法判定"],
                      "not_excluded": ["所有候选模型"]}
    return P


def render(S):
    L = [f"# 证据评分：{S['model']}", "",
         f"**总分 {S['total']} / 100**（原始得分 {S['raw']}，封顶 {S['cap']}）　{S['grade']}", "",
         f"- 覆盖率：{S['coverage']} / 100 分的检测项实际完成并可判定（未测、无法判定的项计 0 分）；已测项得分率 {S.get('covered_rate')}%",
         "- 这是**规则化的证据评分，不是\"为真的概率\"**；只适用于本次测试时段与该 Key / 分组。", ""]
    if S["vetoes"]:
        L += ["**封顶原因（一票否决）**：", ""] + [f"- {v}" for v in S["vetoes"]] + [""]
    P = S.get("portrait") or {}
    if P:
        L += ["## 结论画像", "", "> 三个独立结论，只代表本次测试时段与该 Key / 分组。", "",
              "| 问题 | 结论 | 证据强度 | 主要证据 | 未排除 |", "|---|---|---|---|---|"]
        for key, title in (("upstream", "上游是否 Anthropic 原生"), ("middle", "中间是否有转换层 / 注入层"), ("model", "模型型号是否被验证")):
            x = P.get(key) or {}
            L.append(f"| {title} | {x.get('conclusion')} | {x.get('strength')} | {'<br>'.join(str(e)[:180] for e in x.get('evidence', []))} | {'<br>'.join(x.get('not_excluded', []))} |")
        L.append("")
    ch = S.get("channel")
    if ch:
        L += ["## 上游画像（渠道识别，S20）", "", f"- **渠道链**：{ch.get('chain_text')}"]
        for c in ch.get("top", []):
            L.append(f"- {c['name']}：得分 {c['score']}，置信 **{c['confidence']}**，命中维度 {c['dims']}")
            for h in c["hits"][:4]:
                L.append(f"  - {'强' if h['strength'] == 'strong' else '弱'}｜{h['evidence']}（{h['source'][:60]}）")
        if ch.get("usage_shapes"):
            L.append(f"- usage 形态分布：{ch['usage_shapes']}")
        for n in ch.get("notes", []):
            L.append(f"- 注意：{n}")
        L += ["- 渠道识别只代表本次测试时段与该 Key / 分组；特征库会过期（版本 %s）。" % ch.get("signatures_version"), ""]
    L += ["## 分项明细", "", "| 维度 | 检测项 | 得分 / 满分 | 状态 | 说明 |", "|---|---|---|---|---|"]
    for dim, d in S["dimensions"].items():
        for c in d["checks"]:
            L.append(f"| {dim}（{d['got']}/{d['weight']}） | {c['name']} | {c['got']} / {c['max']} | {c['state']} | {str(c['detail'])[:220]} |")
    return NL.join(L)


def s_score(a):
    R = results()
    R["_reliability"] = reliability_from_log()
    S = score(R, a.model)
    save("score", S)
    md = render(S)
    open(os.path.join(OUT, "SCORE.md"), "w", encoding="utf-8").write(md)
    print(md)


# ---------------------------------------------------------------- 一键执行
TIERS = {
    "quick": [("baseline", {}), ("substitution", {}), ("thinking", {"peers": "claude-sonnet-5-5,claude-fable-5-1,claude-opus-5"}),
              ("tokenizer", {"peers": "claude-sonnet-5-5,claude-opus-4-6"}), ("cachegate", {}), ("injection", {}),
              ("purity", {"samples": 4}), ("params", {"peers": ""}), ("channel", {})],
    "standard": [("audit", {"samples": 30, "peers": "claude-fable-5-1,claude-sonnet-5-5"}), ("effort", {}),
                 ("bench", {"seed": "random", "per_family": 2}), ("tools", {}), ("context", {"sizes": "30000,190000"}),
                 ("stream", {"displays": "summarized"}), ("onetoken", {"n": 20, "peers": "claude-sonnet-5-5,claude-opus-4-8"})],
    "deep": [("context", {"sizes": "30000,190000,250000,750000"}), ("bench", {"seed": "20260929", "per_family": 4, "hard": True}),
             ("audit", {"samples": 60, "peers": "claude-fable-5-1,claude-sonnet-5-5"}), ("stream", {"displays": "summarized,omitted", "stream_effort": "max"})],
}


# 每一步的大致请求数与单次请求耗时（秒，2 并发下按以往实测估算）；只用于给出进度预期
SEC_PER_REQ = {"effort": (40, 90), "bench": (25, 70), "stream": (30, 200), "context": (15, 90), "thinking": (5, 15), "audit": (4, 12)}


def est_requests(cmd, kw):
    n = {"baseline": 14, "substitution": 26, "thinking": 60, "tokenizer": 24, "cachegate": 12, "injection": 16, "params": 9,
         "channel": 4, "effort": 12, "tools": 3}.get(cmd)
    if n is None:
        n = {"purity": kw.get("samples", 6), "audit": 8 * kw.get("samples", 6),
             "bench": int(5 * kw.get("per_family", 2) * (1.5 if kw.get("hard") else 1)),
             "context": 3 * len(str(kw.get("sizes", "30000")).split(",")),
             "stream": 2 * len(str(kw.get("displays", "summarized")).split(",")), "onetoken": 9 * kw.get("n", 20)}.get(cmd, 10)
    lo, hi = SEC_PER_REQ.get(cmd, (2, 8))
    return n, n * lo / 60, n * hi / 60


def _nreq():
    p = os.path.join(OUT, "raw.jsonl")
    return sum(1 for _ in open(p, encoding="utf-8")) if os.path.exists(p) else 0


def _progress(**kw):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "progress.json")
    cur = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    cur.update(kw)
    cur["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(cur, open(p, "w", encoding="utf-8"), ensure_ascii=True, indent=1)


def s_status(a):
    """查看一次检测的进度：已完成的步骤、正在运行的步骤、已发请求数、是否已生成评分。检测进行中可随时运行。"""
    p = os.path.join(OUT, "progress.json")
    pr = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    R = results()
    print(f"[status] 结果目录 {OUT}")
    if pr:
        print(f"[status] 档位 {pr.get('tier')}，开始于 {pr.get('started')}，最近更新 {pr.get('updated')}"
              + (f"，已于 {pr['finished']} 完成" if pr.get("finished") else ""))
        print(f"[status] 预计 {pr.get('est_requests')} 次请求、{pr.get('est_minutes')} 分钟；已发 {_nreq()} 次请求")
        for d in pr.get("done", []):
            print(f"[status]   ✓ {d['step']}  用时 {d['minutes']} 分钟、{d.get('requests')} 次请求" + ("（出错）" if d.get("error") else ""))
        if pr.get("current"):
            print(f"[status]   … 正在运行 {pr['current']}（开始于 {pr.get('current_started')}）")
    else:
        print(f"[status] 没有 progress.json（不是用 run 启动的）；已发 {_nreq()} 次请求")
    print(f"[status] results.json 中已有的结果：{[k for k in R if not k.startswith('_')]}")
    print(f"[status] 证据评分：{'已生成 SCORE.md' if os.path.exists(os.path.join(OUT, 'SCORE.md')) else '尚未生成'}")


def s_run(a, dispatch):
    steps = list(TIERS["quick"])
    if a.tier in ("standard", "deep"):
        steps += TIERS["standard"]
    if a.tier == "deep":
        steps += TIERS["deep"]
    est = [est_requests(c, kw) for c, kw in steps]
    n_req, lo, hi = sum(e[0] for e in est), sum(e[1] for e in est), sum(e[2] for e in est)
    t0, hi_start = time.time(), None
    print(f"[run] 档位 {a.tier}，共 {len(steps)} 步；结果目录 {OUT}", flush=True)
    print(f"[run] 预计约 {n_req} 次请求，耗时约 {lo:.0f}～{hi:.0f} 分钟（2 并发，按以往实测估算；中转越慢越接近上限）", flush=True)
    print("[run] 进行中可另开终端运行 python scripts/claude_probe.py status 查看进度", flush=True)
    _progress(tier=a.tier, steps=[c for c, _ in steps], started=time.strftime("%Y-%m-%d %H:%M:%S"), finished=None,
              est_requests=n_req, est_minutes=f"{lo:.0f}~{hi:.0f}", done=[], current=None)
    done = []
    for i, ((cmd, kw), (n, elo, ehi)) in enumerate(zip(steps, est), 1):
        b = copy.copy(a)
        for k, v in kw.items():
            setattr(b, k, v)
        if cmd == "bench" and not getattr(b, "baseline", ""):
            bp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "baseline_verified_20260929.json")
            if os.path.exists(bp):
                b.baseline = bp
        ts, r0 = time.time(), _nreq()
        print(f"[run] [{i}/{len(steps)}] {cmd} 开始（约 {n} 次请求、{elo:.0f}～{ehi:.0f} 分钟）；已用 {(ts - t0) / 60:.1f} 分钟 {kw or ''}", flush=True)
        _progress(current=f"[{i}/{len(steps)}] {cmd}", current_started=time.strftime("%H:%M:%S"))
        err = None
        try:
            dispatch[cmd](b)
        except Exception as e:                                # 单步失败不影响后续步骤，记为无法判定
            err = repr(e)
            print(f"[run] {cmd} 失败：{e!r}", flush=True)
        mins = round((time.time() - ts) / 60, 1)
        done.append({"step": f"[{i}/{len(steps)}] {cmd}", "minutes": mins, "requests": _nreq() - r0, "error": err})
        _progress(done=done, current=None)
        print(f"[run] [{i}/{len(steps)}] {cmd} 完成，用时 {mins} 分钟、{_nreq() - r0} 次请求；累计 {(time.time() - t0) / 60:.1f} 分钟", flush=True)
        if cmd == "baseline":
            hi_start = (results().get("baseline", {}).get("injection", {}).get("hi_x4") or [None])[0]
    # 结尾复测：hi 计数与替换指纹
    print("[run] 结尾复测稳定性（hi 计数 + 替换指纹）", flush=True)
    r = ask(a.model, [{"role": "user", "content": "hi"}], 16, stream=False, tag="run_end_hi", **LOW)
    hi_end = tin(r["msg"]["usage"]) if r["status"] == 200 else None
    before = results().get("substitution", {}).get("claimed_consistent")
    try:
        dispatch["substitution"](copy.copy(a))
    except Exception:
        pass
    after = results().get("substitution", {}).get("claimed_consistent")
    save("stability", {"hi_start": hi_start, "hi_end": hi_end, "sub_start_consistent": before, "sub_end_consistent": after,
                       "ended": time.strftime("%Y-%m-%d %H:%M:%S")})
    s_score(a)
    _progress(current=None, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"[run] 全部完成，总用时 {(time.time() - t0) / 60:.1f} 分钟、{_nreq()} 次请求", flush=True)
