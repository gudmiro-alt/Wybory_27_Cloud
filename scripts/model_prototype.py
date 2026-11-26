#!/usr/bin/env python3
"""Prototyp modelu: filtr Kalmana (lokalny trend z tłumieniem) na surowych sondażach z korektą pracowni, rozłamami jako blokami
(PiS+R+, KONF+KPP, TD→PSL+PL2050), backtestem i kalibracją rozrzutu. Uruchom: python model_prototype.py polls.json"""
import json, sys, math, datetime as dt, collections, itertools
import numpy as np

OTHER = 1.6; TOT = 100 - OTHER
BREAK = "2026-07-17"                       # pierwszy sondaż z R+ podanym (niehipotetycznym)
D = dt.date.fromisoformat
D0 = dt.date(2023, 10, 1)
wk = lambda d: (d - D0).days // 7

def load(path):
    P = [p for p in json.load(open(path, encoding="utf-8"))["polls"] if not p["election"]]
    for p in P:
        a, b = D(p["date_start"]), D(p["date_end"]); p["mid"] = a + (b - a) / 2
        p["v"] = dict(p["values"])
        if p["hypothetical"]: p["v"].pop("R+", None); p["rp"] = False          # R+ hipotetyczne traktujemy jak brak R+
        else: p["rp"] = "R+" in p["v"]
    P = [p for p in P if 80 <= sum(p["v"].values()) <= 110]
    return sorted(P, key=lambda p: p["mid"])

def razem_impute(P):
    obs = [(p["mid"], p["v"]["RAZEM"]) for p in P if "RAZEM" in p["v"]]
    for p in P:
        if "RAZEM" not in p["v"]:
            near = [v for d, v in obs if abs((d - p["mid"]).days) <= 60] or [v for _, v in obs]
            p["v"]["RAZEM"] = float(np.median(near))

def blocks(p):
    """Seria po normalizacji do TOT: KO, LEWICA, RAZEM oraz bloki BP=PiS+R+, BK=KONF+KPP, BT=TD albo PSL+PL2050."""
    v = p["v"]; g = lambda k: v.get(k, 0.0)
    td = g("TD") + g("PSL") + g("PL2050")
    s = {"KO": g("KO"), "LEWICA": g("LEWICA"), "RAZEM": g("RAZEM"), "BP": g("PIS") + g("R+"), "BK": g("KONF") + g("KPP"), "BT": td}
    S = sum(s.values()); k = TOT / S
    for key in s: s[key] *= k
    kids = {"R+": g("R+") * k, "KPP": g("KPP") * k, "PL2050": g("PL2050") * k}
    return s, kids

def prepare(P):
    razem_impute(P)
    for p in P: p["s"], p["kids"] = blocks(p)
    # pary: ten sam sondaż z R+ i bez R+ -> korekta definicji dla sondaży sprzed rozłamu
    grp = collections.defaultdict(list)
    for p in P: grp[(p["pollster_id"], p["date_end"], p["n"])].append(p)
    diffs = collections.defaultdict(list); drop = set()
    for g in grp.values():
        w = [x for x in g if x["rp"]]; wo = [x for x in g if not x["rp"]]
        if w and wo:
            for k in w[0]["s"]: diffs[k].append(w[0]["s"][k] - wo[0]["s"][k])
            drop.add(id(wo[0]))
    delta = {k: float(np.mean(v)) for k, v in diffs.items()}
    se = {k: float(np.std(v, ddof=1) / math.sqrt(len(v))) for k, v in diffs.items()}
    P2 = []
    for p in P:
        if id(p) in drop: continue
        if not p["rp"]:
            for k in p["s"]: p["s"][k] += delta[k]
        P2.append(p)
    return P2, delta, se, len(diffs["KO"])

def kern_mean(obs, d, others, half=21, minn=3):
    xs = [(y, q) for (dd, y, q) in obs if abs((dd - d).days) <= half and q != others]
    return float(np.mean([y for y, _ in xs])) if len(xs) >= minn and len({q for _, q in xs}) >= 2 else None

def house(obs):
    res = collections.defaultdict(list)
    for (d, y, q) in obs:
        m = kern_mean(obs, d, q)
        if m is not None: res[q].append(y - m)
    h = {q: float(np.mean(r)) * len(r) / (len(r) + 4) for q, r in res.items()}
    mu = np.mean(list(h.values())) if h else 0
    return {q: v - mu for q, v in h.items()}

