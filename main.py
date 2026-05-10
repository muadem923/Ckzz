import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 5 
UA = "Mozilla/5.0_Windows_NT_10.0"

async def fetch_m3u8(context, match, sem):
    """Nhiệm vụ: Mở trận, lấy link M3U8 và tên BLV"""
    async with sem:
        page = await context.new_page()
        # Chặn rác tối đa để tăng tốc
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

        m3u8_url = None
        
        def handle_request(request):
            nonlocal m3u8_url
            url = request.url
            if ".m3u8" in url and "ad" not in url.lower() and "lulu" not in url.lower():
                m3u8_url = url

        page.on("request", handle_request)

        try:
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=15000)
            
            # Lấy tên BLV đang đọc trận này (nếu có)
            blv_name = await page.evaluate("""() => {
                let el = document.querySelector('.player-info-name, .blv-name, .name-blv');
                return el ? el.innerText.trim() : "";
            }""")
            match['blv'] = blv_name
            
            for _ in range(15):
                if m3u8_url: break
                await page.wait_for_timeout(500)
        except: pass
        finally: await page.close()

        if m3u8_url:
            match['m3u8'] = m3u8_url
            return match
        return None

async def main():
    print("🥷 ĐANG TINH CHẾ FILE M3U - CHỈ GIỮ LẠI BÓNG ĐÁ...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--mute-audio'])
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script"] else route.abort())

        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        except:
            await browser.close(); return

        # QUÉT TRẬN ĐẤU VÀ LỌC CHỈ LẤY BÓNG ĐÁ
        matches_data = await page.evaluate("""() => {
            let items = [];
            // Tìm tất cả các khung chứa trận đấu
            document.querySelectorAll('.match-item, .item-match, a').forEach(el => {
                let href = el.href || "";
                let text = el.innerText || "";
                
                // LỌC: Chỉ lấy bóng đá và đúng link trận đấu
                if((href.includes('/truc-tiep/') || href.includes('/room/')) && !text.includes('Bóng rổ') && !text.includes('Tennis')) {
                    
                    // Lấy thời gian
                    let time = el.querySelector('.time, .match-time')?.innerText || "";
                    
                    // Lấy logo đội bóng
                    let logo = el.querySelector('img')?.src || "";
                    
                    // Lấy tên hai đội
                    let teams = el.querySelectorAll('.team-name, .name');
                    let teamA = teams[0]?.innerText.trim() || "";
                    let teamB = teams[1]?.innerText.trim() || "";
                    
                    if(teamA && teamB) {
                        items.push({
                            url: href,
                            time: time.trim(),
                            logo: logo,
                            teamA: teamA,
                            teamB: teamB
                        });
                    }
                }
            });
            return items;
        }""")

        await page.close()
        
        if not matches_data:
            print("❌ Không tìm thấy trận bóng đá nào!"); await browser.close(); return

        # Loại bỏ trùng lặp link
        unique_matches = {m['url']: m for m in matches_data}.values()

        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_m3u8(context, m, sem) for m in unique_matches]
        results = await asyncio.gather(*tasks)

        # XUẤT FILE M3U
        playlist = "#EXTM3U\n"
        count = 0
        for res in results:
            if res and 'm3u8' in res:
                # Định dạng tên: (Giờ) BLV - Đội A vs Đội B
                blv_part = f"{res['blv']} - " if res['blv'] else ""
                time_part = f"({res['time']}) " if res['time'] else ""
                display_name = f"{time_part}{blv_part}{res['teamA']} vs {res['teamB']}"
                
                # Làm sạch các từ thừa nếu còn sót
                display_name = re.sub(r'Xem ngay|Trực tiếp|Hot|Bóng đá|Live', '', display_name).strip()
                
                origin = TARGET_URL.rstrip('/')
                fixed_url = f"{res['m3u8']}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}"
                
                playlist += f'#EXTINF:-1 tvg-logo="{res["logo"]}", {display_name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{fixed_url}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
        
        print(f"🎉 Đã lọc xong {count} trận bóng đá xịn xò!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
