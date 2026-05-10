import os
import re
import json
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
        
        # Tìm tất cả link trực tiếp
        for a_tag in soup.select('a[href*="/truc-tiep/"]'):
            href = a_tag['href']
            full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
            title = a_tag.get('title') or a_tag.text.strip()
            if not title or len(title) < 5: continue
            
            img_tag = a_tag.find('img')
            logo = ""
            if img_tag:
                logo = img_tag.get('data-src') or img_tag.get('src') or ""
                if logo.startswith('//'): logo = 'https:' + logo

            if full_link not in [m['url'] for m in matches]:
                matches.append({'url': full_link, 'title': title, 'logo': logo})
        return matches
    except Exception as e:
        print(f"❌ Lỗi trang chủ: {e}")
        return []

def scan_text_for_m3u8(text):
    """Hàm phụ để tìm m3u8 trong một chuỗi văn bản"""
    pattern = r'(https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*)'
    links = re.findall(pattern, text)
    cleaned = []
    for l in links:
        clean = l.replace('\\/', '/').replace('\\', '').split('"')[0].split("'")[0]
        if ".m3u8" in clean:
            cleaned.append(clean)
    return cleaned

def extract_m3u8(match_url):
    """Mổ xẻ sâu: Quét HTML gốc -> Quét Iframe -> Quét Script"""
    try:
        res = crequests.get(match_url, impersonate="chrome110", timeout=20)
        html = res.text
        soup = BeautifulSoup(html, 'html.parser')
        streams = []
        seen = set()

        # Bước 1: Tìm m3u8 trong HTML hiện tại
        found_links = scan_text_for_m3u8(html)

        # Bước 2: Tìm các iframe (Cakhia thường giấu player trong này)
        iframes = soup.find_all('iframe')
        for ifr in iframes:
            ifr_url = ifr.get('src') or ifr.get('data-src')
            if ifr_url:
                if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
                try:
                    # Truy cập vào nội dung bên trong iframe để tìm link
                    ifr_res = crequests.get(ifr_url, impersonate="chrome110", timeout=10, headers={"Referer": match_url})
                    found_links.extend(scan_text_for_m3u8(ifr_res.text))
                except:
                    continue

        # Bước 3: Lọc link chuẩn
        for link in found_links:
            if link not in seen:
                name = "Luồng Chính"
                if "tieng-viet" in link.lower() or "blv" in link.lower(): name = "Thuyết Minh VN"
                elif "fullhd" in link.lower(): name = "HD Siêu Nét"
                
                streams.append({'url': link, 'name': name})
                seen.add(link)
        
        return streams
    except Exception as e:
        print(f"⚠️ Lỗi xử lý {match_url}: {e}")
        return []

def main():
    matches = get_matches()
    if not matches: return

    playlist = "#EXTM3U\n"
    count_link = 0
    
    for m in matches:
        links = extract_m3u8(m['url'])
        if links:
            print(f"✅ {m['title']} -> OK")
            for s in links:
                # Quan trọng: Cakhia cần Referer gốc để xem được
                final_link = f"{s['url']}|Referer=https://cakhiazkz.cc/&User-Agent={UA}"
                playlist += f'#EXTINF:-1 tvg-logo="{m["logo"]}", {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count_link += 1
        else:
            print(f"❌ {m['title']} -> Không tìm thấy link")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 HOÀN TẤT! Đã gắp được {count_link} link stream.")

if __name__ == "__main__":
    main()
    
