import html
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup


RSS_URL = os.environ.get(
    "RSS_URL",
    "http://vocaro.wikidot.com/feed/pages/pagename/recently-translated-lyrics/category/_default/tags/%EB%85%B8%EB%9E%98/order/created_at+desc/limit/100/t/%EB%B3%B4%EC%B9%B4%EB%A1%9C+%EA%B0%80%EC%82%AC+%EC%9C%84%ED%82%A4+%EC%B5%9C%EA%B7%BC+%EA%B0%80%EC%82%AC",
).strip()

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"].strip()

STATE_FILE = os.environ.get("STATE_FILE", "seen_pages.json").strip()
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "60"))
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Asia/Tokyo").strip()

REQUIRE_CREATION_KEYWORD = (
    os.environ.get("REQUIRE_CREATION_KEYWORD", "false").lower() == "true"
)

SEND_ON_FIRST_RUN = (
    os.environ.get("SEND_ON_FIRST_RUN", "false").lower() == "true"
)

CREATION_KEYWORDS = [
    item.strip().lower()
    for item in os.environ.get(
        "CREATION_KEYWORDS",
        "created,new page,page created,created page,생성,새 페이지",
    ).split(",")
    if item.strip()
]

EXCLUDE_PATTERNS = [
    item.strip().lower()
    for item in os.environ.get(
        "EXCLUDE_PATTERNS",
        "/forum/,/system:,/category:,/tag:,/nav:,/admin:",
    ).split(",")
    if item.strip()
]

DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "보카로 가사 위키 알림").strip()
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()


NICONICO_ID_RE = re.compile(
    r"\b([a-z]{2}\d+)\b",
    re.IGNORECASE,
)

YOUTUBE_ID_RE = re.compile(
    r"\b([A-Za-z0-9_-]{11})\b"
)


