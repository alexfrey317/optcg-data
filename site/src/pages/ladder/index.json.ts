import type { APIRoute } from 'astro';
import { players, leaders, seasonRecord, colorVar, flag } from '../../lib/data';

/** Compact index of every ladder row, fetched by the ladder page only when someone searches or filters, so
 *  those work across all 10,000 players rather than the 200 on the current page.
 *  Row: [rank, id, name, country, bounty, games, wins, losses, winRate, "CODE:wr:games|CODE:wr:games|..."] */
export const GET: APIRoute = () => {
  const rows = players.map((p) => {
    const r = seasonRecord(p);
    return [p.rank, p.id, p.name, p.country ?? '', Math.round(p.bounty), r.games ?? 0, r.wins ?? 0, r.losses ?? 0, r.winRate ?? 0,
      p.topLeaders.slice(0, 3).map((t) => `${t.code}:${Math.round(t.winRate ?? 0)}:${t.matches ?? 0}`).join('|')];
  });
  const names: Record<string, [string, string]> = {};
  for (const [code, l] of Object.entries(leaders)) names[code] = [l.name, colorVar(l.color)];
  for (const p of players) for (const t of p.topLeaders) if (!names[t.code]) names[t.code] = [t.code, colorVar(null)];
  const flags: Record<string, string> = {};
  for (const p of players) if (p.country && !(p.country in flags)) flags[p.country] = flag(p.country);
  return new Response(JSON.stringify({ leaders: names, flags, rows }), { headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'public, max-age=3600' } });
};
