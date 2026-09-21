// Build-time access to ../data/latest and ../data/history (committed by the ingest job).
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = join(process.cwd(), '..', 'data');
const LATEST = join(ROOT, 'latest');
const HISTORY = join(ROOT, 'history');

function readJson<T>(path: string, fallback: T): T {
  return existsSync(path) ? (JSON.parse(readFileSync(path, 'utf8')) as T) : fallback;
}

export interface TopLeader { code: string; winRate: number | null; matches: number | null }
export interface Player {
  id: number | string | null; rank: number; name: string; bounty: number; wins: number; losses: number;
  matches: number; winRate: number; country: string | null; topLeaders: TopLeader[];
}
export interface SourceStats { [k: string]: number | string | string[] | null | undefined }
export interface Leader {
  code: string; name: string; color: string | null; img: string | null;
  sources: Record<string, SourceStats>; matchups: Record<string, Record<string, SourceStats>>;
  deckCount: number; cardCount: number;
}
export interface Deck { hash: string; cards: { id: string; qty: number }[]; size: number; sources: Record<string, SourceStats> }
export interface DeckFile { leader: string; decks: Deck[]; topPilots: { name: string; rank: number; wins: number; games: number; winRate: number }[] }
export interface CardStat {
  id: string; games: number; usage: number; winRate: number; winRateVsLeader: number; avgCopies: number;
  firstWinRate: number | null; secondWinRate: number | null; quantities: { qty: number; games: number; winRate: number }[];
}
export interface CardFile {
  leader: string; cards: CardStat[]; openingHand: { id: string; rate: number }[];
  tech: Record<string, { id: string; lift: number; winRateWith: number; winRateWithout: number; gamesWith: number }[]>;
  leaderStats?: { games: number; winRate: number; uniqueDecklists: number; uniquePilots: number };
}
export interface Card { name: string; type: string; cost: string; color: string; power: string; counter: string; rarity: string; img: string; set: string }
export interface Meta { generatedAt: string; date: string; status: Record<string, string>; players: number; leaders: number; sources: { name: string; url: string; support?: string }[]; [k: string]: unknown }

/** Card image. Limitless CDN: no watermark and no Cross-Origin-Resource-Policy header, so it embeds cross-site.
 *  (Card Kaizoku's CDN 403s on foreign referers; Bandai's official images send CORP: same-site and are blocked by browsers.) */
export const imgUrl = (id: string) => `https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/one-piece/${id.split('-')[0]}/${id}_EN.webp`;
/** Inline onerror handler: swap in a labelled placeholder (promos are missing upstream). */
export const imgOnError = (_id: string) => `this.style.visibility='hidden';this.parentElement.classList.add('noimg')`;

export const meta = readJson<Meta>(join(LATEST, 'meta.json'), { generatedAt: '', date: '', status: {}, players: 0, leaders: 0, sources: [] });
export const players = readJson<Player[]>(join(LATEST, 'players.json'), []);
export const leaders = readJson<Record<string, Leader>>(join(LATEST, 'leaders.json'), {});
export const cards = readJson<Record<string, Card>>(join(LATEST, 'cards.json'), {});
for (const [id, c] of Object.entries(cards)) c.img = imgUrl(id);
for (const [code, l] of Object.entries(leaders)) l.img = imgUrl(code);

