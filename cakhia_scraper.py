import re
import json
import base64
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

# --- CẤU HÌNH ---
HOME_URL = "https://cakhiazkz.cc"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def get_matches():
    print(f"🚀 Đang quét danh sách trận tại: {HOME_URL}")
    try:
        # Giả lập TLS Fingerprint của Chrome thật
        res = crequests.get(HOME_URL, impersonate="chrome110", timeout=25)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        # Quét tất cả các thẻ a có chứa đường dẫn trực tiếp
        for a_tag in soup.select('a[href*="/truc-tiep/"]'):
            href = a_tag['href']
            full_link = href if href.startswith('http') else f"{HOME_URL.rstrip('/')}{href}"
            title = a_tag.get('title') or a_tag.text.strip()
            if len(title) > 5:
                matches.append({'url': full_link, 'title': title})
        return matches
    except: return []

def extract_m3u8(url):
    """Chiêu thức: Quét mọi chuỗi Base64 và Json ẩn trong script"""
    try:
        headers = {"Referer": HOME_URL, "User-Agent": UA}
        res = crequests.get(url, impersonate="chrome110", headers=headers, timeout=20)
        content = res.text
        
        # 1. Thu thập tất cả các chuỗi có định dạng m3u8 (kể cả bị escape)
        # Bắt cả dạng: https:\/\/...playlist.m3u8
        found = re.findall(r'https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*', content.replace('\\/', '/'))
        
        # 2. Chiêu cao cấp: Tìm link trong các khối Base64
        # Các trang bóng đá rất thích giấu link trong chuỗi b64 dài
        b64_list = re.findall(r'[A-Za-z0-9+/]{50,}=*', content)
        for b in b64_list:
            try:
                decoded = base64.b64decode(b).decode('utf-8')
                if ".m3u8" in decoded:
                    found.append(decoded)
            except: continue

        # 3. Quét sâu vào các Iframe lồng nhau (vòng lặp 2 lớp)
        soup = BeautifulSoup(content, 'html.parser')
        for ifr in soup.find_all('iframe'):
            ifr_url = ifr.get('src') or ifr.get('data-src') or ""
            if "cakhia" in ifr_url or "bitmovin" in ifr_url or "player" in ifr_url:
                if ifr_url.startswith('//'): ifr_url = 'https:' + ifr_url
                try:
                    ifr_res = crequests.get(ifr_url, impersonate="chrome110", headers={"Referer": url}, timeout=10)
                    found.extend(re.findall(r'https?[:\\/]+[^\s"\'<>]*\.m3u8[^\s"\'<>]*', ifr_res.text.replace('\\/', '/')))
                except: continue

        # Lọc link sạch
        streams = []
        seen = set()
        for link in found:
            clean = link.split('"')[0].split("'")[0].replace('\\', '').strip()
            if ".m3u8" in clean and clean not in seen:
                # Tự động nhận diện server Tiếng Việt
                name = "Server VIP"
                if "blv" in clean.lower() or "tieng-viet" in clean.lower():
                    name = "Thuyết Minh VN"
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
            print(f"✅ Đã hốt: {m['title']}")
            for s in links:
                # Header Referer là bắt buộc để link không bị die khi xem
                final_link = f"{s['url']}|Referer={HOME_URL}/&User-Agent={UA}"
                playlist += f'#EXTINF:-1, {m["title"]} ({s["name"]})\n'
                playlist += f'{final_link}\n'
                count += 1
        else:
            print(f"❌ Vẫn tịt: {m['title']}")

    with open("cakhia_live.m3u", "w", encoding="utf-8") as f:
        f.write(playlist)
    print(f"\n🎉 Kết quả: Gắp được {count} link. Toàn kiểm tra file nhé!")

if __name__ == "__main__":
    main()
