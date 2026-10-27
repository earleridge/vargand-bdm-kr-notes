import os
import re
import json
import time
import html
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

FORUM_URL = "https://forum.blackdesertm.com/Board?boardNo=7"
STATE_FILE = Path("state.json")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SEND_LATEST = os.getenv("SEND_LATEST_ON_FIRST_RUN", "false").lower() == "true"
MAX_ARTICLES_PER_RUN = int(os.getenv("MAX_ARTICLES_PER_RUN", "3"))

# The Discord role that should be notified for every new qualifying post.
DISCORD_ROLE_ID = "1522619727350857738"

SOURCE_CHUNK_CHARS = 5000
DISCORD_SAFE_LIMIT = 1750

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; VargandBDMNoticeBot/1.0; "
        "+https://github.com)"
    )
}


def fetch_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def clean_text(node):
    for tag in node.select(
        "script, style, nav, header, footer, iframe, noscript, "
        "button, form, svg"
    ):
        tag.decompose()

    for image in node.select("img"):
        alt = (image.get("alt") or "").strip()
        if alt:
            image.replace_with(
                BeautifulSoup(
                    f"\n[Image description: {html.escape(alt)}]\n",
                    "html.parser",
                )
            )
        else:
            image.decompose()

    text = node.get_text("\n", strip=True)
    lines = []
    previous_blank = False
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", html.unescape(raw)).strip()
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        previous_blank = False
        lines.append(line)
    return "\n".join(lines).strip()


def normalize_title(raw_title):
    title = re.sub(r"\s+", " ", raw_title or "").strip()
    title = re.split(r"\s+검은사막 모바일\b", title, maxsplit=1)[0].strip()
    title = re.sub(r"\s+\d+\s*시간 전\b.*$", "", title).strip()
    title = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}.*$", "", title).strip()
    title = re.sub(r"\s+\[\d+\]\s*$", "", title).strip()
    return title




def classify_article(title):
    """Return 'advance' or 'patch'; ignore shops and routine announcements."""
    normalized = re.sub(r"\s+", " ", title or "").strip()
    compact = normalized.replace(" ", "").lower()

    advance_markers = (
        "사전안내",
        "사전공지",
        "업데이트사전안내",
        "패치사전안내",
        "업데이트예고",
        "패치예고",
        "업데이트사전공개",
    )

    if any(marker in compact for marker in advance_markers):
        return "advance"

    if "[패치]" in normalized or "패치노트" in normalized:
        return "patch"

    return None
    


def get_patch_list():
    soup = fetch_soup(FORUM_URL)
    found = []
    seen_ids = set()

    for a in soup.find_all("a", href=True):
        href = html.unescape(a.get("href", "")).strip()
        full_url = urljoin(FORUM_URL, href)
        parsed = urlparse(full_url)
        params = parse_qs(parsed.query)

        board_no = params.get("boardNo", [""])[0]
        content_no = params.get("contentNo", [""])[0]

        if board_no not in {"6", "7"} or not content_no:
            continue

        article_key = f"{board_no}:{content_no}"
        if article_key in seen_ids:
            continue

        title_attribute = normalize_title(a.get("title", ""))
        visible_text = normalize_title(a.get_text(" ", strip=True))

        candidates = [
            candidate
            for candidate in (title_attribute, visible_text)
            if candidate and candidate not in {
                "패치노트",
                "목록",
                "자세히 보기",
            }
        ]

        if not candidates:
            continue

        title = max(candidates, key=len)
        article_type = classify_article(title)

        if article_type is None:
            if board_no == "6":
                print(f"DEBUG board 6 title: {title!r}")
            continue

        seen_ids.add(article_key)
        found.append({
            "title": title,
            "url": full_url,
            "type": article_type,
        })

    if not found:
        raise RuntimeError(
            "No matching advance notices or main patch notes were found. "
            "Check the official forum URL, board numbers, and article titles."
        )

    print(
        f"Found {sum(1 for item in found if item['type'] == 'advance')} "
        "advance notice(s) and "
        f"{sum(1 for item in found if item['type'] == 'patch')} "
        "main patch note(s)."
    )

    return found[:50]
    
    

def get_article_text(url):
    soup = fetch_soup(url)
    selectors = [
        "article",
        '[class*="board-view"]',
        '[class*="boardView"]',
        '[class*="board-detail"]',
        '[class*="detail-content"]',
        '[class*="view-content"]',
        '[class*="content_view"]',
        '[class*="boardContent"]',
        "main",
    ]

    candidates = []
    for selector in selectors:
        try:
            candidates.extend(soup.select(selector))
        except Exception:
            pass

    best_text = ""
    for candidate in candidates:
        candidate_copy = BeautifulSoup(str(candidate), "html.parser")
        text = clean_text(candidate_copy)
        if len(text) > len(best_text):
            best_text = text

    if len(best_text) < 500 and soup.body:
        best_text = clean_text(BeautifulSoup(str(soup.body), "html.parser"))

    image_urls = []
    for image in soup.find_all("img", src=True):
        src = image.get("src", "").strip()
        if not src or src.startswith("data:"):
            continue
        full_src = urljoin(url, src)
        if full_src.startswith("http") and full_src not in image_urls:
            image_urls.append(full_src)

    if image_urls:
        best_text += "\n\n[Images from the official post]\n"
        best_text += "\n".join(image_urls[:25])

    if len(best_text) < 250:
        raise RuntimeError(
            "Article text extraction returned too little text; "
            "stopping to avoid sending an incomplete translation."
        )

    return best_text[:60000]


