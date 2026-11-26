#!/usr/bin/env python3
"""Model produkcyjny: polls.json -> forecast.json (+ forecast_history.json). Wymaga numpy.
Użycie: python scripts/model.py [polls.json] [--backfill]. Opis metody: filtr Kalmana (lokalny trend z tłumieniem) na surowych sondażach,
korekta pracowni, rozłamy jako bloki, mandaty D'Hondt w 41 okręgach, symulacja niepewności (Monte Carlo)."""
import json, sys, os, math, datetime as dt
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_prototype import load, prepare, series, kalman, forecast, wk, TOT, BREAK, D

ELECTION = dt.date(2027, 11, 14)          # założenie robocze (data wyborów nie jest jeszcze ustalona) — zmień tutaj
PARAMS = (0.18, 0.015, 0.97)               # q_poziom, q_trend, phi tygodniowe (z backtestu: model_prototype.py)
P_ORDER = ["KO", "PIS", "KONF", "KPP", "LEWICA", "R+", "RAZEM", "PSL", "PL2050"]
BLOC = {"KO": "D", "LEWICA": "D", "PSL": "D", "RAZEM": "D", "PL2050": "D", "PIS": "P", "KONF": "P", "KPP": "P", "R+": "P"}
DIST_M = np.array([12,8,14,12,13,15,12,12,10,9,12,8,14,10,9,10,9,12,20,12,12,11,15,14,12,14,9,7,9,9,12,9,16,8,10,12,9,9,10,8,12])
DIST_P = np.array([[35,20,13,8,6,5,3,3,1],[38,19,12,13,6,4,3,3,1],[38,15,12,6,8,3,6,2,2],[36,17,13,7,7,4,3,3,2],[30,19,14,7,7,4,3,4,1],[21,26,15,11,4,6,3,3,1],[18,29,15,14,4,7,2,4,1],[39,16,13,8,6,4,3,4,1],[42,15,9,6,9,3,5,2,1],[22,27,14,10,5,6,3,4,1],[27,24,13,8,6,5,3,4,1],[25,24,16,9,5,6,3,3,1],[32,18,12,7,8,4,7,3,2],[16,31,16,11,2,7,2,3,1],[17,28,16,10,4,6,3,5,1],[23,25,14,9,5,6,3,5,1],[21,28,15,10,5,6,3,4,1],[19,28,16,11,4,6,3,4,1],[44,11,8,5,11,3,7,2,2],[36,18,12,7,7,4,4,3,2],[34,18,14,9,5,4,3,4,2],[16,31,15,12,3,7,2,3,1],[18,29,16,12,3,7,3,3,1],[21,24,15,10,4,6,3,3,2],[43,14,12,6,8,3,6,2,2],[39,17,14,8,7,4,4,3,2],[29,21,15,8,5,5,3,3,2],[30,21,13,9,6,5,3,3,1],[37,17,13,7,7,4,4,2,2],[31,22,16,8,5,5,3,2,2],[38,18,12,6,7,4,5,2,2],[31,17,11,7,8,4,4,2,1],[21,27,13,11,5,6,3,4,1],[33,20,14,8,6,5,3,4,1],[34,18,14,8,7,4,3,5,1],[29,20,15,9,6,5,3,5,2],[25,22,14,8,7,5,3,4,2],[36,17,14,8,7,4,3,5,2],[45,11,10,5,10,3,7,2,2],[40,18,12,8,7,4,3,3,1],[41,16,11,7,7,4,4,3,1]], float)
NAT_W = (DIST_M[:, None] * DIST_P).sum(0) / 460
MON = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]
COMP = {"KO": [("KO", 1)], "LEWICA": [("LEWICA", 1)], "RAZEM": [("RAZEM", 1)], "PIS": [("BP", 1), ("kid_R+", -1)],
        "KONF": [("BK", 1), ("kid_KPP", -1)], "PSL": [("BT", 1), ("kid_PL2050", -1)],
        "R+": [("kid_R+", 1)], "KPP": [("kid_KPP", 1)], "PL2050": [("kid_PL2050", 1)]}

def kappa(h): return max(1.3, 1.6 - 0.3 * min(h, 52) / 52)   # skala przedziałów z backtestu (maleje z horyzontem)

