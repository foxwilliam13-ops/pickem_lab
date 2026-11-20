# Pick'em Lab (pickem_lab)

A copy of elyse_pickem (made 2026-11-20, fresh git history) for expanding the dashboard.
**Never change the original `elyse_pickem` folder or repo from here**; it is the version Elyse uses.

Starting point: a free research dashboard for Elyse's weekly college football pick'em. She picks **against the spread** from a set list of games her league gives her. The dashboard collects information in one place; it must never recommend or make picks.

Live page: https://foxwilliam13-ops.github.io/pickem_lab/ (once the repo and Pages are set up)

## Hard rules
- **$0 budget.** Only free services. No paid APIs, no paid tiers. The owner is on the Claude Pro plan, which has no API credits, so do not add Claude API calls and never set ANTHROPIC_API_KEY.
- The repo is public. Keys live only in GitHub Secrets (`CFBD_API_KEY`), never in files or the page.
- Keep it simple: build features one small step at a time.
- **No branding.** Don't name pick'em sites (Yahoo, ESPN, etc.) in the page, and never load team logos, league marks or other images of teams. Team names as plain text only.

## How it works
Each sport has a short key and its own folder: `docs/sports/<key>/` holds `data.json` and `weeks/`. Every data file carries a `sport` field. The page picks the sport from `?sport=<key>` (default `cfb`, listed in `SPORTS` in `index.html`). Planned sports, added one at a time: `cfb` (built), `nfl`, `nba`, `epl`.

1. `.github/workflows/update.yml` runs on "Run workflow" and on push. Its Thu/Fri schedule is commented out for now (see the file).
   - Scheduled and manual runs: run `scripts/build_data.py`, commit `docs/sports/`, deploy Pages.
   - Push runs: only redeploy `docs/` (so page edits go live quickly).
2. `scripts/build_data.py` (Python standard library only) pulls from CollegeFootballData.com API v2 (about 6 calls a run; free tier is 1,000 a month) and Open-Meteo (free, no key). Writes `docs/sports/cfb/data.json` (`SPORT = "cfb"` at the top).
3. `docs/index.html` is plain HTML/CSS/JS. It reads the sport's `data.json` and `weeks/index.json`. It builds DOM with the `el()` helper; never use innerHTML with data.
4. No hand-typed notes: the old `notes.txt` was removed on purpose (owner doesn't enter anything by hand). Add it back only if asked.

## What each game card shows (v1)
Kickoff (Central time), home team, AP/CFP rank, record, season ATS record, spread (preferred book: consensus, then DraftKings, ESPN Bet, Bovada) with a toss-up flag under 3 points, win probability (from moneylines, else estimated from the spread), last 4 games per team (score, opponent and their record; per-game covered/missed was removed on purpose), kickoff weather (skipped for domes), SP+ national rank table per team (overall, offense, defense, from `/ratings/sp`, one optional call; latest only, no per-week history, so past weeks rely on the archive), stakes flags (ranked; one win from bowl eligibility), rest flags (off a bye; short week), AI news notes. Left sidebar lists all games with a find-a-team box.

## Testing locally
- `CFBD_API_KEY` as a terminal environment variable only, then `python scripts/build_data.py`.
- Serve with `python -m http.server 8000 --directory docs` (opening the file directly won't load data).
- On Windows, `pip install tzdata` first (needed for Central time).
- Always `git pull` before pushing: workflow runs commit the data files.

## Roadmap
- **Phase 2: AI-written notes (built, in `scripts/news.py`).** For teams whose game isn't final: headlines from Google News RSS, falling back to Bing News RSS (Google returns 503 to GitHub Actions servers; a source failing 3 times in a row is skipped for the run). Search "School Mascot" football, last 7 days, top 10 → Gemini free tier (`GEMINI_MODEL`, default `gemini-3.5-flash-lite`, then `gemini-3.8-flash`, then any stable Flash model the key's ListModels call returns; 2.x models were retired for new users in Nov 2026, so expect future retirements) returns JSON notes citing headline numbers. Notes citing nothing are dropped; links come from RSS, never from Gemini. Stored as `team.news` in data.json: `null` = not checked, `[]` = nothing notable. A later run in the same week (Friday) only adds notes: it keeps the earlier run's notes, sends Gemini only headlines newer than that run (plus the earlier notes so it won't repeat them), skips Gemini when there are none, keeps earlier notes on any failure, and checks never-checked teams first. The step stops early if 3 teams in a row fail, 8 teams fail in total, or 30 minutes pass (each team tries at most 2 models, 30 s timeout per call; the build job has a 50-minute hard timeout); `news_status` in data.json shows the outcome in the page header. Paced at about 5 s per Gemini call, so a full slate takes 10+ minutes. Key in Secrets as `GEMINI_API_KEY`; without it the run still works and simply skips news.
- **Week archive (built).** Each run also writes `docs/sports/<key>/weeks/{season}-{type}-{week:02d}.json` and `weeks/index.json`; the page's Week dropdown appears once 2+ weeks exist.
- **Slate flags (built, browser only).** "Add to my slate" button per card + "Only my slate" filter. Saved in localStorage by game id, one list per sport (`slate-<key>`), so flags don't sync between devices. Owner chose this over a database for now.
- **Maybe later: users and picks.** Would need a free backend (Firebase Spark suggested): Google sign-in, per-user picks locked at kickoff, scoring, dashboards. Moving slate flags to it would make them shared.
- Backlog: projected score. (Line movement and TV channel were dropped; don't add them.)

## Known caveats
- A pick'em site may set its own spread; the dashboard shows the market line. Pick against the league's number.
- GitHub pauses scheduled workflows on repos with no recent activity; re-enable before the 2027 season.
- Shares the CFBD and Gemini free keys with elyse_pickem (if the same keys are added as secrets), so heavy use here eats into her limits.
