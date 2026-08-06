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
import time
import unicodedata
import urllib.request
from html import unescape as html_unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
STARTING_BALANCE = 50_000_000
DEFAULT_LEAGUE_ID = "709656"

CUATROPICAS_CATEGORY_URL = "https://cuatropicas.com/categoria/analisis-equipos/"
# El sitio no es consistente: unos autores escriben "90% RECOMENDABLE" y otros
# "RECOMENDABLE 90%" - se acepta cualquiera de los dos ordenes.
CUATROPICAS_RATING_RE = re.compile(r"(\d{1,3})\s*%\s*RECOMENDABLE|RECOMENDABLE\s*[:\-]?\s*(\d{1,3})\s*%", re.I)
CUATROPICAS_NAME_RE = re.compile(r"^[–‒\-]\s*([A-ZÀ-Ü][A-ZÀ-Ü0-9 .,'\-]{1,45}):\s*$")

SLUGS = ["alaves","athletic","atletico","barcelona","betis","celta","deportivo","elche",
    "espanyol","getafe","levante","malaga","osasuna","racing","rayo-vallecano","real-madrid",
    "real-sociedad","sevilla","valencia","villarreal"]

CAT_MAP = {"dios":"Dios","clave":"Clave","importantes":"Importante","rotacion":"Rotación",
    "revulsivos":"Revulsivo","reservas":"Reserva","descarte2":"Descarte"}


def fetch(url):
    # This pipeline makes ~70 external requests in a single run (20 FF team
    # pages x2, injuries, cuatropicas category+articles, Biwenger league
    # calls...) unattended once a day - a single transient timeout/500 on
    # any one of them used to abort the entire run before it ever got to
    # writing the HTML, which is the leading suspect for the automated daily
    # refresh silently going stale for days at a time. One quiet retry after
    # a short pause absorbs most of those blips without changing behavior
    # for the (common) success case.
    last_err = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)
    raise last_err


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
        try:
            html = fetch(f"https://www.futbolfantasy.com/laliga/equipos/{slug}")
        except Exception as e:
            print(f"  -> {slug}: FAILED ({e}), skipping this team for today")
            continue
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


def _strip_html_tags(s):
    return html_unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()


