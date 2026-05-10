import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
CONCURRENCY_LIMIT = 4 # Giảm xuống 4 để máy chủ tập trung "soi" kỹ hơn
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

async def fetch_video_stream(context, match, sem):
    async with sem:
        page = await context.new_page()
        # Chặn quảng cáo nhưng phải cho phép các luồng stream chạy
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch", "websocket"] else route.abort())

        stream_url = None
        
        def handle_request(request):
            nonlocal stream_url
            url = request.url
            # RADAR MỚI: Săn cả .flv và .m3u8 (giống link bác đưa)
            if (".flv" in url or ".m3u8" in url) and "ad" not in url.lower() and "lulu" not in url.lower():
                stream_url = url

        page.on("request", handle_request)

        try:
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=25000)
            
            # CHIÊU MỚI: Tự động bấm "Play" nếu trình duyệt không tự chạy để kích hoạt link
            try:
                await page.click('button[class*="play"], .vjs-big-play-button', timeout=3000)
            except: pass

            # Chờ đợi kiên nhẫn hơn để bắt luồng video
            for _ in range(20): 
                if stream_url: break
                await page.wait_for_timeout(1000)
                
            # Lấy tên BLV
            blv = await page.evaluate("() => document.querySelector('.player-info-name, .blv-name, .chat-item-name')?.innerText || ''")
            match['blv'] = blv.strip()
        except: pass
        finally: await page.close()

        if stream_url:
            match['stream_url'] = stream_url
            return match
        return None

async def main():
    print("🚀 ĐANG KHỞI CHẠY RADAR TỔNG LỰC (SĂN CẢ .FLV VÀ .M3U8)...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        
        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            # Cuộn để load trận đấu
            for _ in range(4):
                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(1000)
        except:
            await browser.close(); return

        # QUÉT TRẬN ĐẤU (Bóng đá)
        matches_data = await page.evaluate("""() => {
            let res = [];
            document.querySelectorAll('a').forEach(el => {
                let href = el.href;
                if((href.includes('/truc-tiep/') || href.includes('/room/'))) {
                    let container = el.closest('div[class*="item"], div[class*="match"]') || el;
                    let text = container.innerText || "";
                    if(text.includes('Bóng rổ') || text.includes('Tennis')) return;

                    let time = text.match(/(\\d{2}:\\d{2})/) ? text.match(/(\\d{2}:\\d{2})/)[1] : "";
                    let img = container.querySelector('img');
                    let logo = img ? (img.src || img.getAttribute('data-src')) : "";
                    
                    // Lấy tên đội
                    let clean = text.replace(/Xem ngay|Trực tiếp|Hot|Live|\\d{2}:\\d{2}/g, '').trim();
                    let parts = clean.split(/\\n|vs|\\-/);
                    let teamA = parts[0]?.trim() || "Team A";
                    let teamB = parts[parts.length - 1]?.trim() || "Team B";

                    res.push({ url: href, time: time, logo: logo, teamA: teamA, teamB: teamB });
                }
            });
            return res;
        }""")
        await page.close()

        if not matches_data:
            print("❌ Không thấy trận nào!"); await browser.close(); return

        # Chạy đa luồng săn link video
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_video_stream(context, m, sem) for m in matches_data]
        results = await asyncio.gather(*tasks)

        # XUẤT FILE M3U
        playlist = "#EXTM3U\n"
        count = 0
        for r in results:
            if r and 'stream_url' in r:
                t_str = f"({r['time']}) " if r['time'] else ""
                b_str = f"{r['blv']} - " if r['blv'] else ""
                display_name = f"{t_str}{b_str}{r['teamA']} vs {r['teamB']}"
                
                logo = r['logo'] if (r['logo'] and 'http' in r['logo']) else "https://socolivee.cv/logo.png"
                origin = "https://socolivee.cv"
                
                playlist += f'#EXTINF:-1 tvg-logo="{logo}", {display_name}\n'
                playlist += f'#EXTVLCOPT:http-referer={origin}/\n'
                playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                # Ép chặt Auth cho cả FLV và M3U8
                playlist += f'{r["stream_url"]}|Referer={origin}/&Origin={origin}&User-Agent={UA}\n'
                count += 1

        with open("socolive_live.m3u", "w", encoding="utf-8") as f:
            f.write(playlist)
            
        print(f"🎉 XONG! Đã hốt {count} luồng (bao gồm cả FLV).")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
