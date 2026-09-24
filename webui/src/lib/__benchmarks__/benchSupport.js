// Shared helpers for the WebUI microbenchmarks (`*.bench.js`, run with
// `npx vitest bench --run` or `python scripts/perf_bench.py --only frontend`).
// Fixtures are deterministic so runs stay comparable.

const DEFAULT_TIME_MS = 1000;

// Benchmark names carry their size parameters after the name, e.g.
// `markdown.stream[20k] (items=200, unit=chunk, chars=20000)`. The Python
// report runner splits them off; `items`/`unit` describe what one operation
// processes so a per-item cost can be derived.
export function benchName(name, params) {
  const entries = Object.entries(params);
  if (entries.length === 0) return name;
  return `${name} (${entries.map(([key, value]) => `${key}=${value}`).join(', ')})`;
}

// `VBOT_PERF_BENCH_TIME_MS` lets the Python runner shorten or lengthen each
// benchmark's measuring time (tinybench `time`, in milliseconds).
export function benchOptions() {
  const requested = Number(process.env.VBOT_PERF_BENCH_TIME_MS);
  const time =
    Number.isFinite(requested) && requested > 0 ? requested : DEFAULT_TIME_MS;
  return { time, warmupTime: Math.min(100, time), warmupIterations: 2 };
}

// mulberry32: a small deterministic PRNG.
export function createRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

const WORDS = (
  'agent session request stream provider model tool result history token ' +
  'budget project workspace file path config value render markdown chunk ' +
  'buffer queue latency sample timing event loop worker thread writer reader ' +
  'index cache summary message answer context window output input delta ' +
  'frame payload the a of to and in for with on is are was be this that it ' +
  'from by as at update check build parse encode decode apply merge filter'
).split(' ');

const CODE_LINES = [
  'def handle(request, *, timeout=30):',
  '    payload = json.loads(request.body)',
  "    for item in payload.get('items', []):",
  '        result.append(transform(item))',
  "    return {'ok': True, 'count': len(result)}",
  'const rows = items.filter((row) => row.visible);',
  'export function renderRow(row) { return row.label; }',
];

function pick(random, values) {
  return values[Math.floor(random() * values.length)];
}

function between(random, low, high) {
  return low + Math.floor(random() * (high - low + 1));
}

export function words(random, chars) {
  const parts = [];
  let size = 0;
  while (size < chars) {
    const word = pick(random, WORDS);
    parts.push(word);
    size += word.length + 1;
  }
  return parts.join(' ').slice(0, Math.max(chars, 1));
}

export function code(random, chars) {
  const lines = [];
  let size = 0;
  while (size < chars) {
    const line = pick(random, CODE_LINES);
    lines.push(line);
    size += line.length + 1;
  }
  return lines.join('\n').slice(0, Math.max(chars, 1));
}

// Headings, paragraphs, bullet lists with inline code, and fenced code blocks.
// Blocks are whole, so the full text never ends inside an open fence.
export function markdownText(random, chars) {
  const blocks = [];
  let size = 0;
  while (size < chars) {
    const kind = random();
    let block;
    if (kind < 0.12) {
      block = `## ${words(random, between(random, 12, 40))}`;
    } else if (kind < 0.3) {
      const items = [];
      for (let index = between(random, 2, 5); index > 0; index -= 1) {
        items.push(
          `- \`${pick(random, WORDS)}\` ${words(random, between(random, 20, 70))}`,
        );
      }
      block = items.join('\n');
    } else if (kind < 0.42) {
      block = `\`\`\`python\n${code(random, between(random, 80, 320))}\n\`\`\``;
    } else {
      block = `${words(random, between(random, 120, 480))}.`;
    }
    blocks.push(block);
    size += block.length + 2;
  }
  return blocks.join('\n\n');
}
