import asyncio
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import BrowserContext, Page, Route, async_playwright


# =========================
# CẤU HÌNH
# =========================
TARGET_URL = "https://socolivem.cv/"
OUTPUT_M3U = "socolive_live.m3u"
OUTPUT_DEBUG = "socolive_debug.json"

CONCURRENCY_LIMIT = 8
HOME_WAIT_MS = 5000
STREAM_WAIT_SECONDS = 20
EXTRA_WAIT_AFTER_FIRST_STREAM = 2.0

HEADLESS = True

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

STREAM_EXTENSIONS = (".m3u8", ".flv")
AD_MARKERS = (
    "doubleclick.",
    "googleads.",
    "/ads/",
    "/advert",
    "imasdk",
)

PLAY_SELECTORS = (
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".play-button",
    ".btn-play",
    "button[aria-label*='Play' i]",
    "button[title*='Play' i]",
    "[class*='play'][role='button']",
)


def is_stream_url(url: str) -> bool:
    """Trả về True nếu URL có vẻ là luồng HLS/FLV thật."""
    if not url:
        return False

    clean = html.unescape(url).replace("\\/", "/").strip()
    lower = clean.lower()

    if not any(ext in lower for ext in STREAM_EXTENSIONS):
        return False

    return not any(marker in lower for marker in AD_MARKERS)


def normalize_stream_url(url: str) -> str:
    return html.unescape(url).replace("\\/", "/").strip()


