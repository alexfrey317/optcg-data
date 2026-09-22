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
export interface DeckFile { leader: string; decks: Deck[]; topPilots: { name: string; rank?: number; wins: number; games: number; winRate: number | null; handle?: string; bounty?: number; ladderId?: string | null }[] }
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
export const imgOnError = (_id: string): string | undefined => undefined; // handled once per page by the delegated listener in Base.astro

export const meta = readJson<Meta>(join(LATEST, 'meta.json'), { generatedAt: '', date: '', status: {}, players: 0, leaders: 0, sources: [] });
export const players = readJson<Player[]>(join(LATEST, 'players.json'), []);
/** The ladder's top 1,000: what the home page and "Top-1000" counts use; player pages exist for every pulled row. */
export const top1000 = players.slice(0, 1000);
export const LADDER_PAGE = 200;
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
  opbId: string; kzId: string; handle: string; via?: 'handle' | 'name'; window: { start: string; end: string; days: number }; isPrivate: boolean;
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
export const volume = (l: Leader) => l.sources.ranked ? Number(l.sources.ranked.matches ?? 0) : Number(l.sources.kaizoku?.matches ?? 0) + Number(l.sources.opbounty?.matches ?? 0);

/** Consolidated deck stats. The ladder feed and the ranked-decklist feed describe the same match pool, so we take the
 *  larger of the two rather than adding them; the sim-wide pool is a separate set of games and is added. Win rate is games-weighted. */
export const deckStats = (d: Deck): { games: number; winRate: number | null; pilots: number | null } => {
  if (d.sources.player) return { games: Number(d.sources.player.games ?? 0), winRate: d.sources.player.winRate == null ? null : Number(d.sources.player.winRate), pilots: null };
  // the ranked match archive is the complete pool; when present it supersedes the sampled feeds
  if (d.sources.ranked) return { games: Number(d.sources.ranked.games ?? 0), winRate: d.sources.ranked.winRate == null ? null : Number(d.sources.ranked.winRate), pilots: d.sources.ranked.pilots == null ? null : Number(d.sources.ranked.pilots) };
  const o = d.sources.opbounty, n = d.sources.optcgone, k = d.sources.kaizokuBest ?? d.sources.kaizokuPlayed;
  const ladder = (Number(o?.games ?? 0) >= Number(n?.games ?? 0) ? o : n) ?? null;
  const pools = [ladder, k].filter(Boolean) as SourceStats[];
  const games = pools.reduce((s, p) => s + Number(p.games ?? 0), 0);
  const wins = pools.reduce((s, p) => s + Number(p.games ?? 0) * Number(p.winRate ?? 0) / 100, 0);
  return { games, winRate: games ? Math.round((wins / games) * 1000) / 10 : null, pilots: k?.pilots != null ? Number(k.pilots) : null };
};
/** "Most successful" ordering: win rate shrunk toward 50% for lists with few games, so a 3-0 list
 *  does not outrank a 60% list with 200 games. K=25 games of prior weight. */
export const deckScore = (s: { games: number; winRate: number | null }) => {
  const g = s.games ?? 0, wr = s.winRate ?? 50;
  return Math.round(((wr * g + 50 * 25) / (g + 25)) * 100) / 100;
};
export const deckGames = (d: Deck) => deckStats(d).games;
export const deckWinRate = (d: Deck) => deckStats(d).winRate;

/** Berry sign for bounty values. */
export const berry = (n: number | null | undefined) => (n == null ? '–' : `฿${fmt(n, 0)}`);

