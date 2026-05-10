from playwright.sync_api import sync_playwright
import re

TARGET_URL = "https://socolivee.cv" 

def main():
    print("🥷 ĐANG THẢ SÁT THỦ TÀNG HÌNH SĂN SOCOLIVE TRÊN GITHUB...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--mute-audio', 
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 720}
        )
        page = context.new_page()
        
        # Bóp cổ quảng cáo mạng để GitHub chạy nhanh, đỡ tốn tài nguyên
        page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "fetch", "xhr"] else route.abort())
        
        print(f"👉 Đang đột nhập: {TARGET_URL}")
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print("❌ Lỗi mạng khi vào trang chủ.")
            browser.close()
            return
            
        print("🔍 Đang quét trận đấu Socolive...")
        links = page.evaluate("""() => {
            let items = [];
            document.querySelectorAll('a').forEach(a => {
                let href = a.href;
                // Radar chuyên bắt link Socolive
                if(href.includes('/truc-tiep/') || href.includes('/room/')) {
                    let title = a.title || a.innerText.replace(/\\s+/g, ' ').trim();
                    if(title.length > 5 && !title.includes('Xem ngay') && !items.find(i => i.url === href)) {
                        items.push({url: href, title: title});
                    }
                }
            });
            return items;
        }""")
        
        if not links:
            print("❌ Không tìm thấy trận nào!")
            browser.close()
            return
            
        print(f"✅ Tóm được {len(links)} trận. Bắt đầu ép lấy luồng M3U8...")
        playlist = "#EXTM3U\n"
        count = 0
        
        for m in links:
            clean_title = m['title'].strip()
            print(f"-> Đang xử lý: {clean_title[:40]}...")
            
            match_page = context.new_page()
            match_page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "fetch", "xhr"] else route.abort())
            
            m3u8_links = []
            
            def handle_request(request):
                url = request.url
                # Lọc luồng xịn, sút bay luồng lulu và qc
                if ".m3u8" in url and "ad" not in url.lower() and "lulu" not in url.lower():
                    m3u8_links.append(url)
                    
            match_page.on("request", handle_request)
            
            try:
                match_page.goto(m['url'], wait_until="domcontentloaded", timeout=30000)
                # Tăng thời gian chờ lên 6 giây vì Player Socolive load JS chậm hơn Bún Chả một nhịp
                match_page.wait_for_timeout(6000) 
            except:
                pass
            
            if m3u8_links:
                final_url = m3u8_links[-1]
                origin = TARGET_URL.rstrip('/')
                # Ép "Giấy thông hành" để qua mặt bảo mật của app Tivi
                fixed_url = f"{final_url}|Referer={TARGET_URL}/&Origin={origin}&User-Agent=Mozilla/5.0_Windows_NT_10.0"
                
                playlist += f'#EXTINF:-1 tvg-logo="{TARGET_URL}/logo.png", Soco: {clean_title}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent=Mozilla/5.0_Windows_NT_10.0\n'
                playlist += f'{fixed_url}\n'
                count += 1
                
            match_page.close()
            
        if count > 0:
            with open("socolive_live.m3u", "w", encoding="utf-8") as f:
                f.write(playlist)
            print(f"\n🎉 QUÁ NGON! Đã lưu {count} trận vào file 'socolive_live.m3u'.")
        else:
            print("\n❌ Không bắt được luồng nào!")
            
        browser.close()

if __name__ == "__main__":
    main()
  
