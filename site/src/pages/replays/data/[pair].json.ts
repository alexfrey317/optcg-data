import type { APIRoute, GetStaticPaths } from 'astro';
import { replayBest, replayGame, leaders, card, colorVar } from '../../../lib/data';

/** One bundle per matchup: every selected game of the pair plus the card names it needs. The viewer at /replays/g/ fetches it. */
const CARD = /\[([A-Z]{1,3}\d{2}-\d{3}(?:_p\d+)?)\]/g;
const pairSlug = (k: string) => k.replace('|', '__');

export const getStaticPaths: GetStaticPaths = () => Object.keys(replayBest().pairs).map((k) => ({ params: { pair: pairSlug(k) } }));

export const GET: APIRoute = ({ params }) => {
  const key = String(params.pair).replace('__', '|');
  const best = replayBest();
  const buckets = best.pairs[key] ?? {};
  const ids = [...new Set(Object.values(buckets).flatMap((gs) => gs.map((g) => g.id)))];
  const games: Record<string, unknown> = {};
  const used = new Set<string>();
  for (const id of ids) {
    const g = replayGame(id);
    if (!g) continue;
    games[id] = g;
    for (const p of g.players) { used.add(p.leader); for (const c of p.mulligan ?? []) used.add(c); for (const t of (p.deck ?? '').split(' ')) if (t.includes('x')) used.add(t.split('x', 2)[1]); }
    for (const st of g.steps ?? []) {
      if (st[0] === 'm') { if (st[2] !== 'Don') used.add(String(st[2])); }
      else if (st[0] === 'e') { for (const m of String(st[3]).matchAll(CARD)) used.add(m[1]); }
      else if (st[0] === 's') { for (const z of [st[2], st[3], st[4]] as string[][]) for (const c of z ?? []) used.add(c); }
    }
  }
  const names = Object.fromEntries([...used].map((id) => [id, card(id).name]));
  const lead = Object.fromEntries(key.split('|').map((c) => [c, { name: leaders[c]?.name ?? card(c).name ?? c, color: colorVar(leaders[c]?.color ?? card(c).color) }]));
  return new Response(JSON.stringify({ pair: key, leaders: lead, names, games }), { headers: { 'Content-Type': 'application/json; charset=utf-8' } });
};
