"""本機版 net_app 的離線測試：用假的網站內容，不會真的連到 NET。"""

import json
import threading
import time
import urllib.request

import pytest

import net_app as na
import net_crawler as nc
from test_net_crawler import FakeSession

BASE = nc.BASE_URL


def product(name, origin, color, sizes, others=(), price='299', orig='599', pid='1'):
    """sizes: {'S': 2, 'M': 0}；others: 其他顏色的產品編號。"""
    blocks = ''.join(f'<div class="product_color_block"><img product_id="{o}"><div class="product_color_tag">x</div></div>'
                     for o in others)
    size_links = ''.join(f'<a quantity="{q}">{s}</a>' for s, q in sizes.items())
    return f"""
    <div class="product_detail_Right_title">{name}</div>
    <div class="product_priceR_real"><b>{price}</b></div><div class="product_priceR_original">{orig}</div>
    <div class="html_block_detail"><p><span>●商品產地 {origin}</span></p></div>
    <img id="PRODUCT_IMAGE_MAIN" src="/img/{pid}_main.jpg">
    <div class="product_color_block color_active"><img product_id="{pid}"><div class="product_color_tag">{color}</div></div>
    {blocks}<div class="product_size">{size_links}</div>"""


def listing(*items):
    return ''.join(f'<div class="main_img"><a href="{BASE}/product/{pid}"><img src="/img/{pid}.jpg" alt="{name}"></a></div>'
                   for pid, name in items)


SITE = {
    '/robots.txt': (200, ''),
    '/': (200, '<ul class="main_nav"><li><a href="https://www.net-fashion.net/category/2451">BABY 嬰兒</a></li>'
               '<li><a href="https://www.net-fashion.net/promotion/658">SALE 促銷</a></li></ul>'),
    # 分類 9：A 款台灣製（兩色，一色沒貨）、B 款中國製（有其他顏色，不該被抓）、C 款越南製全部沒貨
    '/category/9': (200, listing(('1', '長褲A'), ('3', '上衣B'), ('5', '短褲C')) + """
        <div class="silderbar_category"><div class="siderbar_sec"><ul>
          <li><a class="parent" href="https://www.net-fashion.net/category/1808"><b>Girl 女嬰</b></a>
            <ul><li><a href="https://www.net-fashion.net/category/1822">。下身類</a></li></ul></li>
          <li><a class="parent" href="https://www.net-fashion.net/category/1144"><b>精選優惠</b></a>
            <ul><li><a href="https://www.net-fashion.net/promotion/1490">嬰幼兒夏日內搭褲 ⦁ 任選 3件 249</a></li></ul></li>
        </ul></div></div>"""),
    '/product/1': (200, product('長褲A', '台灣製', '002白色', {'S': 2, 'M': 0}, others=['2'], pid='1')),
    '/product/2': (200, product('長褲A', '台灣製', '900黑色', {'S': 0}, others=['1'], pid='2')),
    '/product/3': (200, product('上衣B', '中國製', '001雪白', {'L': 5}, others=['4'], pid='3')),
    '/product/4': (200, product('上衣B', '中國製', '900黑色', {'L': 5}, pid='4')),
    '/product/5': (200, product('短褲C', '越南製', '102卡其', {'S': 0, 'M': -1}, pid='5')),
    # 活動 7：D 款有貨、E 款全部沒貨（不該抓商品頁）
    '/promotion/7': (200, """<div class="saleGroup_title"><span class="saleGroup_title_name">零碼出清</span>
        <span class="saleGroup_title_price">任選 3件 5折</span></div><script>var v={data:{promotionProducts:[
        {"id":11,"name":"背心D","color":"505淺黃","price":"199","promotion_price":99,
         "image400":{"file_name":"https://img/11.jpg"},"sizes":[{"size":"XL","quantity":"1"}]},
        {"id":12,"name":"洋裝E","color":"001雪白","price":"399","promotion_price":199,
         "image400":{"file_name":"https://img/12.jpg"},"sizes":[]}
        ], pagination:{"pageCount":1}}}</script>"""),
    '/product/11': (200, product('背心D', '柬埔寨製', '505淺黃', {'XL': 1}, pid='11')),
}


@pytest.fixture
def crawler(tmp_path, monkeypatch):
    session = FakeSession(dict(SITE))
    monkeypatch.setattr(nc.requests, 'Session', lambda: session)
    monkeypatch.setattr(nc.time, 'sleep', lambda s: None)
    client = nc.PoliteClient(delay=0, jitter=0, cache_dir=tmp_path / 'cache', max_age_hours=6)
    c = na.Crawler(client, na.OriginMemory(tmp_path / 'origins.sqlite3'))
    c.session = session
    return c


