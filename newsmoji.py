#!/usr/bin/env python3
"""
newsmoji - the hottest news headline, translated to emoji, on a full-screen page.

Every 5 minutes a system cron runs this script. Per cycle it:

  1. Fetches a basket of major-outlet RSS feeds into a pooled headline list.
  2. Makes ONE Anthropic API call (Claude Haiku) to pick the single hottest
     headline and translate it into emoji.
  3. Renders a self-contained index.html (emoji scaled to fill the viewport,
     meta-refresh every 5 min).
  4. Publishes index.html to the qarl.com web host over ssh.

Pure Python standard library - no pip, no venv. Runs on everett.

Robustness contract: on ANY failure (feed fetch, API call, publish, anything)
the last good index.html stays published. The page is never overwritten with
an error or a blank. Worst case it is a few minutes stale, never broken.

See AGENTS.md / README.md for the full picture.
"""

import html
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
MAX_TOKENS = 500

# Headline basket
MAX_HEADLINES = 60                             # cap sent to the model
HTTP_TIMEOUT = 12                              # per-feed fetch timeout (s)
API_TIMEOUT = 40                               # Anthropic call timeout (s)
USER_AGENT = "newsmoji/1.0 (+https://newsmoji.qarl.com)"

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
# RSS fetching
# --------------------------------------------------------------------------

def fetch_feed(url):
    """Fetch one RSS/Atom feed and return its item/entry titles."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    titles = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        for child in node:
            if child.tag.rsplit("}", 1)[-1].lower() == "title":
                text = "".join(child.itertext()).strip()
                if text:
                    titles.append(text)
                break
    return titles


def gather_headlines(feeds):
    """Fetch every feed and round-robin merge into a deduped headline pool.

    Round-robin keeps any single outlet from dominating the basket. Per-feed
    failures are logged and skipped - as long as one feed works we proceed.
    """
    per_feed = []
    for url in feeds:
        try:
            titles = fetch_feed(url)
            if titles:
                per_feed.append(titles)
                log(f"feed OK ({len(titles):>2} headlines): {url}")
            else:
                log(f"feed EMPTY: {url}", "WARN")
        except (urllib.error.URLError, ET.ParseError, ValueError,
                TimeoutError, OSError) as exc:
            log(f"feed FAIL: {url} -- {exc}", "WARN")

    pooled = []
    seen = set()
    for i in range(max((len(t) for t in per_feed), default=0)):
        for titles in per_feed:
            if i < len(titles):
                title = " ".join(titles[i].split())  # collapse whitespace
                key = title.lower()
                if title and key not in seen:
                    seen.add(key)
                    pooled.append(title)
    return pooled[:MAX_HEADLINES]


# --------------------------------------------------------------------------
# Anthropic API call
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are the editor of newsmoji. You receive a list of current news "
    "headlines. Do two things:\n"
    "1. Pick the SINGLE hottest headline - the most significant, urgent, or "
    "widely-discussed story in the list right now.\n"
    "2. Translate that headline into a short sequence of emoji (2 to 6 emoji) "
    "that captures its meaning. Someone seeing only the emoji should be able "
    "to guess the gist of the story. Favour clear, recognizable emoji.\n"
    "Respond with JSON only, no prose, no code fences."
)


def pick_emoji(headlines, api_key):
    """One Anthropic call: hottest headline in, (headline, emoji) out."""
    numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
    user_msg = (
        "Here are the current news headlines:\n\n"
        f"{numbered}\n\n"
        'Respond with exactly this JSON shape:\n'
        '{"headline": "<the exact chosen headline, copied verbatim>", '
        '"emoji": "<2 to 6 emoji>"}'
    )
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
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
    log(f"API ok: in={usage.get('input_tokens')} "
        f"out={usage.get('output_tokens')} model={result.get('model')}")

    text = "{" + "".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )
    parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])

    headline = str(parsed.get("headline", "")).strip()
    emoji = str(parsed.get("emoji", "")).strip()
    if not headline or not emoji:
        raise ValueError(f"model returned incomplete result: {parsed!r}")
    if all(ord(ch) < 0x2190 for ch in emoji):
        raise ValueError(f"model returned no emoji characters: {emoji!r}")
    return headline, emoji


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<meta name="description" content="The hottest news headline, in emoji.">
<meta name="robots" content="index, follow">
<title>newsmoji</title>
<style>
  html, body {{
    height: 100%; margin: 0; background: #0b0b0f; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    overflow: hidden;
  }}
  #stage {{
    position: fixed; inset: 0;
    display: flex; align-items: center; justify-content: center;
  }}
  #emoji {{
    white-space: nowrap; line-height: 1; font-size: 100px; user-select: none;
    font-family: "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji",
                 sans-serif;
  }}
  #footer {{
    position: fixed; left: 0; right: 0; bottom: 0; padding: 16px 18px;
    text-align: center; pointer-events: none;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.6));
  }}
  #headline {{
    color: #c9c9d6; font-size: 15px; line-height: 1.35;
    max-width: 52rem; margin: 0 auto;
  }}
  #ts {{ color: #6a6a78; font-size: 12px; margin-top: 4px; }}
</style>
</head>
<body>
<div id="stage"><div id="emoji">{emoji}</div></div>
<div id="footer">
  <div id="headline">{headline}</div>
  <div id="ts" data-epoch="{epoch}">updated &hellip;</div>
</div>
<script>
(function () {{
  var emoji = document.getElementById("emoji");
  function fit() {{
    emoji.style.fontSize = "100px";
    var w = (window.innerWidth * 0.92) / emoji.offsetWidth;
    var h = (window.innerHeight * 0.74) / emoji.offsetHeight;
    emoji.style.fontSize = (100 * Math.max(0.1, Math.min(w, h))) + "px";
  }}
  function stamp() {{
    var el = document.getElementById("ts");
    var ms = parseInt(el.getAttribute("data-epoch"), 10) * 1000;
    if (ms) {{
      var d = new Date(ms);
      el.textContent = "updated " + d.toLocaleTimeString([],
        {{ hour: "2-digit", minute: "2-digit" }});
    }}
  }}
  fit();
  stamp();
  window.addEventListener("resize", fit);
}})();
</script>
</body>
</html>
"""


def render_html(headline, emoji):
    """Render the self-contained page. String formatting only - cannot fail."""
    return PAGE_TEMPLATE.format(
        emoji=html.escape(emoji),
        headline=html.escape(headline),
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
    headlines = gather_headlines(feeds)
    if not headlines:
        log("abort: no headlines from any feed -- keeping last good page",
            "ERROR")
        return 1
    log(f"pooled {len(headlines)} headline(s) from {len(feeds)} feed(s)")

    try:
        headline, emoji = pick_emoji(headlines, api_key)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            KeyError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        log(f"abort: API step failed ({exc}) -- keeping last good page",
            "ERROR")
        return 1
    log(f"chosen headline: {headline}")
    log(f"emoji: {emoji}")

    page = render_html(headline, emoji)
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
