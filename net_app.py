"""NET 選品小幫手（本機版）

★ 這是「在你自己的電腦上執行」的版本：啟動後只在本機 http://127.0.0.1:8765 提供網頁，
  外部連不進來；爬蟲也只在你點了目錄之後才在背景執行。

功能：
- 上方目錄自動取自 NET 網站選單（大分類 → 子分類／活動），點了才開始爬
- 一款一款確認完整才顯示：先用本機記憶排除已知中國製（0 請求）、活動頁先排除沒庫存（0 請求），
  其餘才抓商品頁，確認產地與每個顏色的庫存；只有確定有貨的顏色、尺寸才會出現
- 背景一路爬完整個分類，卡片陸續出現；捲到底還沒爬完會看到「資料抓取中」
- 購物清單（點尺寸加入、估算活動價、湊件提醒、複製清單），存在瀏覽器

執行（建議在專案資料夾內建立獨立虛擬環境，不影響電腦上其他 Python 程式）：
    python -m venv .venv
    .venv\\Scripts\\activate        （macOS / Linux：source .venv/bin/activate）
    pip install -r requirements.txt
    python net_app.py

需要 Python 3.8 以上；只用標準函式庫 + requests + beautifulsoup4。
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import net_crawler as nc

APP_DIR = Path(__file__).resolve().parent
TEMPLATE = APP_DIR / 'app_template.html'
DEFAULT_EXCLUDE = ['中國', '大陸', 'China']
CATEGORY_RULES = [('童裝', ['BABY', '童']), ('褲・裙', ['褲', '裙']), ('洋裝', ['洋裝']),
                  ('外套', ['外套', '夾克']), ('配件', ['襪', '皮帶', '帽', '包'])]

log = logging.getLogger('net_app')


# ---------- 小工具 ----------

def guess_category(name):
    upper = name.upper()
    for category, words in CATEGORY_RULES:
        if any(w in upper for w in words):
            return category
    return '上衣'


def to_int(value):
    digits = re.sub(r'[^\d.]', '', str(value or ''))
    return int(float(digits)) if digits else 0


def split_color(color):
    """'720暗夜藍' → ('720', '暗夜藍')。"""
    m = re.match(r'(\d{2,4})\s*(.*)', color or '')
    return (m.group(1), m.group(2) or color) if m else ('', color or '')


def is_excluded(origin, words):
    return any(w.lower() in (origin or '').lower() for w in words)


# ---------- 產地記憶 ----------

class OriginMemory:
    """記住每一款（以品名為準）的產地。產地幾乎不會變，所以長期保存；
    下次在列表上遇到已知中國製的款式，不必再抓商品頁。
    實測零碼出清 207 款，同名不同顏色的產地沒有一筆不同。"""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute('CREATE TABLE IF NOT EXISTS origins '
                         '(name TEXT PRIMARY KEY, origin TEXT NOT NULL, checked_at REAL NOT NULL)')
        self._db.commit()
        self._lock = threading.Lock()

    def get(self, name):
        with self._lock:
            row = self._db.execute('SELECT origin FROM origins WHERE name = ?', (name,)).fetchone()
        return row[0] if row else None

    def set(self, name, origin):
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO origins VALUES (?, ?, ?)', (name, origin, time.time()))
            self._db.commit()


# ---------- 目錄 ----------

def _target(href):
    m = re.search(r'(category|promotion)(?:/|\?id=)(\d+)', href or '')
    return (m.group(1), m.group(2)) if m else (None, None)


def parse_main_nav(html):
    """首頁上方主選單：WOMEN 女裝、MEN 男裝、KIDS 童裝、BABY 嬰兒、SALE 促銷。"""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for a in soup.select('ul.main_nav > li > a'):
        kind, target_id = _target(a.get('href'))
        if kind:
            items.append({'title': a.get_text(' ', strip=True), 'kind': kind, 'id': target_id})
    return items


def parse_sidebar(html):
    """分類頁左側選單：[{title, kind, id, children: [...]}]，含精選優惠的活動連結。"""
    soup = BeautifulSoup(html, 'html.parser')
    groups = []
    for li in soup.select('.silderbar_category .siderbar_sec > ul > li'):
        head = li.find('a', recursive=False)
        if not head:
            continue
        kind, target_id = _target(head.get('href'))
        children = []
        for a in li.select(':scope > ul > li > a'):  # 只看自己底下的子項目
            ck, cid = _target(a.get('href'))
            if ck:
                title = ' '.join(a.get_text(' ', strip=True).lstrip('。').split())
                children.append({'title': title, 'kind': ck, 'id': cid})
        groups.append({'title': head.get_text(' ', strip=True), 'kind': kind, 'id': target_id,
                       'children': children})
    return groups


# ---------- 爬取 ----------

class Section:
    """一個目錄項目（分類或活動）的爬取狀態與結果。"""

    def __init__(self, kind, target_id, title):
        self.kind, self.id, self.title = kind, target_id, title or f'{kind}/{target_id}'
        self.status = 'idle'      # idle / queued / running / done / error
        self.message = ''
        self.promo = ''
        self.refresh = False      # True：這次商品頁一律重抓（使用者按「重新抓庫存」）
        self.total = 0            # 已知款數（讀完列表頁後確定）
        self.checked = 0
        self.excluded_origin = 0
        self.excluded_stock = 0
        self.failed = 0           # 商品頁讀取失敗（下架、網路不穩）而略過的款數
        self.requests = 0
        self.cards = []
        self.finished_at = None

    @property
    def key(self):
        return f'{self.kind}/{self.id}'

    def reset(self):
        self.__init__(self.kind, self.id, self.title)

    def to_dict(self, start=0):
        return {
            'key': self.key, 'kind': self.kind, 'id': self.id, 'title': self.title,
            'status': self.status, 'message': self.message, 'promo': self.promo,
            'total': self.total, 'checked': self.checked, 'shown': len(self.cards),
            'excluded_origin': self.excluded_origin, 'excluded_stock': self.excluded_stock,
            'failed': self.failed, 'requests': self.requests, 'finished_at': self.finished_at,
            'from': start, 'cards': self.cards[start:],
            'url': f'{nc.BASE_URL}/{self.kind}/{self.id}',
        }


class SkipStyle(Exception):
    """這一款讀不到（下架、網路不穩、版面解析失敗），略過它繼續下一款。"""


class Crawler:
    """單一背景執行緒，依點擊順序一次爬一個目錄項目；全程一次只發一個請求。"""

    def __init__(self, client, memory, exclude=DEFAULT_EXCLUDE, stock_hours=6):
        self.client = client
        self.memory = memory
        self.exclude = exclude
        self.stock_seconds = stock_hours * 3600
        self.sections = {}
        self.queue = deque()
        self.lock = threading.RLock()           # 保護 sections / queue / current / blocked
        self.net_lock = threading.Lock()        # 確保同一時間只有一個請求
        self.wake = threading.Condition(self.lock)
        self.blocked = ''
        self.current = None
        self._thread = threading.Thread(target=self._run, name='crawler', daemon=True)

    def start(self):
        self._thread.start()

    # ---- 對外 ----

    def block(self, reason):
        """網站拒絕請求：之後任何請求都不再發出，排隊中的也全部取消。"""
        with self.lock:
            if not self.blocked:
                log.error('已停止所有抓取：%s', reason)
            self.blocked = self.blocked or str(reason)
            for s in self.queue:
                s.status, s.message = 'error', '已停止：網站拒絕請求，請稍後重開程式'
            self.queue.clear()

    def fetch(self, url, use_cache=True):
        """所有對 NET 的請求都經過這裡：一次一個；被擋之後一律不再發。"""
        with self.net_lock:
            if self.blocked:
                raise nc.BlockedError(self.blocked)
            before = self.client.requests_made
            try:
                return self.client.get(url, use_cache=use_cache)
            except nc.BlockedError as e:
                self.block(e)
                raise
            finally:
                if threading.current_thread() is self._thread:  # 只把背景爬取的請求算進該分類
                    sec = self.current
                    if sec is not None:
                        sec.requests += self.client.requests_made - before

    def request(self, kind, target_id, title='', refresh=False):
        with self.lock:
            sec = self.sections.get(f'{kind}/{target_id}')
            if sec is None:
                sec = self.sections[f'{kind}/{target_id}'] = Section(kind, target_id, title)
            if self.blocked:  # 被擋之後不再發任何請求
                if sec.status != 'done':
                    sec.status, sec.message = 'error', '已停止：網站拒絕請求，請稍後重開程式'
                return sec
            stale = (sec.status == 'done' and sec.finished_at
                     and time.time() - sec.finished_at > self.stock_seconds)
            if sec.status in ('idle', 'error') or stale or (refresh and sec.status == 'done'):
                sec.reset()
                sec.refresh = bool(refresh)
                sec.status = 'queued'
                self.queue.append(sec)
                self.wake.notify()
            return sec

    def section(self, kind, target_id):
        with self.lock:
            return self.sections.get(f'{kind}/{target_id}')

    def status(self):
        with self.lock:
            return {'blocked': self.blocked,
                    'current': self.current.key if self.current else None,
                    'queue': [s.key for s in self.queue],
                    'sections': {k: {'status': s.status, 'shown': len(s.cards), 'title': s.title}
                                 for k, s in self.sections.items()}}

    # ---- 背景執行緒 ----

    def _run(self):
        while True:
            with self.lock:
                while not self.queue:
                    self.wake.wait()
                sec = self.queue.popleft()
                sec.status = 'running'
                self.current = sec
            try:
                if sec.kind == 'promotion':
                    self._crawl_promotion(sec)
                else:
                    self._crawl_category(sec)
                sec.status = 'done'
            except nc.BlockedError as e:
                sec.status, sec.message = 'error', str(e)
                self.block(e)
            except Exception as e:  # 單一分類失敗不影響其他分類
                log.exception('爬取 %s 失敗', sec.key)
                sec.status, sec.message = 'error', f'{type(e).__name__}: {e}'
            finally:
                sec.finished_at = time.time()
                with self.lock:
                    self.current = None
                log.info('%s %s：顯示 %d 款／檢查 %d 款，排除產地 %d、沒庫存 %d、讀取失敗 %d，請求 %d 次',
                         sec.key, sec.status, len(sec.cards), sec.checked, sec.excluded_origin,
                         sec.excluded_stock, sec.failed, sec.requests)

    def _listing(self, sec):
        """讀完所有列表頁（每頁 1 請求），依品名分組；同名 = 同一款的不同顏色。"""
        first_url, html = self.fetch(nc.page_url(sec.kind, sec.id), use_cache=False)
        if urlparse(first_url).path in ('', '/'):
            raise RuntimeError('這個分類已不存在（被導回首頁）')
        products, last_page, title = nc.parse_listing(sec.kind, html, first_url)
        if sec.kind == 'promotion':
            sec.promo = title
            if products and not any('sizes' in p[3] for p in products):
                raise RuntimeError('讀不到活動頁的庫存資料（網站版面可能改了）')
        for page in range(2, (last_page or 1) + 1):
            url, html = self.fetch(nc.page_url(sec.kind, sec.id, page), use_cache=False)
            products += nc.parse_listing(sec.kind, html, url)[0]
        styles = OrderedDict()
        seen = set()
        for p in products:
            if p[0] not in seen:
                seen.add(p[0])
                # 列表上沒有品名時，用連結當作獨立一款，避免把不同商品混在一起
                styles.setdefault(p[2] or p[0], []).append(p)
        sec.total = len(styles)
        return styles

    def _known_excluded(self, name):
        origin = self.memory.get(name) if name else None
        return origin is not None and is_excluded(origin, self.exclude)

    def _product(self, sec, link):
        """抓商品頁；讀不到或解析不到品名就 SkipStyle。被擋（BlockedError）則往上拋，整個停止。"""
        try:
            _, html = self.fetch(link, use_cache=not sec.refresh)
        except (requests.RequestException, nc.TemporaryError) as e:
            log.warning('商品頁讀取失敗（%s），略過：%s', e, link)
            raise SkipStyle()
        item = nc.parse_product_page(html)
        if not item['name']:
            log.warning('解析不到品名，略過：%s', link)
            raise SkipStyle()
        return item

    def _remember(self, name, item):
        if name and not name.startswith('http'):
            self.memory.set(name, item['origin'])

    def _card(self, name, origin, colors, promo=''):
        return {'name': name, 'cat': guess_category(name), 'origin': origin.replace('製', ''),
                'promo': promo, 'colors': colors}

    def _crawl_category(self, sec):
        for name, entries in self._listing(sec).items():
            try:
                if self._known_excluded(name):           # 已知中國製：0 請求
                    sec.excluded_origin += 1
                    continue
                todo = [(link, img) for link, img, _, _ in entries]
                done, colors, origin = set(), [], None
                while todo:
                    link, img = todo.pop(0)
                    if link in done:
                        continue
                    done.add(link)
                    try:
                        item = self._product(sec, link)
                    except SkipStyle:
                        if origin is None:               # 第一個顏色就讀不到：整款略過
                            raise
                        continue                         # 其他顏色讀不到（例如下架）：略過這個顏色
                    if origin is None:
                        origin = item['origin']
                        name = name if not name.startswith('http') else item['name']
                        self._remember(name, item)
                    if is_excluded(origin, self.exclude):  # 一發現中國製就停，不再抓其他顏色
                        break
                    todo += [(c, '') for c in item['color_links'] if c not in done]
                    if item['sizes']:                       # 只收確定有庫存的顏色
                        code, color = split_color(item['color'])
                        price = to_int(item['price'])
                        colors.append({'code': code, 'color': color, 'sizes': item['sizes'].split('/'),
                                       'price': price, 'orig': to_int(item['original_price']) or price,
                                       'link': link, 'img': img or urljoin(link, item['main_img'])})
                if is_excluded(origin, self.exclude):
                    sec.excluded_origin += 1
                elif colors:
                    sec.cards.append(self._card(name, origin, colors))
                else:
                    sec.excluded_stock += 1
            except SkipStyle:
                sec.failed += 1
            finally:
                sec.checked += 1

    def _crawl_promotion(self, sec):
        for name, entries in self._listing(sec).items():
            try:
                # 活動頁本身就有各顏色庫存：先排除沒貨的，0 請求
                stocked = [e for e in entries if e[3].get('sizes')]
                if not stocked:
                    sec.excluded_stock += 1
                    continue
                origin = self.memory.get(name) if not name.startswith('http') else None
                if origin is None:
                    item = self._product(sec, stocked[0][0])
                    origin = item['origin']
                    self._remember(name, item)
                if is_excluded(origin, self.exclude):
                    sec.excluded_origin += 1
                    continue
                colors = []
                for link, img, _, extra in stocked:
                    code, color = split_color(extra.get('color', ''))
                    price = to_int(extra.get('promo_price'))
                    colors.append({'code': code, 'color': color, 'sizes': extra['sizes'].split('/'),
                                   'price': price, 'orig': to_int(extra.get('original_price')) or price,
                                   'link': link, 'img': img})
                sec.cards.append(self._card(name, origin, colors, sec.promo))
            except SkipStyle:
                sec.failed += 1
            finally:
                sec.checked += 1


# ---------- 網頁伺服器 ----------

class App:
    def __init__(self, crawler, exclude):
        self.crawler = crawler
        self.exclude = exclude
        self._menu = None

    def menu(self):
        if self._menu is None:
            _, html = self.crawler.fetch(nc.BASE_URL + '/')
            self._menu = parse_main_nav(html)
        return self._menu

    def submenu(self, kind, target_id):
        if kind != 'category':
            return []
        _, html = self.crawler.fetch(nc.page_url(kind, target_id))
        return parse_sidebar(html)

    def page(self):
        config = {'exclude': self.exclude, 'stock_hours': self.crawler.stock_seconds / 3600}
        return TEMPLATE.read_text(encoding='utf-8').replace(
            '/*CONFIG*/{}', json.dumps(config, ensure_ascii=False).replace('<', '\\u003c'))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'NetApp/1.0'

        def log_message(self, fmt, *args):  # 不在終端機洗版
            log.debug(fmt, *args)

        def _send(self, status, body, ctype='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj, ensure_ascii=False))

        def _local_only(self):
            # 只接受本機網址（擋 DNS rebinding）；API 另外要求自訂標頭，其他網站的網頁無法帶上
            # （跨站請求帶自訂標頭需要 CORS 預檢，本程式不允許），避免別的網站偷偷叫本機程式開始爬
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0]
            ok = host in ('127.0.0.1', 'localhost')
            if ok and self.path.startswith('/api/'):
                ok = (self.headers.get('X-Requested-With') == 'net-app'
                      and self.headers.get('Sec-Fetch-Site', 'same-origin') in ('same-origin', 'none'))
            if not ok:
                self._send(403, 'forbidden', 'text/plain')
            return ok

        def do_GET(self):
            if not self._local_only():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split('/') if p]
            q = parse_qs(url.query)
            try:
                if not parts:
                    return self._send(200, app.page(), 'text/html; charset=utf-8')
                if parts == ['api', 'menu']:
                    return self._json({'items': app.menu()})
                if len(parts) == 4 and parts[:2] == ['api', 'submenu']:
                    return self._json({'groups': app.submenu(parts[2], parts[3])})
                if len(parts) == 4 and parts[:2] == ['api', 'section']:
                    sec = app.crawler.section(parts[2], parts[3])
                    if sec is None:
                        return self._json({'status': 'idle', 'cards': [], 'from': 0})
                    start = int((q.get('from') or ['0'])[0])
                    return self._json(sec.to_dict(start))
                if parts == ['api', 'status']:
                    return self._json(app.crawler.status())
            except nc.BlockedError as e:  # fetch() 已經停止所有抓取
                return self._json({'error': str(e)}, 502)
            except Exception as e:
                log.exception('處理 %s 失敗', self.path)
                return self._json({'error': f'{type(e).__name__}: {e}'}, 500)
            self._send(404, 'not found', 'text/plain')

        def do_POST(self):
            if not self._local_only():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split('/') if p]
            q = parse_qs(url.query)
            if len(parts) == 4 and parts[:2] == ['api', 'crawl'] and parts[2] in ('category', 'promotion') \
                    and parts[3].isdigit():
                sec = app.crawler.request(parts[2], parts[3], (q.get('title') or [''])[0],
                                          refresh=(q.get('refresh') or ['0'])[0] == '1')
                return self._json(sec.to_dict(len(sec.cards)))
            self._send(404, 'not found', 'text/plain')

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description='NET 選品小幫手（本機版）')
    p.add_argument('--port', type=int, default=8765, help='本機網頁埠號（預設 8765）')
    p.add_argument('--data-dir', default=str(APP_DIR / 'data'), help='快取與產地記憶的資料夾')
    p.add_argument('--stock-hours', type=float, default=6, help='商品頁（庫存）幾小時內不重抓（預設 6）')
    p.add_argument('--delay', type=float, default=3.0, help='每個請求最少間隔秒數（預設 3）')
    p.add_argument('--jitter', type=float, default=3.0, help='額外隨機間隔上限秒數（預設 3）')
    p.add_argument('--max-requests', type=int, default=5000, help='本次執行請求上限（預設 5000）')
    p.add_argument('--exclude-origin', nargs='*', default=DEFAULT_EXCLUDE, help='排除的產地')
    p.add_argument('--no-browser', action='store_true', help='啟動後不自動開瀏覽器')
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')

    data_dir = Path(args.data_dir)
    client = nc.PoliteClient(delay=args.delay, jitter=args.jitter, max_requests=args.max_requests,
                             cache_dir=data_dir / 'cache', max_age_hours=args.stock_hours)
    crawler = Crawler(client, OriginMemory(data_dir / 'origins.sqlite3'), args.exclude_origin,
                      stock_hours=args.stock_hours)
    crawler.start()
    app = App(crawler, args.exclude_origin)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    url = f'http://127.0.0.1:{args.port}/'
    log.info('NET 選品小幫手（本機版）已啟動：%s　（按 Ctrl+C 結束）', url)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('已結束')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
