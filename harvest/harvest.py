#!/usr/bin/env python3
"""Fool Me Thrice — harvest script.

Pulls FAKE claims from @PIBFactCheck (X syndication timeline + PIB's
Operation Sindoor tweet index resolved via X oEmbed) and REAL claims from
PIB press releases (allRel.aspx), normalizes both pools into game cards
with the Claude API, and upserts them into Supabase keyed on source_id.

Design rules:
  - Idempotent: items already in Supabase are never re-fetched from Claude
    and upserts conflict on source_id.
  - Loud failure: any structural surprise in a PIB/X response (missing
    markers, suspiciously low counts, schema violations) exits non-zero
    with a clear message. Nothing is silently skipped wholesale.
"""

import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 FoolMeThriceHarvester/1.0"
)

SYNDICATION_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/PIBFactCheck"
)
SINDOOR_URL = "https://www.pib.gov.in/factcheckupdates.aspx?reg=3&lang=1"
ALLREL_URL = "https://www.pib.gov.in/allRel.aspx?reg=3&lang=1"
RELEASE_URL = "https://www.pib.gov.in/PressReleasePage.aspx?PRID={prid}"
OEMBED_URL = "https://publish.twitter.com/oembed"

CATEGORIES = ["defence", "health", "finance", "schemes", "jobs-education", "tech", "misc"]

CLAUDE_MODEL = "claude-haiku-4-5"
NORMALIZE_BATCH_SIZE = 10
MAX_NEW_RELEASES_PER_RUN = int(os.environ.get("MAX_NEW_RELEASES_PER_RUN", "60"))
MAX_NEW_TWEETS_PER_RUN = int(os.environ.get("MAX_NEW_TWEETS_PER_RUN", "120"))

# Sanity floors: if a source yields less than this, its page structure has
# almost certainly changed and we must not write anything derived from it.
MIN_SYNDICATION_ENTRIES = 20
MIN_SINDOOR_TWEETS = 10
MIN_ALLREL_LINKS = 10


class HarvestError(Exception):
    """Structural failure — the run must stop and turn the cron red."""


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------- env / .env

def load_env() -> dict:
    """Read config from the environment, topping up from a local .env."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

    cfg = {
        "supabase_url": os.environ.get("SUPABASE_URL", "").rstrip("/"),
        "supabase_key": os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise HarvestError(f"missing required environment values: {', '.join(missing)}")
    return cfg


# ------------------------------------------------------------ http helpers

def fetch(url: str, timeout: int = 30, **kwargs) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def tweet_date(tweet_id: int) -> str:
    """Snowflake ID -> ISO date."""
    ms = (tweet_id >> 22) + 1288834974657
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


# ------------------------------------------------------- source: FAKE pool

def fetch_syndication_tweets() -> dict:
    """Primary FAKE source: X's syndication timeline for @PIBFactCheck.

    Returns {tweet_id_str: {text, lang, date}}.
    """
    resp = fetch(SYNDICATION_URL, timeout=45)
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
        re.S,
    )
    if not m:
        raise HarvestError(
            "syndication timeline: __NEXT_DATA__ marker not found — "
            "X may have changed or gated this endpoint"
        )
    try:
        data = json.loads(m.group(1))
        entries = data["props"]["pageProps"]["timeline"]["entries"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise HarvestError(f"syndication timeline: unexpected JSON shape ({e})")

    if len(entries) < MIN_SYNDICATION_ENTRIES:
        raise HarvestError(
            f"syndication timeline: only {len(entries)} entries "
            f"(expected >= {MIN_SYNDICATION_ENTRIES}) — refusing to continue"
        )

    tweets = {}
    for entry in entries:
        t = entry.get("content", {}).get("tweet", {})
        tid, text = t.get("id_str"), t.get("full_text", "")
        if not tid or not text or text.startswith("RT @"):
            continue
        tweets[tid] = {
            "text": html_lib.unescape(text),
            "lang": t.get("lang", "und"),
            "date": tweet_date(int(tid)),
        }
    log(f"syndication: {len(tweets)} tweets ({len(entries)} raw entries)")
    return tweets


def fetch_sindoor_tweet_ids() -> list:
    """Secondary FAKE source: tweet URLs embedded in PIB's factcheckupdates page."""
    resp = fetch(SINDOOR_URL)
    m = re.search(r"tweetData\s*=\s*(\[.*?\])\s*;", resp.text, re.S)
    if not m:
        raise HarvestError(
            "factcheckupdates.aspx: tweetData array not found — PIB changed the page"
        )
    try:
        items = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise HarvestError(f"factcheckupdates.aspx: tweetData is not valid JSON ({e})")

    ids = []
    for item in items:
        m2 = re.search(r"/status/(\d+)", item.get("url", ""))
        if m2:
            ids.append(m2.group(1))
    ids = list(dict.fromkeys(ids))  # dedupe, preserve order
    if len(ids) < MIN_SINDOOR_TWEETS:
        raise HarvestError(
            f"factcheckupdates.aspx: only {len(ids)} tweet ids parsed "
            f"(expected >= {MIN_SINDOOR_TWEETS})"
        )
    log(f"sindoor page: {len(ids)} tweet ids")
    return ids


