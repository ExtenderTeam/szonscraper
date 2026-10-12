"""
SZON hírgyűjtő + REST API

Futtatás:  uvicorn app:app --host 0.0.0.0 --port 8000
Docs:      http://localhost:8000/docs
"""
import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query

# ---------------------------------------------------------------- beállítások
BASE_URL = os.getenv("BASE_URL", "https://www.szon.hu/")
DB_PATH = os.getenv("DB_PATH", "news.db")
SCRAPE_INTERVAL_MIN = int(os.getenv("SCRAPE_INTERVAL_MIN", "15"))
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "40"))   # egy futásban max ennyi új cikk oldalát töltjük le
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "1.0"))  # udvarias várakozás kérések között
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")  # ha be van állítva, a POST /refresh ezt kéri az X-Token fejlécben
USER_AGENT = os.getenv("USER_AGENT", "SzonNewsAggregator/1.0 (+kapcsolat: te@pelda.hu)")
# Cikk-URL felismerése a főoldali linkek közül (ha az oldal szerkezete miatt módosítani kell):
ARTICLE_URL_REGEX = re.compile(os.getenv("ARTICLE_URL_REGEX", r"^/[^/]+/.*[a-z0-9]+(?:-[a-z0-9]+){3,}"), re.I)

FEED_CANDIDATES = ["/rss", "/rss.xml", "/feed", "/feed.xml", "/rss/hirek", "/hirek/rss"]

log = logging.getLogger("szon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_db_lock = threading.Lock()
last_run = {"at": None, "new": 0, "source": None, "error": None}


# ---------------------------------------------------------------- adatbázis
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                image TEXT,
                published_at TEXT,
                scraped_at TEXT NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub ON articles(published_at DESC)")
        c.commit()


def save_article(a: dict) -> bool:
    with _db_lock, closing(db()) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO articles (url, title, summary, image, published_at, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                a["url"],
                a["title"],
                a.get("summary"),
                a.get("image"),
                a.get("published_at"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        c.commit()
        return cur.rowcount > 0


def known_urls() -> set:
    with closing(db()) as c:
        return {r["url"] for r in c.execute("SELECT url FROM articles")}


# ---------------------------------------------------------------- scraper
def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "hu"},
        timeout=20,
        follow_redirects=True,
    )


def robots_allows(client: httpx.Client, url: str) -> bool:
    rp = RobotFileParser()
    try:
        r = client.get(urljoin(BASE_URL, "/robots.txt"))
        if r.status_code >= 400:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def to_iso(value) -> str | None:
    if not value:
        return None
    try:
        if isinstance(value, time.struct_time):
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
        s = str(value).strip()
        try:
            dt = parsedate_to_datetime(s)  # RSS formátum
        except (TypeError, ValueError):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO formátum
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def clean(text: str | None, limit: int = 600) -> str | None:
    if not text:
        return None
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return text[:limit] or None


def find_feeds(client: httpx.Client, home_html: str) -> list[str]:
    soup = BeautifulSoup(home_html, "html.parser")
    found = []
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        t = (link.get("type") or "").lower()
        if "rss" in t or "atom" in t:
            found.append(urljoin(BASE_URL, link.get("href", "")))
    found += [urljoin(BASE_URL, p) for p in FEED_CANDIDATES]
    return list(dict.fromkeys(found))


def from_feed(client: httpx.Client, feed_url: str) -> list[dict]:
    try:
        r = client.get(feed_url)
        if r.status_code != 200:
            return []
        parsed = feedparser.parse(r.content)
    except Exception:
        return []
    items = []
    for e in parsed.entries:
        if not e.get("link") or not e.get("title"):
            continue
        image = None
        if e.get("media_content"):
            image = e["media_content"][0].get("url")
        elif e.get("enclosures"):
            image = e["enclosures"][0].get("href")
        items.append(
            {
                "url": e["link"],
                "title": clean(e["title"], 300),
                "summary": clean(e.get("summary")),
                "image": image,
                "published_at": to_iso(e.get("published_parsed") or e.get("published")),
            }
        )
    return items


def from_homepage(home_html: str) -> list[dict]:
    """Tartalék: cikklinkek gyűjtése a főoldalról."""
    soup = BeautifulSoup(home_html, "html.parser")
    host = urlparse(BASE_URL).netloc
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE_URL, a["href"]).split("#")[0]
        p = urlparse(url)
        if p.netloc != host or url in seen or not ARTICLE_URL_REGEX.search(p.path):
            continue
        seen.add(url)
        items.append({"url": url, "title": None})
    return items


