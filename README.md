# OPTCG Data

Free, static site showing the OPTCG Sim ranked ladder: top players and bounty, the leaders they
play, and the decks and cards winning with those leaders. Data is pulled **once a day** by a
GitHub Actions job and committed to `data/`; the site is rebuilt from those files. Nothing is
fetched from upstream at page-view time.

Sources (used with permission): [OPBounty](https://stats.tcgmatchmaking.com/),
[Card Kaizoku](https://www.cardkaizoku.com/), [optcg.one](https://www.optcg.one/).

## Layout

```
ingest/            Python 3.12+, stdlib only.  python -m ingest.run
  sources/         one module per upstream (opbounty ladder, opbounty_matches archive, kaizoku, kaizoku_players, optcgone)
  link.py          ladder row -> sim handle linking via the match archive (exact bounty equality, then name)
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

Live at https://optcg-data.pages.dev (classic Pages project, direct upload).

The daily workflow builds `site/` and runs `wrangler pages deploy` when the repo secrets
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set. Create the token in the Cloudflare
dashboard (My Profile → API Tokens → Create → custom token with **Cloudflare Pages: Edit**), then:

```bash
gh secret set CLOUDFLARE_API_TOKEN   # paste the token when prompted
```

Manual deploy from a machine that has run `wrangler login`:

```bash
cd site && npm run build && npx wrangler pages deploy dist --project-name optcg-data --branch main
```

## Request budget per day

| Source | Requests |
|---|---|
| OPBounty | 1 token + 5 leaderboard pages + 3 tables + ~20 decklists |
| Card Kaizoku | 1 manifest + up to 6 files (skipped when unchanged) + card db |
| optcg.one | 1 |

## Ranked match archive

`ingest/sources/opbounty_matches.py` pulls every finished ranked match from OPBounty's Firestore
`Replays` collection (used with permission) into `data/matches/YYYY-MM-DD.json.gz`: leaders, bounties,
both decklists (hashed, text in the day's `decks` map), timestamp and combat-log path. For games where a
player is above bounty 1,900 the first 4 KB of the combat log is read to get both sim handles
(`data/matches/handles/`). Files are never deleted; windows (`data/windows/7d`, `30d`) are summed from
them. Requires `OPB_FS_EMAIL` / `OPB_FS_PASSWORD` (GitHub secrets); without them the step is skipped.
Backfill: `OPB_MATCH_DAYS=31 python -m ingest.sources.opbounty_matches 31`.
