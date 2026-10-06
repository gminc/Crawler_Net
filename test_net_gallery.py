"""net_gallery 離線測試：不下載圖片。"""

import json
import re

import net_gallery as ng

ROWS = [
    {'name': 'Slim Fit修身直筒牛仔褲', 'color': '720暗夜藍', 'sizes': '32/34', 'price': '299',
     'original_price': '599', 'origin': '越南製', 'promo': '零碼出清 任選 3件 5折',
     'source': 'promotion/658', 'link': 'https://www.net-fashion.net/product/715032', 'img': ''},
    {'name': 'Slim Fit修身直筒牛仔褲', 'color': '719深藍', 'sizes': '', 'price': '$299',
     'original_price': '', 'origin': '越南製', 'promo': '零碼出清 任選 3件 5折',
     'source': 'promotion/658', 'link': 'https://www.net-fashion.net/product/715030', 'img': ''},
    {'name': 'BABY牛仔輕便短褲', 'color': '703淡藍', 'sizes': '80cm', 'price': '149',
     'original_price': '299', 'origin': '台灣製', 'promo': '', 'source': 'category/1824',
     'link': 'https://www.net-fashion.net/product/667690', 'img': ''},
]


def test_build_styles_groups_colors_by_name():
    styles = ng.build_styles(ROWS)
    assert [s['name'] for s in styles] == ['Slim Fit修身直筒牛仔褲', 'BABY牛仔輕便短褲']
    jeans, baby = styles
    assert jeans['cat'] == '褲・裙' and baby['cat'] == '童裝'
    first, second = jeans['colors']
    assert (first['code'], first['color'], first['sizes']) == ('720', '暗夜藍', ['32', '34'])
    assert (first['price'], first['orig']) == (299, 599)
    assert (second['sizes'], second['price'], second['orig']) == ([], 299, 299)  # 沒原價時用售價
    assert baby['colors'][0]['origin'] == '台灣'


def test_split_color_without_code():
    assert ng.split_color('單一顏色') == ('', '單一顏色')
    assert ng.split_color('') == ('', '')


def test_render_document_and_fragment():
    styles = ng.build_styles(ROWS)
    page = ng.render(styles, '測試 <清單>', '2026-10-06 10:00')
    assert page.startswith('<!doctype html>')
    assert '<title>測試 &lt;清單&gt;</title>' in page
    data = json.loads(re.search(r'const DATA = (\[.*?\]);\n', page).group(1))
    assert data[0]['colors'][0]['link'].endswith('/715032')
    meta = json.loads(re.search(r'const META = (\{.*?\});\n', page).group(1))
    assert meta['origins'] == [['越南', 2], ['台灣', 1]]
    assert {s['label'] for s in meta['sources']} == {'promotion/658', 'category/1824'}

    fragment = ng.render(styles, 'x', 'y', fragment=True)
    assert not fragment.startswith('<!doctype') and fragment.startswith('<title>')


def test_script_close_tag_is_escaped():
    rows = [dict(ROWS[0], name='</script><b>x')]
    page = ng.render(ng.build_styles(rows), 't', 's')
    assert '</script><b>' not in page


def test_default_title():
    assert ng.default_title(ROWS[:2]) == '零碼出清 任選 3件 5折'
    assert ng.default_title(ROWS) == 'NET 商品清單'
