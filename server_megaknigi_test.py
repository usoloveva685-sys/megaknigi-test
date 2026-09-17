#!/usr/bin/env python3
import json, re, os, urllib.parse, urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.geturl(), r.read().decode("utf-8", "ignore")

def card_blocks(html):
    starts = list(re.finditer(
        r'<div\b[^>]*class=["\'][^"\']*bookCard[^"\']*["\'][^>]*>',
        html, re.I
    ))
    blocks = []
    for i, sm in enumerate(starts):
        end = starts[i+1].start() if i+1 < len(starts) else len(html)
        blocks.append(html[sm.start():end])
    return blocks

def parse_search_card(card):
    links = []
    for m in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*>(.*?)</a>',
        card, re.I|re.S
    ):
        href = urllib.parse.urljoin(BASE, m.group(1))
        txt = clean(m.group(2))
        if href not in [x[0] for x in links]:
            links.append((href, txt))
    if not links:
        return []

    title = ""
    for p in [
        r'<[^>]+class=["\'][^"\']*(?:book-title|bookTitle|book-name|bookName|title)[^"\']*["\'][^>]*>(.*?)</[^>]+>',
        r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>'
    ]:
        m = re.search(p, card, re.I|re.S)
        if m and clean(m.group(1)):
            title = clean(m.group(1))
            break
    if not title:
        for _, txt in links:
            if txt and txt.lower() not in {"читать","скачать","подробнее"}:
                title = txt
                break

    cover = ""
    for tag in re.findall(r'<img\b[^>]*>', card, re.I|re.S):
        src = attr(tag,"src") or attr(tag,"data-src") or attr(tag,"data-original") or attr(tag,"data-lazy-src")
        if src and not src.startswith("data:"):
            full = urllib.parse.urljoin(BASE, src)
            if "/storage/cover/" in full.lower():
                cover = full
                break

    return [{"title": title, "author": "", "cover": cover, "url": href} for href,_ in links]

def extract_jsonld(html):
    authors, images = [], []
    for raw in re.findall(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I|re.S
    ):
        try:
            obj = json.loads(raw.strip())
        except Exception:
            continue
        objs = obj if isinstance(obj,list) else [obj]
        for o in objs:
            if not isinstance(o,dict):
                continue
            a = o.get("author")
            if isinstance(a,dict) and a.get("name"):
                authors.append(str(a["name"]))
            elif isinstance(a,list):
                for x in a:
                    if isinstance(x,dict) and x.get("name"):
                        authors.append(str(x["name"]))
                    elif isinstance(x,str):
                        authors.append(x)
            elif isinstance(a,str):
                authors.append(a)
            im = o.get("image")
            if isinstance(im,str):
                images.append(im)
            elif isinstance(im,dict) and im.get("url"):
                images.append(str(im["url"]))
            elif isinstance(im,list):
                images.extend([str(x) for x in im if isinstance(x,str)])
    return authors, images

def enrich(item):
    # The search page may repeat a generic image/genre text.
    # The detail page is treated as the authoritative source for author and cover.
    try:
        status, final_url, html = fetch(item["url"])
    except Exception as e:
        item["detail_error"] = type(e).__name__
        return item

    authors, images = extract_jsonld(html)

    # Author: JSON-LD first, then explicit author class/label.
    author = ""
    for a in authors:
        a = clean(a)
        if a and a.lower() not in {"проза","детская литература","фэнтези","фантастика"}:
            author = a
            break

    if not author:
        patterns = [
            r'<[^>]+class=["\'][^"\']*author[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            r'(?:Автор|Авторы)\s*[:\-]\s*([^<]{2,160})',
        ]
        for p in patterns:
            m = re.search(p, html, re.I|re.S)
            if m:
                cand = clean(m.group(1))
                if cand and cand.lower() not in {"проза","детская литература"}:
                    author = cand
                    break

    if author:
        item["author"] = author

    # Cover: prefer og:image / JSON-LD image from the actual book page.
    detail_cover = ""
    for m in re.finditer(
        r'<meta\b[^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*>',
        html, re.I
    ):
        detail_cover = attr(m.group(0), "content")
        if detail_cover:
            break

    if not detail_cover and images:
        detail_cover = images[0]

    if detail_cover:
        item["cover"] = urllib.parse.urljoin(BASE, detail_cover)

    item["detail_status"] = status
    return item

def parse_results(html):
    results, seen = [], set()

    blocks = card_blocks(html)
    for block in blocks:
        for item in parse_search_card(block):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            results.append(item)

    # Fallback if MegaKnigi changes the card class.
    if not results:
        for m in re.finditer(
            r'<a\b[^>]*href=["\']([^"\']*/read/\d+(?:/[^"\']*)?)["\'][^>]*>(.*?)</a>',
            html, re.I|re.S
        ):
            href = urllib.parse.urljoin(BASE,m.group(1))
            if href in seen:
                continue
            txt = clean(m.group(2))
            if txt and txt.lower() not in {"читать","скачать"}:
                seen.add(href)
                results.append({"title":txt,"author":"","cover":"","url":href})

    # Enrich only the first 10 results in the diagnostic test.
    # This avoids hammering MegaKnigi while giving us authoritative detail-page data.
    for i in range(min(10, len(results))):
        results[i] = enrich(results[i])

    return results[:30]

def diagnostic(q):
    url = BASE + "/search?" + urllib.parse.urlencode({"q": q})
    try:
        status, final_url, html = fetch(url)
        results = parse_results(html)
        return {
            "query": q,
            "request": {
                "method":"GET","url":url,"status":status,
                "ok":200 <= status < 400,
                "bytes":len(html.encode("utf-8")),
                "final_url":final_url,"error":None,"parse_error":None
            },
            "structure": {
                "read_links_found":len(re.findall(r'/read/\d+',html,re.I)),
                "bookCard_mentions":len(re.findall(r'bookCard',html,re.I)),
                "cards_used":len(card_blocks(html))
            },
            "cards_found":len(results),
            "results":results
        }
    except Exception as e:
        return {
            "query":q,
            "request":{"method":"GET","url":url,"status":None,"ok":False,"bytes":0,
                       "final_url":None,"error":type(e).__name__+": "+str(e),"parse_error":None},
            "structure":{},"cards_found":0,"results":[]
        }

class Handler(BaseHTTPRequestHandler):
    def send_json(self,obj,code=200):
        data=json.dumps(obj,ensure_ascii=False,indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        p=urllib.parse.urlparse(self.path)
        if p.path=="/api/health":
            return self.send_json({
                "ok":True,
                "test":"MegaKnigi adapter v5",
                "endpoint":"/api/megaknigi-test?q=Гарри%20Поттер"
            })
        if p.path=="/api/megaknigi-test":
            q=urllib.parse.parse_qs(p.query).get("q",[""])[0].strip()
            if not q:
                return self.send_json({"error":"query required"},400)
            return self.send_json(diagnostic(q))
        return self.send_json({"ok":True,"service":"MegaKnigi parser v5","try":"/api/health"})
    def log_message(self,fmt,*args):
        print(fmt%args)

if __name__=="__main__":
    print(f"MegaKnigi adapter v5 listening on {HOST}:{PORT}")
    HTTPServer((HOST,PORT),Handler).serve_forever()