def seats_mc(sh):
    """sh: (S,9) udziały w głosach ważnych w kolejności P_ORDER -> mandaty (S,9). D'Hondt w 41 okręgach, próg 5%."""
    S = sh.shape[0]; ok = sh >= 5.0; out = np.zeros((S, 9), int)
    for d in range(41):
        M = int(DIST_M[d]); v = np.where(ok, sh * DIST_P[d] / NAT_W, 0.0)
        Q = (v[:, :, None] / np.arange(1, M + 1)[None, None, :]).reshape(S, -1)
        party = np.argpartition(-Q, M - 1, axis=1)[:, :M] // M
        for j in range(9): out[:, j] += (party == j).sum(1)
    return out

class Ser:
    def __init__(self, obs, w_end, params, trend=True):
        self.qm, qb, phi = params
        self.span = (w_end - wk(obs[0][0])) if obs else 0
        self.qb, self.phi = (qb, phi) if trend else (0.0, 0.0)
        self.obs, self.w_end = obs, w_end
        r = kalman(obs, self.qm, self.qb, self.phi, w_end)
        self.x, self.P = r
        self.w0, self.sm = self._smooth()
    def _smooth(self):
        qm, qb, phi = self.qm, self.qb, self.phi
        obs = [o for o in self.obs if wk(o[0]) <= self.w_end]
        import collections
        from model_prototype import ovar
        byw = collections.defaultdict(list)
        for d, y, q, n in obs: byw[wk(d)].append((y, n))
        w0 = min(byw); x = np.array([np.mean([y for y, _ in byw[w0]]), 0.0]); P = np.diag([9.0, 0.09]); F = np.array([[1, 1], [0, phi]])
        xs, Ps, xp, Pp = [], [], [], []
        for w in range(w0, self.w_end + 1):
            if w > w0:
                sc = math.sqrt(max(x[0], 1.5) / 20); x = F @ x; P = F @ P @ F.T + np.diag([(qm * sc) ** 2, (qb * sc) ** 2])
            xp.append(x.copy()); Pp.append(P.copy())
            for y, n in byw.get(w, []):
                H = np.array([1.0, 0.0]); S = P[0, 0] + ovar(x[0], n); K = P @ H / S
                x = x + K * (y - x[0]); P = P - np.outer(K, H) @ P
            xs.append(x.copy()); Ps.append(P.copy())
        sm = [None] * len(xs); sm[-1] = xs[-1]
        for i in range(len(xs) - 2, -1, -1):
            C = Ps[i] @ F.T @ np.linalg.pinv(Pp[i + 1]); sm[i] = xs[i] + C @ (sm[i + 1] - xp[i + 1])
        return w0, sm
    def hist(self, w):
        return float(self.sm[w - self.w0][0]) if self.w0 <= w <= self.w_end else None
    def fc(self, h):
        if h <= 0: return float(self.x[0]), float(self.P[0, 0])
        return forecast(self.x, self.P, h, self.qm, self.qb, self.phi)

def build_series(P, w_end, origin):
    S = {}
    for k in ["KO", "LEWICA", "RAZEM", "BP", "BK", "BT"]:
        obs, _ = series(P, k); S[k] = Ser(obs, w_end, PARAMS)
    for k, since in (("R+", BREAK), ("KPP", "2025-05-28"), ("PL2050", "2025-06-24")):
        if origin < D(since): continue
        obs, _ = series(P, k, kid=True, since=since)
        if len([o for o in obs if wk(o[0]) <= w_end]) < 3: continue
        s0 = Ser(obs, w_end, PARAMS, trend=False)
        S["kid_" + k] = Ser(obs, w_end, PARAMS, trend=(s0.span >= 26))     # trend dla dziecka dopiero po ≥ 26 tygodniach danych
    return S

def party_val(S, party, getter, dBP, w_date):
    parts = COMP[party]; tot = 0.0
    for name, sign in parts:
        if name not in S:
            if party == "PIS" and name == "kid_R+":                       # przed pojawieniem się R+: PiS w definicji sprzed rozłamu
                continue
            if party in ("R+", "KPP", "PL2050"): return None
            continue
        v = getter(S[name]);
        if v is None: return None
        tot += sign * v
    if party == "PIS" and "kid_R+" not in S: tot -= dBP                   # wyłączenie korekty definicji dla starego PiS
    return tot

def months_axis(start=(2025, 11), end=(2027, 11)):
    y, m = start; out = []
    while (y, m) <= end:
        out.append((y, m)); m += 1
        if m == 13: y, m = y + 1, 1
    return out

