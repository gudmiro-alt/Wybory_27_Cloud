#!/usr/bin/env python3
"""Zbiera surowe sondaże parlamentarne z ewybory.eu -> polls.json oraz raport zakresu danych polls_report.md.
Uruchamiane w GitHub Actions. Nic nie poprawia ani nie normalizuje: zapisuje dane takie, jakie są na stronie, z flagami."""
import json, re, sys, collections, datetime as dt
import requests
from bs4 import BeautifulSoup

URL = "https://ewybory.eu/sondaze/polska/"
OUT, REPORT = "polls.json", "polls_report.md"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PrognozaBot/1.0)"}
PARTIES = ["KO", "PIS", "KONF", "KPP", "LEWICA", "RAZEM", "PSL", "PL2050", "R+", "TD"]

def code_of(h):
    h = h.strip()
    if "KKP" in h: return "KPP"
    if h.startswith("Razem"): return "RAZEM"
    if h.startswith("Lewica"): return "LEWICA"
    if h == "KO": return "KO"
    if "2050" in h: return "PL2050"
    if h == "PSL": return "PSL"
    if h.startswith("R+"): return "R+"
    if h.startswith("PiS"): return "PIS"
    if h.startswith("Konfederacja"): return "KONF"
    if h == "TD" or h.startswith("TD"): return "TD"
    return None

def parse_cell(raw):
    """-> (wartość|None, hipotetyczna?, notatka|None). '—' lub puste = brak; '*2.1**' = partia hipotetyczna."""
    t = raw.replace("\xa0", " ").strip()
    m = re.search(r"\d+(?:[.,]\d+)?", t)
    if not m: return None, False, None
    note = re.search(r"[A-Za-z]+\d*\+[A-Za-z0-9]+", t)
    return float(m.group().replace(",", ".")), "*" in t, (note.group() if note else None)

def parse_range(s, end_year):
    s = s.strip().strip(".")
    full = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if full:
        d = dt.date(int(full[3]), int(full[2]), int(full[1])); return d, d, True
    l, r = (s.split("-", 1) if "-" in s else (None, s))
    rd, rm = [int(x) for x in r.split(".")[:2]]
    end = dt.date(end_year, rm, rd)
    if l:
        p = l.split("."); ld = int(p[0]); lm = int(p[1]) if len(p) > 1 and p[1] else rm
        start = dt.date(end_year - 1 if lm > rm else end_year, lm, ld)
    else:
        start = end
    return start, end, False

CANON = [("united surveys", "United Surveys"), ("research partner", "Research Partner"), ("resarch parnert", "Research Partner"),
         ("pollster", "Pollster"), ("ibris", "IBRiS"), ("opinia24", "Opinia24"), ("cbos", "CBOS"), ("ogb", "OGB"),
         ("ipsos", "Ipsos"), ("arc rynek", "ARC Rynek i Opinia"), ("cbm", "CBM Indicator"), ("ibsp", "IBSP")]
def canon(name):
    n = name.lower()
    for k, v in CANON:
        if k in n: return v
    return name.split(" / ")[0].strip()

SPAN = object()   # znacznik: kolejna kolumna zajęta przez scaloną komórkę (colspan)
txt = lambda c: "" if c is SPAN else c.get_text(" ", strip=True)

def expand(table):
    """Rozwija rowspan/colspan, żeby każdy wiersz miał tyle komórek co nagłówek."""
    rows, pending = [], {}
    for tr in table.find_all("tr"):
        it, out, ci = iter(tr.find_all(["th", "td"])), [], 0
        while True:
            if ci in pending:
                out.append(pending[ci][1]); pending[ci][0] -= 1
                if pending[ci][0] == 0: del pending[ci]
                ci += 1; continue
            c = next(it, None)
            if c is None: break
            rs = int(c.get("rowspan", 1) or 1); cs = int(c.get("colspan", 1) or 1)
            for k in range(cs):
                item = c if k == 0 else SPAN
                out.append(item)
                if rs > 1: pending[ci] = [rs - 1, item]
                ci += 1
        rows.append(out)
    return rows

