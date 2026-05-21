# newsmoji

The single hottest news story, retold **entirely in emoji**, laid out like a
newspaper front page. Live at **https://newsmoji.qarl.com**.

```
                     🌍🔥📰  ->  the news, but make it emoji
```

## What it does

Every 5 minutes a cron job on **everett** runs `newsmoji.py`, which:

1. **Fetches** a basket of major-outlet RSS feeds into a pooled story list.
2. **Picks** - Anthropic call #1 (Claude Haiku) chooses the hottest story and
   renders its headline as a short emoji glyph.
3. **Reads** - fetches that story's full article body from the outlet's page
   (JSON-LD `articleBody`, with a `<p>`-scraping fallback).
4. **Translates** - Anthropic call #2 (Claude Haiku) retells the whole story
   as a long emoji narrative.
5. **Renders** a single self-contained `index.html`: a portrait-broadsheet
   newspaper - emoji masthead, lead emoji, the emoji story in newsprint
   columns - auto-sized to fit the screen with no scrolling. The page is
   100% emoji: not one word of text anywhere.
6. **Publishes** `index.html` to the qarl.com web host over ssh.

everett is tailnet-only and can't serve the public internet, so it generates
the page and pushes it to the public DreamHost-hosted web host.

## Robustness

The page **never breaks**. On any failure - a feed is down, an API call
fails, the publish step fails - the cycle aborts and the last good
`index.html` keeps serving. The article fetch (step 3) is best-effort: if it
fails, the RSS summary is used instead, then the bare headline. Worst case
the page is a few minutes stale.

## Layout

| Path | What |
|------|------|
| `newsmoji.py` | The whole runtime. Pure Python stdlib - no pip, no venv. |
| `deploy.sh`   | Installs/refreshes the runtime + cron on everett. |
| `AGENTS.md`   | Notes for Jimmy-newsmoji (not committed). |

Runtime state lives in `~/newsmoji/` on everett (local disk, not the repo):
`newsmoji.env` (the API key), `index.html` (last good page), `newsmoji.log`
(verbose log), `cron.err`, optional `feeds.txt` (feed override).

## Setup / deploy

On everett:

```sh
cd /project/newsmoji
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/newsmoji/newsmoji.env   # console key
chmod 600 ~/newsmoji/newsmoji.env
./deploy.sh                 # install runtime + cron (publishing off)
PUBLISH=1 ./deploy.sh       # ...and enable publishing once the host is wired
```

The API key must be a **standalone pay-as-you-go console key**
(console.anthropic.com), not a Claude subscription. Each cycle makes two
Haiku calls (pick + translate) plus one article-page fetch.

## Operating

```sh
tail -f ~/newsmoji/newsmoji.log     # watch cycles
python3 ~/newsmoji/newsmoji.py      # run one cycle by hand (logs to stderr)
crontab -l                          # confirm the */5 schedule
```

To change the feed basket without touching code, drop a `feeds.txt` in
`~/newsmoji/` - one RSS URL per line, `#` for comments.

## Config knobs

Environment variables (set in the crontab or shell):

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (from `newsmoji.env`) | API key. |
| `NEWSMOJI_PUBLISH_ENABLED` | `0` | `1` to actually publish. |
| `NEWSMOJI_PUBLISH_SSH` | `newsmoji-web` | ssh destination for publishing. |
| `NEWSMOJI_PUBLISH_PATH` | `/home/qqqqarl/newsmoji.qarl.com/index.html` | remote path. |
| `NEWSMOJI_STATE_DIR` | `~/newsmoji` | runtime state dir. |
