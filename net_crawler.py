"""NET 購物網站產品爬蟲（禮貌版）

爬取分類頁或活動頁下的產品資訊（品名、價格、產地、活動、連結、圖片），輸出 CSV。

為了避免再被對方封鎖，這版做了以下限制：
- 遵守 robots.txt（被禁止的網址不抓）
- 每個請求之間固定間隔 + 隨機抖動（預設 3~6 秒），不並行
- 共用同一個 Session（Keep-Alive），帶正常的 User-Agent
- 429 / 503 依 Retry-After 退避重試；遇到 403 或連續失敗直接停止，不硬撞
- 產品頁 HTML 存本機快取，重跑時不重抓；產品連結去重，不重複請求
- 每次執行有請求數上限（--max-requests）

用法範例：
    # 找出嬰幼兒相關分類代號
    python net_crawler.py discover --keyword 嬰 寶寶 BABY 童

    # 爬分類 1662，只留品名含「褲」、排除中國製
    python net_crawler.py crawl 1662 --name-keyword 褲 --exclude-origin 中國 大陸 China

    # 爬活動頁（可直接貼網址）
    python net_crawler.py crawl https://www.net-fashion.net/promotion/1490 promotion/658
"""

import argparse
import csv
import hashlib
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = 'https://www.net-fashion.net'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/128.0 Safari/537.36')
UNKNOWN_ORIGIN = '未標示'

log = logging.getLogger('net_crawler')


class BlockedError(RuntimeError):
    """對方開始拒絕我們（403）或達到請求上限，應立即停止所有請求。"""


class TemporaryError(RuntimeError):
    """連線一直失敗（網路不穩、對方暫時沒回應），重試後仍不行。不代表被封鎖。"""


class PoliteClient:
    def __init__(self, delay=3.0, jitter=3.0, max_requests=300,
                 cache_dir='cache', respect_robots=True, max_retries=3, refresh=False, max_age_hours=None):
        self.delay = delay
        self.jitter = jitter
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.refresh = refresh  # True：不讀快取（庫存要最新時用），但仍會更新快取
        self.max_age = max_age_hours * 3600 if max_age_hours else None  # 只沿用這麼新的快取
        self.requests_made = 0
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': USER_AGENT,
            'Accept-Language': 'zh-TW,zh;q=0.9',
        })
        self.robots = None
        if respect_robots:
            self.robots = robotparser.RobotFileParser()
            try:
                r = self._request(urljoin(BASE_URL, '/robots.txt'))
                # 網站沒有 robots.txt 時會被導回首頁，此時視為沒有限制
                is_robots = r.ok and urlparse(r.url).path.endswith('/robots.txt')
                self.robots.parse(r.text.splitlines() if is_robots else [])
            except requests.RequestException as e:
                log.warning('讀不到 robots.txt（%s），視為全部允許', e)
                self.robots.parse([])

    def _wait(self):
        target = self._last_request + self.delay + random.uniform(0, self.jitter)
        remaining = target - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, url):
        if self.requests_made >= self.max_requests:
            raise BlockedError(f'已達本次請求上限 {self.max_requests}，停止（可用 --max-requests 調整）')
        self._wait()
        self.requests_made += 1
        try:
            return self.session.get(url, timeout=20)
        finally:
            self._last_request = time.monotonic()

    def _cache_path(self, url):
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + '.html')

    def get(self, url, use_cache=False):
        """回傳 (最終網址, HTML)。use_cache=True 時優先讀本機快取。"""
        if use_cache and self.cache_dir and not self.refresh:
            path = self._cache_path(url)
            fresh = path.exists() and (self.max_age is None
                                       or time.time() - path.stat().st_mtime < self.max_age)
            if fresh:
                return url, path.read_text(encoding='utf-8')

        r = self._fetch(url)
        r.encoding = r.apparent_encoding if r.encoding in (None, 'ISO-8859-1') else r.encoding
        if use_cache and self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(url).write_text(r.text, encoding='utf-8')
        return r.url, r.text

    def get_bytes(self, url):
        """下載圖片等二進位檔。圖片網址帶版本號，內容不會變，一律走快取（--refresh 也不重抓）。"""
        path = None
        if self.cache_dir:
            path = self.cache_dir / 'img' / hashlib.sha1(url.encode()).hexdigest()
            if path.exists():
                return path.read_bytes()
        data = self._fetch(url).content
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return data

    def _fetch(self, url):
        """實際發出請求：檢查 robots.txt、重試、退避；遇到 403 就停。"""
        if self.robots is not None and not self.robots.can_fetch(USER_AGENT, url):
            raise PermissionError(f'robots.txt 不允許抓取：{url}')

        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._request(url)
            except requests.RequestException as e:
                log.warning('連線失敗（第 %d 次）%s：%s', attempt, url, e)
                time.sleep(self.delay * 2 ** attempt)
                continue

            if r.status_code == 403:
                raise BlockedError(f'收到 403（可能被封鎖），立即停止：{url}')
            if r.status_code in (429, 503):
                wait = _retry_after(r) or self.delay * 2 ** (attempt + 1)
                log.warning('收到 %d，等待 %.0f 秒後重試：%s', r.status_code, wait, url)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r

        raise TemporaryError(f'重試 {self.max_retries} 次仍失敗：{url}')


