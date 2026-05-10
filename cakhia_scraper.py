import os
import re
import json
import base64
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
# Luôn kiểm tra xem domain có bị đổi không (ví dụ: cakhia1.com, cakhia6.tv...)
HOME_URL = "https://cakhiazkz.cc" 
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_matches():
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    try:
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        # Cakhia hay thay đổi class, nên quét theo cấu trúc link /truc-tiep/
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if '/truc-tiep/' in href:
                full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
                title = a_tag.get('title') or a_tag.text.strip()
                if len(title) < 5: continue
                
                img = a_tag.find('img')
                logo = img.get('data-src') or img.get('src') or "" if img else ""
                if logo.startswith('//'): logo = 'https:' + logo
                
                if full_link not in [m['url'] for m in matches]:
                    matches.append({'url': full_link, 'title': title, 'logo': logo})
        return matches
    except: return []

def deep_scan_m3u8(text):
    """Kỹ thuật quét sâu: Tìm link m3u8 trong mọi định dạng (JSON, Unicode, Base64)"""
    found = []
    # 1. Tìm m3u8 thông thường và Unicode (\u002f thay cho /)
    text = text.replace('\\u002f', '/').replace('\\/', '/')
    pattern = r'(https?://[^\s"\'<>]*\.m3u8[^\s"\'<>]*)'
    found.extend(re.findall(pattern, text))
    
    # 2. Quét các khối JSON ẩn
    json_blocks = re.findall(r'\{.*?"url".*?\}', text)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if 'url' in data and '.m3u8' in data['url']:
                found.append(data['url'])
        except: continue
        
    return found

def extract_m3u8(match_url):
    try:
        # Cần Referer để server trả về nội dung đúng
        headers = {"Referer": HOME_URL, "User-Agent": UA}
        res = crequests.get(match_url, impersonate="chrome110", headers=headers, timeout=20)
        html = res.text
        
        streams = []
        seen = set()
        
        # Bước 1: Quét toàn bộ HTML của trang trận đấu
        raw_links = deep_scan_m3u8(html)
        
        # Bước 2: Tìm Iframe và quét sâu vào trong
        soup = BeautifulSoup(html, 'html.parser')
        for ifr in soup.find_all('iframe'):
            ifr_url = ifr.get('src') or ifr.get('data-src') or ""
            if not ifr_url: continue
            if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
            
            try:
                # Ép Referer là match_url để "đánh lừa" iframe player
                ifr_res = crequests.get(ifr_url, impersonate="chrome110", headers={"Referer": match_url}, timeout=10)
                raw_links.extend(deep_scan_m3u8(ifr_res.text))
            except: continue

        # Bước 3: Chuẩn hóa link
        for link in raw_links:
            # Loại bỏ các tham số thừa sau .m3u8 nếu có dấu " hoặc '
            clean = link.split('"')[0].split("'")[0].replace('\\', '')
            if ".m3u8" in clean and clean not in seen:
                name = "Server VIP"
                if "blv" in clean.lower(): name = "Thuyết Minh VN"
                elif "fullhd" in clean.lower(): name = "HD 1080p"
                
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
        # Xóa các ký tự gây lỗi trong tên trận
        clean_title = m['title'].replace(',', '').replace('"', '')
        links = extract_m3u8(m['url'])
        
        if links:
            print(f"✅ Đã tìm thấy: {clean_title}")
            for s in links:
                # THÊM HEADER VÀO LINK - Đây là mấu chốt để xem được
                final_link = f"{s['url']}|Referer={HOME_URL}/&User-Agent={UA}"
                playlist += f'#EXTINF:-1 tvg-logo="{m["logo"]}", {clean_title} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count += 1
        else:
            print(f"❌ Trận này chưa có link hoặc bị chặn: {clean_title}")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 HOÀN TẤT! Tổng cộng gắp được {count} link.")

if __name__ == "__main__":
    main()
