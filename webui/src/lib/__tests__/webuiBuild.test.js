import { describe, expect, it, vi } from 'vitest';

import { isWebuiOutdated } from '../webuiBuild.js';

describe('isWebuiOutdated', () => {
  it.each([
    ['a page without a build id', null, 'served', false],
    ['the build the server serves', 'same', 'same', false],
    ['another build than the server serves', 'loaded', 'served', true],
    ['a server that names no build', 'loaded', null, false],
  ])('judges %s', async (_label, loaded, served, outdated) => {
    const getServedBuild = vi.fn().mockResolvedValue(served);

    await expect(isWebuiOutdated(getServedBuild, loaded)).resolves.toBe(
      outdated,
    );
    expect(getServedBuild).toHaveBeenCalledTimes(loaded === null ? 0 : 1);
  });
});