def _retry_after(response):
    value = response.headers.get('Retry-After', '')
    return float(value) if value.isdigit() else None


# ---------- 解析 ----------

def _text(node):
    return node.get_text(' ', strip=True) if node else ''


def _product_links(anchors, page_url):
    """回傳 [(link, img, 品名, 額外欄位)]；品名取自圖片 alt，可在抓產品頁前先篩選。"""
    products = []
    for a in anchors:
        link = a.get('href')
        if not link:
            continue
        img_tag = a.find('img')
        img = (img_tag.get('src') or img_tag.get('data-src')) if img_tag else ''
        name = (img_tag.get('alt') or '').strip() if img_tag else ''
        products.append((urljoin(page_url, link), urljoin(page_url, img) if img else '', name, {}))
    return products


def parse_category_page(html, page_url):
    """分類頁：回傳 (產品清單, 最後頁碼或 None)。"""
    soup = BeautifulSoup(html, 'html.parser')
    page_numbers = [int(a.text) for a in soup.select('.yahoo a') if a.text.strip().isdigit()]
    return (_product_links(soup.select('.main_img > a'), page_url),
            max(page_numbers) if page_numbers else None)


def _in_stock(sizes):
    """[(尺寸, 庫存數)] → 有庫存的尺寸，以「/」串接。"""
    return '/'.join(size for size, qty in sizes if int(qty or 0) > 0)


def parse_promotion_products(html):
    """活動頁內嵌的 Vue 資料 promotionProducts：每個顏色一筆，含各尺寸庫存與活動價。"""
    i = html.find('promotionProducts:')
    if i < 0:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[html.index('[', i):])
    except ValueError:
        return []
    products = []
    for p in data:
        img = (p.get('image400') or {}).get('file_name', '')
        products.append((f"{BASE_URL}/product/{p['id']}", img, p.get('name', ''), {
            'color': p.get('color', ''),
            'sizes': _in_stock((s.get('size', ''), s.get('quantity')) for s in p.get('sizes', [])),
            'promo_price': str(p.get('promotion_price', '')),
            'original_price': str(p.get('price', '')),
        }))
    return products


def parse_promotion_page(html, page_url):
    """活動頁：回傳 (產品清單, 總頁數或 None, 活動名稱)。總頁數寫在頁面內的 Vue 資料裡。"""
    soup = BeautifulSoup(html, 'html.parser')
    m = re.search(r'"pageCount"\s*:\s*(\d+)', html)
    title = ' '.join(' '.join(_text(soup.select_one(sel)) for sel in
                              ('.saleGroup_title_name', '.saleGroup_title_price')).split())
    products = (parse_promotion_products(html)
                or _product_links(soup.select('a.hover-box[href*="/product/"]'), page_url))
    return products, int(m.group(1)) if m else None, title


def parse_origin(soup):
    for node in soup.select('div.html_block_detail p, div.html_block_detail span, div.html_block_detail li'):
        m = re.search(r'產\s*地\s*[:：]?\s*([^\s/|，,]+)', node.get_text(' ', strip=True))
        if m:
            return m.group(1)
    m = re.search(r'產\s*地\s*[:：]?\s*([^\s/|，,<]+)', soup.get_text(' ', strip=True))
    return m.group(1) if m else UNKNOWN_ORIGIN


