import os
import re
import json
import codecs
from datetime import datetime
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
HOME_URL = "https://cakhiazkz.cc/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_matches():
    """Quét trang chủ lấy danh sách trận đấu"""
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    try:
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        
        # Cakhia thường dùng thẻ a có class liên quan đến item trận đấu
        for a_tag in soup.select('a[href*="/truc-tiep/"]'):
            href = a_tag['href']
            full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
            
            # Lấy tên trận và Logo
            title = a_tag.get('title') or a_tag.text.strip()
            img_tag = a_tag.find('img')
            logo = img_tag.get('data-src') or img_tag.get('src') if img_tag else ""
            if logo and logo.startswith('//'): logo = 'https:' + logo

            if full_link not in [m['url'] for m in matches]:
                matches.append({'url': full_link, 'title': title, 'logo': logo})
        
        return matches
    except Exception as e:
        print(f"❌ Lỗi quét trang chủ: {e}")
        return []

def extract_m3u8(match_url):
    """Vào trang chi tiết để lấy link stream"""
    try:
        res = crequests.get(match_url, impersonate="chrome110", timeout=15)
        html = res.text
        streams = []
        
        # Regex tìm link m3u8 trong script hoặc data attributes
        # Cakhia hay dùng định dạng "link": "..." hoặc "url": "..."
        pattern = r'(https?://[^\s"\'<>]*\.m3u8[^\s"\'<>]*)'
        raw_links = re.findall(pattern, html)
        
        seen = set()
        for link in raw_links:
            clean_link = link.replace('\\/', '/').split('"')[0].split("'")[0]
            if clean_link not in seen:
                # Thử tìm tên Server/BLV gần đó
                name = "Server VIP"
                if "tieng-viet" in clean_link: name = "Thuyết Minh VN"
                
                streams.append({'url': clean_link, 'name': name})
                seen.add(clean_link)
        
        return streams
    except:
        return []

def main():
    matches = get_matches()
    if not matches:
        print("Không tìm thấy trận nào!")
        return

    playlist = "#EXTM3U\n"
    for m in matches:
        print(f"-> Đang xử lý: {m['title']}")
        links = extract_m3u8(m['url'])
        
        for s in links:
            # Thêm Header vào link để lách chặn của Cakhia
            final_link = f"{s['url']}|Referer={HOME_URL}&User-Agent={UA}"
            
            playlist += f'#EXTINF:-1 tvg-logo="{m["logo"]}", {m["title"]} ({s["name"]})\n'
            playlist += f'{final_link}\n'

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"🎉 Xong! Đã tạo file m3u với {len(matches)} trận.")

if __name__ == "__main__":
    main()
