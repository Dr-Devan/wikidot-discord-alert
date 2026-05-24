import html
import json
import os
import re
from datetime import datetime, timezone

import feedparser
import requests


RSS_URL = os.environ.get(
    "RSS_URL",
    "https://vocaro.wikidot.com/feed/site-changes.xml",
).strip()

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"].strip()
STATE_FILE = os.environ.get("STATE_FILE", "seen_pages.json").strip()

REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "45"))

REQUIRE_CREATION_KEYWORD = (
    os.environ.get("REQUIRE_CREATION_KEYWORD", "true").lower() == "true"
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

DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "Wikidot 알림").strip()
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "").strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


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
        )
    }

    response = requests.get(
        RSS_URL,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)

    if feed.bozo:
        print(f"[경고] RSS 파싱 경고: {feed.bozo_exception}")

    return feed


def normalize_entry(entry):
    link = clean_text(entry.get("link", ""))
    title = clean_text(entry.get("title", ""))
    summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
    published = clean_text(entry.get("published", "") or entry.get("updated", ""))

    return {
        "link": link,
        "title": title,
        "summary": summary,
        "published": published,
    }


def is_excluded(entry):
    text = f"{entry['title']} {entry['summary']} {entry['link']}".lower()
    return any(pattern in text for pattern in EXCLUDE_PATTERNS)


def looks_like_created_page(entry):
    if not REQUIRE_CREATION_KEYWORD:
        return True

    text = f"{entry['title']} {entry['summary']}".lower()
    return any(keyword in text for keyword in CREATION_KEYWORDS)


def send_to_discord(entry):
    title = entry["title"] or "새 Wikidot 페이지"
    link = entry["link"]
    summary = entry["summary"]

    description = summary[:800] if summary else "새 페이지가 생성된 것으로 감지되었습니다."

    payload = {
        "username": DISCORD_USERNAME,
        "content": DISCORD_MENTION or None,
        "embeds": [
            {
                "title": title[:256],
                "url": link,
                "description": f"{description}\n\n[페이지 열기]({link})",
                "color": 0x4DB6AC,
                "footer": {
                    "text": "Wikidot RSS"
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
    response.raise_for_status()


def trim_seen_links(seen_links, max_items=3000):
    return seen_links[-max_items:]


def main():
    print(f"[정보] RSS 확인: {RSS_URL}")

    state = load_state()
    first_run = not state.get("initialized", False)

    seen_links = list(state["seen_links"])
    seen_set = set(seen_links)

    feed = fetch_feed()
    entries = [normalize_entry(entry) for entry in feed.entries]
    entries = [entry for entry in entries if entry["link"]]

    print(f"[정보] RSS 항목 수: {len(entries)}")

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

        if first_run:
            print(f"[초기화] 기존 항목 저장만 함: {entry['title']} / {link}")
            continue

        if is_excluded(entry):
            print(f"[제외] 제외 패턴 일치: {entry['title']} / {link}")
            continue

        if not looks_like_created_page(entry):
            print(f"[제외] 생성 이벤트로 판단되지 않음: {entry['title']} / {link}")
            print(f"       summary: {entry['summary'][:300]}")
            continue

        alerts.append(entry)

    if first_run:
        print("[완료] 첫 실행이므로 기존 RSS 항목은 알림하지 않고 저장만 했습니다.")
    elif not alerts:
        print("[완료] 새 페이지 알림 대상 없음")
    else:
        for entry in alerts:
            print(f"[전송] Discord 알림: {entry['title']} / {entry['link']}")
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