def parse_product_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    name = _text(soup.select_one('div.product_detail_Right_title'))
    price = _text(soup.select_one('div.product_priceR_real > b'))
    original_price = _text(soup.select_one('div.product_priceR_original'))
    size_links = soup.select('.product_size a[quantity]')
    main_img = soup.select_one('#PRODUCT_IMAGE_MAIN')
    return {
        'name': name, 'price': price, 'original_price': original_price or price,
        'origin': parse_origin(soup),
        'color': _text(soup.select_one('.product_color_block.color_active .product_color_tag')),
        # 頁面沒有尺寸區塊時為 None（未知），有區塊但全部沒貨時為 ''
        'sizes': _in_stock((_text(a), a['quantity']) for a in size_links) if size_links else None,
        'main_img': main_img.get('src', '') if main_img else '',
        # 同款其他顏色的產品頁（--all-colors 用）
        'color_links': [f"{BASE_URL}/product/{img['product_id']}"
                        for img in soup.select('.product_color_block:not(.color_active) img[product_id]')],
    }


# ---------- 流程 ----------

def parse_target(target):
    """'1662'、'category/1662'、'promotion/658' 或完整網址 → ('category'|'promotion', 代號)。"""
    m = re.search(r'(category|promotion)(?:/|\?id=)(\d+)', target)
    if m:
        return m.group(1), m.group(2)
    if target.isdigit():
        return 'category', target
    raise ValueError(f'看不懂的目標：{target}（請給分類代號、category/代號、promotion/代號或網址）')


def page_url(kind, target_id, page=1):
    if kind == 'promotion':
        return (f'{BASE_URL}/promotion/{target_id}' if page == 1
                else f'{BASE_URL}/promotion?id={target_id}&page={page}')
    return f'{BASE_URL}/category/{target_id}' + (f'/{page}' if page > 1 else '')


def parse_listing(kind, html, url):
    if kind == 'promotion':
        return parse_promotion_page(html, url)
    return (*parse_category_page(html, url), '')


def collect_product_links(client, kind, target_id, max_pages):
    """回傳 ([(link, img, 品名)], 活動名稱)。"""
    links = {}
    first_url, html = client.get(page_url(kind, target_id))
    if urlparse(first_url).path in ('', '/'):
        log.warning('%s/%s 被導回首頁，可能已不存在', kind, target_id)
        return [], ''
    products, last_page, title = parse_listing(kind, html, first_url)
    links.update({p[0]: p for p in products})
    last_page = min(last_page or 1, max_pages)
    log.info('%s/%s %s：共 %d 頁', kind, target_id, title, last_page)

    for page in range(2, last_page + 1):
        url, html = client.get(page_url(kind, target_id, page))
        products = parse_listing(kind, html, url)[0]
        new = {p[0]: p for p in products if p[0] not in links}
        if not new:
            break
        links.update(new)
        log.info('  第 %d 頁：+%d 件', page, len(new))
    return list(links.values()), title


def name_matches(name, name_keywords):
    return not name_keywords or any(k.lower() in name.lower() for k in name_keywords)


def matches(item, name_keywords, exclude_origins):
    if not name_matches(item['name'], name_keywords):
        return False
    if exclude_origins and any(x.lower() in item['origin'].lower() for x in exclude_origins):
        return False
    return True


def crawl(client, targets, max_pages, name_keywords, exclude_origins, rows,
          in_stock_only=False, all_colors=False, progress=None):
    """結果直接 append 到 rows，中途被擋時已抓到的資料不會遺失。

    progress(已處理件數, 目前已知總件數) 會在每件商品處理後呼叫（總件數會隨著擴充顏色變多）。

    all_colors=True 時，分類頁的商品會再抓同款其他顏色（分類頁通常每款只列一個顏色）。
    活動頁只列有參加活動的顏色，不會擴充。
    """
    seen = set()
    parsed = sorted((parse_target(t) for t in targets), key=lambda t: t[0] != 'promotion')
    for kind, target_id in parsed:  # 活動頁先處理，同一商品優先保留活動價與即時庫存
        products, promo = collect_product_links(client, kind, target_id, max_pages)
        queue = list(products)
        for i, (link, img, list_name, extra) in enumerate(queue):  # 迴圈中可能再加入其他顏色
            if progress and i:
                progress(i, len(queue))
            if link in seen:
                continue
            seen.add(link)
            if list_name and not name_matches(list_name, name_keywords):
                continue  # 列表上的品名就不符合，不必抓產品頁
            try:
                _, html = client.get(link, use_cache=True)
            except requests.HTTPError as e:  # 商品下架（404 等）：跳過這件，不中斷整批
                log.warning('商品頁讀取失敗（%s），略過：%s', e, link)
                continue
            item = parse_product_page(html)
            if not item['name']:
                log.warning('解析不到品名（網站版面可能改了）：%s', link)
                continue
            if all_colors and kind == 'category' and matches(item, [], exclude_origins):
                queue.extend((c, '', item['name'], {}) for c in item['color_links'] if c not in seen)
            if not img and item['main_img']:
                img = urljoin(link, item['main_img'])
            item.update(extra)  # 活動頁資料是當下的庫存與價格，比快取的產品頁新
            if extra.get('promo_price'):
                item['price'] = extra['promo_price']
            item.update(source=f'{kind}/{target_id}', promo=promo, link=link, img=img)
            if in_stock_only and item['sizes'] == '':
                continue
            if matches(item, name_keywords, exclude_origins):
                rows.append(item)
                log.info('  ✓ %s｜%s｜%s｜%s', item['name'], item['color'], item['sizes'], item['origin'])
        if progress:
            progress(len(queue), len(queue))


