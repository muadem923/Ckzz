import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 5 
UA = "Mozilla/5.0_Windows_NT_10.0"

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
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=15000)
            # Lấy tên BLV
            blv = await page.evaluate("() => document.querySelector('.player-info-name, .blv-name')?.innerText || ''")
            match['blv'] = blv.strip()
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
    print("🚀 ĐANG QUÉT LẠI VỚI SIÊU RADAR...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
        except:
            await browser.close(); return

        # QUÉT TOÀN BỘ LINK TRẬN ĐẤU
        raw_matches = await page.evaluate("""() => {
            let res = [];
            document.querySelectorAll('a').forEach(a => {
                let h = a.href;
                let t = a.innerText || "";
                if((h.includes('/truc-tiep/') || h.includes('/room/')) && t.length > 10) {
                    // Lọc bỏ môn khác ngay từ đầu
                    if(!t.includes('Bóng rổ') && !t.includes('Tennis') && !t.includes('Cầu lông')) {
                        res.push({url: h, raw_text: t, logo: a.querySelector('img')?.src || ""});
                    }
                }
            });
            return res;
        }""")
        await page.close()

        if not raw_matches:
            print("❌ Không quét được trận nào!"); await browser.close(); return

        # XỬ LÝ CHUẨN HÓA TÊN TRẬN
        final_list = []
        seen_urls = set()
        for rm in raw_matches:
            if rm['url'] in seen_urls: continue
            seen_urls.add(rm['url'])
            
            txt = rm['raw_text'].replace('\n', ' ')
            # Tách lấy giờ (ví dụ: 18:00)
            time_search = re.search(r'(\d{2}:\d{2})', txt)
            match_time = time_search.group(1) if time_search else ""
            
            # Dọn rác
            clean_name = re.sub(r'Xem ngay|Trực tiếp|Hot|Bóng đá|Live|Sắp diễn ra|\d{2}:\d{2}', '', txt)
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            
            rm['display_name_base'] = clean_name
            rm['match_time'] = match_time
            final_list.append(rm)

        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_m3u8(context, m, sem) for m in final_list]
        results = await asyncio.gather(*tasks)

        # XUẤT FILE
        playlist = "#EXTM3U\n"
        count = 0
        for r in results:
            if r and 'm3u8' in r:
                t_part = f"({r['match_time']}) " if r['match_time'] else ""
                b_part = f"{r['blv']} - " if r['blv'] else ""
                name = f"{t_part}{b_part}{r['display_name_base']}"
                
                logo = r['logo'] if r['logo'] else "https://socolivee.cv/logo.png"
                origin = TARGET_URL.rstrip('/')
                
                playlist += f'#EXTINF:-1 tvg-logo="{logo}", {name}\n'
                playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                playlist += f'{r["m3u8"]}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
        print(f"🎉 XONG! Đã hốt {count} trận bóng đá.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
