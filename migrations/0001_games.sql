-- Games from each sport's feed (scripts/build_data.py -> feed.json -> POST /api/ingest).
-- All times are UTC ISO strings ending in Z (e.g. 2026-11-28T17:00:00.000Z), so text order = time order.
CREATE TABLE games (
  id            TEXT PRIMARY KEY,          -- "<sport>:<source id>", e.g. cfb:401866458
  sport         TEXT NOT NULL,
  season        INTEGER NOT NULL,
  season_type   TEXT NOT NULL,             -- regular | postseason
  week          INTEGER NOT NULL,
  start         TEXT NOT NULL,             -- kickoff as the source gives it (placeholder when TBA)
  start_tba     INTEGER NOT NULL DEFAULT 0,
  lock_at       TEXT NOT NULL,             -- picks close; once passed it never moves
  freeze_at     TEXT NOT NULL,             -- spread mode: line freezes at the first check after this
  home          TEXT NOT NULL,
  away          TEXT NOT NULL,
  home_ap       INTEGER,                   -- AP rank 1-25; stops updating once freeze_at passes
  away_ap       INTEGER,
  spread        REAL,                      -- latest line, home team's side (-3.5 = home favored)
  spread_at     TEXT,                      -- when the latest line last changed
  frozen_spread REAL,                      -- the line leagues pick against; set once, never changed
  frozen_at     TEXT,
  completed     INTEGER NOT NULL DEFAULT 0,
  home_pts      INTEGER,
  away_pts      INTEGER,
  updated_at    TEXT NOT NULL
);

CREATE INDEX games_by_week ON games (sport, season, season_type, week);
CREATE INDEX games_to_freeze ON games (freeze_at) WHERE frozen_spread IS NULL;
