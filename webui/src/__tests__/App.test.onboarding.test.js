// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  App,
  cleanupAppHarness,
  createOnboardingRpcMock,
  resetAppHarness,
  rpcMock,
  waitForCondition,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it('renders the first-run onboarding wizard on a fresh, unconnected install', async () => {
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: false }));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('.onboarding-view')).toBeTruthy();
    });
    // The wizard takes over the content area (no normal view rendered).
    expect(document.querySelector('.chat-view')).toBeFalsy();
  });

  it('renders the normal shell when a provider is already connected', async () => {
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: true }));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('.chat-view')).toBeTruthy();
    });
    expect(document.querySelector('.onboarding-view')).toBeFalsy();
  });

  it('keeps the wizard hidden when dismissed and offers a Finish setup re-entry', async () => {
    localStorage.setItem('vbot.onboardingDismissed', '1');
    rpcMock.mockImplementation(createOnboardingRpcMock({ connected: false }));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('.app-finish-setup')).toBeTruthy();
    });
    expect(document.querySelector('.onboarding-view')).toBeFalsy();

    const finishButton = document.querySelector('.app-finish-setup button');
    finishButton?.click();
    flushSync();

    await waitForCondition(() => {
      expect(document.querySelector('.onboarding-view')).toBeTruthy();
    });
  });
});
