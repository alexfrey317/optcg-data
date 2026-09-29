// Diff the browser parser against the Python parser's stored output.
//   cd site && npx -y -p node@22 node --experimental-strip-types scripts/check-parser-port.ts [N]
// Reads data/replays/raw/<id>.log.gz and compares parseLog() with data/replays/games/<id>.json.gz.
import { readdirSync, readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { join } from 'node:path';
import { parseLog } from '../src/lib/replayParse.ts';

const ROOT = join(import.meta.dirname, '..', '..', 'data', 'replays');
const n = Number(process.argv[2] || 200);
const ids = readdirSync(join(ROOT, 'games')).filter((f) => f.endsWith('.json.gz')).map((f) => f.slice(0, -8)).sort().slice(0, n);
let ok = 0, diff = 0, missing = 0;
for (const id of ids) {
  let raw: string;
  try { raw = gunzipSync(readFileSync(join(ROOT, 'raw', `${id}.log.gz`))).toString('utf8'); } catch { missing++; continue; }
  const py = JSON.parse(gunzipSync(readFileSync(join(ROOT, 'games', `${id}.json.gz`))).toString('utf8'));
  const P: Record<number, any> = {}; for (const p of py.players) P[p.seat] = p;
  const W = P[py.winner], L = P[3 - py.winner];
  const decks: Record<string, string> = {}; if (W.deck) decks.w = W.deck; if (L.deck) decks.l = L.deck;
  const g = parseLog(raw, { id, ts: py.ts, w: { l: W.leader, b: W.bounty, d: W.deck ? 'w' : null }, l: { l: L.leader, b: L.bounty, d: L.deck ? 'l' : null }, decks });
  if (!g) { diff++; console.log(id, 'port returned null'); continue; }
  const a = JSON.stringify({ players: g.players, winner: g.winner, endTurns: g.endTurns, end: g.end, finished: g.finished, steps: g.steps });
  const b = JSON.stringify({ players: py.players, winner: py.winner, endTurns: py.endTurns, end: py.end, finished: py.finished, steps: py.steps });
  if (a === b) ok++;
  else {
    diff++;
    const i = [...a].findIndex((c, k) => c !== b[k]);
    console.log(id, 'differs at', i, '\n  port:', a.slice(Math.max(0, i - 80), i + 120), '\n  py:  ', b.slice(Math.max(0, i - 80), i + 120));
  }
}
console.log({ ok, diff, missing, of: ids.length });
process.exit(diff ? 1 : 0);
