#!/usr/bin/env python3
import json, os, re, urllib.parse, urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE='https://megaknigi.ru'
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36'

def fetch(url, timeout=25):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8','Accept-Language':'ru-RU,ru;q=0.9,en;q=0.5'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read()
            return {'ok':True,'status':getattr(r,'status',200),'url':r.geturl(),'bytes':len(raw),'html':raw.decode('utf-8','ignore'),'error':None}
    except urllib.error.HTTPError as e:
        raw=e.read(5000) if hasattr(e,'read') else b''
        return {'ok':False,'status':e.code,'url':url,'bytes':len(raw),'html':raw.decode('utf-8','ignore'),'error':str(e)}
    except Exception as e:
        return {'ok':False,'status':None,'url':url,'bytes':0,'html':'','error':f'{type(e).__name__}: {e}'}

def clean(s): return re.sub(r'\s+',' ',s or '').strip()
def absurl(x): return urllib.parse.urljoin(BASE,x or '')

def text_of(raw):
    return clean(re.sub(r'<[^>]+>',' ',raw))

class LinkScanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.stack=[]; self.links=[]; self.images=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs); classes=(a.get('class') or '').split(); ident=a.get('id') or ''
        self.stack.append({'tag':tag,'class':classes,'id':ident})
        if tag=='a' and '/read/' in (a.get('href') or ''):
            self.links.append({'href':absurl(a['href']),'text':'','ancestors':self.stack[-8:].copy()})
        if tag=='img':
            src=a.get('src') or a.get('data-src') or a.get('data-lazy-src') or a.get('data-original') or ''
            if src: self.images.append(absurl(src))
    def handle_data(self,data):
        t=clean(data)
        if t and self.links:
            # attach to the currently open read link only when it is still open
            if self.stack and self.stack[-1]['tag']=='a': self.links[-1]['text']=clean(self.links[-1]['text']+' '+t)
    def handle_endtag(self,tag):
        if self.stack: self.stack.pop()

def structure_diagnostic(html):
    p=LinkScanner(); p.feed(html); p.close()
    class_counts={}
    for x in p.links:
        for anc in x['ancestors']:
            for c in anc.get('class',[]): class_counts[c]=class_counts.get(c,0)+1
    top=sorted(class_counts.items(),key=lambda z:(-z[1],z[0]))[:30]
    return {'read_links_found':len(p.links),'read_links':p.links[:20],'top_ancestor_classes':top,'images_found':len(p.images),'sample_images':p.images[:10]}

# Generic parser: use every /read/ link and inspect its nearby HTML block.
def parse_generic(html):
    # Split around read links. MegaKnigi has changed card markup over time, so don't depend on .bookCard.row.
    pat=re.compile(r'<a[^>]+href=["\']([^"\']*/read/[^"\']*)["\'][^>]*>(.*?)</a>',re.I|re.S)
    matches=list(pat.finditer(html)); out=[]; seen=set()
    for m in matches:
        url=absurl(m.group(1)); anchor=text_of(m.group(2))
        # A generous local window around the result link.
        start=max(0,m.start()-7000); end=min(len(html),m.end()+3500); block=html[start:end]
        # Prefer nearest enclosing div with card/item/book-like class if present.
        containers=re.findall(r'<(?:div|article|li)[^>]*class=["\'][^"\']*(?:book|card|item|result)[^"\']*["\'][^>]*>.*?</(?:div|article|li)>',block,re.I|re.S)
        if containers:
            block=min(containers,key=lambda x: abs(x.find(m.group(0))))
        txt=text_of(block)
        title=anchor if anchor and anchor.lower() not in ('читать','подробнее','скачать') else ''
        # Search likely title headings/links containing the same /read/ url.
        if not title:
            hs=re.findall(r'<(?:h[1-6]|strong|b)[^>]*>(.*?)</(?:h[1-6]|strong|b)>',block,re.I|re.S)
            for h in hs:
                t=text_of(h)
                if len(t)>2 and t.lower() not in ('читать','подробнее'):
                    title=t; break
        if not title:
            # first meaningful line before metadata
            bits=[b for b in re.split(r'\s{2,}|\n',txt) if len(b.strip())>2]
            title=bits[0][:250] if bits else ''
        # Cover
        cover=''
        for src in re.findall(r'<img[^>]+(?:src|data-src|data-lazy-src|data-original)=["\']([^"\']+)',block,re.I):
            u=absurl(src)
            if 'cover' in u.lower() or '/storage/' in u.lower(): cover=u; break
        # Author: explicit label first, then common author link patterns.
        author=''
        am=re.search(r'(?:Автор|автор)\s*[:\-]?\s*</?[^>]*>?\s*([^<]{2,160})',block,re.I)
        if am: author=clean(am.group(1))
        if not author:
            alinks=re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',block,re.I|re.S)
            for href,inner in alinks:
                t=text_of(inner)
                if '/author' in href.lower() and 2<len(t)<120:
                    author=t; break
        # If the title itself contains a colon subtitle, don't treat it as author.
        rec={'title':clean(title),'author':clean(author),'cover':cover,'url':url}
        key=url
        if key not in seen and (rec['title'] or rec['cover']): seen.add(key); out.append(rec)
    return out[:50]

def test_query(q):
    q=(q or '').strip(); url=BASE+'/search?q='+urllib.parse.quote(q,safe='')
    r=fetch(url)
    diag=structure_diagnostic(r.get('html','')) if r.get('ok') else {}
    results=parse_generic(r.get('html','')) if r.get('ok') else []
    return {'query':q,'request':{k:r.get(k) for k in ('status','ok','bytes','url','error')},'diagnostic':diag,'cards_found':len(results),'results':results}

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        u=urllib.parse.urlsplit(self.path)
        if u.path=='/api/health':
            body=json.dumps({'ok':True,'test':'MegaKnigi adapter v3','endpoint':'/api/megaknigi-test?q=Гарри%20Поттер'},ensure_ascii=False).encode()
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        if u.path=='/api/megaknigi-test':
            q=urllib.parse.parse_qs(u.query).get('q',[''])[0]; body=json.dumps(test_query(q),ensure_ascii=False,indent=2).encode()
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        super().do_GET()

if __name__=='__main__':
    port=int(os.environ.get('PORT','8765')); print('MegaKnigi diagnostic v3 on',port); ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
