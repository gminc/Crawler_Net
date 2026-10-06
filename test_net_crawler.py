"""離線測試：用假的 HTML 與假的 Session，不會真的連到網站。"""

import pytest

import net_crawler as nc

CATEGORY_HTML = """
<div class="main_img"><a href="/product/1"><img src="/img/1.jpg"></a></div>
<div class="main_img"><a href="/product/2"><img data-src="/img/2.jpg"></a></div>
<div class="main_img"><a href="/product/3"><img src="/img/3.jpg"></a></div>
<div class="yahoo"><a>1</a><a href="/category/9/2">2</a><a>下一頁</a></div>
"""
CATEGORY_PAGE2_HTML = """
<div class="main_img"><a href="/product/3"><img src="/img/3.jpg"></a></div>
"""


def product_html(name, price, origin_line):
    return f"""
    <div class="product_detail_Right_title"> {name} </div>
    <div class="product_priceR_real"><b>{price}</b></div>
    <div class="html_block_detail"><p><span>材質 棉100%</span><span>{origin_line}</span></p></div>
    """


PAGES = {
    '/robots.txt': (200, 'User-agent: *\nDisallow: /member/\n'),
    '/category/9': (200, CATEGORY_HTML),
    '/category/9/2': (200, CATEGORY_PAGE2_HTML),
    '/product/1': (200, product_html('嬰幼兒針織長褲', '$199', '產地 台灣')),
    '/product/2': (200, product_html('嬰幼兒內搭褲', '$150', '產地：中國')),
    '/product/3': (200, product_html('嬰幼兒包屁衣', '$250', '產地 越南')),
}