def series(P, key, kid=False, since=None):
    obs = []
    for p in P:
        if since and p["mid"] < D(since): continue
        if kid:
            if key == "R+" and not p["rp"]: continue
            if key == "KPP" and "KPP" not in p["v"]: continue
            if key == "PL2050" and "PL2050" not in p["v"]: continue
            y = p["kids"][key]
        else: y = p["s"][key]
        obs.append((p["mid"], y, p["pollster_id"], p["n"] or 1000))
    H = house([(d, y, q) for d, y, q, n in obs])
    return [(d, y - H.get(q, 0.0), q, n) for d, y, q, n in obs], H

DESIGN, TAU = 1.5, 0.8
def ovar(y, n): return DESIGN * (max(y, 0.5) / 100) * (1 - max(y, 0.5) / 100) / max(n, 300) * 1e4 + TAU ** 2

def kalman(obs, qm, qb, phi, w_end, level_scale=True):
    obs = [o for o in obs if wk(o[0]) <= w_end]
    if not obs: return None
    byw = collections.defaultdict(list)
    for d, y, q, n in obs: byw[wk(d)].append((y, n))
    w0 = min(byw); x = np.array([np.mean([y for y, _ in byw[w0]]), 0.0]); P = np.diag([9.0, 0.3 ** 2])
    F = np.array([[1, 1], [0, phi]])
    for w in range(w0, w_end + 1):
        if w > w0:
            sc = math.sqrt(max(x[0], 1.5) / 20) if level_scale else 1
            Q = np.diag([(qm * sc) ** 2, (qb * sc) ** 2]); x = F @ x; P = F @ P @ F.T + Q
        for y, n in byw.get(w, []):
            H = np.array([1.0, 0.0]); S = P[0, 0] + ovar(x[0], n); K = P @ H / S
            x = x + K * (y - x[0]); P = P - np.outer(K, H) @ P
    return x, P

def forecast(x, P, h, qm, qb, phi, level_scale=True):
    F = np.array([[1, 1], [0, phi]])
    for _ in range(h):
        sc = math.sqrt(max(x[0], 1.5) / 20) if level_scale else 1
        x = F @ x; P = F @ P @ F.T + np.diag([(qm * sc) ** 2, (qb * sc) ** 2])
    return x[0], P[0, 0]

def realized(obs, d):
    xs = [y for dd, y, q, n in obs if abs((dd - d).days) <= 21]
    return float(np.mean(xs)) if len(xs) >= 3 else None

HS = [4, 13, 26, 39, 52]
def backtest(S, params, origins):
    qm, qb, phi = params; rows = []
    for name, obs in S.items():
        for o in origins:
            r = kalman(obs, qm, qb, phi, wk(o))
            if r is None or len([1 for d, *_ in obs if d <= o]) < 20: continue
            for h in HS:
                tgt = o + dt.timedelta(days=7 * h); y = realized(obs, tgt)
                if y is None: continue
                m, v = forecast(r[0], r[1], h, qm, qb, phi)
                rows.append((name, o, h, m, math.sqrt(v), y))
    return rows

def rmse(rows, h): e = [(m - y) for _, _, hh, m, s, y in rows if hh == h]; return math.sqrt(np.mean(np.square(e))) if e else float("nan")

