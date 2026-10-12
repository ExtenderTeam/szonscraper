# SZONScraper

15 percenként (állítható) letölti a szon.hu friss híreit, SQLite-ba menti, és REST API-n kiszolgálja.

## Indítás

```bash
python -m venv venv && source venv/bin/activate   # Windowson: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Interaktív dokumentáció: http://localhost:8000/docs

## Végpontok

| Végpont | Leírás |
|---|---|
| `GET /news?limit=20&offset=0&q=baleset&since=2026-10-12T08:00:00Z` | Hírek listája, keresés, lapozás |
| `GET /news/{id}` | Egy hír |
| `POST /refresh` | Azonnali frissítés (ha `REFRESH_TOKEN` be van állítva: `X-Token` fejléc kell) |
| `GET /health` | Állapot, cikkek száma, utolsó futás eredménye |

Példa válasz:

```json
{
  "count": 1,
  "items": [{
    "id": 1,
    "url": "https://www.szon.hu/...",
    "title": "...",
    "summary": "...",
    "image": "https://...jpg",
    "published_at": "2026-10-12T18:00:00+00:00",
    "scraped_at": "2026-10-12T18:05:00+00:00"
  }]
}
```

## Beállítások (környezeti változók)

| Változó | Alapérték | Jelentés |
|---|---|---|
| `SCRAPE_INTERVAL_MIN` | 15 | Frissítés gyakorisága percben |
| `MAX_NEW_PER_RUN` | 40 | Egy futásban letöltött új cikkoldalak max. száma |
| `REQUEST_DELAY_SEC` | 1.0 | Várakozás két cikkletöltés között |
| `REFRESH_TOKEN` | – | `/refresh` védelme |
| `USER_AGENT` | SzonNewsAggregator/1.0 | **Írd át a saját elérhetőségedre!** |
| `ARTICLE_URL_REGEX` | lásd app.py | Cikk-URL felismerése HTML-fallback esetén |
| `DB_PATH` | news.db | Adatbázis fájl |

## Hogyan gyűjt?

1. Megkeresi az oldal RSS/Atom csatornáját (`<link rel="alternate">` + gyakori címek). Ha van, abból dolgozik.
2. Ha nincs RSS, a főoldalról gyűjti a cikklinkeket, és minden új cikk oldalából az `og:title`, `og:description`, `og:image`, `article:published_time` meta tagekat olvassa ki.
3. Csak az új cikkeket tölti le, tiszteletben tartja a robots.txt-et, és késleltet a kérések között.

Ha a HTML-fallback rossz linkeket vagy túl keveset talál, a `ARTICLE_URL_REGEX` változóval finomhangolható.

## Jogi megjegyzés

A hírek a SZON szerzői jogvédett tartalmai. Az API csak **címet, rövid leírást, képlinket és az eredeti cikk URL-jét** tárolja, a teljes cikket nem. Ha más appok felé publikusan szolgálod ki, érdemes mindig az eredeti cikkre linkelni és forrásként megjelölni a SZON-t. Nyilvános/üzleti használat előtt kérj engedélyt a kiadótól, és nézd át az oldal felhasználási feltételeit.
