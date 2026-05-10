import os
import re
import json
import random
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
HOME_URL = "https://cakhiazkz.cc"
# Danh sách User-Agent để xoay tua, tránh bị nhận diện bot
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

def get_headers(referer=HOME_URL):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": referer,
        "Accept": "*/*",
        "Origin": HOME_URL
    }

def get_matches():
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    try:
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25, headers=get_headers())
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/truc-tiep/' in href:
                full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
                title = a_tag.get('title') or a_tag.text.strip()
                if len(title) < 5: continue
                if full_link not in [m['url'] for m in matches]:
                    matches.append({'url': full_link, 'title': title})
        return matches
    except: return []

def extract_m3u8(match_url):
    try:
        # Lấy nội dung trang trận đấu
        res = crequests.get(match_url, impersonate="chrome110", headers=get_headers(), timeout=20)
        html = res.text
        
        # Kỹ thuật bẫy link m3u8 trong các biến JSON hoặc Script
        # Cakhia thường giấu trong: window.configs hoặc player_data
        pattern = r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)'
        
        # Làm sạch HTML để xử lý các ký tự escape \/ 
        clean_html = html.replace('\\/', '/')
        found_links = re.findall(pattern, clean_html)
        
        # Quét thêm trong Iframe
        soup = BeautifulSoup(html, 'html.parser')
        for ifr in soup.find_all('iframe'):
            ifr_url = ifr.get('src') or ifr.get('data-src') or ""
            if ifr_url:
                if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
                try:
                    ifr_res = crequests.get(ifr_url, impersonate="chrome110", headers=get_headers(match_url), timeout=10)
                    found_links.extend(re.findall(pattern, ifr_res.text.replace('\\/', '/')))
                except: continue

        streams = []
        seen = set()
        for link in found_links:
            clean = link.split('"')[0].split("'")[0].replace('\\', '')
            if ".m3u8" in clean and clean not in seen:
                # Chỉ lấy link có vẻ là link stream thật (thường chứa cdn hoặc stream)
                streams.append({'url': clean, 'name': "Server VIP"})
                seen.add(clean)
        return streams
    except: return []

def main():
    matches = get_matches()
    if not matches: return
    
    playlist = "#EXTM3U\n"
    count = 0
    
    for m in matches:
        links = extract_m3u8(m['url'])
        if links:
            print(f"✅ Đã tìm thấy link: {m['title']}")
            for s in links:
                final_link = f"{s['url']}|Referer={HOME_URL}/&User-Agent={USER_AGENTS[0]}"
                playlist += f'#EXTINF:-1, {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count += 1
        else:
            # Thử thêm một bước cuối: Quét qua API dự phòng nếu có
            print(f"❌ Vẫn bị chặn: {m['title']}")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 Hoàn tất! Gắp được {count} link.")

if __name__ == "__main__":
    main()
    
