import asyncio
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, async_playwright


# ============================================================
# CẤU HÌNH
# ============================================================
TARGET_URL = "https://socolivem.cv/"

OUTPUT_M3U = "socolive_live.m3u"
OUTPUT_DEBUG = "socolive_debug.json"
OUTPUT_MATCHES = "socolive_matches.json"

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

# Cơ chế cân bằng: 4 player song song, không quá tải như 8 và không chậm như 1.
MATCH_CONCURRENCY = int(os.getenv("SOCOLIVE_MATCH_CONCURRENCY", "4"))

# Mỗi phòng chỉ được chờ ngắn. Có stream thì đóng gần như ngay lập tức.
ROOM_WAIT_SECONDS = float(os.getenv("SOCOLIVE_ROOM_WAIT_SECONDS", "8"))
EXTRA_WAIT_AFTER_STREAM = float(
    os.getenv("SOCOLIVE_EXTRA_WAIT_AFTER_STREAM", "0.8")
)
DELAY_BETWEEN_ROOMS = float(
    os.getenv("SOCOLIVE_DELAY_BETWEEN_ROOMS", "0.15")
)

# Thử HTTP nhẹ trước khi mở Chromium. Nếu HTML/config đã chứa stream thì xong ngay.
HTTP_FIRST = os.getenv("SOCOLIVE_HTTP_FIRST", "0") != "0"
HTTP_FETCH_TIMEOUT_SECONDS = float(
    os.getenv("SOCOLIVE_HTTP_FETCH_TIMEOUT_SECONDS", "5")
)
# CDP tạo thêm nhiều sự kiện trùng; mặc định tắt, chỉ bật khi cần debug sâu.
ENABLE_CDP = os.getenv("SOCOLIVE_ENABLE_CDP", "1") == "1"

# Chỉ quét các trận trong khoảng thời gian hợp lý để tránh mở hàng trăm player.
SCAN_BEFORE_MINUTES = int(
    os.getenv("SOCOLIVE_SCAN_BEFORE_MINUTES", "300")
)
SCAN_AFTER_MINUTES = int(
    os.getenv("SOCOLIVE_SCAN_AFTER_MINUTES", "90")
)
SCAN_ALL = os.getenv("SOCOLIVE_SCAN_ALL", "0") == "1"

# Mặc định chỉ cần một link tốt/trận và thử tối đa 3 phòng BLV.
MAX_SUCCESSFUL_ROOMS_PER_MATCH = int(
    os.getenv("SOCOLIVE_MAX_SUCCESSFUL_ROOMS_PER_MATCH", "1")
)
MAX_ROOMS_PER_MATCH = int(
    os.getenv("SOCOLIVE_MAX_ROOMS_PER_MATCH", "3")
)

# Phòng BLV thứ hai/ba được chờ lâu hơn vì nhiều player khởi tạo chậm.
RETRY_ROOM_WAIT_SECONDS = float(
    os.getenv("SOCOLIVE_RETRY_ROOM_WAIT_SECONDS", "14")
)

# Chạy theo từng lô thay vì giữ 4 tab hoạt động liên tục suốt cả danh sách.
BATCH_COOLDOWN_SECONDS = float(
    os.getenv("SOCOLIVE_BATCH_COOLDOWN_SECONDS", "2")
)
FAILED_BATCH_COOLDOWN_SECONDS = float(
    os.getenv("SOCOLIVE_FAILED_BATCH_COOLDOWN_SECONDS", "8")
)

# Lưu bằng chứng cho các phòng thất bại.
FAILURE_DIR = Path(
    os.getenv("SOCOLIVE_FAILURE_DIR", "socolive_failures")
)
SAVE_FAILURE_ARTIFACTS = (
    os.getenv("SOCOLIVE_SAVE_FAILURE_ARTIFACTS", "1") != "0"
)

HEADLESS = os.getenv("SOCOLIVE_HEADLESS", "1") != "0"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

STREAM_SUFFIXES = (".m3u8", ".flv")

PLAY_SELECTORS = (
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".jwplayer",
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    ".play-button",
    ".btn-play",
    "button[aria-label*='Play' i]",
    "button[title*='Play' i]",
    "[class*='play'][role='button']",
)

LIVE_STATUS_WORDS = (
    "đang diễn ra",
    "trực tiếp",
    "live",
    "hiệp 1",
    "hiệp 2",
    "đang đá",
)


# ============================================================
# URL / TEXT HELPERS
# ============================================================
def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_match_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def room_blv_id(url: str) -> str:
    try:
        return parse_qs(urlsplit(url).query).get("blv", [""])[0]
    except Exception:
        return ""


def normalize_absolute_url(url: str, base: str = TARGET_URL) -> str:
    value = clean_text(url)
    if not value:
        return ""

    if value.startswith("//"):
        return "https:" + value

    if value.startswith("/"):
        root = urlsplit(base)
        return f"{root.scheme}://{root.netloc}{value}"

    return value


def match_datetime_from_url(url: str) -> datetime | None:
    """
    Ví dụ:
    /manchester-united-vs-wrexham-18-07-2026-2200/
    """
    path = urlsplit(url).path.rstrip("/")
    matched = re.search(
        r"-(\d{2})-(\d{2})-(\d{4})-(\d{2})(\d{2})$",
        path,
    )
    if not matched:
        return None

    day, month, year, hour, minute = map(int, matched.groups())

    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=TIMEZONE,
        )
    except ValueError:
        return None