def fetch_tweet_via_oembed(tweet_id: str) -> dict | None:
    """Resolve one tweet through X's free oEmbed endpoint. None on failure."""
    url = f"https://twitter.com/PIBFactCheck/status/{tweet_id}"
    try:
        resp = fetch(OEMBED_URL, timeout=20, params={"url": url, "omit_script": "true"})
        payload = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None
    html = payload.get("html", "")
    m = re.search(r'<p lang="([^"]*)"[^>]*>(.*?)</p>', html, re.S)
    if not m:
        return None
    lang = m.group(1)
    text = re.sub(r"<br\s*/?>", "\n", m.group(2))
    text = re.sub(r"<[^>]+>", "", text)
    return {"text": html_lib.unescape(text), "lang": lang, "date": tweet_date(int(tweet_id))}


# ------------------------------------------------------- source: REAL pool

def _parse_release_links(page_html: str) -> list:
    links = re.findall(
        r'href=["\']?/?PressReleasePage\.aspx\?PRID=(\d+)["\']?[^>]*>\s*([^<]{10,})',
        page_html,
    )
    return [(prid, html_lib.unescape(title.strip())) for prid, title in links]


def fetch_release_list() -> list:
    """English press releases: [(prid, title)].

    Covers today (plain GET) plus BACKFILL_DAYS previous days, plus
    HISTORICAL_DAYS randomly sampled days from 2020 onward — historical
    sampling keeps the REAL pool's date distribution overlapping the FAKE
    pool's, so recency never becomes a tell. Past days are reached through
    the page's ASP.NET date-filter postback.
    """
    import random
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    backfill_days = int(os.environ.get("BACKFILL_DAYS", "1"))
    historical_days = int(os.environ.get("HISTORICAL_DAYS", "2"))

    session = requests.Session()
    session.headers["User-Agent"] = UA
    resp = session.get(ALLREL_URL, timeout=30)
    resp.raise_for_status()
    releases = _parse_release_links(resp.text)

    hidden = dict(
        re.findall(
            r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"',
            resp.text,
        )
    )
    if "__VIEWSTATE" not in hidden:
        raise HarvestError("allRel.aspx: __VIEWSTATE not found — page structure changed")

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    dates = [today - timedelta(days=n) for n in range(1, backfill_days + 1)]
    hist_start = datetime(2020, 1, 1).date()
    hist_span = (today - timedelta(days=90) - hist_start).days
    dates += [
        hist_start + timedelta(days=random.randrange(hist_span))
        for _ in range(historical_days)
    ]

    for d in dates:
        post = session.post(
            ALLREL_URL,
            data={
                **hidden,
                "__EVENTTARGET": "ctl00$ContentPlaceHolder1$ddlday",
                "ctl00$ContentPlaceHolder1$ddlMinistry": "0",
                "ctl00$ContentPlaceHolder1$ddlday": str(d.day),
                "ctl00$ContentPlaceHolder1$ddlMonth": str(d.month),
                "ctl00$ContentPlaceHolder1$ddlYear": str(d.year),
            },
            timeout=45,
        )
        post.raise_for_status()
        day_links = _parse_release_links(post.text)
        log(f"allRel {d.isoformat()}: {len(day_links)} releases")
        releases += day_links
        time.sleep(0.3)

    seen, out = set(), []
    for prid, title in releases:
        if prid not in seen:
            seen.add(prid)
            out.append((prid, title))
    if len(out) < MIN_ALLREL_LINKS:
        raise HarvestError(
            f"allRel.aspx: only {len(out)} release links across {len(dates) + 1} days "
            f"(expected >= {MIN_ALLREL_LINKS}) — page structure may have changed"
        )
    # Shuffle so the per-run cap samples uniformly across today + historical
    # days, rather than exhausting today's (larger) list first — this is what
    # keeps the REAL pool's date distribution spread out.
    random.shuffle(out)
    log(f"allRel: {len(out)} unique releases listed")
    return out


