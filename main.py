import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 5 
UA = "Mozilla/5.0_Windows_NT_10.0"

async def fetch_stream(context, match, sem):
    """Nhiệm vụ: Mở 1 trận, chộp link FLV/M3U8 và tên BLV rồi đóng lại thật nhanh"""
    async with sem:
        print(f"-> Đang áp sát: {match['raw_title'][:40]}...")
        page = await context.new_page()
        
        # Bóp cổ mạng: Chỉ cho phép tải text và script để tìm link, chặn video/ảnh cho nhẹ
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

        stream_url = None
        
        def handle_request(request):
            nonlocal stream_url
            url = request.url
            # ĐIỂM ĂN TIỀN: Săn cả .flv (link xịn) và .m3u8, né quảng cáo lulu
            if (".flv" in url or ".m3u8" in url) and "ad" not in url.lower() and "lulu" not in url.lower():
                stream_url = url

        page.on("request", handle_request)

        try:
            # Nhảy vào trận đấu
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=15000)
            
            # Hút tên BLV (thường nằm ở class blv-name hoặc player-info-name)
            blv_name = await page.evaluate("""() => {
                let el = document.querySelector('.blv-name, .player-info-name, .name-blv, .chat-item-name');
                return el ? el.innerText.trim() : '';
            }""")
            match['blv'] = blv_name
            
            # Vòng lặp chờ thông minh: Cứ 0.5s check 1 lần. Thấy link là RÚT LUI NGAY.
            for _ in range(15):
                if stream_url:
                    break
                await page.wait_for_timeout(500)
        except:
            pass
        finally:
            await page.close() 

        if stream_url:
            match['stream_url'] = stream_url
            return match
        return None

async def main():
    print("🥷 KHỞI ĐỘNG BẢN CHUẨN: QUÉT FLV + M3U8 + LOGO...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--mute-audio']
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

        print("🔍 Đang đếm trận đấu và lấy Logo...")
        links = await page.evaluate("""() => {
            let items = [];
            document.querySelectorAll('a').forEach(a => {
                let href = a.href;
                if(href.includes('/truc-tiep/') || href.includes('/room/')) {
                    let text = a.innerText || "";
                    
                    // SÚT BAY CÁC MÔN KHÁC KHỎI TẦM NHÌN
                    if(!text.includes('Bóng rổ') && !text.includes('Tennis') && !text.includes('Cầu lông')) {
                        // TÌM LOGO NGAY LÚC NÀY
                        let img = a.querySelector('img') || a.closest('div').querySelector('img');
                        let logo = img ? (img.src || img.getAttribute('data-src')) : "";
                        
                        let title = a.title || text.replace(/\\s+/g, ' ').trim();
                        if(title.length > 5 && !items.find(i => i.url === href)) {
                            items.push({url: href, raw_title: title, logo: logo});
                        }
                    }
                }
            });
            return items;
        }""")

        await page.close()

        if not links:
            print("❌ Trang web hiện không có trận nào hoặc giấu link!")
            await browser.close()
            return

        print(f"✅ Tóm được {len(links)} trận Bóng đá. Bắt đầu ĐÁNH ÚP ĐỒNG LOẠT...")
        
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_stream(context, m, sem) for m in links]
        results = await asyncio.gather(*tasks)

        # Xử lý thành phẩm và dọn dẹp tên
        playlist = "#EXTM3U\n"
        count = 0
        
        for res in results:
            if res and 'stream_url' in res:
                # 1. Tách Giờ thi đấu
                time_match = re.search(r'(\d{2}:\d{2})', res['raw_title'])
                time_str = f"[{time_match.group(1)}] " if time_match else ""
                
                # 2. Dọn rác khỏi tên (chỉ giữ lại tên đội)
                clean_name = re.sub(r'(?i)(Xem ngay|Trực tiếp|Hot|Live|Bóng đá|Sắp diễn ra|\d{2}:\d{2})', '', res['raw_title'])
                clean_name = re.sub(r'\s+', ' ', clean_name).strip()
                
                # 3. Ghép tên BLV
                blv_str = f" [BLV {res['blv']}]" if res['blv'] else ""
                
                # Tên hoàn chỉnh: [Giờ] Đội A vs Đội B [BLV]
                display_name = f"{time_str}{clean_name}{blv_str}"
                
                # Xử lý Logo và Link
                logo = res['logo'] if (res['logo'] and 'http' in res['logo']) else "https://socolivee.cv/logo.png"
                final_url = res['stream_url']
                origin = TARGET_URL.rstrip('/')
                fixed_url = f"{final_url}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}"
                
                # Xuất ra định dạng m3u
                playlist += f'#EXTINF:-1 group-title="Socolive" tvg-logo="{logo}", {display_name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{fixed_url}\n'
                count += 1

        if count > 0:
            with open("socolive_live.m3u", "w", encoding="utf-8") as f:
                f.write(playlist)
            print(f"\n🎉 QUÁ ĐỈNH! Quét xong {count} trận nét căng (đã có FLV + Logo).")
        else:
            print("\n❌ Không bắt được luồng nào!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
