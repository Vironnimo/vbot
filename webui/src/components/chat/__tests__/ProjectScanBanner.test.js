// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ProjectScanBanner } = await import('../ProjectScanBanner.svelte');

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

  it('renders nothing for a clean report', () => {
    mountedComponent = mount(ProjectScanBanner, {
      target: document.body,
      props: { report: { clean: true, findingCount: 0 } },
    });
    flushSync();

    expect(document.querySelector('.project-scan-banner')).toBeNull();
  });

  it('renders nothing when the report is absent', () => {
    mountedComponent = mount(ProjectScanBanner, {
      target: document.body,
      props: { report: null },
    });
    flushSync();

    expect(document.querySelector('.project-scan-banner')).toBeNull();
  });

  it('shows a non-blocking banner with a finding count for an unclean report', () => {
    mountedComponent = mount(ProjectScanBanner, {
      target: document.body,
      props: { report: { clean: false, findingCount: 3 } },
    });
    flushSync();

    const banner = document.querySelector('.project-scan-banner');
    expect(banner).not.toBeNull();
    expect(banner.textContent).toContain('3');
    // Non-blocking: it is a status region, not a modal/alert.
    expect(banner.getAttribute('role')).toBe('status');
  });

  it('invokes the navigate callback when the review link is clicked', () => {
    const onNavigateToProjects = vi.fn();
    mountedComponent = mount(ProjectScanBanner, {
      target: document.body,
      props: {
        report: { clean: false, findingCount: 1 },
        onNavigateToProjects,
      },
    });
    flushSync();

    const link = document.querySelector('.project-scan-banner__link');
    expect(link).not.toBeNull();
    link.click();
    flushSync();

    expect(onNavigateToProjects).toHaveBeenCalledTimes(1);
  });
});
