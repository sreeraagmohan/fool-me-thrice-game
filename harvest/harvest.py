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

import collections

import requests
from bs4 import BeautifulSoup

import feeds

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
MAX_NEW_FACTCHECK_PER_RUN = int(os.environ.get("MAX_NEW_FACTCHECK_PER_RUN", "80"))
MAX_NEW_NEWS_PER_RUN = int(os.environ.get("MAX_NEW_NEWS_PER_RUN", "200"))
FACTCHECK_PAGES = int(os.environ.get("FACTCHECK_PAGES", "6"))
NEWS_PAGES = int(os.environ.get("NEWS_PAGES", "3"))

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


NEW_COLUMNS = ("valence", "source_type")


def upsert_cards(cfg: dict, cards: list) -> None:
    """Upsert, degrading gracefully if the valence/source_type migration has
    not been applied yet — better to keep the deck fresh than to hard-fail on
    two analytics columns."""
    if not cards:
        return
    resp = _post_cards(cfg, cards)
    if resp.status_code == 400 and any(c in resp.text for c in NEW_COLUMNS):
        warn("valence/source_type columns missing — writing without them. "
             "Apply the migration to enable balance tracking.")
        stripped = [{k: v for k, v in c.items() if k not in NEW_COLUMNS} for c in cards]
        resp = _post_cards(cfg, stripped)
    if resp.status_code not in (200, 201, 204):
        raise HarvestError(f"supabase upsert failed: HTTP {resp.status_code} {resp.text[:300]}")


def _post_cards(cfg: dict, cards: list):
    return requests.post(
        f"{cfg['supabase_url']}/rest/v1/cards",
        headers={
            **supabase_headers(cfg),
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "source_id"},
        data=json.dumps(cards),
        timeout=60,
    )


def deactivate_all_cards(cfg: dict) -> None:
    """Set every card inactive. Used only in --refresh so that cards NOT
    re-processed this run (old-prompt leftovers) are retired instead of
    lingering in a mixed deck; the refresh's upsert immediately switches the
    freshly-regenerated good cards back to active."""
    resp = requests.patch(
        f"{cfg['supabase_url']}/rest/v1/cards",
        headers={**supabase_headers(cfg), "Prefer": "return=minimal"},
        params={"active": "eq.true"},
        data=json.dumps({"active": False}),
        timeout=60,
    )
    if resp.status_code not in (200, 204):
        raise HarvestError(f"deactivate failed: HTTP {resp.status_code} {resp.text[:300]}")


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
                    "valence": {"type": "string",
                                "enum": ["positive", "negative", "neutral"]},
                    "explanation": {"type": "string"},
                },
                "required": ["id", "usable", "share_score", "claim",
                             "category", "valence", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}

SCOPE = """SCOPE — keep the game out of live political disputes. Apply this precisely; do NOT over-apply it.

IN SCOPE — use these freely, they are the substance of the game:
government schemes, policy, budgets, regulations and rules; health and medicine; science, space and technology; business and the economy; infrastructure and transport; agriculture; education; sport; wildlife and the environment; consumer scams and frauds; records, firsts and milestones; disasters and accidents whose facts are established.
Mentioning a ministry, a minister, a government programme or an official body does NOT make an item political. Ordinary policy and administration news is IN SCOPE.

OUT OF SCOPE — mark unusable:
- Party politics, elections, campaign claims, and who-said-what political rows.
- Communal or religious conflict, or anything that frames a religious community.
- Protests, police conduct, and contested accounts of what happened at one.
- Claims that are the subject of an unresolved public dispute between credible sources.
- Sexual violence, self-harm, named private individuals as victims, or deaths of identifiable private people. A quiz card is the wrong frame for these."""

VALENCE = """- valence: how the claim reads to a player — "positive" (flattering to India, the government, or an institution), "negative" (alarming, critical, or unflattering), or "neutral". Judge the claim as written, not the source."""

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
- usable: judged by both standards below. Fraudulent-website and job-scam alerts ARE usable when phrased as the false promise they made.
{VALENCE}

