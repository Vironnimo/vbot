import { describe, expect, it } from 'vitest';
import {
  PORTAL_STATUS_CLOSED,
  PORTAL_STATUS_LOADING,
  PORTAL_STATUS_RUNNING,
  PORTAL_STATUS_UNKNOWN,
  PORTAL_STATUS_WAITING,
  portalFromTool,
  portalStatus,
  portalUrl,
  portalsReachable,
} from '$lib/chatTimelinePresentation.js';

const listed = (overrides = {}) => ({
  id: 'prt_1',
  path: '/',
  listen_port: 41001,
  listening: true,
  token: 'a+b/c=',
  ...overrides,
});

describe('portal cards', () => {
  it('reads the Portal a call opened from its display only', () => {
    expect(
      portalFromTool({
        name: 'portal',
        display: { portal: { id: 'prt_1', title: 'Shop', port: 5173 } },
      }),
    ).toEqual({ id: 'prt_1', title: 'Shop', port: 5173 });
    expect(portalFromTool({ name: 'portal', display: {} })).toBeNull();
  });

  it.each([
    [
      'the page host on the Portal port with the encoded credential',
      { protocol: 'http:', hostname: 'localhost' },
      listed(),
      'http://localhost:41001/?vbot_portal=a%2Bb%2Fc%3D',
    ],
    [
      'brackets around a bare IPv6 host',
      { protocol: 'https:', hostname: '::1' },
      listed({ path: '/app' }),
      'https://[::1]:41001/app?vbot_portal=a%2Bb%2Fc%3D',
    ],
    [
      'an IPv6 host that already has brackets unchanged',
      { protocol: 'http:', hostname: '[fe80::1]' },
      listed(),
      'http://[fe80::1]:41001/?vbot_portal=a%2Bb%2Fc%3D',
    ],
    [
      'the credential after an existing query and before a fragment',
      { protocol: 'http:', hostname: '192.168.1.5' },
      listed({ path: '/search?q=1#top' }),
      'http://192.168.1.5:41001/search?q=1&vbot_portal=a%2Bb%2Fc%3D#top',
    ],
  ])('addresses a Portal with %s', (_case, location, portal, expected) => {
    expect(portalUrl(portal, location)).toBe(expected);
  });

  it('gives no address without a credential or port', () => {
    const location = { protocol: 'http:', hostname: 'localhost' };
    expect(portalUrl(listed({ token: '' }), location)).toBe('');
    expect(portalUrl(listed({ listen_port: null }), location)).toBe('');
  });

  it('maps the Session projection to the card state', () => {
    const ready = {
      loaded: true,
      portals: {
        up: listed({ id: 'up' }),
        starting: listed({ id: 'starting', listening: false }),
      },
    };
    expect(portalStatus(null, 'up')).toBe(PORTAL_STATUS_LOADING);
    expect(portalStatus({ loaded: false, error: 'offline' }, 'up')).toBe(
      PORTAL_STATUS_UNKNOWN,
    );
    expect(portalStatus(ready, 'up')).toBe(PORTAL_STATUS_RUNNING);
    expect(portalStatus(ready, 'starting')).toBe(PORTAL_STATUS_WAITING);
    expect(portalStatus(ready, 'gone')).toBe(PORTAL_STATUS_CLOSED);
  });

  it.each([
    [false, 'vbot.example.com', true],
    [true, 'vbot.example.com', false],
    [true, 'localhost', true],
    [true, '127.0.0.1', true],
    [true, '[::1]', true],
  ])(
    'behind a reverse proxy %s, a page on %s reaches Portals: %s',
    (reverseProxy, hostname, expected) => {
      expect(portalsReachable(reverseProxy, hostname)).toBe(expected);
    },
  );
});