/** Matchup cell for leader a vs b: prefer the sim-wide pool (has 1st/2nd), fall back to the ladder pool. */
export const matchup = (a: string, b: string) => {
  return matchupIn(leaders, a, b);
};
/** Leader headline numbers from whichever pool has them. */
export const leaderStats = (l: Leader) => {
  const k = l.sources.kaizoku ?? {}, o = l.sources.opbounty ?? {}, r = l.sources.ranked;
  // games, win rate and play rate come from the complete ranked archive when we have it; 1st/2nd only exist in the sim-wide feed
  const main = r ?? k;
  const games = r ? Number(r.matches ?? 0) : Number(k.matches ?? 0) + Number(o.matches ?? 0);
  // shrink toward 50% with a 1,000-game prior: a leader with 100 games at 58% lands near 50.7%, one with 40,000 games is untouched
  const wins = main.wins == null ? null : Number(main.wins);
  const weighted = wins != null && games ? Math.round(1000 * (wins + 500) / (games + 1000)) / 10 : main.weightedWinRate == null ? null : Number(main.weightedWinRate);
  return {
    games,
    winRate: (main.winRate ?? o.winRate) == null ? null : Number(main.winRate ?? o.winRate),
    weighted,
    playRate: main.playRate == null ? (o.popularity == null ? null : Number(o.popularity)) : Number(main.playRate),
    first: (r?.firstWinRate ?? k.firstWinRate) == null ? null : Number(r?.firstWinRate ?? k.firstWinRate),
    second: (r?.secondWinRate ?? k.secondWinRate) == null ? null : Number(r?.secondWinRate ?? k.secondWinRate),
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
const hasWindow = (w: Win) => existsSync(join(WDIR, w, 'leaders.json'));
export const windowMeta = (w: Win): WindowMeta | null =>
  hasWindow(w) ? readJson<WindowMeta | null>(join(WDIR, w, 'meta.json'), null)
  : w === '7d' ? { days: 7, targetDays: 7, start: null, end: meta.date, matches: Object.values(leaders).reduce((n, l) => n + Number(l.sources.kaizoku?.matches ?? 0), 0) / 2 }
  : null;
const windowLeadersCache: Partial<Record<Win, Record<string, Leader>>> = {};
export const leadersFor = (w: Win): Record<string, Leader> => {
  if (!hasWindow(w)) return w === '7d' ? leaders : {};
  if (!windowLeadersCache[w]) {
    const ls = readJson<Record<string, Leader>>(join(WDIR, w, 'leaders.json'), {});
    for (const [code, l] of Object.entries(ls)) { l.img = imgUrl(code); l.color ??= leaders[code]?.color ?? null; }
    windowLeadersCache[w] = ls;
  }
  return windowLeadersCache[w]!;
};
export const leadersByVolumeFor = (w: Win): Leader[] => Object.values(leadersFor(w)).filter((l) => volume(l) > 0).sort((a, b) => volume(b) - volume(a));
export const deckFileFor = (w: Win, code: string): DeckFile =>
  hasWindow(w) ? readJson<DeckFile>(join(WDIR, w, 'decks', `${code}.json`), { leader: code, decks: [], topPilots: [] }) : w === '7d' ? deckFile(code) : { leader: code, decks: [], topPilots: [] };
/** Card stats for a window from the complete published files; falls back to the weekly Card Kaizoku file. */
export const cardFileFor = (w: Win, code: string): CardFile & { complete: boolean } => {
  const p = join(WDIR, w, 'cards', `${code}.json`);
  return existsSync(p) ? { ...readJson<CardFile>(p, { leader: code, cards: [], openingHand: [], tech: {} }), complete: true } : { ...cardFile(code), complete: false };
};
export interface TopPilot { handle: string; name: string; games: number; wins: number; winRate: number | null; bounty: number; ladderId: string | null }
/** Per-player ranked history from the match archive (linked ladder players only). */
export interface PlayerGame { ts: string; id: string; side: 'w' | 'l'; b: number; leader: string; deck: string | null; opp: string; oppB: number; oppDeck: string | null; won: boolean }
export interface PlayerMatches { handle: string; name: string; games: PlayerGame[]; decks: Record<string, string> }
export const playerMatches = (id: string | number) => readJson<PlayerMatches | null>(join(LATEST, 'matches', `${id}.json`), null);
export const handleLinks = readJson<Record<string, { handle: string; name: string; confidence: string; gap: number; lastSeen: string; lastBounty: number; games: number }>>(join(LATEST, 'handles.json'), {});
/** Parse a compact "4xOP01-016 3xOP01-024" list into cards. */
/** OPBounty personal profile (Firestore PublicUsers): complete season record, top-3 leader stats, bounty-per-game series, newest public matches. */
export interface ProfileLeader { code: string; games: number; wins: number; losses: number; winRate: number | null; avgDuration: number | null; first: { games: number; winRate: number | null } | null; second: { games: number; winRate: number | null } | null }
export interface ProfileSide { id: string; nick: string; b: number; delta: number; status: string; leader: string | null; deck: string | null }
export interface ProfileMatch { idx: number; ts: string; dur: number; mode: number; p1: ProfileSide; p2: ProfileSide; winner: 'p1' | 'p2' | null }
export interface PlayerProfile { id: string; updated: string; writtenAt: string | null; wins: number; losses: number; games: number; winRate: number | null; avgDuration: number | null; leaders: ProfileLeader[]; graph: number[]; recent: ProfileMatch[]; decks: Record<string, string> }
export const playerProfile = (id: string | number) => readJson<PlayerProfile | null>(join(LATEST, 'profiles', `${id}.json`), null);
/** Best pilots per leader from OPBounty's leader-filtered ladder (complete): players with the leader among their most-played, ranked by bounty. */
export interface Pilot { id: string; name: string; leaderRank: number; rank: number | null; bounty: number; games: number | null; winRate: number | null; country: string | null }
export const pilotsFor = (code: string) => readJson<Pilot[]>(join(LATEST, 'pilots', `${code}.json`), []);
/** Season record for a ladder row: the player's own profile when we have it (complete), else the ladder's top-3 sum. */
export const seasonRecord = (p: Player) => {
  const pr = p.id == null ? null : playerProfile(p.id);
  // the ladder's count is the top-3-leader subset of the same data, so a profile reporting fewer games is stale: keep the ladder then
  return pr && pr.games && pr.games >= (p.matches || 0) ? { wins: pr.wins, losses: pr.losses, games: pr.games, winRate: pr.winRate, src: 'profile' as const }
    : { wins: p.wins, losses: p.losses, games: p.matches, winRate: p.winRate, src: 'ladder' as const };
};
export const profileCount =(): number => existsSync(join(LATEST, 'profiles')) ? readdirSync(join(LATEST, 'profiles')).length : 0;
/** Every public match row ever collected from profiles, grouped by ladder id (both sides). Loaded once per build. */
let _public: { byPlayer: Map<string, ProfileMatch[]>; decks: Record<string, string> } | null = null;
export const publicMatches = () => {
  if (_public) return _public;
  const dir = join(ROOT, 'matches', 'public');
  const byPlayer = new Map<string, ProfileMatch[]>(); const decks: Record<string, string> = {};
  if (existsSync(dir)) for (const f of readdirSync(dir).filter((f) => f.endsWith('.json')).sort()) {
    const day = readJson<{ matches: Record<string, ProfileMatch>; decks: Record<string, string> }>(join(dir, f), { matches: {}, decks: {} });
    Object.assign(decks, day.decks);
    for (const m of Object.values(day.matches)) for (const side of ['p1', 'p2'] as const) {
      const arr = byPlayer.get(m[side].id) ?? []; arr.push(m); byPlayer.set(m[side].id, arr);
    }
  }
  return (_public = { byPlayer, decks });
};
export const parseCompactDeck =(txt: string) => txt.split(/\s+/).filter(Boolean).map((t) => { const [q, id] = t.split('x'); return { id, qty: Number(q) }; }).sort((a, b) => a.id.localeCompare(b.id));
export const matchupIn = (ls: Record<string, Leader>, a: string, b: string) => {
  const m = ls[a]?.matchups?.[b];
  const s = m?.ranked ?? m?.kaizoku ?? m?.opbounty, k = m?.kaizoku;
  return s ? { games: Number(s.games ?? 0), winRate: s.winRate == null ? null : Number(s.winRate), first: k?.firstWinRate == null ? null : Number(k.firstWinRate), second: k?.secondWinRate == null ? null : Number(k.secondWinRate) } : null;
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
