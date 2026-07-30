#!/usr/bin/env python3
"""
Chollometro pipeline: Biwenger market data + FutbolFantasy roles/injuries -> biwenger.html
Stdlib only (urllib/re/json/unicodedata) so it runs anywhere with Python 3, no pip install needed.
Run from the repo root: python3 update.py
Writes biwenger.html next to this script.
"""
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
STARTING_BALANCE = 50_000_000
DEFAULT_LEAGUE_ID = "709656"

SLUGS = ["alaves","athletic","atletico","barcelona","betis","celta","deportivo","elche",
    "espanyol","getafe","levante","malaga","osasuna","racing","rayo-vallecano","real-madrid",
    "real-sociedad","sevilla","valencia","villarreal"]

CAT_MAP = {"dios":"Dios","clave":"Clave","importantes":"Importante","rotacion":"Rotación",
    "revulsivos":"Revulsivo","reservas":"Reserva","descarte2":"Descarte"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


# Letters with no canonical NFD decomposition (so accent-stripping alone
# can't fold them) but that still show up in player names, e.g. Sorloth.
NON_DECOMPOSING = str.maketrans({
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ß": "ss",
})


def normalize(s):
    if not s:
        return ""
    translated = str(s).translate(NON_DECOMPOSING)
    nfd = unicodedata.normalize("NFD", translated)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    stripped = unicodedata.normalize("NFC", stripped).lower()
    stripped = re.sub(r"[^a-z0-9]+", " ", stripped).strip()
    return re.sub(r"\s+", " ", stripped)


def fetch_lineup_probabilities(slugs):
    """The team's own front page (not /jerarquias) has a hidden 'Lista' view
    (class 'tipo_lista', toggled client-side, but present server-rendered) with
    one div per squad player carrying data-nombre (their url slug) and
    data-probabilidad. It covers far more of the squad than the 'Campo' pitch
    diagram, which only draws the projected starters + a short bench list."""
    lista_re = re.compile(r'<div class="jugador_\d+ jugador tipo_lista[^"]*"(?P<attrs>.*?)>', re.S)
    nombre_re = re.compile(r'data-nombre="(?P<slug>[a-z0-9\-]+)"')
    prob_re = re.compile(r'data-probabilidad="(?P<prob>\d+)%"')
    prob_by_slug = {}
    for slug in slugs:
        html = fetch(f"https://www.futbolfantasy.com/laliga/equipos/{slug}")
        n = 0
        for m in lista_re.finditer(html):
            attrs = m.group("attrs")
            nm = nombre_re.search(attrs)
            p = prob_re.search(attrs)
            if nm and p:
                prob_by_slug[nm.group("slug")] = int(p.group("prob"))
                n += 1
        print(f"  -> {slug}: {n} con % de titularidad")
    return prob_by_slug


def fetch_auth(url, token, league_id=None, x_user=None):
    headers = {"User-Agent": UA, "Authorization": f"Bearer {token}"}
    if league_id:
        headers["X-League"] = str(league_id)
    if x_user:
        headers["X-User"] = str(x_user)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_league_money(token, league_id, player_names):
    """Read-only: reconstructs each manager's balance since this season's reset
    (45M start) from the public transfer feed, since Biwenger hides other
    managers' live balance when the league's 'balance' privacy setting is on."""
    acct = fetch_auth("https://biwenger.as.com/api/v2/account", token)
    league_entry = next((l for l in acct["data"]["leagues"] if str(l["id"]) == str(league_id)), None)
    if not league_entry:
        return None, None
    my_user_id = str(league_entry["user"]["id"])

    league = fetch_auth(f"https://biwenger.as.com/api/v2/league/{league_id}?fields=*,standings,users",
                         token, league_id, my_user_id)
    users = {str(u["id"]): u["name"] for u in league["data"]["users"]}
    balances = {uid: STARTING_BALANCE for uid in users}
    transfers = []
    HANDLED_TYPES = {"market", "adminTransfer", "transfer", "bonus",
                      "seasonStarted", "seasonFinished",
                      "adminText", "playerMovements", "leagueSettings",
                      "leaguePremium", "ultra", "userLeave", "userName"}
    unhandled_types = set()

    offset = 0
    limit = 20
    done = False
    while not done and offset < 400:
        page = fetch_auth(f"https://biwenger.as.com/api/v2/league/{league_id}/board?offset={offset}&limit={limit}",
                           token, league_id, my_user_id)
        items = page["data"]
        if not items:
            break
        for item in items:
            if item["type"] not in HANDLED_TYPES:
                unhandled_types.add(item["type"])
            if item["type"] in ("seasonStarted", "seasonFinished"):
                done = True
                break
            if item["type"] == "market":
                for c in item.get("content", []):
                    buyer = c.get("to")
                    seller = c.get("from")
                    amount = c.get("amount", 0)
                    buyer_id = str(buyer["id"]) if buyer else None
                    seller_id = str(seller["id"]) if seller else None
                    if buyer_id in balances:
                        balances[buyer_id] -= amount
                    if seller_id in balances:
                        balances[seller_id] += amount
                    transfers.append([
                        item["date"], player_names.get(c.get("player"), "?"),
                        buyer["name"] if buyer else "?", amount,
                        seller["name"] if seller else None,
                    ])
            elif item["type"] == "adminTransfer":
                for c in item.get("content", []):
                    frm = c.get("from")
                    frm_id = str(frm["id"]) if frm else None
                    if frm_id in balances:
                        balances[frm_id] += c.get("amount", 0)
            elif item["type"] == "transfer":
                # Sale, either to the market ("machine" - only 'from') or
                # directly to another manager (both 'from' and 'to').
                for c in item.get("content", []):
                    seller = c.get("from")
                    buyer = c.get("to")
                    amount = c.get("amount", 0)
                    seller_id = str(seller["id"]) if seller else None
                    buyer_id = str(buyer["id"]) if buyer else None
                    if seller_id in balances:
                        balances[seller_id] += amount
                    if buyer_id in balances:
                        balances[buyer_id] -= amount
                    transfers.append([
                        item["date"], player_names.get(c.get("player"), "?"),
                        buyer["name"] if buyer else "Máquina", amount,
                        seller["name"] if seller else None,
                    ])
            elif item["type"] == "bonus":
                for c in item.get("content", []):
                    user = c.get("user")
                    uid = str(user["id"]) if user else None
                    if uid in balances:
                        balances[uid] += c.get("amount", 0)
        if done:
            break
        offset += limit

    if unhandled_types:
        print(f"  -> WARNING: unhandled board event types (money not accounted for): {sorted(unhandled_types)}")

    team_rows = sorted(([uid, users[uid], balances[uid]] for uid in users), key=lambda r: -r[2])
    transfers.sort(key=lambda t: -t[0])
    return team_rows, transfers


def main():
    print("[1/8] Fetching Biwenger data...")
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

    print("[2/8] Fetching FutbolFantasy team hierarchies (20 teams)...")
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
                lst.append({"name": nm, "norm": normalize(nm), "cat": cat, "slug": m.group("slug")})
        roles_by_team[slug] = lst
        print(f"  -> {slug}: {len(lst)} players")

    print("[3/8] Fetching starting-XI probabilities (team pages)...")
    prob_by_slug = fetch_lineup_probabilities(SLUGS)
    print(f"  -> {len(prob_by_slug)} players with a published probability")

    print("[4/8] Fetching FutbolFantasy injuries...")
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

    print("[5/8] Matching FutbolFantasy data to Biwenger players...")

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
    lineup_prob_matches = 0
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
        # The team-page lineup widget covers far more players (not just
        # doubts/injuries) and is more current, so prefer it when both exist.
        if role_match:
            lineup_prob = prob_by_slug.get(role_match["slug"])
            if lineup_prob is not None:
                prob_v = lineup_prob
                lineup_prob_matches += 1
        rows.append([
            p["id"], p["name"], p["team"], p["pos"], p["price"], p["inc"], p["ptsLS"], p["status"],
            p["nextDiff"], role, injury_txt, days_txt, grav_v, status_txt, prob_v,
        ])
    players_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    print(f"  -> role matches: {role_matches} / injury matches: {inj_matches} / lineup probability matches: {lineup_prob_matches}")

    print("[6/8] Fetching league money (Biwenger, read-only)...")
    teams_json, transfers_json = "[]", "[]"
    token = os.environ.get("BIWENGER_TOKEN")
    if token:
        league_id = os.environ.get("BIWENGER_LEAGUE_ID", DEFAULT_LEAGUE_ID)
        player_names = {p["id"]: p["name"] for p in players}
        try:
            team_rows, transfers = fetch_league_money(token, league_id, player_names)
            if team_rows is not None:
                teams_json = json.dumps(team_rows, ensure_ascii=False, separators=(",", ":"))
                transfers_json = json.dumps(transfers, ensure_ascii=False, separators=(",", ":"))
                print(f"  -> {len(team_rows)} managers, {len(transfers)} fichajes esta temporada")
            else:
                print("  -> league not found for this token, skipping")
        except Exception as e:
            print(f"  -> failed ({e}), skipping league money this run")
    else:
        print("  -> BIWENGER_TOKEN not set, skipping league money section")

    print("[7/8] Building HTML...")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    font600 = (ROOT / "oswald-600.b64").read_text(encoding="utf-8").strip()
    font700 = (ROOT / "oswald-700.b64").read_text(encoding="utf-8").strip()
    html = (template
            .replace("__PLAYERS__", players_json)
            .replace("__MARKETCAP__", mc_line)
            .replace("__FONT600__", font600)
            .replace("__FONT700__", font700)
            .replace("__TEAMS__", teams_json)
            .replace("__TRANSFERS__", transfers_json))
    out_path = ROOT / "biwenger.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  -> wrote {out_path} ({out_path.stat().st_size} bytes)")

    print("[8/8] Done. Publish this file as the artifact to update the live dashboard.")


if __name__ == "__main__":
    main()