def discover(client, keywords):
    _, html = client.get(BASE_URL + '/')
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.select('a[href*="/category/"]'):
        text = a.get_text(' ', strip=True)
        if not keywords or any(k.lower() in text.lower() for k in keywords):
            found[urljoin(BASE_URL, a['href'])] = text
    return found


def write_csv(rows, path):
    # price：商品頁顯示的售價（有活動時是活動價）；original_price：原價；promo_price：活動頁標示的活動價
    fields = ['name', 'color', 'sizes', 'price', 'original_price', 'promo_price', 'origin', 'promo', 'source', 'link', 'img']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:  # utf-8-sig：Excel 開啟不亂碼
        w = csv.DictWriter(f, fieldnames=fields, restval='', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description='NET 購物網站禮貌爬蟲')
    def common(parser, suppress=False):
        # 子指令也接受這些選項（放在 crawl 前後都可以）；子指令沒給時不覆蓋前面的值
        default = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument('--delay', type=float, default=default(3.0), help='每個請求最少間隔秒數（預設 3）')
        parser.add_argument('--jitter', type=float, default=default(3.0), help='額外隨機間隔上限秒數（預設 3）')
        parser.add_argument('--max-requests', type=int, default=default(300), help='本次執行請求上限（預設 300）')
        parser.add_argument('--cache-dir', default=default('cache'), help='產品頁快取資料夾')
        parser.add_argument('--refresh', action='store_true', default=default(False),
                            help='不讀快取，重抓產品頁（要最新庫存時用）')
        parser.add_argument('--max-age', type=float, default=default(None), metavar='HOURS',
                            help='只沿用 N 小時內的快取，較舊的重抓（例如 6）')

    common(p)
    sub = p.add_subparsers(dest='cmd', required=True)

    d = sub.add_parser('discover', help='從首頁列出分類代號')
    common(d, suppress=True)
    d.add_argument('--keyword', nargs='*', default=[], help='分類名稱關鍵字，例如 嬰 寶寶 童')

    c = sub.add_parser('crawl', help='爬指定分類或活動頁的產品')
    common(c, suppress=True)
    c.add_argument('targets', nargs='+',
                   help='分類代號（1662）、promotion/658，或直接貼分類／活動頁網址')
    c.add_argument('--max-pages', type=int, default=20)
    c.add_argument('--name-keyword', nargs='*', default=[], help='品名需包含任一關鍵字，例如 褲')
    c.add_argument('--exclude-origin', nargs='*', default=[], help='排除的產地，例如 中國 大陸 China')
    c.add_argument('--in-stock-only', action='store_true', help='只保留還有尺寸有庫存的商品')
    c.add_argument('--all-colors', action='store_true',
                   help='分類頁的商品也抓同款其他顏色（每個顏色多一個請求）')
    c.add_argument('-o', '--output', default='net_products.csv')

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    client = PoliteClient(delay=args.delay, jitter=args.jitter,
                          max_requests=args.max_requests, cache_dir=args.cache_dir,
                          refresh=args.refresh, max_age_hours=args.max_age)
    if args.cmd == 'discover':
        try:
            for url, text in discover(client, args.keyword).items():
                print(f'{url}\t{text}')
        except (BlockedError, TemporaryError) as e:
            log.error(str(e))
            return 2
        return 0

    rows, status = [], 0
    try:
        crawl(client, args.targets, args.max_pages,
              args.name_keyword, args.exclude_origin, rows, args.in_stock_only, args.all_colors)
    except (BlockedError, TemporaryError) as e:
        log.error('%s（已抓到的 %d 筆仍會寫出，快取保留，之後重跑可接續）', e, len(rows))
        status = 2
    write_csv(rows, args.output)
    log.info('寫入 %d 筆到 %s（本次共發出 %d 個請求）', len(rows), args.output, client.requests_made)
    return status


if __name__ == '__main__':
    sys.exit(main())
