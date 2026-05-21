#!/usr/bin/env python3
"""
newsmoji - the hottest news story, retold entirely in emoji, as a newspaper.

Every 5 minutes a system cron runs this script. Per cycle it:

  1. Fetches a basket of major-outlet RSS feeds into a pooled story list.
  2. Anthropic call #1 (Claude Haiku): picks the single hottest story and
     translates its headline into a short emoji glyph.
  3. Fetches that story's full article body from the outlet's page.
  4. Anthropic call #2 (Claude Haiku): retells the full story as a long
     emoji narrative.
  5. Renders a self-contained index.html laid out like a newspaper front
     page - masthead, lead emoji, the emoji story in newsprint columns.
     The page is 100% emoji: not a single word of text anywhere.
  6. Publishes index.html to the qarl.com web host over ssh.

Pure Python standard library - no pip, no venv. Runs on everett.

Robustness contract: on ANY failure (feed fetch, API call, publish,
anything) the last good index.html stays published. Article fetch is
best-effort - if it fails the RSS summary is used instead. The page is
never overwritten with an error or a blank. Worst case it is a few
minutes stale, never broken.

See AGENTS.md / README.md for the full picture.
"""

import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Where runtime state lives. Deliberately on everett's local disk (NOT the
# JuiceFS-mounted repo) so a JuiceFS hiccup can't break the cron runtime.
STATE_DIR = Path(os.environ.get("NEWSMOJI_STATE_DIR", Path.home() / "newsmoji"))

LOG_PATH = STATE_DIR / "newsmoji.log"
INDEX_PATH = STATE_DIR / "index.html"          # last successfully rendered page
FEEDS_OVERRIDE = STATE_DIR / "feeds.txt"       # optional, one URL per line
ENV_PATH = STATE_DIR / "newsmoji.env"          # optional KEY=VALUE config

MAX_LOG_BYTES = 5 * 1024 * 1024                # trim log past 5 MB

# Anthropic API
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = "claude-haiku-4-5"                     # cheap; the task is easy
MAX_TOKENS = 600                               # cap for the pick call
STORY_MAX_TOKENS = 3000                        # cap for the narration call

# Story basket
MAX_ITEMS = 60                                 # cap sent to the pick call
HTTP_TIMEOUT = 12                              # per-feed fetch timeout (s)
ARTICLE_TIMEOUT = 15                           # article-page fetch timeout (s)
API_TIMEOUT = 60                               # Anthropic call timeout (s)
USER_AGENT = "newsmoji/1.0 (+https://newsmoji.qarl.com)"

# Article body extraction
MIN_ARTICLE_CHARS = 400                        # below this, extraction is thin
MAX_ARTICLE_CHARS = 6000                       # cap body text sent to model

# Default RSS basket - broad outlet/geography/spectrum spread so "hottest" is
# not skewed by one newsroom. Override by creating STATE_DIR/feeds.txt.
DEFAULT_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "http://rss.cnn.com/rss/cnn_topstories.rss",
    "https://moxie.foxnews.com/google-publisher/latest.xml",
    "https://feeds.skynews.com/feeds/rss/world.xml",
]

# Publish target. Filled in once Jimmy-www authorizes everett's key and the
# newsmoji.qarl.com vhost exists. PUBLISH_SSH is an ssh destination (a Host
# alias from ~/.ssh/config is fine). While blank, publishing is skipped and
# the page is generated locally only.
PUBLISH_SSH = os.environ.get("NEWSMOJI_PUBLISH_SSH", "newsmoji-web")
PUBLISH_REMOTE_PATH = os.environ.get(
    "NEWSMOJI_PUBLISH_PATH", "/home/qqqqarl/newsmoji.qarl.com/index.html"
)
PUBLISH_ENABLED = os.environ.get("NEWSMOJI_PUBLISH_ENABLED", "0") == "1"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def log(msg, level="INFO"):
    """Append a timestamped line to the log file (and stderr if interactive).

    Log space is cheap - this stays verbose on purpose.
    """
    line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} [{level}] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stderr.isatty():
        print(line, file=sys.stderr)