{SCOPE}

{JUDGEABILITY}

Return every input item exactly once, using its id."""

REAL_SYSTEM = f"""You turn official Indian government press releases into cards for a real-vs-fake news game.

Players see a claim and guess REAL or FAKE. The FAKE pool is sensational viral misinformation, so a REAL card must hold its own as shareable news — the kind of fact a person would actually forward to a family WhatsApp group.

For each input item (a genuine PIB press release with its DATE):
- Find the single most share-worthy fact in the release: a decision, launch, new rule, first-of-its-kind achievement, or a surprising qualitative fact. Ask: would a normal person retell this to a friend? Most press releases contain no such fact — that is expected.
- share_score: honest 1-5 rating (5 = would genuinely go viral; 3 = mildly interesting; 1 = only a bureaucrat cares). Most releases deserve 1-2.
- claim: the fact as a confident, informal assertion under 200 characters, phrased the way a person would retell it — never press-release officialese. Frame the ANNOUNCEMENT, decision, or event — NOT a tally. Reject any fact whose punchline is a cumulative total, count, or aggregate figure, even reframed as savings or impact: "the government will pay people for reporting dirty highway toilets" is good; "NHAI rewarded 429 commuters", "20,000 Jan Aushadhi centres opened", and "generic medicines saved Indians Rs 45,000 crore" are all BAD (mark them unusable). A single number is allowed only when it IS the surprise (a "Rs 1 lakh crore fund", "the 4th country ever to dock spacecraft") — never a running total. No honorifics, no "today". Must stay factually accurate to the release; for older releases, phrase it so it reads correctly with its DATE (anchor the year when it matters).
- category: one of the allowed values.
- explanation: ONE sentence shown after answering, confirming the fact and citing the PIB press release as the source.
- usable: false if ceremonial, routine administrative business, a tallied statistic (per above), share_score of 3 or less, or it fails either standard below.
{VALENCE}

{SCOPE}

{JUDGEABILITY}

Return every input item exactly once, using its id."""


FACTCHECK_SYSTEM = f"""You turn debunked misinformation into cards for a real-vs-fake news game.

Each input is an article from an independent Indian fact-checker (Alt News or Factly), with its DATE.

FIRST, DECIDE WHETHER IT IS EVEN A FACT-CHECK. These outlets also publish data journalism, explainers, interviews and media criticism — and THOSE ARTICLES ARE TRUE. If the input is not debunking a specific false claim, mark it unusable. Never invert a true article into a false claim; that would teach players the opposite of reality.
  IS a debunk: the headline says something is false, misleading, morphed, AI-generated, old, unrelated, or misattributed; or it corrects who/what/where something shows.
  NOT a debunk: a headline that simply reports a finding or statistic — "Ten States Account for 84% of India's Stampede Deaths" is a true data story. Mark unusable.

Your job is to recover THE FALSE CLAIM ITSELF as it circulated — the proposition people actually believed — NOT the article's description of the debunking.

Many fact-checks concern miscaptioned photos or video. For those, extract THE PROPOSITION THE MEDIA WAS USED TO SUPPORT:
  "Old Mumbai video falsely circulated as Naseeruddin Shah joining the stir"
     -> claim: "Naseeruddin Shah joined the Jantar Mantar protest"  (but see SCOPE — protests are out)
  "Viral 'pen bomb' warning messages baseless; police refute rumour"
     -> claim: "Pens rigged as bombs are being planted in public places"
If no standalone proposition survives — the fact-check is purely about where an image came from, with no claim a player could weigh — mark it unusable.

- claim: the FALSE claim as a confident assertion under 200 characters, in casual informal English, the way it actually circulated. Never use the words "fake", "false", "debunked", "viral", "rumour", or lead with "No,". No hashtags, no emoji.
- share_score: honest 1-5 for how share-worthy the claim reads.
- category: one of the allowed values.
{VALENCE}
- explanation: ONE sentence stating what the fact-checkers actually found. Name the outlet's finding, not the outlet's opinion.
- usable: judged by both standards below.

