"""把 net_crawler.py 產生的 CSV 做成圖片清單網頁

產出單一 HTML 檔：圖片內嵌、不需網路也能開，可依分類／尺寸／產地篩選，
並有購物清單（點尺寸加入、估算活動價總額、湊件提醒、複製清單）。

用法：
    python net_crawler.py crawl promotion/658 --exclude-origin 中國 大陸 China --in-stock-only -o clearance.csv
    python net_gallery.py clearance.csv -o clearance.html

    # 分類頁建議加 --all-colors，才會有同款的所有顏色
    python net_crawler.py crawl 1822 1824 --all-colors --in-stock-only --refresh -o baby_bottoms.csv
    python net_gallery.py baby_bottoms.csv --title 嬰兒下身類
"""

import argparse
import base64
import csv
import html
import io
import json
import logging
import os
import re
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

import net_crawler as nc

TEMPLATE = Path(__file__).with_name('gallery_template.html')
CATEGORY_RULES = [('童裝', ['BABY', '童']), ('褲・裙', ['褲', '裙']), ('洋裝', ['洋裝']),
                  ('外套', ['外套', '夾克']), ('配件', ['襪', '皮帶', '帽', '包'])]

log = logging.getLogger('net_gallery')


def guess_category(name):
    upper = name.upper()
    for category, words in CATEGORY_RULES:
        if any(w in upper for w in words):
            return category
    return '上衣'


def _int(value):
    digits = re.sub(r'[^\d.]', '', value or '')
    return int(float(digits)) if digits else 0


def split_color(color):
    """'720暗夜藍' → ('720', '暗夜藍')。"""
    m = re.match(r'(\d{2,4})\s*(.*)', color or '')
    return (m.group(1), m.group(2) or color) if m else ('', color or '')


def build_styles(rows):
    """CSV 列 → 依品名分組的款式，每款底下是各顏色。"""
    by_link = OrderedDict()
    for r in rows:  # 同一商品出現在多份 CSV 時只留一筆，優先保留有活動資料的
        if r['link'] not in by_link or (r.get('promo') and not by_link[r['link']].get('promo')):
            by_link[r['link']] = r
    styles = OrderedDict()
    for r in by_link.values():
        style = styles.setdefault(r['name'], {'name': r['name'], 'cat': guess_category(r['name']),
                                              'colors': []})
        code, color = split_color(r.get('color', ''))
        price = _int(r.get('price'))
        style['colors'].append({
            'code': code, 'color': color,
            'sizes': [s for s in (r.get('sizes') or '').split('/') if s],
            'price': price, 'orig': _int(r.get('original_price')) or price,
            'origin': (r.get('origin') or '').replace('製', ''),
            'promo': r.get('promo', ''), 'source': r.get('source', ''),
            'link': r['link'], 'img': r.get('img', ''),
        })
    return list(styles.values())


def to_data_uri(data, thumb_width):
    try:
        from PIL import Image
    except ImportError:  # 沒裝 Pillow 就直接內嵌原圖，檔案會大一些
        kinds = {b'\xff\xd8\xff': 'jpeg', b'\x89PNG': 'png', b'RIFF': 'webp', b'GIF8': 'gif'}
        kind = next((k for magic, k in kinds.items() if data.startswith(magic)), None)
        if not kind:
            raise ValueError('不是圖片檔')
        return f'data:image/{kind};base64,' + base64.b64encode(data).decode()
    im = Image.open(io.BytesIO(data)).convert('RGB')
    im.thumbnail((thumb_width, thumb_width * 4 // 3))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=78, optimize=True)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def embed_images(client, styles, thumb_width):
    for style in styles:
        for c in style['colors']:
            if c['img'].startswith('http'):
                try:
                    c['img'] = to_data_uri(client.get_bytes(c['img']), thumb_width)
                except nc.BlockedError:
                    raise
                except Exception as e:  # 單張圖失敗不影響整頁
                    log.warning('圖片下載失敗（%s）：%s', e, c['img'])
                    c['img'] = ''


DOCUMENT = ('<!doctype html>\n<html lang="zh-Hant">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '</head>\n<body>\n{}\n</body>\n</html>\n')


def render(styles, title, stamp, template=TEMPLATE, fragment=False):
    """fragment=True 時只輸出內容片段（給會自己包 <html> 的平台用，例如 Claude Artifact）。"""
    sources = sorted({c['source'] for s in styles for c in s['colors'] if c['source']})
    origins = Counter(c['origin'] for s in styles for c in s['colors'])
    meta = {
        'sources': [{'label': src, 'url': f"{nc.BASE_URL}/{src}"} for src in sources],
        'origins': origins.most_common(),
        'promos': sorted({c['promo'] for s in styles for c in s['colors'] if c['promo']}),
    }
    page = template.read_text(encoding='utf-8')
    # 內嵌在 <script> 裡：把 < 轉義，避免 </script> 或 <!-- 提早結束程式碼
    dump = lambda obj: json.dumps(obj, ensure_ascii=False).replace('<', '\\u003c')
    page = (page.replace('/*DATA*/[]', dump(styles))
                .replace('/*META*/{}', dump(meta))
                .replace('{{TITLE}}', html.escape(title))
                .replace('{{STAMP}}', html.escape(stamp)))
    return page if fragment else DOCUMENT.format(page)


def default_title(rows):
    promos = {r.get('promo') or '' for r in rows}
    if len(promos) == 1 and '' not in promos:  # 全部都是同一個活動
        return promos.pop()
    return 'NET 商品清單'


def main(argv=None):
    p = argparse.ArgumentParser(description='把爬蟲 CSV 做成圖片清單網頁')
    p.add_argument('csv', nargs='+', help='net_crawler.py 輸出的 CSV（可多個）')
    p.add_argument('-o', '--output', default='gallery.html')
    p.add_argument('--title', help='網頁標題（預設用活動名稱）')
    p.add_argument('--thumb-width', type=int, default=360, help='內嵌圖片寬度（需要 Pillow）')
    p.add_argument('--delay', type=float, default=1.5, help='下載圖片的間隔秒數（預設 1.5）')
    p.add_argument('--jitter', type=float, default=1.0)
    p.add_argument('--max-requests', type=int, default=600)
    p.add_argument('--cache-dir', default='cache')
    p.add_argument('--fragment', action='store_true', help='只輸出內容片段，不含 <html>/<head>')
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')

    rows = []
    for path in args.csv:
        with open(path, encoding='utf-8-sig') as f:
            rows.extend(csv.DictReader(f))
    if not rows:
        log.error('CSV 沒有資料')
        return 1
    newest = max(os.path.getmtime(path) for path in args.csv)
    stamp = datetime.fromtimestamp(newest).strftime('%Y-%m-%d %H:%M')

    styles = build_styles(rows)
    client = nc.PoliteClient(delay=args.delay, jitter=args.jitter,
                             max_requests=args.max_requests, cache_dir=args.cache_dir)
    try:
        embed_images(client, styles, args.thumb_width)
    except nc.BlockedError as e:
        log.error('%s（沒下載到的圖片會留白）', e)
        for style in styles:
            for c in style['colors']:
                if not c['img'].startswith('data:'):
                    c['img'] = ''

    page = render(styles, args.title or default_title(rows), stamp, fragment=args.fragment)
    Path(args.output).write_text(page, encoding='utf-8')
    log.info('寫入 %s：%d 款、%d 個顏色、%d KB', args.output, len(styles),
             sum(len(s['colors']) for s in styles), len(page.encode()) // 1024)
    return 0


if __name__ == '__main__':
    sys.exit(main())
