#!/usr/bin/env python3
"""Builds docs/sports/cfb/data.json for the Pick'em Prep dashboard.

GitHub Actions runs this every Thursday and Friday morning, and whenever you
press "Run workflow". It pulls games, betting lines, records and rankings from
CollegeFootballData.com and kickoff weather from Open-Meteo. It uses only
Python's standard library, so there is nothing to install.

Needs one secret: CFBD_API_KEY (your free CollegeFootballData key).
Optional secret: GEMINI_API_KEY (free) turns on the AI news notes in news.py.
Optional overrides: SEASON=2026, WEEK=13, SEASON_TYPE=regular (or postseason).

Also writes docs/sports/cfb/feed.json: plain numbers (no display text) for the
leagues database. See build_feed().
"""

import datetime as dt
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from news import team_news

CFBD_BASE = "https://api.collegefootballdata.com"
WEATHER_BASE = "https://api.open-meteo.com/v1/forecast"
CENTRAL = ZoneInfo("America/Chicago")
EASTERN = ZoneInfo("America/New_York")   # TBA kickoffs come as midnight Eastern on game day
SPORT = "cfb"           # each sport keeps its files in docs/sports/<key>/ (cfb, later nfl, nba, epl)
OUT_FILE = Path(os.environ.get("OUT_FILE") or Path(__file__).resolve().parent.parent / "docs" / "sports" / SPORT / "data.json")

TOSS_UP_POINTS = 3      # spreads smaller than this get the toss-up flag
SPREAD_SD = 15.0        # rough spread of college results around the line (backup win-probability estimate)
LAST_N = 4              # recent games shown per team
FEED_DAYS_BACK = 10     # feed keeps finished games this many days back (late finals, score fixes)
FEED_DAYS_AHEAD = 16    # and upcoming games this far ahead (covers next week's early games)
PREFERRED_BOOKS = ["consensus", "DraftKings", "ESPN Bet", "Bovada", "William Hill (New Jersey)"]
SNOW_CODES = {71, 73, 75, 77, 85, 86}
STORM_CODES = {95, 96, 99}

api_calls = {"cfbd": 0, "weather": 0}


# ---------------------------------------------------------------- web helpers

def fetch_json(url, headers=None, tries=3):
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "pickem-lab-dashboard",
        **(headers or {}),
    })
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as err:
            if err.code in (400, 401, 403, 404) or attempt == tries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries:
                raise
        time.sleep(2 * attempt)


