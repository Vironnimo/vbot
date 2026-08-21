// One-off analysis: rank a built chunk's sources by original size via its
// sourcemap's sourcesContent. Usage: node scripts/analyze-chunk.mjs <chunk.js.map>
import { readFileSync } from 'node:fs';

const mapPath = process.argv[2];
if (!mapPath) {
  console.error('usage: node analyze-chunk.mjs <chunk.js.map>');
  process.exit(1);
}

const map = JSON.parse(readFileSync(mapPath, 'utf8'));
const byPackage = new Map();
for (let index = 0; index < map.sources.length; index += 1) {
  const content = map.sourcesContent?.[index] ?? '';
  const source = String(map.sources[index]).replace(/^\.\.\//g, '').replace(/^\//, '');
  const match = source.match(/node_modules\/(@[^/]+\/[^/]+|[^/]+)/);
  const key = match ? `npm:${match[1]}` : source.split('?')[0];
  byPackage.set(key, (byPackage.get(key) ?? 0) + content.length);
}

const total = [...byPackage.values()].reduce((sum, value) => sum + value, 0);
console.log(`total original sources: ${(total / 1024).toFixed(0)} kB`);
console.log('--- top contributors ---');
for (const [key, value] of [...byPackage.entries()].sort((a, b) => b[1] - a[1]).slice(0, 25)) {
  console.log(`${(value / 1024).toFixed(0).padStart(6)} kB  ${key}`);
}
