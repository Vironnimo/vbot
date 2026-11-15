import {
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {
  dirname,
  extname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parseAst } from 'vite';

const webuiRoot = resolve(import.meta.dirname, '..');
const sourceRoot = resolve(webuiRoot, 'src');
export const extensionSdkRoot = resolve(webuiRoot, 'extension-sdk');
const manifest = JSON.parse(
  readFileSync(resolve(extensionSdkRoot, 'package.json'), 'utf8'),
);
const sourceExports = Object.entries(manifest.exports).filter(([, target]) =>
  target.startsWith('./dist/'),
);
// Canonical data shared by browser and server owners is packaged explicitly.
// Other imports outside the WebUI remain an error, never a directory copy.
const sharedSources = new Map([
  [
    resolve(webuiRoot, '../core/secrets/api_key_rules.json'),
    'shared/api_key_rules.json',
  ],
]);
const slash = (value) => value.split(sep).join('/');
const cssResource = /(?:@import\s*|url\(\s*)(['"]?)([^'"\s);]+)\1/g;

function imports(source, extension) {
  const references = [];
  function script(text, offset = 0) {
    function visit(node) {
      if (!node || typeof node !== 'object') return;
      if (
        [
          'ImportDeclaration',
          'ExportAllDeclaration',
          'ExportNamedDeclaration',
          'ImportExpression',
        ].includes(node.type) &&
        typeof node.source?.value === 'string'
      ) {
        references.push({
          specifier: node.source.value,
          start: offset + node.source.start,
          end: offset + node.source.end,
        });
      }
      for (const child of Object.values(node)) {
        if (Array.isArray(child)) child.forEach(visit);
        else if (child && typeof child === 'object') visit(child);
      }
    }
    visit(parseAst(text));
  }
  if (extension === '.js') script(source);
  else {
    for (const match of source.matchAll(
      /<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g,
    ))
      script(match[1], match.index + match[0].indexOf('>') + 1);
  }
  return references;
}

/** Only declared exports resolve in-tree, to keep test and app state identical. */
export function extensionSdkSourceAliases() {
  return sourceExports.map(([name, target]) => ({
    find: `${manifest.name}/${name.slice(2)}`,
    replacement: resolve(sourceRoot, target.slice('./dist/'.length)),
  }));
}

function inside(root, file) {
  const path = relative(root, file);
  return path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path);
}

/** Snapshot the public exports' closure; owners remain the original WebUI files. */
export function buildExtensionSdk({ output = extensionSdkRoot } = {}) {
  const destination = resolve(output);
  const distribution = resolve(destination, 'dist');
  // The caller chooses the package directory, never a deletion target. Only its
  // literal dist child is replaced, after the containment check.
  if (!inside(destination, distribution))
    throw new Error('Invalid SDK destination');
  rmSync(distribution, { recursive: true, force: true });
  mkdirSync(distribution, { recursive: true });
  const copied = new Set();
  const dependencies = new Set();

  function outputPath(absolute) {
    if (inside(sourceRoot, absolute))
      return resolve(distribution, relative(sourceRoot, absolute));
    const shared = sharedSources.get(absolute);
    if (shared) return resolve(distribution, shared);
    throw new Error(`SDK import escapes its declared sources: ${absolute}`);
  }

  function copySource(file) {
    const absolute = resolve(file);
    const transport = resolve(sourceRoot, 'lib/api');
    if (
      absolute === resolve(sourceRoot, 'lib/api.js') ||
      inside(transport, absolute)
    )
      throw new Error(`SDK imports application transport: ${absolute}`);
    const target = outputPath(absolute);
    if (copied.has(absolute)) return;
    copied.add(absolute);
    mkdirSync(dirname(target), { recursive: true });
    const extension = extname(absolute);
    if (!['.js', '.svelte', '.css'].includes(extension)) {
      copyFileSync(absolute, target);
      return;
    }
    let source = readFileSync(absolute, 'utf8');
    if (extension !== '.css') {
      const replacements = [];
      for (const reference of imports(source, extension)) {
        const { specifier } = reference;
        let imported;
        let published = specifier;
        if (specifier.startsWith('$lib/')) {
          imported = resolve(sourceRoot, 'lib', specifier.slice(5));
        } else if (specifier.startsWith('.')) {
          imported = resolve(dirname(absolute), specifier);
        } else {
          dependencies.add(
            specifier.startsWith('@')
              ? specifier.split('/').slice(0, 2).join('/')
              : specifier.split('/')[0],
          );
        }
        if (imported) {
          published = slash(relative(dirname(target), outputPath(imported)));
          if (!published.startsWith('.')) published = `./${published}`;
          try {
            copySource(imported);
          } catch (error) {
            throw new Error(`${error.message}\nImported by ${absolute}`, {
              cause: error,
            });
          }
        }
        if (published !== specifier)
          replacements.push({ ...reference, published });
      }
      for (const { start, end, published } of replacements.sort(
        (a, b) => b.start - a.start,
      ))
        source = `${source.slice(0, start)}${JSON.stringify(published)}${source.slice(end)}`;
    }
    if (extension === '.css' || extension === '.svelte') {
      for (const match of source.matchAll(cssResource)) {
        const resource = match[2];
        if (resource.startsWith('.'))
          copySource(resolve(dirname(absolute), resource));
      }
    }
    writeFileSync(target, source, 'utf8');
  }

  for (const [, target] of sourceExports)
    copySource(resolve(sourceRoot, target.slice('./dist/'.length)));
  const missing = [...dependencies].filter(
    (dependency) =>
      !manifest.dependencies?.[dependency] &&
      !manifest.peerDependencies?.[dependency],
  );
  if (missing.length)
    throw new Error(`SDK dependencies are not declared: ${missing.join(', ')}`);
  mkdirSync(resolve(distribution, 'fonts'), { recursive: true });
  copyFileSync(
    resolve(webuiRoot, 'public/fonts/OFL.txt'),
    resolve(distribution, 'fonts/OFL.txt'),
  );
  for (const name of ['package.json', 'build.mjs', 'README.md']) {
    const source = resolve(extensionSdkRoot, name);
    if (source !== resolve(destination, name))
      copyFileSync(source, resolve(destination, name));
  }
  if (destination !== extensionSdkRoot)
    cpSync(
      resolve(extensionSdkRoot, 'example'),
      resolve(destination, 'example'),
      { recursive: true },
    );
  copyFileSync(
    resolve(webuiRoot, '../LICENSE'),
    resolve(destination, 'LICENSE'),
  );
  return { directory: destination, files: copied.size };
}

/** Package the prepared SDK for installed applications; this never publishes. */
export function packExtensionSdk({
  directory = extensionSdkRoot,
  destination = resolve(webuiRoot, '../resources/sdk'),
} = {}) {
  const npmCli = [
    process.env.npm_execpath,
    resolve(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'),
    resolve(
      dirname(process.execPath),
      '../lib/node_modules/npm/bin/npm-cli.js',
    ),
    '/usr/share/nodejs/npm/bin/npm-cli.js',
  ].find((candidate) => candidate && existsSync(candidate));
  if (!npmCli)
    throw new Error('Build the Extension SDK from npm with Node.js installed');
  mkdirSync(destination, { recursive: true });
  const [packed] = JSON.parse(
    execFileSync(
      process.execPath,
      [
        npmCli,
        'pack',
        '--ignore-scripts',
        '--json',
        '--pack-destination',
        resolve(destination),
      ],
      { cwd: directory, encoding: 'utf8', windowsHide: true },
    ),
  );
  return resolve(destination, packed.filename);
}

if (
  process.argv[1] &&
  fileURLToPath(import.meta.url) === resolve(process.argv[1])
) {
  const result = buildExtensionSdk();
  process.stdout.write(
    `Built ${manifest.name}: ${result.files} source and asset files\n`,
  );
}