def fetch_release_body(prid: str) -> dict | None:
    """Fetch one press release page; return {body, date} or None if unusable."""
    try:
        resp = fetch(RELEASE_URL.format(prid=prid))
    except requests.RequestException:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")

    container = soup.find("div", class_=re.compile("innner-page-main-about-us-content"))
    if container is None:
        # Fallback: densest text div. If even that is empty the page changed.
        divs = soup.find_all("div")
        container = max(divs, key=lambda d: len(d.get_text()), default=None)
    if container is None:
        return None
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    if len(text) < 200:
        return None

    date_iso = None
    m = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", text)
    if m:
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        mon = months.get(m.group(2))
        if mon:
            date_iso = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return {"body": text[:1800], "date": date_iso}


# ----------------------------------------------------------- supabase I/O

def supabase_headers(cfg: dict) -> dict:
    return {
        "apikey": cfg["supabase_key"],
        "Authorization": f"Bearer {cfg['supabase_key']}",
        "Content-Type": "application/json",
    }


def fetch_existing_source_ids(cfg: dict) -> set:
    ids, offset, page = set(), 0, 1000
    while True:
        resp = requests.get(
            f"{cfg['supabase_url']}/rest/v1/cards",
            headers={**supabase_headers(cfg), "Range": f"{offset}-{offset + page - 1}"},
            params={"select": "source_id"},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
        ids.update(r["source_id"] for r in rows)
        if len(rows) < page:
            return ids
        offset += page


def upsert_cards(cfg: dict, cards: list) -> None:
    if not cards:
        return
    resp = requests.post(
        f"{cfg['supabase_url']}/rest/v1/cards",
        headers={
            **supabase_headers(cfg),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "source_id"},
        data=json.dumps(cards),
        timeout=60,
    )
    if resp.status_code not in (200, 201, 204):
        raise HarvestError(f"supabase upsert failed: HTTP {resp.status_code} {resp.text[:300]}")


# ------------------------------------------------------ claude normalization

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "usable": {"type": "boolean"},
                    "share_score": {"type": "integer"},
                    "claim": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "explanation": {"type": "string"},
                },
                "required": ["id", "usable", "share_score", "claim",
                             "category", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}

JUDGEABILITY = """A card is only good if a thoughtful player has something to REASON about. The claim must be fully self-contained:
- Specific: name the concrete thing alleged or announced. Never vague references like "accusations against X" or "a confidential letter" whose contents the player cannot know.
- Anchored: if the claim is tied to an event or period, anchor it in the claim itself using the DATE provided (e.g. "during the May 2025 India-Pakistan clashes", "at the height of the 2020 lockdown"). A floating "an army post was destroyed" is unjudgeable; anchored, it becomes reasoning material.
- No insider knowledge required: mark unusable anything a player could only judge by having seen a specific document, image, or account (morphed screenshots with no specific claim, confidential letters, fake handles of obscure officials)."""

FAKE_SYSTEM = f"""You turn debunked misinformation into cards for a real-vs-fake news game.

Each input is a tweet by PIB Fact Check (Indian government) debunking a claim that circulated on social media, with the DATE it circulated. For each item produce:
- claim: the FALSE claim itself, restated as a confident assertion in under 200 characters — the way it circulated on WhatsApp/social media, in casual, informal English. Never include words like "fake", "fact check", "debunked", "claim", or hashtags. No emoji. Write it so a player cannot tell from style alone whether it is true.
- share_score: honest 1-5 rating of how viral/share-worthy the claim reads (analytics only).
- category: one of the allowed values.
- explanation: ONE sentence a player sees after answering, explaining why the claim is false and what PIB Fact Check found. Start with something other than "The claim".
- usable: judged by the standard below. Fraudulent-website and job-scam alerts ARE usable when phrased as the false promise they made.

{JUDGEABILITY}

Return every input item exactly once, using its id."""