export const deckFile = (code: string) => readJson<DeckFile>(join(LATEST, 'decks', `${code}.json`), { leader: code, decks: [], topPilots: [] });
export const cardFile = (code: string) => readJson<CardFile>(join(LATEST, 'cards', `${code}.json`), { leader: code, cards: [], openingHand: [], tech: {} });
/** Per-player exact decklists from the Card Kaizoku player routes (ranked, private-lobby bucket). */
export interface PlayerMatchup { opp: string; games: number; winRate: number | null }
export interface PlayerDeck extends Deck { leader: string; leaderName: string | null; games: number; wins: number; winRate: number | null; matchups: PlayerMatchup[] }
export interface PlayerTurns { firstGames: number; firstWinRate: number | null; secondGames: number; secondWinRate: number | null }
export interface PlayerLeaderStat { code: string; name: string | null; games: number; wins: number; winRate: number | null; turns: PlayerTurns | null; matchups: PlayerMatchup[] }
export interface PlayerDecks {
  opbId: string; kzId: string; handle: string; window: { start: string; end: string; days: number }; isPrivate: boolean;
  matches: number; wins: number; winRate: number | null; avgTurns: number | null; turns: PlayerTurns | null;
  coinflip: { won: number; lost: number; winRateAfterWin: number | null; winRateAfterLoss: number | null };
  leaders: PlayerLeaderStat[]; decks: PlayerDeck[];
}
export const playerDecks = (id: string | number) => readJson<PlayerDecks | null>(join(LATEST, 'players', `${id}.json`), null);
export const playersWithDecks = (): Set<string> => new Set(existsSync(join(LATEST, 'players')) ? readdirSync(join(LATEST, 'players')).map((f) => f.replace(/\.json$/, '')) : []);

export const bountyHistory =(id: string | number) => readJson<[string, number, number][]>(join(HISTORY, 'bounty', `${id}.json`), []);
export const leaderHistory = (code: string) => readJson<(string | number | null)[][]>(join(HISTORY, 'leaders', `${code}.json`), []);

export const card = (id: string): Card => cards[id] ?? { name: id, type: '', cost: '', color: '', power: '', counter: '', rarity: '', img: imgUrl(id), set: id.split('-')[0] };
export const leaderName = (code: string) => leaders[code]?.name ?? card(code).name ?? code;

/** Leaders sorted by Card Kaizoku match volume, then OPBounty. */
export const leadersByVolume = (): Leader[] =>
  Object.values(leaders).sort((a, b) => volume(b) - volume(a));
export const volume = (l: Leader) => Number(l.sources.kaizoku?.matches ?? 0) + Number(l.sources.opbounty?.matches ?? 0);

/** Consolidated deck stats. The ladder feed and the ranked-decklist feed describe the same match pool, so we take the
 *  larger of the two rather than adding them; the sim-wide pool is a separate set of games and is added. Win rate is games-weighted. */
export const deckStats = (d: Deck): { games: number; winRate: number | null; pilots: number | null } => {
  if (d.sources.player) return { games: Number(d.sources.player.games ?? 0), winRate: d.sources.player.winRate == null ? null : Number(d.sources.player.winRate), pilots: null };
  const o = d.sources.opbounty, n = d.sources.optcgone, k = d.sources.kaizokuBest ?? d.sources.kaizokuPlayed;
  const ladder = (Number(o?.games ?? 0) >= Number(n?.games ?? 0) ? o : n) ?? null;
  const pools = [ladder, k].filter(Boolean) as SourceStats[];
  const games = pools.reduce((s, p) => s + Number(p.games ?? 0), 0);
  const wins = pools.reduce((s, p) => s + Number(p.games ?? 0) * Number(p.winRate ?? 0) / 100, 0);
  return { games, winRate: games ? Math.round((wins / games) * 1000) / 10 : null, pilots: k?.pilots != null ? Number(k.pilots) : null };
};
export const deckGames = (d: Deck) => deckStats(d).games;
export const deckWinRate = (d: Deck) => deckStats(d).winRate;

/** Berry sign for bounty values. */
export const berry = (n: number | null | undefined) => (n == null ? '–' : `฿${fmt(n, 0)}`);

