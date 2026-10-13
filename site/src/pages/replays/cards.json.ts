import type { APIRoute } from 'astro';
import { cards, cardFlags, leaders, colorVar } from '../../lib/data';

/** Card names (and leader colours) for the viewer when it parses a combat log in the browser instead of using a matchup bundle. */
export const GET: APIRoute = () => {
  const names: Record<string, string> = {};
  const powers: Record<string, number> = {};
  const costs: Record<string, number> = {};
  const lead: Record<string, { name: string; color: string }> = {};
  for (const [id, c] of Object.entries(cards)) {
    names[id] = c.name;
    if (c.power && Number(c.power)) powers[id] = Number(c.power);
    if (c.cost !== '' && !Number.isNaN(Number(c.cost))) costs[id] = Number(c.cost);
    if (c.type === 'LEADER') lead[id] = { name: leaders[id]?.name ?? c.name, color: colorVar(leaders[id]?.color ?? c.color) };
  }
  return new Response(JSON.stringify({ names, leaders: lead, powers, costs, flags: cardFlags }), { headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'public, max-age=3600' } });
};
