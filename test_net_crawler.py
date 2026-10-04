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


def test_request_cap(client):
    client.max_requests = client.requests_made + 1
    client.get(nc.BASE_URL + '/category/9')
    with pytest.raises(nc.BlockedError):
        client.get(nc.BASE_URL + '/category/9/2')


def test_origin_missing_is_marked():
    item = nc.parse_product_page('<div class="product_detail_Right_title">褲</div>')
    assert item['origin'] == nc.UNKNOWN_ORIGIN
