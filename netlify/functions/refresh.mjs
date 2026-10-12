// Kézi frissítés: POST /api/refresh  (X-Token fejléc kell, = REFRESH_TOKEN env változó)
import { scrape } from "../lib/scraper.mjs";

export default async (req) => {
  const token = process.env.REFRESH_TOKEN;
  const j = (b, s) => new Response(JSON.stringify(b), { status: s, headers: { "Content-Type": "application/json" } });
  if (req.method !== "POST") return j({ error: "Csak POST" }, 405);
  if (!token) return j({ error: "A REFRESH_TOKEN nincs beállítva, a végpont ki van kapcsolva" }, 403);
  if (req.headers.get("x-token") !== token) return j({ error: "Hibás token" }, 401);
  return j(await scrape(), 200);
};

export const config = { path: "/api/refresh" };
