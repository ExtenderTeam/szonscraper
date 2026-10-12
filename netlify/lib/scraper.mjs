import { getStore } from "@netlify/blobs";
import * as cheerio from "cheerio";
import { XMLParser } from "fast-xml-parser";
import { createHash } from "node:crypto";

// ------------------------------------------------------------ beállítások
const BASE_URL = process.env.BASE_URL || "https://www.szon.hu/";
const MAX_ITEMS = Number(process.env.MAX_ITEMS || 500);       // ennyi cikket tartunk meg
const MAX_NEW = Number(process.env.MAX_NEW_PER_RUN || 8);     // egy futásban max ennyi új cikkoldal letöltése (a scheduled function 30 mp-es limitje miatt)
const CONCURRENCY = 4;
const UA = process.env.USER_AGENT || "SzonNewsAggregator/1.0 (+kapcsolat: te@pelda.hu)";
const ARTICLE_RE = new RegExp(
  process.env.ARTICLE_URL_REGEX || "^/[^/]+/.*[a-z0-9]+(?:-[a-z0-9]+){3,}",
  "i"
);
const FEED_CANDIDATES = ["/rss", "/rss.xml", "/feed", "/feed.xml", "/rss/hirek", "/hirek/rss"];

export const store = () => getStore("szon-news");

// ------------------------------------------------------------ segédek
const idOf = (url) => createHash("sha1").update(url).digest("hex").slice(0, 12);
const arr = (x) => (x == null ? [] : Array.isArray(x) ? x : [x]);
const txt = (x) => (x && typeof x === "object" ? x["#text"] ?? "" : x ?? "");
const xml = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: "@_" });

function toIso(v) {
  if (!v) return null;
  const d = new Date(v);
  return isNaN(d) ? null : d.toISOString();
}

function clean(html, limit = 600) {
  if (!html) return null;
  const t = cheerio.load(String(html)).text().replace(/\s+/g, " ").trim();
  return t ? t.slice(0, limit) : null;
}

async function get(url) {
  const res = await fetch(url, {
    headers: { "User-Agent": UA, "Accept-Language": "hu" },
    signal: AbortSignal.timeout(8000),
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.text();
}

async function loadRobots() {
  try {
    const body = await get(new URL("/robots.txt", BASE_URL).href);
    const rules = [];
    let applies = false;
    for (const line of body.split(/\r?\n/)) {
      const [k, ...rest] = line.split("#")[0].split(":");
      const key = k.trim().toLowerCase();
      const val = rest.join(":").trim();
      if (key === "user-agent") applies = val === "*";
      else if (applies && key === "disallow" && val) rules.push(val);
    }
    return (path) => !rules.some((r) => path.startsWith(r));
  } catch {
    return () => true;
  }
}

// ------------------------------------------------------------ források
function findFeeds(homeHtml) {
  const $ = cheerio.load(homeHtml);
  const found = [];
  $('link[rel="alternate"]').each((_, el) => {
    const t = ($(el).attr("type") || "").toLowerCase();
    if (t.includes("rss") || t.includes("atom")) found.push(new URL($(el).attr("href"), BASE_URL).href);
  });
  for (const p of FEED_CANDIDATES) found.push(new URL(p, BASE_URL).href);
  return [...new Set(found)];
}

function parseFeed(body) {
  const doc = xml.parse(body);
  const entries = arr(doc?.rss?.channel?.item ?? doc?.feed?.entry);
  return entries
    .map((e) => {
      let link = e.link;
      if (Array.isArray(link)) link = link.find((l) => l?.["@_rel"] !== "self") ?? link[0];
      if (link && typeof link === "object") link = link["@_href"];
      const image =
        e.enclosure?.["@_url"] ??
        arr(e["media:content"])[0]?.["@_url"] ??
        arr(e["media:thumbnail"])[0]?.["@_url"] ??
        null;
      return {
        url: link,
        title: clean(txt(e.title), 300),
        summary: clean(txt(e.description ?? e.summary)),
        image,
        published_at: toIso(e.pubDate ?? e.published ?? e.updated),
      };
    })
    .filter((i) => i.url && i.title);
}

function fromHomepage(homeHtml) {
  const $ = cheerio.load(homeHtml);
  const host = new URL(BASE_URL).host;
  const seen = new Set();
  const items = [];
  $("a[href]").each((_, el) => {
    let u;
    try {
      u = new URL($(el).attr("href"), BASE_URL);
    } catch {
      return;
    }
    u.hash = "";
    if (u.host !== host || !ARTICLE_RE.test(u.pathname) || seen.has(u.href)) return;
    seen.add(u.href);
    items.push({ url: u.href });
  });
  return items;
}

async function enrich(item) {
  const $ = cheerio.load(await get(item.url));
  const meta = (...names) => {
    for (const n of names) {
      const v = $(`meta[property="${n}"]`).attr("content") || $(`meta[name="${n}"]`).attr("content");
      if (v) return v.trim();
    }
    return null;
  };
  const title = meta("og:title", "twitter:title") || $("title").first().text().trim();
  if (!title) return null;
  return {
    ...item,
    title: clean(title, 300),
    summary: clean(meta("og:description", "description")),
    image: meta("og:image", "twitter:image"),
    published_at: toIso(meta("article:published_time", "datePublished")),
  };
}

// ------------------------------------------------------------ fő logika
export async function scrape() {
  const result = { new: 0, source: null, error: null };
  try {
    const home = await get(BASE_URL);

    let items = [];
    for (const f of findFeeds(home)) {
      try {
        items = parseFeed(await get(f));
      } catch {
        items = [];
      }
      if (items.length) {
        result.source = `rss: ${f}`;
        break;
      }
    }
    if (!items.length) {
      items = fromHomepage(home);
      result.source = "html: főoldal";
    }

    const st = store();
    const existing = (await st.get("articles", { type: "json" })) ?? [];
    const known = new Set(existing.map((a) => a.url));
    const allowed = await loadRobots();

    const fresh = items
      .filter((i) => !known.has(i.url) && allowed(new URL(i.url).pathname))
      .slice(0, MAX_NEW);

    const done = [];
    for (let i = 0; i < fresh.length; i += CONCURRENCY) {
      const chunk = fresh.slice(i, i + CONCURRENCY);
      const out = await Promise.all(
        chunk.map(async (it) =>
          it.title && it.published_at ? it : await enrich(it).catch(() => (it.title ? it : null))
        )
      );
      done.push(...out);
    }

    const now = new Date().toISOString();
    const added = done.filter(Boolean).map((i) => ({
      id: idOf(i.url),
      url: i.url,
      title: i.title,
      summary: i.summary ?? null,
      image: i.image ?? null,
      published_at: i.published_at ?? null,
      scraped_at: now,
    }));
    result.new = added.length;

    const key = (a) => a.published_at ?? a.scraped_at;
    const merged = [...added, ...existing]
      .sort((a, b) => (key(a) < key(b) ? 1 : -1))
      .slice(0, MAX_ITEMS);
    await st.setJSON("articles", merged);
  } catch (e) {
    result.error = String(e?.message ?? e);
  }
  await store().setJSON("last_run", { at: new Date().toISOString(), ...result });
  return result;
}