def wait_until(cond, timeout=5):
    """測試把 time.sleep 換成空函式了，這裡改用 Event.wait 真的等待。"""
    deadline, tick = time.monotonic() + timeout, threading.Event()
    while not cond() and time.monotonic() < deadline:
        tick.wait(0.01)
    return cond()


def run(crawler, kind, target_id):
    sec = na.Section(kind, target_id, 't')
    crawler.current = sec
    (crawler._crawl_promotion if kind == 'promotion' else crawler._crawl_category)(sec)
    crawler.current = None
    return sec


def test_category_shows_only_in_stock_non_china(crawler):
    sec = run(crawler, 'category', '9')
    assert [c['name'] for c in sec.cards] == ['長褲A']
    a = sec.cards[0]
    assert a['origin'] == '台灣' and a['cat'] == '褲・裙'
    assert [(c['code'], c['color'], c['sizes']) for c in a['colors']] == [('002', '白色', ['S'])]  # 黑色沒貨不出現
    assert a['colors'][0]['price'] == 299 and a['colors'][0]['orig'] == 599
    assert (sec.total, sec.checked, sec.excluded_origin, sec.excluded_stock) == (3, 3, 1, 1)
    calls = crawler.session.calls
    assert '/product/2' in calls        # 非中國製 → 會確認其他顏色
    assert '/product/4' not in calls    # 中國製 → 一發現就停，不抓其他顏色


def test_known_china_costs_zero_requests(crawler):
    run(crawler, 'category', '9')
    crawler.client.refresh = True       # 強迫重抓，看哪些頁面還會被請求
    before = len(crawler.session.calls)
    run(crawler, 'category', '9')
    again = crawler.session.calls[before:]
    assert '/product/3' not in again    # 已記住上衣B 是中國製
    assert '/product/1' in again


def test_promotion_skips_out_of_stock_without_requests(crawler):
    sec = run(crawler, 'promotion', '7')
    assert [c['name'] for c in sec.cards] == ['背心D']
    d = sec.cards[0]
    assert d['promo'] == '零碼出清 任選 3件 5折' and d['origin'] == '柬埔寨'
    assert (d['colors'][0]['price'], d['colors'][0]['orig'], d['colors'][0]['sizes']) == (99, 199, ['XL'])
    assert '/product/12' not in crawler.session.calls  # 沒庫存的不抓商品頁
    assert sec.excluded_stock == 1


def test_parse_menus():
    assert na.parse_main_nav(SITE['/'][1]) == [
        {'title': 'BABY 嬰兒', 'kind': 'category', 'id': '2451'},
        {'title': 'SALE 促銷', 'kind': 'promotion', 'id': '658'}]
    groups = na.parse_sidebar(SITE['/category/9'][1])
    assert [(g['title'], g['id']) for g in groups] == [('Girl 女嬰', '1808'), ('精選優惠', '1144')]
    assert groups[0]['children'] == [{'title': '下身類', 'kind': 'category', 'id': '1822'}]
    assert groups[1]['children'][0]['kind'] == 'promotion'


def test_blocked_stops_everything(crawler):
    crawler.session.pages['/product/1'] = (403, '')
    crawler.start()
    a = crawler.request('category', '9', 'A')
    b = crawler.request('promotion', '7', 'B')
    wait_until(lambda: a.status == 'error' and b.status == 'error')
    assert a.status == 'error' and b.status == 'error' and crawler.blocked
    assert crawler.request('category', '9').status == 'error'  # 被擋後不再排隊


@pytest.fixture
def server(crawler):
    crawler.start()
    app = na.App(crawler, na.DEFAULT_EXCLUDE)
    httpd = na.ThreadingHTTPServer(('127.0.0.1', 0), na.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


def call(url, method='GET', host=None, token=True):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header('X-Requested-With', 'net-app')
    if host:
        req.add_header('Host', host)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode()) if 'json' in r.headers['Content-Type'] else r.read().decode()


def test_http_flow(server):
    page = call(server + '/')
    assert '本機版' in page and '"exclude": ["中國", "大陸", "China"]' in page
    assert call(server + '/api/menu')['items'][0]['id'] == '2451'
    assert call(server + '/api/submenu/category/9')['groups'][0]['title'] == 'Girl 女嬰'
    assert call(server + '/api/section/category/9')['status'] == 'idle'   # 沒點之前不抓
    call(server + '/api/crawl/category/9?title=X', method='POST')
    wait_until(lambda: call(server + '/api/section/category/9')['status'] == 'done')
    d = call(server + '/api/section/category/9')
    assert d['status'] == 'done' and [c['name'] for c in d['cards']] == ['長褲A']
    assert call(server + '/api/section/category/9?from=1')['cards'] == []  # 增量讀取