{SCOPE}

{JUDGEABILITY}

Return every input item exactly once, using its id."""


NEWS_SYSTEM = f"""You turn genuine Indian news into cards for a real-vs-fake news game.

Each input is a headline and summary from a mainstream Indian news outlet, with its DATE. Everything here is TRUE. Your job is to turn it into a claim that does NOT look obviously true.

- Find the single most share-worthy fact. Would a person forward this to a friend? Routine local municipal items ("expert team inspects sewage plant sites") have no such fact — mark them unusable. That is expected for most inputs.
- CRITICAL: do NOT select only flattering stories. Negative and neutral true news — failures, shortfalls, court rulings, price rises, disasters, criticism of institutions, things that went wrong — makes the BEST cards, because players wrongly assume that unflattering means fake. Actively prefer these when the input offers them.
- claim: under 200 characters, confident and informal, the way a person would retell it. AT MOST one number, rounded the way people speak. No honorifics, no "today". Anchor the year when it matters.
- Use ONLY facts stated in the headline and summary. If the summary is too thin to support a specific claim, mark it unusable — never infer or embellish.
- category: one of the allowed values.
{VALENCE}
- explanation: ONE sentence confirming the fact and citing the reporting outlet as the source.
- usable: false for opinion, analysis, editorials, "why X matters" explainers, listicles, previews of upcoming events, share_score of 3 or less, or if it fails either standard below.

{SCOPE}

{JUDGEABILITY}

