# Pick'em Prep

A free dashboard for Yahoo College Pick'em research. Every Thursday and Friday morning, GitHub runs a small Python script that collects the week's games, betting lines, recent results, rankings and kickoff weather, then publishes them as one web page. The page helps with research; it never makes the pick.

## What's in here

| Path | What it does |
| --- | --- |
| `scripts/build_data.py` | Pulls the data and writes `docs/data.json`. Standard library only, nothing to install. |
| `docs/index.html` | The dashboard page. Reads `data.json` and `notes.txt`. |
| `docs/notes.txt` | Notes typed by hand, one per line: `Team name: the note`. |
| `docs/data.json` | This week's data. The workflow rewrites it on every run. |
| `.github/workflows/update.yml` | The schedule (Thu and Fri, 7:07 AM Central) and the publishing steps. |

## One-time setup

1. **Add the data key.** Settings → Secrets and variables → Actions → New repository secret. Name: `CFBD_API_KEY`. Value: your CollegeFootballData key.
2. **Turn on Pages.** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
3. **Upload the files** (keep the folders): Code tab → Add file → Upload files.
4. **Run it once.** Actions tab → Update dashboard → Run workflow.
5. **Open the page** at `https://<your-username>.github.io/elyse_pickem/`.

## Everyday use

- **Add a note:** open `docs/notes.txt`, click the pencil, add a line such as `Texas A&M: Starting QB questionable`, then Commit changes. The page updates in a minute or two.
- **Refresh early:** Actions → Update dashboard → Run workflow.
- **Rebuild a specific week:** add `WEEK: '13'` under `env:` in the "Pull this week's games" step, run it, then remove it.

## Cost

$0. The repository is public, so Actions and Pages are free. Each run makes about 6 CollegeFootballData calls (the free key allows 1,000 a month) and one free Open-Meteo call per outdoor game in the next 15 days.

## If a run fails

Open the failed run in the Actions tab and read the red step. A message about the key means the `CFBD_API_KEY` secret is missing or mistyped. Anything else, copy the log text into your chat with Claude.
