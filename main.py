import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

async def fetch_stream(context, match):
    """Bản này chỉ tập trung lấy Link và Logo, không làm màu"""
    page = await context.new_page()
    # Chặn rác nhưng giữ lại stream
    await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

    stream_url = None
    # Lắng nghe mọi luồng mạng để chộp .m3u8 hoặc .flv (như bác yêu cầu)
    def handle_request(request):
        nonlocal stream_url
        u = request.url
        if (".m3u8" in u or ".flv" in u) and "ad" not in u.lower() and "lulu" not in u.lower():
            stream_url = u
    page.on("request", handle_request)

    try:
        await page.goto(match['url'], wait_until="domcontentloaded", timeout=20000)
        # Đợi 8 giây cho trình phát video kịp nhả link
        for _ in range(16):
            if stream_url: break
            await page.wait_for_timeout(500)
    except: pass
    finally: await page.close()

    if stream_url:
        match['stream'] = stream_url
        return match
    return None

async def main():
    print("🔄 QUAY LẠI CÁCH TIẾP CẬN CƠ BẢN - ƯU TIÊN RA LINK VÀ LOGO...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        
        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            # Cuộn nhẹ để các ảnh logo (lazy load) hiện ra
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(2000)
        except:
            await browser.close(); return

        # BƯỚC 1: QUÉT TẤT CẢ TRẬN ĐẤU (Không kén chọn)
        raw_list = await page.evaluate("""() => {
            let res = [];
            document.querySelectorAll('a').forEach(a => {
                let h = a.href;
                if(h.includes('/truc-tiep/') || h.includes('/room/')) {
                    // Lấy Logo: Tìm ảnh img gần nhất hoặc nằm trong thẻ a
                    let img = a.querySelector('img') || a.closest('div')?.querySelector('img');
                    let logo = img ? (img.src || img.getAttribute('data-src') || img.getAttribute('data-original')) : "";
                    
                    // Lấy tên trận (Lấy toàn bộ text thô cho chắc ăn)
                    let name = a.innerText.replace(/\\n/g, ' ').trim();
                    if(name.length > 5 && !name.includes('Bóng rổ')) {
                        res.push({url: h, title: name, logo: logo});
                    }
                }
            });
            return res;
        }""")
        await page.close()

        if not raw_list:
            print("❌ Không quét được trận nào!"); await browser.close(); return

        # Loại trùng
        unique_matches = {m['url']: m for m in raw_list}.values()
        
        # BƯỚC 2: ĐI LẤY LINK STREAM (Chạy 3 trận 1 lúc cho nhanh)
        results = []
        for i in range(0, len(unique_matches), 3):
            batch = list(unique_matches)[i:i+3]
            tasks = [fetch_stream(context, m) for m in batch]
            results.extend(await asyncio.gather(*tasks))

        # BƯỚC 3: XUẤT FILE M3U
        playlist = "#EXTM3U\n"
        count = 0
        for r in results:
            if r and 'stream' in r:
                # Dọn rác tên trận đơn giản nhất
                name = r['title'].replace("Xem ngay", "").replace("Trực tiếp", "").strip()
                logo = r['logo'] if (r['logo'] and 'http' in r['logo']) else "https://socolivee.cv/logo.png"
                
                playlist += f'#EXTINF:-1 tvg-logo="{logo}", {name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{r["stream"]}|Referer={TARGET_URL}/&Origin={TARGET_URL}&User-Agent={UA}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
            
        print(f"🎉 THÀNH CÔNG! Đã lấy lại phong độ với {count} trận kèm Logo.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