def run(P, delta, origin, nsims=4000, seed=7, full=True):
    w_end = wk(origin); S = build_series(P, w_end, origin); dBP = delta["BP"]
    he = max(1, wk(ELECTION) - w_end); kap = kappa(he)
    out = {"now": {}, "parties": {}}
    mx = months_axis()
    present = [p for p in P_ORDER if all((n in S) or (p == "PIS") or False for n, _ in COMP[p]) and (p != "R+" or "kid_R+" in S)]
    # chwila obecna (filtr w ostatnim tygodniu)
    nowv = {p: party_val(S, p, lambda s: float(s.x[0]), dBP, None) for p in present}
    ts = sum(v for p, v in nowv.items() if v is not None)
    # ścieżki miesięczne
    for p in present:
        hist, proj, lo, hi = [], [], [], []
        for (y, m) in mx:
            d = ELECTION if (y, m) == (2027, 11) else dt.date(y, m, 15)
            w = wk(d); past = d <= origin
            hv = None
            if past or (y, m) == (origin.year, origin.month):
                hv = party_val(S, p, lambda s: s.hist(min(w, w_end)), dBP, None)
                if p == "PIS" and (y, m) == (2026, 7): hv = None             # lipiec 2026: zmiana definicji — przerwa w linii
                if p == "PIS" and (y, m) < (2026, 7) and "kid_R+" in S: hv = party_val({k: S[k] for k in ("BP",)}, "PIS", lambda s: s.hist(min(w, w_end)), dBP, None)
            hist.append(None if hv is None else round(hv, 2))
            if d > origin:
                h = w - w_end; mv = 0.0; vv = 0.0; okk = True
                for name, sign in COMP[p]:
                    if name not in S: continue
                    a, b = S[name].fc(h); mv += sign * a; vv += b
                if p == "PIS" and "kid_R+" not in S: mv -= dBP
                proj.append(mv); lo.append(math.sqrt(vv) * kappa(h))
            else:
                proj.append(None if (y, m) != (origin.year, origin.month) else nowv[p]); lo.append(0.0 if (y, m) == (origin.year, origin.month) else None)
        out["parties"][p] = {"hist": hist, "proj": proj, "sd": lo}
    # normalizacja ścieżki prognozy do sumy TOT w każdym miesiącu
    n = len(mx)
    for i in range(n):
        vals = [out["parties"][p]["proj"][i] for p in present]
        if all(v is not None for v in vals) and mx[i] > (origin.year, origin.month):
            k = TOT / sum(vals)
            for p in present:
                q = out["parties"][p]; q["proj"][i] *= k; q["sd"][i] *= k
    # symulacja w dniu wyborów (niezależne błędy składowych, potem renormalizacja do TOT)
    rng = np.random.default_rng(seed); names = list(S.keys())
    mean = {k: S[k].fc(he)[0] for k in names}; sd = {k: math.sqrt(S[k].fc(he)[1]) * kap for k in names}
    draw = {k: rng.normal(mean[k], sd[k], nsims) for k in names}
    sims = np.zeros((nsims, 9))
    for j, p in enumerate(P_ORDER):
        if p not in present: continue
        v = np.zeros(nsims)
        for name, sign in COMP[p]:
            if name in S: v += sign * draw[name]
        if p == "PIS" and "kid_R+" not in S: v -= dBP
        sims[:, j] = np.maximum(v, 0)
    sims *= TOT / sims.sum(1, keepdims=True)
    res = {}
    full_seats = full and ("kid_R+" in S)
    if full_seats:
        st = seats_mc(sims)
        medv = np.median(sims, axis=0); medv = medv * TOT / medv.sum()    # punktowy podział mandatów = D'Hondt dla median poparcia (jak w poprzedniej aplikacji; partie < 5% mają 0)
        pt = seats_mc(medv[None, :])[0]
        nowarr = np.array([[ (nowv[p] or 0) if p in present else 0 for p in P_ORDER]]); nowarr *= TOT / nowarr.sum()
        sn = seats_mc(nowarr)[0]
    for j, p in enumerate(P_ORDER):
        if p not in present: continue
        s = sims[:, j]; r = {"now": round(nowv[p] * TOT / ts, 2), "forecast": round(float(np.median(s)), 2),
                             "lo80": round(float(np.quantile(s, .1)), 2), "hi80": round(float(np.quantile(s, .9)), 2), "p_threshold": round(float((s >= 5).mean()), 3)}
        tr = None
        for name, sign in COMP[p]:
            if name in S and sign == 1: tr = float(S[name].x[1]) * 4.33; break
        r["trend_month"] = None if tr is None else round(tr, 2)
        if full_seats:
            q = st[:, j]; r["seats"] = {"now": int(sn[j]), "pt": int(pt[j]), "med": int(np.median(q)), "lo80": int(np.quantile(q, .1)), "hi80": int(np.quantile(q, .9)), "p_any": round(float((q > 0).mean()), 3)}
        res[p] = r
        pp = out["parties"][p]; i = n - 1                                   # ostatni miesiąc = dzień wyborów: przedziały z symulacji
        pp["proj"][i] = r["forecast"]; pp["lo"] = [None] * n; pp["hi"] = [None] * n
        for t in range(n):
            if pp["proj"][t] is not None and pp["sd"][t] is not None:
                pp["lo"][t] = round(pp["proj"][t] - 1.2816 * pp["sd"][t], 2); pp["hi"][t] = round(pp["proj"][t] + 1.2816 * pp["sd"][t], 2)
        pp["lo"][i], pp["hi"][i] = r["lo80"], r["hi80"]
        pp["proj"] = [None if v is None else round(v, 2) for v in pp["proj"]]; del pp["sd"]
    out["election"] = res
    if full_seats:
        dm = sum(st[:, j] for j, p in enumerate(P_ORDER) if BLOC[p] == "D" and p in present); pr = sum(st[:, j] for j, p in enumerate(P_ORDER) if BLOC[p] == "P" and p in present)
        out["majority"] = {"demokratyczny": round(float((dm >= 231).mean()), 3), "prawica": round(float((pr >= 231).mean()), 3),
                           "zadna": round(float(((dm < 231) & (pr < 231)).mean()), 3),
                           "mandaty_D": {"pt": int(sum(pt[j] for j, p in enumerate(P_ORDER) if BLOC[p] == "D")), "med": int(np.median(dm)), "lo80": int(np.quantile(dm, .1)), "hi80": int(np.quantile(dm, .9))},
                           "mandaty_P": {"pt": int(sum(pt[j] for j, p in enumerate(P_ORDER) if BLOC[p] == "P")), "med": int(np.median(pr)), "lo80": int(np.quantile(pr, .1)), "hi80": int(np.quantile(pr, .9))}}
    out["horizon_weeks"] = he; out["kappa"] = round(kap, 2)
    return out

