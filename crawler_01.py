### 1. 取三位數loop, 001~999
### 2. 判斷r是否有值
### 3. 只跑有值的r

from os import write
import requests
from bs4 import BeautifulSoup

nets = []
net = []
for x in range(1000):
    x += 0
    if x < 10:
        x = '00' + str(x)
    elif 10 <= x < 100:
        x = '0' + str(x)
    websites = str('https://www.net-fashion.net/product/344' + str(x))
    r = requests.get(websites) #將此頁面的HTML GET下來
    if r.url == 'https://www.net-fashion.net/':
        continue
    soup = BeautifulSoup(r.text,'html.parser') #將網頁資料以html.parser
    names = soup.select_one('div.product_detail_Right_title') #取HTML標中的 <div class="html_block_detail"></div> 中的 <p> & <span> 標籤存入selectItems
    # print(names.text.strip())
    prices = soup.select_one('div.product_priceR_real > b') #取HTML標中的 <div class="html_block_detail"></div> 中的 <p> & <span> 標籤存入selectItems
    # print(prices.text.strip())
    origins = soup.select('div.html_block_detail > p > span') #取HTML標中的 <div class="html_block_detail"></div> 中的 <p> & <span> 標籤存入selectItems
    for origin in origins:
        if '產地' in origin.text:
            originData = origin.text.split(' ')[1]
            # print(origin.text.split(' ')[1])
    # print(selectedItems[0].text) 該方法風險較高 結構可能各有變化
    nets.append([names.text.strip(), prices.text.strip(), originData])
    for item in nets:    
        if item in net:
            continue
        net.append(item)


import csv
with open('net.csv', 'w', newline='', encoding = 'utf-8') as f:        
    w = csv.writer(f)
    w.writerows(net)