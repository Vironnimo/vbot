// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ProjectScanBanner } =
  await import('../ProjectScanBanner.svelte');

describe('ProjectScanBanner', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  function mountBanner(props) {
    mountedComponent = mount(ProjectScanBanner, {
      target: document.body,
      props,
    });
    flushSync();
    return document.querySelector('.project-scan-banner');
  }

  it.each([
    ['a clean report', { clean: true, findingCount: 0 }],
    ['an absent report', null],
  ])('renders nothing for %s', (_case, report) => {
    expect(mountBanner({ report })).toBeNull();
  });

  it.each([
    [
      'with its finding count',
      { clean: false, findingCount: 3 },
      t('chat.project.scanBannerCount', { count: 3 }),
    ],
    ['without a finding count', { clean: false }, t('chat.project.scanBanner')],
  ])(
    'shows a non-blocking banner for an unclean report %s',
    (_case, report, message) => {
      const onNavigateToProjects = vi.fn();
      const banner = mountBanner({ report, onNavigateToProjects });

      expect(
        banner.querySelector('.project-scan-banner__message').textContent,
      ).toBe(message);
      // Non-blocking: it is a status region, not a modal or alert.
      expect(banner.getAttribute('role')).toBe('status');

      const link = banner.querySelector('.project-scan-banner__link');
      expect(link.textContent.trim()).toBe(t('chat.project.scanBannerLink'));
      link.click();
      expect(onNavigateToProjects).toHaveBeenCalledTimes(1);
    },
  );
});