def main(path="polls.json", backfill=False):
    P, delta, se, npair = prepare(load(path)); last = P[-1]["mid"]
    now = run(P, delta, last)
    doc = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "last_poll": last.isoformat(), "election_date": ELECTION.isoformat(),
           "polls_used": len(P), "params": {"q_level": PARAMS[0], "q_trend": PARAMS[1], "phi_week": PARAMS[2]}, "months": [f"{MON[m-1]}.{str(y)[2:]}" for y, m in months_axis()],
           "break_delta": {k: [round(delta[k], 2), round(se[k], 2)] for k in delta}, "bloc": BLOC, **now}
    try:
        old = json.load(open("forecast.json", encoding="utf-8"))
        same = {k: v for k, v in old.items() if k != "generated"} == {k: v for k, v in doc.items() if k != "generated"}
        fresh = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(old["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() < 5 * 3600
    except Exception: same = fresh = False
    if same and fresh and not backfill:
        print("Brak zmian w prognozie (ostatni zapis < 5 h temu)"); return
    json.dump(doc, open("forecast.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # historia prognoz (do oglądania, dokąd zmierza prognoza w czasie)
    try: hist = json.load(open("forecast_history.json", encoding="utf-8"))
    except Exception: hist = []
    def entry(res, d):
        return {"date": d.isoformat(), "horizon_weeks": res["horizon_weeks"],
                "forecast": {p: [v["forecast"], v["lo80"], v["hi80"]] for p, v in res["election"].items()},
                **({"majority": res["majority"]["demokratyczny"], "majority_P": res["majority"]["prawica"],
                    "seats": {p: v["seats"]["med"] for p, v in res["election"].items() if "seats" in v}} if "majority" in res else {})}
    if backfill or not hist:
        hist = []; o = dt.date(2025, 7, 1)
        while o < last - dt.timedelta(days=20):
            lp = max((p["mid"] for p in P if p["mid"] <= o), default=None)
            if lp:
                r = run(P, delta, lp, nsims=1500, full=True); hist.append(entry(r, lp))
            o += dt.timedelta(days=28)
    if not hist or hist[-1]["date"] != last.isoformat(): hist.append(entry(now, last))
    else: hist[-1] = entry(now, last)
    json.dump(hist, open("forecast_history.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({p: {k: v for k, v in r.items() if k != "seats"} | {"seats": r.get("seats", {}).get("med")} for p, r in now["election"].items()}, ensure_ascii=False))
    print("większość:", now.get("majority")); print("historia:", len(hist), "wpisów")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "polls.json", "--backfill" in sys.argv)
