# newsmoji

The single hottest news headline, translated into emoji, scaled to fill your
screen. Live at **https://newsmoji.qarl.com**.

```
                     🌍🔥📰  ->  the news, but make it emoji
```

## What it does

Every 5 minutes a cron job on **everett** runs `newsmoji.py`, which:

1. **Fetches** a basket of major-outlet RSS feeds into a pooled headline list.
2. **Picks + translates** - one Anthropic API call (Claude Haiku) chooses the
   hottest headline and renders it as 2-6 emoji.
3. **Renders** a single self-contained `index.html`: the emoji scaled to fill
   the viewport, a subtle headline caption, meta-refresh every 5 minutes.
4. **Publishes** `index.html` to the qarl.com web host over ssh.

everett is tailnet-only and can't serve the public internet, so it generates
the page and pushes it to the public DreamHost-hosted web host.

## Robustness

The page **never breaks**. On any failure - a feed is down, the API call
fails, the publish step fails - the cycle aborts and the last good
`index.html` keeps serving. Worst case the page is a few minutes stale.

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
(console.anthropic.com), not a Claude subscription.

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