/** Matchup cell for leader a vs b: prefer the sim-wide pool (has 1st/2nd), fall back to the ladder pool. */
export const matchup = (a: string, b: string) => {
  const m = leaders[a]?.matchups?.[b];
  const s = m?.kaizoku ?? m?.opbounty;
  return s ? { games: Number(s.games ?? 0), winRate: s.winRate == null ? null : Number(s.winRate), first: s.firstWinRate == null ? null : Number(s.firstWinRate), second: s.secondWinRate == null ? null : Number(s.secondWinRate) } : null;
};
/** Leader headline numbers from whichever pool has them. */
export const leaderStats = (l: Leader) => {
  const k = l.sources.kaizoku ?? {}, o = l.sources.opbounty ?? {};
  return {
    games: Number(k.matches ?? 0) + Number(o.matches ?? 0),
    winRate: (k.winRate ?? o.winRate) == null ? null : Number(k.winRate ?? o.winRate),
    weighted: k.weightedWinRate == null ? null : Number(k.weightedWinRate),
    playRate: k.playRate == null ? (o.popularity == null ? null : Number(o.popularity)) : Number(k.playRate),
    first: k.firstWinRate == null ? null : Number(k.firstWinRate),
    second: k.secondWinRate == null ? null : Number(k.secondWinRate),
    avgDuration: o.avgDuration == null ? null : Number(o.avgDuration),
  };
};
export const heat = (wr: number | null) => wr == null ? 'transparent' : wr >= 55 ? 'rgba(79,209,143,.32)' : wr >= 50 ? 'rgba(79,209,143,.14)' : wr >= 45 ? 'rgba(239,90,96,.14)' : 'rgba(239,90,96,.32)';

export const fmt = (n: number | string | null | undefined, digits = 1) =>
  n == null || n === '' ? '–' : typeof n === 'number' ? n.toLocaleString('en-US', { maximumFractionDigits: digits }) : String(n);
export const pct = (n: number | string | null | undefined) => (n == null ? '–' : `${fmt(n)}%`);

export const TYPE_ORDER = ['LEADER', 'CHARACTER', 'EVENT', 'STAGE', 'DON'];
export const colorVar = (color: string | null | undefined) => {
  const c = (color ?? '').split('/')[0].toLowerCase();
  return ({ red: '#d64545', green: '#3f9f5f', blue: '#3d7bd9', purple: '#8a5cc7', black: '#5a5a6a', yellow: '#d9b83d' } as Record<string, string>)[c] ?? '#7a7a8a';
};

export const flag = (country: string | null) => {
  if (!country) return '';
  const code = COUNTRY_CODES[country];
  return code ? String.fromCodePoint(...[...code].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65)) : '';
};
const COUNTRY_CODES: Record<string, string> = {
  'United States': 'US', 'Brazil': 'BR', 'Japan': 'JP', 'Germany': 'DE', 'France': 'FR', 'Italy': 'IT', 'Spain': 'ES',
  'United Kingdom': 'GB', 'Canada': 'CA', 'Mexico': 'MX', 'Argentina': 'AR', 'Chile': 'CL', 'Colombia': 'CO', 'Peru': 'PE',
  'Australia': 'AU', 'Philippines': 'PH', 'Indonesia': 'ID', 'Malaysia': 'MY', 'Singapore': 'SG', 'Thailand': 'TH',
  'South Korea': 'KR', 'Taiwan': 'TW', 'Hong Kong': 'HK', 'China': 'CN', 'India': 'IN', 'Nigeria': 'NG', 'Portugal': 'PT',
  'Netherlands': 'NL', 'Belgium': 'BE', 'Poland': 'PL', 'Sweden': 'SE', 'Norway': 'NO', 'Denmark': 'DK', 'Finland': 'FI',
  'Austria': 'AT', 'Switzerland': 'CH', 'Turkey': 'TR', 'Russia': 'RU', 'Ukraine': 'UA', 'Greece': 'GR', 'Ireland': 'IE',
  'New Zealand': 'NZ', 'South Africa': 'ZA', 'Egypt': 'EG', 'Saudi Arabia': 'SA', 'United Arab Emirates': 'AE',
  'Vietnam': 'VN', 'Venezuela': 'VE', 'Ecuador': 'EC', 'Uruguay': 'UY', 'Costa Rica': 'CR', 'Dominican Republic': 'DO',
  'Puerto Rico': 'PR', 'Czech Republic': 'CZ', 'Hungary': 'HU', 'Romania': 'RO', 'Israel': 'IL',
};

