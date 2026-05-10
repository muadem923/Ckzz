import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 5 # Số trận mở cùng lúc (để 5 cho an toàn, không bị GitHub khóa)
UA = "Mozilla/5.0_Windows_NT_10.0"

async def fetch_m3u8(context, match, sem):
    """Nhiệm vụ: Mở 1 trận, chộp link M3U8 rồi đóng lại thật nhanh"""
    async with sem:
        print(f"-> Đang áp sát: {match['title'][:40]}...")
        page = await context.new_page()
        
        # Bóp cổ mạng: Chỉ cho phép tải text và script để tìm link, chặn 100% video/ảnh
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

        m3u8_url = None
        
        def handle_request(request):
            nonlocal m3u8_url
            url = request.url
            # Lọc luồng xịn, bỏ qua quảng cáo
            if ".m3u8" in url and "ad" not in url.lower() and "lulu" not in url.lower():
                m3u8_url = url

        page.on("request", handle_request)

        try:
            # Nhảy vào trận đấu
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=15000)
            
            # Vòng lặp chờ thông minh: Cứ 0.5s check 1 lần. Thấy link M3U8 là RÚT LUI NGAY lập tức, không chờ load hết trang.
            for _ in range(15):
                if m3u8_url:
                    break
                await page.wait_for_timeout(500)
        except:
            pass
        finally:
            await page.close() # Xong việc đóng tab ngay cho nhẹ RAM

        if m3u8_url:
            match['m3u8'] = m3u8_url
            return match
        return None

async def main():
    print("🥷 KÍCH HOẠT CHIẾN THUẬT: CÀN QUÉT ĐA LUỒNG SIÊU TỐC...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled', 
                '--no-sandbox', 
                '--mute-audio'
            ]
        )
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        
        # Ngắt quảng cáo ở trang chủ
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script"] else route.abort())

        print(f"👉 Đang thâm nhập Socolive: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print("❌ Lỗi mạng khi vào trang chủ.")
            await browser.close()
            return

        print("🔍 Đang đếm số lượng trận đấu...")
        links = await page.evaluate("""() => {
            let items = [];
            document.querySelectorAll('a').forEach(a => {
                let href = a.href;
                if(href.includes('/truc-tiep/') || href.includes('/room/')) {
                    let title = a.title || a.innerText.replace(/\\s+/g, ' ').trim();
                    if(title.length > 5 && !title.includes('Xem ngay') && !items.find(i => i.url === href)) {
                        items.push({url: href, title: title});
                    }
                }
            });
            return items;
        }""")

        await page.close()

        if not links:
            print("❌ Trang web hiện không có trận nào hoặc nó giấu link!")
            await browser.close()
            return

        print(f"✅ Tóm được {len(links)} trận. Bắt đầu ĐÁNH ÚP ĐỒNG LOẠT...")
        
        # Tạo hàng đợi, cho phép 5 tab chạy song song
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_m3u8(context, m, sem) for m in links]
        
        # Kích hoạt chạy đồng loạt tất cả các trận
        results = await asyncio.gather(*tasks)

        # Xử lý thành phẩm
        playlist = "#EXTM3U\n"
        count = 0
        
        for res in results:
            if res and 'm3u8' in res:
                final_url = res['m3u8']
                origin = TARGET_URL.rstrip('/')
                fixed_url = f"{final_url}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}"
                
                playlist += f'#EXTINF:-1 tvg-logo="{TARGET_URL}/logo.png", Soco: {res["title"]}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{fixed_url}\n'
                count += 1

        if count > 0:
            with open("socolive_live.m3u", "w", encoding="utf-8") as f:
                f.write(playlist)
            print(f"\n🎉 QUÁ ĐỈNH! Quét xong {count} trận trong chớp mắt.")
        else:
            print("\n❌ Không bắt được luồng nào!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
