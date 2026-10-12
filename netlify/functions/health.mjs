import { store } from "../lib/scraper.mjs";

export default async () => {
  const st = store();
  const articles = (await st.get("articles", { type: "json" })) ?? [];
  const last_run = (await st.get("last_run", { type: "json" })) ?? null;
  return new Response(JSON.stringify({ status: "ok", articles: articles.length, last_run }), {
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
};

export const config = { path: "/api/health" };