def trim_log():
    """Keep the log from growing without bound. Cheap, runs once per cycle."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            data = LOG_PATH.read_bytes()
            keep = data[-(MAX_LOG_BYTES // 2):]
            cut = keep.find(b"\n")
            if cut != -1:
                keep = keep[cut + 1:]
            LOG_PATH.write_bytes(b"# --- log trimmed ---\n" + keep)
            log("log trimmed (exceeded size cap)")
    except OSError as exc:
        log(f"could not trim log: {exc}", "WARN")


# --------------------------------------------------------------------------
# Config / secrets
# --------------------------------------------------------------------------

def _load_env_file(path):
    """Parse a simple KEY=VALUE file. Missing file -> empty dict."""
    env = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def get_api_key():
    """Resolve the Anthropic API key.

    Order: process environment, then STATE_DIR/newsmoji.env, then
    ~/Jimmy/.env. A standalone pay-as-you-go console key - never the
    Claude subscription, which hits weekly limits.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    for path in (ENV_PATH, Path.home() / "Jimmy" / ".env"):
        key = _load_env_file(path).get("ANTHROPIC_API_KEY", "").strip()
        if key:
            return key
    raise RuntimeError(
        "no ANTHROPIC_API_KEY found (env, "
        f"{ENV_PATH}, or ~/Jimmy/.env)"
    )


def load_feeds():
    """Return the RSS feed list - the override file if present, else default."""
    if FEEDS_OVERRIDE.exists():
        feeds = []
        for raw in FEEDS_OVERRIDE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                feeds.append(line)
        if feeds:
            log(f"using {len(feeds)} feed(s) from {FEEDS_OVERRIDE}")
            return feeds
    return list(DEFAULT_FEEDS)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ASCII_WORD_RE = re.compile(r"[A-Za-z]+")
# A bare ASCII digit - one NOT already part of a keycap emoji sequence
# (digit + U+FE0F + U+20E3), so existing keycaps are left intact.
_BARE_DIGIT_RE = re.compile(r"[0-9](?![️⃣])")

# RSS/Atom child tags that can carry a story summary. We keep whichever
# yields the most text, so a richer field always wins over a terse one.
SUMMARY_TAGS = ("description", "summary", "encoded", "content", "subtitle")


def clean_text(raw):
    """Strip any HTML tags, unescape entities, collapse whitespace.

    RSS <description> fields and JSON-LD bodies routinely carry HTML or
    stray markup; this flattens whatever comes in to plain prose text.
    """
    if not raw:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())


def enforce_emoji(text):
    """Force a model emoji field to be 100% emoji: no letters, no bare digits.

    Drops any ASCII letter runs and promotes any stray ASCII digit to its
    keycap-emoji form, then re-normalises spacing so the space-separated
    emoji layout survives. A defensive net - the prompts already ask for
    emoji only, but the all-emoji page contract is strict.
    """
    text = _ASCII_WORD_RE.sub(" ", text)
    text = _BARE_DIGIT_RE.sub(lambda m: m.group() + "️⃣", text)
    return " ".join(text.split())


# --------------------------------------------------------------------------
# RSS fetching
# --------------------------------------------------------------------------