def match_name_from_url(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    slug = unquote(slug)
    slug = re.sub(
        r"-\d{2}-\d{2}-\d{4}-\d{4}$",
        "",
        slug,
    )
    slug = slug.replace("-vs-", " vs ").replace("-", " ")
    return clean_text(slug)


def parse_teams_from_title(title: str) -> tuple[str, str]:
    text = clean_text(title)

    text = re.sub(
        r"(?i)^.*?(?:socolive\s+)?",
        "",
        text,
        count=1,
    ) if False else text

    # Bỏ phần mở đầu và phần giờ/ngày.
    text = re.sub(
        r"(?i)^(?:xem\s+)?trực\s+tiếp\s+bóng\s+đá\s+(?:socolive\s+)?",
        "",
        text,
    )
    text = re.sub(
        r"(?i)\s*(?:\||lúc)\s*\d{1,2}:\d{2}.*$",
        "",
        text,
    )
    text = clean_text(text)

    matched = re.search(r"(.+?)\s+vs\s+(.+)", text, flags=re.I)
    if not matched:
        return "", ""

    return clean_text(matched.group(1)), clean_text(matched.group(2))


def match_display_name(match: dict[str, Any]) -> str:
    home = clean_text(match.get("home_team", ""))
    away = clean_text(match.get("away_team", ""))

    if home and away:
        return f"{home} vs {away}"

    name = clean_text(match.get("match_name", ""))
    if name:
        return name

    return match_name_from_url(match.get("base_url", ""))


def is_direct_stream_url(value: str) -> bool:
    """
    Chỉ coi là stream khi chính PATH của URL kết thúc bằng .m3u8/.flv.
    Vì ping.gif?...&mu=https://...m3u8 KHÔNG phải stream.
    """
    try:
        parsed = urlsplit(value)
    except Exception:
        return False

    path = parsed.path.lower().rstrip("/")
    return path.endswith(STREAM_SUFFIXES)


def clean_candidate_url(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\\/", "/")
    value = value.strip(" \t\r\n\"'`()[]{}<>,;")
    return value


def extract_stream_urls_from_value(value: str) -> set[str]:
    """
    Bắt được:
      1. URL m3u8/flv trực tiếp.
      2. URL m3u8 nằm trong query telemetry, ví dụ:
         ping.gif?...&mu=https%3A%2F%2F...m3u8%3F...
    Nhưng tuyệt đối không lưu ping.gif làm stream.
    """
    found: set[str] = set()
    pending: list[str] = [value or ""]
    seen: set[str] = set()

    for _ in range(5):
        if not pending:
            break

        next_pending: list[str] = []

        for raw in pending:
            decoded = clean_candidate_url(raw)
            if not decoded or decoded in seen:
                continue

            seen.add(decoded)

            # Giải mã URL lồng tối đa vài lớp.
            variants = [decoded]
            try:
                unquoted = clean_candidate_url(unquote(decoded))
                if unquoted and unquoted != decoded:
                    variants.append(unquoted)
            except Exception:
                pass

            for candidate_text in variants:
                if is_direct_stream_url(candidate_text):
                    found.add(candidate_text)

                # Tìm URL tuyệt đối trong HTML/script/query đã giải mã.
                absolute_urls = re.findall(
                    r"https?://[^\s\"'<>]+",
                    candidate_text,
                    flags=re.I,
                )

                for absolute in absolute_urls:
                    absolute = clean_candidate_url(absolute)
                    if is_direct_stream_url(absolute):
                        found.add(absolute)
                    next_pending.append(absolute)

                # Duyệt các tham số query như mu, file, src, stream...
                try:
                    parsed = urlsplit(candidate_text)
                    query = parse_qs(
                        parsed.query,
                        keep_blank_values=True,
                    )
                    for values in query.values():
                        for item in values:
                            if item:
                                next_pending.append(item)
                except Exception:
                    pass

        pending = next_pending

    # Nếu cùng một đường dẫn stream xuất hiện ở cả dạng thiếu và đủ
    # query ký số, giữ bản dài nhất/đầy đủ nhất.
    best_by_path: dict[tuple[str, str, str], str] = {}

    for item in found:
        cleaned = clean_candidate_url(item)
        if not is_direct_stream_url(cleaned):
            continue

        parsed = urlsplit(cleaned)
        key = (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
        )

        current = best_by_path.get(key, "")
        if len(cleaned) > len(current):
            best_by_path[key] = cleaned

    return set(best_by_path.values())


def unique_rooms(rooms: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    with_blv: list[dict[str, str]] = []
    base_rooms: list[dict[str, str]] = []

    for room in rooms:
        url = clean_text(room.get("url", ""))
        if not url or url in seen:
            continue

        seen.add(url)
        item = {
            "url": url,
            "blv": clean_text(room.get("blv", "")),
            "blv_id": clean_text(room.get("blv_id", ""))
            or room_blv_id(url),
        }

        if item["blv_id"]:
            with_blv.append(item)
        else:
            base_rooms.append(item)

    # Giữ nguyên thứ tự BLV trên trang. URL tổng chỉ dùng làm fallback sau cùng.
    return with_blv + base_rooms


# ============================================================
# LẤY DANH SÁCH TRẬN + LOGO
# ============================================================
async def collect_home_matches(
    context: BrowserContext,
) -> list[dict[str, Any]]:
    page = await context.new_page()

    try:
        print(f"👉 Đang mở trang chủ: {TARGET_URL}")

        await page.goto(
            TARGET_URL,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await page.wait_for_timeout(5000)

        # Cuộn để render các card lazy-load.
        for _ in range(6):
            await page.evaluate(
                "window.scrollBy(0, Math.max(700, window.innerHeight));"
            )
            await page.wait_for_timeout(450)

        raw_matches = await page.evaluate(
            r"""() => {
                const clean = (value) =>
                    (value || "").replace(/\s+/g, " ").trim();

                const absoluteImage = (img) => {
                    if (!img) return "";

                    let src =
                        img.currentSrc ||
                        img.getAttribute("data-src") ||
                        img.getAttribute("data-lazy-src") ||
                        img.getAttribute("data-original") ||
                        img.getAttribute("src") ||
                        "";

                    if (!src) {
                        const srcset =
                            img.getAttribute("data-srcset") ||
                            img.getAttribute("srcset") ||
                            "";
                        if (srcset) {
                            src = srcset
                                .split(",")[0]
                                .trim()
                                .split(/\s+/)[0];
                        }
                    }

                    try {
                        return src ? new URL(src, location.href).href : "";
                    } catch (_) {
                        return src || "";
                    }
                };

                const canonical = (href) => {
                    try {
                        const u = new URL(href, location.href);
                        let path = u.pathname;
                        if (!path.endsWith("/")) path += "/";
                        return `${u.origin}${path}`;
                    } catch (_) {
                        return "";
                    }
                };

                const allLinks = Array.from(
                    document.querySelectorAll(
                        'a[href*="/truc-tiep/"], a[href*="/room/"]'
                    )
                );

                const groups = new Map();

                for (const a of allLinks) {
                    const baseUrl = canonical(a.href);
                    if (!baseUrl) continue;

                    if (!groups.has(baseUrl)) {
                        groups.set(baseUrl, []);
                    }
                    groups.get(baseUrl).push(a);
                }

                const output = [];

                for (const [baseUrl, links] of groups.entries()) {
                    const baseAnchor =
                        links.find((a) => {
                            try {
                                return !new URL(
                                    a.href,
                                    location.href
                                ).searchParams.has("blv");
                            } catch (_) {
                                return false;
                            }
                        }) || links[0];

                    let card = null;
                    let node = baseAnchor;

                    // Tìm ancestor nhỏ nhất chứa đúng card trận:
                    // có VS + ít nhất hai ảnh alt="Logo ..."
                    for (let depth = 0; node && depth < 12; depth++) {
                        const text = clean(node.innerText);
                        const logos = Array.from(
                            node.querySelectorAll("img")
                        ).filter((img) =>
                            /^logo\s+/i.test(
                                clean(img.getAttribute("alt"))
                            )
                        );

                        const sameMatchLinks = Array.from(
                            node.querySelectorAll(
                                'a[href*="/truc-tiep/"], a[href*="/room/"]'
                            )
                        ).filter(
                            (a) => canonical(a.href) === baseUrl
                        );

                        if (
                            /\bVS\b/i.test(text) &&
                            logos.length >= 2 &&
                            sameMatchLinks.length >= 1 &&
                            text.length < 2500
                        ) {
                            card = node;
                            break;
                        }

                        node = node.parentElement;
                    }

                    if (!card) {
                        card =
                            baseAnchor.closest("article") ||
                            baseAnchor.closest("li") ||
                            baseAnchor.parentElement ||
                            baseAnchor;
                    }

                    const cardText = clean(card.innerText);

                    if (
                        /bóng rổ|tennis|cầu lông/i.test(cardText)
                    ) {
                        continue;
                    }

                    const logoImages = Array.from(
                        card.querySelectorAll("img")
                    ).filter((img) =>
                        /^logo\s+/i.test(
                            clean(img.getAttribute("alt"))
                        )
                    );

                    const teams = [];
                    const teamSeen = new Set();

                    for (const img of logoImages) {
                        const alt = clean(img.getAttribute("alt"));
                        const name = clean(
                            alt.replace(/^logo\s+/i, "")
                        );
                        if (!name || teamSeen.has(name.toLowerCase())) {
                            continue;
                        }

                        teamSeen.add(name.toLowerCase());
                        teams.push({
                            name,
                            logo: absoluteImage(img),
                        });
                    }

                    let title =
                        clean(baseAnchor.getAttribute("title")) ||
                        clean(baseAnchor.getAttribute("aria-label")) ||
                        "";

                    if (!title) {
                        const titleElement = Array.from(
                            card.querySelectorAll(
                                "h1, h2, h3, h4, [class*='title']"
                            )
                        ).find((el) =>
                            /\bvs\b/i.test(clean(el.innerText))
                        );

                        title = titleElement
                            ? clean(titleElement.innerText)
                            : "";
                    }

                    const roomMap = new Map();

                    for (const a of links) {
                        try {
                            const u = new URL(a.href, location.href);
                            const id = u.searchParams.get("blv") || "";
                            const blv =
                                clean(a.innerText) ||
                                clean(a.getAttribute("title")) ||
                                clean(a.getAttribute("aria-label")) ||
                                "";

                            roomMap.set(u.href, {
                                url: u.href,
                                blv,
                                blv_id: id,
                            });
                        } catch (_) {}
                    }

                    if (!roomMap.has(baseUrl)) {
                        roomMap.set(baseUrl, {
                            url: baseUrl,
                            blv: "",
                            blv_id: "",
                        });
                    }

                    const timeMatch = cardText.match(
                        /\b([01]?\d|2[0-3]):([0-5]\d)\b/
                    );
                    const dateMatch = cardText.match(
                        /\b([0-3]?\d)\/([01]?\d)\b/
                    );

                    let status = "";
                    for (const word of [
                        "Đang diễn ra",
                        "Trực tiếp",
                        "LIVE",
                        "Hiệp 1",
                        "Hiệp 2",
                        "Sắp diễn ra",
                    ]) {
                        if (
                            cardText
                                .toLowerCase()
                                .includes(word.toLowerCase())
                        ) {
                            status = word;
                            break;
                        }
                    }

                    output.push({
                        base_url: baseUrl,
                        raw_title: title,
                        home_team: teams[0]?.name || "",
                        away_team: teams[1]?.name || "",
                        home_logo: teams[0]?.logo || "",
                        away_logo: teams[1]?.logo || "",
                        time_text: timeMatch
                            ? `${timeMatch[1].padStart(2, "0")}:${timeMatch[2]}`
                            : "",
                        date_text: dateMatch
                            ? `${dateMatch[1].padStart(2, "0")}/${dateMatch[2].padStart(2, "0")}`
                            : "",
                        status,
                        card_text: cardText.slice(0, 1200),
                        rooms: Array.from(roomMap.values()),
                    });
                }

                return output;
            }"""
        )

        matches: list[dict[str, Any]] = []

        for item in raw_matches:
            base_url = canonical_match_url(item.get("base_url", ""))
            if not base_url:
                continue

            home_team = clean_text(item.get("home_team", ""))
            away_team = clean_text(item.get("away_team", ""))

            if not home_team or not away_team:
                title_home, title_away = parse_teams_from_title(
                    item.get("raw_title", "")
                )
                home_team = home_team or title_home
                away_team = away_team or title_away

            if not home_team or not away_team:
                slug_home, slug_away = parse_teams_from_title(
                    match_name_from_url(base_url)
                )
                home_team = home_team or slug_home
                away_team = away_team or slug_away

            matches.append(
                {
                    **item,
                    "base_url": base_url,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_logo": normalize_absolute_url(
                        item.get("home_logo", "")
                    ),
                    "away_logo": normalize_absolute_url(
                        item.get("away_logo", "")
                    ),
                    "match_name": (
                        f"{home_team} vs {away_team}"
                        if home_team and away_team
                        else match_name_from_url(base_url)
                    ),
                    "rooms": unique_rooms(item.get("rooms", [])),
                    "scheduled_at": (
                        match_datetime_from_url(base_url).isoformat()
                        if match_datetime_from_url(base_url)
                        else ""
                    ),
                    "streams": [],
                    "room_results": [],
                    "errors": [],
                }
            )

        # Dedup theo URL trận.
        dedup: dict[str, dict[str, Any]] = {}
        for match in matches:
            dedup[match["base_url"]] = match

        return list(dedup.values())

    finally:
        await page.close()


def should_scan_match(
    match: dict[str, Any],
    now: datetime,
) -> bool:
    if SCAN_ALL:
        return True

    status = clean_text(match.get("status", "")).lower()

    if any(word in status for word in LIVE_STATUS_WORDS):
        return True

    scheduled = match_datetime_from_url(match.get("base_url", ""))
    if not scheduled:
        return False

    earliest = now - timedelta(minutes=SCAN_BEFORE_MINUTES)
    latest = now + timedelta(minutes=SCAN_AFTER_MINUTES)
    return earliest <= scheduled <= latest


# ============================================================
# METADATA TỪ TRANG TRẬN
# ============================================================
async def extract_page_metadata(
    page: Page,
    room_url: str,
) -> dict[str, str]:
    try:
        data = await page.evaluate(
            r"""() => {
                const clean = (value) =>
                    (value || "").replace(/\s+/g, " ").trim();

                const absoluteImage = (img) => {
                    if (!img) return "";

                    let src =
                        img.currentSrc ||
                        img.getAttribute("data-src") ||
                        img.getAttribute("data-lazy-src") ||
                        img.getAttribute("data-original") ||
                        img.getAttribute("src") ||
                        "";

                    if (!src) {
                        const srcset =
                            img.getAttribute("data-srcset") ||
                            img.getAttribute("srcset") ||
                            "";
                        if (srcset) {
                            src = srcset
                                .split(",")[0]
                                .trim()
                                .split(/\s+/)[0];
                        }
                    }

                    try {
                        return src ? new URL(src, location.href).href : "";
                    } catch (_) {
                        return src || "";
                    }
                };

                const h1 =
                    clean(document.querySelector("h1")?.innerText) ||
                    clean(document.title);

                const logoImages = Array.from(
                    document.querySelectorAll("img")
                ).filter((img) =>
                    /^logo\s+/i.test(
                        clean(img.getAttribute("alt"))
                    )
                );

                const teams = [];
                const seen = new Set();

                for (const img of logoImages) {
                    const alt = clean(img.getAttribute("alt"));
                    const name = clean(
                        alt.replace(/^logo\s+/i, "")
                    );

                    if (!name || seen.has(name.toLowerCase())) {
                        continue;
                    }

                    seen.add(name.toLowerCase());
                    teams.push({
                        name,
                        logo: absoluteImage(img),
                    });

                    if (teams.length >= 2) break;
                }

                let activeBlv = "";
                try {
                    const currentId =
                        new URL(location.href).searchParams.get("blv");

                    if (currentId) {
                        const links = Array.from(
                            document.querySelectorAll(
                                'a[href*="blv="]'
                            )
                        );

                        const active = links.find((a) => {
                            try {
                                return (
                                    new URL(
                                        a.href,
                                        location.href
                                    ).searchParams.get("blv") === currentId
                                );
                            } catch (_) {
                                return false;
                            }
                        });

                        activeBlv =
                            clean(active?.innerText) ||
                            clean(active?.getAttribute("title")) ||
                            "";
                    }
                } catch (_) {}

                return {
                    h1,
                    home_team: teams[0]?.name || "",
                    away_team: teams[1]?.name || "",
                    home_logo: teams[0]?.logo || "",
                    away_logo: teams[1]?.logo || "",
                    active_blv: activeBlv,
                };
            }"""
        )

        return {
            key: clean_text(str(value or ""))
            for key, value in data.items()
        }
    except Exception:
        return {}


async def collect_frame_candidates(page: Page) -> set[str]:
    values: set[str] = set()

    for frame in page.frames:
        try:
            frame_values = await frame.evaluate(
                r"""() => {
                    const out = new Set();

                    try {
                        for (
                            const entry of
                            performance.getEntriesByType("resource")
                        ) {
                            if (entry?.name) out.add(entry.name);
                        }
                    } catch (_) {}

                    document
                        .querySelectorAll("video, source")
                        .forEach((element) => {
                            [
                                element.src,
                                element.currentSrc,
                                element.getAttribute("src"),
                                element.getAttribute("data-src"),
                                element.getAttribute("data-url"),
                                element.getAttribute("data-stream"),
                                element.getAttribute("data-file"),
                            ].forEach((value) => {
                                if (value) out.add(value);
                            });
                        });

                    // JWPlayer API nếu player đã khởi tạo.
                    try {
                        if (typeof window.jwplayer === "function") {
                            const ids = Array.from(
                                document.querySelectorAll(
                                    ".jwplayer[id]"
                                )
                            ).map((el) => el.id);

                            if (!ids.length) ids.push("");

                            for (const id of ids) {
                                try {
                                    const player = id
                                        ? window.jwplayer(id)
                                        : window.jwplayer();

                                    const item =
                                        player?.getPlaylistItem?.();

                                    [
                                        item?.file,
                                        item?.sources?.[0]?.file,
                                        player?.getPlaylist?.()?.[0]?.file,
                                    ].forEach((value) => {
                                        if (value) out.add(value);
                                    });
                                } catch (_) {}
                            }
                        }
                    } catch (_) {}

                    const source = document.documentElement?.innerHTML || "";
                    const matches =
                        source.match(
                            /https?:\/\/[^"' <>\n\r]+/gi
                        ) || [];

                    matches.forEach((value) => out.add(value));

                    return Array.from(out);
                }"""
            )

            for value in frame_values:
                values.add(str(value))
        except Exception:
            continue

    return values


async def stimulate_player(page: Page) -> None:
    try:
        await page.bring_to_front()
    except Exception:
        pass

    # Thử click các nút play hiện hữu.
    for selector in PLAY_SELECTORS:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 2)

            for index in range(count):
                try:
                    if await locator.nth(index).is_visible():
                        await locator.nth(index).click(
                            timeout=800,
                            force=True,
                        )
                except Exception:
                    pass
        except Exception:
            pass

    # Thử gọi play trực tiếp.
    for frame in page.frames:
        try:
            await frame.evaluate(
                """() => {
                    document.querySelectorAll("video").forEach(
                        (video) => {
                            try {
                                video.muted = true;
                                video.volume = 0;
                                video.autoplay = true;
                                video.setAttribute(
                                    "playsinline",
                                    "true"
                                );
                                const result = video.play();
                                if (
                                    result &&
                                    typeof result.catch === "function"
                                ) {
                                    result.catch(() => {});
                                }
                            } catch (_) {}
                        }
                    );
                }"""
            )
        except Exception:
            pass


async def diagnose_page(page: Page) -> dict[str, Any]:
    try:
        return await page.evaluate(
            """() => ({
                url: location.href,
                title: document.title || "",
                h1: document.querySelector("h1")?.innerText || "",
                ready_state: document.readyState,
                visibility_state: document.visibilityState,
                hidden: document.hidden,
                video_count: document.querySelectorAll("video").length,
                iframe_count: document.querySelectorAll("iframe").length,
                body_sample: (
                    document.body?.innerText || ""
                ).replace(/\\s+/g, " ").trim().slice(0, 1200),
            })"""
        )
    except Exception as exc:
        return {"diagnostic_error": f"{type(exc).__name__}: {exc}"}


async def fast_http_probe(
    context: BrowserContext,
    room_url: str,
) -> tuple[set[str], dict[str, Any]]:
    """
    Request nhẹ trước khi mở Chromium:
      - bắt stream nếu URL nằm trong HTML/config/script;
      - ghi trạng thái HTTP để debug;
      - không chạy JavaScript nên rất tiết kiệm thời gian.
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "enabled": HTTP_FIRST,
        "status": None,
        "elapsed_seconds": 0.0,
        "error": "",
    }

    if not HTTP_FIRST:
        return set(), result

    try:
        response = await context.request.get(
            room_url,
            headers={
                "User-Agent": UA,
                "Referer": TARGET_URL,
                "Accept-Language":
                    "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=HTTP_FETCH_TIMEOUT_SECONDS * 1000,
            fail_on_status_code=False,
        )
        result["status"] = response.status
        body = await response.text()

        streams = extract_stream_urls_from_value(body)
        result["elapsed_seconds"] = round(
            time.monotonic() - started,
            3,
        )
        return streams, result

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["elapsed_seconds"] = round(
            time.monotonic() - started,
            3,
        )
        return set(), result


def safe_file_part(value: str, max_length: int = 90) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = value.strip("-._")
    return (value or "unknown")[:max_length]


def classify_failure(
    main_status: int | None,
    diagnostics: dict[str, Any],
    network_events: list[dict[str, Any]],
    http_errors: list[str],
) -> dict[str, Any]:
    statuses = [
        int(event["status"])
        for event in network_events
        if isinstance(event.get("status"), int)
    ]

    urls = [
        str(event.get("url", ""))
        for event in network_events
    ]

    lower_body = clean_text(
        str(diagnostics.get("body_sample", ""))
    ).lower()

    pull_urls = [
        url for url in urls
        if any(
            marker in url.lower()
            for marker in (
                "niur.live",
                "niues.live",
                "niu.live",
                "/live/stream-",
            )
        )
    ]

    m3u8_events = [
        event for event in network_events
        if ".m3u8" in str(event.get("url", "")).lower()
    ]

    if main_status == 403:
        code = "SITE_HTTP_403"
        explanation = "Trang trận trả HTTP 403."
    elif main_status == 429 or 429 in statuses:
        code = "RATE_LIMIT_429"
        explanation = "Có HTTP 429: máy chủ đang giới hạn tần suất."
    elif any(
        event.get("status") in {401, 403}
        and ".m3u8" in str(event.get("url", "")).lower()
        for event in network_events
    ):
        code = "CDN_TOKEN_REJECTED"
        explanation = "CDN đã nhận URL m3u8 nhưng trả 401/403."
    elif any(
        marker in lower_body
        for marker in (
            "checking your browser",
            "verify you are human",
            "access denied",
            "too many requests",
            "temporarily blocked",
            "captcha",
            "cloudflare ray id",
        )
    ):
        code = "CHALLENGE_OR_BLOCK_PAGE"
        explanation = "Nội dung trang có dấu hiệu challenge/chặn truy cập."
    elif not pull_urls and diagnostics.get("video_count", 0) == 0:
        code = "PLAYER_NOT_CREATED"
        explanation = (
            "HTML tải được nhưng player/video chưa được tạo; "
            "thường do script lỗi, tài nguyên bị chặn hoặc trang chưa sẵn sàng."
        )
    elif not pull_urls:
        code = "PLAYER_HAS_NO_SOURCE"
        explanation = (
            "Player tồn tại nhưng không gọi tới CDN niu; có thể nguồn chưa được "
            "gắn, timeout quá ngắn hoặc giới hạn mềm theo session/IP."
        )
    elif pull_urls and not m3u8_events:
        code = "CDN_CONTACTED_NO_MANIFEST"
        explanation = (
            "Đã liên hệ hạ tầng CDN/player nhưng chưa thấy manifest m3u8."
        )
    elif http_errors:
        code = "NETWORK_HTTP_ERRORS"
        explanation = "Có lỗi HTTP trong quá trình khởi tạo player."
    else:
        code = "UNKNOWN_NO_STREAM"
        explanation = (
            "Trang trả bình thường nhưng không quan sát được luồng trong thời gian chờ."
        )

    return {
        "code": code,
        "explanation": explanation,
        "main_status": main_status,
        "status_codes": sorted(set(statuses)),
        "pull_request_count": len(pull_urls),
        "m3u8_event_count": len(m3u8_events),
        "pull_url_samples": pull_urls[:10],
    }


async def save_failure_artifacts(
    page: Page,
    match: dict[str, Any],
    room_url: str,
    blv_id: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    if not SAVE_FAILURE_ARTIFACTS:
        return {}

    FAILURE_DIR.mkdir(parents=True, exist_ok=True)

    match_part = safe_file_part(match_display_name(match))
    blv_part = safe_file_part(blv_id or "base")
    stem = f"{match_part}__blv-{blv_part}"

    paths = {
        "json": str(FAILURE_DIR / f"{stem}.json"),
        "html": str(FAILURE_DIR / f"{stem}.html"),
        "screenshot": str(FAILURE_DIR / f"{stem}.png"),
    }

    try:
        Path(paths["json"]).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        paths.pop("json", None)

    try:
        html_content = await page.content()
        Path(paths["html"]).write_text(
            html_content,
            encoding="utf-8",
        )
    except Exception:
        paths.pop("html", None)

    try:
        await page.screenshot(
            path=paths["screenshot"],
            full_page=False,
        )
    except Exception:
        paths.pop("screenshot", None)

    return paths


# ============================================================
# QUÉT MỘT PHÒNG BLV
# ============================================================
async def scan_room(
    context: BrowserContext,
    match: dict[str, Any],
    room: dict[str, str],
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    room_url = room["url"]
    room_blv = clean_text(room.get("blv", ""))
    blv_id = room.get("blv_id", "") or room_blv_id(room_url)
    effective_wait = (
        float(wait_seconds)
        if wait_seconds is not None
        else ROOM_WAIT_SECONDS
    )

    stream_urls: set[str] = set()
    errors: list[str] = []
    failed_requests: list[str] = []
    http_errors: list[str] = []
    network_events: list[dict[str, Any]] = []
    stream_event = asyncio.Event()
    started = time.monotonic()
    main_status: int | None = None
    main_headers: dict[str, str] = {}

    # HTTP-first mặc định đã tắt vì trang Socolive cấp luồng sau khi JS chạy.
    http_streams, http_probe = await fast_http_probe(
        context,
        room_url,
    )
    if http_streams:
        for stream in sorted(http_streams):
            print(f"      🎯 [http-first] {stream}")

        return {
            "url": room_url,
            "blv": room_blv,
            "blv_id": blv_id,
            "streams": sorted(http_streams),
            "diagnostics": {
                "scan_mode": "http-first",
                "elapsed_seconds": round(
                    time.monotonic() - started,
                    3,
                ),
            },
            "http_probe": http_probe,
            "network_events": network_events,
            "block_verdict": {
                "code": "STREAM_FOUND_HTTP_FIRST",
                "explanation": "Tìm thấy stream ngay trong phản hồi HTTP.",
            },
            "failure_artifacts": {},
            "errors": errors,
            "failed_requests": failed_requests,
            "http_errors": http_errors,
        }

    page = await context.new_page()
    cdp = None

    async def route_handler(route: Any) -> None:
        # Không chặn stylesheet vì nó có thể ảnh hưởng trạng thái/khả năng click player.
        if route.request.resource_type in {"image", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)

    def append_network_event(
        phase: str,
        url: str,
        resource_type: str = "",
        status: int | None = None,
    ) -> None:
        if len(network_events) >= 160:
            return

        lower = (url or "").lower()
        important = (
            resource_type in {
                "document",
                "script",
                "xhr",
                "fetch",
                "media",
                "manifest",
                "other",
            }
            or ".m3u8" in lower
            or "niur.live" in lower
            or "niues.live" in lower
            or "/live/stream-" in lower
        )

        if not important:
            return

        event = {
            "t": round(time.monotonic() - started, 3),
            "phase": phase,
            "resource_type": resource_type,
            "url": url[:1600],
        }
        if status is not None:
            event["status"] = int(status)

        network_events.append(event)

    def capture(value: str, source: str) -> None:
        for stream in extract_stream_urls_from_value(value):
            if stream not in stream_urls:
                stream_urls.add(stream)
                stream_event.set()
                print(f"      🎯 [{source}] {stream}")

    def on_request(request: Any) -> None:
        append_network_event(
            "request",
            request.url,
            request.resource_type,
        )
        capture(request.url, f"request/{request.resource_type}")

    def on_response(response: Any) -> None:
        append_network_event(
            "response",
            response.url,
            response.request.resource_type,
            response.status,
        )
        capture(response.url, f"response/{response.status}")

        if response.status >= 400 and len(http_errors) < 30:
            http_errors.append(
                f"{response.status} {response.url[:700]}"
            )

    def on_request_failed(request: Any) -> None:
        if len(failed_requests) >= 30:
            return
        failed_requests.append(
            f"{request.resource_type} {request.url[:600]} "
            f"| {request.failure}"
        )

    def on_page_error(error: Any) -> None:
        if len(errors) < 40:
            errors.append(f"JS: {error}")

    def on_console(message: Any) -> None:
        if (
            message.type in {"error", "warning"}
            and len(errors) < 40
        ):
            value = clean_text(str(message.text))
            if value:
                errors.append(
                    f"console/{message.type}: {value[:900]}"
                )

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    page.on("pageerror", on_page_error)
    page.on("console", on_console)

    try:
        if ENABLE_CDP:
            try:
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")

                def on_cdp_request(event: dict[str, Any]) -> None:
                    request_data = event.get("request", {})
                    url = request_data.get("url", "")
                    resource_type = str(event.get("type", "")).lower()
                    append_network_event(
                        "cdp_request",
                        url,
                        resource_type,
                    )
                    capture(url, "cdp/request")

                def on_cdp_response(event: dict[str, Any]) -> None:
                    response_data = event.get("response", {})
                    url = response_data.get("url", "")
                    status = response_data.get("status")
                    resource_type = str(event.get("type", "")).lower()
                    append_network_event(
                        "cdp_response",
                        url,
                        resource_type,
                        int(status) if status is not None else None,
                    )
                    capture(url, "cdp/response")

                cdp.on("Network.requestWillBeSent", on_cdp_request)
                cdp.on("Network.responseReceived", on_cdp_response)

            except Exception as exc:
                errors.append(
                    f"CDP unavailable: {type(exc).__name__}: {exc}"
                )

        main_response = await page.goto(
            room_url,
            wait_until="domcontentloaded",
            timeout=22000,
            referer=TARGET_URL,
        )

        if main_response is not None:
            main_status = main_response.status
            try:
                main_headers = await main_response.all_headers()
            except Exception:
                main_headers = {}

        await page.wait_for_timeout(650)

        if not stream_event.is_set():
            await stimulate_player(page)

        metadata = await extract_page_metadata(page, room_url)

        if metadata.get("active_blv"):
            room_blv = metadata["active_blv"]

        for key in (
            "home_team",
            "away_team",
            "home_logo",
            "away_logo",
        ):
            if not match.get(key) and metadata.get(key):
                if key.endswith("_logo"):
                    match[key] = normalize_absolute_url(
                        metadata[key],
                        room_url,
                    )
                else:
                    match[key] = metadata[key]

        if (
            (not match.get("home_team") or not match.get("away_team"))
            and metadata.get("h1")
        ):
            home, away = parse_teams_from_title(metadata["h1"])
            match["home_team"] = match.get("home_team") or home
            match["away_team"] = match.get("away_team") or away

        # Chia thời gian chờ thành hai nhịp để kích hoạt player lại sau khi DOM ổn định.
        first_wait = min(3.0, effective_wait)
        if not stream_event.is_set() and first_wait > 0:
            try:
                await asyncio.wait_for(
                    stream_event.wait(),
                    timeout=first_wait,
                )
            except asyncio.TimeoutError:
                pass

        remaining_wait = max(0.0, effective_wait - first_wait)
        if not stream_event.is_set() and remaining_wait > 0:
            await stimulate_player(page)
            try:
                await asyncio.wait_for(
                    stream_event.wait(),
                    timeout=remaining_wait,
                )
            except asyncio.TimeoutError:
                pass

        if stream_event.is_set():
            await page.wait_for_timeout(
                int(EXTRA_WAIT_AFTER_STREAM * 1000)
            )
        else:
            for value in await collect_frame_candidates(page):
                capture(value, "final/frame-scan")

            if stream_event.is_set():
                await page.wait_for_timeout(250)

        diagnostics = await diagnose_page(page)
        diagnostics.update(
            {
                "scan_mode": "browser-event-diagnostic",
                "elapsed_seconds": round(
                    time.monotonic() - started,
                    3,
                ),
                "wait_seconds": effective_wait,
                "main_status": main_status,
                "final_url": page.url,
                "expected_stream_fragment": (
                    f"stream-{blv_id}_lhd.m3u8"
                    if blv_id
                    else ""
                ),
            }
        )

        block_verdict = classify_failure(
            main_status,
            diagnostics,
            network_events,
            http_errors,
        ) if not stream_urls else {
            "code": "STREAM_FOUND",
            "explanation": "Đã quan sát được URL stream.",
            "main_status": main_status,
        }

        failure_artifacts: dict[str, str] = {}

        if not stream_urls:
            artifact_payload = {
                "match": match_display_name(match),
                "room_url": room_url,
                "blv": room_blv,
                "blv_id": blv_id,
                "diagnostics": diagnostics,
                "block_verdict": block_verdict,
                "main_headers": main_headers,
                "network_events": network_events,
                "http_errors": http_errors,
                "failed_requests": failed_requests,
                "errors": errors,
            }

            failure_artifacts = await save_failure_artifacts(
                page,
                match,
                room_url,
                blv_id,
                artifact_payload,
            )

    except Exception as exc:
        diagnostics = {
            "scan_mode": "browser-event-diagnostic",
            "elapsed_seconds": round(
                time.monotonic() - started,
                3,
            ),
            "wait_seconds": effective_wait,
            "main_status": main_status,
            "expected_stream_fragment": (
                f"stream-{blv_id}_lhd.m3u8"
                if blv_id
                else ""
            ),
        }
        errors.append(f"{type(exc).__name__}: {exc}")
        block_verdict = classify_failure(
            main_status,
            diagnostics,
            network_events,
            http_errors,
        )
        failure_artifacts = {}

        try:
            failure_artifacts = await save_failure_artifacts(
                page,
                match,
                room_url,
                blv_id,
                {
                    "match": match_display_name(match),
                    "room_url": room_url,
                    "blv": room_blv,
                    "blv_id": blv_id,
                    "diagnostics": diagnostics,
                    "block_verdict": block_verdict,
                    "main_headers": main_headers,
                    "network_events": network_events,
                    "http_errors": http_errors,
                    "failed_requests": failed_requests,
                    "errors": errors,
                },
            )
        except Exception:
            pass
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
        await page.close()

    return {
        "url": room_url,
        "blv": room_blv,
        "blv_id": blv_id,
        "streams": sorted(stream_urls),
        "diagnostics": diagnostics,
        "http_probe": http_probe,
        "network_events": network_events,
        "block_verdict": block_verdict,
        "failure_artifacts": failure_artifacts,
        "errors": errors,
        "failed_requests": failed_requests,
        "http_errors": http_errors,
    }


# ============================================================
# QUÉT MỘT TRẬN
# ============================================================
async def scan_match(
    context: BrowserContext,
    match: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        name = match_display_name(match)
        scheduled = match_datetime_from_url(match["base_url"])

        time_label = (
            scheduled.strftime("%H:%M %d/%m")
            if scheduled
            else match.get("time_text", "")
        )

        print(f"\n⚽ {name} | {time_label}")

        rooms = unique_rooms(match.get("rooms", []))
        if not rooms:
            rooms = [
                {
                    "url": match["base_url"],
                    "blv": "",
                    "blv_id": "",
                }
            ]

        rooms = rooms[:MAX_ROOMS_PER_MATCH]
        successful_rooms = 0

        for index, room in enumerate(rooms, start=1):
            blv_label = room.get("blv") or room.get("blv_id") or "URL tổng"

            print(
                f"   -> Phòng {index}/{len(rooms)}: "
                f"{blv_label}"
            )

            room_wait = (
                ROOM_WAIT_SECONDS
                if index == 1
                else RETRY_ROOM_WAIT_SECONDS
            )

            room_result = await scan_room(
                context,
                match,
                room,
                wait_seconds=room_wait,
            )
            match["room_results"].append(room_result)

            if room_result["streams"]:
                successful_rooms += 1

                for stream in room_result["streams"]:
                    match["streams"].append(
                        {
                            "url": stream,
                            "room_url": room_result["url"],
                            "blv": room_result.get("blv", ""),
                            "blv_id": room_result.get(
                                "blv_id",
                                "",
                            ),
                        }
                    )

                print(
                    f"      ✅ {len(room_result['streams'])} "
                    f"luồng hợp lệ"
                )

                if (
                    successful_rooms
                    >= MAX_SUCCESSFUL_ROOMS_PER_MATCH
                ):
                    break
            else:
                diag = room_result.get("diagnostics", {})
                verdict = room_result.get(
                    "block_verdict",
                    {},
                )
                print(
                    "      ⚠️ Không thấy stream trực tiếp"
                    f" | verdict={verdict.get('code', 'UNKNOWN')}"
                    f" | HTTP={diag.get('main_status', '?')}"
                    f" | video={diag.get('video_count', '?')}"
                    f" iframe={diag.get('iframe_count', '?')}"
                    f" | {verdict.get('explanation', '')}"
                )

            await asyncio.sleep(DELAY_BETWEEN_ROOMS)

        # Dedup stream trong cùng trận.
        seen_streams: set[str] = set()
        unique_stream_list: list[dict[str, str]] = []

        for item in match["streams"]:
            url = item["url"]
            if url in seen_streams:
                continue
            seen_streams.add(url)
            unique_stream_list.append(item)

        match["streams"] = unique_stream_list
        match["match_name"] = match_display_name(match)

        if match["streams"]:
            print(
                f"   ✅ HOÀN TẤT TRẬN: "
                f"{len(match['streams'])} link"
            )
        else:
            print("   ❌ Trận này chưa bắt được link")

        return match


# ============================================================
# XUẤT FILE
# ============================================================
def escape_m3u(value: str) -> str:
    return (
        clean_text(value)
        .replace('"', "'")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def write_outputs(matches: list[dict[str, Any]]) -> tuple[int, int]:
    playlist = ["#EXTM3U"]
    seen_urls: set[str] = set()
    count_links = 0
    count_matches = 0

    origin = TARGET_URL.rstrip("/")

    for match in matches:
        streams = match.get("streams", [])
        if not streams:
            continue

        match_counted = False
        match_name = match_display_name(match)
        scheduled = match_datetime_from_url(match["base_url"])
        time_prefix = (
            scheduled.strftime("[%H:%M] ")
            if scheduled
            else ""
        )

        home_logo = escape_m3u(
            normalize_absolute_url(
                match.get("home_logo", ""),
                match["base_url"],
            )
        )
        away_logo = escape_m3u(
            normalize_absolute_url(
                match.get("away_logo", ""),
                match["base_url"],
            )
        )

        # M3U chuẩn chỉ hiểu một tvg-logo.
        # Dùng logo đội chủ nhà cho tvg-logo và lưu cả hai bằng field mở rộng.
        standard_logo = home_logo or away_logo

        for index, stream_item in enumerate(streams, start=1):
            stream = stream_item["url"]
            if stream in seen_urls:
                continue

            seen_urls.add(stream)

            blv = clean_text(stream_item.get("blv", ""))
            room_url = stream_item.get("room_url") or match["base_url"]

            display_name = f"{time_prefix}{match_name}"
            if blv:
                display_name += f" [BLV {blv}]"
            if len(streams) > 1:
                display_name += f" (Luồng {index})"

            display_name = escape_m3u(display_name)

            extinf_attributes = [
                'group-title="Socolive"',
                f'tvg-logo="{standard_logo}"',
                f'tvg-logo-home="{home_logo}"',
                f'tvg-logo-away="{away_logo}"',
                f'tvg-name="{escape_m3u(match_name)}"',
            ]

            playlist.append(
                f"#EXTINF:-1 {' '.join(extinf_attributes)},"
                f"{display_name}"
            )
            playlist.append(
                f"#EXTVLCOPT:http-referrer={room_url}"
            )
            playlist.append(
                f"#EXTVLCOPT:http-referer={room_url}"
            )
            playlist.append(
                f"#EXTVLCOPT:http-origin={origin}"
            )
            playlist.append(
                f"#EXTVLCOPT:http-user-agent={UA}"
            )
            playlist.append(
                f"{stream}"
                f"|Referer={room_url}"
                f"&Origin={origin}"
                f"&User-Agent={UA}"
            )

            count_links += 1
            match_counted = True

        if match_counted:
            count_matches += 1

    Path(OUTPUT_MATCHES).write_text(
        json.dumps(matches, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    Path(OUTPUT_DEBUG).write_text(
        json.dumps(
            {
                "generated_at": datetime.now(TIMEZONE).isoformat(),
                "configuration": {
                    "match_concurrency": MATCH_CONCURRENCY,
                    "room_wait_seconds": ROOM_WAIT_SECONDS,
                    "max_rooms_per_match": MAX_ROOMS_PER_MATCH,
                    "max_successful_rooms_per_match": MAX_SUCCESSFUL_ROOMS_PER_MATCH,
                    "http_first": HTTP_FIRST,
                    "http_fetch_timeout_seconds": HTTP_FETCH_TIMEOUT_SECONDS,
                    "enable_cdp": ENABLE_CDP,
                    "retry_room_wait_seconds": RETRY_ROOM_WAIT_SECONDS,
                    "batch_cooldown_seconds": BATCH_COOLDOWN_SECONDS,
                    "failed_batch_cooldown_seconds":
                        FAILED_BATCH_COOLDOWN_SECONDS,
                    "save_failure_artifacts":
                        SAVE_FAILURE_ARTIFACTS,
                    "scan_before_minutes": SCAN_BEFORE_MINUTES,
                    "scan_after_minutes": SCAN_AFTER_MINUTES,
                    "scan_all": SCAN_ALL,
                },
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if count_links:
        Path(OUTPUT_M3U).write_text(
            "\n".join(playlist) + "\n",
            encoding="utf-8",
        )

    return count_matches, count_links


async def create_scan_context(browser: Any) -> BrowserContext:
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=UA,
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
        ignore_https_errors=True,
        extra_http_headers={
            "Accept-Language":
                "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
        },
    )

    await context.add_init_script(
        """() => {
            try {
                Object.defineProperty(
                    navigator,
                    "webdriver",
                    { get: () => undefined }
                );
                Object.defineProperty(
                    navigator,
                    "languages",
                    { get: () => ["vi-VN", "vi", "en-US", "en"] }
                );
                Object.defineProperty(
                    document,
                    "hidden",
                    { get: () => false }
                );
                Object.defineProperty(
                    document,
                    "visibilityState",
                    { get: () => "visible" }
                );
            } catch (_) {}
        }"""
    )

    return context


async def scan_match_isolated(
    browser: Any,
    match: dict[str, Any],
) -> dict[str, Any]:
    # Mỗi trận dùng cookie/localStorage/session riêng để tránh nhiễu giữa các player.
    context = await create_scan_context(browser)
    try:
        return await scan_match(
            context,
            match,
            asyncio.Semaphore(1),
        )
    finally:
        await context.close()


# ============================================================
# MAIN
# ============================================================
async def main() -> None:
    print("🥷 SOCOLIVE STREAM SCANNER V4 - BATCH + BLOCK DIAGNOSTIC")
    print(
        "ℹ️ Test riêng một URL:\n"
        '   python main_socolive_v4_diagnostic.py "https://socolivem.cv/truc-tiep/.../?blv=..."'
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
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )

        context = await create_scan_context(browser)

        if direct_urls:
            grouped: dict[str, dict[str, Any]] = {}

            for url in direct_urls:
                base = canonical_match_url(url)
                if base not in grouped:
                    name = match_name_from_url(base)
                    home, away = parse_teams_from_title(name)

                    grouped[base] = {
                        "base_url": base,
                        "raw_title": name,
                        "home_team": home,
                        "away_team": away,
                        "home_logo": "",
                        "away_logo": "",
                        "time_text": "",
                        "date_text": "",
                        "status": "direct-test",
                        "card_text": "",
                        "rooms": [],
                        "scheduled_at": (
                            match_datetime_from_url(base).isoformat()
                            if match_datetime_from_url(base)
                            else ""
                        ),
                        "match_name": (
                            f"{home} vs {away}"
                            if home and away
                            else name
                        ),
                        "streams": [],
                        "room_results": [],
                        "errors": [],
                    }

                grouped[base]["rooms"].append(
                    {
                        "url": url,
                        "blv": "",
                        "blv_id": room_blv_id(url),
                    }
                )

            matches = list(grouped.values())

            for match in matches:
                match["rooms"] = unique_rooms(match["rooms"])

            print(
                f"✅ Chế độ test trực tiếp: "
                f"{len(matches)} trận."
            )
        else:
            all_matches = await collect_home_matches(context)
            now = datetime.now(TIMEZONE)

            matches = [
                match
                for match in all_matches
                if should_scan_match(match, now)
            ]

            print(
                f"✅ Trang chủ có {len(all_matches)} trận; "
                f"chọn {len(matches)} trận trong khung đang/sắp đá."
            )

            # Ghi metadata ngay cả trước khi quét stream.
            Path(OUTPUT_MATCHES).write_text(
                json.dumps(
                    all_matches,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        if not matches:
            print(
                "❌ Không có trận nào trong khung thời gian quét. "
                "Dùng SOCOLIVE_SCAN_ALL=1 để quét tất cả."
            )
            await context.close()
            await browser.close()
            return

        # Context lấy danh sách trận không được dùng lại cho player.
        await context.close()

        results: list[dict[str, Any]] = []

        for batch_start in range(
            0,
            len(matches),
            MATCH_CONCURRENCY,
        ):
            batch = matches[
                batch_start:
                batch_start + MATCH_CONCURRENCY
            ]

            batch_number = (
                batch_start // MATCH_CONCURRENCY
            ) + 1
            total_batches = (
                len(matches) + MATCH_CONCURRENCY - 1
            ) // MATCH_CONCURRENCY

            print(
                f"\n🚦 LÔ {batch_number}/{total_batches}: "
                f"{len(batch)} trận, mỗi trận một session riêng"
            )

            batch_results = await asyncio.gather(
                *[
                    scan_match_isolated(browser, item)
                    for item in batch
                ]
            )
            results.extend(batch_results)

            batch_successes = sum(
                1
                for item in batch_results
                if item.get("streams")
            )

            if batch_start + MATCH_CONCURRENCY < len(matches):
                cooldown = (
                    BATCH_COOLDOWN_SECONDS
                    if batch_successes
                    else FAILED_BATCH_COOLDOWN_SECONDS
                )

                print(
                    f"⏸ Nghỉ {cooldown:g}s trước lô tiếp theo "
                    f"(lô vừa rồi thành công {batch_successes}/{len(batch)})"
                )
                await asyncio.sleep(cooldown)

        count_matches, count_links = write_outputs(results)

        if count_links:
            print(
                f"\n🎉 HOÀN TẤT: {count_links} link "
                f"từ {count_matches} trận."
            )
            print(f"📺 Playlist: {Path(OUTPUT_M3U).resolve()}")
        else:
            print("\n❌ Chưa bắt được stream trực tiếp nào.")

        print(
            f"⚽ Metadata trận/logo: "
            f"{Path(OUTPUT_MATCHES).resolve()}"
        )
        print(
            f"🧾 Debug chi tiết: "
            f"{Path(OUTPUT_DEBUG).resolve()}"
        )
        print(
            f"🕵️ Bằng chứng phòng thất bại: "
            f"{FAILURE_DIR.resolve()}"
        )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
