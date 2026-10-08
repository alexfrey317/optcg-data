/** Browser port of ingest/sources/opbounty_replays.parse_log (PARSER_VERSION 2).
 *  Turns a raw OPBounty combat log into the anonymised step list the viewer replays. Keep in lockstep with the Python
 *  parser; `scripts/check-parser-port.mjs` diffs the two on stored logs. */
/** Card move: seat, card, fromZone, toZone, rested, deck, donDeck, donCost, donAttached, fromSlot, toSlot, faceUp, powerDelta, costDelta.
 *  Zones (from the sim's ReplaySyncZone): 0 deck (last slot = top), 1 hand, 2 characters (slot = board position), 3 life (last = top),
 *  4 DON deck, 5 DON cost area, 6 trash (last = top), 7 stage, 8 leader, 9 attached DON (slot = parent*100 + index, parent 99 = leader). */
export type Step =
  | ['m', 1 | 2, string, number, number, 0 | 1, number | null, number | null, number | null, number | null, number, number, 0 | 1, number, number]
  | ['e', 0 | 1 | 2, string, string]
  | ['s', 1 | 2, string[], string[], string[], number];
export interface MatchInfo { id: string; ts: string; w: { l: string; b: number; d?: string | null }; l: { l: string; b: number; d?: string | null }; decks?: Record<string, string> }
export interface Player { seat: 1 | 2; leader: string; bounty: number; won: boolean; first: boolean; deck: string | null; mulligan: string[] | null }
export interface Game { v: number; id: string; ts: string; date: string; players: Player[]; first: 1; winner: 1 | 2; endTurns: number; end: { how: string; loserLife: number | null; winnerLife: number | null }; finished: boolean; steps: Step[] }

export const PARSER_VERSION = 3;
const MIN_END_TURNS = 8, MAX_LOSER_LIFE = 2, LONG_GAME_TURNS = 12;
const MARK = /\[<mark><link="([^"]+)">[^<]*<\/link><\/mark>\]/g;
const TAG = /<\/?(?:b|i|size(?:=\d+)?|color(?:=[^>]+)?)>/g;
const ACTOR = /^\[(.+?)\] (.*)$/;
const PLY = /^RZ1\|PLY\|([12])\|(.+?)\|([A-Z0-9]+-\d+)\s*$/;
const CARD_ID = /\[([A-Z]{1,3}\d{2}-\d{3}(?:_p\d+)?)\]/g;
const LIST = /^(Hand|Board|Trash) before Mulligan: \[(.*)\]$|^(Hand|Board|Trash): \[(.*)\]$/;
const MOVE = /^RZ1\|(\d+)\|([12])\|([^|]+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d)\|(\d)\|(\d)\|(-?\d+)\|(-?\d+)/;
const CHK = /^RZ1\|CHK\|(\d+)\|([12])\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)/;
const DREW = /^Drew card from deck: .*\[([A-Z0-9]+-\d+(?:_p\d+)?)\]$/;
const ALIASES = new Set(['You', 'Your Client', 'Opponent']);
const SKIP_LINE = ['Waiting for a Connection', 'Version is'];

export function kindOf(t: string, actor: number): string {
  if (t === 'End Turn') return 'end';
  if (t === 'Concedes!') return 'concede';
  if (t.startsWith('Draw ') && t.includes('Don')) return 'don';
  if (t.startsWith('Draw ') || t.startsWith('Drew card')) return 'draw';
  if (t.startsWith('Deploy ')) return 'deploy';
  if (t.includes(' attacking ')) return 'attack';
  if (actor === 0 && t.includes('] vs ')) return 'combat';
  if (t === 'Attack Fails') return 'fail';
  if (t.includes(' hit for ')) return 'hit';
  if (t.endsWith(' Blocks')) return 'block';
  if (t.includes(' for Counter ') || t.includes(': Activate Counter')) return 'counter';
  if (t.startsWith('Attach ') || t.startsWith('Activate ')) return 'don';
  if (t.startsWith('Trash ') || t.includes(' Destroyed') || t.endsWith(' Destroyed')) return 'trash';
  if (t.includes('Mulligan') || t.startsWith('Placing Cards')) return 'mull';
  if (t.startsWith('Chose to go') || t.startsWith('Will select')) return 'info';
  return 'effect';
}

const deckIds = (text: string | null | undefined) => new Set((text || '').split(/\s+/).filter((t) => t.includes('x')).map((t) => t.slice(t.indexOf('x') + 1)));
const splitList = (s: string) => s.split(',').filter(Boolean);
const setDefault = <K, V>(m: Map<K, V>, k: K, v: V) => { if (!m.has(k)) m.set(k, v); };

