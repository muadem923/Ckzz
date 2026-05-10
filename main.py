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
            # Chờ 1 chút để BLV hiện tên
            await page.wait_for_timeout(2000)
            blv = await page.evaluate("() => document.querySelector('.player-info-name, .blv-name, .name-blv')?.innerText || ''")
            match['blv'] = blv.strip()
            
            for _ in range(10):
                if m3u8_url: break
                await page.wait_for_timeout(1000)
        except: pass
        finally: await page.close()

        if m3u8_url:
            match['m3u8'] = m3u8_url
            return match
        return None

async def main():
    print("🚀 ĐANG KÍCH HOẠT RADAR THẾ HỆ MỚI - CUỘN TRANG LẤY LOGO...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        
        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            # CHIÊU THỨC: Cuộn trang để ép Socolive load hết Logo và Tên đội
            for _ in range(3):
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(1000)
        except:
            await browser.close(); return

        # QUÉT CHUYÊN SÂU TỪNG KHUNG TRẬN ĐẤU
        matches_data = await page.evaluate("""() => {
            let items = [];
            // Quét các khối bao quanh trận đấu
            document.querySelectorAll('.match-item, .item-match, a.item').forEach(el => {
                let href = el.href || el.querySelector('a')?.href || "";
                if(!href.includes('/truc-tiep/') && !href.includes('/room/')) return;
                
                let text = el.innerText || "";
                if(text.includes('Bóng rổ') || text.includes('Tennis')) return;

                // Lấy giờ
                let time = el.querySelector('.time, .match-time, .start-time')?.innerText || "";
                
                // Lấy Logo - Socolive hay để trong src hoặc data-src
                let imgEl = el.querySelector('img');
                let logo = imgEl ? (imgEl.src || imgEl.dataset.src || imgEl.dataset.original) : "";
                
                // Lấy tên 2 đội bóng
                let teamEls = el.querySelectorAll('.team-name, .name, .team-item span');
                let teamA = teamEls[0]?.innerText.trim() || "";
                let teamB = teamEls[1]?.innerText.trim() || "";
                
                if (teamA && teamB) {
                    items.push({ url: href, time: time, logo: logo, teamA: teamA, teamB: teamB });
                }
            });
            return items;
        }""")
        await page.close()

        if not matches_data:
            print("❌ Không tìm thấy dữ liệu đội bóng!"); await browser.close(); return

        # Lọc trùng
        unique_list = {m['url']: m for m in matches_data}.values()
        
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_m3u8(context, m, sem) for m in unique_list]
        results = await asyncio.gather(*tasks)

        # XUẤT FILE M3U CHUẨN
        playlist = "#EXTM3U\n"
        count = 0
        for r in results:
            if r and 'm3u8' in r:
                t_str = f"({r['time']}) " if r['time'] else ""
                b_str = f"{r['blv']} - " if r['blv'] else ""
                display_name = f"{t_str}{b_str}{r['teamA']} vs {r['teamB']}"
                
                # Xóa sạch rác
                display_name = re.sub(r'Xem ngay|Trực tiếp|Hot|Bóng đá|Live|Sắp diễn ra', '', display_name).strip()
                
                logo = r['logo'] if (r['logo'] and 'http' in r['logo']) else "https://socolivee.cv/logo.png"
                origin = TARGET_URL.rstrip('/')
                
                playlist += f'#EXTINF:-1 tvg-logo="{logo}", {display_name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{r["m3u8"]}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
            
        print(f"🎉 Đã gắp xong {count} trận bóng đá nét căng!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
