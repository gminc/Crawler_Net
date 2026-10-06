# Crawler_Net
NET 購物網站 (https://www.net-fashion.net) 選品工具：排除中國製、只看有庫存的商品。

| 程式 | 用途 |
|---|---|
| `net_app.py` | **NET 選品小幫手（本機版）**：在自己電腦上開網頁，點目錄才爬、邊爬邊顯示，有購物清單 |
| `net_crawler.py` | 命令列爬蟲：爬指定分類／活動頁，輸出 CSV（本機版也使用它） |

---

## NET 選品小幫手（本機版）

> ⚠️ **這是在你自己的電腦上執行的版本。** 程式只在本機 `http://127.0.0.1:8765` 開網頁，外部連不進來；
> 不是放在網路上的服務，關掉終端機視窗就停止。

### 安裝（建立獨立的虛擬環境，不影響電腦上其他 Python 程式）
電腦上若有其他重要的 Python 程式（例如程式交易），**請務必用虛擬環境**：套件只裝在本專案的 `.venv` 資料夾，
不會改動系統或其他程式使用的 Python 與套件。

Windows（命令提示字元或 PowerShell，在專案資料夾內）：
```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```
macOS / Linux：
```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```
- 需要 Python 3.8 以上（已在 3.8 與 3.11 測試）。只用標準函式庫 + `requests`、`beautifulsoup4`
- 一律用 `.venv` 裡的 python 執行（如上），不需要 activate，也不會用到全域的 pip
- 要移除：直接刪掉 `.venv` 和 `data` 資料夾即可

### 啟動
Windows：
```
.venv\Scripts\python net_app.py
```
macOS / Linux：
```
.venv/bin/python net_app.py
```
瀏覽器會自動打開 `http://127.0.0.1:8765/`，按 `Ctrl+C` 結束。

常用選項：
| 選項 | 說明 |
|---|---|
| `--port 8765` | 換埠號（如果 8765 被其他程式佔用） |
| `--stock-hours 6` | 商品頁（庫存）幾小時內不重抓 |
| `--exclude-origin 中國 大陸 China` | 要排除的產地 |
| `--no-browser` | 不自動開瀏覽器 |

### 怎麼用
1. 上方目錄是從 NET 網站選單自動取得的：先點大分類（女裝、男裝、童裝、嬰兒、促銷），再點子分類或活動
2. **點了才開始抓**，抓取在背景一路跑完整個分類；確認好的商品會陸續出現
3. 捲到底部如果還沒抓完，會看到「資料抓取中・已檢查 x/y 款」動畫
4. 點卡片上的尺寸就加入購物清單（右下角）；清單會依「任選 N 件 X 折」估算金額、提醒還差幾件湊滿，可複製清單
5. 結帳：從清單點「到 NET 加入購物車」，在 NET 網站上加入購物車結帳（本程式不會碰你的 NET 帳號）

### 抓取流程（最省請求的順序）
每一款商品依序判斷，越便宜的篩選越先做：
1. **本機已記住是中國製** → 跳過，0 個請求（產地記在 `data/origins.sqlite3`，同名不同顏色共用）
2. **活動頁已顯示沒庫存** → 跳過，0 個請求（活動頁本身附有各顏色庫存）
3. 都不是才抓商品頁：中國製 → 記住並跳過整款（不抓其他顏色）；非中國製 → 逐一確認其他顏色庫存
4. 只有**確定有庫存**的顏色與尺寸才會出現；整款都沒貨就不出現

實測（女嬰下身類 37 款）：第一次 42 個請求、約 4 分鐘；第二次（產地已記住）3 個請求、約 20 秒。

### 對電腦與網站的負擔
- 一次只爬一個分類（多點幾個會排隊），全程一次只發一個請求，每 3～6 秒一次
- 遇到網站拒絕（403）會立刻停止所有抓取，畫面上會提示
- 只接受本機網址的請求（擋掉其他網站透過瀏覽器呼叫）
- 資料都在專案的 `data/` 資料夾：商品頁快取、產地記憶

---

## 命令列爬蟲 `net_crawler.py`
```
# 從首頁找出分類代號（例如嬰幼兒相關）
.venv/bin/python net_crawler.py discover --keyword 嬰 寶寶 BABY 童

# 爬指定分類：品名含「褲」、排除中國製、只留有貨
.venv/bin/python net_crawler.py crawl 1822 1824 --name-keyword 褲 --exclude-origin 中國 大陸 China --in-stock-only -o baby_pants.csv

# 活動頁（可直接貼網址）
.venv/bin/python net_crawler.py crawl https://www.net-fashion.net/promotion/658 --in-stock-only -o clearance.csv
```
- 可一次給多個分類代號或活動；活動頁會先處理
- `--all-colors`：分類頁通常每款只列一個顏色，加了會再抓同款其他顏色（已被產地排除的不抓）
- `--max-age 6`：6 小時內抓過的商品頁直接用快取，較舊的重抓；`--refresh` 一律重抓
- CSV 欄位：`name`、`color`、`sizes`（有庫存的尺寸，如 `S/M/L`）、`price`（售價，有活動時已是活動價）、
  `original_price`（原價）、`promo_price`、`origin`、`promo`（活動名稱）、`source`、`link`、`img`
- 產地沒標示的商品會保留，產地欄寫「未標示」；CSV 用 utf-8-sig，Excel 直接開不會亂碼

## 避免被封鎖的設計
舊版（`crawler_01.py` 用 000~999 暴力試代號、`crawler_02.py` 每換一頁就把前面所有產品重抓一次）
請求量很大又沒有間隔，因此被對方封鎖。新版改為：

| 機制 | 說明 |
|---|---|
| 請求間隔 | 每個請求間隔 `--delay`（預設 3 秒）＋隨機 0~`--jitter`（預設 3 秒），單線程 |
| robots.txt | 被禁止的路徑不抓 |
| 去重 + 快取 | 產品連結去重；商品頁有快取，重跑不會再抓 |
| 產地記憶 | 本機版記住已知中國製的款式，下次 0 請求跳過 |
| 退避 | 429 / 503 依 `Retry-After` 等待後重試 |
| 熔斷 | 遇到 403 或重試失敗就立即停止 |
| 上限 | 命令列每次最多 `--max-requests`（預設 300）個請求 |

若網站改版導致抓不到品名，程式會印出「解析不到品名」警告，需要更新 `net_crawler.py` 裡的 CSS selector。

## 測試
```
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```
測試使用假網站內容，不會連到 NET。