INFO_FIELD_RULES = {
    "composer": {
        "label": "작곡",
        "classes": {"composer-cell"},
    },
    "writer": {
        "label": "작사",
        "classes": {"writer-cell"},
    },
    "vocals": {
        "label": "노래",
        "classes": {"vocaro-cell", "vocal-cell", "singer-cell"},
    },
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def escape_markdown_text(value):
    """
    Discord Markdown 링크 텍스트 안에서 깨질 수 있는 일부 문자를 이스케이프한다.
    """
    if not value:
        return ""

    replacements = {
        "\\": "\\\\",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
    }

    for src, dst in replacements.items():
        value = value.replace(src, dst)

    return value


def markdown_link(text, url):
    text = clean_text(text)
    url = clean_text(url)

    if not text:
        return ""

    if not url:
        return escape_markdown_text(text)

    return f"[{escape_markdown_text(text)}]({url})"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "initialized": False,
            "seen_links": [],
        }

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    if "initialized" not in state:
        state["initialized"] = False

    if "seen_links" not in state:
        state["seen_links"] = []

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def fetch_feed():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 WikidotDiscordNotifier/1.0 "
            "(GitHub Actions RSS checker)"
        ),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }

    response = requests.get(
        RSS_URL,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    print(f"[정보] HTTP status: {response.status_code}")
    print(f"[정보] Final URL: {response.url}")
    print(f"[정보] Response length: {len(response.content)} bytes")

    response.raise_for_status()

    feed = feedparser.parse(response.content)

    if feed.bozo:
        print(f"[경고] RSS 파싱 경고: {feed.bozo_exception}")

    print(f"[정보] RSS 항목 수: {len(feed.entries)}")

    return feed


def pick_entry_link(entry):
    candidates = [
        entry.get("link", ""),
        entry.get("id", ""),
        entry.get("guid", ""),
    ]

    for candidate in candidates:
        candidate = clean_text(candidate)

        if not candidate:
            continue

        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate

        if candidate.startswith("/"):
            return urljoin(RSS_URL, candidate)

    return ""


def get_entry_html(entry):
    parts = []

    for key in ("summary", "description"):
        value = entry.get(key, "")
        if value:
            parts.append(str(value))

    content_items = entry.get("content", [])
    if isinstance(content_items, list):
        for item in content_items:
            value = item.get("value", "")
            if value:
                parts.append(str(value))

    return "\n".join(parts)


def extract_niconico_id_from_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if "nicovideo.jp" not in host:
        return None

    # http://www.nicovideo.jp/watch/sm35202505
    # https://embed.nicovideo.jp/watch/sm35202505
    match = re.search(r"/watch/([a-z]{2}\d+)", path, re.IGNORECASE)

    if match:
        return match.group(1)

    return None


def extract_youtube_id_from_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    query = parse_qs(parsed.query)

    # https://www.youtube.com/watch?v=xxxxxxxxxxx
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        video_ids = query.get("v")

        if video_ids:
            candidate = video_ids[0]
            if YOUTUBE_ID_RE.fullmatch(candidate):
                return candidate

        # https://www.youtube.com/embed/xxxxxxxxxxx
        # https://www.youtube-nocookie.com/embed/xxxxxxxxxxx
        match = re.search(r"/embed/([A-Za-z0-9_-]{11})", path)

        if match:
            return match.group(1)

        # https://www.youtube.com/shorts/xxxxxxxxxxx
        match = re.search(r"/shorts/([A-Za-z0-9_-]{11})", path)

        if match:
            return match.group(1)

    # https://youtu.be/xxxxxxxxxxx
    if "youtu.be" in host:
        candidate = path.strip("/").split("/")[0]

        if YOUTUBE_ID_RE.fullmatch(candidate):
            return candidate

    return None


def normalize_original_source_from_url(url):
    url = clean_text(url)

    if not url:
        return None, None

    niconico_id = extract_niconico_id_from_url(url)
    if niconico_id:
        return niconico_id, f"http://www.nicovideo.jp/watch/{niconico_id}"

    youtube_id = extract_youtube_id_from_url(url)
    if youtube_id:
        return youtube_id, f"https://www.youtube.com/watch?v={youtube_id}"

    return None, None


def extract_original_source(raw_html):
    """
    원본 출처 링크를 추출한다.

    지원:
    - NicoNico: http://www.nicovideo.jp/watch/sm35202505
    - NicoNico embed: https://embed.nicovideo.jp/watch/sm35202505
    - YouTube: https://www.youtube.com/watch?v=xxxxxxxxxxx
    - YouTube short: https://youtu.be/xxxxxxxxxxx
    - YouTube embed: https://www.youtube.com/embed/xxxxxxxxxxx
    - YouTube nocookie embed: https://www.youtube-nocookie.com/embed/xxxxxxxxxxx
    """

    if not raw_html:
        return None, None

    decoded = html.unescape(raw_html)
    soup = BeautifulSoup(decoded, "html.parser")

    # 1. 정보표의 <a href="...">에서 먼저 찾기
    for a in soup.find_all("a"):
        href = clean_text(a.get("href", ""))

        if not href:
            continue

        source_id, source_url = normalize_original_source_from_url(href)

        if source_id and source_url:
            return source_id, source_url

    # 2. iframe src 등 HTML 전체 URL에서 찾기
    for tag in soup.find_all(["iframe", "embed", "source"]):
        src = clean_text(tag.get("src", ""))

        if not src:
            continue

        source_id, source_url = normalize_original_source_from_url(src)

        if source_id and source_url:
            return source_id, source_url

    # 3. data-attribute="sm35202505" 또는 data-attribute="YouTubeID" 대응
    for tag in soup.find_all(attrs={"data-attribute": True}):
        value = clean_text(tag.get("data-attribute", ""))

        if not value:
            continue

        if NICONICO_ID_RE.fullmatch(value):
            return value, f"http://www.nicovideo.jp/watch/{value}"

        if YOUTUBE_ID_RE.fullmatch(value):
            return value, f"https://www.youtube.com/watch?v={value}"

    # 4. 최후 fallback: raw HTML 문자열에서 직접 검색
    niconico_match = re.search(
        r"https?://(?:www\.|embed\.)?nicovideo\.jp/watch/([a-z]{2}\d+)",
        decoded,
        re.IGNORECASE,
    )

    if niconico_match:
        source_id = niconico_match.group(1)
        return source_id, f"http://www.nicovideo.jp/watch/{source_id}"

    youtube_patterns = [
        r"https?://(?:www\.)?youtube\.com/watch\?[^\"'\s<>]*v=([A-Za-z0-9_-]{11})",
        r"https?://youtu\.be/([A-Za-z0-9_-]{11})",
        r"https?://(?:www\.)?youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"https?://(?:www\.)?youtube-nocookie\.com/embed/([A-Za-z0-9_-]{11})",
        r"https?://(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]

    for pattern in youtube_patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)

        if match:
            source_id = match.group(1)
            return source_id, f"https://www.youtube.com/watch?v={source_id}"

    return None, None


def parse_cell_items(td):
    """
    <td> 안의 링크들을 Discord Markdown 링크 목록으로 변환한다.
    링크가 없으면 텍스트만 반환한다.
    """

    if td is None:
        return []

    items = []
    seen = set()

    for a in td.find_all("a"):
        text = clean_text(a.get_text(" ", strip=True))
        href = clean_text(a.get("href", ""))

        if not text:
            continue

        if href.startswith("javascript:"):
            href = ""

        if href:
            href = urljoin(RSS_URL, href)

        key = (text, href)
        if key in seen:
            continue

        seen.add(key)
        items.append({
            "text": text,
            "url": href,
        })

    if items:
        return items

    fallback_text = clean_text(td.get_text(" ", strip=True))
    if fallback_text:
        return [{
            "text": fallback_text,
            "url": "",
        }]

    return []


def match_info_field(th):
    if th is None:
        return None

    th_text = clean_text(th.get_text(" ", strip=True))
    th_classes = set(th.get("class", []))

    for field_key, rule in INFO_FIELD_RULES.items():
        label = rule["label"]
        classes = rule["classes"]

        if label and th_text == label:
            return field_key

        if th_classes & classes:
            return field_key

    return None


def extract_info_fields(raw_html):
    """
    Wikidot 정보 테이블에서 작곡/작사/노래 정보를 추출한다.

    반환 예:
    {
        "composer": [{"text": "Crusher", "url": "http://vocaro.wikidot.com/artist:crusher"}],
        "writer": [{"text": "Crusher", "url": "http://vocaro.wikidot.com/artist:crusher"}],
        "vocals": [{"text": "하츠네 미쿠 English", "url": "http://vocaro.wikidot.com/hatsune-miku"}]
    }
    """

    result = {
        "composer": [],
        "writer": [],
        "vocals": [],
    }

    if not raw_html:
        return result

    decoded = html.unescape(raw_html)
    soup = BeautifulSoup(decoded, "html.parser")

    for tr in soup.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")

        field_key = match_info_field(th)

        if not field_key:
            continue

        items = parse_cell_items(td)

        if items:
            result[field_key] = items

    return result


def format_info_items(items):
    if not items:
        return ""

    formatted = []

    for item in items:
        text = item.get("text", "")
        url = item.get("url", "")
        value = markdown_link(text, url)

        if value:
            formatted.append(value)

    return ", ".join(formatted)


def format_pubdate(pubdate):
    """
    RSS pubDate를 yyyy.mm.dd hh:mm 형식으로 변환한다.
    DISPLAY_TIMEZONE 기본값은 Asia/Tokyo.
    """

    if not pubdate:
        return ""

    try:
        dt = parsedate_to_datetime(pubdate)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        dt = dt.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
        return dt.strftime("%Y.%m.%d %H:%M")

    except Exception as exc:
        print(f"[경고] pubDate 변환 실패: {pubdate} / {exc}")
        return clean_text(pubdate)


def normalize_entry(entry):
    raw_html = get_entry_html(entry)
    source_id, source_url = extract_original_source(raw_html)
    info_fields = extract_info_fields(raw_html)

    pubdate = clean_text(
        entry.get("published", "")
        or entry.get("updated", "")
    )

    return {
        "link": pick_entry_link(entry),
        "title": clean_text(entry.get("title", "")),
        "summary": clean_text(entry.get("summary", "") or entry.get("description", "")),
        "published": pubdate,
        "published_display": format_pubdate(pubdate),
        "raw_html": raw_html,
        "source_id": source_id,
        "source_url": source_url,
        "composer_items": info_fields.get("composer", []),
        "writer_items": info_fields.get("writer", []),
        "vocal_items": info_fields.get("vocals", []),
    }


def is_excluded(entry):
    text = f"{entry['title']} {entry['summary']} {entry['link']}".lower()
    return any(pattern in text for pattern in EXCLUDE_PATTERNS)


def looks_like_created_page(entry):
    if not REQUIRE_CREATION_KEYWORD:
        return True

    text = f"{entry['title']} {entry['summary']}".lower()
    return any(keyword in text for keyword in CREATION_KEYWORDS)


def build_embed_description(entry):
    lines = []

    if entry.get("source_id") and entry.get("source_url"):
        lines.append(f"원본: {markdown_link(entry['source_id'], entry['source_url'])}")

    if entry.get("published_display"):
        lines.append(f"작성일: {entry['published_display']}")

    info_lines = []

    composer = format_info_items(entry.get("composer_items", []))
    writer = format_info_items(entry.get("writer_items", []))
    vocals = format_info_items(entry.get("vocal_items", []))

    if composer:
        info_lines.append(f"작곡: {composer}")

    if writer:
        info_lines.append(f"작사: {writer}")

    if vocals:
        info_lines.append(f"노래: {vocals}")

    if info_lines:
        if lines:
            lines.append("")
        lines.append("**정보**")
        lines.extend(info_lines)

    if entry.get("link"):
        lines.append("")
        lines.append(f"🔗 {markdown_link('가사 페이지 열기', entry['link'])}")

    return "\n".join(lines)


def send_to_discord(entry):
    title = entry["title"] or "새 Wikidot 페이지"
    link = entry["link"]

    payload = {
        "username": DISCORD_USERNAME,
        "content": DISCORD_MENTION or None,
        "embeds": [
            {
                "title": f"🎵 {title}"[:256],
                "url": link,
                "description": build_embed_description(entry)[:4096],
                "color": 0x4DB6AC,
                "footer": {
                    "text": "보카로 가사 위키 최근 가사"
                },
                "timestamp": now_iso(),
            }
        ],
    }

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=20,
    )

    print(f"[정보] Discord status: {response.status_code}")
    response.raise_for_status()


