from os import write
import requests
from bs4 import BeautifulSoup
from attr import attrs, attrib

nets = []
net_links_imgs = []
r = requests.get('https://www.net-fashion.net/category/1466') #將此頁面的HTML GET下來
soup = BeautifulSoup(r.text,'html.parser') #將網頁資料以html.parser
last_page = soup.select('.yahoo > a')[-2]
last_page_number = int(last_page.text)

for one_page in range(0, last_page_number):
    one_page += 1
    all_pages = 'https://www.net-fashion.net/category/1466' + '/' + str(one_page)
    r = requests.get(all_pages) #將此頁面的HTML GET下來
    soup = BeautifulSoup(r.text,'html.parser') #將網頁資料以html.parser
    links_imgs = soup.select('.main_img > a')

    for item in links_imgs:
        link = item.get('href')
        img = item.contents[1].get('src')
        net_links_imgs.append([link, img])

    for link_img in net_links_imgs:
        r = requests.get(str(link_img[0])) #將此頁面的HTML GET下來    
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
        nets.append([names.text.strip(), prices.text.strip(), originData, link_img[0], link_img[1]])

import csv
with open('net_02.csv', 'w', newline='', encoding = 'utf-8') as f:        
    w = csv.writer(f)
    w.writerows(nets)