#!/usr/bin/env python3
"""
Chollometro pipeline: Biwenger market data + FutbolFantasy roles/injuries -> biwenger.html
Stdlib only (urllib/re/json/unicodedata) so it runs anywhere with Python 3, no pip install needed.
Run from the repo root: python3 update.py
Writes biwenger.html next to this script.
"""
import json
import re
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

SLUGS = ["alaves","athletic","atletico","barcelona","betis","celta","deportivo","elche",
    "espanyol","getafe","levante","malaga","osasuna","racing","rayo-vallecano","real-madrid",
    "real-sociedad","sevilla","valencia","villarreal"]

CAT_MAP = {"dios":"Dios","clave":"Clave","importantes":"Importante","rotacion":"Rotación",
    "revulsivos":"Revulsivo","reservas":"Reserva","descarte2":"Descarte"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def normalize(s):
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", str(s))
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    stripped = unicodedata.normalize("NFC", stripped).lower()
    stripped = re.sub(r"[^a-z0-9]+", " ", stripped).strip()
    return re.sub(r"\s+", " ", stripped)


def main():
    print("[1/6] Fetching Biwenger data...")
    biw_data = json.loads(fetch("https://cf.biwenger.com/api/v2/competitions/la-liga/data?lang=es&score=1"))["data"]
    biw_market = json.loads(fetch("https://cf.biwenger.com/api/v2/competitions/la-liga/market?interval=day&includeValues=true"))["data"]

    teams = {}
    for tid, t in biw_data["teams"].items():
        diff = None
        games = t.get("nextGames") or []
        if games:
            g = games[0]
            diff = g["home"]["difficulty"]["rating"] if g["home"]["id"] == t["id"] else g["away"]["difficulty"]["rating"]
        teams[str(t["id"])] = {"name": t["name"], "nextDiff": diff}

    players = []
    for pid, p in biw_data["players"].items():
        ti = teams.get(str(p.get("teamID")))
        team_name = ti["name"] if ti else "Libre"
        next_diff = ti["nextDiff"] if ti else None
        players.append({
            "id": p["id"], "name": p["name"], "team": team_name, "nextDiff": next_diff,
            "pos": p["position"], "price": p["price"], "inc": p["priceIncrement"],
            "ptsLS": p.get("pointsLastSeason"), "status": p["status"],
        })
    vals = biw_market["values"][-60:]
    mc_line = json.dumps(vals, separators=(",", ":"))
    print(f"  -> {len(players)} players")

    print("[2/6] Fetching FutbolFantasy team hierarchies (20 teams)...")
    header_re = re.compile(
        r'<img width="30" class="mr-2" src="https://static\.futbolfantasy\.com/uploads/images/(?P<img>[a-z0-9]+)\.png">\s*(?P<cat>[^<]+?)\s*</header>',
        re.S)
    player_re = re.compile(
        r'href="https://www\.futbolfantasy\.com/jugadores/(?P<slug>[a-z0-9\-]+)" class="jugador">\s*(?P<name>[^<]+?)\s*</a>\s*<span class="comentario">\s*<span>(?P<pos>[^<]*)</span>',
        re.S)
    end_marker_re = re.compile(r"Once tipo y mapa rotacional")

    roles_by_team = {}
    for slug in SLUGS:
        html = fetch(f"https://www.futbolfantasy.com/laliga/equipos/{slug}/jerarquias")
        headers = list(header_re.finditer(html))
        end_m = end_marker_re.search(html)
        end_pos = end_m.start() if end_m else len(html)
        lst = []
        for i, h in enumerate(headers):
            cat_key = h.group("img")
            if cat_key not in CAT_MAP:
                continue
            cat = CAT_MAP[cat_key]
            start = h.end()
            stop = headers[i + 1].start() if i + 1 < len(headers) else end_pos
            if stop <= start:
                continue
            chunk = html[start:stop]
            for m in player_re.finditer(chunk):
                nm = m.group("name").strip()
                lst.append({"name": nm, "norm": normalize(nm), "cat": cat})
        roles_by_team[slug] = lst
        print(f"  -> {slug}: {len(lst)} players")

    print("[3/6] Fetching FutbolFantasy injuries...")
    inj_html = fetch("https://www.futbolfantasy.com/laliga/lesionados")
    parts = re.split(r'<div class="elemento lesionado col-12">', inj_html)
    name_re = re.compile(r'href="https://www\.futbolfantasy\.com/jugadores/[a-z0-9\-]+" class="jugador">(?P<name>[^<]+?)</a>', re.S)
    injury_re = re.compile(r'<span class="lesion">(?P<injury>[^<]*)</span>', re.S)
    days_re = re.compile(r'fa-calendar"></i>\s*(?P<days>[^<]*)</span>', re.S)
    grav_re = re.compile(r'class="gravedad-(?P<grav>[0-9])">(?P<statustxt>[^<]*)</span>', re.S)
    prob_re = re.compile(r'class="prob-[0-9][a-z]?\s*[a-z0-9\-]*">(?P<prob>[0-9]+)%', re.S)

    inj_by_norm = {}
    for chunk in parts[1:]:
        nm = name_re.search(chunk)
        if not nm:
            continue
        name = nm.group("name").strip()
        n = normalize(name)
        if n in inj_by_norm:
            continue
        inj = injury_re.search(chunk)
        days = days_re.search(chunk)
        grav = grav_re.search(chunk)
        prob = prob_re.search(chunk)
        inj_by_norm[n] = {
            "injury": inj.group("injury").strip() if inj else "",
            "days": days.group("days").strip() if days else "",
            "grav": grav.group("grav") if grav else None,
            "statustxt": grav.group("statustxt").strip() if grav else "",
            "prob": prob.group("prob") if prob else None,
        }
    print(f"  -> {len(inj_by_norm)} injury/doubt entries")

    print("[4/6] Matching FutbolFantasy data to Biwenger players...")

    def team_slug(team):
        n = normalize(team)
        for s in SLUGS:
            if s.replace("-", " ") == n:
                return s
        return None

    def find_role_match(bw_norm, slug):
        if not slug or slug not in roles_by_team:
            return None
        cands = roles_by_team[slug]
        for c in cands:
            if c["norm"] == bw_norm:
                return c
        bw_last = bw_norm.split(" ")[-1]
        if len(bw_last) > 2:
            for c in cands:
                if c["norm"].split(" ")[-1] == bw_last:
                    return c
        for c in cands:
            if bw_norm in c["norm"] or c["norm"] in bw_norm:
                return c
        bw_first = bw_norm.split(" ")[0]
        if len(bw_first) > 2:
            for c in cands:
                if c["norm"].split(" ")[0] == bw_first:
                    return c
        return None

    def find_injury_match(bw_norm):
        if bw_norm in inj_by_norm:
            return inj_by_norm[bw_norm]
        bw_last = bw_norm.split(" ")[-1]
        if len(bw_last) <= 3:
            return None
        found = [v for k, v in inj_by_norm.items() if k.split(" ")[-1] == bw_last]
        return found[0] if len(found) == 1 else None

    role_matches = 0
    inj_matches = 0
    rows = []
    for p in players:
        bw_norm = normalize(p["name"])
        slug = team_slug(p["team"])
        role_match = find_role_match(bw_norm, slug)
        role = None
        if role_match:
            role_matches += 1
            role = role_match["cat"]
        inj_match = find_injury_match(bw_norm)
        injury_txt = days_txt = grav_v = status_txt = prob_v = None
        if inj_match:
            inj_matches += 1
            injury_txt = inj_match["injury"]
            days_txt = inj_match["days"]
            grav_v = int(inj_match["grav"]) if inj_match["grav"] is not None else None
            status_txt = inj_match["statustxt"]
            prob_v = int(inj_match["prob"]) if inj_match["prob"] is not None else None
        rows.append([
            p["id"], p["name"], p["team"], p["pos"], p["price"], p["inc"], p["ptsLS"], p["status"],
            p["nextDiff"], role, injury_txt, days_txt, grav_v, status_txt, prob_v,
        ])
    players_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    print(f"  -> role matches: {role_matches} / injury matches: {inj_matches}")

    print("[5/6] Building HTML...")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    font600 = (ROOT / "oswald-600.b64").read_text(encoding="utf-8").strip()
    font700 = (ROOT / "oswald-700.b64").read_text(encoding="utf-8").strip()
    html = (template
            .replace("__PLAYERS__", players_json)
            .replace("__MARKETCAP__", mc_line)
            .replace("__FONT600__", font600)
            .replace("__FONT700__", font700))
    out_path = ROOT / "biwenger.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  -> wrote {out_path} ({out_path.stat().st_size} bytes)")

    print("[6/6] Done. Publish this file as the artifact to update the live dashboard.")


if __name__ == "__main__":
    main()