def parse_meta(cell):
    a = cell.find("a")
    url = a["href"] if a is not None and a.get("href", "#") not in ("#", "") else None
    parts = [p.strip() for p in cell.get_text("|", strip=True).split("|") if p.strip()]
    text = " ".join(parts)
    name = re.split(r"\s+N\s*=", parts[0])[0].strip() if parts else ""
    mn = re.search(r"N\s*=\s*([0-9][0-9 ]*)", text)
    md = re.search(r"(Termin|Pub\.)\s*:\s*([0-9.\-]+)", text)
    return {"url": url, "name": name, "n": int(mn.group(1).replace(" ", "")) if mn else None,
            "kind": ("pub" if (md and md.group(1).startswith("Pub")) or "(pub.)" in text else "fieldwork"),
            "date": md.group(2) if md else None, "election": name.lower().startswith("wybory")}

def parse_page(html, today=None):
    today = today or dt.date.today()
    soup = BeautifulSoup(html, "html.parser")
    polls, bad = [], []
    year, prev_m, first = today.year, None, True
    for t in soup.find_all("table"):
        rows = expand(t)
        if not rows: continue
        cols = [code_of(txt(c).replace(" ", "")) for c in rows[0]]
        if not {"KO", "PIS"} <= set(cols): continue
        for cells in rows[1:]:
            if len(cells) == len(cols) - 1 and "PSL" in cols:   # brakująca komórka PSL (okres Trzeciej Drogi) = pusta
                cells = cells[:cols.index("PSL")] + [BeautifulSoup("<td></td>", "html.parser").td] + cells[cols.index("PSL"):]
            if len(cells) != len(cols): bad.append("liczba kolumn: " + " ".join(c.get_text(" ", strip=True) for c in cells)[:80]); continue
            meta = parse_meta(cells[0])
            if not meta["date"]: bad.append("brak daty: " + meta["name"]); continue
            try:
                full = re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", meta["date"])
                if first and not full:   # pierwszy wiersz: ustal rok (np. uruchomienie w styczniu)
                    em = int(re.findall(r"\.(\d{1,2})", meta["date"])[-1])
                    if em > today.month + 1: year = today.year - 1
                if not full:
                    em = int(re.findall(r"\.(\d{1,2})", meta["date"])[-1])
                    if prev_m is not None and em - prev_m >= 6: year -= 1
                    prev_m = em
                start, end, explicit = parse_range(meta["date"], year)
                if explicit: year, prev_m = end.year, end.month
                first = False
            except Exception as e:
                bad.append(f"data '{meta['date']}': {e}"); continue
            vals, hyp, notes = {}, [], []
            for i, (code, c) in enumerate(zip(cols, cells)):
                if i == 0 or c is SPAN or not code: continue
                v, h, n = parse_cell(txt(c))
                if v is not None:
                    # komórka PL2050 scalona z kolumną PSL (colspan=2) = wynik Trzeciej Drogi (PL2050+PSL razem)
                    if code == "PL2050" and i + 1 < len(cells) and cells[i + 1] is SPAN and cols[i + 1] == "PSL":
                        code = "TD"; notes.append("TD:PL2050+PSL (komórka scalona)")
                    vals[code] = v
                    if h: hyp.append(code)
                    if n: notes.append(f"{code}:{n}")
            # do 20.06.2025 kolumna „PL2050” przy pustej kolumnie PSL zawiera wynik Trzeciej Drogi (PL2050+PSL razem)
            if "PL2050" in vals and "PSL" not in vals and "PSL" in cols and end.isoformat() < "2025-06-21":
                vals["TD"] = vals.pop("PL2050"); notes.append("TD:z kolumny PL2050")
            polls.append({"date_start": start.isoformat(), "date_end": end.isoformat(), "date_kind": meta["kind"],
                          "pollster": meta["name"], "pollster_id": canon(meta["name"]), "n": meta["n"], "url": meta["url"], "election": meta["election"],
                          "rplus_column": "R+" in cols, "rplus_listed": "R+" in vals,
                          "hypothetical": hyp, "notes": notes, "values": vals})
    return polls, bad

def key(p): return f'{p["date_end"]}|{p["pollster"]}|{p["n"]}|{json.dumps(p["values"], sort_keys=True)}'

