"""Pobiera średnie sondażowe z 3 serwisów i zapisuje data.json (czyta je przycisk „Aktualizuj” w HTML)."""
import json, re, sys, datetime as dt, requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; PrognozaBot/1.0)"}
OUT = "data.json"
NAMES = {  # kod partii w HTML -> nazwy używane przez serwis
    "KO": ["KO"], "PIS": ["PiS"], "KONF": ["Konfederacja(?! Korony)"], "KPP": ["Konfederacja Korony Polskiej"],
    "LEWICA": ["Lewica"], "R+": ["Rozwój Plus"], "RAZEM": ["Razem"], "PSL": ["PSL"], "PL2050": ["Polska 2050"]}
PP_NAMES = {"KO": ["KO"], "PIS": ["PiS"], "KONF": ["Kon"], "KPP": ["KKP"], "LEWICA": ["Lewica"],
            "R+": ["Rozwój Plus"], "RAZEM": ["Razem"], "PSL": ["PSL"], "PL2050": ["PL2050"]}

def get(url):
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status(); return r.text

def flat(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))

def find(text, names, tmpl):
    out = {}
    for code, pats in names.items():
        for p in pats:
            m = re.search(tmpl % p, text)
            if m:
                v = float(m.group(1).replace(",", "."))
                if 0.2 < v < 60:
                    out[code] = v; break
    return out

def atlas():
    return find(flat(get("https://atlaspolityczny.pl/sondaze/partie")), NAMES, r"(?<!\w)%s\s+(\d{1,2},\d)\s*%%")

def politpro():
    return find(flat(get("https://politpro.eu/pl/polska/wybory-parlamentarne")), PP_NAMES, r"(?<!\w)%s z (?:wynikiem )?(\d{1,2},\d)%%")

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

def ewybory(days=30, min_polls=5):
    """Własna prosta średnia z surowych sondaży z ostatnich 30 dni (strona nie udostępnia swojej średniej w HTML)."""
    soup = BeautifulSoup(get("https://ewybory.eu/sondaze/polska/"), "html.parser")
    today = dt.date.today(); sums = {}; used = 0
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if not rows: continue
        head = [c.get_text("", strip=True) for c in rows[0].find_all(["th", "td"])]
        cols = [code_of(h) for h in head]
        if not {"KO", "PIS", "KPP", "R+"} <= set(cols): continue
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) != len(head): continue
            m = re.findall(r"(\d{1,2})\.(\d{1,2})", cells[0].split(":", 1)[-1])
            if not m: continue
            d, mo = int(m[-1][0]), int(m[-1][1])
            try: date = dt.date(today.year, mo, d)
            except ValueError: continue
            if date > today: date = date.replace(year=today.year - 1)
            if (today - date).days > days: continue
            used += 1
            for code, c in zip(cols, cells):
                v = re.search(r"\d+(?:\.\d+)?", c)
                if code and v and c.strip() not in ("—", "-"):
                    sums.setdefault(code, []).append(float(v.group()))
        break  # tylko pierwsza (najnowsza) tabela
    if used < min_polls: return {}
    return {k: round(sum(v) / len(v), 2) for k, v in sums.items() if len(v) >= 3}

try: old = json.load(open(OUT, encoding="utf-8"))
except Exception: old = {"sources": {}}
new = dict(old.get("sources", {})); ok = 0; status = {}
for key, fn in (("ewAvg", ewybory), ("apAvg", atlas), ("ppAvg", politpro)):
    try:
        res = fn()
        if len(res) >= 5: new[key] = res; ok += 1; status[key] = "ok"
        else: status[key] = f"odczytano tylko {len(res)} partii"; print(key, status[key])
    except Exception as e:
        status[key] = f"błąd: {str(e)[:80]}"; print(key, status[key])
    print(key, new.get(key))
now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
changed = new != old.get("sources")
json.dump({"updated": now if changed or "updated" not in old else old["updated"], "checked": now, "status": status, "sources": new,
           "info": {"ewAvg": "średnia prosta z sondaży z ostatnich 30 dni (obliczona z tabeli ewybory.eu)",
                    "apAvg": "średnia Atlasu Politycznego", "ppAvg": "Trend wyborczy PolitPro"}},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
sys.exit(0 if ok else 1)