def fetch_feed(url):
    """Fetch one RSS/Atom feed -> list of {"title","summary","link"} dicts."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    stories = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = ""
        summary = ""
        link = ""
        for child in node:
            ctag = child.tag.rsplit("}", 1)[-1].lower()
            if ctag == "title" and not title:
                title = clean_text("".join(child.itertext()))
            elif ctag in SUMMARY_TAGS:
                text = clean_text("".join(child.itertext()))
                if len(text) > len(summary):
                    summary = text  # keep the richest summary-ish field
            elif ctag == "link" and not link:
                href = (child.get("href") or "").strip()      # Atom
                if href:
                    if child.get("rel", "alternate") == "alternate":
                        link = href
                elif child.text and child.text.strip():        # RSS
                    link = child.text.strip()
        if title:
            stories.append(
                {"title": title, "summary": summary, "link": link}
            )
    return stories


def gather_items(feeds):
    """Fetch every feed and round-robin merge into a deduped story pool.

    Round-robin keeps any single outlet from dominating the basket. Per-feed
    failures are logged and skipped - as long as one feed works we proceed.
    Each pooled item is a {"title", "summary", "link"} dict.
    """
    per_feed = []
    for url in feeds:
        try:
            stories = fetch_feed(url)
            if stories:
                per_feed.append(stories)
                log(f"feed OK ({len(stories):>2} stories): {url}")
            else:
                log(f"feed EMPTY: {url}", "WARN")
        except (urllib.error.URLError, ET.ParseError, ValueError,
                TimeoutError, OSError) as exc:
            log(f"feed FAIL: {url} -- {exc}", "WARN")

    pooled = []
    seen = set()
    for i in range(max((len(s) for s in per_feed), default=0)):
        for stories in per_feed:
            if i < len(stories):
                story = stories[i]
                key = story["title"].lower()
                if key not in seen:
                    seen.add(key)
                    pooled.append(story)
    return pooled[:MAX_ITEMS]


# --------------------------------------------------------------------------
# Article fetching - pull the full story body for the chosen item
# --------------------------------------------------------------------------

_LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_article_body(data):
    """Recursively search parsed JSON-LD for an articleBody string."""
    if isinstance(data, dict):
        body = data.get("articleBody")
        if isinstance(body, str) and body.strip():
            return body
        for value in data.values():
            found = _jsonld_article_body(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _jsonld_article_body(item)
            if found:
                return found
    return ""


class _ParagraphExtractor(HTMLParser):
    """Collect text inside <p> tags, skipping <script>/<style> noise."""

    _SKIP_TAGS = ("script", "style", "noscript", "figure", "aside")

    def __init__(self):
        super().__init__()
        self._in_p = 0
        self._skip = 0
        self._buf = []
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._in_p += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            text = clean_text("".join(self._buf))
            if len(text) > 40:                # drop nav/caption fragments
                self.paragraphs.append(text)
            self._buf = []

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def fetch_article(url):
    """Fetch one article URL and return its body text, or "" on any failure.

    Tries JSON-LD articleBody first (clean and complete on most major
    outlets), then falls back to concatenating <p> paragraphs. Never raises:
    a failure here just means the caller falls back to the RSS summary.
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=ARTICLE_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read().decode(charset, "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            LookupError) as exc:
        # LookupError covers a page declaring a bogus charset - article
        # fetch is best-effort and must never escape to break the cycle.
        log(f"article fetch FAIL: {url} -- {exc}", "WARN")
        return ""

    # 1. JSON-LD articleBody - the cleanest source when present.
    for match in _LDJSON_RE.finditer(page):
        try:
            body = _jsonld_article_body(json.loads(match.group(1).strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        body = clean_text(body)
        if len(body) >= MIN_ARTICLE_CHARS:
            return body[:MAX_ARTICLE_CHARS]

    # 2. Fallback: stitch together the page's <p> paragraphs.
    parser = _ParagraphExtractor()
    try:
        parser.feed(page)
    except (ValueError, AssertionError):
        pass
    body = clean_text(" ".join(parser.paragraphs))
    if len(body) >= MIN_ARTICLE_CHARS:
        return body[:MAX_ARTICLE_CHARS]

    log(f"article extract thin ({len(body)} chars): {url}", "WARN")
    return ""


# --------------------------------------------------------------------------
# Anthropic API
# --------------------------------------------------------------------------

SYSTEM_PICK = (
    "You are the editor of newsmoji. You receive a numbered list of current "
    "news headlines. Pick the SINGLE hottest one - the most significant, "
    "urgent, or widely-discussed story right now - and translate that "
    "headline into a SHORT, punchy sequence of emoji (2 to 6 emoji) that "
    "captures its gist. Favour clear, recognizable emoji. The emoji field "
    "must contain ONLY emoji - never letters, words, or names. "
    "Respond with JSON only, no prose, no code fences."
)

SYSTEM_NARRATE = (
    "You are the editor of newsmoji. You receive one full news story. "
    "Retell the WHOLE story as a long, detailed sequence of emoji - an "
    "emoji newspaper article. Walk through it in order: who, what, where, "
    "when, why, and what happens next. Be thorough and specific; use as "
    "many emoji as the story needs (aim for 70 to 140). Separate every "
    "emoji - or tight 2-3 emoji phrase - with a single space, like emoji "
    "words in a sentence. Use ONLY emoji: never letters, words, or names, "
    "and write any numbers as number/keycap emoji. A reader should be able "
    "to follow the whole story from the emoji alone. "
    "Respond with JSON only, no prose, no code fences."
)


def _anthropic(system, user_msg, api_key, max_tokens):
    """One Anthropic messages call with JSON prefill -> parsed dict.

    Raises on transport/HTTP errors and on unparseable output; callers
    treat any exception as "abort the cycle, keep the last good page".
    """
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": "{"},  # prefill -> force JSON
        ],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Surface the API's explanation (bad key, overload, rate limit) - it
        # is the single most useful thing when debugging a stale page.
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except OSError:
            pass
        log(f"API HTTP {exc.code} {exc.reason}: {body}", "ERROR")
        raise

    usage = result.get("usage", {})
    stop = result.get("stop_reason")
    log(f"API ok: in={usage.get('input_tokens')} "
        f"out={usage.get('output_tokens')} stop={stop} "
        f"model={result.get('model')}")
    if stop == "max_tokens":
        log("API hit max_tokens - response may be truncated", "WARN")

    text = "{" + "".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def _has_emoji(text):
    """True if the string contains at least one non-ASCII pictographic char."""
    return any(ord(ch) >= 0x2190 for ch in text)


def pick_lead(items, api_key):
    """Anthropic call #1: pick the hottest story -> (index, headline, emoji)."""
    numbered = "\n".join(
        f"{i + 1}. {item['title']}" for i, item in enumerate(items)
    )
    user_msg = (
        "Here are the current news headlines:\n\n"
        f"{numbered}\n\n"
        "Respond with exactly this JSON shape:\n"
        '{"number": <list number of the hottest story>, '
        '"headline": "<that headline, copied verbatim>", '
        '"emoji": "<2 to 6 emoji>"}'
    )
    parsed = _anthropic(SYSTEM_PICK, user_msg, api_key, MAX_TOKENS)

    headline = str(parsed.get("headline", "")).strip()
    emoji = enforce_emoji(str(parsed.get("emoji", "")))
    try:
        index = int(parsed.get("number")) - 1
    except (TypeError, ValueError):
        index = -1
    if not 0 <= index < len(items):
        # Number was missing or junk - fall back to matching the headline.
        for i, item in enumerate(items):
            if item["title"].lower() == headline.lower():
                index = i
                break
    if not 0 <= index < len(items) or not emoji:
        raise ValueError(f"pick step returned unusable result: {parsed!r}")
    if not _has_emoji(emoji):
        raise ValueError(f"pick step returned no emoji: {emoji!r}")
    # Trust the basket's own headline text over the model's transcription.
    return index, items[index]["title"], emoji


def emojify_story(headline, body, api_key):
    """Anthropic call #2: translate the full story body into emoji prose."""
    user_msg = (
        f"Headline: {headline}\n\n"
        f"Full story:\n{body}\n\n"
        "Respond with exactly this JSON shape:\n"
        '{"story_emoji": "<the whole story as many space-separated emoji>"}'
    )
    parsed = _anthropic(SYSTEM_NARRATE, user_msg, api_key,
                        max_tokens=STORY_MAX_TOKENS)
    story_emoji = enforce_emoji(str(parsed.get("story_emoji", "")))
    if not story_emoji:
        raise ValueError(f"narrate step returned nothing: {parsed!r}")
    if not _has_emoji(story_emoji):
        raise ValueError(f"narrate step returned no emoji: {story_emoji!r}")
    return story_emoji


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

# digit -> keycap emoji (e.g. "2" -> 2 + variation selector + enclosing keycap)
_KEYCAP = {str(d): f"{d}️⃣" for d in range(10)}


def _emoji_number(value):
    """Render an integer (or digit string) as keycap-digit emoji."""
    return "".join(_KEYCAP.get(ch, ch) for ch in str(value))


PAGE_TEMPLATE = """<!doctype html>
<html lang="zxx">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<meta name="robots" content="index, follow">
<title>📰😀</title>
<style>
  :root {{
    --paper: #f4efe1;
    --ink: #1a1712;
    --rule: #28231b;
    --hair: #c7bda3;
  }}
  html, body {{
    margin: 0; height: 100dvh; overflow: hidden;
    background: #1b1916;
    color: var(--ink);
    font-family: Georgia, "Times New Roman", serif;
  }}
  body {{
    display: flex; align-items: center; justify-content: center;
  }}
  .emoji {{
    font-family: "Apple Color Emoji", "Segoe UI Emoji",
                 "Noto Color Emoji", serif;
  }}
  /* The sheet: forced into a portrait broadsheet even on a landscape
     desktop. Width is derived from the viewport HEIGHT, so the page is
     always a tall newspaper column with the dark desk around it; it
     scrolls inside, like a phone, when the story runs long. container-type
     sizes the emoji to the sheet (cqi units), not the browser window. */
  #paper {{
    --sheet-h: calc(100dvh - 32px);
    width: min(100vw, calc(var(--sheet-h) * 0.62));
    height: var(--sheet-h);
    overflow-y: auto;
    container-type: inline-size;
    background: var(--paper);
    padding: 22px clamp(14px, 3.2vh, 32px) 46px;
    box-sizing: border-box;
    box-shadow: 0 0 50px rgba(0, 0, 0, 0.6);
  }}
  #nameplate {{
    text-align: center; line-height: 1.05;
    font-size: 24cqi; margin: 2px 0 10px;
  }}
  #dateline {{
    display: flex; justify-content: space-between; flex-wrap: wrap;
    align-items: center; gap: 5px 14px; font-size: 3.4cqi;
    border-top: 3px double var(--rule);
    border-bottom: 3px double var(--rule);
    padding: 7px 2px; margin-bottom: 20px;
  }}
  #lead-emoji {{
    text-align: center; line-height: 1.14;
    font-size: 13.5cqi; margin: 10px 0 14px;
  }}
  #hr {{
    border: 0; border-top: 3px double var(--rule); margin: 2px 0 18px;
  }}
  #story {{
    columns: 3 8rem; column-gap: 20px;
    column-rule: 1px solid var(--hair);
    font-size: 6cqi; line-height: 1.5;
    text-align: justify; word-spacing: 0.04em;
  }}
  #footer {{
    text-align: center; font-size: 3cqi; letter-spacing: 0.16em;
    border-top: 1px solid var(--rule);
    margin-top: 26px; padding-top: 10px;
  }}
</style>
</head>
<body>
<div id="paper">
  <header>
    <div id="nameplate" class="emoji">📰😀</div>
    <div id="dateline" class="emoji">
      <span>🌐🔥</span>
      <span>{dateline}</span>
      <span>👀🗞️</span>
    </div>
  </header>
  <div id="lead-emoji" class="emoji">{emoji}</div>
  <hr id="hr">
  <div id="story" class="emoji">{story_emoji}</div>
  <div id="footer" class="emoji" data-epoch="{epoch}">🔄 5️⃣</div>
</div>
<script>
(function () {{
  var paper = document.getElementById("paper");
  var story = document.getElementById("story");
  var footer = document.getElementById("footer");
  var nameplate = document.getElementById("nameplate");
  var lead = document.getElementById("lead-emoji");

  // Stamp the footer with the local edition time, in keycap-digit emoji.
  var epoch = parseInt(footer.getAttribute("data-epoch"), 10);
  if (epoch) {{
    var d = new Date(epoch * 1000);
    var kc = function (n) {{
      return String(n).padStart(2, "0").replace(/[0-9]/g, function (c) {{
        return c + "️⃣";
      }});
    }};
    footer.textContent = "🗞️ 🕐 " + kc(d.getHours()) + " "
      + kc(d.getMinutes()) + "   🔄 5️⃣";
  }}

  // Fit the whole front page onto the sheet with no scrolling: binary
  // search the largest story-emoji size at which nothing overflows. A long
  // story lands on small emoji, a short one grows to fill the page.
  function fit() {{
    nameplate.style.fontSize = "";   // reset chrome so resize can grow back
    lead.style.fontSize = "";
    var lo = 5, hi = 64, best = lo;
    for (var i = 0; i < 20; i++) {{
      var mid = (lo + hi) / 2;
      story.style.fontSize = mid + "px";
      if (paper.scrollHeight <= paper.clientHeight) {{
        best = mid;
        lo = mid;
      }} else {{
        hi = mid;
      }}
    }}
    story.style.fontSize = best + "px";
    // Last resort on a very short viewport: shrink the masthead and lead
    // emoji too (their CSS sizes are 24cqi and 13.5cqi) until it all fits.
    for (var s = 0.92; paper.scrollHeight > paper.clientHeight && s > 0.3;
         s -= 0.08) {{
      nameplate.style.fontSize = (24 * s) + "cqi";
      lead.style.fontSize = (13.5 * s) + "cqi";
    }}
  }}
  fit();
  window.addEventListener("load", fit);
  window.addEventListener("resize", fit);
}})();
</script>
</body>
</html>
"""


def render_html(emoji, story_emoji):
    """Render the self-contained page. String formatting only - cannot fail."""
    now = datetime.now(timezone.utc)
    dateline = (
        "🗓️ "
        + _emoji_number(f"{now.day:02d}") + " · "
        + _emoji_number(f"{now.month:02d}") + " · "
        + _emoji_number(now.year)
    )
    return PAGE_TEMPLATE.format(
        emoji=html.escape(emoji),
        story_emoji=html.escape(story_emoji),
        dateline=dateline,
        epoch=int(time.time()),
    )


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------

def publish(local_path):
    """Push index.html to the web host. scp to a .tmp, then ssh-mv (atomic).

    Returns True on success. A failure here leaves the remote untouched, so
    the previously published page keeps serving.
    """
    if not PUBLISH_ENABLED:
        log("publish: disabled (NEWSMOJI_PUBLISH_ENABLED != 1) -- "
            "page generated locally only", "WARN")
        return False
    if not PUBLISH_SSH or not PUBLISH_REMOTE_PATH:
        log("publish: target not configured -- page generated locally only",
            "WARN")
        return False

    remote_tmp = PUBLISH_REMOTE_PATH + ".tmp"
    ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    scp_cmd = ["scp", "-q", *ssh_opts, str(local_path),
               f"{PUBLISH_SSH}:{remote_tmp}"]
    mv_cmd = ["ssh", *ssh_opts, PUBLISH_SSH,
              f"mv -f {shlex.quote(remote_tmp)} "
              f"{shlex.quote(PUBLISH_REMOTE_PATH)}"]
    try:
        subprocess.run(scp_cmd, check=True, capture_output=True,
                       text=True, timeout=60)
        subprocess.run(mv_cmd, check=True, capture_output=True,
                       text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        log(f"publish FAIL: {' '.join(exc.cmd)} -- "
            f"rc={exc.returncode} {exc.stderr.strip()}", "ERROR")
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"publish FAIL: {exc}", "ERROR")
        return False
    log(f"published -> {PUBLISH_SSH}:{PUBLISH_REMOTE_PATH}")
    return True


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------

# Anything raised by an Anthropic call that means "abort, keep last good page".
API_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, ValueError,
              KeyError, json.JSONDecodeError, TimeoutError, OSError)


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    trim_log()
    log("=== cycle start ===")
    started = time.monotonic()

    try:
        api_key = get_api_key()
    except RuntimeError as exc:
        log(f"abort: {exc} -- keeping last good page", "ERROR")
        return 1

    feeds = load_feeds()
    items = gather_items(feeds)
    if not items:
        log("abort: no stories from any feed -- keeping last good page",
            "ERROR")
        return 1
    log(f"pooled {len(items)} stories from {len(feeds)} feed(s)")

    try:
        index, headline, emoji = pick_lead(items, api_key)
    except API_ERRORS as exc:
        log(f"abort: pick step failed ({exc}) -- keeping last good page",
            "ERROR")
        return 1
    log(f"chosen headline: {headline}")
    log(f"emoji: {emoji}")

    # Best-effort full article; fall back to the RSS summary, then headline.
    chosen = items[index]
    body = fetch_article(chosen.get("link", ""))
    if len(body) >= MIN_ARTICLE_CHARS:
        log(f"fetched full article body ({len(body)} chars)")
    else:
        body = chosen.get("summary", "")
        if body:
            log(f"using RSS summary as story body ({len(body)} chars)", "WARN")
        else:
            body = headline
            log("no body or summary -- narrating from headline alone", "WARN")

    try:
        story_emoji = emojify_story(headline, body, api_key)
    except API_ERRORS as exc:
        log(f"abort: narrate step failed ({exc}) -- keeping last good page",
            "ERROR")
        return 1
    log(f"story emoji ({len(story_emoji)} chars): {story_emoji}")

    page = render_html(emoji, story_emoji)
    try:
        tmp = INDEX_PATH.with_suffix(".html.tmp")
        tmp.write_text(page, encoding="utf-8")
        tmp.replace(INDEX_PATH)
    except OSError as exc:
        log(f"abort: could not write {INDEX_PATH} ({exc}) -- "
            "keeping last good page", "ERROR")
        return 1
    log(f"rendered {INDEX_PATH} ({len(page)} bytes)")

    published = publish(INDEX_PATH)
    elapsed = time.monotonic() - started
    log(f"=== cycle done in {elapsed:.1f}s "
        f"(published={published}) ===")
    return 0 if published or not PUBLISH_ENABLED else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - last-ditch guard, never crash loud
        log(f"unhandled exception: {exc!r} -- keeping last good page", "ERROR")
        sys.exit(1)
