import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 5 
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

async def fetch_m3u8(context, match, sem):
    async with sem:
        page = await context.new_page()
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())
        m3u8_url = None
        
        def handle_request(request):
            nonlocal m3u8_url
            if ".m3u8" in request.url and "ad" not in request.url.lower() and "lulu" not in request.url.lower():
                m3u8_url = request.url
        page.on("request", handle_request)

        try:
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
            # Hút tên BLV từ khung chat hoặc info
            blv = await page.evaluate("() => document.querySelector('.player-info-name, .blv-name, .name-blv, .chat-item-name')?.innerText || ''")
            match['blv'] = blv.strip()
            
            for _ in range(12):
                if m3u8_url: break
                await page.wait_for_timeout(1000)
        except: pass
        finally: await page.close()

        if m3u8_url:
            match['m3u8'] = m3u8_url
            return match
        return None

async def main():
    print("🚀 ĐANG KÍCH HOẠT RADAR VÉT CẠN - CHẤP MỌI LOẠI CẤU TRÚC...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        
        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            # Cuộn trang cực mạnh để bung hết dữ liệu ẩn
            for _ in range(5):
                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(800)
        except:
            await browser.close(); return

        # THUẬT TOÁN VÉT CẠN: Quét mọi thẻ có chứa link trực tiếp
        matches_data = await page.evaluate("""() => {
            let items = [];
            let seen = new Set();
            
            // Tìm tất cả link dẫn đến phòng live
            document.querySelectorAll('a').forEach(el => {
                let href = el.href;
                if((href.includes('/truc-tiep/') || href.includes('/room/')) && !seen.has(href)) {
                    
                    // Đi tìm khung bao quanh cái link này để lấy thông tin
                    let container = el.closest('div[class*="item"], div[class*="match"], div[class*="card"]') || el;
                    let text = container.innerText || "";
                    
                    // Lọc: Chỉ lấy Bóng đá
                    if(text.includes('Bóng rổ') || text.includes('Tennis') || text.includes('Cầu lông')) return;

                    // Hút thời gian (dạng HH:mm)
                    let timeMatch = text.match(/(\\d{2}:\\d{2})/);
                    let time = timeMatch ? timeMatch[1] : "";

                    // Hút Logo (ưu tiên ảnh đầu tiên trong khung)
                    let img = container.querySelector('img');
                    let logo = img ? (img.src || img.getAttribute('data-src')) : "";

                    // Hút tên Đội (Tách theo dòng hoặc theo dấu VS)
                    let cleanText = text.replace(/Xem ngay|Trực tiếp|Hot|Live|Sắp diễn ra|Bóng đá|\\d{2}:\\d{2}/g, '').trim();
                    let nameParts = cleanText.split(/\\n|vs|\\-/);
                    let teamA = nameParts[0]?.trim() || "";
                    let teamB = nameParts[nameParts.length - 1]?.trim() || "";

                    if(teamA.length > 2 && teamB.length > 2) {
                        seen.add(href);
                        items.push({ url: href, time: time, logo: logo, teamA: teamA, teamB: teamB });
                    }
                }
            });
            return items;
        }""")
        await page.close()

        if not matches_data:
            print("❌ Vẫn không thấy dữ liệu! Thử quét dự phòng..."); await browser.close(); return
        
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_m3u8(context, m, sem) for m in matches_data]
        results = await asyncio.gather(*tasks)

        # XUẤT FILE M3U
        playlist = "#EXTM3U\n"
        count = 0
        for r in results:
            if r and 'm3u8' in r:
                t_str = f"({r['time']}) " if r['time'] else ""
                b_str = f"{r['blv']} - " if r['blv'] else ""
                # Làm sạch tên đội lần cuối (xóa khoảng trắng thừa)
                team_info = f"{r['teamA']} vs {r['teamB']}".replace('\\n', ' ').strip()
                display_name = f"{t_str}{b_str}{team_info}"
                
                logo = r['logo'] if (r['logo'] and 'http' in r['logo']) else "https://socolivee.cv/logo.png"
                origin = TARGET_URL.rstrip('/')
                
                playlist += f'#EXTINF:-1 tvg-logo="{logo}", {display_name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{r["m3u8"]}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
            
        print(f"🎉 THÀNH CÔNG! Đã gắp xong {count} trận bóng đá cho bác Toàn.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