def cfbd(path, **params):
    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        raise SystemExit("CFBD_API_KEY is missing. Add it under Settings > Secrets and variables > Actions.")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    api_calls["cfbd"] += 1
    try:
        return fetch_json(f"{CFBD_BASE}{path}?{query}", {"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise SystemExit(f"CollegeFootballData refused the key ({err.code}). Check the CFBD_API_KEY secret.") from err
        raise


def cfbd_fbs_games(year, season_type):
    """All FBS games for a season. Tries the current filter name, then the older one."""
    for params in ({"classification": "fbs"}, {"division": "fbs"}, {}):
        try:
            return cfbd("/games", year=year, seasonType=season_type, **params)
        except urllib.error.HTTPError as err:
            if err.code != 400:
                raise
    return []


def field(obj, *names, default=None):
    """First non-empty value among several possible field names (the API has renamed a few)."""
    for name in names:
        if isinstance(obj, dict) and obj.get(name) is not None:
            return obj[name]
    return default


def parse_time(value):
    if not value:
        return None
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- loaders

def norm_game(g):
    return {
        "id": field(g, "id"),
        "week": field(g, "week"),
        "season_type": field(g, "seasonType", "season_type", default="regular"),
        "start": parse_time(field(g, "startDate", "start_date")),
        "tbd": bool(field(g, "startTimeTBD", "start_time_tbd", default=False)),
        "completed": bool(field(g, "completed", default=False)),
        "neutral": bool(field(g, "neutralSite", "neutral_site", default=False)),
        "venue_id": field(g, "venueId", "venue_id"),
        "venue": field(g, "venue", default=""),
        "home": field(g, "homeTeam", "home_team"),
        "away": field(g, "awayTeam", "away_team"),
        "home_fbs": str(field(g, "homeClassification", "home_division", default="")).lower() == "fbs",
        "away_fbs": str(field(g, "awayClassification", "away_division", default="")).lower() == "fbs",
        "home_pts": field(g, "homePoints", "home_points"),
        "away_pts": field(g, "awayPoints", "away_points"),
    }


def load_lines(rows):
    """Game id -> the preferred sportsbook line (spread is from the home team's side)."""
    out = {}
    for entry in rows or []:
        lines = [l for l in (entry.get("lines") or []) if l.get("spread") not in (None, "")]
        if not lines:
            continue
        def book_rank(line):
            name = line.get("provider") or ""
            return PREFERRED_BOOKS.index(name) if name in PREFERRED_BOOKS else len(PREFERRED_BOOKS)
        best = sorted(lines, key=book_rank)[0]
        out[entry.get("id")] = {
            "spread": float(best["spread"]),
            "home_ml": best.get("homeMoneyline"),
            "away_ml": best.get("awayMoneyline"),
            "provider": best.get("provider") or "",
        }
    return out


def load_records(rows):
    out = {}
    for r in rows or []:
        total = r.get("total") or {}
        if r.get("team"):
            out[r["team"]] = (int(total.get("wins") or 0), int(total.get("losses") or 0))
    return out


def load_ranks(rows):
    """Team -> (rank, poll). Uses each poll's latest week; Playoff rankings win over AP."""
    latest = {}
    for week in sorted(rows or [], key=lambda r: (field(r, "seasonType", default="") == "postseason", int(field(r, "week", default=0)))):
        for poll in week.get("polls") or []:
            latest[poll.get("poll")] = poll.get("ranks") or []
    ranks = {}
    for poll_name, label in (("AP Top 25", "AP"), ("Playoff Committee Rankings", "CFP")):
        for r in latest.get(poll_name, []):
            if r.get("school") and r.get("rank"):
                ranks[r["school"]] = (int(r["rank"]), label)
    return ranks


def load_ap(rows):
    """Latest AP poll for the leagues feed: (team -> rank 1-25, which poll it was).
    CollegeFootballData lists only the 25 ranked teams, so leagues offer top 25, not more."""
    latest, when = [], {}
    for week in sorted(rows or [], key=lambda r: (field(r, "seasonType", default="") == "postseason", int(field(r, "week", default=0)))):
        for poll in week.get("polls") or []:
            if poll.get("poll") == "AP Top 25":
                latest = poll.get("ranks") or []
                when = {"season_type": field(week, "seasonType", default="regular"), "week": int(field(week, "week", default=0))}
    ranks = {r["school"]: int(r["rank"]) for r in latest if r.get("school") and r.get("rank")}
    return ranks, dict(when, ranked=len(ranks))


def load_sp(rows):
    """Team -> SP+ national ranks {overall, offense, defense}. #1 is best (for defense too)."""
    out = {}
    for r in rows or []:
        offense, defense = r.get("offense") or {}, r.get("defense") or {}
        if r.get("team") and r.get("ranking"):
            out[r["team"]] = {"overall": r.get("ranking"), "offense": offense.get("ranking"), "defense": defense.get("ranking")}
    return out


def optional(path, **params):
    """A CollegeFootballData call the dashboard can live without: errors give an empty list."""
    try:
        return cfbd(path, **params)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
        print(f"  skipped {path}: {err}")
        return []


def load_venues(rows):
    out = {}
    for v in rows or []:
        lat, lon = field(v, "latitude"), field(v, "longitude")
        location = v.get("location") or {}
        if lat is None and location:
            lat, lon = location.get("x"), location.get("y")
        out[v.get("id")] = {
            "lat": lat, "lon": lon, "dome": bool(v.get("dome")),
            "name": v.get("name") or "", "city": v.get("city") or "", "state": v.get("state") or "",
        }
    return out


def pick_week(calendar, now):
    if os.environ.get("WEEK"):
        return int(os.environ["WEEK"]), os.environ.get("SEASON_TYPE") or "regular"
    weeks = []
    for c in calendar or []:
        end = parse_time(field(c, "endDate", "lastGameStart", "last_game_start"))
        if end:
            weeks.append((end, int(field(c, "week")), field(c, "seasonType", "season_type", default="regular")))
    if not weeks:
        raise SystemExit("CollegeFootballData returned no calendar for this season.")
    weeks.sort(key=lambda w: w[0])
    for end, week, season_type in weeks:
        if end >= now:
            return week, season_type
    return weeks[-1][1], weeks[-1][2]


def season_for(now):
    local = now.astimezone(CENTRAL)
    return local.year if local.month >= 3 else local.year - 1   # January bowls belong to last season


# ---------------------------------------------------------------- football math

def moneyline_prob(ml):
    ml = float(ml)
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def home_win_prob(line):
    """Chance the home team wins, from moneylines when we have them, else from the spread."""
    if line["home_ml"] not in (None, 0, "") and line["away_ml"] not in (None, 0, ""):
        home, away = moneyline_prob(line["home_ml"]), moneyline_prob(line["away_ml"])
        return home / (home + away), "betting odds"
    return 0.5 * (1 + math.erf(-line["spread"] / (SPREAD_SD * math.sqrt(2)))), "the spread"


def ats_result(team_is_home, home_pts, away_pts, home_spread):
    value = (home_pts - away_pts) + home_spread      # above 0: home team covered
    if not team_is_home:
        value = -value
    return "Covered" if value > 0 else "Missed" if value < 0 else "Push"


def points(x):
    return f"{x:g}"


def spread_label(home, away, spread):
    if spread is None:
        return "No line yet"
    if spread < 0:
        return f"{home} −{points(-spread)}"
    if spread > 0:
        return f"{away} −{points(spread)}"
    return "Even (pick'em)"


def record_text(wins, losses):
    return f"{wins}–{losses}"


# ---------------------------------------------------------------- weather

def kickoff_weather(venue, kickoff, tbd, now):
    if not venue:
        return "Weather unavailable"
    if venue["dome"]:
        return "Indoors (dome): weather not a factor"
    if venue["lat"] is None or venue["lon"] is None:
        return "Weather unavailable"
    if kickoff - now > dt.timedelta(days=15):
        return "Forecast not out yet"
    when = kickoff.astimezone(CENTRAL).replace(hour=14, minute=0) if tbd else kickoff
    hour = when.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    params = {
        "latitude": venue["lat"], "longitude": venue["lon"],
        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,weather_code",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "UTC",
        "start_date": hour.date().isoformat(), "end_date": hour.date().isoformat(),
    }
    try:
        api_calls["weather"] += 1
        hourly = fetch_json(f"{WEATHER_BASE}?{urllib.parse.urlencode(params)}").get("hourly") or {}
        i = (hourly.get("time") or []).index(hour.strftime("%Y-%m-%dT%H:%M"))
    except Exception as err:  # one bad forecast shouldn't stop the whole update
        print(f"  weather skipped for {venue['name']}: {err}")
        return "Weather unavailable"
    temp = hourly["temperature_2m"][i]
    wind = hourly["wind_speed_10m"][i]
    chance = hourly["precipitation_probability"][i]
    code = hourly["weather_code"][i]
    kind = "Rain"
    if chance is None or chance < 20:
        kind = "Rain or snow"
    elif code in SNOW_CODES:
        kind = "Snow"
    elif code in STORM_CODES:
        kind = "Storms"
    parts = []
    if temp is not None:
        parts.append(f"{round(temp)}°F")
    if wind is not None:
        parts.append(f"Wind {round(wind)} mph")
    if chance is not None:
        parts.append(f"{kind} {round(chance)}%")
    text = " · ".join(parts) or "Weather unavailable"
    return f"{text} (time TBA)" if tbd else text


# ---------------------------------------------------------------- building cards

def clock(t):
    return t.strftime("%I:%M %p").lstrip("0") + " CT"


def build_team(name, is_fbs, game, ctx):
    played = [g for g in ctx["history"].get(name, []) if g["start"] < game["start"]]
    known = name in ctx["records"] or bool(played)
    wins, losses = ctx["records"].get(name) or (
        sum(1 for g in played if (g["home_pts"] > g["away_pts"]) == (g["home"] == name)),
        sum(1 for g in played if (g["home_pts"] < g["away_pts"]) == (g["home"] == name)),
    )
    ats = {"Covered": 0, "Missed": 0, "Push": 0}
    recent = []
    for g in played:
        line = ctx["lines"].get(g["id"])
        if line:
            ats[ats_result(g["home"] == name, g["home_pts"], g["away_pts"], line["spread"])] += 1
    for g in reversed(played[-LAST_N:]):
        is_home = g["home"] == name
        us, them = (g["home_pts"], g["away_pts"]) if is_home else (g["away_pts"], g["home_pts"])
        opponent = g["away"] if is_home else g["home"]
        where = "vs" if is_home or g["neutral"] else "at"
        opp_record = ctx["records"].get(opponent)
        opp_sp = (ctx["sp"].get(opponent) or {}).get("overall")
        details = [record_text(*opp_record) if opp_record else "", f"SP+ #{opp_sp}" if opp_sp else ""]
        recent.append({
            "result": f"{'W' if us > them else 'L' if us < them else 'T'} {us}–{them}",
            "opponent": f"{where} {opponent}" + (" (neutral)" if g["neutral"] else ""),
            "opp_record": f"({', '.join(d for d in details if d)})" if any(details) else "",
        })
    ats_text = f"ATS {ats['Covered']}–{ats['Missed']}" + (f"–{ats['Push']}" if ats["Push"] else "")

    rank = ctx["ranks"].get(name)
    flags = []
    if rank:
        flags.append(f"{name}: ranked #{rank[0]} ({rank[1]})")
    if ctx["season_type"] == "regular" and is_fbs and wins == 5:
        flags.append(f"{name}: one win from bowl eligibility")
    if ctx["season_type"] == "regular" and played:
        gap = (game["start"].astimezone(CENTRAL).date() - played[-1]["start"].astimezone(CENTRAL).date()).days
        if gap >= 11:
            flags.append(f"{name}: off a bye week")
        elif gap <= 5:
            flags.append(f"{name}: short week (played {played[-1]['start'].astimezone(CENTRAL):%a})")

    return {
        "name": name,
        "rank": f"#{rank[0]}" if rank else "",
        "record": record_text(wins, losses) if known else "Record n/a",
        "ats": ats_text,
        "sp": ctx["sp"].get(name),   # None for teams SP+ doesn't rate (FCS)
        "last": recent,
    }, flags


def build_game(g, ctx, now):
    local = g["start"].astimezone(CENTRAL)
    time_text = "Time TBA" if g["tbd"] else clock(local)
    line = ctx["lines"].get(g["id"])
    away, away_flags = build_team(g["away"], g["away_fbs"], g, ctx)
    home, home_flags = build_team(g["home"], g["home_fbs"], g, ctx)

    away["win_pct"] = home["win_pct"] = None
    prob_source = None
    if line:
        p_home, prob_source = home_win_prob(line)
        home["win_pct"] = min(99, max(1, round(p_home * 100)))
        away["win_pct"] = 100 - home["win_pct"]

    final = None
    if g["completed"] and g["home_pts"] is not None:
        final = f"Final: {g['away']} {g['away_pts']}, {g['home']} {g['home_pts']}"

    venue = ctx["venues"].get(g["venue_id"])
    weather = "Game finished" if final else kickoff_weather(venue, g["start"], g["tbd"], now)
    place = ", ".join(p for p in [g["venue"] or (venue or {}).get("name"), (venue or {}).get("city"), (venue or {}).get("state")] if p)

    return {
        "id": g["id"],
        "anchor": f"game-{g['id']}",
        "start": g["start"].isoformat(),
        "day": f"{local:%A}, {local:%b} {local.day}",
        "time": time_text,
        "kickoff": f"{local:%a}, {local:%b} {local.day} · {time_text}",
        "neutral": g["neutral"],
        "venue": place,
        "final": final,
        "spread": spread_label(g["home"], g["away"], line["spread"] if line else None),
        "line_source": line["provider"] if line else "",
        "tossup": bool(line) and abs(line["spread"]) < TOSS_UP_POINTS,
        "prob_source": prob_source,
        "weather": weather,
        "flags": away_flags + home_flags,
        "away": away,
        "home": home,
    }


# ---------------------------------------------------------------- main

def main():
    now = dt.datetime.now(dt.timezone.utc)
    year = int(os.environ.get("SEASON") or season_for(now))
    week, season_type = pick_week(cfbd("/calendar", year=year), now)
    print(f"Building {year} {season_type} week {week}")

    types = ["regular"] + (["postseason"] if season_type == "postseason" else [])
    games, lines = [], {}
    for st in types:
        games += [norm_game(g) for g in cfbd_fbs_games(year, st)]
        lines.update(load_lines(cfbd("/lines", year=year, seasonType=st)))

    history = defaultdict(list)
    for g in games:
        if g["completed"] and g["start"] and g["home_pts"] is not None and g["away_pts"] is not None:
            history[g["home"]].append(g)
            history[g["away"]].append(g)
    for played in history.values():
        played.sort(key=lambda g: g["start"])

    rank_rows = cfbd("/rankings", year=year)
    ctx = {
        "history": history,
        "lines": lines,
        "records": load_records(cfbd("/records", year=year)),
        "ranks": load_ranks(rank_rows),
        "venues": load_venues(cfbd("/venues")),
        "sp": load_sp(optional("/ratings/sp", year=year)),
        "season_type": season_type,
    }

    this_week = [g for g in games if g["week"] == week and g["season_type"] == season_type and g["start"]]
    this_week.sort(key=lambda g: (g["start"], g["home"] or ""))
    cards = [build_game(g, ctx, now) for g in this_week]

    # Phase 2: AI news notes for teams that haven't played this week's game yet.
    mascots = {t.get("school"): t.get("mascot") for t in cfbd("/teams", year=year) or [] if t.get("school")}
    upcoming = {}
    for card in cards:
        if not card["final"]:
            for side in ("away", "home"):
                name = card[side]["name"]
                upcoming[name] = f"{name} {mascots[name]}" if mascots.get(name) else name
    # An earlier run this same week (Thursday's, seen by Friday's run): its notes are kept and only added to.
    old_news, old_time = previous_news(year, season_type, week)
    news, news_status = team_news(upcoming, old_news, old_time) if upcoming else ({}, "no upcoming games")
    for card in cards:
        for side in ("away", "home"):
            name = card[side]["name"]
            card[side]["news"] = news[name] if name in news else old_news.get(name)   # None = not checked

    stamp = now.astimezone(CENTRAL)
    data = {
        "sport": SPORT,
        "season": year,
        "week": week,
        "season_type": season_type,
        "title": f"Week {week}" if season_type == "regular" else "Bowl season",
        "generated_at": now.isoformat(),
        "generated_label": f"{stamp:%a}, {stamp:%b} {stamp.day} at {clock(stamp)}",
        "news_status": news_status,
        "sp_teams": len(ctx["sp"]),
        "games": cards,
    }
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(text, encoding="utf-8")
    save_archive(data, text)
    write_feed(build_feed(games, lines, load_ap(rank_rows), now, year))
    print(f"Wrote {len(cards)} games to {OUT_FILE} "
          f"({api_calls['cfbd']} CollegeFootballData calls, {api_calls['weather']} weather calls)")


def lock_time(g):
    """When picks close: kickoff, or for a TBA game 12:00 AM Central at the start of game day."""
    if not g["tbd"]:
        return g["start"]
    day = g["start"].astimezone(EASTERN).date()
    return dt.datetime.combine(day, dt.time(0), CENTRAL)


def freeze_time(lock, season_type):
    """When the line freezes (spread mode): 1 hour before lock, or earlier if the normal freeze
    comes first. Normal freeze: regular season, Thursday 9 AM Central of the game's Monday-Sunday
    week; postseason, 9 AM Central the day before."""
    day = lock.astimezone(CENTRAL).date()
    if season_type == "postseason":
        usual = day - dt.timedelta(days=1)
    else:
        usual = day - dt.timedelta(days=day.weekday()) + dt.timedelta(days=3)
    return min(dt.datetime.combine(usual, dt.time(9), CENTRAL), lock - dt.timedelta(hours=1))


def build_feed(games, lines, ap, now, year):
    """Plain game data for the leagues database: no display text, times in UTC, spread from the
    home team's side (-3.5 = home favored by 3.5). Keeps games from FEED_DAYS_BACK days ago to
    FEED_DAYS_AHEAD days ahead, so finals get graded and next week's early games are known."""
    ranks, poll = ap
    lo, hi = now - dt.timedelta(days=FEED_DAYS_BACK), now + dt.timedelta(days=FEED_DAYS_AHEAD)
    rows = []
    for g in sorted((g for g in games if g["start"] and lo <= g["start"] <= hi), key=lambda g: (g["start"], g["id"])):
        line = lines.get(g["id"])
        done = g["completed"] and g["home_pts"] is not None and g["away_pts"] is not None
        lock = lock_time(g)
        rows.append({
            "id": f"{SPORT}:{g['id']}",
            "season": year,
            "season_type": g["season_type"],
            "week": g["week"],
            "start": g["start"].astimezone(dt.timezone.utc).isoformat(),
            "start_tba": g["tbd"],
            "lock_at": lock.astimezone(dt.timezone.utc).isoformat(),
            "freeze_at": freeze_time(lock, g["season_type"]).astimezone(dt.timezone.utc).isoformat(),
            "home": g["home"],
            "away": g["away"],
            "home_ap": ranks.get(g["home"]),
            "away_ap": ranks.get(g["away"]),
            "spread": line["spread"] if line else None,
            "completed": done,
            "home_pts": g["home_pts"] if done else None,
            "away_pts": g["away_pts"] if done else None,
        })
    return {"sport": SPORT, "season": year, "generated_at": now.isoformat(), "ap_poll": poll, "games": rows}


def write_feed(feed):
    path = OUT_FILE.parent / "feed.json"
    path.write_text(json.dumps(feed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    p = feed["ap_poll"]
    print(f"Wrote {len(feed['games'])} games to {path} "
          f"(AP poll week {p.get('week', '?')}: {p.get('ranked', 0)} teams ranked)")


def previous_news(year, season_type, week):
    """News notes from the last run if it covered this same week: ({team: notes}, its run time).
    Teams that weren't checked then are left out. ({}, None) if there's nothing to reuse."""
    try:
        old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if (old.get("sport"), old.get("season"), old.get("season_type"), old.get("week")) != (SPORT, year, season_type, week):
            return {}, None
        when = dt.datetime.fromisoformat(old["generated_at"])
    except Exception:
        return {}, None
    notes = {}
    for game in old.get("games") or []:
        for side in ("away", "home"):
            team = game.get(side) or {}
            if team.get("name") and isinstance(team.get("news"), list):
                notes[team["name"]] = team["news"]
    return notes, when


def save_archive(data, text):
    """Keeps a copy of every week in docs/sports/<sport>/weeks/ plus an index the page uses for its week picker.
    Thursday's and Friday's runs share one file per week; Friday's (newest) wins."""
    folder = OUT_FILE.parent / "weeks"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{data['season']}-{data['season_type']}-{int(data['week']):02d}.json").write_text(text, encoding="utf-8")
    weeks = []
    for path in folder.glob("*-*-*.json"):
        try:
            season, season_type, week = path.stem.split("-")
            season, week = int(season), int(week)
        except ValueError:
            continue
        label = f"Week {week}" if season_type == "regular" else "Bowl season"
        weeks.append({"sport": SPORT, "file": path.name, "season": season, "season_type": season_type, "week": week,
                      "label": f"{season} · {label}"})
    weeks.sort(key=lambda w: (w["season"], w["season_type"] == "postseason", w["week"]), reverse=True)
    (folder / "index.json").write_text(json.dumps(weeks, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
