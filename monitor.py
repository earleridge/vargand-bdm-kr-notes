import os
import re
import json
import time
import html
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

FORUM_URL = "https://forum.blackdesertm.com/Board?boardNo=7"
STATE_FILE = Path("state.json")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SEND_LATEST = os.getenv("SEND_LATEST_ON_FIRST_RUN", "false").lower() == "true"
MAX_ARTICLES_PER_RUN = int(os.getenv("MAX_ARTICLES_PER_RUN", "3"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VargandBDMNoticeBot/1.0; +https://github.com)"
}

def fetch_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")

def clean_text(node):
    for tag in node.select("script, style, nav, header, footer, iframe, noscript, button, form"):
        tag.decompose()
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

def get_patch_list():
    soup = fetch_soup(FORUM_URL)
    found = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "Board/Detail" not in href or "contentNo=" not in href:
            continue
        full_url = urljoin(FORUM_URL, href)
        if "boardNo=7" not in full_url:
            continue
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not title or full_url in seen_urls:
            continue
        # Filter obvious navigation labels rather than actual patch-note titles.
        if title in {"패치노트", "목록", "자세히 보기"}:
            continue
        seen_urls.add(full_url)
        found.append({"title": title, "url": full_url})

    if not found:
        raise RuntimeError("Could not find patch-note links on the official KR forum. The site layout may have changed.")
    # The official list is normally newest-first; preserve page order.
    return found[:20]

def get_article_text(url):
    soup = fetch_soup(url)
    # Prefer article/content containers; fall back to body if the site's markup changes.
    selectors = [
        "article",
        '[class*="board-view"]',
        '[class*="boardView"]',
        '[class*="board-detail"]',
        '[class*="detail-content"]',
        '[class*="view-content"]',
        '[class*="content_view"]',
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

    if len(best_text) < 800 and soup.body:
        best_text = clean_text(soup.body)

    # Avoid sending an excessive amount of page boilerplate to the summarizer.
    return best_text[:30000]

def summarize_english(title, article_text, url):
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = f"""
You are preparing a concise English notice for a Black Desert Mobile guild Discord.
Translate and summarize the Korean official patch notes below for players.
Rules:
- Use English.
- Prioritize class balance, new/changed content, rewards/events, maintenance-related player actions, and important system changes.
- Preserve class/item/content names and exact numbers when clear.
- Do not invent details. If a detail is unclear, omit it.
- Keep the summary under 1,200 characters.
- Use 4-8 short bullet points, with a brief heading.
- Do not include the source URL in the summary; it will be added separately.
Korean title: {title}
Official article URL: {url}
Article text:
{article_text}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 700
        }
    }
    response = requests.post(endpoint, json=payload, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini returned no summary text.")

def post_to_discord(title, url, summary):
    content = (
        "🇰🇷 **VARGAND | BDM KOREA PATCH NOTES**\n"
        f"**{title[:180]}**\n\n"
        f"{summary}\n\n"
        f"🔗 **Original Korean notes:** {url}\n"
        "_English summary generated automatically; check the original for full details._"
    )
    # Discord message content limit is 2,000 characters.
    if len(content) > 1950:
        content = content[:1900].rsplit("\n", 1)[0] + "\n\n🔗 Original Korean notes: " + url
    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": content, "allowed_mentions": {"parse": []}},
        timeout=30
    )
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook error {response.status_code}: {response.text[:500]}")

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
        json.dumps({"seen_urls": sorted(seen_urls)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

def main():
    articles = get_patch_list()
    seen = load_state()

    if not seen:
        if SEND_LATEST:
            # Explicit test run: process the newest article on first run.
            new_articles = [articles[0]]
            # Mark the rest of the current page as old so the next run doesn't backfill history.
            seen = {item["url"] for item in articles[1:]}
        else:
            # Safe initial setup: mark current articles as seen to avoid flooding the channel.
            save_state({item["url"] for item in articles})
            print(f"Initialized state with {len(articles)} existing posts. No messages sent.")
            return
    else:
        new_articles = [item for item in articles if item["url"] not in seen]
        new_articles = list(reversed(new_articles))[:MAX_ARTICLES_PER_RUN]

    if not new_articles:
        print("No new Korean patch notes.")
        return

    for item in new_articles:
        print(f"Processing: {item['title']} | {item['url']}")
        article_text = get_article_text(item["url"])
        if len(article_text) < 300:
            raise RuntimeError("Article text extraction returned too little text; stopping to avoid sending a bad summary.")
        summary = summarize_english(item["title"], article_text, item["url"])
        post_to_discord(item["title"], item["url"], summary)
        seen.add(item["url"])
        time.sleep(2)

    # Record all currently visible links to prevent reposting older posts on later runs.
    seen.update(item["url"] for item in articles)
    save_state(seen)
    print(f"Posted {len(new_articles)} new Korean patch note(s).")

if __name__ == "__main__":
    main()
