"""Phase 2: short news notes per team, written by Gemini from Google News headlines.

How it stays honest:
- Gemini only sees a numbered list of real headlines from Google News RSS.
- Each note must say which headline numbers it came from. Notes that cite
  nothing (or cite numbers that don't exist) are thrown away.
- Links on the page come straight from the RSS feed, never from Gemini.
- No headlines, no key, or any error -> the team simply gets no AI notes
  and the page says "Nothing notable".

Needs GEMINI_API_KEY (free tier, from Google AI Studio). Optional: GEMINI_MODEL.
Standard library only.
"""

import datetime as dt
import email.utils
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GOOGLE_RSS = "https://news.google.com/rss/search"
BING_RSS = "https://www.bing.com/news/search"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ["gemini-3.8-flash"]   # tried when the main one is overloaded
# Google retires models often. Each run also asks Gemini which Flash models this key can use
# (see available_models) and adds them after these, so a retirement doesn't break the notes.
MODEL_NAME = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?$")
GIVE_UP_AFTER = 3       # teams in a row failing on every model -> skip the rest of the news step
GIVE_UP_TOTAL = 8       # teams failing in total (even with successes between) -> skip the rest
MODELS_PER_TEAM = 2     # models tried for one team before calling it failed (failing ones rotate to the back)
NEWS_TIME_LIMIT = 30 * 60   # seconds; the whole news step stops after this, keeping what it has
GEMINI_TIMEOUT = 30     # seconds to wait for one Gemini answer
MAX_HEADLINES = 10      # headlines sent to Gemini per team
MAX_NOTES = 3           # notes kept per team
DAYS_BACK = 7
GEMINI_GAP = 5.0        # seconds between Gemini calls (keeps us under the free per-minute limit)
NEWS_GAP = 1.0          # seconds between Google News fetches (be polite)

PROMPT = """You help someone research a college football game. Below are recent news headlines about {team} football, numbered.

Write at most {max_notes} short notes (one sentence each, under 25 words) about things that could matter for this week's game: injuries, suspensions, quarterback or coaching changes, players opting out or transferring, team turmoil, travel or weather problems.

Rules:
- Use ONLY facts stated in the headlines. Do not add anything from your own knowledge.
- Every note must list the headline numbers it is based on.
- Skip game recaps, rankings talk, betting picks, recruiting, and anything about other sports.
- Never say who will win or cover. Do not recommend a pick.
- If nothing is relevant, return an empty list. That is a good answer.

Return JSON only: {{"notes": [{{"text": "...", "headlines": [1, 3]}}]}}
{already}
Headlines:
{headlines}"""

ALREADY = """
These notes were already written earlier this week. Do not repeat them; only add something new:
{notes}
"""

calls = {"news": 0, "gemini": 0}


def _get(url, headers=None, data=None, timeout=30):
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "pickem-lab-dashboard", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _rss_items(xml_bytes):
    """(title, source, link, date) for each <item> in an RSS feed."""
    for item in ET.fromstring(xml_bytes).iter("item"):
        source = (item.findtext("source") or "").strip()
        if not source:   # Bing puts the outlet name in its own namespaced <Source> tag
            source = next(((c.text or "").strip() for c in item if c.tag.endswith("}Source")), "")
        try:
            when = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        yield (item.findtext("title") or "").strip(), source, (item.findtext("link") or "").strip(), when


def google_news(query):
    params = {"q": f'"{query}" football when:{DAYS_BACK}d', "hl": "en-US", "gl": "US", "ceid": "US:en"}
    for title, source, link, when in _rss_items(_get(f"{GOOGLE_RSS}?{urllib.parse.urlencode(params)}")):
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        yield {"title": title, "source": source, "url": link, "date": when}


def bing_news(query):
    params = {"q": f"{query} football", "format": "rss"}
    for title, source, link, when in _rss_items(_get(f"{BING_RSS}?{urllib.parse.urlencode(params)}")):
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url")   # unwrap Bing's click-tracking link
        source = source.removesuffix(" on MSN")
        yield {"title": title, "source": source, "url": real[0] if real else link, "date": when}


SOURCES = {"Google News": google_news, "Bing News": bing_news}
source_failures = {name: 0 for name in SOURCES}   # a source failing 3 times in a row is skipped for the rest of the run


