#!/usr/bin/env python3
import json
import os
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE = "https://megaknigi.ru"
UA = "VisualKeiBookshelf-MegaKnigi-Test/2.0"


def fetch(url, method="GET", data=None, timeout=20):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return {
                "ok": True,
                "status": getattr(r, "status", 200),
                "url": r.geturl(),
                "bytes": len(raw),
                "html": raw.decode("utf-8", errors="ignore"),
            }
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(3000)
        except Exception:
            raw = b""
        return {
            "ok": False,
            "status": e.code,
            "url": url,
            "error": str(e),
            "html": raw.decode("utf-8", errors="ignore"),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "error": f"{type(e).__name__}: {e}",
            "html": "",
        }


def clean(s):
    s = re.sub(r"\s+", " ", s or "")
    return s.strip()


class BookCardParser(HTMLParser):
    """
    Parses the actual MegaKnigi card container:
    .bookCard.row

    We deliberately collect data only inside that container so menu,
    category and footer links cannot become fake book results.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card_depth = None
        self.cards = []
        self.card = None
        self.current_href = ""
        self.current_img = ""
        self.text_parts = []
        self.anchor_text = []
        self.in_anchor = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()

        if self.card_depth is None and tag == "div" and "bookCard" in classes and "row" in classes:
            self.card_depth = self.depth
            self.card = {"title": "", "author": "", "cover": "", "url": "", "text": []}
            self.text_parts = []
            self.depth += 1
            return

        if self.card_depth is None:
            self.depth += 1
            return

        # Inside a card.
        if tag == "a":
            self.current_href = attrs.get("href") or ""
            self.anchor_text = []
            self.in_anchor = True

        if tag == "img":
            src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src") or ""
            if src and not self.card["cover"]:
                self.card["cover"] = urllib.parse.urljoin(BASE, src)

        self.depth += 1

    def handle_data(self, data):
        if self.card_depth is not None:
            t = clean(data)
            if t:
                self.text_parts.append(t)
                if self.in_anchor:
                    self.anchor_text.append(t)

    def handle_endtag(self, tag):
        if self.card_depth is None:
            self.depth = max(0, self.depth - 1)
            return

        if tag == "a" and self.in_anchor:
            text = clean(" ".join(self.anchor_text))
            href = urllib.parse.urljoin(BASE, self.current_href)
            if href and "/read/" in href and not self.card["url"]:
                self.card["url"] = href
                self.card["title"] = text
            self.current_href = ""
            self.anchor_text = []
            self.in_anchor = False

        self.depth -= 1

        if self.depth == self.card_depth:
            # Card ended.
            self.card["text"] = clean(" ".join(self.text_parts))

            # If the title anchor was not the /read/ link, use the first useful
            # non-UI anchor text as fallback.
            if not self.card["title"]:
                # Recover from the card's visible text, but avoid generic labels.
                bits = [x for x in self.text_parts if x]
                for bit in bits:
                    low = bit.lower()
                    if bit not in ("Читать", "Подробнее", "Найти") and "мега книги" not in low:
                        self.card["title"] = bit
                        break

            # Try to identify author from visible card text. MegaKnigi places
            # author separately; this conservative heuristic avoids assigning
            # category text as an author.
            if self.card["text"]:
                m = re.search(r"(?:Автор|автор)\s*[:\-]\s*([^|]{2,120})", self.card["text"])
                if m:
                    self.card["author"] = clean(m.group(1))

            if self.card["title"] or self.card["url"]:
                self.cards.append({
                    "title": self.card["title"],
                    "author": self.card["author"],
                    "cover": self.card["cover"],
                    "url": self.card["url"],
                })

            self.card_depth = None
            self.card = None
            self.text_parts = []

    def finish(self):
        # De-duplicate identical cards.
        out = []
        seen = set()
        for c in self.cards:
            key = (c.get("url"), c.get("title"), c.get("cover"))
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out


def parse_cards(html):
    parser = BookCardParser()
    try:
        parser.feed(html)
        parser.close()
        cards = parser.finish()
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    return cards[:50], None


def test_query(q):
    q = (q or "").strip()
    encoded = urllib.parse.quote(q, safe="")
    url = f"{BASE}/search?q={encoded}"
    r = fetch(url)

    cards = []
    parse_error = None
    if r.get("ok"):
        cards, parse_error = parse_cards(r.get("html", ""))

    return {
        "query": q,
        "request": {
            "method": "GET",
            "url": url,
            "status": r.get("status"),
            "ok": r.get("ok"),
            "bytes": r.get("bytes", 0),
            "final_url": r.get("url"),
            "error": r.get("error"),
            "parse_error": parse_error,
        },
        "cards_found": len(cards),
        "results": cards,
    }


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)

        if u.path == "/api/health":
            body = json.dumps(
                {
                    "ok": True,
                    "test": "MegaKnigi adapter v2",
                    "endpoint": "/api/megaknigi-test?q=Корнев",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/api/megaknigi-test":
            q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
            result = test_query(q)
            body = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        super().do_GET()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"MegaKnigi diagnostic v2: http://0.0.0.0:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
