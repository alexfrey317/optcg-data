// OPlayTCG tournament decklist collector. Runs INSIDE a real browser session on oplaytcg.com (Playwright
// `page` callback, e.g. the Playwright MCP run-code tool), because Cloudflare returns 403 to every non-browser
// client, headless Chromium included. Edit CFG, run, then save localStorage.optcg_dump to a file (the return
// value is only a summary, the dump itself is too big to print) and run:
//   python -m ingest.sources.oplaytcg <dump.json>
// Decks are pulled for tournament winners and for anyone in the ladder top CFG.ladderPages*100. ~1 request/s.
async (page) => {
  const CFG = { from: 1979, to: 2300, ladderPages: 20, pace: 900 };
  await page.goto('https://oplaytcg.com/en/tournaments', { waitUntil: 'domcontentloaded' });
  const dump = await page.evaluate(async (CFG) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const rsc = async (url) => {
      const r = await fetch(url, { headers: { RSC: '1' } });
      if (r.status !== 200) return { status: r.status, objs: [] };
      const objs = [];
      for (const line of (await r.text()).split('\n')) {
        const i = line.indexOf(':');
        if (i < 0) continue;
        try { objs.push(JSON.parse(line.slice(i + 1))); } catch (e) {}
      }
      return { status: 200, objs };
    };
    const find = (o, pred, d = 0) => {
      if (d > 16 || !o || typeof o !== 'object') return null;
      if (pred(o)) return o;
      for (const k of Array.isArray(o) ? o.keys() : Object.keys(o)) { const r = find(o[k], pred, d + 1); if (r) return r; }
      return null;
    };
    const ladder = {};
    for (let p = 1; p <= CFG.ladderPages; p++) {
      const o = find((await rsc(`/play/leaderboard?page=${p}&_rsc=1`)).objs, (x) => Array.isArray(x.rows) && x.rows[0]?.userId);
      for (const r of o?.rows ?? []) ladder[r.userId] = { rank: r.rank, name: r.name, cc: r.country, rating: r.rating };
      await sleep(CFG.pace);
    }
    const tournaments = [], targets = [];
    let last = CFG.from - 1;
    for (let id = CFG.from; id <= CFG.to; id++) {
      const res = await rsc(`/en/tournaments/${id}?_rsc=1`);
      await sleep(CFG.pace);
      if (res.status === 404) continue;
      last = id;
      const d = find(res.objs, (x) => Array.isArray(x.participants) && 'status' in x && 'name' in x);
      if (!d || d.status !== 'completed') continue;
      tournaments.push({ id, name: d.name, date: d.scheduledAt, format: d.format, bestOf: d.bestOf, players: d.participants.length });
      for (const p of d.participants) {
        if (!p.deckVisible || !p.leaderCode || !(p.placement === 1 || ladder[p.userId])) continue;
        targets.push({ t: id, user: p.userId, name: p.name, cc: p.country, place: p.placement, w: p.wins, l: p.losses, leader: p.leaderCode });
      }
    }
    const decks = [];
    for (const t of targets) {
      const r = await fetch(`/en/tournaments/${t.t}`, { method: 'POST', body: JSON.stringify([t.t, t.user]), headers: {
        'Next-Action': '607e232416de32b96ffda2b3a1b38c6a26af6264d1', Accept: 'text/x-component', 'Content-Type': 'text/plain;charset=UTF-8' } });
      const line = (await r.text()).split('\n').find((l) => l.startsWith('1:'));
      await sleep(CFG.pace);
      let deck = null;
      try { deck = line && JSON.parse(line.slice(2)); } catch (e) {}
      if (!deck?.cards?.length) continue;
      decks.push({ ...t, leaderName: deck.leader?.name ?? null, cards: deck.cards.map((c) => [c.code, c.count]) });
    }
    const dump = { scanned: [CFG.from, last], ladderTop: CFG.ladderPages * 100, ladder, tournaments, decks };
    localStorage.setItem('optcg_dump', JSON.stringify(dump));
    return { scanned: dump.scanned, tournaments: tournaments.length, targets: targets.length, decks: decks.length };
  }, CFG);
  return JSON.stringify(dump);
}
