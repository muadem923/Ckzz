import asyncio
import copy
import hashlib
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
TARGET_URL = (
    os.getenv(
        "SOCOLIVE_TARGET_URL",
        "https://socolivep.cv/",
    ).rstrip("/")
    + "/"
)

OUTPUT_M3U = "socolive_live.m3u"
OUTPUT_DEBUG = "socolive_debug.json"
OUTPUT_MATCHES = "socolive_matches.json"
OUTPUT_STATE = "socolive_state.json"
OUTPUT_PENDING = "socolive_pending.json"
OUTPUT_DECISION = "socolive_chain_decision.json"

# Catalog độc lập với state quét. M3U luôn được dựng từ catalog tích lũy,
# không dựng riêng từ số trận vừa quét trong workflow hiện tại.
OUTPUT_CATALOG = "socolive_catalog.json"

TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

# Cơ chế cân bằng: 4 player song song, không quá tải như 8 và không chậm như 1.
MATCH_CONCURRENCY = int(os.getenv("SOCOLIVE_MATCH_CONCURRENCY", "1"))

# Mỗi phòng chỉ được chờ ngắn. Có stream thì đóng gần như ngay lập tức.
ROOM_WAIT_SECONDS = float(os.getenv("SOCOLIVE_ROOM_WAIT_SECONDS", "12"))
EXTRA_WAIT_AFTER_STREAM = float(
    os.getenv("SOCOLIVE_EXTRA_WAIT_AFTER_STREAM", "4.0")
)
DELAY_BETWEEN_ROOMS = float(
    os.getenv("SOCOLIVE_DELAY_BETWEEN_ROOMS", "2.0")
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
    os.getenv("SOCOLIVE_SCAN_BEFORE_MINUTES", "180")
)
SCAN_AFTER_MINUTES = int(
    os.getenv("SOCOLIVE_SCAN_AFTER_MINUTES", "240")
)
SCAN_ALL = os.getenv("SOCOLIVE_SCAN_ALL", "0") == "1"

# Mục tiêu nhiều luồng cho mỗi trận.
# Một trang có thể nhúng config/link của nhiều BLV khác nhau.
MAX_STREAMS_PER_MATCH = int(
    os.getenv("SOCOLIVE_MAX_STREAMS_PER_MATCH", "24")
)

# Số trang BLV tối đa được mở cho cùng một trận trong MỘT workflow run.
# Mỗi trang đều quét toàn bộ HTML/config, nên thường không cần mở đủ 10 trang.
MAX_ROOMS_PER_MATCH = int(
    os.getenv("SOCOLIVE_MAX_ROOMS_PER_MATCH", "2")
)

# Giữ biến cũ để tương thích workflow cũ; V9 dừng theo coverage stream.
MAX_SUCCESSFUL_ROOMS_PER_MATCH = int(
    os.getenv(
        "SOCOLIVE_MAX_SUCCESSFUL_ROOMS_PER_MATCH",
        str(MAX_STREAMS_PER_MATCH),
    )
)

# Sau khi bắt được stream đầu tiên, chờ thêm một chút rồi quét toàn bộ
# performance entries, JWPlayer playlist và HTML để thu hoạch các BLV khác.
EMBEDDED_HARVEST_WAIT_SECONDS = float(
    os.getenv("SOCOLIVE_EMBEDDED_HARVEST_WAIT_SECONDS", "6.0")
)

# Một BLV tải HTTP 200 nhưng chưa sinh stream sẽ được thử lại ở runner khác.
ROOM_EMPTY_MAX_ATTEMPTS = int(
    os.getenv("SOCOLIVE_ROOM_EMPTY_MAX_ATTEMPTS", "3")
)

# Mỗi BLV giữ tối đa hai nguồn. BLV mới có một nguồn sẽ mang trạng thái
# "partial" và được runner sau mở lại để tìm nguồn thứ hai.
MAX_STREAMS_PER_BLV = int(
    os.getenv("SOCOLIVE_MAX_STREAMS_PER_BLV", "2")
)

# Sau khi thấy stream đầu tiên, không đóng tab ngay. Bot quét lại
# performance/JWPlayer/HTML nhiều vòng cho tới giới hạn hoặc khi yên đủ lâu.
HARVEST_MAX_SECONDS = float(
    os.getenv("SOCOLIVE_HARVEST_MAX_SECONDS", "12")
)
HARVEST_POLL_SECONDS = float(
    os.getenv("SOCOLIVE_HARVEST_POLL_SECONDS", "0.8")
)
HARVEST_QUIET_SECONDS = float(
    os.getenv("SOCOLIVE_HARVEST_QUIET_SECONDS", "3.5")
)

HEADLESS = os.getenv("SOCOLIVE_HEADLESS", "1") != "0"

# Khi gặp HTTP 429, không retry trong cùng runner. Dừng mở trang mới và giữ kết quả đã lấy.
STOP_ON_429 = os.getenv("SOCOLIVE_STOP_ON_429", "1") != "0"

# Giữ link/tiến độ từ lần chạy trước trong thời gian ngắn để hoàn tất qua nhiều run.
STATE_MAX_AGE_MINUTES = int(
    os.getenv("SOCOLIVE_STATE_MAX_AGE_MINUTES", "180")
)

STATE_MAX_CHAIN_RUNS = int(
    os.getenv("SOCOLIVE_STATE_MAX_CHAIN_RUNS", "20")
)
STATE_MAX_NO_PROGRESS_RUNS = int(
    os.getenv("SOCOLIVE_STATE_MAX_NO_PROGRESS_RUNS", "3")
)

# Khi bật, cứ còn BLV pending/partial/rate_limited thì tự gọi runner mới.
# STATE_MAX_CHAIN_RUNS là cầu chì khẩn cấp; đặt <= 0 để không giới hạn.
CHAIN_UNTIL_PENDING_EMPTY = (
    os.getenv("SOCOLIVE_CHAIN_UNTIL_PENDING_EMPTY", "1") != "0"
)

# Tự dừng nếu queue không đổi hoặc mọi runner chỉ gặp 429 liên tiếp.
STATE_MAX_UNCHANGED_PENDING_RUNS = int(
    os.getenv("SOCOLIVE_STATE_MAX_UNCHANGED_PENDING_RUNS", "3")
)
STATE_MAX_RATE_LIMIT_ONLY_RUNS = int(
    os.getenv("SOCOLIVE_STATE_MAX_RATE_LIMIT_ONLY_RUNS", "3")
)

# Flow nối tiếp ưu tiên dùng queue trong state, không tải lại trang chủ.
RESUME_STATE_FIRST = (
    os.getenv("SOCOLIVE_RESUME_STATE_FIRST", "1") != "0"
)
RESET_CHAIN_GUARD = (
    os.getenv("SOCOLIVE_RESET_CHAIN_GUARD", "0") == "1"
)

# Link ký số gần hết hạn hoặc đã hết hạn không được tính vào coverage/catalog.
TOKEN_EXPIRY_GRACE_SECONDS = int(
    os.getenv("SOCOLIVE_TOKEN_EXPIRY_GRACE_SECONDS", "60")
)

# Đổi schema để reset state V10 cũ và cho phép quét lại từng BLV theo
# mục tiêu tối đa hai nguồn.
# vì V9 đã loại sai stream-ID không trùng BLV-ID.
STATE_SCHEMA_VERSION = "v10.3-safe-chain-valid-streams"

# Giữ trận trong playlist sau giờ bắt đầu để các workflow sau không làm
# biến mất link chỉ vì trận rơi khỏi khung quét -180 phút.
CATALOG_RETENTION_AFTER_START_MINUTES = int(
    os.getenv(
        "SOCOLIVE_CATALOG_RETENTION_AFTER_START_MINUTES",
        "360",
    )
)

# Trận không đọc được giờ vẫn được giữ theo thời điểm cuối xuất hiện.
CATALOG_RETENTION_UNSEEN_MINUTES = int(
    os.getenv(
        "SOCOLIVE_CATALOG_RETENTION_UNSEEN_MINUTES",
        "240",
    )
)

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


def stream_blv_id(stream_url: str) -> str:
    """
    Ví dụ:
      /live/stream-387694_lhd.m3u8 -> 387694
    """
    try:
        path = urlsplit(stream_url).path
    except Exception:
        return ""

    match = re.search(
        r"/stream-(\d+)(?:_|\.|/)",
        path,
        flags=re.I,
    )
    return match.group(1) if match else ""


def match_room_map(
    match: dict[str, Any],
) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}

    for room in unique_rooms(
        match.get("rooms", [])
    ):
        room_id = clean_text(
            room.get("blv_id", "")
        ) or room_blv_id(room.get("url", ""))

        if room_id:
            output[room_id] = room

    return output


def room_queue_key(
    room: dict[str, str],
) -> str:
    room_id = clean_text(
        room.get("blv_id", "")
    ) or room_blv_id(room.get("url", ""))

    if room_id:
        return room_id

    return "__base__"


