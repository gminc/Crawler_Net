# Crawler_Net
爬取 NET 購物網站 (https://www.net-fashion.net) 分類頁或活動頁的產品資訊（品名、價格、產地、活動、連結、圖片），產出 CSV。

## 安裝
```
pip install -r requirements.txt
```

## 使用
```
# 1. 從首頁找出分類代號（例如嬰幼兒相關）
python net_crawler.py discover --keyword 嬰 寶寶 BABY 童

# 2. 爬指定分類：品名含「褲」、排除中國製
python net_crawler.py crawl 1662 --name-keyword 褲 --exclude-origin 中國 大陸 China -o baby_pants.csv
```
- 可一次給多個分類代號：`crawl 1662 1663`
- 活動頁（任選 N 件、零碼出清等）也可以爬，直接貼網址或寫 `promotion/代號`：
  ```
  python net_crawler.py crawl https://www.net-fashion.net/promotion/1490 promotion/658
  ```
  CSV 的 `promo` 欄會寫活動名稱（例如「嬰幼兒夏日內搭褲 任選 3件 249」），`price` 是商品頁的單件售價
- 加 `--in-stock-only` 只留還有尺寸有貨的商品；CSV 的 `color` 是顏色、`sizes` 是有庫存的尺寸（例如 `S/M/L`）
  - 活動頁的尺寸庫存直接取自活動頁內嵌資料，是當下的庫存；分類頁的庫存來自產品頁，若快取過舊可加 `--refresh` 重抓
- 價格欄位：`price` 是商品頁顯示的售價（有活動時已是活動價）、`original_price` 是原價、`promo_price` 是活動頁標示的活動價
- 有給 `--name-keyword` 時，列表上品名就不符合的商品不會去抓商品頁，省請求
- 產地沒標示的商品會保留，產地欄寫「未標示」，請自行確認
- CSV 用 utf-8-sig 編碼，Excel 直接開不會亂碼

## 避免被封鎖的設計
舊版（`crawler_01.py` 用 000~999 暴力試代號、`crawler_02.py` 每換一頁就把前面所有產品重抓一次）
請求量很大又沒有間隔，因此被對方封鎖。新版改為：

| 機制 | 說明 |
|---|---|
| 請求間隔 | 每個請求間隔 `--delay`（預設 3 秒）＋隨機 0~`--jitter`（預設 3 秒），單線程 |
| robots.txt | 被禁止的路徑不抓 |
| 去重 + 快取 | 產品連結去重；產品頁存在 `cache/`，重跑不會再抓 |
| 退避 | 429 / 503 依 `Retry-After` 等待後重試 |
| 熔斷 | 遇到 403 或重試失敗就立即停止，已抓到的資料仍會寫出 |
| 上限 | 每次最多 `--max-requests`（預設 300）個請求 |

若網站改版導致抓不到品名，程式會印出「解析不到品名」警告，需要更新 `net_crawler.py` 裡的 CSS selector。

## 測試
```
pip install pytest
python -m pytest -q
```
測試使用假 HTML，不會連到網站。