/* ---------------------------------------------------------------- time windows */
export type Win = '7d' | '30d';
export const WINDOWS: Record<Win, { label: string; short: string; base: string }> = {
  '7d': { label: 'Last 7 Days', short: '7 days', base: '' },
  '30d': { label: 'Last 30 Days', short: '30 days', base: '/30d' },
};
export const WINDOW_KEYS = Object.keys(WINDOWS) as Win[];
const WDIR = join(ROOT, 'windows');
export interface WindowMeta { days: number; targetDays: number; start: string | null; end: string | null; matches: number }
export const windowMeta = (w: Win): WindowMeta | null =>
  w === '7d' ? { days: 7, targetDays: 7, start: null, end: meta.date, matches: Object.values(leaders).reduce((n, l) => n + Number(l.sources.kaizoku?.matches ?? 0), 0) / 2 }
             : readJson<WindowMeta | null>(join(WDIR, w, 'meta.json'), null);
const windowLeadersCache: Partial<Record<Win, Record<string, Leader>>> = {};
export const leadersFor = (w: Win): Record<string, Leader> => {
  if (w === '7d') return leaders;
  if (!windowLeadersCache[w]) {
    const ls = readJson<Record<string, Leader>>(join(WDIR, w, 'leaders.json'), {});
    for (const [code, l] of Object.entries(ls)) { l.img = imgUrl(code); l.color ??= leaders[code]?.color ?? null; }
    windowLeadersCache[w] = ls;
  }
  return windowLeadersCache[w]!;
};
export const leadersByVolumeFor = (w: Win): Leader[] => Object.values(leadersFor(w)).filter((l) => volume(l) > 0).sort((a, b) => volume(b) - volume(a));
export const deckFileFor = (w: Win, code: string): DeckFile =>
  w === '7d' ? deckFile(code) : readJson<DeckFile>(join(WDIR, w, 'decks', `${code}.json`), { leader: code, decks: [], topPilots: [] });
export const matchupIn = (ls: Record<string, Leader>, a: string, b: string) => {
  const m = ls[a]?.matchups?.[b];
  const s = m?.kaizoku ?? m?.opbounty;
  return s ? { games: Number(s.games ?? 0), winRate: s.winRate == null ? null : Number(s.winRate), first: s.firstWinRate == null ? null : Number(s.firstWinRate), second: s.secondWinRate == null ? null : Number(s.secondWinRate) } : null;
};
/** Route prefix for a window: '' for the default 7-day view, '/30d' otherwise. */
export const wpath = (w: Win, path: string) => `${WINDOWS[w].base}${path}`;

/** Pilot score for "Best Pilots": win rate with the leader, shrunk toward 50% for small samples, plus a bounty bonus.
 *  +1 point per 200 bounty above 3,000 so a ฿4,500 pilot at 60% outranks a ฿3,000 pilot at 62%. */
export const pilotScore = (winRate: number | null, games: number | null, bounty: number) => {
  const g = games ?? 0, wr = winRate ?? 50;
  const adj = (wr * g + 50 * 20) / (g + 20);
  return Math.round((adj + (bounty - 3000) / 200) * 10) / 10;
};

/** Title Case for headings (keeps all-caps tokens like OPTCG). */
export const titleCase = (s: string) => s.replace(/\b([a-z])(\w*)/g, (_m, a: string, b: string) => a.toUpperCase() + b);

export const listDeckFiles = () => (existsSync(join(LATEST, 'decks')) ? readdirSync(join(LATEST, 'decks')).map((f) => f.replace(/\.json$/, '')) : []);