def report(polls, bad, prev_count):
    L = []; today = dt.date.today()
    real = [p for p in polls if not p["election"]]
    ds = sorted(p["date_end"] for p in real)
    L += ["# Raport zakresu danych (ewybory.eu)", f"Wygenerowano: {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC",
          f"\n- Sondaży: **{len(real)}** (wyniki wyborów w danych: {len(polls) - len(real)})",
          f"- Zakres dat: **{ds[0]} … {ds[-1]}**" if ds else "- brak danych",
          f"- Wierszy odrzuconych przy odczycie: {len(bad)}; poprzednia liczba w pliku: {prev_count}"]
    by_year = collections.Counter(d[:4] for d in ds)
    L += ["\n## Liczba sondaży wg roku", "| rok | sondaży |", "|---|---|"] + [f"| {y} | {n} |" for y, n in sorted(by_year.items())]
    pc = collections.Counter(p["pollster_id"] for p in real)
    L += ["\n## Pracownie (liczba sondaży)", ", ".join(f"{k}: {v}" for k, v in pc.most_common())]
    L += ["\n## Pokrycie partii", "| partia | pierwszy | ostatni | sondaży |", "|---|---|---|---|"]
    for c in PARTIES:
        d = sorted(p["date_end"] for p in real if c in p["values"])
        L.append(f"| {c} | {d[0] if d else '-'} | {d[-1] if d else '-'} | {len(d)} |")
    L += ["\n## R+ w sondażach wg miesiąca (od 2026)", "| miesiąc | R+ podane | R+ hipotetyczne (*) | bez R+ | razem |", "|---|---|---|---|---|"]
    mo = collections.defaultdict(lambda: [0, 0, 0])
    for p in real:
        if p["date_end"] >= "2026-01" and p["rplus_column"]:
            i = 0 if (p["rplus_listed"] and "R+" not in p["hypothetical"]) else (1 if p["rplus_listed"] else 2)
            mo[p["date_end"][:7]][i] += 1
    for m in sorted(mo): L.append(f"| {m} | {mo[m][0]} | {mo[m][1]} | {mo[m][2]} | {sum(mo[m])} |")
    grp = collections.Counter((p["pollster"], p["date_end"], p["n"]) for p in real)
    L.append(f"\n## Duplikaty (ten sam sondaż w kilku wariantach, np. z i bez R+): {sum(1 for v in grp.values() if v > 1)}")
    sums = [(round(sum(p['values'].values()), 1), p["date_end"], p["pollster"]) for p in real]
    odd = [s for s in sums if s[0] < 80 or s[0] > 102]
    L.append(f"\n## Podejrzane sumy wyników (<80% lub >102%): {len(odd)}")
    L += [f"- {s[1]} {s[2]}: {s[0]}%" for s in odd[:15]]
    mm = collections.defaultdict(set)
    for p in real: mm[p["date_end"][:7]].add(p["pollster_id"])
    L += ["\n## Sondaże i pracownie w ostatnich 24 miesiącach", "| miesiąc | sondaży | pracowni |", "|---|---|---|"]
    cnt = collections.Counter(p["date_end"][:7] for p in real)
    for m in sorted(cnt)[-24:]: L.append(f"| {m} | {cnt[m]} | {len(mm[m])} |")
    if bad: L += ["\n## Odrzucone wiersze (pierwsze 10)"] + [f"- {b}" for b in bad[:10]]
    return "\n".join(L) + "\n"

def main():
    r = requests.get(URL, headers=UA, timeout=40); r.raise_for_status()
    polls, bad = parse_page(r.text)
    try: old = json.load(open(OUT, encoding="utf-8"))["polls"]
    except Exception: old = []
    if len(polls) < 100 or (old and len(polls) < 0.5 * len(old)):
        print(f"Odczytano tylko {len(polls)} sondaży (poprzednio {len(old)}) — nie nadpisuję pliku"); sys.exit(1)
    seen = {key(p) for p in polls}
    lo = min(p["date_end"] for p in polls)   # w zakresie dat odczytanym teraz dane ze strony zastępują stare; starsze wiersze z pliku zostają
    merged = polls + [p for p in old if p["date_end"] < lo]
    merged.sort(key=lambda p: (p["date_end"], p["pollster"]), reverse=True)
    if old and [key(p) for p in merged] == [key(p) for p in old]:
        print("Brak nowych sondaży — pliki bez zmian"); return
    json.dump({"generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "source": URL, "count": len(merged), "polls": merged},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    rep = report(merged, bad, len(old)); open(REPORT, "w", encoding="utf-8").write(rep); print(rep)

if __name__ == "__main__": main()