def main(path):
    P, delta, se, npair = prepare(load(path))
    print(f"Sondaży po przygotowaniu: {len(P)}; par z/bez R+: {npair}")
    print("Korekta definicji (z R+ minus bez R+, pp po normalizacji do 98,4%):", {k: f"{v:+.2f}±{se[k]:.2f}" for k, v in delta.items()})
    S, HE = {}, {}
    for k in ["KO", "LEWICA", "RAZEM", "BP", "BK", "BT"]: S[k], HE[k] = series(P, k)
    origins = [dt.date(2024, 6, 1) + dt.timedelta(days=30 * i) for i in range(0, 18)]
    grid = list(itertools.product([0.08, 0.12, 0.18], [0.0, 0.006, 0.015, 0.03], [0.90, 0.97]))
    res = []
    for params in grid:
        if params[1] == 0.0 and params[2] != 0.90: continue
        rows = backtest(S, params, origins)
        res.append((params, [rmse(rows, h) for h in HS], rows))
    print("\nBacktest: RMSE (pp) prognozy średniej sondaży o h tygodni naprzód; kolumny h=" + ",".join(map(str, HS)))
    for params, r, _ in sorted(res, key=lambda t: t[1][2] + t[1][4])[:8]:
        print(f"  q_poziom={params[0]:.2f} q_trend={params[1]:.3f} phi={params[2]:.2f}: " + " ".join(f"{x:5.2f}" for x in r))
    naive = [t for t in res if t[0][1] == 0.0]
    best_n = min(naive, key=lambda t: t[1][2] + t[1][4])
    print("  BEZ TRENDU (najlepszy):  q_poziom=%.2f:        " % best_n[0][0] + " ".join(f"{x:5.2f}" for x in best_n[1]))
    best = min(res, key=lambda t: t[1][2] + t[1][4]); params, _, rows = best
    # kalibracja rozrzutu: skala kappa(h) tak, by 80% przedziałów pokrywało wynik
    kappa = {}
    for h in HS:
        z = [abs(y - m) / s for _, _, hh, m, s, y in rows if hh == h]
        kappa[h] = max(1.0, float(np.quantile(z, 0.8)) / 1.2816) if z else 1.0
    print("\nNajlepszy model:", params, "| skala przedziałów kappa(h):", {h: round(k, 2) for h, k in kappa.items()})
    print("Pokrycie 80% przedziałów przed kalibracją:", {h: round(float(np.mean([abs(y - m) <= 1.2816 * s for _, _, hh, m, s, y in rows if hh == h])), 2) for h in HS})
    print("Liczba punktów backtestu:", {h: sum(1 for r in rows if r[2] == h) for h in HS})
    # --- prognoza na ~52 tygodnie od ostatniego sondażu ---
    qm, qb, phi = params; last = P[-1]["mid"]; w_end = wk(last); h = 52; kap = kappa[52]
    out = {}
    def run(name, obs, qb_=qb, phi_=phi):
        r = kalman(obs, qm, qb_, phi_, w_end); m, v = forecast(r[0], r[1], h, qm, qb_, phi_)
        out[name] = {"now": float(r[0][0]), "trend_wk": float(r[0][1]), "fc": m, "sd": math.sqrt(v) * kap}
    for k in S: run(k, S[k])
    kids = {}
    for k, since, qb_, ph_ in (("R+", BREAK, 0.0, 0.0), ("KPP", "2025-05-28", qb, phi), ("PL2050", "2025-06-24", qb, phi)):   # R+: tylko poziom (zbyt krótka historia na trend)
        kids[k], _ = series(P, k, kid=True, since=since); run("kid_" + k, kids[k], qb_, ph_)
    f = {}
    for k in ("KO", "LEWICA", "RAZEM"): f[k] = (out[k]["now"], out[k]["fc"], out[k]["sd"])
    comb = {"PIS": ("BP", "R+"), "KONF": ("BK", "KPP"), "PSL": ("BT", "PL2050")}
    for k, (b, c) in comb.items():
        f[k] = (out[b]["now"] - out["kid_" + c]["now"], out[b]["fc"] - out["kid_" + c]["fc"], math.hypot(out[b]["sd"], out["kid_" + c]["sd"]))
    for k in ("R+", "KPP", "PL2050"): f[k] = (out["kid_" + k]["now"], out["kid_" + k]["fc"], out["kid_" + k]["sd"])
    tot_now = sum(v[0] for v in f.values()); tot_fc = sum(v[1] for v in f.values())
    print(f"\nPrognoza na {last + dt.timedelta(days=7*h)} (h={h} tyg.), skala: głosy ważne (suma = {TOT}%). Suma bez normalizacji: teraz {tot_now:.1f}, prognoza {tot_fc:.1f}")
    print("partia   | teraz | prognoza | przedział 80%      | trend(pp/mies.)")
    final = {}
    for k, (now, fc, sd) in f.items():
        fcn = fc * TOT / tot_fc; sdn = sd * TOT / tot_fc
        final[k] = {"now": round(now * TOT / tot_now, 2), "forecast": round(fcn, 2), "lo80": round(fcn - 1.2816 * sdn, 2), "hi80": round(fcn + 1.2816 * sdn, 2)}
        tr = out.get({"PIS": "BP", "KONF": "BK", "PSL": "BT"}.get(k, k), {}).get("trend_wk", float("nan")) * 4.33
        print(f"{k:8s} | {final[k]['now']:5.1f} | {final[k]['forecast']:8.1f} | {final[k]['lo80']:5.1f} – {final[k]['hi80']:5.1f}      | {tr:+.2f}")
    json.dump({"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "last_poll": last.isoformat(), "horizon_weeks": h,
               "params": {"q_level": qm, "q_trend": qb, "phi_week": phi}, "kappa": kappa, "forecast": final,
               "break_delta": {k: [round(delta[k], 2), round(se[k], 2)] for k in delta}},
              open("forecast_model.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

if __name__ == "__main__": main(sys.argv[1] if len(sys.argv) > 1 else "polls.json")
