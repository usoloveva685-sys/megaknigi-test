#!/usr/bin/env python3
import json, re, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import os

UA = 'VisualKeiBookshelf-MegaKnigi-Test/1.0'
BASE = 'https://megaknigi.ru'


def fetch(url, method='GET', data=None, timeout=15):
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.5',
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return {
                'ok': True,
                'status': getattr(r, 'status', 200),
                'url': r.geturl(),
                'content_type': r.headers.get_content_type(),
                'bytes': len(raw),
                'html': raw.decode('utf-8', errors='ignore'),
            }
    except urllib.error.HTTPError as e:
        try: raw = e.read(1200)
        except Exception: raw = b''
        return {'ok': False, 'status': e.code, 'url': url, 'error': str(e), 'html': raw.decode('utf-8', errors='ignore')}
    except Exception as e:
        return {'ok': False, 'status': None, 'url': url, 'error': f'{type(e).__name__}: {e}', 'html': ''}


def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s or '')).strip()


def parse_cards(html):
    # Lightweight parser deliberately avoids relying on one exact CSS framework.
    # We look for MegaKnigi book/read links and nearby title/author/image data.
    out, seen = [], set()
    for m in re.finditer(r'href=[\"\']([^\"\']*(?:/read/|/book/)[^\"\']*)[\"\']', html, re.I):
        href = urllib.parse.urljoin(BASE, m.group(1))
        if href in seen: continue
        seen.add(href)
        a = max(0, m.start()-1800); b = min(len(html), m.end()+1800)
        block = html[a:b]
        imgs = re.findall(r'<img[^>]+(?:src|data-src)=[\"\']([^\"\']+)[\"\'][^>]*>', block, re.I)
        image = urllib.parse.urljoin(BASE, imgs[0]) if imgs else ''
        texts = [clean(x) for x in re.findall(r'<(?:h1|h2|h3|h4|a|p|span|div)[^>]*>(.*?)</(?:h1|h2|h3|h4|a|p|span|div)>', block, re.I|re.S)]
        texts = [x for x in texts if x and len(x) < 220]
        # Prefer the first nearby linked/title text that is not generic UI.
        title = ''
        for t in texts:
            low = t.lower()
            if t not in ('Читать', 'Читать →', 'Подробнее') and 'мега книги' not in low:
                title = t; break
        out.append({'title': title, 'author': '', 'cover': image, 'url': href})
        if len(out) >= 30: break
    return out


def test_query(q):
    q = (q or '').strip()
    candidates = []
    encoded = urllib.parse.quote(q, safe='')
    # We intentionally test common search parameter names instead of assuming
    # one undocumented endpoint. The first successful result page wins.
    for name in ('q', 'query', 'search', 'term', 'text', 's'):
        candidates.append(('GET', f'{BASE}/search?{name}={encoded}', None))
    for name in ('q', 'query', 'search', 'term', 'text', 's'):
        candidates.append(('POST', f'{BASE}/search', {name: q}))

    attempts = []
    best = None
    for method, url, data in candidates:
        r = fetch(url, method, data)
        sample = clean(r.get('html', ''))[:240]
        cards = parse_cards(r.get('html', '')) if r.get('ok') else []
        item = {
            'method': method, 'url': url, 'status': r.get('status'),
            'ok': r.get('ok'), 'bytes': r.get('bytes', len(r.get('html',''))),
            'final_url': r.get('url'), 'error': r.get('error'),
            'cards': len(cards), 'sample': sample,
        }
        attempts.append(item)
        if r.get('ok') and cards:
            best = {'attempt': item, 'results': cards}
            break

    return {'query': q, 'best': best, 'attempts': attempts}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == '/api/megaknigi-test':
            q = urllib.parse.parse_qs(u.query).get('q', [''])[0]
            result = test_query(q)
            body = json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body); return
        if u.path == '/api/health':
            body = json.dumps({'ok': True, 'test': 'MegaKnigi adapter', 'endpoint': '/api/megaknigi-test?q=Корнев'}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers(); self.wfile.write(body); return
        super().do_GET()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8765'))
    print(f'MegaKnigi diagnostic: http://0.0.0.0:{port}')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
