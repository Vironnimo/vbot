// The icon of each Session kind, as data ({ viewBox, attrs, shapes }): the
// row's marker and its details card draw the same shapes, so the card teaches
// what the marker means.

const LINE = {
  fill: 'none',
  stroke: 'currentColor',
  'stroke-width': '1.4',
  'stroke-linecap': 'round',
  'stroke-linejoin': 'round',
};

const lineIcon = (shapes) => ({ viewBox: '0 0 14 14', attrs: LINE, shapes });

const TELEGRAM = {
  viewBox: '0 0 18 18',
  attrs: { fill: 'currentColor' },
  shapes: [
    [
      'path',
      {
        d: 'M15.36 3.27c.39-.15.77.2.67.61l-1.94 9.14c-.07.34-.45.5-.74.31l-3.16-2.13-1.62 1.57c-.22.22-.6.11-.67-.2l-.52-2.41 6.72-5.91c.14-.12-.04-.35-.2-.24L5.6 9.04 2.5 7.8c-.34-.13-.35-.6-.02-.75l12.88-3.78z',
      },
    ],
  ],
};

const DISCORD = {
  viewBox: '0 0 16 16',
  attrs: { ...LINE, 'stroke-width': '1.45' },
  shapes: [
    [
      'path',
      {
        d: 'M4.1 4.1a9.5 9.5 0 0 1 7.8 0c1.15 1.75 1.7 3.7 1.55 5.8a8.8 8.8 0 0 1-2.4 1.25l-.75-1.05',
      },
    ],
    [
      'path',
      {
        d: 'M5.7 10.1l-.75 1.05a8.8 8.8 0 0 1-2.4-1.25C2.4 7.8 2.95 5.85 4.1 4.1',
      },
    ],
    ['path', { d: 'M5.25 5.05a7.7 7.7 0 0 1 5.5 0' }],
    [
      'circle',
      { cx: '5.8', cy: '7.7', r: '.8', fill: 'currentColor', stroke: 'none' },
    ],
    [
      'circle',
      { cx: '10.2', cy: '7.7', r: '.8', fill: 'currentColor', stroke: 'none' },
    ],
  ],
};

const CHANNEL = lineIcon([
  [
    'circle',
    { cx: '7', cy: '7', r: '1.2', fill: 'currentColor', stroke: 'none' },
  ],
  ['path', { d: 'M4.6 4.6a3.4 3.4 0 0 0 0 4.8M9.4 4.6a3.4 3.4 0 0 1 0 4.8' }],
  ['path', { d: 'M2.5 2.5a6.35 6.35 0 0 0 0 9M11.5 2.5a6.35 6.35 0 0 1 0 9' }],
]);

const KIND_ICONS = {
  subagent: lineIcon([
    ['circle', { cx: '4', cy: '3.5', r: '1.7' }],
    ['circle', { cx: '10.25', cy: '9.75', r: '1.35' }],
    ['path', { d: 'M4 5.2v2.55c0 1.1.9 2 2 2h2.9' }],
  ]),
  fork: lineIcon([
    ['circle', { cx: '3.25', cy: '3', r: '1.25' }],
    ['circle', { cx: '3.25', cy: '11', r: '1.25' }],
    ['circle', { cx: '10.5', cy: '3.75', r: '1.25' }],
    ['path', { d: 'M3.25 4.25v5.5M4.5 7.25h1.25a3.5 3.5 0 0 0 3.5-3.5' }],
  ]),
  cron: lineIcon([
    ['circle', { cx: '7', cy: '7', r: '4.75' }],
    ['path', { d: 'M7 4.25v3.1l2.15 1.2' }],
  ]),
};

// Every reflection kind shares the circular arrow with a spark.
const REFLECTION = lineIcon([
  ['path', { d: 'M10.9 6.9A4.1 4.1 0 1 1 9.65 4' }],
  ['path', { d: 'M9.65 1.9V4h-2.1' }],
  ['path', { d: 'M11.1 1.7v2.2M10 2.8h2.2' }],
]);

// `kind` is one entry of sessionKinds().
export function sessionKindIcon({ kind, platform }) {
  if (platform === 'telegram') return TELEGRAM;
  if (platform === 'discord') return DISCORD;
  if (platform) return CHANNEL;
  return KIND_ICONS[kind] ?? REFLECTION;
}