REAL_SYSTEM = f"""You turn official Indian government press releases into cards for a real-vs-fake news game.

Players see a claim and guess REAL or FAKE. The FAKE pool is sensational viral misinformation, so a REAL card must hold its own as shareable news — the kind of fact a person would actually forward to a family WhatsApp group.

For each input item (a genuine PIB press release with its DATE):
- Find the single most share-worthy fact in the release: surprising, consequential, or delightfully specific. Ask: would a normal person retell this to a friend? Most press releases contain no such fact — that is expected.
- share_score: honest 1-5 rating (5 = would genuinely go viral; 3 = mildly interesting; 1 = only a bureaucrat cares). Most releases deserve 1-2.
- claim: the fact as a confident, informal assertion under 200 characters, phrased the way a person would retell it — never press-release officialese. Frame ANNOUNCEMENTS and decisions, not tallied statistics ("NHAI will pay you for reporting dirty highway toilets", not "NHAI rewarded 429 commuters with Rs 1000"). AT MOST one number, rounded the way people speak ("Rs 1 lakh crore", "nearly 30 years"). Never stack statistics. No honorifics, no "today". Must stay factually accurate to the release; for older releases, phrase it so it reads correctly with its DATE (anchor the year when it matters).
- category: one of the allowed values.
- explanation: ONE sentence shown after answering, confirming the fact and citing the PIB press release as the source.
- usable: false if ceremonial, routine administrative business, share_score of 3 or less, or it fails the standard below.

{JUDGEABILITY}

Return every input item exactly once, using its id."""