Return every input item exactly once, using its id."""


def accept_real(card: dict) -> bool:
    """Gate for REAL cards.

    Share-worthiness keeps the pool competitive with viral fakes, but a
    merely-interesting UNFLATTERING truth is worth more to this game than a
    dazzling flattering one: players wrongly assume unflattering means fake,
    so those cards are where the learning happens. Hold them to a lower bar.
    """
    score = card["share_score"]
    return score >= 4 or (score >= 3 and card["valence"] != "positive")


def normalize_with_claude(client, system: str, items: list, min_share: int = 0,
                          accept=None) -> tuple:
    """items: [{id, text, date}] -> (validated card dicts, unusable real ids).

    Claude only ever sees short synthetic ids (batch positions) so it cannot
    mangle 19-digit tweet ids; we map back to real ids afterwards. Items it
    declares unusable — or that score below min_share — are returned
    separately so callers can tombstone them and never pay for them again.
    Items dropped by OUR validation are in neither list and will be retried
    on a future run.
    """
    cards_out, unusable, done = [], [], set()
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
            max_tokens=8000,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": CARD_SCHEMA}},
            messages=[{"role": "user", "content": payload}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        try:
            cards = json.loads(text)["cards"]
        except (json.JSONDecodeError, KeyError) as e:
            # A single truncated/garbled batch must not abort the whole run —
            # its items are neither kept nor tombstoned, so they retry next
            # time. The pool-level sanity floor below still catches systemic
            # breakage (e.g. every batch failing).
            warn(f"batch at offset {i} unparseable ({e}); skipping, will retry")
            if response.stop_reason == "max_tokens":
                warn(f"  (hit max_tokens — batch of {len(batch)} too large)")
            continue

        for card in cards:
            real_id = idmap.get(card["id"])
            if real_id is None:
                warn(f"claude returned unknown batch id {card['id']!r}; dropping")
                continue
            if real_id in done:  # Claude repeated an id in its response
                warn(f"{real_id}: duplicate in claude output; dropping repeat")
                continue
            done.add(real_id)
            # share_score is an analytics/gating hint, not a quality signal —
            # an out-of-range value coerces to 0 (fails any share gate) rather
            # than dropping the item into permanent retry limbo.
            score = card.get("share_score")
            score = score if isinstance(score, int) and 1 <= score <= 5 else 0
            ok = accept(card) if accept else score >= min_share
            if not card["usable"] or not ok:
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
                              "category": card["category"],
                              "valence": card["valence"], "explanation": expl})
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

    # ---- gather candidates from every source ----------------------------
    # Each pool carries its own prompt. Item ids ARE the final source_id, so
    # dedup, tombstoning, URL and date lookup all key off one value.
    urls, stypes, dates, pools = {}, {}, {}, []

    # 1. PIB Fact Check tweets (FAKE) — static archive, still the core set
    tweets = fetch_syndication_tweets()
    sindoor_ids = fetch_sindoor_tweet_ids()
    oembed_needed = [t for t in sindoor_ids
                     if t not in tweets and f"tweet:{t}" not in existing]
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
            warn("most oEmbed lookups failed — X may be rate-limiting or endpoint changed")

    pib_fakes = []
    for tid, info in tweets.items():
        sid = f"tweet:{tid}"
        if info["lang"] != "en" or sid in existing:
            continue
        pib_fakes.append({"id": sid, "text": info["text"], "date": info["date"],
                          "source_type": "factcheck:pib"})
        urls[sid] = f"https://x.com/PIBFactCheck/status/{tid}"
    pib_fakes = pib_fakes[:MAX_NEW_TWEETS_PER_RUN]
    log(f"pib fact-check tweets: {len(pib_fakes)} to normalize")
    pools.append(("pib-factcheck", "FAKE", pib_fakes, FAKE_SYSTEM, 0))

    # 2. Independent fact-checkers (FAKE) — daily-updating, broad topics
    log("independent fact-checkers:")
    fc_items = [i for i in feeds.fetch_factcheck_candidates(pages=FACTCHECK_PAGES)
                if i["id"] not in existing][:MAX_NEW_FACTCHECK_PER_RUN]
    for i in fc_items:
        urls[i["id"]] = i["url"]
    log(f"independent fact-checks: {len(fc_items)} to normalize")
    pools.append(("independent-factcheck", "FAKE", fc_items, FACTCHECK_SYSTEM, 0))

    # 3. PIB press releases (REAL) — the flattering side, share-gated
    releases = fetch_release_list()
    pib_reals = []
    for prid, title in releases:
        sid = f"pib:{prid}"
        if sid in existing:
            continue
        if len(pib_reals) >= MAX_NEW_RELEASES_PER_RUN:
            break
        detail = fetch_release_body(prid)
        if detail is None:
            warn(f"release {prid}: no usable body; skipping")
            continue
        pib_reals.append({"id": sid, "text": f"TITLE: {title}\nBODY: {detail['body']}",
                          "date": detail["date"], "source_type": "release:pib"})
        urls[sid] = f"https://www.pib.gov.in/PressReleasePage.aspx?PRID={prid}"
        time.sleep(0.3)
    log(f"pib press releases: {len(pib_reals)} to normalize")
    pools.append(("pib-release", "REAL", pib_reals, REAL_SYSTEM, 4))

    # 4. Mainstream news (REAL) — supplies the negative/neutral true claims
    #    that kill the "unflattering must be fake" heuristic
    log("news feeds:")
    news_items = [i for i in feeds.fetch_news_candidates(pages=NEWS_PAGES)
                  if i["id"] not in existing][:MAX_NEW_NEWS_PER_RUN]
    for i in news_items:
        urls[i["id"]] = i["url"]
    log(f"news items: {len(news_items)} to normalize")
    pools.append(("news", "REAL", news_items, NEWS_SYSTEM, 0))

    if not any(p[2] for p in pools):
        log("nothing new to do; exiting cleanly")
        return

    # ---- normalize ------------------------------------------------------
    cards, skips = [], []
    for label, verdict, items, prompt, min_share in pools:
        if not items:
            continue
        for it in items:
            dates[it["id"]] = it.get("date")
            stypes[it["id"]] = it.get("source_type", label)
        got, skipped = normalize_with_claude(
            client, prompt, items, min_share=min_share,
            accept=accept_real if verdict == "REAL" else None)
        if (len(got) + len(skipped)) < len(items) * 0.5:
            raise HarvestError(
                f"{label}: only {len(got) + len(skipped)}/{len(items)} items processed "
                "cleanly — prompt or source drift; refusing to write")
        for c in got:
            cards.append({**c, "verdict": verdict})
        skips += [(sid, verdict) for sid in skipped]
        log(f"  {label}: {len(got)} cards, {len(skipped)} unusable")

    fake_cards = [c for c in cards if c["verdict"] == "FAKE"]
    real_cards = [c for c in cards if c["verdict"] == "REAL"]

    # ---- rows -----------------------------------------------------------
    rows = []
    for c in cards:
        rows.append({
            "source_id": c["id"], "verdict": c["verdict"], "claim": c["claim"],
            "category": c["category"], "valence": c["valence"],
            "explanation": c["explanation"], "source_url": urls.get(c["id"], ""),
            "source_date": dates.get(c["id"]),
            "source_type": stypes.get(c["id"], "unknown"), "active": True,
        })
    for sid, verdict in skips:
        rows.append({
            "source_id": sid, "verdict": verdict,
            "claim": "[skipped during normalization]", "category": "misc",
            "valence": "neutral",
            "explanation": "Source item judged unusable for the game.",
            "source_url": urls.get(sid, ""), "source_date": dates.get(sid),
            "source_type": stypes.get(sid, "unknown"), "active": False,
        })

    # Final guard: one row per source_id (Postgres rejects an upsert batch
    # that touches the same conflict key twice). Accepted cards are appended
    # before tombstones, so keeping the first occurrence prefers the card.
    deduped, seen_sids = [], set()
    for r in rows:
        if r["source_id"] not in seen_sids:
            seen_sids.add(r["source_id"])
            deduped.append(r)
    if len(deduped) != len(rows):
        warn(f"dropped {len(rows) - len(deduped)} duplicate source_id rows before upsert")
    rows = deduped

    # Valence balance is the metric that matters: if every FAKE is alarming
    # and every REAL is flattering, players win on tone alone.
    def valence_mix(pool):
        n = len(pool) or 1
        c = collections.Counter(x["valence"] for x in pool)
        return {v: f"{100*c[v]//n}%" for v in ("positive", "negative", "neutral")}

    log(f"\nvalence mix  FAKE {valence_mix(fake_cards)}")
    log(f"valence mix  REAL {valence_mix(real_cards)}")
    pos_fake = sum(1 for c in fake_cards if c["valence"] == "positive")
    neg_real = sum(1 for c in real_cards if c["valence"] == "negative")
    log(f"cross-cutting cards: {pos_fake} flattering fakes, {neg_real} unflattering reals")

    if dry_run:
        log(f"\nDRY RUN: would upsert {len(rows)} rows "
            f"({len(fake_cards)} fake, {len(real_cards)} real, "
            f"{len(skips)} tombstones)")
        for label, pool in (("FAKE", fake_cards), ("REAL", real_cards)):
            log(f"\n=== {label} samples ({len(pool)}) ===")
            for c in pool[:25]:
                d = (dates.get(c["id"]) or "????")[:7]
                log(f"  [{d} {c['valence'][:3]} {c['category']}] {c['claim']}")
        return

    # In refresh mode, retire every existing card immediately before writing
    # the regenerated set, so no old-prompt card survives in a mixed deck.
    # Guard: refuse to wipe the deck if this run produced almost nothing.
    if refresh:
        active_new = len(fake_cards) + len(real_cards)
        if active_new < 20:
            raise HarvestError(
                f"refresh produced only {active_new} active cards — refusing to "
                "deactivate the live deck for so few replacements"
            )
        deactivate_all_cards(cfg)
        log("refresh: deactivated all prior cards")

    upsert_cards(cfg, rows)
    log(
        f"DONE: upserted {len(fake_cards)} fake + {len(real_cards)} real cards, "
        f"tombstoned {len(skips)} unusable items"
    )


if __name__ == "__main__":
    try:
        main()
    except HarvestError as e:
        print(f"HARVEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
