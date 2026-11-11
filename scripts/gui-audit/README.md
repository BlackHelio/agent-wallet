# GUI audit — the pane in a real browser

The pane is DOM + inline CSS. `render-check.mjs` proves the tree is shaped right; this proves it
**renders** right — at real window sizes, in the app's real themes, against the app's real compiled
CSS. It exists because three bugs shipped past every other test:

* every `<select>` was near-black on near-black in a light theme (1.09:1 — an invisible dropdown),
  because `--card` is not a token this app defines, so the dark fallback was the real value;
* every text field was the page ink on a hardcoded `#23262c` face — an empty-looking field;
* a 320 px window (a phone, a half-screen split) pushed the mandate form 9 px past the viewport.

## Run it

```sh
npm ci                       # package.json + package-lock.json are committed; nothing floats
node scripts/gui-audit/build-page.mjs          # plugin + SDK stub + fixtures -> page/
npx rollup -c scripts/gui-audit/rollup.config.mjs   # or the config you already have
(cd page && python3 -m http.server 8899 &)
node scripts/gui-audit/audit.mjs               # writes audit.json + shots/
```

`CHROME=/path/to/chrome` skips Playwright's download; `HERMES_APP_CSS` points at the app's
`main-*.css` when the desktop app lives somewhere else.

## What it measures

Per render (2 states × 2 themes × 9 viewports, 320 → 1920 px wide):

| check | why |
|---|---|
| `overflow` | the page scrolls sideways — the "works on my 1440p" bug |
| `clipped` | an element's box extends past the viewport |
| `squashed` | content wider than its box with nothing scrolling or ellipsising |
| `targets` | a control under 24×24 CSS px (WCAG 2.2 AA 2.5.8); a checkbox inside a `<label>` is measured as the label |
| `contrast` | text under 4.5:1 (3:1 for large/bold) against its effective background, including `input`/`select` ink-vs-face, which has no text node to inspect |

The dark pass is an **emulation**: the app sets its ink tokens from JS when a theme is chosen, so a
`.dark` class alone cannot reproduce one. The harness applies the same tokens the app sets. That
emulation is what caught `Canvas`/`CanvasText` staying white on a dark page — the app pins
`color-scheme: light` and never flips it.

## Keep it honest

* Don't widen a threshold to make a row pass; fix the pane.
* A passing run prints `36 renders, 0 with at least one finding`. Anything else is a regression.
* The screenshots in `shots/` are the visual check; open the 375 and 1440 ones in both themes.
