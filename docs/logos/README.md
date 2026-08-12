# ShipnoToad logo

The source kit. **Nothing here is served at runtime** — the two variants the app
actually uses were copied to where they are consumed, for reasons in "How this
repo uses them" below. This folder is the master copy and the place to edit the
artwork.

## Files

| File | Use |
| --- | --- |
| `shipnotoad-badge.svg` | Full-color mark. README, social, anywhere with a known background. |
| `shipnotoad-badge-mono.svg` | One-color version; inherits `currentColor` when inlined. **Copied to `src/bbq_shipment_agent/ui/templates/_logo.svg`** as the app header mark. |
| `shipnotoad-tile.svg` | Solid-ground tile. **Inlined as the favicon** in `templates/base.html`. |
| `shipnotoad-hump.svg` | Alternate, unused. |
| `ShipnoToadMark.jsx` | React lockups. **This repo has no React** — kept as artwork reference only, and not wired to anything. |

## How this repo uses them

The web UI has no static file mount and no asset pipeline: its CSS is inline in
the templates, and its one file-serving route is keyed on an exact filename from
a globbed dict, never `dir / name`. Adding a `StaticFiles` mount for a logo
would reintroduce exactly the shape that rule refuses, on an app that serves
real home addresses. So both marks are **inlined into the HTML** instead:

- **Header** — `templates/_logo.svg` is the mono variant with its `width`/`height`
  stripped, pulled in with `{% include "_logo.svg" %}`. Inlined, `currentColor`
  resolves against `header.top .mark { color: var(--accent) }`, so one file
  serves both the light and dark palettes in `base.html`. As an `<img>` it would
  render black, and the full-colour badge would need two files — the kit lists
  separate on-dark values.
- **Favicon** — the tile variant, minified into a `data:` URI in `base.html`.
  Solid ground is what survives 16px. Note `#` must be percent-encoded as `%23`;
  unencoded it terminates the URL at the first colour and the icon silently
  fails to load.

Editing the artwork means editing here **and** re-copying to the template. Two
copies is the cost of having no build step; the alternative was a static mount.

> The Caprasimo webfont below is loaded from Google Fonts. Do not wire that into
> the app UI — it binds `127.0.0.1` with no external network assumptions, and a
> CDN request from a page showing private addresses is a leak, not a style
> choice. Use it for exported artwork only.

## Palette

crate `#c67139` · groove `#a8551f` · orbit `#7a8a5e` · echo `#a86fa0` · ink `#201e1d` · paper `#f5ead8`
On dark grounds: crate `#e0a273` · orbit `#a3b184` · echo `#c48fbb`.

## In Markdown

README and docs images are served by GitHub from the repo, not by the app, so a
relative path is all that is needed:

```markdown
<img src="docs/logos/shipnotoad-badge.svg" alt="ShipnoToad" width="40" height="40">
```

Use the full-colour badge here. The mono version only picks up `currentColor`
when the SVG is inlined in the DOM — through `<img>` it renders black.

## As a component

`ShipnoToadMark.jsx` is written for a global `React` (no imports), so it drops into a plain script tag. For a bundler, add `import React from 'react';` at the top and replace the last two lines with `export { ShipnoToadMark, ShipnoToadLogo, PALETTE, DARK };`.

```jsx
<ShipnoToadMark size={32} />                 // full color
<ShipnoToadMark size={20} mono />            // takes the parent's text color
<ShipnoToadMark size={40} theme="dark" />    // for dark grounds
<ShipnoToadLogo layout="horizontal" size={44} />
<ShipnoToadLogo layout="stacked" size={64} color="#f5ead8" theme="dark" />
```

The lockups set the wordmark in **Caprasimo** — load it in the host page:

```html
<link href="https://fonts.googleapis.com/css2?family=Caprasimo&display=swap" rel="stylesheet">
```

## Notes

- The mark is drawn on a 64×64 viewBox and scales cleanly from 16px up.
- Below 20px, prefer `shipnotoad-tile.svg` or the mono version — the orbit echo gets thin.
- Keep clear space of at least 25% of the mark's width on all sides.