def parse_cuatropicas_article(article_html):
    """Cuatro Picas team-analysis articles write one player per paragraph
    block: a heading paragraph "– NAME:" (nothing else in it), then a couple
    of paragraphs of stats + prose ending in a "NN% RECOMENDABLE" rating
    somewhere in the text. Rather than fight the inconsistent nested-tag
    markup (some names/ratings are wrapped in nested <span>/<strong> that
    split the text across tags), every <p> is tag-stripped to plain text
    first and matched on that - the same lesson learned from FutbolFantasy's
    markup, just worse here since even the tag ORDER varies between authors."""
    start = article_html.find('<div class="entry-content clr"')
    content = article_html[start:start + 200000] if start != -1 else article_html
    paras = [_strip_html_tags(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", content, re.S)]
    name_idxs = [(i, m.group(1)) for i, p in enumerate(paras) if (m := CUATROPICAS_NAME_RE.match(p))]
    entries = []
    n = len(paras)
    for j, (idx, name) in enumerate(name_idxs):
        end = name_idxs[j + 1][0] if j + 1 < len(name_idxs) else n
        comment = " ".join(paras[idx + 1:end]).strip()
        rm = CUATROPICAS_RATING_RE.search(comment)
        if not rm:
            continue
        rating = int(rm.group(1) or rm.group(2))
        entries.append({"name": name.strip(), "norm": normalize(name), "rating": rating, "comment": comment})
    return entries


def fetch_cuatropicas_ratings():
    """Only articles for the CURRENT season (26/27) - Cuatro Picas publishes
    team analyses progressively over pretemporada, so at any given time some
    teams only have last season's article. Those are deliberately skipped
    entirely rather than used as a stand-in, per explicit instruction: this
    season's ratings or nothing. Returns {team_label_from_title: {"url":..., "players":[...]}}."""
    entries = []
    for page in range(1, 4):
        url = CUATROPICAS_CATEGORY_URL if page == 1 else f"{CUATROPICAS_CATEGORY_URL}page/{page}/"
        try:
            page_html = fetch(url)
        except Exception:
            break
        arts = re.split(r'(?=<article id="post-)', page_html)[1:]
        if not arts:
            break
        found_this_page = 0
        for a in arts:
            tm = re.search(r'<h2 class="blog-entry-title entry-title">\s*<a href="([^"]+)"[^>]*>([^<]+)</a>', a)
            if not tm:
                continue
            link, title = tm.groups()
            title = html_unescape(title).strip()
            if "26/27" in title or "26-27" in title:
                entries.append((title, link))
                found_this_page += 1
        if found_this_page == 0 and page > 1:
            break

    ratings_by_team = {}
    for title, link in entries:
        team_label = re.sub(r"^An[aá]lisis\s+", "", title, flags=re.I)
        team_label = re.sub(r"\s*26[/-]27\s*$", "", team_label).strip()
        try:
            article_html = fetch(link)
        except Exception:
            continue
        players = parse_cuatropicas_article(article_html)
        if players:
            ratings_by_team[team_label] = {"url": link, "players": players}
    return ratings_by_team


def cuatropicas_team_matches(cuatro_label, biw_team_name):
    a, b = normalize(cuatro_label), normalize(biw_team_name)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def fetch_auth(url, token, league_id=None, x_user=None):
    headers = {"User-Agent": UA, "Authorization": f"Bearer {token}"}
    if league_id:
        headers["X-League"] = str(league_id)
    if x_user:
        headers["X-User"] = str(x_user)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def fetch_league_money(token, league_id, player_names, player_prices, player_pos):
    """Read-only: reconstructs each manager's balance since this season's reset
    (45M start) from the public transfer feed, since Biwenger hides other
    managers' live balance when the league's 'balance' privacy setting is on.
    Also collects each manager's roster and their bidding history (wins and
    losing bids), and today's live market listing, for the rival-scouting and
    buy-recommendation features. Returns a dict, or None if the league/token
    combination doesn't resolve to a real league."""
    acct = fetch_auth("https://biwenger.as.com/api/v2/account", token)
    league_entry = next((l for l in acct["data"]["leagues"] if str(l["id"]) == str(league_id)), None)
    if not league_entry:
        return None
    my_user_id = str(league_entry["user"]["id"])

    league = fetch_auth(f"https://biwenger.as.com/api/v2/league/{league_id}?fields=*,standings,users",
                         token, league_id, my_user_id)
    users = {str(u["id"]): u["name"] for u in league["data"]["users"]}
    balances = {uid: STARTING_BALANCE for uid in users}
    transfers = []
    # wins: listings this manager won. losses: losing bids they placed (from
    # the 'bids' array of listings someone else won) - both are a proxy for
    # how active/aggressive a manager is in the market.
    bid_stats = {uid: {"wins": 0, "losses": 0, "lastLossAmount": []} for uid in users}
    # A market win can later get undone by an admin (adminTransfer removing
    # that same player from that same buyer, e.g. a league correction) - the
    # buyer never really keeps the "win" in that case, so track both sets and
    # net them out after the full pass instead of counting them live.
    market_wins = set()  # {(buyer_id, player_id)}
    admin_reversals = set()  # {(from_id, player_id)}
    # What each manager actually paid for a player, so the roster modal can
    # show plusvalía/minusvalía against today's market value. The board feed
    # is newest-first, so the FIRST time we see a (buyer, player) pair here
    # is their most recent acquisition - exactly the one that matters if they
    # still own it now.
    acquisition_price = {}  # {(buyer_id, player_id): amount}
    # winning_amount / the player's CURRENT general market value, for free-agent
    # (machine-pool) signings this season. This is what "puja sugerida" is based
    # on: not how much the winner beat the runner-up by (that undersells early
    # season, when cash-rich/squad-light managers routinely pay well above
    # nominal value), but how much people actually pay relative to real value.
    # Tracked per position too: real data shows the premium is NOT the same
    # across positions (e.g. goalkeepers historically sell for LESS above
    # value than outfield players do, the opposite of what "scarcity" would
    # naively predict) - grounding the model in what was actually paid per
    # position beats a generic heuristic.
    value_ratios = []
    value_ratios_by_pos = {1: [], 2: [], 3: [], 4: [], 5: []}
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
                    if buyer_id in bid_stats:
                        bid_stats[buyer_id]["wins"] += 1
                        market_wins.add((buyer_id, c.get("player")))
                    if buyer_id and (buyer_id, c.get("player")) not in acquisition_price:
                        acquisition_price[(buyer_id, c.get("player"))] = amount
                    bids = c.get("bids", [])
                    for b in bids:
                        bidder = str((b.get("user") or {}).get("id") or "")
                        if bidder in bid_stats:
                            bid_stats[bidder]["losses"] += 1
                            bid_stats[bidder]["lastLossAmount"].append(b.get("amount", 0))
                    current_value = player_prices.get(c.get("player"))
                    if current_value and current_value >= 200000:
                        ratio = amount / current_value
                        value_ratios.append(ratio)
                        pos = player_pos.get(c.get("player"))
                        if pos in value_ratios_by_pos:
                            value_ratios_by_pos[pos].append(ratio)
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
                    if frm_id:
                        admin_reversals.add((frm_id, c.get("player")))
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
                    if buyer_id and (buyer_id, c.get("player")) not in acquisition_price:
                        acquisition_price[(buyer_id, c.get("player"))] = amount
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

    reversed_wins = market_wins & admin_reversals
    for buyer_id, _pid in reversed_wins:
        bid_stats[buyer_id]["wins"] = max(0, bid_stats[buyer_id]["wins"] - 1)
    if reversed_wins:
        print(f"  -> {len(reversed_wins)} fichaje(s) ganado(s) en mercado luego revertido(s) por un admin - no cuentan como 'ganada'")

    team_rows = sorted(([uid, users[uid], balances[uid]] for uid in users), key=lambda r: -r[2])
    transfers.sort(key=lambda t: -t[0])

    value_ratios.sort()
    DEFAULT_MEDIAN = 1.10  # not enough purchase history yet this season - conservative early-season default
    DEFAULT_SPREAD = 0.15
    if len(value_ratios) >= 5:
        global_median = percentile(value_ratios, 0.5)
        # The gap between the median and the 75th percentile is how much MORE
        # a genuinely contested listing goes for, on top of the typical case -
        # used below as the ceiling for how far role/titularity can push an
        # individual player's suggested bid above his position's baseline.
        # Capped so a couple of freak outlier sales (e.g. one wildly contested
        # transfer) can't blow up every suggestion.
        bid_spread = min(max(percentile(value_ratios, 0.75) - global_median, 0.05), 0.35)
        print(f"  -> {len(value_ratios)} fichajes libres analizados: mediana {global_median:.2f}x el valor de mercado, "
              f"margen por disputa +{bid_spread:.2f}x")
    else:
        global_median = DEFAULT_MEDIAN
        bid_spread = DEFAULT_SPREAD
        print(f"  -> solo {len(value_ratios)} fichajes libres, histórico insuficiente, uso valores por defecto "
              f"(mediana {DEFAULT_MEDIAN:.2f}x, margen +{DEFAULT_SPREAD:.2f}x)")

    # Per-position median, shrunk toward the league-wide median when a position
    # has few samples (e.g. only a handful of goalkeepers change hands) so
    # noise in a small sample can't swing that position's baseline too far.
    SHRINK_K = 10
    bid_median_by_pos = {}
    pos_names = {1: "POR", 2: "DEF", 3: "CEN", 4: "DEL", 5: "ENT"}
    for pos in (1, 2, 3, 4, 5):
        ratios = sorted(value_ratios_by_pos.get(pos, []))
        n = len(ratios)
        pos_median = percentile(ratios, 0.5) if ratios else global_median
        weight = min(n, SHRINK_K) / SHRINK_K
        blended = pos_median * weight + global_median * (1 - weight)
        bid_median_by_pos[pos] = round(blended, 3)
        if n:
            print(f"     {pos_names[pos]}: n={n} mediana real {pos_median:.2f}x -> {blended:.2f}x usado (peso {weight:.1f})")

    # The league's actual daily market listing: the system auto-adds ~20 free
    # ("Lliure", user is null) players per day, plus whatever other managers
    # have explicitly put up for sale (user is their manager info). This is
    # NOT "every unowned La Liga player" - only this specific rotating batch
    # is actually purchasable right now.
    print("  -> Fetching today's league market listing...")
    market_resp = fetch_auth("https://biwenger.as.com/api/v2/market", token, league_id, my_user_id)
    market = {}
    for sale in market_resp["data"].get("sales") or []:
        pid = (sale.get("player") or {}).get("id")
        if pid is None:
            continue
        user = sale.get("user")
        market[pid] = {
            "free": user is None,
            "seller": user.get("name") if user else None,
            "price": sale.get("price"),
            "until": sale.get("until"),
        }
    n_free = sum(1 for m in market.values() if m["free"])
    print(f"  -> {n_free} libres (máquina), {len(market) - n_free} en venta por managers")

    print("  -> Fetching current rosters (for rival squad composition)...")
    rosters = {}
    for uid in users:
        roster = fetch_auth(f"https://biwenger.as.com/api/v2/user/{uid}?fields=*,players",
                             token, league_id, my_user_id)
        rosters[uid] = [p["id"] for p in (roster["data"].get("players") or [])]

    # Only keep what each manager paid for players they still actually own -
    # no point exposing acquisition prices for players long since sold.
    paid_by_user = {}
    for uid, pids in rosters.items():
        prices = {pid: acquisition_price[(uid, pid)] for pid in pids if (uid, pid) in acquisition_price}
        if prices:
            paid_by_user[uid] = prices

    return {
        "team_rows": team_rows, "transfers": transfers, "market": market,
        "bid_stats": bid_stats, "rosters": rosters, "users": users,
        "bid_median_by_pos": bid_median_by_pos, "bid_spread": bid_spread,
        "paid_by_user": paid_by_user,
    }


def main():
    print("[1/10] Fetching Biwenger data...")
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
            "pts": p.get("points", 0),
            # Per-matchday recent form - empty until the season actually kicks
            # off and Biwenger starts populating it. Passed through as-is
            # (shape unverified against real match data yet) for the "racha
            # de los últimos 5 partidos" in the roster modal.
            "fitness": p.get("fitness") or [],
            "cuatroRating": None, "cuatroComment": None, "cuatroUrl": None,
        })
    vals = biw_market["values"][-60:]
    mc_line = json.dumps(vals, separators=(",", ":"))
    print(f"  -> {len(players)} players")

    print("[2/10] Fetching FutbolFantasy team hierarchies (20 teams)...")
    header_re = re.compile(
        r'<img width="30" class="mr-2" src="https://static\.futbolfantasy\.com/uploads/images/(?P<img>[a-z0-9]+)\.png">\s*(?P<cat>[^<]+?)\s*</header>',
        re.S)
    player_re = re.compile(
        r'href="https://www\.futbolfantasy\.com/jugadores/(?P<slug>[a-z0-9\-]+)" class="jugador">\s*(?P<name>[^<]+?)\s*</a>\s*<span class="comentario">\s*<span>(?P<pos>[^<]*)</span>',
        re.S)
    end_marker_re = re.compile(r"Once tipo y mapa rotacional")

    roles_by_team = {}
    for slug in SLUGS:
        try:
            html = fetch(f"https://www.futbolfantasy.com/laliga/equipos/{slug}/jerarquias")
        except Exception as e:
            print(f"  -> {slug}: FAILED ({e}), skipping this team for today")
            continue
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

    print("[3/10] Fetching starting-XI probabilities (team pages)...")
    prob_by_slug = fetch_lineup_probabilities(SLUGS)
    print(f"  -> {len(prob_by_slug)} players with a published probability")

    print("[4/10] Fetching FutbolFantasy injuries...")
    try:
        inj_html = fetch("https://www.futbolfantasy.com/laliga/lesionados")
    except Exception as e:
        print(f"  -> FAILED ({e}), continuing without injury data today")
        inj_html = ""
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

    print("[5/10] Fetching Cuatro Picas team-analysis ratings (temporada 26/27 únicamente)...")
    cuatro_by_team_label = fetch_cuatropicas_ratings()
    print(f"  -> {len(cuatro_by_team_label)} equipos con análisis 26/27 publicado: {sorted(cuatro_by_team_label.keys())}")
    biw_team_names = sorted({p["team"] for p in players if p["team"] != "Libre"})
    cuatro_players_by_biw_team = {}
    for cuatro_label, info in cuatro_by_team_label.items():
        for biw_team_name in biw_team_names:
            if cuatropicas_team_matches(cuatro_label, biw_team_name):
                cuatro_players_by_biw_team[biw_team_name] = info
                break

    def find_cuatro_match(bw_norm, team):
        info = cuatro_players_by_biw_team.get(team)
        if not info:
            return None
        for c in info["players"]:
            if c["norm"] == bw_norm:
                return c
        bw_last = bw_norm.split(" ")[-1]
        if len(bw_last) > 2:
            found = [c for c in info["players"] if c["norm"].split(" ")[-1] == bw_last]
            if len(found) == 1:
                return found[0]
        return None

    print("[6/10] Matching FutbolFantasy data to Biwenger players...")

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

    print("[7/10] Fetching league money, market and rivals (Biwenger, read-only)...")
    rivals_json = "[]"
    rosters_json = "{}"
    paid_json = "{}"
    market = {}
    bid_median_by_pos = {1: 1.10, 2: 1.10, 3: 1.10, 4: 1.10, 5: 1.10}
    bid_spread = 0.15
    token = os.environ.get("BIWENGER_TOKEN")
    if token:
        league_id = os.environ.get("BIWENGER_LEAGUE_ID", DEFAULT_LEAGUE_ID)
        player_names = {p["id"]: p["name"] for p in players}
        pos_by_id = {p["id"]: p["pos"] for p in players}
        players_by_id = {p["id"]: p for p in players}
        player_prices = {p["id"]: p["price"] for p in players}
        try:
            league_data = fetch_league_money(token, league_id, player_names, player_prices, pos_by_id)
            if league_data is not None:
                market = league_data["market"]
                bid_median_by_pos = league_data["bid_median_by_pos"]
                bid_spread = league_data["bid_spread"]
                print(f"  -> {len(league_data['team_rows'])} managers, {len(league_data['transfers'])} fichajes esta temporada")

                # Any owned player can be instant-sold to the market ("machine")
                # for roughly its current value at any time, so a manager who
                # looks broke right now may not be for long - and specifically,
                # anyone with a listing already up for sale (in `market`) will
                # get that cash the moment it sells (today, at the listed price).
                pending_by_seller = {}
                for listing in market.values():
                    if not listing["free"] and listing["seller"]:
                        pending_by_seller[listing["seller"]] = pending_by_seller.get(listing["seller"], 0) + (listing["price"] or 0)

                rivals = []
                for uid, name in league_data["users"].items():
                    balance = next((b for u, n, b in league_data["team_rows"] if u == uid), 0)
                    pos_counts = [0, 0, 0, 0, 0]  # POR,DEF,CEN,DEL,ENT
                    roster_value = 0
                    roster_value_change = 0  # suma de lo que ha subido/bajado hoy cada jugador de la plantilla
                    for pid in league_data["rosters"].get(uid, []):
                        pos = pos_by_id.get(pid)
                        if pos and 1 <= pos <= 5:
                            pos_counts[pos - 1] += 1
                        roster_value += players_by_id.get(pid, {}).get("price", 0) or 0
                        roster_value_change += players_by_id.get(pid, {}).get("inc", 0) or 0
                    stats = league_data["bid_stats"].get(uid, {"wins": 0, "losses": 0})
                    pending = pending_by_seller.get(name, 0)
                    rivals.append([uid, name, balance, pending, roster_value,
                                    *pos_counts, sum(pos_counts), stats["wins"], stats["losses"],
                                    roster_value_change])
                rivals.sort(key=lambda r: -r[2])
                rivals_json = json.dumps(rivals, ensure_ascii=False, separators=(",", ":"))
                rosters_json = json.dumps(league_data["rosters"], ensure_ascii=False, separators=(",", ":"))
                paid_json = json.dumps(league_data["paid_by_user"], ensure_ascii=False, separators=(",", ":"))
            else:
                print("  -> league not found for this token, skipping")
        except Exception as e:
            print(f"  -> failed ({e}), skipping league money/market/rivals this run")
    else:
        print("  -> BIWENGER_TOKEN not set, skipping league money/market/rivals section")

    print("[8/10] Building player rows...")
    role_matches = 0
    inj_matches = 0
    lineup_prob_matches = 0
    cuatro_matches = 0
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
        listing = market.get(p["id"])
        league_free = bool(listing) and listing["free"]
        league_forsale = bool(listing) and not listing["free"]
        sale_price = listing["price"] if listing else None
        sale_seller = listing["seller"] if listing else None
        sale_until = listing["until"] if listing else None
        cuatro_match = find_cuatro_match(bw_norm, p["team"])
        cuatro_rating = cuatro_comment = cuatro_url = None
        if cuatro_match:
            cuatro_matches += 1
            cuatro_rating = cuatro_match["rating"]
            cuatro_comment = cuatro_match["comment"]
            cuatro_url = cuatro_players_by_biw_team[p["team"]]["url"]
        rows.append([
            p["id"], p["name"], p["team"], p["pos"], p["price"], p["inc"], p["ptsLS"], p["status"],
            p["nextDiff"], role, injury_txt, days_txt, grav_v, status_txt, prob_v,
            league_free, league_forsale, sale_price, sale_seller, sale_until,
            p["pts"], p["fitness"], cuatro_rating, cuatro_comment, cuatro_url,
        ])
    players_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    print(f"  -> role matches: {role_matches} / injury matches: {inj_matches} / lineup probability matches: {lineup_prob_matches} / Cuatro Picas matches: {cuatro_matches}")

    print("[9/10] Building HTML...")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    font600 = (ROOT / "oswald-600.b64").read_text(encoding="utf-8").strip()
    font700 = (ROOT / "oswald-700.b64").read_text(encoding="utf-8").strip()
    html = (template
            .replace("__PLAYERS__", players_json)
            .replace("__MARKETCAP__", mc_line)
            .replace("__FONT600__", font600)
            .replace("__FONT700__", font700)
            .replace("__RIVALS__", rivals_json)
            .replace("__ROSTERS__", rosters_json)
            .replace("__PAID_PRICES__", paid_json)
            .replace("__BID_MEDIAN_BY_POS__", json.dumps({str(k): v for k, v in bid_median_by_pos.items()}))
            .replace("__BID_SPREAD__", json.dumps(round(bid_spread, 3))))
    out_path = ROOT / "biwenger.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  -> wrote {out_path} ({out_path.stat().st_size} bytes)")

    print("[10/10] Done. Publish this file as the artifact to update the live dashboard.")


if __name__ == "__main__":
    main()
