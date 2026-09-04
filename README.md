# newsmoji

The single hottest news story, retold **entirely in emoji**, laid out like a
newspaper front page. The future of being informed.

<p align="center">
  <a href="https://www.qarl.com/newsmoji/">
    <img src="screenshot.png" width="420"
         alt="newsmoji - an all-emoji newspaper front page, printed in grayscale on newsprint">
  </a>
  <br>
  <strong><a href="https://www.qarl.com/newsmoji/">Read today's edition, live &rarr;</a></strong>
</p>

## Why

Let's be adults about this: nobody reads anymore. You skimmed a headline,
glanced at the thumbnail, and felt informed. That instinct is correct -
newsmoji simply finishes the thought.

Emoji is the first genuinely universal language: every device on Earth
ships the same glyphs, no translation required, none of the usual trouble
with *grammar*. Humanity spent five thousand years migrating away from
hieroglyphics, and with the benefit of hindsight the Egyptians were simply
early. newsmoji closes the loop and returns the news to pictures, where it
belongs.

Consider it a head start. When the official Duolingo emoji course finally
ships there will be a stampede - so get fluent now, while reading 📰🌍🔥 is
still a rare and marketable skill, and you'll be the only person in the
meeting who knows what is going on in the world.

## What it does

`newsmoji.py` runs one cycle, end to end:

1. **Fetches** a basket of major-outlet RSS feeds into a pooled story list.
2. **Picks** - Anthropic call #1 (Claude Sonnet) chooses the single hottest
   story, skipping ones it covered in the last few editions, and renders the
   headline as a short emoji glyph.
3. **Reads** the chosen story's full article body, so that you do not have to.
4. **Translates** - Anthropic call #2 (Claude Sonnet) retells the whole story
   as a tight emoji narrative: a hard 70-140 emoji, the modern attention
   span, generously rounded up.
5. **Renders** a self-contained `index.html`: a portrait-broadsheet
   newspaper - emoji masthead, lead emoji, the emoji story in newsprint
   columns - auto-sized to fit the screen with no scrolling (scrolling is a
   form of reading), and set to reload itself every 10 minutes. The page is
   100% emoji: not one word of text anywhere, printed on newsprint - the
   colour emoji desaturated to grayscale so they read as ink, and the date
   and any year drawn as a little calendar page.

Run it on a schedule (say a `*/10` cron) and you have a news page that keeps
itself current, indefinitely, with no further need for words.

## Running it

```sh
export ANTHROPIC_API_KEY=sk-ant-...     # a pay-as-you-go console key
python3 newsmoji.py
```

Pure Python standard library - no pip, no venv. One cycle makes two
Anthropic API calls (pick + translate) plus one article-page fetch. The API
key can instead live in `newsmoji.env` (a `KEY=VALUE` file) in the state
directory.

## Output & state

Everything lives in the **state directory** - `~/newsmoji/` by default,
override with `NEWSMOJI_STATE_DIR`:

| File | What |
|------|------|
| `index.html`   | the rendered page - this is the output |
| `newsmoji.log` | verbose per-cycle log, self-trimming at 5 MB |
| `history.json` | recently-covered stories, so the next pick won't repeat |
| `feeds.txt`    | optional - one RSS URL per line, replaces the default basket |

The page is self-contained: it uses the viewer's own system emoji font (no
web font to serve), so `index.html` is the only file you need to publish.

## Robustness

The page never breaks. On any failure - a feed down, an API call failing, a
bad render - the cycle aborts and the last good `index.html` is left
untouched. Worst case the page is a little stale, never broken or blank. The
public must be informed.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | (or from `newsmoji.env`) | Anthropic API key |
| `NEWSMOJI_STATE_DIR` | `~/newsmoji` | state + output directory |

## License

GPLv3 - see [`LICENSE`](LICENSE). Free as in speech, itself a legacy text
format.
