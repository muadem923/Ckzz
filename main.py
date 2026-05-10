import asyncio
from playwright.async_api import async_playwright
import re

# --- CẤU HÌNH ---
TARGET_URL = "https://socolivee.cv"
# 1. TĂNG TỐC: Cho phép mở 8 trận cùng lúc (GitHub gánh tốt)
CONCURRENCY_LIMIT = 8 
UA = "Mozilla/5.0_Windows_NT_10.0"

async def fetch_stream(context, match, sem):
    async with sem:
        print(f"-> Đang giăng lưới: {match['raw_title'][:40]}...")
        page = await context.new_page()
        
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script", "xhr", "fetch"] else route.abort())

        stream_urls = set()
        
        def handle_request(request):
            url = request.url
            if (".flv" in url or ".m3u8" in url) and "ad" not in url.lower() and "lulu" not in url.lower():
                stream_urls.add(url)

        page.on("request", handle_request)

        try:
            await page.goto(match['url'], wait_until="domcontentloaded", timeout=15000)
            
            blv_name = await page.evaluate("""() => {
                let el = document.querySelector('.blv-name, .player-info-name, .name-blv, .chat-item-name');
                return el ? el.innerText.trim() : '';
            }""")
            match['blv'] = blv_name
            
            if not match.get('logo'):
                room_logo = await page.evaluate("""() => {
                    let imgs = document.querySelectorAll('.team-logo img, .match-info img, .logo img');
                    for(let i of imgs) {
                        let src = i.getAttribute('data-src') || i.src || "";
                        if(src && !src.includes('base64') && !src.includes('icon') && !src.includes('.svg')) {
                            return src;
                        }
                    }
                    return "";
                }""")
                if room_logo:
                    match['logo'] = room_logo

            # 2. TĂNG TỐC: Giảm thời gian nằm vùng xuống còn 3.5 giây (Tiết kiệm cả đống thời gian)
            await page.wait_for_timeout(3500)
            
        except:
            pass
        finally:
            await page.close() 

        if stream_urls:
            match['stream_urls'] = list(stream_urls)
            return match
        return None

async def main():
    print("🥷 KHỞI ĐỘNG BẢN SIÊU TỐC & LỌC IDOL (.JPG)...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--mute-audio']
        )
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        
        await page.route("**/*", lambda route: route.continue_() if route.request.resource_type in ["document", "script"] else route.abort())

        print(f"👉 Đang thâm nhập Socolive: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print("❌ Lỗi mạng khi vào trang chủ.")
            await browser.close()
            return

        links = await page.evaluate("""() => {
            let items = [];
            document.querySelectorAll('a').forEach(a => {
                let href = a.href;
                if(href.includes('/truc-tiep/') || href.includes('/room/')) {
                    let text = a.innerText || "";
                    
                    if(!text.includes('Bóng rổ') && !text.includes('Tennis') && !text.includes('Cầu lông')) {
                        let logo = "";
                        let container = a.closest('div[class*="item"]') || a.closest('div');
                        if (container) {
                            let imgs = container.querySelectorAll('img');
                            for (let img of imgs) {
                                let src = img.getAttribute('data-src') || img.getAttribute('data-original') || img.src || "";
                                if(src && !src.includes('data:image') && !src.includes('base64') && !src.includes('icon') && !src.includes('gif')) {
                                    logo = src;
                                    break; 
                                }
                            }
                        }
                        
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

        print(f"✅ Tóm được {len(links)} trận. Bắt đầu dùng máy hút bụi tốc độ cao...")
        
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [fetch_stream(context, m, sem) for m in links]
        results = await asyncio.gather(*tasks)

        playlist = "#EXTM3U\n"
        count_matches = 0
        count_links = 0
        
        for res in results:
            if res and 'stream_urls' in res and len(res['stream_urls']) > 0:
                
                logo = res.get('logo', '')
                if logo:
                    if logo.startswith('//'): logo = 'https:' + logo
                    elif logo.startswith('/'): logo = 'https://socolivee.cv' + logo
                else:
                    logo = "https://socolivee.cv/logo.png"
                    
                # 3. LỌC IDOL/STREAMER CHÉM GIÓ: Hủy diệt các link có logo chứa .jpg hoặc .jpeg
                if ".jpg" in logo.lower() or ".jpeg" in logo.lower():
                    print(f"🚫 Đã loại bỏ phòng Idol: {res['raw_title']}")
                    continue
                
                # Bonus thêm 1 màng lọc: Nếu tên trận không có dấu "vs" thì khả năng cao cũng là rác
                if "vs" not in res['raw_title'].lower():
                    continue

                count_matches += 1
                time_match = re.search(r'(\d{2}:\d{2})', res['raw_title'])
                time_str = f"[{time_match.group(1)}] " if time_match else ""
                
                clean_name = re.sub(r'(?i)(Xem ngay|Trực tiếp|Hot|Live|Bóng đá|Sắp diễn ra|\d{2}:\d{2})', '', res['raw_title'])
                clean_name = re.sub(r'\s+', ' ', clean_name).strip()
                
                blv_str = f" [BLV {res['blv']}]" if res['blv'] else ""
                base_display_name = f"{time_str}{clean_name}{blv_str}"
                
                origin = TARGET_URL.rstrip('/')
                
                for idx, stream in enumerate(res['stream_urls']):
                    server_tag = f" (Luồng {idx + 1})" if len(res['stream_urls']) > 1 else ""
                    display_name = f"{base_display_name}{server_tag}"
                    
                    fixed_url = f"{stream}|Referer={TARGET_URL}/&Origin={origin}&User-Agent={UA}"
                    
                    playlist += f'#EXTINF:-1 group-title="Socolive" tvg-logo="{logo}", {display_name}\n'
                    playlist += f'#EXTVLCOPT:http-referer={TARGET_URL}/\n'
                    playlist += f'#EXTVLCOPT:http-user-agent={UA}\n'
                    playlist += f'{fixed_url}\n'
                    count_links += 1

        if count_links > 0:
            with open("socolive_live.m3u", "w", encoding="utf-8") as f:
                f.write(playlist)
            print(f"\n🎉 VÉT SẠCH VÀ LỌC BÓNG! Lấy được {count_links} link từ {count_matches} trận (Đã dọn sạch Idol JPG).")
        else:
            print("\n❌ Không bắt được luồng nào!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
