"""probe_tasks.py — 固定种子、程序生成、程序判分的题库。
与 audit/tasks.py（已验证基线所用题库）逐字一致：标准档 make_suite("20260929", 6)、困难档 make_suite("20260929", 4, True)。
"""
import random
from probe_lib import NL, answer, ints

SUF = ("\n\nYou have no tools and cannot run code; work it out yourself. End your response with a final line in exactly "
       "this format:\nANSWER: <value>")


def g_trace(rng, lo=14, hi=20):
    M = rng.choice([97, 101, 103, 107, 109, 113, 127])
    a, b, c = rng.randrange(M), rng.randrange(M), rng.randrange(M)
    n, k = rng.randint(lo, hi), rng.randint(3, 6)
    m = lambda: rng.randint(2, 9)
    s1 = rng.choice([f"a = (a * {m()} + c) % {M}", f"a = (a + b * i) % {M}", f"a = (a ^ (b + i)) % {M}"])
    s2 = rng.choice([f"b = (b - a + {m()}) % {M}", f"b = (b * {m()} + i) % {M}", f"b = (b + c * {m()}) % {M}"])
    s3 = rng.choice([f"c = (c + a + i) % {M}", f"c = (c * {m()} - b) % {M}", f"c = (c ^ a) % {M}"])
    tail = rng.choice([f"c = (c + 1) % {M}", "a, b = b, a", f"b = (b + i) % {M}"])
    src = (f"a, b, c = {a}, {b}, {c}\nfor i in range(1, {n + 1}):\n    if (a + i) % {k} == 0:\n        {s1}\n"
           f"    elif b > c:\n        {s2}\n    else:\n        {s3}\n    {tail}\nprint(a, b, c)")
    env = {}
    exec(src.replace("print(a, b, c)", "_r = (a, b, c)"), {}, env)
    return ("What does this Python 3 program print? (Python semantics: % with a positive modulus is non-negative; ^ is bitwise XOR.)\n\n"
            f"```python\n{src}\n```" + SUF.replace("<value>", "<a> <b> <c>")), " ".join(map(str, env["_r"])), "ints"


def g_count(rng, i, hard=False):
    if i % 2 == 0:
        p, N = rng.choice([13, 17, 29, 37, 41, 53] if hard else [13, 17, 29, 37, 41]), rng.randint(*((6000, 12000) if hard else (1200, 3000)))
        ans = sum(1 for a in range(1, N + 1) for b in range(1, N // a + 1) if (a * a + b * b) % p == 0)
        q = f"Find the number of ordered pairs of positive integers (a, b) with a*b <= {N} such that a^2 + b^2 is divisible by {p}."
    elif hard:
        p, N = rng.choice([7, 13, 19, 31, 37]), rng.randint(4000, 8000)
        ans = sum(1 for a in range(1, N + 1) for b in range(1, N // a + 1) if (a * a + a * b + b * b) % p == 0)
        q = f"Find the number of ordered pairs of positive integers (a, b) with a*b <= {N} such that a^2 + a*b + b^2 is divisible by {p}."
    else:
        p, N = rng.choice([7, 13, 19, 31, 37]), rng.randint(200, 600)
        ans = sum(1 for a in range(1, N + 1) for b in range(a + 1, N + 1) if (a * a + a * b + b * b) % p == 0)
        q = f"Find the number of pairs of integers (a, b) with 1 <= a < b <= {N} such that a^2 + a*b + b^2 is divisible by {p}."
    return q + SUF, str(ans), "int"


def g_knap(rng):
    n = 12
    w = [rng.randint(4, 30) for _ in range(n)]; v = [rng.randint(8, 70) for _ in range(n)]
    cap = int(sum(w) * rng.uniform(0.35, 0.5)); dp = [0] * (cap + 1)
    for wi, vi in zip(w, v):
        for c in range(cap, wi - 1, -1):
            dp[c] = max(dp[c], dp[c - wi] + vi)
    tab = NL.join(f"item {j + 1}: weight {w[j]}, value {v[j]}" for j in range(n))
    return (f"0/1 knapsack. Capacity = {cap}. Each item can be taken at most once.{NL}{tab}{NL}{NL}"
            "What is the maximum total value achievable without exceeding the capacity?" + SUF), str(dp[cap]), "int"


def g_chain(rng, n_lines=3000, hops=12):
    names, lines, val = set(), [], {}
    def new():
        while True:
            x = f"v{rng.randint(10000, 99999)}"
            if x not in names: names.add(x); return x
    def make(length):
        cur = new(); val[cur] = rng.randint(10, 99); lines.append(f"{cur} = {val[cur]}"); muls = 0
        for _ in range(length):
            nv, r = new(), rng.random()
            if r < 0.15 and muls < 2: lines.append(f"{nv} = {cur} * 2"); val[nv] = val[cur] * 2; muls += 1
            elif r < 0.6: k = rng.randint(1, 50); lines.append(f"{nv} = {cur} + {k}"); val[nv] = val[cur] + k
            else: k = rng.randint(1, 50); lines.append(f"{nv} = {cur} - {k}"); val[nv] = val[cur] - k
            cur = nv
        return cur
    target = make(hops)
    while len(lines) < n_lines: make(rng.randint(2, 14))
    rng.shuffle(lines)
    return ("Below is a list of variable assignments. Each variable is assigned exactly once; the lines are in random order."
            f"{NL}{NL}" + NL.join(lines) + f"{NL}{NL}What is the integer value of {target}?" + SUF), str(val[target]), "int"


def g_mul(rng, d=15):
    a, b = rng.randint(10 ** (d - 1), 10 ** d - 1), rng.randint(10 ** (d - 1), 10 ** d - 1)
    return f"Compute {a} × {b} exactly." + SUF, str(a * b), "int"


STD_FAMS = ("trace", "count", "knap", "chain", "mul")
HARD_FAMS = ("trace", "count", "chain", "mul")   # 困难档不含 knap，与附录 B 基线一致


def make_suite(seed, per, hard=False, fams=STD_FAMS):
    out = []
    for fam in fams:
        for i in range(per if fam != "chain" or not hard else min(per, 3)):
            rng = random.Random(f"hard-{seed}-{fam}-{i}" if hard else f"{seed}-{fam}-{i}")   # 与基线数据的生成方式逐字一致
            if fam == "trace": p, a, k = g_trace(rng, *((40, 48) if hard else (14, 20)))
            elif fam == "count": p, a, k = g_count(rng, i, hard)
            elif fam == "knap": p, a, k = g_knap(rng)
            elif fam == "chain": p, a, k = g_chain(rng, *((10000, 25) if hard else (3000, 12)))
            else: p, a, k = g_mul(rng, 25 if hard else 15)
            out.append({"id": f"{'H' if hard else ''}{fam}-{i}", "family": fam, "prompt": p, "answer": a, "kind": k})
    return out


def score(task, text):
    got = answer(text)
    if got is None: return False, None
    g = ints(got)
    return (g[:3] == ints(task["answer"])) if task["kind"] == "ints" else (bool(g) and g[0] == int(task["answer"])), got
