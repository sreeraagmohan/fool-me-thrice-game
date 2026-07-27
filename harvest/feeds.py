"""RSS sources for Fool Me Thrice.

Two families:
  * fact-checkers (Alt News, Factly)  -> FAKE candidates
  * mainstream news (Hindu, IE, HT, NDTV, TOI) -> REAL candidates

Both support deep paging so we can reach past the current news cycle, which
matters: the front page of any Indian fact-checker is dominated by whatever
political story is running this week, and we deliberately do not want that.

Everything here is read-only RSS. Structural surprises raise FeedError so the
harvest fails loudly rather than writing garbage.
"""

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 FoolMeThriceHarvester/1.0"
)

FACTCHECK_FEEDS = {
    "altnews": "https://www.altnews.in/feed/",
    "factly": "https://factly.in/feed/",
}

# National/regional desks carry a lot of municipal filler; business, science
# and technology desks carry far more share-worthy, non-partisan material.
NEWS_FEEDS = {
    "thehindu": "https://www.thehindu.com/news/national/feeder/default.rss",
    "thehindu-sci": "https://www.thehindu.com/sci-tech/feeder/default.rss",
    "thehindu-biz": "https://www.thehindu.com/business/feeder/default.rss",
    "indianexpress": "https://indianexpress.com/section/india/feed/",
    "indianexpress-biz": "https://indianexpress.com/section/business/feed/",
    "indianexpress-tech": "https://indianexpress.com/section/technology/feed/",
    "hindustantimes": "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml",
    "ndtv": "https://feeds.feedburner.com/ndtvnews-india-news",
    "toi": "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms",
}

# Live-political / communal content is excluded by policy: the game must not
# adjudicate contested political events, and such claims are unjudgeable
# anyway. This is a cheap prefilter; the model applies the real judgement.
POLITICAL_MARKERS = (
    "cjp", "protest", "protester", "protestor", "jantar", "lathicharge",
    "pellet", "police firing", "police action", "communal", "riot",
    "muslim", "hindu", "hindutva", "bjp", "congress", "aap ", "rss ",
    "rahul gandhi", "modi", "amit shah", "election", "vote bank",
    "arrest", "sedition", "fir ", "detained", "custody", "encounter",
    "temple", "mosque", "church", "namaz", "madrasa", "conversion",
    "caste", "dalit", "reservation quota", "minister claim",
)

# Distasteful as game content regardless of politics: sexual violence, named
# private victims, self-harm. A quiz card is the wrong frame for these.
SENSITIVE_MARKERS = (
    "rape", "gangrape", "sexual", "molest", "grope", "harass",
    "suicide", "self-harm", "abuse", "assault", "murder", "lynch",
    "minor girl", "child abuse", "trafficking",
)

# Non-Latin scripts we cannot use (English-only game).
NON_LATIN = re.compile(r"[ऀ-ॿఀ-౿ঀ-৿஀-௿઀-૿]")


class FeedError(Exception):
    """Feed missing or structurally unrecognisable."""


def _strip(html: str) -> str:
    """RSS descriptions carry HTML; flatten to plain text."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&#8216;", "'").replace("&#8217;", "'")
                .replace("&#8220;", '"').replace("&#8221;", '"')
                .replace("&quot;", '"').replace("&#039;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def _slug(link: str) -> str:
    """Stable id from a URL path — survives title edits."""
    path = re.sub(r"^https?://[^/]+/", "", link or "").strip("/")
    path = re.sub(r"\?.*$", "", path)
    return re.sub(r"[^a-zA-Z0-9]+", "-", path)[-80:].strip("-").lower()


def iso_date(raw: str):
    """RSS dates are RFC-822; the cards table wants a plain ISO date."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        return m.group(1) if m else None


def is_english(text: str) -> bool:
    return not NON_LATIN.search(text or "")


def looks_political(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in POLITICAL_MARKERS)


def looks_sensitive(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in SENSITIVE_MARKERS)


# A passing mention deep in a summary should not disqualify an item — the card
# is built from the headline. Only the most charged markers are checked against
# the summary as well.
SEVERE_MARKERS = (
    "communal", "riot", "lathicharge", "pellet", "sedition", "lynch",
    "namaz", "madrasa", "hindutva", "rape", "gangrape", "molest", "suicide",
)


