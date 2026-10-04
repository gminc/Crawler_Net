"""NET 購物網站產品爬蟲（禮貌版）

爬取分類頁下的產品資訊（品名、價格、產地、連結、圖片），輸出 CSV。

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
"""

import argparse
import csv
import hashlib
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
    """對方開始拒絕我們（403 / 連續失敗），應立即停止。"""


class PoliteClient:
    def __init__(self, delay=3.0, jitter=3.0, max_requests=300,
                 cache_dir='cache', respect_robots=True, max_retries=3):
        self.delay = delay
        self.jitter = jitter
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
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
        if use_cache and self.cache_dir:
            path = self._cache_path(url)
            if path.exists():
                return url, path.read_text(encoding='utf-8')

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
            r.encoding = r.apparent_encoding if r.encoding in (None, 'ISO-8859-1') else r.encoding

            if use_cache and self.cache_dir:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._cache_path(url).write_text(r.text, encoding='utf-8')
            return r.url, r.text

        raise BlockedError(f'重試 {self.max_retries} 次仍失敗，停止：{url}')


def _retry_after(response):
    value = response.headers.get('Retry-After', '')
    return float(value) if value.isdigit() else None


# ---------- 解析 ----------

def _text(node):
    return node.get_text(' ', strip=True) if node else ''


def parse_category_page(html, page_url):
    """回傳 (產品清單 [(link, img)], 最後頁碼或 None)。"""
    soup = BeautifulSoup(html, 'html.parser')
    products = []
    for a in soup.select('.main_img > a'):
        link = a.get('href')
        if not link:
            continue
        img_tag = a.find('img')
        img = (img_tag.get('src') or img_tag.get('data-src')) if img_tag else ''
        products.append((urljoin(page_url, link), urljoin(page_url, img) if img else ''))

    page_numbers = [int(a.text) for a in soup.select('.yahoo a') if a.text.strip().isdigit()]
    return products, (max(page_numbers) if page_numbers else None)


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
    return {'name': name, 'price': price, 'origin': parse_origin(soup)}


# ---------- 流程 ----------

def category_url(category, page=1):
    return f'{BASE_URL}/category/{category}' + (f'/{page}' if page > 1 else '')


def collect_product_links(client, category, max_pages):
    links = {}
    first_url, html = client.get(category_url(category))
    if urlparse(first_url).path in ('', '/'):
        log.warning('分類 %s 被導回首頁，可能已不存在', category)
        return []
    products, last_page = parse_category_page(html, first_url)
    links.update(dict(products))
    last_page = min(last_page or 1, max_pages)
    log.info('分類 %s：共 %d 頁', category, last_page)

    for page in range(2, last_page + 1):
        url, html = client.get(category_url(category, page))
        products, _ = parse_category_page(html, url)
        new = {k: v for k, v in products if k not in links}
        if not new:
            break
        links.update(new)
        log.info('  第 %d 頁：+%d 件', page, len(new))
    return list(links.items())


def matches(item, name_keywords, exclude_origins):
    if name_keywords and not any(k.lower() in item['name'].lower() for k in name_keywords):
        return False
    if exclude_origins and any(x.lower() in item['origin'].lower() for x in exclude_origins):
        return False
    return True


def crawl(client, categories, max_pages, name_keywords, exclude_origins, rows):
    """結果直接 append 到 rows，中途被擋時已抓到的資料不會遺失。"""
    seen = set()
    for category in categories:
        for link, img in collect_product_links(client, category, max_pages):
            if link in seen:
                continue
            seen.add(link)
            _, html = client.get(link, use_cache=True)
            item = parse_product_page(html)
            if not item['name']:
                log.warning('解析不到品名（網站版面可能改了）：%s', link)
                continue
            item.update(category=category, link=link, img=img)
            if matches(item, name_keywords, exclude_origins):
                rows.append(item)
                log.info('  ✓ %s｜%s｜%s', item['name'], item['price'], item['origin'])


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
    fields = ['name', 'price', 'origin', 'category', 'link', 'img']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:  # utf-8-sig：Excel 開啟不亂碼
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    p = argparse.ArgumentParser(description='NET 購物網站禮貌爬蟲')
    p.add_argument('--delay', type=float, default=3.0, help='每個請求最少間隔秒數（預設 3）')
    p.add_argument('--jitter', type=float, default=3.0, help='額外隨機間隔上限秒數（預設 3）')
    p.add_argument('--max-requests', type=int, default=300, help='本次執行請求上限（預設 300）')
    p.add_argument('--cache-dir', default='cache', help='產品頁快取資料夾')
    sub = p.add_subparsers(dest='cmd', required=True)

    d = sub.add_parser('discover', help='從首頁列出分類代號')
    d.add_argument('--keyword', nargs='*', default=[], help='分類名稱關鍵字，例如 嬰 寶寶 童')

    c = sub.add_parser('crawl', help='爬指定分類的產品')
    c.add_argument('categories', nargs='+', help='分類代號，例如 1662')
    c.add_argument('--max-pages', type=int, default=20)
    c.add_argument('--name-keyword', nargs='*', default=[], help='品名需包含任一關鍵字，例如 褲')
    c.add_argument('--exclude-origin', nargs='*', default=[], help='排除的產地，例如 中國 大陸 China')
    c.add_argument('-o', '--output', default='net_products.csv')

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    client = PoliteClient(delay=args.delay, jitter=args.jitter,
                          max_requests=args.max_requests, cache_dir=args.cache_dir)
    if args.cmd == 'discover':
        try:
            for url, text in discover(client, args.keyword).items():
                print(f'{url}\t{text}')
        except BlockedError as e:
            log.error(str(e))
            return 2
        return 0

    rows, status = [], 0
    try:
        crawl(client, args.categories, args.max_pages,
              args.name_keyword, args.exclude_origin, rows)
    except BlockedError as e:
        log.error('%s（已抓到的 %d 筆仍會寫出，快取保留，之後重跑可接續）', e, len(rows))
        status = 2
    write_csv(rows, args.output)
    log.info('寫入 %d 筆到 %s（本次共發出 %d 個請求）', len(rows), args.output, client.requests_made)
    return status


if __name__ == '__main__':
    sys.exit(main())