type Row = ['t', string | null, string] | ['m', string, string, number, number, 0 | 1, number, number, 0 | 1, number, number] | ['c', string, [number, number, number, number]];

export function parseLog(raw: string, match: MatchInfo): Game | null {
  const decks = match.decks || {};
  const wl = match.w.l, ll = match.l.l;
  const deckW = deckIds(match.w.d ? decks[match.w.d] : null), deckL = deckIds(match.l.d ? decks[match.l.d] : null);
  const rows: Row[] = [];
  const leaderOf = new Map<string, string>();
  const cardsOf = new Map<string, Set<string>>();
  let conceder: string | null = null, choseFirst: string | null = null, firstEnd: string | null = null;
  const numLabel = new Map<string, string>();
  const pendingDraw = new Map<string, string>(), pendingText = new Map<string, string>();
  for (let line of raw.split('\n')) {
    line = line.replace(/\s+$/, '');
    let m = PLY.exec(line);
    if (m) { leaderOf.set(m[2].trim(), m[3]); setDefault(numLabel, m[1], m[2].trim()); continue; }
    m = MOVE.exec(line);
    if (m) {
      const zf = Number(m[4]), zt = Number(m[6]);
      if (zf === 0 && zt === 0) continue;
      rows.push(['m', m[2], m[3], zf, zt, Number(m[10]) as 0 | 1, Number(m[5]), Number(m[7]), m[8] === '1' && m[9] === '1' ? 1 : 0, Number(m[11]), Number(m[12])]);
      if (zf === 0 && zt === 1) {
        if (pendingText.has(m[3])) { setDefault(numLabel, m[2], pendingText.get(m[3])!); pendingText.delete(m[3]); }
        else pendingDraw.set(m[3], m[2]);
      }
      continue;
    }
    m = CHK.exec(line);
    if (m) { rows.push(['c', m[2], [Number(m[3]), Number(m[7]), Number(m[8]), Number(m[12])]]); continue; }
    if (!line || line.startsWith('RZ1|') || line.includes('<size=') || SKIP_LINE.some((p) => line.startsWith(p)) || line.endsWith(' Has Connected')) continue;
    let actor: string | null = null, text = line;
    m = ACTOR.exec(line);
    if (m) { actor = m[1]; text = m[2]; }
    text = text.replace(MARK, '[$1]').replace(TAG, '').trim();
    rows.push(['t', actor, text]);
    if (actor === null) continue;
    const d = DREW.exec(text);
    if (d) {
      if (pendingDraw.has(d[1])) { setDefault(numLabel, pendingDraw.get(d[1])!, actor); pendingDraw.delete(d[1]); }
      else pendingText.set(d[1], actor);
    }
    if (text.startsWith('Leader is ')) { const ids = [...text.matchAll(CARD_ID)].map((x) => x[1]); if (ids.length) leaderOf.set(actor, ids[ids.length - 1]); }
    else if (text === 'Concedes!') conceder = conceder ?? actor;
    else if (text.startsWith('Chose to go ')) choseFirst = choseFirst ?? (text.endsWith('First') ? actor : '!' + actor);
    else if (text === 'End Turn') firstEnd = firstEnd ?? actor;
    else { const lm = LIST.exec(text); if (lm) { if (!cardsOf.has(actor)) cardsOf.set(actor, new Set()); for (const c of splitList(lm[2] || lm[4] || '')) cardsOf.get(actor)!.add(c); } }
  }

  const labels = new Set<string>([...leaderOf.keys(), ...cardsOf.keys(), ...rows.filter((r) => r[0] === 't' && r[1]).map((r) => r[1] as string)]);
  const side = new Map<string, 'w' | 'l'>();
  if (wl !== ll) for (const [lab, ld] of leaderOf) { if (ld === wl) side.set(lab, 'w'); else if (ld === ll) side.set(lab, 'l'); }
  if (conceder) setDefault(side, conceder, 'l');
  const sameDeck = deckW.size === deckL.size && [...deckW].every((c) => deckL.has(c));
  if (deckW.size && deckL.size && !sameDeck) {
    for (const [lab, cs] of cardsOf) {
      if (side.has(lab)) continue;
      let sw = 0, sl = 0; for (const c of cs) { if (deckW.has(c)) sw++; if (deckL.has(c)) sl++; }
      if (sw !== sl) side.set(lab, sw > sl ? 'w' : 'l');
    }
  }
  const aliasGroup = [...labels].filter((x) => ALIASES.has(x)), nameGroup = [...labels].filter((x) => !ALIASES.has(x));
  for (const group of [aliasGroup, nameGroup]) {
    const known = new Set(group.filter((x) => side.has(x)).map((x) => side.get(x)!));
    if (group.length === 2 && known.size === 1) for (const x of group) setDefault(side, x, known.has('w') ? 'l' : 'w');
  }
  const sides = new Set(side.values());
  if ([...labels].some((lab) => !side.has(lab)) || !sides.has('w') || !sides.has('l')) return null;
  let firstSide: 'w' | 'l';
  if (choseFirst) {
    firstSide = choseFirst.startsWith('!') ? side.get(choseFirst.slice(1))! : side.get(choseFirst)!;
    if (choseFirst.startsWith('!')) firstSide = firstSide === 'w' ? 'l' : 'w';
  } else if (firstEnd) firstSide = side.get(firstEnd)!;
  else return null;
  const seatOfSide: Record<'w' | 'l', 1 | 2> = { w: firstSide === 'w' ? 1 : 2, l: firstSide === 'l' ? 1 : 2 };
  const seat = new Map<string, 1 | 2>(); for (const [lab, s] of side) seat.set(lab, seatOfSide[s]);
  const numSeat = new Map<string, 1 | 2>(); for (const [n, lab] of numLabel) if (seat.has(lab)) numSeat.set(n, seat.get(lab)!);
  if (numSeat.size === 1) { const [n, s] = [...numSeat.entries()][0]; numSeat.set(n === '1' ? '2' : '1', (3 - s) as 1 | 2); }
  if (!(numSeat.has('1') && numSeat.has('2') && numSeat.size === 2) || new Set(numSeat.values()).size !== 2) return null;
  const winner = seatOfSide.w, loser = (3 - winner) as 1 | 2;
  const leaders: Record<number, string> = { [seatOfSide.w]: wl, [seatOfSide.l]: ll };

  const steps: Step[] = [];
  const life: Record<number, number | null> = { 1: null, 2: null };
  const mull: Record<number, string[]> = {};
  let ends = 0; const snap: Record<number, Record<string, string[]>> = { 1: {}, 2: {} };
  for (const r of rows) {
    if (r[0] === 'm') steps.push(['m', numSeat.get(r[1])!, r[2], r[3], r[4], r[5], null, null, null, null, r[6], r[7], r[8], r[9], r[10]]);
    else if (r[0] === 'c') { const last = steps[steps.length - 1]; if (last && last[0] === 'm' && last[6] === null) { last[6] = r[2][0]; last[7] = r[2][1]; last[8] = r[2][2]; last[9] = r[2][3]; } }
    else {
      const [, actor, text] = r;
      const s = actor ? seat.get(actor)! : 0;
      if (s && text.startsWith('Leader is ')) continue;
      const lm = LIST.exec(text);
      if (lm && s) {
        if (lm[1]) mull[s] = splitList(lm[2]);
        else snap[s][lm[3].toLowerCase()] = splitList(lm[4]);
        continue;
      }
      if (s && text.startsWith('Life: ')) {
        life[s] = Number(text.slice(6));
        steps.push(['s', s, snap[s].hand ?? [], snap[s].board ?? [], snap[s].trash ?? [], life[s]!]);
        snap[s] = {};
        continue;
      }
      if (s && (text.startsWith('Chose to go ') || text.startsWith('Will select'))) continue;
      const kind = kindOf(text, s);
      if (kind === 'end') ends++;
      steps.push(['e', s as 0 | 1 | 2, kind, text]);
    }
  }
  const finished = ends >= MIN_END_TURNS && ((life[loser] !== null && life[loser]! <= MAX_LOSER_LIFE) || ends >= LONG_GAME_TURNS);
  const how = conceder ? 'concede' : life[loser] === 0 ? 'lethal' : 'unknown';
  const players: Player[] = [];
  for (const s of [1, 2] as const) {
    const m = s === winner ? match.w : match.l;
    players.push({ seat: s, leader: leaders[s], bounty: m.b, won: s === winner, first: s === 1, deck: m.d ? decks[m.d] ?? null : null, mulligan: mull[s] ?? null });
  }
  return { v: PARSER_VERSION, id: match.id, ts: match.ts, date: match.ts.slice(0, 10), players, first: 1, winner, endTurns: ends,
    end: { how, loserLife: life[loser], winnerLife: life[winner] }, finished, steps };
}
