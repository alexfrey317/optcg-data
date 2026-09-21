# OPTCG Ladder

Free, static site showing the OPTCG Sim ranked ladder: top players and bounty, the leaders they
play, and the decks and cards winning with those leaders. Data is pulled **once a day** by a
GitHub Actions job and committed to `data/`; the site is rebuilt from those files. Nothing is
fetched from upstream at page-view time.

Sources (used with permission): [OPBounty](https://stats.tcgmatchmaking.com/),
[Card Kaizoku](https://www.cardkaizoku.com/), [optcg.one](https://www.optcg.one/).

## Layout

```
ingest/            Python 3.12+, stdlib only.  python -m ingest.run
  sources/         one module per upstream
  normalize.py     merge + hash decks, build per-leader files
data/latest/       players.json, leaders.json, cards.json, decks/<LEADER>.json, cards/<LEADER>.json, meta.json
data/history/      bounty/<playerId>.json  [date, bounty, rank] ; leaders/<LEADER>.json
site/              Astro static site, reads ../data at build time
.github/workflows/daily.yml   09:00 UTC ingest -> commit -> (Cloudflare Pages rebuilds on push)
```

## Local

```bash
python3 -m ingest.run                 # ~2 min; writes raw/ (gitignored) and data/
python3 -m ingest.run --skip opbounty,kaizoku,optcgone,cards   # rebuild data/ from raw/ only
cd site && npm install && npm run build && npx astro preview
```

Environment knobs: `OPB_TOP_PAGES` (default 5 = top 1,000), `OPB_INTERVAL` seconds between
OPBounty calls (2.0), `KAIZOKU_DATASET` (`op17_lw_p`).

## Deploy (Cloudflare Pages, free)

1. Cloudflare dashboard → Workers & Pages → Create → Pages → **Connect to Git** → pick this repo.
2. Build settings: framework **Astro**, root directory `site`, build command `npm run build`,
   output directory `dist`. Environment variable `NODE_VERSION` = `22`.
3. Save. Every push to `main` (including the daily data commit) triggers a rebuild.
   The site is served at `https://<project>.pages.dev`.

## Request budget per day

| Source | Requests |
|---|---|
| OPBounty | 1 token + 5 leaderboard pages + 3 tables + ~20 decklists |
| Card Kaizoku | 1 manifest + up to 6 files (skipped when unchanged) + card db |
| optcg.one | 1 |