def derive_match_info(url: str, raw_title: str = "") -> tuple[str, str, str]:
    """
    Trả về:
      - tên trận
      - giờ trận
      - tên BLV suy đoán
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    raw_title = re.sub(r"\s+", " ", raw_title or "").strip()
    raw_lower = raw_title.lower()

    has_match_title = " vs " in f" {raw_lower} "
    blv_name = ""

    if "blv" in query and raw_title and not has_match_title:
        blv_name = raw_title

    if has_match_title:
        match_name = raw_title
    else:
        slug = unquote(parsed.path.rstrip("/").split("/")[-1])
        slug = re.sub(r"-\d{2}-\d{2}-\d{4}-\d{4}$", "", slug)
        slug = slug.replace("-vs-", " vs ")
        slug = slug.replace("-", " ")
        match_name = re.sub(r"\s+", " ", slug).strip()

    match_name = re.sub(
        r"(?i)\b(xem ngay|trực tiếp|hot|live|bóng đá|sắp diễn ra|socolive)\b",
        "",
        match_name,
    )
    match_name = re.sub(r"\s+", " ", match_name).strip(" -")

    time_match = re.search(r"-(\d{2})(\d{2})/?$", parsed.path)
    time_str = f"{time_match.group(1)}:{time_match.group(2)}" if time_match else ""

    return match_name or raw_title or url, time_str, blv_name


async def install_route_filter(page: Page, homepage: bool = False) -> None:
    """
    Trang trận KHÔNG chặn media/manifest/other.
    Chỉ chặn ảnh và font để giảm tải.
    Trang chủ có thể chặn media vì chỉ cần lấy danh sách link.
    """
    blocked_types = {"image", "font"}
    if homepage:
        blocked_types.add("media")

    async def route_handler(route: Route) -> None:
        if route.request.resource_type in blocked_types:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)


async def collect_dom_stream_candidates(page: Page) -> list[str]:
    """
    Bắt thêm URL từ:
      - Performance Resource Timing
      - src/currentSrc của video/source
      - HTML/script đã render
    """
    try:
        candidates = await page.evaluate(
            r"""() => {
                const out = new Set();

                try {
                    for (const entry of performance.getEntriesByType("resource")) {
                        if (entry && entry.name) out.add(entry.name);
                    }
                } catch (_) {}

                document.querySelectorAll("video, source").forEach((el) => {
                    const values = [
                        el.src,
                        el.currentSrc,
                        el.getAttribute("src"),
                        el.getAttribute("data-src"),
                        el.getAttribute("data-url"),
                        el.getAttribute("data-stream"),
                    ];
                    values.forEach((value) => {
                        if (value) out.add(value);
                    });
                });

                const htmlText = document.documentElement
                    ? document.documentElement.innerHTML
                    : "";

                const absoluteMatches =
                    htmlText.match(/https?:\/\/[^"' <>\n\r]+?(?:\.m3u8|\.flv)(?:\?[^"' <>\n\r]*)?/gi) || [];

                absoluteMatches.forEach((value) => out.add(value));

                return Array.from(out);
            }"""
        )
        return [str(item) for item in candidates if item]
    except Exception:
        return []


async def stimulate_player(page: Page) -> None:
    """Thử kích hoạt player/autoplay mà không làm hỏng trang."""
    for selector in PLAY_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count():
                await locator.first.click(timeout=700, force=True)
        except Exception:
            pass

    try:
        await page.evaluate(
            """() => {
                document.querySelectorAll("video").forEach((video) => {
                    try {
                        video.muted = true;
                        video.volume = 0;
                        const result = video.play();
                        if (result && typeof result.catch === "function") {
                            result.catch(() => {});
                        }
                    } catch (_) {}
                });
            }"""
        )
    except Exception:
        pass


async def fetch_stream(
    context: BrowserContext,
    match: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        match_name, time_str, blv_from_link = derive_match_info(
            match["url"], match.get("raw_title", "")
        )
        match["match_name"] = match_name
        match["time"] = time_str
        match["blv"] = blv_from_link
        match["stream_urls"] = []
        match["errors"] = []

        label = match_name
        if blv_from_link:
            label += f" | BLV {blv_from_link}"

        print(f"-> Đang quét: {label[:90]}")

        page = await context.new_page()
        await install_route_filter(page, homepage=False)

        stream_urls: set[str] = set()
        first_stream_at: float | None = None

        def capture_url(url: str, source: str) -> None:
            nonlocal first_stream_at

            normalized = normalize_stream_url(url)
            if not is_stream_url(normalized):
                return

            if normalized not in stream_urls:
                stream_urls.add(normalized)
                if first_stream_at is None:
                    first_stream_at = time.monotonic()
                print(f"   🎯 [{source}] {normalized}")

        def handle_request(request: Any) -> None:
            capture_url(request.url, f"request/{request.resource_type}")

        def handle_response(response: Any) -> None:
            capture_url(response.url, "response")

        def handle_page_error(error: Any) -> None:
            message = f"JS: {error}"
            match["errors"].append(message)

        def handle_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                text = str(message.text)
                if len(text) <= 500:
                    match["errors"].append(f"console/{message.type}: {text}")

        page.on("request", handle_request)
        page.on("response", handle_response)
        page.on("pageerror", handle_page_error)
        page.on("console", handle_console)

        try:
            await page.goto(
                match["url"],
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Thử đọc tên BLV thật trên trang.
            try:
                dom_blv = await page.evaluate(
                    """() => {
                        const selectors = [
                            ".blv-name",
                            ".player-info-name",
                            ".name-blv",
                            ".chat-item-name",
                            "[class*='blv'] [class*='name']",
                            "[class*='commentator'] [class*='name']"
                        ];

                        for (const selector of selectors) {
                            const el = document.querySelector(selector);
                            if (el && el.innerText && el.innerText.trim()) {
                                return el.innerText.trim();
                            }
                        }
                        return "";
                    }"""
                )
                if dom_blv:
                    match["blv"] = re.sub(r"\s+", " ", dom_blv).strip()
            except Exception:
                pass

            # Logo chỉ dùng để hiển thị; đuôi JPG không còn bị coi là phòng rác.
            if not match.get("logo"):
                try:
                    room_logo = await page.evaluate(
                        """() => {
                            const imgs = document.querySelectorAll(
                                ".team-logo img, .match-info img, .logo img, img"
                            );

                            for (const img of imgs) {
                                const src =
                                    img.getAttribute("data-src") ||
                                    img.getAttribute("data-original") ||
                                    img.getAttribute("src") ||
                                    img.src ||
                                    "";

                                if (
                                    src &&
                                    !src.includes("base64") &&
                                    !src.includes("data:image") &&
                                    !src.includes("icon") &&
                                    !src.includes(".svg") &&
                                    !src.includes(".gif")
                                ) {
                                    return src;
                                }
                            }
                            return "";
                        }"""
                    )
                    if room_logo:
                        match["logo"] = room_logo
                except Exception:
                    pass

            deadline = time.monotonic() + STREAM_WAIT_SECONDS

            while time.monotonic() < deadline:
                await stimulate_player(page)

                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(candidate, "dom/performance")

                if (
                    first_stream_at is not None
                    and time.monotonic() - first_stream_at
                    >= EXTRA_WAIT_AFTER_FIRST_STREAM
                ):
                    break

                await page.wait_for_timeout(1000)

        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            match["errors"].append(error_text)
            print(f"   ❌ {label[:70]} | {error_text}")
        finally:
            # Quét lần cuối trước khi đóng trang.
            try:
                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(candidate, "final-scan")
            except Exception:
                pass

            await page.close()

        match["stream_urls"] = sorted(stream_urls)

        if not stream_urls:
            print(f"   ⚠️ Không thấy m3u8/flv: {label[:85]}")

        return match


async def collect_home_links(context: BrowserContext) -> list[dict[str, str]]:
    page = await context.new_page()
    await install_route_filter(page, homepage=True)

    print(f"👉 Đang mở trang chủ: {TARGET_URL}")

    try:
        await page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(HOME_WAIT_MS)

        # Cuộn nhẹ để các danh sách lazy-load được render.
        for _ in range(4):
            await page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight));")
            await page.wait_for_timeout(500)

        links = await page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();

                document.querySelectorAll("a").forEach((a) => {
                    const href = a.href || "";
                    if (
                        !href.includes("/truc-tiep/") &&
                        !href.includes("/room/")
                    ) {
                        return;
                    }

                    const text = (a.innerText || "").replace(/\s+/g, " ").trim();
                    const lowerText = text.toLowerCase();

                    if (
                        lowerText.includes("bóng rổ") ||
                        lowerText.includes("tennis") ||
                        lowerText.includes("cầu lông")
                    ) {
                        return;
                    }

                    if (seen.has(href)) return;
                    seen.add(href);

                    let logo = "";
                    const container =
                        a.closest("div[class*='item']") ||
                        a.closest("article") ||
                        a.closest("li") ||
                        a.closest("div");

                    if (container) {
                        const imgs = container.querySelectorAll("img");
                        for (const img of imgs) {
                            const src =
                                img.getAttribute("data-src") ||
                                img.getAttribute("data-original") ||
                                img.getAttribute("src") ||
                                img.src ||
                                "";

                            if (
                                src &&
                                !src.includes("data:image") &&
                                !src.includes("base64") &&
                                !src.includes("icon") &&
                                !src.includes("gif")
                            ) {
                                logo = src;
                                break;
                            }
                        }
                    }

                    const title =
                        a.title ||
                        a.getAttribute("aria-label") ||
                        text ||
                        href;

                    items.push({
                        url: href,
                        raw_title: title.replace(/\s+/g, " ").trim(),
                        logo: logo,
                    });
                });

                return items;
            }"""
        )

        return list(links)

    except Exception as exc:
        print(f"❌ Không lấy được danh sách trang chủ: {type(exc).__name__}: {exc}")
        return []
    finally:
        await page.close()


def normalize_logo(logo: str) -> str:
    logo = (logo or "").strip()

    if not logo:
        return TARGET_URL.rstrip("/") + "/logo.png"

    if logo.startswith("//"):
        return "https:" + logo

    if logo.startswith("/"):
        return TARGET_URL.rstrip("/") + logo

    return logo


def escape_m3u_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value or "").replace('"', "'").strip()


