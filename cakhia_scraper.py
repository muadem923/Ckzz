import re
import json
import random
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
HOME_URL = "https://cakhiazkz.cc"
# Giả lập Header cực giống trình duyệt thật
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_matches():
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    headers = {"User-Agent": UA, "Referer": HOME_URL}
    try:
        # Lấy danh sách trận từ trang chủ
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25, headers=headers)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/truc-tiep/' in href:
                full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
                title = a_tag.get('title') or a_tag.text.strip()
                if len(title) > 5 and full_link not in [m['url'] for m in matches]:
                    matches.append({'url': full_link, 'title': title})
        return matches
    except: return []

def extract_m3u8_api(match_url):
    """Kỹ thuật: Giả lập gọi API lấy link của Cakhia"""
    try:
        headers = {"User-Agent": UA, "Referer": HOME_URL, "Origin": HOME_URL}
        res = crequests.get(match_url, impersonate="chrome110", headers=headers, timeout=20)
        html = res.text
        
        # 1. Tìm các chuỗi JSON chứa link stream (Cakhia hay để trong mảng 'links' hoặc 'play_info')
        # Regex này bắt các link m3u8 bị dấu trong JSON string
        streams = []
        seen = set()
        
        # Tìm các link m3u8 kể cả khi nó bị dấu / thành \/
        raw_found = re.findall(r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', html.replace('\\/', '/'))
        
        # 2. Nếu không thấy, quét các link Iframe nhưng truy cập với Header 'X-Requested-With'
        soup = BeautifulSoup(html, 'html.parser')
        iframes = soup.find_all('iframe')
        for ifr in iframes:
            ifr_url = ifr.get('src') or ifr.get('data-src') or ""
            if ifr_url:
                if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
                try:
                    # Truy cập vào Player Iframe
                    ifr_res = crequests.get(ifr_url, impersonate="chrome110", headers={"Referer": match_url}, timeout=10)
                    raw_found.extend(re.findall(r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', ifr_res.text.replace('\\/', '/')))
                except: continue

        for link in raw_found:
            clean = link.split('"')[0].split("'")[0].replace('\\', '').strip()
            if ".m3u8" in clean and clean not in seen:
                # Ưu tiên các link từ CDN chính của Cakhia
                streams.append({'url': clean, 'name': "Server VIP"})
                seen.add(clean)
        
        return streams
    except: return []

def main():
    matches = get_matches()
    if not matches:
        print("❌ Không lấy được danh sách trận đấu.")
        return

    playlist = "#EXTM3U\n"
    count = 0
    
    for m in matches:
        links = extract_m3u8_api(m['url'])
        if links:
            print(f"✅ OK: {m['title']}")
            for s in links:
                # LƯU Ý: Referer phải là domain hiện tại của Cakhia
                final_link = f"{s['url']}|Referer={HOME_URL}/&User-Agent={UA}"
                playlist += f'#EXTINF:-1, {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count += 1
        else:
            print(f"❌ Chặn: {m['title']}")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 Kết quả: Gắp được {count} link.")

if __name__ == "__main__":
    main()
