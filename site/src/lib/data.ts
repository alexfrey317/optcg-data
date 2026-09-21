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

export const meta = readJson<Meta>(join(LATEST, 'meta.json'), { generatedAt: '', date: '', status: {}, players: 0, leaders: 0, sources: [] });
export const players = readJson<Player[]>(join(LATEST, 'players.json'), []);
export const leaders = readJson<Record<string, Leader>>(join(LATEST, 'leaders.json'), {});
export const cards = readJson<Record<string, Card>>(join(LATEST, 'cards.json'), {});

export const deckFile = (code: string) => readJson<DeckFile>(join(LATEST, 'decks', `${code}.json`), { leader: code, decks: [], topPilots: [] });
export const cardFile = (code: string) => readJson<CardFile>(join(LATEST, 'cards', `${code}.json`), { leader: code, cards: [], openingHand: [], tech: {} });
export const bountyHistory = (id: string | number) => readJson<[string, number, number][]>(join(HISTORY, 'bounty', `${id}.json`), []);
export const leaderHistory = (code: string) => readJson<(string | number | null)[][]>(join(HISTORY, 'leaders', `${code}.json`), []);

export const card = (id: string): Card => cards[id] ?? { name: id, type: '', cost: '', color: '', power: '', counter: '', rarity: '', img: '', set: id.split('-')[0] };
export const leaderName = (code: string) => leaders[code]?.name ?? card(code).name ?? code;

/** Leaders sorted by Card Kaizoku match volume, then OPBounty. */
export const leadersByVolume = (): Leader[] =>
  Object.values(leaders).sort((a, b) => volume(b) - volume(a));
export const volume = (l: Leader) => Number(l.sources.kaizoku?.matches ?? 0) + Number(l.sources.opbounty?.matches ?? 0);

/** Total games across all sources for a deck. */
export const deckGames = (d: Deck) => Object.values(d.sources).reduce((n, s) => n + Number(s.games ?? 0), 0);

/** Best available win rate for a deck: prefer OPBounty ladder, then Kaizoku, then optcg.one. */
export const deckWinRate = (d: Deck): number | null => {
  const s = d.sources.opbounty ?? d.sources.kaizokuBest ?? d.sources.kaizokuPlayed ?? d.sources.optcgone;
  return s?.winRate == null ? null : Number(s.winRate);
};

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

export const listDeckFiles = () => (existsSync(join(LATEST, 'decks')) ? readdirSync(join(LATEST, 'decks')).map((f) => f.replace(/\.json$/, '')) : []);