def write_outputs(results: list[dict[str, Any]]) -> tuple[int, int]:
    playlist_lines = ["#EXTM3U"]
    written_streams: set[str] = set()
    match_keys_with_streams: set[str] = set()

    referer_origin = TARGET_URL.rstrip("/")
    count_links = 0

    for result in results:
        streams = result.get("stream_urls") or []
        if not streams:
            continue

        match_name = result.get("match_name") or result.get("raw_title") or "Socolive"
        time_str = result.get("time") or ""
        blv = result.get("blv") or ""
        logo = normalize_logo(result.get("logo", ""))
        page_referer = result.get("url") or TARGET_URL

        display_base = f"[{time_str}] {match_name}" if time_str else match_name
        if blv and blv.lower() not in display_base.lower():
            display_base += f" [BLV {blv}]"

        display_base = escape_m3u_text(display_base)
        logo = escape_m3u_text(logo)

        unique_streams = [
            stream for stream in streams
            if stream not in written_streams
        ]

        if not unique_streams:
            continue

        match_key = f"{match_name}|{blv}|{time_str}"
        match_keys_with_streams.add(match_key)

        for index, stream in enumerate(unique_streams, start=1):
            written_streams.add(stream)

            server_tag = (
                f" (Luồng {index})"
                if len(unique_streams) > 1
                else ""
            )
            display_name = display_base + server_tag

            fixed_url = (
                f"{stream}"
                f"|Referer={page_referer}"
                f"&Origin={referer_origin}"
                f"&User-Agent={UA}"
            )

            playlist_lines.append(
                f'#EXTINF:-1 group-title="Socolive" tvg-logo="{logo}",'
                f'{display_name}'
            )
            playlist_lines.append(
                f"#EXTVLCOPT:http-referrer={page_referer}"
            )
            playlist_lines.append(
                f"#EXTVLCOPT:http-referer={page_referer}"
            )
            playlist_lines.append(
                f"#EXTVLCOPT:http-origin={referer_origin}"
            )
            playlist_lines.append(
                f"#EXTVLCOPT:http-user-agent={UA}"
            )
            playlist_lines.append(fixed_url)

            count_links += 1

    Path(OUTPUT_DEBUG).write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if count_links:
        Path(OUTPUT_M3U).write_text(
            "\n".join(playlist_lines) + "\n",
            encoding="utf-8",
        )

    return len(match_keys_with_streams), count_links