def test_rejects_foreign_host(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server + '/api/menu', host='evil.example.com')
    assert e.value.code == 403


# ---------- 審查發現的問題（回歸測試） ----------

def test_missing_sibling_or_product_does_not_stop_section(crawler):
    del crawler.session.pages['/product/2']            # A 款的黑色下架（404）
    crawler.session.pages['/category/9'] = (200, listing(('6', '下架款'), ('1', '長褲A'), ('5', '短褲C')))
    sec = run(crawler, 'category', '9')                 # 下架款的商品頁也 404
    assert [c['name'] for c in sec.cards] == ['長褲A']
    assert (sec.checked, sec.failed, sec.excluded_stock) == (3, 1, 1)


def test_temporary_network_error_skips_without_blocking(crawler, monkeypatch):
    real_get = crawler.session.get

    def flaky(url, timeout=None):
        if url.endswith('/product/5'):
            raise nc.requests.ConnectionError('reset')
        return real_get(url, timeout)
    monkeypatch.setattr(crawler.session, 'get', flaky)
    sec = run(crawler, 'category', '9')
    assert sec.failed == 1 and not crawler.blocked


def test_blocked_by_menu_request_stops_worker_and_further_requests(crawler):
    app = na.App(crawler, na.DEFAULT_EXCLUDE)
    crawler.session.pages['/'] = (403, '')
    with pytest.raises(nc.BlockedError):
        app.menu()
    assert crawler.blocked
    before = len(crawler.session.calls)
    with pytest.raises(nc.BlockedError):
        app.submenu('category', '9')
    assert len(crawler.session.calls) == before        # 被擋之後不再發任何請求
    assert crawler.request('category', '9').status == 'error'


def test_refresh_refetches_product_pages(crawler):
    run(crawler, 'category', '9')
    crawler.session.pages['/product/1'] = (200, product('長褲A', '台灣製', '002白色', {'S': 0, 'M': 3},
                                                        others=['2'], pid='1'))
    sec = na.Section('category', '9', 't')
    sec.refresh = True
    crawler._crawl_category(sec)
    assert sec.cards[0]['colors'][0]['sizes'] == ['M']  # 不是快取裡舊的 S


def test_promotion_unparseable_product_is_not_shown(crawler):
    crawler.session.pages['/product/11'] = (200, '<html>維護中</html>')
    sec = run(crawler, 'promotion', '7')
    assert sec.cards == [] and sec.failed == 1


def test_empty_alt_names_are_separate_styles(crawler):
    crawler.session.pages['/category/9'] = (200, listing(('3', ''), ('1', '')))
    sec = run(crawler, 'category', '9')
    assert sec.total == 2 and [c['name'] for c in sec.cards] == ['長褲A']
    assert crawler.memory.get('') is None


def test_stale_done_section_is_recrawled(crawler):
    sec = crawler.request('category', '9')
    sec.status, sec.finished_at = 'done', time.time() - 7 * 3600   # 超過 6 小時
    crawler.queue.clear()
    assert crawler.request('category', '9').status == 'queued'
    sec.status, sec.finished_at = 'done', time.time()
    crawler.queue.clear()
    assert crawler.request('category', '9').status == 'done'       # 6 小時內直接用


def test_removed_category_and_broken_promotion_are_errors(crawler):
    crawler.session.pages['/category/9'] = (200, '')
    real_get = crawler.session.get

    def redirect_home(url, timeout=None):
        r = real_get(url, timeout)
        if url.endswith('/category/9'):
            r.url = BASE + '/'
        return r
    crawler.session.get = redirect_home
    with pytest.raises(RuntimeError, match='不存在'):
        run(crawler, 'category', '9')
    crawler.session.pages['/promotion/7'] = (200, '<div class="saleGroup_title"></div>'
                                              '<td><a class="hover-box" href="https://www.net-fashion.net/product/11">'
                                              '<img alt="背心D" src="x"></a></td>')
    with pytest.raises(RuntimeError, match='庫存資料'):
        run(crawler, 'promotion', '7')


def test_api_requires_app_header(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server + '/api/crawl/category/9', method='POST', token=False)
    assert e.value.code == 403
    assert '本機版' in call(server + '/', token=False)   # 網頁本身可以直接開
