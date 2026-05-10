import os
import re
import json
import base64
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
HOME_URL = "https://cakhiazkz.cc/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_matches():
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    try:
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        for a_tag in soup.select('a[href*="/truc-tiep/"]'):
            href = a_tag['href']
            full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
            title = a_tag.get('title') or a_tag.text.strip()
            if not title or len(title) < 5: continue
            img_tag = a_tag.find('img')
            logo = img_tag.get('data-src') or img_tag.get('src') or "" if img_tag else ""
            if full_link not in [m['url'] for m in matches]:
                matches.append({'url': full_link, 'title': title, 'logo': logo})
        return matches
    except: return []

def decode_base64_links(text):
    """Giải mã các chuỗi có khả năng là link m3u8 bị ẩn dưới dạng Base64"""
    found = []
    # Tìm các chuỗi trông giống Base64 (độ dài lớn, không chứa khoảng trắng)
    potential_base64 = re.findall(r'[A-Za-z0-9+/]{30,}=*', text)
    for b in potential_base64:
        try:
            decoded = base64.b64decode(b).decode('utf-8')
            if ".m3u8" in decoded:
                found.append(decoded)
        except: continue
    return found

def extract_m3u8(match_url):
    try:
        res = crequests.get(match_url, impersonate="chrome110", timeout=20)
        html = res.text
        streams = []
        seen = set()

        # 1. Tìm trực tiếp (dành cho server cũ)
        raw_links = re.findall(r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', html)
        
        # 2. Tìm trong Script ẩn & Giải mã Base64
        # Cakhia thường để link trong biến: var configs = {...}
        raw_links.extend(decode_base64_links(html))
        
        # 3. Quét các Iframe Player
        soup = BeautifulSoup(html, 'html.parser')
        iframes = soup.find_all('iframe')
        for ifr in iframes:
            ifr_url = ifr.get('src') or ifr.get('data-src') or ""
            if ifr_url:
                if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
                try:
                    # Gửi thêm Referer là trang chủ Cakhia để Iframe trả về nội dung
                    ifr_res = crequests.get(ifr_url, impersonate="chrome110", timeout=10, headers={"Referer": match_url})
                    found_ifr = re.findall(r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', ifr_res.text)
                    raw_links.extend(found_ifr)
                    raw_links.extend(decode_base64_links(ifr_res.text))
                except: continue

        for link in raw_links:
            clean = link.replace('\\/', '/').replace('\\', '').split('"')[0].split("'")[0]
            if ".m3u8" in clean and clean not in seen:
                name = "Server VIP"
                if "tieng-viet" in clean.lower(): name = "Thuyết Minh VN"
                streams.append({'url': clean, 'name': name})
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
            print(f"✅ OK: {m['title']}")
            for s in links:
                # Referer cực kỳ quan trọng để lách 403 Forbidden
                final_link = f"{s['url']}|Referer={HOME_URL}&User-Agent={UA}"
                playlist += f'#EXTINF:-1 tvg-logo="{m["logo"]}", {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count += 1
        else:
            print(f"❌ Không link: {m['title']}")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 Xong! Đã lấy được {count} luồng phát.")

if __name__ == "__main__":
    main()
    