async def main() -> None:
    print("🥷 KHỞI ĐỘNG SOCOLIVE STREAM SCANNER - BẢN FIX")
    print(
        "ℹ️ Có thể test riêng một trận bằng lệnh:\n"
        '   python main_socolive_fixed.py "https://socolivem.cv/truc-tiep/.../?blv=..."'
    )

    direct_urls = [
        arg.strip()
        for arg in sys.argv[1:]
        if arg.strip().startswith(("http://", "https://"))
    ]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=UA,
            locale="vi-VN",
            ignore_https_errors=True,
            service_workers="block",
            extra_http_headers={
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        )

        if direct_urls:
            links = []
            for url in direct_urls:
                match_name, _, _ = derive_match_info(url)
                links.append(
                    {
                        "url": url,
                        "raw_title": match_name,
                        "logo": "",
                    }
                )
            print(f"✅ Chế độ test trực tiếp: {len(links)} URL.")
        else:
            links = await collect_home_links(context)

        if not links:
            print("❌ Không tìm thấy link trận/phòng nào.")
            await context.close()
            await browser.close()
            return

        print(
            f"✅ Tìm thấy {len(links)} link trận/phòng. "
            f"Bắt đầu quét tối đa {CONCURRENCY_LIMIT} trang cùng lúc..."
        )

        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        tasks = [
            fetch_stream(context, match, semaphore)
            for match in links
        ]
        results = await asyncio.gather(*tasks)

        count_matches, count_links = write_outputs(results)

        if count_links:
            print(
                f"\n🎉 HOÀN TẤT: lấy được {count_links} link "
                f"từ {count_matches} trận/phòng."
            )
            print(f"📺 Playlist: {Path(OUTPUT_M3U).resolve()}")
        else:
            print("\n❌ Không bắt được m3u8/flv nào.")

        print(f"🧾 Nhật ký chi tiết: {Path(OUTPUT_DEBUG).resolve()}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
