#!/usr/bin/env python3
import json
import re
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = 10000
BASE = "https://megaknigi.ru"

def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Android) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        data = r.read()
        return r.status, r.geturl(), data.decode("utf-8", "ignore")

def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()

def attr(tag, name):
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    return m.group(1).strip() if m else ""

def parse_results(html):
    results = []
    seen = set()

    # MegaKnigi result links have /read/<id>/... .
    # Work from each read link and inspect a surrounding container.
    links = list(re.finditer(r'<a\b[^>]*href=["\']([^"\']*/read/\d+[^"\']*)["\'][^>]*>(.*?)</a>', html, re.I | re.S))

    for lm in links:
        href = urllib.parse.urljoin(BASE, lm.group(1))
        if href in seen:
            continue
        seen.add(href)

        # Surrounding HTML: enough to catch the card without assuming one CSS class.
        start = max(0, lm.start() - 7000)
        end = min(len(html), lm.end() + 7000)
        block = html[start:end]

        # Title: prefer explicit title/name-like elements.
        title = ""
        patterns = [
            r'<[^>]+class=["\'][^"\']*(?:book-title|title|bookName|book-name)[^"\']*["\'][^>]*>(.*?)</',
            r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>',
        ]
        for p in patterns:
            mm = re.search(p, block, re.I | re.S)
            if mm:
                title = clean(mm.group(1))
                if title:
                    break

        # If the link itself has useful text, use it.
        if not title:
            title = clean(lm.group(2))

        # Remove obvious navigation text.
        if title.lower() in {"читать", "найти", "подробнее", "скачать"} or len(title) < 2:
            title = ""

        # Author heuristics. MegaKnigi often puts the author as nearby text/link.
        author = ""
        author_patterns = [
            r'<[^>]+class=["\'][^"\']*author[^"\']*["\'][^>]*>(.*?)</',
            r'(?:Автор|Авторы)\s*[:\-]?\s*</?[^>]*>\s*(.*?)<',
            r'(?:Автор|Авторы)\s*[:\-]?\s*([^<]{2,120})',
        ]
        for p in author_patterns:
            mm = re.search(p, block, re.I | re.S)
            if mm:
                candidate = clean(mm.group(1))
                if candidate and candidate.lower() not in {"автор", "авторы"}:
                    author = candidate
                    break

        # Cover image near the card.
        cover = ""
        imgs = re.findall(r'<img\b[^>]*>', block, re.I | re.S)
        for tag in imgs:
            src = attr(tag, "src") or attr(tag, "data-src") or attr(tag, "data-lazy-src")
            if src and ("/storage/cover/" in src or "cover" in src.lower()):
                cover = urllib.parse.urljoin(BASE, src)
                break

        if not cover and imgs:
            src = attr(imgs[0], "src") or attr(imgs[0], "data-src")
            if src and not src.startswith("data:"):
                cover = urllib.parse.urljoin(BASE, src)

        results.append({
            "title": title,
            "author": author,
            "cover": cover,
            "url": href,
        })

    # De-duplicate by URL/title and keep a useful maximum.
    out, keys = [], set()
    for r in results:
        key = (r["url"], r["title"])
        if key in keys:
            continue
        keys.add(key)
        out.append(r)
        if len(out) >= 30:
            break
    return out

def diagnostic(q):
    url = BASE + "/search?" + urllib.parse.urlencode({"q": q})
    try:
        status, final_url, html = fetch(url)
        results = parse_results(html)
        # Small structural diagnostics, useful if a query behaves differently.
        read_count = len(re.findall(r'/read/\d+', html, re.I))
        bookcard_count = len(re.findall(r'bookCard', html, re.I))
        return {
            "query": q,
            "request": {
                "method": "GET",
                "url": url,
                "status": status,
                "ok": 200 <= status < 400,
                "bytes": len(html.encode("utf-8")),
                "final_url": final_url,
                "error": None,
                "parse_error": None,
            },
            "structure": {
                "read_links_found": read_count,
                "bookCard_mentions": bookcard_count,
            },
            "cards_found": len(results),
            "results": results,
        }
    except Exception as e:
        return {
            "query": q,
            "request": {
                "method": "GET",
                "url": url,
                "status": None,
                "ok": False,
                "bytes": 0,
                "final_url": None,
                "error": type(e).__name__ + ": " + str(e),
                "parse_error": None,
            },
            "structure": {},
            "cards_found": 0,
            "results": [],
        }

class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            return self.send_json({
                "ok": True,
                "test": "MegaKnigi adapter v3",
                "endpoint": "/api/megaknigi-test?q=Корнев"
            })
        if parsed.path == "/api/megaknigi-test":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if not q:
                return self.send_json({"error": "query required"}, 400)
            return self.send_json(diagnostic(q))
        self.send_json({
            "ok": True,
            "service": "MegaKnigi parser v3",
            "try": "/api/health",
            "search": "/api/megaknigi-test?q=Гарри%20Поттер"
        })

    def log_message(self, fmt, *args):
        print(fmt % args)

if __name__ == "__main__":
    print(f"MegaKnigi adapter v3 listening on {HOST}:{PORT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
