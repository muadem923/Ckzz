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
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        
        # Cakhia thường dùng thẻ a chứa đường dẫn /truc-tiep/
        for a_tag in soup.select('a[href*="/truc-tiep/"]'):
            href = a_tag['href']
            full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
            
            # Lấy tên trận
            title = a_tag.get('title') or a_tag.text.strip()
            if not title or len(title) < 5: continue

            # Lấy Logo
            img_tag = a_tag.find('img')
            logo = ""
            if img_tag:
                logo = img_tag.get('data-src') or img_tag.get('src') or ""
                if logo.startswith('//'): logo = 'https:' + logo

            if full_link not in [m['url'] for m in matches]:
                matches.append({'url': full_link, 'title': title, 'logo': logo})
        
        return matches
    except Exception as e:
        print(f"❌ Lỗi quét trang chủ: {e}")
        return []

def extract_m3u8(match_url):
    """Truy quét sâu link m3u8 bên trong trang chi tiết"""
    try:
        res = crequests.get(match_url, impersonate="chrome110", timeout=20)
        html = res.text
        streams = []
        seen = set()
        
        # 1. Regex tìm link m3u8 (xử lý cả link bị escape trong JSON)
        # Bắt các dạng: http...m3u8, http...m3u8?auth=...
        pattern = r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)'
        raw_links = re.findall(pattern, html)
        
        # 2. Tìm link trong các thẻ script ẩn (thường nằm trong biến JSON)
        # Cakhia hay dùng: "link":"https:\/\/..."
        soup = BeautifulSoup(html, 'html.parser')
        for script in soup.find_all('script'):
            if script.string:
                found = re.findall(pattern, script.string)
                raw_links.extend(found)

        # 3. Lọc và làm sạch link
        for link in raw_links:
            clean_link = link.replace('\\/', '/').replace('\\', '').split('"')[0].split("'")[0]
            
            # Chỉ lấy link m3u8 và tránh các link quảng cáo/icon
            if ".m3u8" in clean_link and clean_link not in seen:
                # Định dạng tên server dựa trên nội dung link
                name = "Luồng Chính"
                if "fullhd" in clean_link.lower(): name = "Full HD"
                elif "cdn" in clean_link.lower(): name = "Dự Phòng"
                
                streams.append({'url': clean_link, 'name': name})
                seen.add(clean_link)
        
        return streams
    except Exception as e:
        print(f"⚠️ Lỗi bóc tách {match_url}: {e}")
        return []

def main():
    matches = get_matches()
    if not matches:
        print("❌ Không lấy được danh sách trận đấu.")
        return

    print(f"🔍 Tìm thấy {len(matches)} trận. Bắt đầu mổ link...")
    
    playlist = "#EXTM3U\n"
    count_link = 0
    
    for m in matches:
        links = extract_m3u8(m['url'])
        
        if links:
            print(f"✅ {m['title']} -> {len(links)} link")
            for s in links:
                # Cấu hình Header để xem được trên app (VLC, OTT Navigator,...)
                # Cakhia bắt buộc phải có Referer chuẩn của nó
                final_link = f"{s['url']}|Referer={HOME_URL}&User-Agent={UA}"
                
                playlist += f'#EXTINF:-1 tvg-logo="{m["logo"]}", {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count_link += 1
        else:
            # Ghi log để biết trận nào không có link
            print(f"❌ {m['title']} -> Không tìm thấy link")

    # Lưu file
    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
        
    print(f"\n🎉 HOÀN TẤT! Đã gắp được {count_link} link stream.")

if __name__ == "__main__":
    main()