def headlines_for(query):
    """Recent headlines for a team: [{title, source, url, date}]. Tries Google News, then Bing News.
    Raises only if every source failed."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS_BACK)
    errors = []
    for name, fetch in SOURCES.items():
        if source_failures[name] >= 3:
            continue
        try:
            for attempt in (1, 2):   # one retry: both sites sometimes hiccup under quick repeat requests
                calls["news"] += 1
                try:
                    items = [h for h in fetch(query)
                             if h["title"] and h["url"].startswith("https://") and (h["date"] is None or h["date"] >= cutoff)]
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(5)
        except Exception as err:
            source_failures[name] += 1
            if source_failures[name] == 3:
                print(f"  {name} failed 3 times in a row; skipping it for the rest of this run")
            errors.append(f"{name}: {err}")
            continue
        source_failures[name] = 0
        items.sort(key=lambda h: h["date"].timestamp() if h["date"] else 0, reverse=True)
        return items[:MAX_HEADLINES]
    raise RuntimeError("; ".join(errors) or "no news source left to try")


def ask_gemini(team, headlines, key, model, earlier=()):
    already = ALREADY.format(notes="\n".join(f"- {n['text']}" for n in earlier)) if earlier else ""
    listing = "\n".join(
        f"{i}. {h['title']} ({h['source'] or 'unknown source'}"
        + (f", {h['date']:%b} {h['date'].day}" if h["date"] else "") + ")"
        for i, h in enumerate(headlines, 1))
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(team=team, max_notes=MAX_NOTES, already=already, headlines=listing)}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    url = f"{GEMINI_BASE}/{urllib.parse.quote(model)}:generateContent"
    calls["gemini"] += 1
    raw = _get(url, {"Content-Type": "application/json", "x-goog-api-key": key}, json.dumps(body).encode(), timeout=GEMINI_TIMEOUT)
    reply = json.loads(raw)
    text = "".join(p.get("text", "") for p in reply["candidates"][0]["content"]["parts"])
    return json.loads(text).get("notes") or []


def checked_notes(raw_notes, headlines):
    """Keep only notes that cite real headline numbers; attach those headlines' links."""
    out = []
    for note in raw_notes:
        if not isinstance(note, dict):
            continue
        text = str(note.get("text") or "").strip()
        cited = []
        for n in note.get("headlines") or []:
            if isinstance(n, int) and 1 <= n <= len(headlines) and n not in cited:
                cited.append(n)
        if not text or not cited:
            continue
        out.append({
            "text": text[:300],
            "sources": [{"title": headlines[n - 1]["title"], "source": headlines[n - 1]["source"],
                         "url": headlines[n - 1]["url"]} for n in cited],
        })
        if len(out) == MAX_NOTES:
            break
    return out


def available_models(key):
    """Stable Flash models this key can call, Flash-Lite first, newest version first.
    Skips previews and specialty versions (names with extra parts). Empty list if the lookup fails."""
    try:
        calls["gemini"] += 1
        listing = json.loads(_get(f"{GEMINI_BASE}?pageSize=1000", {"x-goog-api-key": key}))
    except Exception as err:
        print(f"  could not list Gemini models: {err}")
        return []
    found = []
    for m in listing.get("models") or []:
        name = str(m.get("name", "")).removeprefix("models/")
        match = MODEL_NAME.match(name)
        if match and "generateContent" in (m.get("supportedGenerationMethods") or []):
            found.append((bool(match.group(2)), float(match.group(1)), name))
    return [name for _, _, name in sorted(found, reverse=True)]


class KeyRefused(Exception):
    pass


def error_text(err):
    """Gemini's own explanation from an error response, shortened for the log."""
    try:
        return json.loads(err.read()).get("error", {}).get("message", "")[:200]
    except Exception:
        return ""


def notes_with_fallback(search, headlines, key, models, deadline, earlier=()):
    """Tries each free model in turn. Overloaded (503/500) or rate-limited (429) -> wait and
    retry, then move to the next model. A model that keeps failing is moved to the back of
    the list so later teams start with one that works. Tries at most MODELS_PER_TEAM models
    and stops at the deadline. Raises if none of them worked."""
    for model in list(models)[:MODELS_PER_TEAM]:
        for wait in (0, 15):
            if time.monotonic() > deadline:
                raise RuntimeError("out of time")
            time.sleep(GEMINI_GAP + wait)
            try:
                return checked_notes(ask_gemini(search, headlines, key, model, earlier), headlines)
            except urllib.error.HTTPError as err:
                detail = error_text(err)
                if err.code in (401, 403) or (err.code == 400 and "API key" in detail):
                    raise KeyRefused(f"{err.code} {detail}") from err
                print(f"    {model}: {err.code} {detail}")
                if err.code in (400, 404):      # model name not offered (anymore): drop it
                    models.remove(model)
                    break
            except Exception as err:
                print(f"    {model}: {err}")
        if model in models:
            models.remove(model)
            models.append(model)
    raise RuntimeError("every model failed")


