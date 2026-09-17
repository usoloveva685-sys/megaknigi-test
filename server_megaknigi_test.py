#!/usr/bin/env python3
import json
import re
import urllib.parse
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = int(__import__("os").environ.get("PORT", "10000"))
BASE = "https://megaknigi.ru"

def clean(s):
    s = unescape(s or "")
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I|re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I|re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def attr(tag, name):
    m = re.search(r'\b' + re.escape(name) + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    return unescape(m.group(1).strip()) if m else ""

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        "Referer": BASE + "/",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, r.geturl(), r.read().decode("utf-8", "ignore")

def parse_card(card):
    # Collect /read links inside THIS card only.
    read_links = []
    for m in re.finditer(r'<a\b([^>]*\bhref=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*)>(.*?)</a>', card, re.I|re.S):
        href = urllib.parse.urljoin(BASE, m.group(2))
        text = clean(m.group(3))
        if href not in [x[0] for x in read_links]:
            read_links.append((href, text))

    if not read_links:
        return []

    # Cover must also come from THIS card, never from the surrounding page.
    cover = ""
    for tag in re.findall(r'<img\b[^>]*>', card, re.I|re.S):
        src = attr(tag, "src") or attr(tag, "data-src") or attr(tag, "data-original") or attr(tag, "data-lazy-src")
        if src and not src.startswith("data:"):
            full = urllib.parse.urljoin(BASE, src)
            if "/storage/cover/" in full.lower() or "cover" in full.lower():
                cover = full
                break
    if not cover:
        for tag in re.findall(r'<img\b[^>]*>', card, re.I|re.S):
            src = attr(tag, "src") or attr(tag, "data-src") or attr(tag, "data-original")
            if src and not src.startswith("data:"):
                cover = urllib.parse.urljoin(BASE, src)
                break

    # Prefer explicit title/name classes.
    title = ""
    title_patterns = [
        r'<[^>]+class=["\'][^"\']*(?:book-title|bookTitle|book-name|bookName|title)[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>',
    ]
    for p in title_patterns:
        mm = re.search(p, card, re.I|re.S)
        if mm:
            title = clean(mm.group(1))
            if title:
                break

    # If no title class, the text of the first /read link is usually the book title.
    if not title:
        for href, txt in read_links:
            if txt and txt.lower() not in {"читать", "скачать", "подробнее"}:
                title = txt
                break

    # Author: stay inside the card and look for explicit author markers/classes.
    author = ""
    author_patterns = [
        r'<[^>]+class=["\'][^"\']*author[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        r'<[^>]+(?:data-author|title=["\'][^"\']*author[^"\']*)[^>]*>(.*?)</[^>]+>',
        r'(?:Автор|Авторы)\s*</?(?:strong|b|span|div|p)[^>]*>\s*(.*?)</',
        r'(?:Автор|Авторы)\s*[:\-]\s*([^<]{2,150})',
    ]
    for p in author_patterns:
        mm = re.search(p, card, re.I|re.S)
        if mm:
            cand = clean(mm.group(1))
            if cand and cand.lower() not in {"автор", "авторы"}:
                author = cand
                break

    # Fallback: inspect links in the card. Exclude read links and obvious navigation.
    if not author:
        candidates = []
        for m in re.finditer(r'<a\b([^>]*\bhref=["\']([^"\']+)["\'][^>]*)>(.*?)</a>', card, re.I|re.S):
            href = urllib.parse.urljoin(BASE, m.group(2))
            txt = clean(m.group(3))
            if not txt or len(txt) < 2 or len(txt) > 120:
                continue
            if "/read/" in href:
                continue
            low = txt.lower()
            if low in {"читать", "скачать", "подробнее", "найти", "все книги", "книги"}:
                continue
            if re.fullmatch(r"[\d\s\W_]+", txt):
                continue
            candidates.append(txt)
        # Choose a plausible author-looking candidate, preferring short names.
        if candidates:
            candidates = list(dict.fromkeys(candidates))
            author = min(candidates, key=lambda x: (len(x) > 80, len(x)))

    # A card can contain several editions/parts with the same visible title.
    # Return one record per distinct read URL, preserving its own link.
    out = []
    for href, link_text in read_links:
        item_title = title or link_text
        if item_title:
            out.append({
                "title": item_title,
                "author": author,
                "cover": cover,
                "url": href,
            })
    return out

def parse_results(html):
    results = []
    seen = set()

    # Primary path: actual MegaKnigi cards seen in the test.
    starts = list(re.finditer(r'<div\b[^>]*class=["\'][^"\']*bookCard[^"\']*["\'][^>]*>', html, re.I))
    if starts:
        for i, sm in enumerate(starts):
            end = starts[i+1].start() if i+1 < len(starts) else len(html)
            card = html[sm.start():end]
            for item in parse_card(card):
                key = item["url"]
                if key not in seen:
                    seen.add(key)
                    results.append(item)

    # Fallback if the site changes its card class.
    if not results:
        for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*>(.*?)</a>', html, re.I|re.S):
            href = urllib.parse.urljoin(BASE, m.group(1))
            if href in seen:
                continue
            seen.add(href)
            txt = clean(m.group(2))
            if txt and txt.lower() not in {"читать", "скачать"}:
                results.append({"title": txt, "author": "", "cover": "", "url": href})

    return results[:30]

def diagnostic(q):
    url = BASE + "/search?" + urllib.parse.urlencode({"q": q})
    try:
        status, final_url, html = fetch(url)
        results = parse_results(html)
        return {
            "query": q,
            "request": {
                "method": "GET", "url": url, "status": status,
                "ok": 200 <= status < 400,
                "bytes": len(html.encode("utf-8")),
                "final_url": final_url, "error": None, "parse_error": None
            },
            "structure": {
                "read_links_found": len(re.findall(r'/read/\d+', html, re.I)),
                "bookCard_mentions": len(re.findall(r'bookCard', html, re.I)),
                "cards_used": len(re.findall(r'<div\b[^>]*class=["\'][^"\']*bookCard[^"\']*["\'][^>]*>', html, re.I))
            },
            "cards_found": len(results),
            "results": results
        }
    except Exception as e:
        return {
            "query": q,
            "request": {"method":"GET","url":url,"status":None,"ok":False,"bytes":0,
                        "final_url":None,"error":type(e).__name__+": "+str(e),"parse_error":None},
            "structure": {}, "cards_found": 0, "results":[]
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
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/health":
            return self.send_json({"ok": True, "test": "MegaKnigi adapter v4",
                                   "endpoint": "/api/megaknigi-test?q=Гарри%20Поттер"})
        if p.path == "/api/megaknigi-test":
            q = urllib.parse.parse_qs(p.query).get("q", [""])[0].strip()
            if not q: return self.send_json({"error":"query required"}, 400)
            return self.send_json(diagnostic(q))
        return self.send_json({"ok":True,"service":"MegaKnigi parser v4",
                               "try":"/api/health"})
    def log_message(self, fmt, *args):
        print(fmt % args)

if __name__ == "__main__":
    print(f"MegaKnigi adapter v4 listening on {HOST}:{PORT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