def enrich(client: httpx.Client, item: dict) -> dict | None:
    """Cikkoldal letöltése: cím, leírás, kép, dátum az og/meta tagekből."""
    try:
        r = client.get(item["url"])
        r.raise_for_status()
    except Exception as ex:
        log.warning("Nem tölthető le: %s (%s)", item["url"], ex)
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    if not title:
        return None
    item.update(
        title=clean(title, 300),
        summary=clean(meta("og:description", "description")),
        image=meta("og:image", "twitter:image"),
        published_at=to_iso(meta("article:published_time", "datePublished")),
    )
    return item


def scrape_once() -> dict:
    result = {"new": 0, "source": None, "error": None}
    with make_client() as client:
        try:
            home = client.get(BASE_URL)
            home.raise_for_status()
        except Exception as ex:
            result["error"] = f"Főoldal hiba: {ex}"
            return result

        items = []
        for feed_url in find_feeds(client, home.text):
            items = from_feed(client, feed_url)
            if items:
                result["source"] = f"rss: {feed_url}"
                break
        if not items:
            items = from_homepage(home.text)
            result["source"] = "html: főoldal"

        known = known_urls()
        fresh = [i for i in items if i["url"] not in known][:MAX_NEW_PER_RUN]

        for item in fresh:
            if not robots_allows(client, item["url"]):
                continue
            # RSS-ből már megvan minden; HTML-ből vagy hiányos adatnál oldalt töltünk
            if not item.get("title") or not item.get("published_at"):
                time.sleep(REQUEST_DELAY_SEC)
                item = enrich(client, item) or (item if item.get("title") else None)
                if not item:
                    continue
            if save_article(item):
                result["new"] += 1
    return result


async def scrape_loop():
    while True:
        try:
            res = await asyncio.to_thread(scrape_once)
            last_run.update(at=datetime.now(timezone.utc).isoformat(), **res)
            log.info("Scrape kész: %s", res)
        except Exception as ex:
            last_run.update(at=datetime.now(timezone.utc).isoformat(), error=str(ex))
            log.exception("Scrape hiba")
        await asyncio.sleep(SCRAPE_INTERVAL_MIN * 60)


# ---------------------------------------------------------------- API
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    task = asyncio.create_task(scrape_loop())
    yield
    task.cancel()


app = FastAPI(title="SZON hírek API", version="1.0", lifespan=lifespan)


def row_to_dict(r: sqlite3.Row) -> dict:
    return dict(r)


@app.get("/news", summary="Hírek listázása")
def list_news(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, description="Keresés a címben és a leírásban"),
    since: str | None = Query(None, description="Csak ennél újabb hírek (ISO dátum, pl. 2026-10-12T08:00:00Z)"),
):
    sql, params = "SELECT * FROM articles WHERE 1=1", []
    if q:
        sql += " AND (title LIKE ? OR summary LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if since:
        iso = to_iso(since)
        if not iso:
            raise HTTPException(400, "Hibás 'since' dátum")
        sql += " AND COALESCE(published_at, scraped_at) > ?"
        params.append(iso)
    sql += " ORDER BY COALESCE(published_at, scraped_at) DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with closing(db()) as c:
        rows = [row_to_dict(r) for r in c.execute(sql, params)]
    return {"count": len(rows), "items": rows}


@app.get("/news/{article_id}", summary="Egy hír lekérése")
def get_news(article_id: int):
    with closing(db()) as c:
        r = c.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Nincs ilyen hír")
    return row_to_dict(r)


@app.post("/refresh", summary="Azonnali frissítés indítása")
async def refresh(x_token: str | None = Header(None)):
    if REFRESH_TOKEN and x_token != REFRESH_TOKEN:
        raise HTTPException(401, "Hibás token")
    res = await asyncio.to_thread(scrape_once)
    last_run.update(at=datetime.now(timezone.utc).isoformat(), **res)
    return res


@app.get("/health", summary="Állapot")
def health():
    with closing(db()) as c:
        total = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"status": "ok", "articles": total, "last_run": last_run}
