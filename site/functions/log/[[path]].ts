/** Cloudflare Pages Function: same-origin proxy for OPBounty combat logs.
 *  Firebase Storage serves the logs publicly but without CORS headers, so the browser cannot fetch them directly.
 *  GET /log/Replays/<week>/<mode>/<leader>/<file>.log -> the log text, cached for a year (logs never change). */
const BUCKET = 'opbounty-3623c.firebasestorage.app';
const OK_PATH = /^Replays\/[\w-]+\/\d+\/[A-Za-z0-9_-]+\/[^/]+\.log$/;

export const onRequestGet = async ({ params, request }: { params: { path: string | string[] }; request: Request }) => {
  const parts = Array.isArray(params.path) ? params.path : [params.path];
  const path = parts.map((p) => decodeURIComponent(p)).join('/');
  if (!OK_PATH.test(path)) return new Response('not found', { status: 404 });
  const cache = (caches as unknown as { default: Cache }).default;
  const key = new Request(new URL(request.url).toString(), { method: 'GET' });
  const hit = await cache.match(key);
  if (hit) return hit;
  const up = await fetch(`https://firebasestorage.googleapis.com/v0/b/${BUCKET}/o/${encodeURIComponent(path)}?alt=media`);
  if (!up.ok) return new Response('not found', { status: up.status === 404 ? 404 : 502 });
  const res = new Response(up.body, { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'public, max-age=31536000, immutable' } });
  await cache.put(key, res.clone());
  return res;
};
