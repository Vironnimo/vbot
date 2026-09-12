import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

// Follow the actual stylesheet entrypoint so source guards and DOM fixtures see
// the same ordered rules as the bundled application, including private imports.
export function readStyleSheet(path) {
  return readFileSync(path, 'utf8').replace(
    /@import\s+['"]([^'"]+)['"];\s*/g,
    (_, importedPath) =>
      `${readStyleSheet(resolve(dirname(path), importedPath))}\n`,
  );
}
