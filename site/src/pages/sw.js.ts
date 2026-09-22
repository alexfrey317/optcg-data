import type { APIRoute } from 'astro';
import { meta } from '../lib/data';

/** Service worker, generated at build time so its cache version follows each daily data snapshot.
 *  Pages: network first (fresh data when online), cache fallback, then /offline/.
 *  Same-origin assets: stale-while-revalidate. Card images (static.dotgg.gg): cache first, capped. */
const VERSION = (meta.generatedAt || meta.date || 'dev').replace(/[^0-9A-Za-z]/g, '');
const body = `
const VERSION = ${JSON.stringify(VERSION)};
const PAGES = 'optcg-pages-' + VERSION;
const ASSETS = 'optcg-assets-' + VERSION;
const IMAGES = 'optcg-images-v1';
const IMAGE_LIMIT = 600;
const SHELL = ['/', '/leaders/', '/matchups/', '/counter/', '/offline/', '/manifest.webmanifest', '/icons/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(PAGES);
    await Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== PAGES && k !== ASSETS && k !== IMAGES).map((k) => caches.delete(k)));
    if (self.registration.navigationPreload) { try { await self.registration.navigationPreload.enable(); } catch {} }
    await self.clients.claim();
  })());
});

const trim = async (name, limit) => {
  const c = await caches.open(name); const keys = await c.keys();
  for (let i = 0; i < keys.length - limit; i++) await c.delete(keys[i]);
};

const networkFirst = async (e, req) => {
  const c = await caches.open(PAGES);
  try {
    const pre = e.preloadResponse ? await e.preloadResponse : null;
    const res = pre || await fetch(req);
    if (res && res.ok) c.put(req, res.clone());
    return res;
  } catch {
    const hit = await c.match(req, { ignoreSearch: true });
    return hit || (await c.match('/offline/')) || Response.error();
  }
};

const staleWhileRevalidate = async (req) => {
  const c = await caches.open(ASSETS);
  const hit = await c.match(req);
  const fresh = fetch(req).then((res) => { if (res && res.ok) c.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await fresh) || Response.error();
};

const cacheFirst = async (req) => {
  const c = await caches.open(IMAGES);
  const hit = await c.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res && (res.ok || res.type === 'opaque')) { c.put(req, res.clone()); trim(IMAGES, IMAGE_LIMIT); }
    return res;
  } catch { return Response.error(); }
};

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin === location.origin) {
    if (url.pathname === '/sw.js') return;
    if (req.mode === 'navigate' || req.destination === 'document') { e.respondWith(networkFirst(e, req)); return; }
    e.respondWith(staleWhileRevalidate(req));
    return;
  }
  if (url.hostname === 'static.dotgg.gg') { e.respondWith(cacheFirst(req)); }
});

self.addEventListener('message', (e) => { if (e.data === 'skipWaiting') self.skipWaiting(); });
`;

export const GET: APIRoute = () => new Response(body.trimStart(), { headers: { 'Content-Type': 'application/javascript; charset=utf-8', 'Cache-Control': 'no-cache' } });
