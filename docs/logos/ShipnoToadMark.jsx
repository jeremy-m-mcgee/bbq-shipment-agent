// ShipnoToad — badge mark + wordmark lockups.
//   <ShipnoToadMark size={40} />              full color
//   <ShipnoToadMark mono size={24} />         inherits currentColor
//   <ShipnoToadLogo layout="horizontal" />    mark + wordmark
// Wordmark type is Caprasimo; load it in the host page.

const PALETTE = { crate: '#c67139', groove: '#a8551f', orbit: '#7a8a5e', echo: '#a86fa0', ship: '#201e1d', paper: '#f5ead8' };
const DARK = { crate: '#e0a273', groove: '#a8551f', orbit: '#a3b184', echo: '#c48fbb', ship: '#f5ead8', paper: '#201e1d' };

function ShipnoToadMark({ size = 64, mono = false, theme = 'light', title = 'ShipnoToad', className, style, ...colors }) {
  const e = React.createElement;
  const c = { ...(theme === 'dark' ? DARK : PALETTE), ...colors };
  const body = mono
    ? [
        e('circle', { key: 'ring', cx: 32, cy: 32, r: 30, fill: 'none', stroke: 'currentColor', strokeWidth: 2.5 }),
        e('g', { key: 'orbit', transform: 'rotate(-20 32 34)' }, [
          e('ellipse', { key: 'e', cx: 32, cy: 34, rx: 26, ry: 10, fill: 'none', stroke: 'currentColor', strokeWidth: 2.5 }),
          e('rect', { key: 's', x: 53, y: 31.2, width: 7.5, height: 5.2, rx: 2.6, fill: 'currentColor' }),
        ]),
        e('rect', { key: 'crate', x: 16.5, y: 20.5, width: 31, height: 28, rx: 6.5, fill: 'none', stroke: 'currentColor', strokeWidth: 3 }),
        e('circle', { key: 'l', cx: 26, cy: 34.2, r: 2.8, fill: 'currentColor' }),
        e('circle', { key: 'r', cx: 38, cy: 34.2, r: 2.8, fill: 'currentColor' }),
        e('rect', { key: 'm', x: 21.5, y: 40, width: 21, height: 3, rx: 1.5, fill: 'currentColor' }),
      ]
    : [
        e('circle', { key: 'ring', cx: 32, cy: 32, r: 30, fill: 'none', stroke: c.crate, strokeWidth: 2.5 }),
        e('g', { key: 'orbit', transform: 'rotate(-20 32 34)' }, [
          e('ellipse', { key: 'e', cx: 32, cy: 34, rx: 26, ry: 10, fill: 'none', stroke: c.orbit, strokeWidth: 2.5 }),
          e('rect', { key: 's', x: 53, y: 31.2, width: 7.5, height: 5.2, rx: 2.6, fill: c.ship }),
          e('ellipse', { key: 'echo', cx: 32, cy: 34, rx: 26, ry: 10, fill: 'none', stroke: c.echo, strokeWidth: 1.4, opacity: 0.85, transform: 'rotate(-9 32 34)' }),
        ]),
        e('rect', { key: 'crate', x: 16.5, y: 20.5, width: 31, height: 28, rx: 6.5, fill: c.crate }),
        e('rect', { key: 'p1', x: 16.5, y: 27.2, width: 31, height: 1.8, fill: c.groove }),
        e('rect', { key: 'p2', x: 16.5, y: 39.5, width: 31, height: 2.5, fill: c.groove }),
        e('circle', { key: 'l', cx: 26, cy: 34.2, r: 4, fill: c.paper }),
        e('circle', { key: 'r', cx: 38, cy: 34.2, r: 4, fill: c.paper }),
        e('circle', { key: 'lp', cx: 26.7, cy: 34.4, r: 1.6, fill: c.ship }),
        e('circle', { key: 'rp', cx: 38.7, cy: 34.4, r: 1.6, fill: c.ship }),
      ];
  return e(
    'svg',
    { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 64 64', width: size, height: size, role: 'img', 'aria-label': title, className, style },
    e('title', null, title),
    body
  );
}

function ShipnoToadLogo({ layout = 'horizontal', size = 44, color = '#201e1d', mono = false, theme = 'light', style }) {
  const e = React.createElement;
  const stacked = layout === 'stacked';
  return e(
    'span',
    {
      style: {
        display: 'inline-flex',
        flexDirection: stacked ? 'column' : 'row',
        alignItems: 'center',
        gap: stacked ? size * 0.22 : size * 0.32,
        color,
        ...style,
      },
    },
    e(ShipnoToadMark, { size, mono, theme }),
    e(
      'span',
      {
        style: {
          fontFamily: 'Caprasimo, Georgia, serif',
          fontSize: size * (stacked ? 0.46 : 0.56),
          lineHeight: 1,
          letterSpacing: '-0.01em',
          whiteSpace: 'nowrap',
        },
      },
      'ShipnoToad'
    )
  );
}

if (typeof module !== 'undefined') module.exports = { ShipnoToadMark, ShipnoToadLogo, PALETTE, DARK };
if (typeof window !== 'undefined') { window.ShipnoToadMark = ShipnoToadMark; window.ShipnoToadLogo = ShipnoToadLogo; }