def trim_seen_links(seen_links, max_items=3000):
    return seen_links[-max_items:]


def main():
    print(f"[정보] RSS 확인: {RSS_URL}")

    state = load_state()
    first_run = not state.get("initialized", False)

    print(f"[정보] initialized: {state.get('initialized', False)}")
    print(f"[정보] 기존 seen_links 수: {len(state.get('seen_links', []))}")

    seen_links = list(state["seen_links"])
    seen_set = set(seen_links)

    feed = fetch_feed()

    if len(feed.entries) == 0:
        raise RuntimeError("RSS entries are empty")

    entries = [normalize_entry(entry) for entry in feed.entries]
    entries = [entry for entry in entries if entry["link"]]

    print(f"[정보] link가 있는 RSS 항목 수: {len(entries)}")

    changed = False
    alerts = []

    # 오래된 항목부터 처리해야 Discord 알림 순서가 자연스럽다.
    for entry in reversed(entries):
        link = entry["link"]

        if link in seen_set:
            continue

        seen_links.append(link)
        seen_set.add(link)
        changed = True

        if first_run and not SEND_ON_FIRST_RUN:
            print(f"[초기화] 기존 항목 저장만 함: {entry['title']} / {link}")
            continue

        if is_excluded(entry):
            print(f"[제외] 제외 패턴 일치: {entry['title']} / {link}")
            continue

        if not looks_like_created_page(entry):
            print(f"[제외] 생성 이벤트로 판단되지 않음: {entry['title']} / {link}")
            continue

        alerts.append(entry)

    if first_run and not SEND_ON_FIRST_RUN:
        print("[완료] 첫 실행이므로 기존 RSS 항목은 알림하지 않고 저장만 했습니다.")
    elif not alerts:
        print("[완료] 새 페이지 알림 대상 없음")
    else:
        for entry in alerts:
            print(f"[전송] Discord 알림: {entry['title']} / {entry['link']}")
            print(f"       원본: {entry.get('source_id')}")
            print(f"       작성일: {entry.get('published_display')}")
            print(f"       작곡: {format_info_items(entry.get('composer_items', []))}")
            print(f"       작사: {format_info_items(entry.get('writer_items', []))}")
            print(f"       노래: {format_info_items(entry.get('vocal_items', []))}")
            send_to_discord(entry)

        print(f"[완료] Discord 알림 {len(alerts)}건 전송")

    if first_run:
        state["initialized"] = True
        changed = True

    if changed:
        state["seen_links"] = trim_seen_links(seen_links)
        save_state(state)
        print("[정보] 상태 파일 저장 완료")
    else:
        print("[정보] 상태 변경 없음")


if __name__ == "__main__":
    main()