def split_source_text(text, max_chars=SOURCE_CHUNK_CHARS):
    """Split source text on line boundaries without dropping content."""
    chunks = []
    current = ""

    for paragraph in text.splitlines():
        paragraph = paragraph.rstrip()
        if len(paragraph) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start:start + max_chars])
            continue

        candidate = paragraph if not current else current + "\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current.strip():
                chunks.append(current.strip())
            current = paragraph

    if current.strip():
        chunks.append(current.strip())
    return chunks


def translate_chunk_english(title, article_type, source_chunk, chunk_number, total_chunks):
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    type_label = (
        "ADVANCE NOTICE (upcoming/planned changes)"
        if article_type == "advance"
        else "MAIN PATCH NOTES (actual update/maintenance changes)"
    )

    prompt = f"""
You are a careful Korean-to-English translator for Black Desert Mobile.
Translate the Korean source text below into English for a guild Discord.

Document type: {type_label}
Korean title: {title}
Source segment: {chunk_number} of {total_chunks}

Requirements:
- Translate faithfully and completely. DO NOT summarize, shorten, or omit details.
- Preserve every heading, bullet, date, time, number, percentage, item name, class name,
  requirement, reward, condition, exception, and warning appearing in this segment.
- Keep the same order and structure as the source wherever possible.
- Use clear English. If a proper name is uncertain, retain the Korean name in parentheses.
- Do not invent missing information or add commentary.
- Preserve URLs exactly as written.
- Translate only this source segment.
- Output only the English translation.

KOREAN SOURCE SEGMENT:
{source_chunk}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 6000,
        },
    }

    response = requests.post(endpoint, json=payload, timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini API error {response.status_code}: {response.text[:500]}"
        )

    data = response.json()
    try:
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason", "")
        translated = candidate["content"]["parts"][0]["text"].strip()
        if not translated:
            raise RuntimeError("Gemini returned empty translation text.")
        if finish_reason == "MAX_TOKENS":
            raise RuntimeError(
                "Gemini reached its output limit while translating a segment. "
                "The article was not posted; reduce SOURCE_CHUNK_CHARS and retry."
            )
        return translated
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            "Gemini returned no translation text. The article was not posted."
        )


def translate_english(title, article_type, article_text):
    source_chunks = split_source_text(article_text)
    translations = []

    print(f"Translating {len(source_chunks)} source segment(s) for: {title}")
    for index, chunk in enumerate(source_chunks, start=1):
        translations.append(
            translate_chunk_english(
                title, article_type, chunk, index, len(source_chunks)
            )
        )
        time.sleep(1)

    return "\n\n".join(translations)


def split_for_discord(text, max_chars=DISCORD_SAFE_LIMIT):
    remaining = text.strip()
    parts = []

    while len(remaining) > max_chars:
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars

        part = remaining[:cut].strip()
        if part:
            parts.append(part)
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)
    return parts


def post_to_discord(title, url, translated_text, article_type):
    if article_type == "advance":
        label = "📢 BDM KOREA | ADVANCE NOTICE"
    else:
        label = "🛠️ BDM KOREA | MAIN PATCH NOTES"

    # Ping the role only in the first part to avoid repeated notifications.
    translation_parts = split_for_discord(translated_text)
    total_parts = len(translation_parts)

    for index, part in enumerate(translation_parts, start=1):
        mention = f"<@&{DISCORD_ROLE_ID}>\n" if index == 1 else ""
        header = (
            f"{mention}"
            f"🇰🇷 **VARGAND | {label}**\n"
            f"**{title[:180]}**\n"
            f"**English translation — Part {index}/{total_parts}**\n\n"
        )
        footer = (
            f"\n\n🔗 **Official Korean source:** {url}"
            if index == 1 else ""
        )

        available = 2000 - len(header) - len(footer)
        if len(part) > available:
            raise RuntimeError(
                "Translated section exceeds the safe Discord message size. "
                "No state was saved for this article; lower DISCORD_SAFE_LIMIT."
            )

        content = header + part + footer
        if len(content) > 2000:
            raise RuntimeError("Prepared Discord message exceeds 2,000 characters.")

        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={
                "content": content,
                "allowed_mentions": {
                    "parse": [],
                    "roles": [DISCORD_ROLE_ID] if index == 1 else [],
                },
            },
            timeout=30,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Discord webhook error {response.status_code}: "
                f"{response.text[:500]}"
            )
        time.sleep(1)


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_urls", []))
    except Exception:
        return set()


def save_state(seen_urls):
    STATE_FILE.write_text(
        json.dumps(
            {"seen_urls": sorted(seen_urls)},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def main():
    articles = get_patch_list()
    seen = load_state()

    if seen is None:
        seen = set()

    if SEND_LATEST:
        new_articles = [articles[0]]
        print("SEND_LATEST mode is enabled: processing newest matching article.")
    elif not seen:
        save_state({item["url"] for item in articles})
        print(
            f"Initialized state with {len(articles)} matching existing posts. "
            "No messages sent."
        )
        return
    else:
        new_articles = [
            item for item in articles
            if item["url"] not in seen
        ]
        new_articles = list(reversed(new_articles))[:MAX_ARTICLES_PER_RUN]

    if not new_articles:
        print("No new advance notices or main patch notes.")
        return

    for item in new_articles:
        print(f"Processing {item['type']}: {item['title']} | {item['url']}")
        article_text = get_article_text(item["url"])
        translated_text = translate_english(
            item["title"], item["type"], article_text
        )
        post_to_discord(
            item["title"], item["url"], translated_text, item["type"]
        )
        seen.add(item["url"])
        save_state(seen)
        time.sleep(2)

    print(f"Posted {len(new_articles)} new advance notice/main patch note(s).")


if __name__ == "__main__":
    main()
