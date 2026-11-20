// Pick'em Lab server (Cloudflare Worker, free plan).
// Static files in docs/ are served before this code runs; it only sees requests that aren't files.
//
// POST /api/ingest   GitHub Actions sends a sport's feed.json (needs the INGEST_SECRET bearer token).
// cron (hourly)      freezes lines whose freeze time has passed.
//
// Every time check uses this server's clock, never the browser's.

const SPORTS = new Set(['cfb']);
const MAX_GAMES = 1000;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/api/ingest') {
        if (request.method !== 'POST') return json({ error: 'Use POST' }, 405);
        if (!(await authorized(request, env.INGEST_SECRET))) return json({ error: 'Not allowed' }, 401);
        return json(await ingest(env.DB, await request.json()));
      }
      return json({ error: 'Not found' }, 404);
    } catch (err) {
      if (err instanceof BadRequest) return json({ error: err.message }, 400);
      console.error(err);
      return json({ error: 'Server error' }, 500);
    }
  },

  async scheduled(_event, env) {
    const frozen = await freezeLines(env.DB, nowIso());
    console.log(`Froze ${frozen} lines`);
  },
};

class BadRequest extends Error {}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const nowIso = () => new Date().toISOString();

// Any time string -> the one UTC format stored in the database (so text order = time order).
function iso(value, name) {
  const t = new Date(value);
  if (!value || Number.isNaN(t.getTime())) throw new BadRequest(`Bad time in ${name}: ${value}`);
  return t.toISOString();
}

async function authorized(request, secret) {
  if (!secret) return false;   // secret not set on the server: refuse everything
  const given = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(secret);
  return a.length === b.length && crypto.subtle.timingSafeEqual(a, b);
}

const intOrNull = v => (v === null || v === undefined || v === '' ? null : Math.trunc(Number(v)));
const numOrNull = v => (v === null || v === undefined || v === '' ? null : Number(v));

// Upserts every game in the feed, then freezes any lines that are due.
// Rules kept here so a later feed can never undo them:
//   - lock_at never moves once it has passed (a TBA game that already locked stays locked)
//   - AP ranks and freeze_at stop changing once freeze_at has passed (the week's slate is settled)
//   - frozen_spread is never touched by a feed
async function ingest(db, feed) {
  if (!feed || !SPORTS.has(feed.sport)) throw new BadRequest('Unknown or missing sport');
  const games = Array.isArray(feed.games) ? feed.games : [];
  if (games.length > MAX_GAMES) throw new BadRequest('Too many games');
  const now = nowIso();

  const upsert = db.prepare(`
    INSERT INTO games (id, sport, season, season_type, week, start, start_tba, lock_at, freeze_at,
                       home, away, home_ap, away_ap, spread, spread_at, completed, home_pts, away_pts, updated_at)
    VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14,
            CASE WHEN ?14 IS NULL THEN NULL ELSE ?18 END, ?15, ?16, ?17, ?18)
    ON CONFLICT (id) DO UPDATE SET
      season = excluded.season, season_type = excluded.season_type, week = excluded.week,
      start = excluded.start, start_tba = excluded.start_tba,
      lock_at   = CASE WHEN games.lock_at <= ?18 THEN games.lock_at ELSE excluded.lock_at END,
      freeze_at = CASE WHEN games.freeze_at <= ?18 THEN games.freeze_at ELSE excluded.freeze_at END,
      home = excluded.home, away = excluded.away,
      home_ap = CASE WHEN games.freeze_at <= ?18 THEN games.home_ap ELSE excluded.home_ap END,
      away_ap = CASE WHEN games.freeze_at <= ?18 THEN games.away_ap ELSE excluded.away_ap END,
      spread_at = CASE WHEN excluded.spread IS games.spread THEN games.spread_at ELSE ?18 END,
      spread = excluded.spread,
      completed = excluded.completed, home_pts = excluded.home_pts, away_pts = excluded.away_pts,
      updated_at = excluded.updated_at`);

  const statements = games.map((g, i) => {
    const where = `games[${i}]`;
    if (typeof g.id !== 'string' || !g.id.startsWith(`${feed.sport}:`)) throw new BadRequest(`Bad id in ${where}`);
    if (!g.home || !g.away) throw new BadRequest(`Missing team in ${where}`);
    return upsert.bind(
      g.id, feed.sport, intOrNull(g.season) ?? intOrNull(feed.season), String(g.season_type || 'regular'), intOrNull(g.week) ?? 0,
      iso(g.start, `${where}.start`), g.start_tba ? 1 : 0, iso(g.lock_at, `${where}.lock_at`), iso(g.freeze_at, `${where}.freeze_at`),
      String(g.home), String(g.away), intOrNull(g.home_ap), intOrNull(g.away_ap), numOrNull(g.spread),
      g.completed ? 1 : 0, intOrNull(g.home_pts), intOrNull(g.away_pts), now);
  });
  if (statements.length) await db.batch(statements);
  const frozen = await freezeLines(db, now);
  return { sport: feed.sport, received: games.length, frozen };
}

// Freezes the latest line for games whose freeze time has passed and that haven't locked yet.
// A game with no line by then is frozen the first time a line shows up, as long as it hasn't locked.
async function freezeLines(db, now) {
  const result = await db.prepare(`
    UPDATE games SET frozen_spread = spread, frozen_at = ?1
    WHERE frozen_spread IS NULL AND spread IS NOT NULL AND freeze_at <= ?1 AND lock_at > ?1`).bind(now).run();
  return result.meta.changes;
}