def team_news(teams, earlier=None, earlier_time=None):
    """teams: {display name: search name}.
    earlier: {display name: [notes]} from a previous run this same week (e.g. Thursday's), made at earlier_time.
    Returns ({display name: [notes]}, status text for the page).

    Earlier notes are never dropped: a team with no headlines since earlier_time keeps them as is,
    and new notes from newer headlines are added after them. A team missing from the result
    means "not checked" (no key or an error), so the page can tell that apart from
    "checked, nothing notable" (an empty list)."""
    earlier = earlier or {}
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("  news notes skipped: no GEMINI_API_KEY")
        return {}, "off (no Gemini key)"
    models = list(dict.fromkeys([os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL, *FALLBACK_MODELS,
                                 *available_models(key)]))
    print(f"  Gemini models to try: {', '.join(models)}")
    if earlier:
        print(f"  {len(earlier)} teams already checked earlier this week; only newer headlines are sent to Gemini")
    out, failed_in_a_row, failed_total, unchanged, stopped = {}, 0, 0, 0, ""
    deadline = time.monotonic() + NEWS_TIME_LIMIT
    # Teams that weren't checked before go first, so a run that stops early fills the gaps.
    for name, search in sorted(teams.items(), key=lambda item: item[0] in earlier):
        if time.monotonic() > deadline:
            print(f"  News step hit its {NEWS_TIME_LIMIT // 60}-minute limit; stopping news notes.")
            stopped = "time limit reached"
            break
        old = earlier.get(name)
        try:
            time.sleep(NEWS_GAP)
            headlines = headlines_for(search)
        except Exception as err:
            print(f"  news skipped for {name}: {err}")
            if all(n >= 3 for n in source_failures.values()):
                print("  Every news site is refusing us; stopping news notes.")
                stopped = "news sites unreachable"
                break
            continue
        if old is not None and earlier_time:
            headlines = [h for h in headlines if h["date"] and h["date"] > earlier_time]
        if not headlines:
            out[name] = old if old is not None else []
            unchanged += old is not None
            continue
        try:
            added = notes_with_fallback(search, headlines, key, models, deadline, old or ())
            seen = {n["text"] for n in old or ()}
            out[name] = [*(old or ()), *(n for n in added if n["text"] not in seen)]
            failed_in_a_row = 0
        except KeyRefused as err:
            print(f"  Gemini refused the key ({err}). Check the GEMINI_API_KEY secret. Stopping news notes.")
            stopped = "Gemini refused the key"
            break
        except Exception as err:
            print(f"  Gemini failed for {name}: {err}")
            failed_in_a_row += 1
            failed_total += 1
            if time.monotonic() > deadline:
                print(f"  News step hit its {NEWS_TIME_LIMIT // 60}-minute limit; stopping news notes.")
                stopped = "time limit reached"
                break
            if failed_in_a_row >= GIVE_UP_AFTER or failed_total >= GIVE_UP_TOTAL or not models:
                if not models:
                    print("  None of the Gemini models are available to this key; stopping news notes.")
                    stopped = "no Gemini model available (see run log)"
                elif failed_total >= GIVE_UP_TOTAL and failed_in_a_row < GIVE_UP_AFTER:
                    print(f"  {failed_total} teams failed in total. Gemini is too unreliable today; stopping news notes.")
                    stopped = "Gemini unreliable"
                else:
                    print(f"  {failed_in_a_row} teams in a row failed on every model. Gemini seems down; stopping news notes.")
                    stopped = "Gemini busy or down"
                break
    for name in teams:   # anything not reached this run keeps what the earlier run found
        if name not in out and name in earlier:
            out[name] = earlier[name]
    summary = f"{len(out)} of {len(teams)} teams checked"
    if unchanged:
        summary += f" ({unchanged} had no new headlines since the last run)"
    print(f"  news notes: {summary} ({calls['news']} news fetches, {calls['gemini']} Gemini calls)")
    return out, f"{summary}, stopped early: {stopped}" if stopped else summary