def ensure_room_progress(
    match: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    rooms = unique_rooms(
        match.get("rooms", [])
    )
    progress = match.setdefault(
        "room_progress",
        {},
    )

    for room in rooms:
        key = room_queue_key(room)
        existing = progress.get(key, {})

        progress[key] = {
            "key": key,
            "url": room.get("url", ""),
            "blv": room.get("blv", ""),
            "blv_id": room.get("blv_id", ""),
            "status": existing.get(
                "status",
                "pending",
            ),
            "attempts": int(
                existing.get("attempts", 0)
                or 0
            ),
            "rate_limit_hits": int(
                existing.get(
                    "rate_limit_hits",
                    0,
                )
                or 0
            ),
            "last_http_status":
                existing.get(
                    "last_http_status"
                ),
            "last_stream_count": int(
                existing.get(
                    "last_stream_count",
                    0,
                )
                or 0
            ),
            "last_error": clean_text(
                str(
                    existing.get(
                        "last_error",
                        "",
                    )
                    or ""
                )
            ),
        }

    match["room_progress"] = progress
    return progress


def explicit_blv_rooms(
    match: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        room
        for room in unique_rooms(
            match.get("rooms", [])
        )
        if room_queue_key(room) != "__base__"
    ]


def base_rooms(
    match: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        room
        for room in unique_rooms(
            match.get("rooms", [])
        )
        if room_queue_key(room) == "__base__"
    ]


def pending_room_keys(
    match: dict[str, Any],
) -> list[str]:
    progress = ensure_room_progress(match)

    status_priority = {
        "pending": 0,
        "partial": 1,
        "retry": 2,
        "rate_limited": 3,
    }

    def ranked_keys(
        rooms: list[dict[str, str]],
    ) -> list[str]:
        ranked: list[tuple[int, int, int, int, str]] = []
        seen: set[str] = set()

        for order, room in enumerate(rooms):
            key = room_queue_key(room)
            if key in seen:
                continue
            seen.add(key)

            item = progress.get(key, {})
            status = item.get("status", "pending")
            if status not in status_priority:
                continue

            ranked.append(
                (
                    status_priority[status],
                    int(item.get("attempts", 0) or 0),
                    int(item.get("rate_limit_hits", 0) or 0),
                    order,
                    key,
                )
            )

        ranked.sort()
        return [item[-1] for item in ranked]

    # BLV chưa thử luôn đứng trước partial/retry/rate_limited.
    explicit = ranked_keys(explicit_blv_rooms(match))
    if explicit:
        return explicit

    # URL tổng chỉ là fallback cuối cùng.
    return ranked_keys(base_rooms(match))


def room_queue_complete(
    match: dict[str, Any],
) -> bool:
    progress = ensure_room_progress(match)
    explicit = explicit_blv_rooms(match)

    if explicit:
        return all(
            progress.get(
                room_queue_key(room),
                {},
            ).get("status")
            in {"success", "exhausted"}
            for room in explicit
        )

    bases = base_rooms(match)
    if not bases:
        return False

    return all(
        progress.get(
            room_queue_key(room),
            {},
        ).get("status")
        in {"success", "exhausted"}
        for room in bases
    )



def stream_queue_key(item: dict[str, Any]) -> str:
    return clean_text(
        str(
            item.get("queue_room_key")
            or item.get("blv_id")
            or item.get("source_id")
            or "unknown"
        )
    )


def streams_for_room_key(
    match: dict[str, Any],
    room_key: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in match.get("streams", [])
        if stream_queue_key(item) == room_key
    ]


def stream_kind(url: str) -> str:
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8"):
        return "m3u8"
    if path.endswith(".flv"):
        return "flv"
    return "unknown"



def stream_expiry_datetime(url: str) -> datetime | None:
    """Đọc thời hạn phổ biến từ auth_key hoặc txTime."""
    try:
        query = parse_qs(
            urlsplit(url).query,
            keep_blank_values=True,
        )
    except Exception:
        return None

    candidates: list[int] = []

    auth_key = (query.get("auth_key") or [""])[0]
    if auth_key:
        first = auth_key.split("-", 1)[0]
        if first.isdigit():
            candidates.append(int(first))

    tx_time = (query.get("txTime") or query.get("txtime") or [""])[0]
    if tx_time:
        try:
            candidates.append(int(tx_time, 16))
        except ValueError:
            pass

    for key in ("expires", "expire", "expiry", "e"):
        value = (query.get(key) or [""])[0]
        if value.isdigit():
            candidates.append(int(value))

    plausible = [
        value
        for value in candidates
        if 1_500_000_000 <= value <= 4_000_000_000
    ]
    if not plausible:
        return None

    return datetime.fromtimestamp(
        max(plausible),
        tz=ZoneInfo("UTC"),
    ).astimezone(TIMEZONE)


def stream_item_expiry(item: dict[str, Any]) -> datetime | None:
    stored = parse_iso_datetime(
        str(item.get("valid_until", "") or "")
    )
    if stored is not None:
        return stored
    return stream_expiry_datetime(
        str(item.get("url", "") or "")
    )


def stream_item_is_expired(
    item: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    expiry = stream_item_expiry(item)
    if expiry is None:
        return False

    current = now or datetime.now(TIMEZONE)
    return expiry <= current + timedelta(
        seconds=TOKEN_EXPIRY_GRACE_SECONDS
    )


def enrich_stream_item(
    item: dict[str, Any],
    discovered_at: str | None = None,
) -> dict[str, Any]:
    output = dict(item)
    output["discovered_at"] = (
        clean_text(str(output.get("discovered_at", "") or ""))
        or discovered_at
        or datetime.now(TIMEZONE).isoformat()
    )
    expiry = stream_expiry_datetime(
        str(output.get("url", "") or "")
    )
    output["valid_until"] = (
        expiry.isoformat() if expiry else ""
    )
    output["is_expired"] = stream_item_is_expired(output)
    return output


def stream_preference_key(item: dict[str, Any]) -> tuple[int, float, float, int, int]:
    expiry = stream_item_expiry(item)
    discovered = parse_iso_datetime(
        str(item.get("discovered_at", "") or "")
    )
    return (
        0 if not stream_item_is_expired(item) else 1,
        -(expiry.timestamp() if expiry else 0.0),
        -(discovered.timestamp() if discovered else 0.0),
        0 if item.get("source_confidence") == "network" else 1,
        -len(str(item.get("url", "") or "")),
    )


def prune_expired_streams(match: dict[str, Any]) -> int:
    now = datetime.now(TIMEZONE)
    kept: list[dict[str, Any]] = []
    expired_room_keys: set[str] = set()

    for raw in match.get("streams", []):
        if not isinstance(raw, dict):
            continue
        item = enrich_stream_item(raw)
        if stream_item_is_expired(item, now):
            expired_room_keys.add(stream_queue_key(item))
            continue
        kept.append(item)

    removed = len(match.get("streams", [])) - len(kept)
    match["streams"] = kept

    progress = match.get("room_progress", {})
    for room_key in expired_room_keys:
        room_state = progress.get(room_key)
        if not isinstance(room_state, dict):
            continue
        if not streams_for_room_key(match, room_key):
            room_state["status"] = "retry"
            room_state["attempts"] = 0
            room_state["last_stream_count"] = 0
            room_state["last_error"] = (
                "Link ký số đã hết hạn; đưa BLV trở lại queue."
            )

    return removed


def limit_streams_per_room(
    match: dict[str, Any],
) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}

    for raw in match.get("streams", []):
        if not isinstance(raw, dict):
            continue
        item = enrich_stream_item(raw)
        if stream_item_is_expired(item):
            continue
        grouped.setdefault(
            stream_queue_key(item),
            [],
        ).append(item)

    kept: list[dict[str, Any]] = []

    for items in grouped.values():
        best_by_path: dict[
            tuple[str, str, str],
            dict[str, Any],
        ] = {}

        for item in items:
            url = clean_text(str(item.get("url", "") or ""))
            if not url:
                continue
            parsed = urlsplit(url)
            path_key = (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
            )
            previous = best_by_path.get(path_key)
            if (
                previous is None
                or stream_preference_key(item)
                < stream_preference_key(previous)
            ):
                best_by_path[path_key] = item

        ranked = sorted(
            best_by_path.values(),
            key=stream_preference_key,
        )
        kept.extend(ranked[:MAX_STREAMS_PER_BLV])

    match["streams"] = kept


def match_found_blv_ids(
    match: dict[str, Any],
) -> set[str]:
    found: set[str] = set()

    for item in match.get("streams", []):
        room_id = clean_text(
            item.get("blv_id", "")
        ) or stream_blv_id(
            item.get("url", "")
        )
        if room_id:
            found.add(room_id)

    return found


def match_stream_goal(
    match: dict[str, Any],
) -> int:
    room_count = len(match_room_map(match))
    if room_count <= 0:
        room_count = max(
            1,
            len(unique_rooms(match.get("rooms", []))),
        )

    return max(
        1,
        min(
            room_count * MAX_STREAMS_PER_BLV,
            MAX_STREAMS_PER_MATCH,
        ),
    )


def match_coverage_complete(
    match: dict[str, Any],
) -> bool:
    source_goal_reached = (
        len(match.get("streams", []))
        >= match_stream_goal(match)
    )

    return (
        source_goal_reached
        or room_queue_complete(match)
    )


def update_match_coverage(
    match: dict[str, Any],
) -> None:
    room_ids = match_found_blv_ids(match)
    goal = match_stream_goal(match)

    progress = ensure_room_progress(match)
    pending_keys = pending_room_keys(match)

    match["coverage"] = {
        "found_streams": len(
            match.get("streams", [])
        ),
        "found_blv_ids": sorted(room_ids),
        "goal": goal,
        "source_goal_reached":
            len(match.get("streams", [])) >= goal,
        "room_queue_complete":
            room_queue_complete(match),
        "complete":
            len(match.get("streams", [])) >= goal
            or room_queue_complete(match),
        "max_streams_per_blv":
            MAX_STREAMS_PER_BLV,
        "total_explicit_blv_rooms": len(
            explicit_blv_rooms(match)
        ),
        "pending_room_keys": pending_keys,
        "room_progress": progress,
    }


def dedup_match_streams(
    match: dict[str, Any],
) -> None:
    prune_expired_streams(match)
    best: dict[tuple[str, str, str], dict[str, Any]] = {}

    for raw in match.get("streams", []):
        if not isinstance(raw, dict):
            continue
        item = enrich_stream_item(raw)
        stream_url = clean_text(str(item.get("url", "") or ""))
        if not stream_url or stream_item_is_expired(item):
            continue

        parsed = urlsplit(stream_url)
        key = (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
        )
        previous = best.get(key)
        if (
            previous is None
            or stream_preference_key(item)
            < stream_preference_key(previous)
        ):
            best[key] = item

    match["streams"] = list(best.values())
    limit_streams_per_room(match)
    update_match_coverage(match)


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
    base_rooms_output: list[dict[str, str]] = []

    for room in rooms:
        url = clean_text(room.get("url", ""))
        if not url:
            continue

        blv_id = (
            clean_text(room.get("blv_id", ""))
            or room_blv_id(url)
        )
        dedup_key = (
            f"blv:{blv_id}"
            if blv_id
            else f"base:{canonical_match_url(url)}"
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        item = {
            "url": url,
            "blv": clean_text(room.get("blv", "")),
            "blv_id": blv_id,
        }

        if blv_id:
            with_blv.append(item)
        else:
            base_rooms_output.append(item)

    return with_blv + base_rooms_output


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
                    "attempted_room_ids": [],
                    "coverage": {},
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
) -> dict[str, Any]:
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

                const roomLinks = [];
                const roomSeen = new Set();

                for (
                    const anchor of Array.from(
                        document.querySelectorAll(
                            'a[href*="blv="]'
                        )
                    )
                ) {
                    try {
                        const absolute = new URL(
                            anchor.href,
                            location.href
                        );
                        const roomId =
                            absolute.searchParams.get("blv") || "";

                        if (!roomId) continue;

                        // Chỉ lấy BLV thuộc đúng trang trận hiện tại.
                        if (
                            absolute.pathname.replace(/\/$/, "") !==
                            location.pathname.replace(/\/$/, "")
                        ) {
                            continue;
                        }

                        const key =
                            absolute.pathname + "?blv=" + roomId;
                        if (roomSeen.has(key)) continue;
                        roomSeen.add(key);

                        roomLinks.push({
                            url: absolute.href,
                            blv_id: roomId,
                            blv:
                                clean(anchor.innerText) ||
                                clean(anchor.getAttribute("title")) ||
                                clean(anchor.getAttribute("aria-label")) ||
                                roomId,
                        });
                    } catch (_) {}
                }

                return {
                    h1,
                    home_team: teams[0]?.name || "",
                    away_team: teams[1]?.name || "",
                    home_logo: teams[0]?.logo || "",
                    away_logo: teams[1]?.logo || "",
                    active_blv: activeBlv,
                    rooms: roomLinks,
                };
            }"""
        )

        output: dict[str, Any] = {
            key: clean_text(str(data.get(key, "") or ""))
            for key in (
                "h1",
                "home_team",
                "away_team",
                "home_logo",
                "away_logo",
                "active_blv",
            )
        }

        raw_rooms = data.get("rooms", [])
        output["rooms"] = unique_rooms(
            [
                {
                    "url": clean_text(
                        str(item.get("url", "") or "")
                    ),
                    "blv": clean_text(
                        str(item.get("blv", "") or "")
                    ),
                    "blv_id": clean_text(
                        str(item.get("blv_id", "") or "")
                    ),
                }
                for item in raw_rooms
                if isinstance(item, dict)
            ]
        )

        return output
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


class RateLimitCircuit:
    def __init__(self) -> None:
        self.triggered = asyncio.Event()
        self.first_url = ""
        self.first_status = 0

    def trip(self, url: str, status: int = 429) -> None:
        if not self.triggered.is_set():
            self.first_url = url
            self.first_status = status
            print(
                "\n🛑 PHÁT HIỆN HTTP 429 — DỪNG MỞ TRANG MỚI, "
                "GIỮ TOÀN BỘ LINK ĐÃ LẤY ĐƯỢC.",
                flush=True,
            )
        self.triggered.set()


def match_priority_key(match: dict[str, Any]) -> tuple[int, float, str]:
    """
    Ưu tiên:
      1. Card ghi đang diễn ra/trực tiếp.
      2. Trận đã bắt đầu gần đây.
      3. Trận sắp bắt đầu.
    """
    now = datetime.now(TIMEZONE)
    status = clean_text(match.get("status", "")).lower()
    scheduled = match_datetime_from_url(match.get("base_url", ""))

    live_flag = 0 if any(word in status for word in LIVE_STATUS_WORDS) else 1

    if scheduled is None:
        distance = 10**9
    else:
        delta_minutes = (now - scheduled).total_seconds() / 60.0
        if -30 <= delta_minutes <= 180:
            # Đang/sắp đá gần hiện tại.
            distance = abs(delta_minutes)
        elif delta_minutes > 180:
            distance = 100000 + delta_minutes
        else:
            distance = 200000 + abs(delta_minutes)

    return (
        live_flag,
        distance,
        match_display_name(match).lower(),
    )


# ============================================================
# QUÉT MỘT PHÒNG BLV
# ============================================================
async def scan_room(
    context: BrowserContext,
    match: dict[str, Any],
    room: dict[str, str],
    circuit: RateLimitCircuit,
) -> dict[str, Any]:
    room_url = room["url"]
    room_blv = clean_text(room.get("blv", ""))
    stream_urls: set[str] = set()
    stream_sources: dict[str, set[str]] = {}
    stream_first_seen: list[str] = []
    errors: list[str] = []
    failed_requests: list[str] = []
    http_errors: list[str] = []
    stream_event = asyncio.Event()
    started = time.monotonic()
    main_status: int | None = None

    # Tầng 1: HTTP nhẹ. Nếu có stream trong HTML/config thì không mở browser.
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
            "blv_id": room.get("blv_id", "") or room_blv_id(room_url),
            "streams": sorted(http_streams),
            "stream_sources": {
                stream: ["http-first"]
                for stream in sorted(http_streams)
            },
            "stream_first_seen": sorted(http_streams),
            "diagnostics": {
                "scan_mode": "http-first",
                "elapsed_seconds": round(
                    time.monotonic() - started,
                    3,
                ),
            },
            "http_probe": http_probe,
            "errors": errors,
            "failed_requests": failed_requests,
            "http_errors": http_errors,
        }

    # Tầng 2: mở Chromium và chỉ đợi sự kiện stream, không polling DOM liên tục.
    if STOP_ON_429 and circuit.triggered.is_set():
        return {
            "url": room_url,
            "blv": room_blv,
            "blv_id": room.get("blv_id", "") or room_blv_id(room_url),
            "streams": [],
            "stream_sources": {},
            "stream_first_seen": [],
            "diagnostics": {
                "scan_mode": "skipped-after-429",
                "elapsed_seconds": round(
                    time.monotonic() - started,
                    3,
                ),
                "main_status": 429,
            },
            "http_probe": http_probe,
            "errors": ["Bỏ qua vì circuit breaker HTTP 429 đã kích hoạt."],
            "failed_requests": failed_requests,
            "http_errors": http_errors,
        }

    page = await context.new_page()
    cdp = None

    async def route_handler(route: Any) -> None:
        # Không tải các tài nguyên nặng không cần thiết.
        # Giữ script/xhr/fetch/media/other để player vẫn khởi tạo.
        if route.request.resource_type in {
            "image",
            "font",
            "stylesheet",
        }:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)

    def capture(value: str, source: str) -> None:
        for stream in extract_stream_urls_from_value(value):
            stream_sources.setdefault(
                stream,
                set(),
            ).add(source)

            if stream not in stream_urls:
                stream_urls.add(stream)
                stream_first_seen.append(stream)
                stream_event.set()
                print(f"      🎯 [{source}] {stream}")

    def on_request(request: Any) -> None:
        capture(request.url, f"request/{request.resource_type}")

    def on_response(response: Any) -> None:
        capture(response.url, f"response/{response.status}")

        if (
            response.status == 429
            and response.request.resource_type
            == "document"
        ):
            circuit.trip(response.url, 429)

        if response.status >= 400 and len(http_errors) < 15:
            http_errors.append(
                f"{response.status} {response.url[:500]}"
            )

    def on_request_failed(request: Any) -> None:
        if len(failed_requests) >= 15:
            return
        failed_requests.append(
            f"{request.resource_type} {request.url[:400]} "
            f"| {request.failure}"
        )

    def on_page_error(error: Any) -> None:
        if len(errors) < 20:
            errors.append(f"JS: {error}")

    def on_console(message: Any) -> None:
        if (
            message.type in {"error", "warning"}
            and len(errors) < 20
        ):
            value = clean_text(str(message.text))
            if value:
                errors.append(
                    f"console/{message.type}: {value[:600]}"
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
                cdp.on(
                    "Network.requestWillBeSent",
                    lambda event: capture(
                        event.get("request", {}).get("url", ""),
                        "cdp/request",
                    ),
                )
                def on_cdp_response(event: dict[str, Any]) -> None:
                    response_data = event.get("response", {})
                    response_url = response_data.get("url", "")
                    response_status = int(
                        response_data.get("status", 0) or 0
                    )
                    capture(response_url, "cdp/response")
                    response_type = clean_text(
                        str(event.get("type", "") or "")
                    ).lower()
                    if (
                        response_status == 429
                        and response_type == "document"
                    ):
                        circuit.trip(
                            response_url,
                            429,
                        )

                cdp.on(
                    "Network.responseReceived",
                    on_cdp_response,
                )
            except Exception as exc:
                errors.append(
                    f"CDP unavailable: {type(exc).__name__}: {exc}"
                )

        main_response = await page.goto(
            room_url,
            wait_until="domcontentloaded",
            timeout=18000,
            referer=TARGET_URL,
        )
        if main_response is not None:
            main_status = main_response.status

        if main_status == 429:
            circuit.trip(room_url, 429)

        # Nếu stream xuất hiện ngay trong lúc goto thì không làm thêm việc.
        if not stream_event.is_set():
            await page.wait_for_timeout(350)
            await stimulate_player(page)

        metadata = await extract_page_metadata(page, room_url)

        discovered_rooms = metadata.get("rooms", [])
        if discovered_rooms:
            before_count = len(
                unique_rooms(match.get("rooms", []))
            )
            match["rooms"] = unique_rooms(
                match.get("rooms", [])
                + discovered_rooms
            )
            after_count = len(match["rooms"])

            if after_count > before_count:
                print(
                    f"      🧭 Phát hiện thêm "
                    f"{after_count - before_count} BLV "
                    f"trong trang trận | tổng={after_count}",
                    flush=True,
                )

        if metadata.get("active_blv"):
            room_blv = metadata["active_blv"]

        for key in (
            "home_team",
            "away_team",
            "home_logo",
            "away_logo",
        ):
            if not match.get(key) and metadata.get(key):
                match[key] = normalize_absolute_url(
                    metadata[key],
                    room_url,
                )

        if (
            (not match.get("home_team") or not match.get("away_team"))
            and metadata.get("h1")
        ):
            home, away = parse_teams_from_title(metadata["h1"])
            match["home_team"] = match.get("home_team") or home
            match["away_team"] = match.get("away_team") or away

        # Chờ đúng sự kiện stream. Không vòng lặp 700 ms kéo dài 22 giây.
        if not stream_event.is_set():
            try:
                await asyncio.wait_for(
                    stream_event.wait(),
                    timeout=ROOM_WAIT_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

        harvest_rounds = 0
        harvest_started = time.monotonic()
        last_new_stream_at = harvest_started
        previous_stream_count = len(stream_urls)

        if stream_event.is_set():
            minimum_open_seconds = max(
                EXTRA_WAIT_AFTER_STREAM,
                EMBEDDED_HARVEST_WAIT_SECONDS,
            )
            minimum_deadline = (
                harvest_started + minimum_open_seconds
            )
            maximum_deadline = (
                harvest_started + HARVEST_MAX_SECONDS
            )

            while time.monotonic() < maximum_deadline:
                harvest_rounds += 1

                for value in await collect_frame_candidates(page):
                    capture(
                        value,
                        f"harvest-round-{harvest_rounds}",
                    )

                current_count = len(stream_urls)
                now_monotonic = time.monotonic()

                if current_count > previous_stream_count:
                    last_new_stream_at = now_monotonic
                    previous_stream_count = current_count
                    print(
                        f"      🌾 Thu hoạch vòng {harvest_rounds}: "
                        f"đã có {current_count} link",
                        flush=True,
                    )

                if (
                    now_monotonic >= minimum_deadline
                    and (
                        now_monotonic - last_new_stream_at
                    ) >= HARVEST_QUIET_SECONDS
                ):
                    break

                # Cứ ba vòng kích player lại một lần để nguồn dự phòng
                # hoặc playlist phụ có cơ hội được gọi.
                if harvest_rounds % 3 == 0:
                    await stimulate_player(page)

                await page.wait_for_timeout(
                    int(HARVEST_POLL_SECONDS * 1000)
                )
        else:
            # Không có stream network: vẫn quét DOM/JWPlayer một lượt cuối.
            for value in await collect_frame_candidates(page):
                capture(value, "final-no-stream-scan")
            await page.wait_for_timeout(350)

        # Một lượt cuối sau cửa sổ thu hoạch.
        for value in await collect_frame_candidates(page):
            capture(value, "harvest-final")

        await page.wait_for_timeout(250)

        diagnostics = await diagnose_page(page)
        diagnostics["harvest_rounds"] = harvest_rounds
        diagnostics["harvest_window_seconds"] = round(
            time.monotonic() - harvest_started,
            3,
        )
        diagnostics["main_status"] = main_status
        diagnostics["harvested_stream_count"] = len(stream_urls)
        diagnostics["harvested_blv_ids"] = sorted(
            {
                stream_blv_id(url)
                for url in stream_urls
                if stream_blv_id(url)
            }
        )
        diagnostics["scan_mode"] = "browser-event"
        diagnostics["elapsed_seconds"] = round(
            time.monotonic() - started,
            3,
        )

    except Exception as exc:
        diagnostics = {
            "scan_mode": "browser-event",
            "elapsed_seconds": round(
                time.monotonic() - started,
                3,
            ),
            "main_status": main_status,
        }
        errors.append(f"{type(exc).__name__}: {exc}")
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
        "blv_id": room.get("blv_id", "") or room_blv_id(room_url),
        "streams": list(stream_first_seen),
        "stream_sources": {
            stream: sorted(sources)
            for stream, sources in stream_sources.items()
        },
        "stream_first_seen": list(stream_first_seen),
        "discovered_blv_ids": sorted(
            {
                stream_blv_id(url)
                for url in stream_urls
                if stream_blv_id(url)
            }
        ),
        "diagnostics": diagnostics,
        "http_probe": http_probe,
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
    circuit: RateLimitCircuit,
) -> dict[str, Any]:
    async with semaphore:
        name = match_display_name(match)
        scheduled = match_datetime_from_url(
            match["base_url"]
        )
        time_label = (
            scheduled.strftime("%H:%M %d/%m")
            if scheduled
            else match.get("time_text", "")
        )

        ensure_room_progress(match)
        update_match_coverage(match)

        print(
            f"\n⚽ {name} | {time_label}"
            f" | nguồn="
            f"{len(match_found_blv_ids(match))}/"
            f"{match_stream_goal(match)}"
            f" | BLV pending="
            f"{len(pending_room_keys(match))}"
        )

        pages_opened = 0
        attempted_this_run: set[str] = set()
        rate_limited_this_run: set[str] = set()
        completed_this_run: set[str] = set()

        while pages_opened < MAX_ROOMS_PER_MATCH:
            if match_coverage_complete(match):
                break

            if (
                STOP_ON_429
                and circuit.triggered.is_set()
            ):
                print(
                    "      ⏭ Dừng trận vì đã gặp HTTP 429.",
                    flush=True,
                )
                break

            ensure_room_progress(match)
            pending_keys = [
                key
                for key in pending_room_keys(match)
                if key not in attempted_this_run
            ]

            if not pending_keys:
                print(
                    "      ℹ️ Không mở lại cùng BLV trong một workflow.",
                    flush=True,
                )
                break

            next_key = pending_keys[0]
            attempted_this_run.add(next_key)
            room = next(
                (
                    item
                    for item in unique_rooms(
                        match.get("rooms", [])
                    )
                    if room_queue_key(item)
                    == next_key
                ),
                None,
            )

            if room is None:
                match["room_progress"].pop(
                    next_key,
                    None,
                )
                continue

            pages_opened += 1
            progress = match["room_progress"][
                next_key
            ]
            label = (
                room.get("blv")
                or room.get("blv_id")
                or "URL tổng"
            )

            print(
                f"   -> BLV queue {pages_opened}/"
                f"{MAX_ROOMS_PER_MATCH}: "
                f"{label}"
                f" | attempts={progress['attempts']}"
                f" | status={progress['status']}",
                flush=True,
            )

            room_result = await scan_room(
                context,
                match,
                room,
                circuit,
            )
            match.setdefault(
                "room_results",
                [],
            ).append(room_result)

            # scan_room có thể phát hiện thêm 10–12 BLV trong trang.
            ensure_room_progress(match)
            progress = match["room_progress"][
                next_key
            ]

            diagnostics = room_result.get(
                "diagnostics",
                {},
            )
            main_status = diagnostics.get(
                "main_status"
            )
            scan_mode = clean_text(
                str(
                    diagnostics.get(
                        "scan_mode",
                        "",
                    )
                    or ""
                )
            )
            room_has_streams = bool(
                room_result.get("streams")
            )
            rate_limited = bool(
                main_status == 429
                or scan_mode
                == "skipped-after-429"
                or (
                    not room_has_streams
                    and any(
                        "429 " in value
                        for value
                        in room_result.get(
                            "http_errors",
                            [],
                        )
                    )
                )
            )

            if rate_limited:
                progress["status"] = (
                    "rate_limited"
                )
                progress["rate_limit_hits"] += 1
                progress["last_http_status"] = 429
                progress["last_stream_count"] = 0
                progress["last_error"] = (
                    "HTTP 429; giữ nguyên trong "
                    "pending cho runner kế tiếp."
                )

                rate_limited_this_run.add(next_key)
                print(
                    f"      🧱 BLV {label}: HTTP 429"
                    " — không đánh dấu đã quét.",
                    flush=True,
                )
                break

            progress["attempts"] += 1
            progress["last_http_status"] = (
                main_status
            )
            progress["last_stream_count"] = len(
                room_result.get(
                    "streams",
                    [],
                )
            )
            progress["last_error"] = (
                "; ".join(
                    room_result.get(
                        "errors",
                        [],
                    )[:3]
                )
            )

            added = 0
            existing_urls = {
                item.get("url", "")
                for item in match.get(
                    "streams",
                    [],
                )
            }
            sources_by_stream = room_result.get(
                "stream_sources",
                {},
            )
            first_seen = room_result.get(
                "stream_first_seen",
                room_result.get(
                    "streams",
                    [],
                ),
            )
            first_seen_index = {
                value: index
                for index, value in enumerate(
                    first_seen
                )
            }
            ordered_streams = sorted(
                room_result.get(
                    "streams",
                    [],
                ),
                key=lambda value:
                    first_seen_index.get(
                        value,
                        10**9,
                    ),
            )

            primary_assigned = False
            current_room_id = (
                room.get("blv_id", "")
                or room_blv_id(
                    room.get("url", "")
                )
            )
            current_room_blv = (
                room.get("blv", "")
                or current_room_id
                or "URL tổng"
            )
            room_map = match_room_map(match)

            for stream_index, stream in enumerate(
                ordered_streams,
                start=1,
            ):
                if stream in existing_urls:
                    continue

                detected_id = stream_blv_id(
                    stream
                )
                sources = sources_by_stream.get(
                    stream,
                    [],
                )
                exact_room = (
                    room_map.get(detected_id)
                    if detected_id
                    else None
                )
                is_network = any(
                    source.startswith(
                        (
                            "request/",
                            "response/",
                            "cdp/",
                        )
                    )
                    for source in sources
                )

                if exact_room is not None:
                    display_blv = (
                        exact_room.get("blv")
                        or f"Nguồn {detected_id}"
                    )
                    display_room_url = (
                        exact_room.get("url")
                        or room_result["url"]
                    )
                    mapped_blv_id = (
                        exact_room.get("blv_id")
                        or detected_id
                    )
                    mapping_mode = "exact-room-id"
                elif (
                    current_room_id
                    and is_network
                    and not primary_assigned
                ):
                    # Request network đầu tiên của URL ?blv=X
                    # được xem là nguồn chính của BLV X.
                    display_blv = current_room_blv
                    display_room_url = (
                        room_result["url"]
                    )
                    mapped_blv_id = (
                        current_room_id
                    )
                    mapping_mode = (
                        "current-room-primary"
                    )
                    primary_assigned = True
                else:
                    display_blv = (
                        f"Nguồn {detected_id}"
                        if detected_id
                        else f"Nguồn phụ {stream_index}"
                    )
                    display_room_url = (
                        room_result["url"]
                    )
                    mapped_blv_id = detected_id
                    mapping_mode = (
                        "stream-source-id"
                    )

                assigned_queue_key = (
                    detected_id
                    if exact_room is not None
                    else next_key
                )

                if (
                    len(
                        streams_for_room_key(
                            match,
                            assigned_queue_key,
                        )
                    )
                    >= MAX_STREAMS_PER_BLV
                ):
                    continue

                match.setdefault(
                    "streams",
                    [],
                ).append(
                    {
                        "url": stream,
                        "queue_room_key":
                            assigned_queue_key,
                        "room_url":
                            display_room_url,
                        "blv": display_blv,
                        "blv_id":
                            mapped_blv_id,
                        "source_id":
                            detected_id,
                        "source_provenance":
                            sources,
                        "source_confidence":
                            (
                                "network"
                                if is_network
                                else "embedded"
                            ),
                        "mapping_mode":
                            mapping_mode,
                        "discovered_from_room_url":
                            room_result["url"],
                        "discovered_from_blv":
                            current_room_blv,
                        "discovered_at":
                            datetime.now(TIMEZONE).isoformat(),
                        "valid_until": (
                            stream_expiry_datetime(stream).isoformat()
                            if stream_expiry_datetime(stream)
                            else ""
                        ),
                    }
                )
                existing_urls.add(stream)
                added += 1

            dedup_match_streams(match)

            current_room_stream_count = len(
                streams_for_room_key(
                    match,
                    next_key,
                )
            )

            if (
                current_room_stream_count
                >= MAX_STREAMS_PER_BLV
            ):
                progress["status"] = "success"
            elif (
                current_room_stream_count > 0
                and progress["attempts"]
                < ROOM_EMPTY_MAX_ATTEMPTS
            ):
                progress["status"] = "partial"
            elif current_room_stream_count > 0:
                # Đã thử đủ số lần nhưng BLV chỉ có một nguồn khả dụng.
                progress["status"] = "success"
            elif (
                progress["attempts"]
                >= ROOM_EMPTY_MAX_ATTEMPTS
            ):
                progress["status"] = "exhausted"
            else:
                progress["status"] = "retry"

            if (
                room_result.get("streams")
                and circuit.triggered.is_set()
            ):
                progress["last_error"] = (
                    "Đã giữ stream trước khi "
                    "cầu dao 429 dừng các trang sau."
                )

            match["attempted_room_ids"] = sorted(
                key
                for key, value
                in match["room_progress"].items()
                if (
                    key != "__base__"
                    and value.get("attempts", 0)
                    > 0
                )
            )

            completed_this_run.add(next_key)
            update_match_coverage(match)

            print(
                f"      ✅ BLV {label}: "
                f"+{added} link"
                f" | status={progress['status']}"
                f" | nguồn BLV này="
                f"{current_room_stream_count}/{MAX_STREAMS_PER_BLV}"
                f" | tổng nguồn="
                f"{len(match.get('streams', []))}"
                f" | còn BLV="
                f"{len(pending_room_keys(match))}",
                flush=True,
            )

            await asyncio.sleep(
                DELAY_BETWEEN_ROOMS
            )

        dedup_match_streams(match)
        update_match_coverage(match)
        match["match_name"] = (
            match_display_name(match)
        )
        match["run_attempted_room_keys"] = sorted(
            attempted_this_run
        )
        match["run_rate_limited_room_keys"] = sorted(
            rate_limited_this_run
        )
        match["run_completed_room_keys"] = sorted(
            completed_this_run
        )

        if match.get("streams"):
            print(
                f"   ✅ HOÀN TẤT LƯỢT TRẬN: "
                f"{len(match['streams'])} link"
                f" | pending BLV="
                f"{len(pending_room_keys(match))}",
                flush=True,
            )
        else:
            print(
                "   ❌ Trận này chưa bắt được link",
                flush=True,
            )

        return match


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE)
    except Exception:
        return None


def load_previous_state() -> dict[str, Any]:
    path = Path(OUTPUT_STATE)
    if not path.exists():
        return {}

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        print(
            f"⚠️ Không đọc được state cũ: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return {}

    generated_at = parse_iso_datetime(
        str(payload.get("generated_at", ""))
    )
    if generated_at is None:
        return {}

    age_minutes = (
        datetime.now(TIMEZONE) - generated_at
    ).total_seconds() / 60.0

    if age_minutes > STATE_MAX_AGE_MINUTES:
        print(
            f"ℹ️ State cũ đã {age_minutes:.1f} phút, "
            "không dùng lại link ký cũ.",
            flush=True,
        )
        return {
            "pending_base_urls": payload.get(
                "pending_base_urls",
                [],
            ),
            "matches": [],
            "_state_is_fresh": False,
            "_state_schema_matches": False,
        }

    print(
        f"♻️ Nạp state lần trước: "
        f"{len(payload.get('matches', []))} trận "
        f"| tuổi={age_minutes:.1f} phút",
        flush=True,
    )
    payload["_state_is_fresh"] = True
    payload["_state_schema_matches"] = (
        payload.get("state_schema_version")
        == STATE_SCHEMA_VERSION
    )

    if not payload["_state_schema_matches"]:
        print(
            "🧹 State dùng schema cũ: giữ link còn hạn, "
            "nhưng reset chain và danh sách phòng đã thử.",
            flush=True,
        )

    return payload


def merge_previous_streams(
    matches: list[dict[str, Any]],
    previous_state: dict[str, Any],
) -> int:
    previous_by_url = {
        canonical_match_url(
            item.get("base_url", "")
        ): item
        for item in previous_state.get("matches", [])
        if item.get("base_url")
    }

    merged = 0

    for match in matches:
        key = canonical_match_url(
            match.get("base_url", "")
        )
        previous = previous_by_url.get(key)
        if not previous:
            continue

        old_streams = previous.get(
            "streams",
            [],
        )
        old_attempted = (
            previous.get(
                "attempted_room_ids",
                [],
            )
            if previous_state.get(
                "_state_schema_matches"
            )
            else []
        )
        old_room_progress = (
            previous.get(
                "room_progress",
                {},
            )
            if previous_state.get(
                "_state_schema_matches"
            )
            else {}
        )

        if old_streams:
            match["streams"] = old_streams
        match["room_results"] = previous.get(
            "room_results",
            [],
        )
        match["attempted_room_ids"] = sorted(
            {
                str(value)
                for value in old_attempted
                if value
            }
        )
        match["room_progress"] = (
            old_room_progress
            if isinstance(
                old_room_progress,
                dict,
            )
            else {}
        )

        ensure_room_progress(match)
        dedup_match_streams(match)

        if (
            old_streams
            or old_attempted
            or old_room_progress
        ):
            merged += 1

    if merged:
        print(
            f"♻️ Giữ lại link mới từ lần trước cho "
            f"{merged} trận.",
            flush=True,
        )

    return merged


def order_pending_first(
    matches: list[dict[str, Any]],
    previous_state: dict[str, Any],
) -> None:
    pending = {
        canonical_match_url(url)
        for url in previous_state.get(
            "pending_base_urls",
            [],
        )
        if url
    }

    matches.sort(
        key=lambda match: (
            0
            if canonical_match_url(
                match.get("base_url", "")
            ) in pending
            else 1,
            *match_priority_key(match),
        )
    )


def build_pending_matches(
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []

    for match in matches:
        update_match_coverage(match)
        if match_coverage_complete(match):
            continue

        pending.append(
            {
                "base_url": match.get("base_url", ""),
                "match_name": match_display_name(match),
                "scheduled_at": match.get(
                    "scheduled_at",
                    "",
                ),
                "rooms": unique_rooms(
                    match.get("rooms", [])
                ),
                "streams": match.get(
                    "streams",
                    [],
                ),
                "coverage": match.get(
                    "coverage",
                    {},
                ),
                "attempted_room_ids": match.get(
                    "attempted_room_ids",
                    [],
                ),
                "room_progress": match.get(
                    "room_progress",
                    {},
                ),
                "pending_room_keys":
                    pending_room_keys(match),
            }
        )

    return pending




def pending_fingerprint(
    pending: list[dict[str, Any]],
) -> str:
    normalized = [
        {
            "base_url": canonical_match_url(
                str(item.get("base_url", "") or "")
            ),
            "room_keys": sorted(
                set(item.get("pending_room_keys", []))
            ),
        }
        for item in pending
    ]
    normalized.sort(key=lambda item: item["base_url"])
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_stream_map(
    matches: list[dict[str, Any]],
    allowed_urls: set[str] | None = None,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    output: dict[tuple[str, str, str], dict[str, Any]] = {}

    for match in matches:
        base_url = canonical_match_url(
            str(match.get("base_url", "") or "")
        )
        if allowed_urls is not None and base_url not in allowed_urls:
            continue

        for raw in match.get("streams", []):
            if not isinstance(raw, dict):
                continue
            item = enrich_stream_item(raw)
            if stream_item_is_expired(item):
                continue
            url = clean_text(str(item.get("url", "") or ""))
            if not url:
                continue
            parsed = urlsplit(url)
            key = (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
            )
            previous = output.get(key)
            if (
                previous is None
                or stream_preference_key(item)
                < stream_preference_key(previous)
            ):
                output[key] = item

    return output


def room_attempt_metric(
    matches: list[dict[str, Any]],
    allowed_urls: set[str] | None = None,
) -> int:
    total = 0
    for match in matches:
        base_url = canonical_match_url(
            str(match.get("base_url", "") or "")
        )
        if allowed_urls is not None and base_url not in allowed_urls:
            continue
        for item in match.get("room_progress", {}).values():
            if not isinstance(item, dict):
                continue
            total += int(item.get("attempts", 0) or 0)
            total += int(item.get("rate_limit_hits", 0) or 0)
    return total

def write_resume_state(
    matches: list[dict[str, Any]],
    circuit: RateLimitCircuit,
    previous_state: dict[str, Any],
) -> dict[str, Any]:
    for match in matches:
        dedup_match_streams(match)

    pending = build_pending_matches(matches)
    generated_at = datetime.now(TIMEZONE).isoformat()

    current_urls = {
        canonical_match_url(
            str(match.get("base_url", "") or "")
        )
        for match in matches
        if match.get("base_url")
    }

    completed_count = sum(
        1 for match in matches if match.get("streams")
    )
    coverage_complete_count = sum(
        1
        for match in matches
        if match_coverage_complete(match)
    )

    current_streams = valid_stream_map(
        matches,
        current_urls,
    )
    previous_matches = previous_state.get("matches", [])
    previous_streams = valid_stream_map(
        previous_matches,
        current_urls,
    )

    new_link_count = len(
        set(current_streams) - set(previous_streams)
    )
    refreshed_link_count = sum(
        1
        for key in set(current_streams) & set(previous_streams)
        if clean_text(
            str(current_streams[key].get("url", "") or "")
        )
        != clean_text(
            str(previous_streams[key].get("url", "") or "")
        )
    )
    progress_count = new_link_count + refreshed_link_count
    total_stream_count = len(current_streams)

    previous_target_urls = {
        canonical_match_url(str(url))
        for url in previous_state.get("target_base_urls", [])
        if url
    }
    state_is_fresh = bool(previous_state.get("_state_is_fresh"))
    same_target = bool(
        state_is_fresh
        and previous_state.get("_state_schema_matches")
        and previous_target_urls
        and previous_target_urls == current_urls
        and not RESET_CHAIN_GUARD
    )

    previous_chain_runs = (
        int(previous_state.get("chain_runs", 0))
        if same_target else 0
    )
    chain_runs = previous_chain_runs + 1

    previous_no_progress = (
        int(previous_state.get("no_progress_runs", 0))
        if same_target else 0
    )
    no_progress_runs = (
        0 if progress_count > 0
        else previous_no_progress + 1
    )

    fingerprint = pending_fingerprint(pending)
    previous_fingerprint = (
        str(previous_state.get("pending_fingerprint", ""))
        if same_target else ""
    )
    previous_unchanged = (
        int(previous_state.get("unchanged_pending_runs", 0))
        if same_target else 0
    )
    unchanged_pending_runs = (
        previous_unchanged + 1
        if pending and fingerprint == previous_fingerprint
        else 0
    )

    current_attempt_metric = room_attempt_metric(
        matches,
        current_urls,
    )
    previous_attempt_metric = room_attempt_metric(
        previous_matches,
        current_urls,
    )
    newly_attempted_count = max(
        0,
        current_attempt_metric - previous_attempt_metric,
    )

    run_attempted_count = sum(
        len(set(match.get("run_attempted_room_keys", [])))
        for match in matches
    )
    run_rate_limited_count = sum(
        len(set(match.get("run_rate_limited_room_keys", [])))
        for match in matches
    )
    rate_limit_only_run = bool(
        circuit.triggered.is_set()
        and progress_count == 0
        and run_attempted_count > 0
        and run_rate_limited_count >= run_attempted_count
    )
    previous_rate_limit_only = (
        int(previous_state.get("rate_limit_only_runs", 0))
        if same_target else 0
    )
    rate_limit_only_runs = (
        previous_rate_limit_only + 1
        if rate_limit_only_run else 0
    )

    has_pending = bool(pending)
    pending_room_count = sum(
        len(item.get("pending_room_keys", []))
        for item in pending
    )

    within_chain_limit = (
        STATE_MAX_CHAIN_RUNS <= 0
        or chain_runs < STATE_MAX_CHAIN_RUNS
    )
    within_no_progress_limit = (
        STATE_MAX_NO_PROGRESS_RUNS <= 0
        or no_progress_runs < STATE_MAX_NO_PROGRESS_RUNS
    )
    within_unchanged_limit = (
        STATE_MAX_UNCHANGED_PENDING_RUNS <= 0
        or unchanged_pending_runs
        < STATE_MAX_UNCHANGED_PENDING_RUNS
    )
    within_rate_limit_only_limit = (
        STATE_MAX_RATE_LIMIT_ONLY_RUNS <= 0
        or rate_limit_only_runs
        < STATE_MAX_RATE_LIMIT_ONLY_RUNS
    )

    useful_activity = bool(
        progress_count > 0
        or newly_attempted_count > 0
        or circuit.triggered.is_set()
    )
    dispatch_next = bool(
        has_pending
        and within_chain_limit
        and within_no_progress_limit
        and within_unchanged_limit
        and within_rate_limit_only_limit
        and useful_activity
    )

    if not has_pending:
        stop_reason = "complete"
    elif not within_chain_limit:
        stop_reason = "max_chain_runs"
    elif not within_no_progress_limit:
        stop_reason = "stalled_no_valid_link_progress"
    elif not within_unchanged_limit:
        stop_reason = "stalled_pending_fingerprint_unchanged"
    elif not within_rate_limit_only_limit:
        stop_reason = "stalled_rate_limit_only"
    elif not useful_activity:
        stop_reason = "no_useful_activity"
    else:
        stop_reason = ""

    decision_payload = {
        "generated_at": generated_at,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "dispatch_next": dispatch_next,
        "pending_count": len(pending),
        "pending_room_count": pending_room_count,
        "completed_count": completed_count,
        "coverage_complete_count": coverage_complete_count,
        "total_match_count": len(matches),
        "total_stream_count": total_stream_count,
        "progress_count": progress_count,
        "new_link_count": new_link_count,
        "refreshed_link_count": refreshed_link_count,
        "newly_attempted_count": newly_attempted_count,
        "chain_runs": chain_runs,
        "no_progress_runs": no_progress_runs,
        "unchanged_pending_runs": unchanged_pending_runs,
        "rate_limit_only_runs": rate_limit_only_runs,
        "rate_limit_only_run": rate_limit_only_run,
        "pending_fingerprint": fingerprint,
        "stop_reason": stop_reason,
        "rate_limited": circuit.triggered.is_set(),
        "rate_limit_url": circuit.first_url,
    }

    state_payload = {
        **decision_payload,
        "target_base_urls": sorted(current_urls),
        "pending_base_urls": [
            item["base_url"]
            for item in pending
            if item.get("base_url")
        ],
        "matches": matches,
    }

    Path(OUTPUT_STATE).write_text(
        json.dumps(state_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Path(OUTPUT_PENDING).write_text(
        json.dumps(
            {**decision_payload, "count": len(pending), "matches": pending},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(OUTPUT_DECISION).write_text(
        json.dumps(decision_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"📈 Tiến triển hợp lệ: +{new_link_count} link mới"
        f" | +{refreshed_link_count} token mới"
        f" | tổng hợp lệ={total_stream_count}"
        f" | pending={len(pending)} trận/{pending_room_count} BLV",
        flush=True,
    )
    print(
        f"🛡️ Stall guard: no-progress={no_progress_runs}/"
        f"{STATE_MAX_NO_PROGRESS_RUNS}"
        f" | pending-không-đổi={unchanged_pending_runs}/"
        f"{STATE_MAX_UNCHANGED_PENDING_RUNS}"
        f" | chỉ-429={rate_limit_only_runs}/"
        f"{STATE_MAX_RATE_LIMIT_ONLY_RUNS}"
        f" | chain={chain_runs}/{STATE_MAX_CHAIN_RUNS}",
        flush=True,
    )

    if dispatch_next:
        print(
            "🚀 Còn BLV thiếu và chưa chạm stall guard — gọi runner mới.",
            flush=True,
        )
    elif pending:
        print(
            f"🛑 TỰ DỪNG CHUỖI: {stop_reason}",
            flush=True,
        )
    else:
        print("✅ Không còn BLV chờ quét.", flush=True)

    return state_payload


# ============================================================
# CATALOG TÍCH LŨY QUA NHIỀU WORKFLOW
# ============================================================
def stream_identity(
    item: dict[str, Any],
) -> tuple[str, str, str]:
    url = clean_text(
        str(item.get("url", "") or "")
    )
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
        )
    except Exception:
        return ("", "", url)


def merge_stream_lists(
    old_streams: list[dict[str, Any]],
    new_streams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[
        tuple[str, str, str],
        dict[str, Any],
    ] = {}

    for raw in list(old_streams) + list(new_streams):
        if not isinstance(raw, dict):
            continue
        item = enrich_stream_item(raw)
        if stream_item_is_expired(item):
            continue
        key = stream_identity(item)
        previous = merged.get(key)
        if (
            previous is None
            or stream_preference_key(item)
            < stream_preference_key(previous)
        ):
            merged[key] = item

    return list(merged.values())


def load_cumulative_catalog(
    previous_state: dict[str, Any],
) -> list[dict[str, Any]]:
    path = Path(OUTPUT_CATALOG)

    if path.exists():
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8")
            )
            raw_matches = (
                payload.get("matches", [])
                if isinstance(payload, dict)
                else payload
            )
            if isinstance(raw_matches, list):
                return [
                    item
                    for item in raw_matches
                    if isinstance(item, dict)
                ]
        except Exception as exc:
            print(
                f"⚠️ Không đọc được catalog cũ: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    # Nâng cấp mềm: lần đầu dùng V10.2, lấy state V10.1 làm catalog nền.
    return [
        dict(item)
        for item in previous_state.get(
            "matches",
            [],
        )
        if (
            isinstance(item, dict)
            and item.get("streams")
        )
    ]


def catalog_match_scheduled_at(
    match: dict[str, Any],
) -> datetime | None:
    scheduled = parse_iso_datetime(
        str(match.get("scheduled_at", "") or "")
    )
    if scheduled is not None:
        return scheduled

    return match_datetime_from_url(
        str(match.get("base_url", "") or "")
    )


def catalog_entry_is_fresh(
    match: dict[str, Any],
    current_urls: set[str],
    now: datetime,
) -> bool:
    base_url = canonical_match_url(
        str(match.get("base_url", "") or "")
    )

    if base_url in current_urls:
        return True

    scheduled = catalog_match_scheduled_at(match)
    if scheduled is not None:
        age_after_start = (
            now - scheduled
        ).total_seconds() / 60.0

        # Giữ cả trận sắp đá và trận đã bắt đầu chưa quá thời gian retention.
        return (
            age_after_start
            <= CATALOG_RETENTION_AFTER_START_MINUTES
        )

    last_seen = parse_iso_datetime(
        str(
            match.get("catalog_last_seen_at", "")
            or match.get("catalog_updated_at", "")
            or ""
        )
    )
    if last_seen is None:
        return False

    unseen_minutes = (
        now - last_seen
    ).total_seconds() / 60.0

    return (
        unseen_minutes
        <= CATALOG_RETENTION_UNSEEN_MINUTES
    )


def merge_cumulative_catalog(
    current_results: list[dict[str, Any]],
    previous_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    now = datetime.now(TIMEZONE)
    generated_at = now.isoformat()

    old_catalog = load_cumulative_catalog(
        previous_state
    )
    old_by_url = {
        canonical_match_url(
            str(item.get("base_url", "") or "")
        ): dict(item)
        for item in old_catalog
        if item.get("base_url")
    }

    current_urls = {
        canonical_match_url(
            str(item.get("base_url", "") or "")
        )
        for item in current_results
        if item.get("base_url")
    }

    before_links = sum(
        len(item.get("streams", []))
        for item in old_catalog
    )

    for current in current_results:
        key = canonical_match_url(
            str(current.get("base_url", "") or "")
        )
        previous = old_by_url.get(key, {})

        merged = dict(previous)

        # Metadata/trạng thái mới ghi đè dữ liệu cũ, nhưng stream được hợp nhất.
        for field, value in current.items():
            if field == "streams":
                continue
            merged[field] = value

        merged["streams"] = merge_stream_lists(
            previous.get("streams", []),
            current.get("streams", []),
        )
        merged["rooms"] = unique_rooms(
            list(previous.get("rooms", []))
            + list(current.get("rooms", []))
        )
        merged["catalog_last_seen_at"] = generated_at

        previous_paths = {
            stream_identity(item)
            for item in previous.get("streams", [])
            if isinstance(item, dict)
        }
        current_paths = {
            stream_identity(item)
            for item in current.get("streams", [])
            if isinstance(item, dict)
        }

        if current_paths - previous_paths:
            merged["catalog_updated_at"] = generated_at
        else:
            merged["catalog_updated_at"] = (
                previous.get("catalog_updated_at")
                or generated_at
            )

        # Chỉ catalog hóa trận đã từng có ít nhất một stream.
        if merged.get("streams"):
            old_by_url[key] = merged

    retained: list[dict[str, Any]] = []
    pruned_matches = 0
    pruned_links = 0

    for item in old_by_url.values():
        if not item.get("streams"):
            continue

        if catalog_entry_is_fresh(
            item,
            current_urls,
            now,
        ):
            retained.append(item)
        else:
            pruned_matches += 1
            pruned_links += len(
                item.get("streams", [])
            )

    retained.sort(
        key=lambda item: (
            catalog_match_scheduled_at(item)
            or now + timedelta(days=365),
            match_display_name(item).lower(),
        )
    )

    after_links = sum(
        len(item.get("streams", []))
        for item in retained
    )

    payload = {
        "generated_at": generated_at,
        "retention_after_start_minutes":
            CATALOG_RETENTION_AFTER_START_MINUTES,
        "retention_unseen_minutes":
            CATALOG_RETENTION_UNSEEN_MINUTES,
        "match_count": sum(
            1 for item in retained
            if item.get("streams")
        ),
        "link_count": after_links,
        "matches": retained,
    }

    Path(OUTPUT_CATALOG).write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    stats = {
        "before_links": before_links,
        "after_links": after_links,
        "added_or_refreshed_links": max(
            0,
            after_links - before_links,
        ),
        "pruned_matches": pruned_matches,
        "pruned_links": pruned_links,
    }

    print(
        f"📚 Catalog tích lũy: "
        f"{after_links} link từ "
        f"{payload['match_count']} trận"
        f" | trước={before_links}"
        f" | loại cũ={pruned_links} link/"
        f"{pruned_matches} trận",
        flush=True,
    )

    return retained, stats


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
                    "max_rooms_per_match_per_run":
                        MAX_ROOMS_PER_MATCH,
                    "max_streams_per_match":
                        MAX_STREAMS_PER_MATCH,
                    "embedded_harvest_wait_seconds":
                        EMBEDDED_HARVEST_WAIT_SECONDS,
                    "room_empty_max_attempts":
                        ROOM_EMPTY_MAX_ATTEMPTS,
                    "max_streams_per_blv":
                        MAX_STREAMS_PER_BLV,
                    "harvest_max_seconds":
                        HARVEST_MAX_SECONDS,
                    "harvest_poll_seconds":
                        HARVEST_POLL_SECONDS,
                    "harvest_quiet_seconds":
                        HARVEST_QUIET_SECONDS,
                    "chain_until_pending_empty":
                        CHAIN_UNTIL_PENDING_EMPTY,
                    "catalog_output":
                        OUTPUT_CATALOG,
                    "catalog_retention_after_start_minutes":
                        CATALOG_RETENTION_AFTER_START_MINUTES,
                    "catalog_retention_unseen_minutes":
                        CATALOG_RETENTION_UNSEEN_MINUTES,
                    "state_max_unchanged_pending_runs":
                        STATE_MAX_UNCHANGED_PENDING_RUNS,
                    "state_max_rate_limit_only_runs":
                        STATE_MAX_RATE_LIMIT_ONLY_RUNS,
                    "resume_state_first":
                        RESUME_STATE_FIRST,
                    "token_expiry_grace_seconds":
                        TOKEN_EXPIRY_GRACE_SECONDS,
                    "max_successful_rooms_per_match":
                        MAX_SUCCESSFUL_ROOMS_PER_MATCH,
                    "http_first": HTTP_FIRST,
                    "http_fetch_timeout_seconds": HTTP_FETCH_TIMEOUT_SECONDS,
                    "enable_cdp": ENABLE_CDP,
                    "stop_on_429": STOP_ON_429,
                    "strategy": "v10.3-safe-chain-valid-streams",
                    "state_schema_version": STATE_SCHEMA_VERSION,
                    "state_max_age_minutes": STATE_MAX_AGE_MINUTES,
                    "state_max_chain_runs": STATE_MAX_CHAIN_RUNS,
                    "state_max_no_progress_runs":
                        STATE_MAX_NO_PROGRESS_RUNS,
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

    # Luôn ghi lại playlist. Nếu catalog đã hết link hợp lệ, file chỉ còn
    # #EXTM3U thay vì giữ nhầm link hết hạn từ workflow trước.
    Path(OUTPUT_M3U).write_text(
        "\n".join(playlist) + "\n",
        encoding="utf-8",
    )

    return count_matches, count_links


# ============================================================
# MAIN
# ============================================================
async def main() -> None:
    print("🥷 SOCOLIVE STREAM SCANNER V10.3 - SAFE CHAIN + VALID STREAM AUDIT")
    print(
        "ℹ️ Test riêng một URL:\n"
        '   python main.py "https://socolivem.cv/truc-tiep/.../?blv=..."'
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

        # Giảm dấu hiệu automation nhưng không can thiệp API player.
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

        previous_state = load_previous_state()

        can_resume_state = bool(
            not direct_urls
            and RESUME_STATE_FIRST
            and not RESET_CHAIN_GUARD
            and previous_state.get("_state_is_fresh")
            and previous_state.get("_state_schema_matches")
            and previous_state.get("pending_base_urls")
            and previous_state.get("matches")
        )

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
        elif can_resume_state:
            matches = copy.deepcopy(
                previous_state.get("matches", [])
            )
            for match in matches:
                match["rooms"] = unique_rooms(
                    match.get("rooms", [])
                )
                dedup_match_streams(match)
            order_pending_first(matches, previous_state)
            print(
                f"♻️ Chain resume: dùng trực tiếp queue state "
                f"({len(matches)} trận), không tải lại trang chủ.",
                flush=True,
            )
            Path(OUTPUT_MATCHES).write_text(
                json.dumps(matches, ensure_ascii=False, indent=2),
                encoding="utf-8",
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

        # Direct-test không trộn state. Chain resume đã mang nguyên queue cũ;
        # chỉ homepage mới cần hợp nhất state vào danh sách vừa thu thập.
        if not direct_urls and not can_resume_state:
            merge_previous_streams(
                matches,
                previous_state,
            )
            order_pending_first(
                matches,
                previous_state,
            )
        elif direct_urls:
            matches.sort(key=match_priority_key)

        semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)
        circuit = RateLimitCircuit()

        to_scan = [
            match
            for match in matches
            if not match_coverage_complete(match)
        ]

        print(
            f"📋 Tổng {len(matches)} trận | "
            f"cần quét trong run này: {len(to_scan)}",
            flush=True,
        )

        scanned_results = await asyncio.gather(
            *[
                scan_match(
                    context,
                    match,
                    semaphore,
                    circuit,
                )
                for match in to_scan
            ]
        )

        scanned_by_url = {
            canonical_match_url(
                item.get("base_url", "")
            ): item
            for item in scanned_results
        }

        results = [
            scanned_by_url.get(
                canonical_match_url(
                    match.get("base_url", "")
                ),
                match,
            )
            for match in matches
        ]

        if circuit.triggered.is_set():
            print(
                f"⚠️ Đã dừng sớm vì HTTP 429 tại: "
                f"{circuit.first_url}",
                flush=True,
            )

        # State/pending chỉ quản lý tập trận đang nằm trong khung quét.
        resume_state = write_resume_state(
            results,
            circuit,
            previous_state,
        )

        # M3U không dựng trực tiếp từ riêng run hiện tại nữa.
        # Catalog hợp nhất link của mọi workflow trước rồi mới xuất playlist.
        catalog_matches, catalog_stats = (
            merge_cumulative_catalog(
                results,
                previous_state,
            )
        )
        count_matches, count_links = write_outputs(
            catalog_matches
        )

        run_active_links = sum(
            len(match.get("streams", []))
            for match in results
        )
        run_new_links = int(
            resume_state.get("progress_count", 0)
        )
        pending_count = int(
            resume_state.get("pending_count", 0)
        )

        print(
            f"\n🧩 RUN HIỆN TẠI: "
            f"mới +{run_new_links} link"
            f" | active-state={run_active_links} link"
            f" | pending={pending_count}",
            flush=True,
        )

        if count_links:
            if pending_count == 0:
                print(
                    f"🎉 CHUỖI HOÀN TẤT: "
                    f"playlist tích lũy có "
                    f"{count_links} link từ "
                    f"{count_matches} trận."
                )
            else:
                print(
                    f"💾 CHECKPOINT: playlist tích lũy hiện có "
                    f"{count_links} link từ "
                    f"{count_matches} trận; "
                    "workflow sau sẽ tiếp tục BLV còn thiếu."
                )

            print(
                f"📺 Playlist tích lũy: "
                f"{Path(OUTPUT_M3U).resolve()}"
            )
            print(
                f"📚 Catalog tích lũy: "
                f"{Path(OUTPUT_CATALOG).resolve()}"
            )
        else:
            print(
                "\n❌ Catalog chưa có stream trực tiếp nào."
            )

        print(
            f"⚽ Metadata trận/logo: "
            f"{Path(OUTPUT_MATCHES).resolve()}"
        )
        print(
            f"🧾 Debug chi tiết: "
            f"{Path(OUTPUT_DEBUG).resolve()}"
        )
        print(
            f"💾 State tiếp tục: "
            f"{Path(OUTPUT_STATE).resolve()}"
        )
        print(
            f"⏭ Danh sách chưa quét: "
            f"{Path(OUTPUT_PENDING).resolve()}"
        )
        print(
            f"🧭 Quyết định chain hiện tại: "
            f"{Path(OUTPUT_DECISION).resolve()}"
        )

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