def is_usable(item: dict) -> bool:
    """Cheap prefilter. The model applies the real editorial judgement."""
    title = item["title"]
    summary = item.get("summary", "")
    if not is_english(title):
        return False
    if looks_political(title) or looks_sensitive(title):
        return False
    low_summary = summary.lower()
    return not any(m in low_summary for m in SEVERE_MARKERS)


def _parse_items(content: bytes, source: str) -> list:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise FeedError(f"{source}: feed is not well-formed XML ({e})")
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    return items


def _item_fields(item) -> dict:
    def text_of(*tags):
        for tag in tags:
            el = item.find(tag)
            if el is not None and el.text:
                return _strip(el.text)
        return ""

    link = text_of("link", "{http://www.w3.org/2005/Atom}id")
    if not link:
        el = item.find("{http://www.w3.org/2005/Atom}link")
        if el is not None:
            link = el.get("href", "")
    return {
        "title": text_of("title", "{http://www.w3.org/2005/Atom}title"),
        "summary": text_of("description", "{http://www.w3.org/2005/Atom}summary"),
        "link": link,
        "date": text_of("pubDate", "{http://www.w3.org/2005/Atom}published"),
    }


def fetch_feed(name: str, url: str, pages: int = 1) -> list:
    """Fetch a feed, optionally paging back through its archive."""
    out, seen = [], set()
    for page in range(1, pages + 1):
        page_url = url if page == 1 else f"{url}{'&' if '?' in url else '?'}paged={page}"
        try:
            resp = requests.get(page_url, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            raise FeedError(f"{name}: request failed ({type(e).__name__})")
        if resp.status_code == 404 and page > 1:
            break  # ran off the end of the archive
        if resp.status_code != 200:
            if page == 1:
                raise FeedError(f"{name}: HTTP {resp.status_code}")
            break
        items = _parse_items(resp.content, name)
        if not items:
            break
        fresh = 0
        for item in items:
            f = _item_fields(item)
            if not f["title"] or not f["link"]:
                continue
            sid = _slug(f["link"])
            if sid in seen:
                continue
            seen.add(sid)
            fresh += 1
            out.append({**f, "source": name, "slug": sid})
        if fresh == 0:
            break  # archive is repeating; stop
        time.sleep(0.3)
    return out


def fetch_factcheck_candidates(pages: int = 6) -> list:
    """FAKE candidates from independent fact-checkers.

    Returns items shaped for normalization: the title/summary describe what
    was debunked; the prompt extracts the underlying false proposition.
    """
    out = []
    for name, url in FACTCHECK_FEEDS.items():
        items = fetch_feed(name, url, pages=pages)
        kept = [i for i in items if is_usable(i)]
        out.extend(
            {
                "id": f"fc:{name}:{i['slug']}",
                "text": f"HEADLINE: {i['title']}\nSUMMARY: {i['summary'][:900]}",
                "date": iso_date(i["date"]),
                "url": i["link"],
                "source_type": f"factcheck:{name}",
            }
            for i in kept
        )
        print(f"  {name}: {len(items)} fetched -> {len(kept)} usable "
              f"(dropped {len(items) - len(kept)} non-English/political)", flush=True)
    if not out:
        raise FeedError("no usable fact-check items from any feed")
    return out


def fetch_news_candidates(pages: int = 3) -> list:
    """REAL candidates from mainstream Indian news."""
    out = []
    for name, url in NEWS_FEEDS.items():
        try:
            items = fetch_feed(name, url, pages=pages)
        except FeedError as e:
            print(f"  {name}: SKIPPED ({e})", flush=True)
            continue
        kept = [i for i in items if is_usable(i)]
        out.extend(
            {
                "id": f"news:{name}:{i['slug']}",
                "text": f"HEADLINE: {i['title']}\nSUMMARY: {i['summary'][:900]}",
                "date": iso_date(i["date"]),
                "url": i["link"],
                "source_type": f"news:{name}",
            }
            for i in kept
        )
        print(f"  {name}: {len(items)} fetched -> {len(kept)} usable "
              f"(dropped {len(items) - len(kept)} non-English/political)", flush=True)
    if not out:
        raise FeedError("no usable news items from any feed")
    return out