class FakeResponse:
    def __init__(self, url, status, text):
        self.url, self.status_code, self.text = url, status, text
        self.headers, self.encoding = {}, 'utf-8'
        self.ok = status < 400

    def raise_for_status(self):
        if not self.ok:
            raise nc.requests.HTTPError(self.status_code)


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls, self.headers = pages, [], {}

    def get(self, url, timeout=None):
        path = url.replace(nc.BASE_URL, '')
        self.calls.append(path)
        status, text = self.pages.get(path, (404, ''))
        return FakeResponse(url, status, text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(nc.requests, 'Session', lambda: FakeSession(PAGES))
    monkeypatch.setattr(nc.time, 'sleep', lambda s: None)
    return nc.PoliteClient(delay=0, jitter=0, cache_dir=tmp_path)


def test_exclude_china_and_keep_pants(client):
    rows = []
    nc.crawl(client, ['9'], 20, ['褲'], ['中國', '大陸', 'China'], rows)
    assert [r['name'] for r in rows] == ['嬰幼兒針織長褲']
    assert rows[0]['origin'] == '台灣'
    assert rows[0]['img'].endswith('/img/1.jpg')


def test_each_product_fetched_once_and_cached(client):
    nc.crawl(client, ['9'], 20, [], [], [])
    calls = client.session.calls
    assert calls.count('/product/3') == 1  # 跨頁重複的產品只抓一次
    before = len(calls)
    nc.crawl(client, ['9'], 20, [], [], [])
    product_calls = [c for c in calls[before:] if c.startswith('/product/')]
    assert product_calls == []  # 第二次全部走快取


def test_403_stops_immediately_and_keeps_partial(client):
    client.session.pages = {**PAGES, '/product/2': (403, '')}
    rows = []
    with pytest.raises(nc.BlockedError):
        nc.crawl(client, ['9'], 20, [], [], rows)
    assert [r['name'] for r in rows] == ['嬰幼兒針織長褲']
    assert '/product/3' not in client.session.calls


def test_robots_disallow(client):
    with pytest.raises(PermissionError):
        client.get(nc.BASE_URL + '/member/login')


def test_robots_redirected_to_home_means_no_rules(tmp_path, monkeypatch):
    session = FakeSession({**PAGES, '/robots.txt': (200, 'Disallow: /')})
    original_get = session.get

    def redirecting_get(url, timeout=None):
        r = original_get(url, timeout)
        if url.endswith('/robots.txt'):
            r.url = nc.BASE_URL + '/'  # 模擬被 302 導回首頁
        return r

    session.get = redirecting_get
    monkeypatch.setattr(nc.requests, 'Session', lambda: session)
    client = nc.PoliteClient(delay=0, jitter=0, cache_dir=tmp_path)
    client.get(nc.BASE_URL + '/category/9')  # 不應被當成 Disallow


def test_request_cap(client):
    client.max_requests = client.requests_made + 1
    client.get(nc.BASE_URL + '/category/9')
    with pytest.raises(nc.BlockedError):
        client.get(nc.BASE_URL + '/category/9/2')


def test_origin_missing_is_marked():
    item = nc.parse_product_page('<div class="product_detail_Right_title">褲</div>')
    assert item['origin'] == nc.UNKNOWN_ORIGIN


PROMO_HTML = """
<div class="saleGroup_title"><span class="saleGroup_title_name">嬰幼兒夏日內搭褲</span>
<span class="saleGroup_title_price">
任選 3件
249
</span></div>
<td><a class="hover-box" href="https://www.net-fashion.net/product/1"><img alt="嬰幼兒針織長褲" src="/img/1.jpg"></a></td>
<td><a class="hover-box" href="https://www.net-fashion.net/product/3"><img alt="嬰幼兒包屁衣" src="/img/3.jpg"></a></td>
<script>var app = {data: {pagination: {"previous":null,"current":1,"pageCount":2,"total":3,"next":2}}}</script>
"""
PROMO_PAGE2_HTML = """
<td><a class="hover-box" href="https://www.net-fashion.net/product/2"><img alt="嬰幼兒內搭褲" src="/img/2.jpg"></a></td>
"""


@pytest.mark.parametrize('target, expected', [
    ('1662', ('category', '1662')),
    ('promotion/658', ('promotion', '658')),
    ('https://www.net-fashion.net/promotion/1490', ('promotion', '1490')),
    ('https://www.net-fashion.net/promotion?id=658&page=2', ('promotion', '658')),
    ('https://www.net-fashion.net/category/2451/3', ('category', '2451')),
])
def test_parse_target(target, expected):
    assert nc.parse_target(target) == expected


def test_promotion_pages_and_name_prefilter(client):
    client.session.pages = {**PAGES, '/promotion/7': (200, PROMO_HTML),
                            '/promotion?id=7&page=2': (200, PROMO_PAGE2_HTML)}
    rows = []
    nc.crawl(client, ['promotion/7'], 20, ['褲'], ['中國'], rows)
    assert [r['name'] for r in rows] == ['嬰幼兒針織長褲']
    assert rows[0]['promo'] == '嬰幼兒夏日內搭褲 任選 3件 249'
    assert rows[0]['source'] == 'promotion/7'
    assert '/promotion?id=7&page=2' in client.session.calls  # 有翻到第 2 頁
    assert '/product/2' in client.session.calls               # 第 2 頁的褲子有抓（再被產地排除）
    assert '/product/3' not in client.session.calls           # 包屁衣在列表就被篩掉，不抓產品頁


def test_product_page_sizes_and_color():
    html = product_html('長褲', '$299', '產地 越南製') + """
    <div class="product_color_block"><div class="product_color_tag">719深藍</div></div>
    <div class="product_color_block color_active"><div class="product_color_tag">720暗夜藍</div></div>
    <div class="product_size">
      <a id="size_32" quantity="58"><div class="product_size_block">32</div></a>
      <a id="size_34" quantity="3"><div class="product_size_block">34</div></a>
      <a id="size_36" quantity="0"><div class="product_size_block_none">36</div></a>
      <a id="size_38" quantity="-1"><div class="product_size_block_none">38</div></a>
    </div>"""
    item = nc.parse_product_page(html)
    assert item['color'] == '720暗夜藍'
    assert item['sizes'] == '32/34'
    assert item['original_price'] == '$299'  # 沒有原價區塊時等於售價
    assert nc.parse_product_page(product_html('皮帶', '$149', '產地 台灣製'))['sizes'] is None


PROMO_JSON_HTML = """
<div class="saleGroup_title"><span class="saleGroup_title_name">零碼出清</span>
<span class="saleGroup_title_price">任選 3件 5折</span></div>
<script>var v = new Vue({data: {cartItems: [], promotionProducts: [
 {"id":1,"price":"199","name":"\\u5b30\\u5e7c\\u5152\\u91dd\\u7e54\\u9577\\u8932","color":"002\\u767d\\u8272",
  "image400":{"file_name":"https://img/1.jpg"},"promotion_price":99,
  "sizes":[{"size":"S","quantity":"2"},{"size":"M","quantity":"0"},{"size":"L","quantity":9}]},
 {"id":3,"name":"\\u5b30\\u5e7c\\u5152\\u5305\\u5c41\\u8863","color":"900\\u9ed1\\u8272",
  "image400":{"file_name":"https://img/3.jpg"},"promotion_price":125,
  "sizes":[{"size":"F","quantity":"0"}]}
], pagination: {"previous":null,"current":1,"pageCount":1,"total":2,"next":null}}})</script>
"""


def test_promotion_json_stock_and_in_stock_only(client):
    client.session.pages = {**PAGES, '/promotion/8': (200, PROMO_JSON_HTML)}
    rows = []
    nc.crawl(client, ['promotion/8'], 20, [], [], rows, in_stock_only=True)
    assert len(rows) == 1  # 包屁衣全部沒貨，被排除
    r = rows[0]
    assert (r['name'], r['color'], r['sizes'], r['promo_price']) == ('嬰幼兒針織長褲', '002白色', 'S/L', '99')
    assert r['origin'] == '台灣' and r['img'] == 'https://img/1.jpg'
    assert r['original_price'] == '199'
    assert r['promo'] == '零碼出清 任選 3件 5折'
