from curl_cffi import requests
from bs4 import BeautifulSoup
import re

# --- CẤU HÌNH SOCOLIVE ---
DOMAIN_URL = "https://socolivee.cv"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get_matches(domain_url):
    """Quét trang chủ Socolive để lấy link trận đấu"""
    print(f"🚀 Đang thả cào cào vào Socolive: {domain_url}")
    try:
        # Dùng lớp vỏ Chrome110 của curl_cffi để qua mặt khiên Cloudflare
        res = requests.get(domain_url, impersonate="chrome110", timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        matches = []
        
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            # Bắt đúng cấu trúc link trực tiếp của Soco
            if '/truc-tiep/' in href or '/room/' in href:
                full_link = href if href.startswith('http') else f"{domain_url.rstrip('/')}{href}"
                
                # Bóc tên trận đấu
                raw_name = a_tag.get('title') or a_tag.text.strip()
                clean_name = re.sub(r'\s+', ' ', raw_name).strip()
                
                # Lọc bỏ mấy nút rác
                if len(clean_name) > 5 and "Xem ngay" not in clean_name:
                    if not any(m['url'] == full_link for m in matches):
                        matches.append({'url': full_link, 'title': clean_name})
                        
        return matches
    except Exception as e:
        print(f"❌ Lỗi quét trang chủ: {e}")
        return []

def extract_m3u8(url):
    """Truy cập từng trận để bắt sống link M3U8"""
    try:
        res = requests.get(url, impersonate="chrome110", timeout=15)
        html = res.text
        streams = []
        seen = set()
        
        # Quét sạch các đoạn text chứa đuôi .m3u8 trong mã nguồn
        for l in re.findall(r'(https?://[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', html):
            clean_link = l.replace('\\/', '/').replace('\\', '')
            
            # Sút bay mấy luồng quảng cáo (ad, lulu)
            if clean_link not in seen and "ad" not in clean_link.lower() and "lulu" not in clean_link.lower():
                streams.append(clean_link)
                seen.add(clean_link)
                
        return streams
    except Exception as e:
        return []

def main():
    matches = get_matches(DOMAIN_URL)
    
    if not matches: 
        print("❌ Socolive hiện tại không có trận nào hoặc nó đã đổi tên miền.")
        return

    playlist = "#EXTM3U\n"
    count = 0
    print(f"✅ Quét xong trang chủ, túm được {len(matches)} trận. Bắt đầu lôi luồng M3U8...")
    
    for m in matches:
        print(f"-> Đang xử lý: {m['title'][:50]}...")
        links = extract_m3u8(m['url'])
        
        if links:
            # Lấy luồng cuối cùng (thường là luồng ổn định nhất sau khi né quảng cáo)
            final_url = links[-1]
            
            # Đóng gói chuẩn chỉ lên Tivi
            base_domain = "/".join(DOMAIN_URL.split('/')[:3])
            playlist += f'#EXTINF:-1 tvg-logo="{DOMAIN_URL}/logo.png", Soco: {m["title"]}\n'
            playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
            playlist += f'#EXTVLCOPT:http-referer={base_domain}/\n'
            playlist += f'#EXTVLCOPT:http-origin={base_domain}\n'
            
            # Ép chặt Referer vào link để chống văng IP
            if "|" not in final_url:
                final_url += f"|Referer={base_domain}/&Origin={base_domain}&User-Agent={UA}"
            
            playlist += f'{final_url}\n'
            count += 1
            
    if count > 0:
        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
        print(f"\n🎉 HOÀN TẤT VỚI TỐC ĐỘ BÀN THỜ! Đã cắm cờ {count} trận Socolive.")
    else:
        print("\n❌ Quét xong nhưng không bóc được link M3U8 nào!")

if __name__ == "__main__":
    main()