def normalize_with_claude(client, system: str, items: list, min_share: int = 0) -> tuple:
    """items: [{id, text, date}] -> (validated card dicts, unusable real ids).

    Claude only ever sees short synthetic ids (batch positions) so it cannot
    mangle 19-digit tweet ids; we map back to real ids afterwards. Items it
    declares unusable — or that score below min_share — are returned
    separately so callers can tombstone them and never pay for them again.
    Items dropped by OUR validation are in neither list and will be retried
    on a future run.
    """
    cards_out, unusable = [], []
    for i in range(0, len(items), NORMALIZE_BATCH_SIZE):
        batch = items[i : i + NORMALIZE_BATCH_SIZE]
        idmap = {str(n): it["id"] for n, it in enumerate(batch)}
        payload = json.dumps(
            [
                {
                    "id": str(n),
                    "text": f"DATE: {it.get('date') or 'unknown'}\n{it['text']}",
                }
                for n, it in enumerate(batch)
            ],
            ensure_ascii=False,
        )
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": CARD_SCHEMA}},
            messages=[{"role": "user", "content": payload}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        try:
            cards = json.loads(text)["cards"]
        except (json.JSONDecodeError, KeyError) as e:
            raise HarvestError(f"claude returned unparseable output ({e}): {text[:200]}")

        for card in cards:
            real_id = idmap.get(card["id"])
            if real_id is None:
                warn(f"claude returned unknown batch id {card['id']!r}; dropping")
                continue
            # share_score is an analytics/gating hint, not a quality signal —
            # an out-of-range value coerces to 0 (fails any share gate) rather
            # than dropping the item into permanent retry limbo.
            score = card.get("share_score")
            score = score if isinstance(score, int) and 1 <= score <= 5 else 0
            if not card["usable"] or score < min_share:
                unusable.append(real_id)
                continue
            claim = card["claim"].strip()
            expl = card["explanation"].strip()
            if not (10 <= len(claim) <= 200):
                warn(f"{real_id}: claim length {len(claim)} out of range; dropping")
                continue
            if not expl or len(expl) > 300:
                warn(f"{real_id}: bad explanation; dropping")
                continue
            cards_out.append({"id": real_id, "claim": claim,
                              "category": card["category"], "explanation": expl})
        time.sleep(0.5)
    return cards_out, unusable


# ------------------------------------------------------------------- main

def main() -> None:
    dry_run = "--dry-run" in sys.argv
    refresh = "--refresh" in sys.argv  # re-normalize & overwrite existing rows
    cfg = load_env()

    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_key"])

    if refresh:
        existing = set()
        log("REFRESH mode: re-normalizing everything and overwriting in place")
    else:
        existing = fetch_existing_source_ids(cfg)
        log(f"supabase: {len(existing)} existing cards")

    # ---- FAKE pool ------------------------------------------------------
    tweets = fetch_syndication_tweets()

    sindoor_ids = fetch_sindoor_tweet_ids()
    oembed_needed = [
        tid for tid in sindoor_ids
        if tid not in tweets and f"tweet:{tid}" not in existing
    ]
    oembed_fail = 0
    for tid in oembed_needed:
        info = fetch_tweet_via_oembed(tid)
        if info is None:
            oembed_fail += 1
        else:
            tweets[tid] = info
        time.sleep(0.4)
    if oembed_needed:
        log(f"oembed: resolved {len(oembed_needed) - oembed_fail}/{len(oembed_needed)}")
        if oembed_fail > len(oembed_needed) * 0.8 and len(oembed_needed) > 5:
            warn("most oEmbed lookups failed — X may be rate-limiting or the endpoint changed")

    new_tweets = [
        {"id": tid, "text": info["text"], "date": info["date"]}
        for tid, info in tweets.items()
        if info["lang"] == "en" and f"tweet:{tid}" not in existing
    ][:MAX_NEW_TWEETS_PER_RUN]
    log(f"fake pool: {len(new_tweets)} new English tweets to normalize")

    # ---- REAL pool ------------------------------------------------------
    releases = fetch_release_list()
    new_releases = []
    for prid, title in releases:
        if f"pib:{prid}" in existing:
            continue
        if len(new_releases) >= MAX_NEW_RELEASES_PER_RUN:
            break
        detail = fetch_release_body(prid)
        if detail is None:
            warn(f"release {prid}: no usable body; skipping")
            continue
        new_releases.append({
            "id": prid,
            "text": f"TITLE: {title}\nBODY: {detail['body']}",
            "date": detail["date"],
        })
        time.sleep(0.3)
    log(f"real pool: {len(new_releases)} new releases to normalize")

    if not new_tweets and not new_releases:
        log("nothing new to do; exiting cleanly")
        return

    # ---- normalize ------------------------------------------------------
    # Fakes are kept regardless of virality (they are real misinformation);
    # reals must clear a share-worthiness bar so they can stand next to them.
    fake_cards, fake_skip = (normalize_with_claude(client, FAKE_SYSTEM, new_tweets)
                             if new_tweets else ([], []))
    real_cards, real_skip = (normalize_with_claude(client, REAL_SYSTEM, new_releases,
                                                    min_share=4)
                             if new_releases else ([], []))

    for pool, inputs, cards, skips in (("fake", new_tweets, fake_cards, fake_skip),
                                       ("real", new_releases, real_cards, real_skip)):
        if inputs and (len(cards) + len(skips)) < len(inputs) * 0.5:
            raise HarvestError(
                f"{pool} pool: only {len(cards) + len(skips)}/{len(inputs)} items "
                "processed cleanly — prompt or source drift; refusing to write"
            )

    def row(card, verdict, url, date, tombstone=False):
        return {
            "source_id": None,  # filled in by the caller
            "verdict": verdict,
            "claim": card["claim"] if not tombstone else "[skipped during normalization]",
            "category": card["category"] if not tombstone else "misc",
            "explanation": card["explanation"] if not tombstone
            else "Source item judged unusable for the game.",
            "source_url": url,
            "source_date": date,
            "active": not tombstone,
        }

    dates = {it["id"]: it["date"] for it in new_tweets + new_releases}
    rows = []
    for c in fake_cards:
        r = row(c, "FAKE", f"https://x.com/PIBFactCheck/status/{c['id']}", dates.get(c["id"]))
        r["source_id"] = f"tweet:{c['id']}"
        rows.append(r)
    for c in real_cards:
        r = row(c, "REAL", f"https://www.pib.gov.in/PressReleasePage.aspx?PRID={c['id']}",
                dates.get(c["id"]))
        r["source_id"] = f"pib:{c['id']}"
        rows.append(r)
    # tombstones: recorded inactive so they are never re-normalized
    for sid in fake_skip:
        r = row(None, "FAKE", f"https://x.com/PIBFactCheck/status/{sid}",
                dates.get(sid), tombstone=True)
        r["source_id"] = f"tweet:{sid}"
        rows.append(r)
    for sid in real_skip:
        r = row(None, "REAL", f"https://www.pib.gov.in/PressReleasePage.aspx?PRID={sid}",
                dates.get(sid), tombstone=True)
        r["source_id"] = f"pib:{sid}"
        rows.append(r)

    if dry_run:
        log(f"DRY RUN: would upsert {len(rows)} rows "
            f"({len(fake_cards)} fake, {len(real_cards)} real, "
            f"{len(fake_skip) + len(real_skip)} tombstones)")
        for label, cards, verdict, url_key in (
            ("FAKE", fake_cards, "FAKE", "tweet"),
            ("REAL", real_cards, "REAL", "pib"),
        ):
            log(f"\n=== {label} samples ({len(cards)}) ===")
            for c in cards:
                d = dates.get(c["id"]) or "????"
                log(f"  [{d[:7]} {c['category']}] {c['claim']}")
        return

    upsert_cards(cfg, rows)
    log(
        f"DONE: upserted {len(fake_cards)} fake + {len(real_cards)} real cards, "
        f"tombstoned {len(fake_skip) + len(real_skip)} unusable items; "
        f"{len(new_tweets) + len(new_releases) - len(rows)} left for retry"
    )


if __name__ == "__main__":
    try:
        main()
    except HarvestError as e:
        print(f"HARVEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
