import { store } from "../lib/scraper.mjs";

const headers = {
  "Content-Type": "application/json; charset=utf-8",
  "Access-Control-Allow-Origin": "*",
  "Cache-Control": "public, max-age=60",
};
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers });

export default async (req, context) => {
  if (req.method === "OPTIONS")
    return new Response(null, { status: 204, headers: { ...headers, "Access-Control-Allow-Methods": "GET" } });

  const articles = (await store().get("articles", { type: "json" })) ?? [];

  // GET /api/news/:id
  if (context.params?.id) {
    const a = articles.find((x) => x.id === context.params.id);
    return a ? json(a) : json({ error: "Nincs ilyen hír" }, 404);
  }

  // GET /api/news?limit=&offset=&q=&since=
  const p = new URL(req.url).searchParams;
  const limit = Math.min(Math.max(parseInt(p.get("limit") ?? "20", 10) || 20, 1), 100);
  const offset = Math.max(parseInt(p.get("offset") ?? "0", 10) || 0, 0);
  const q = p.get("q")?.toLowerCase();
  let list = articles;

  if (q) list = list.filter((a) => `${a.title} ${a.summary ?? ""}`.toLowerCase().includes(q));
  if (p.get("since")) {
    const s = new Date(p.get("since"));
    if (isNaN(s)) return json({ error: "Hibás 'since' dátum" }, 400);
    list = list.filter((a) => new Date(a.published_at ?? a.scraped_at) > s);
  }

  const items = list.slice(offset, offset + limit);
  return json({ total: list.length, count: items.length, items });
};

export const config = { path: ["/api/news", "/api/news/:id"] };
