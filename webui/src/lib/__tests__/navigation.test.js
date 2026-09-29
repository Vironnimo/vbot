import { describe, expect, it, vi } from 'vitest';

import {
  createNavigator,
  locationFromHash,
  locationHash,
  sessionLinkFromSearch,
} from '../navigation.svelte.js';

// A browser session history with the browser's semantics: pushState drops the
// forward entries, go() traverses asynchronously and then fires popstate, and
// going below the first entry leaves the document.
function createBrowser({ hash = '', search = '', entries = null } = {}) {
  const listeners = new Map();
  const stack = entries ?? [{ state: null, url: `/${search}${hash}` }];
  let index = stack.length - 1;
  const location = { pathname: '/', search, hash };
  const setUrl = (url) => {
    const hashAt = url.indexOf('#');
    location.hash = hashAt >= 0 ? url.slice(hashAt) : '';
    const queryAt = url.indexOf('?');
    location.search =
      queryAt >= 0 ? url.slice(queryAt, hashAt >= 0 ? hashAt : undefined) : '';
  };
  const copy = (value) => (value == null ? null : structuredClone(value));
  const browser = {
    left: false,
    location,
    stack,
    get index() {
      return index;
    },
    history: {
      get state() {
        return stack[index]?.state ?? null;
      },
      pushState: vi.fn((state, _title, url) => {
        stack.length = index + 1;
        stack.push({ state: copy(state), url });
        index += 1;
        setUrl(url);
      }),
      replaceState: vi.fn((state, _title, url) => {
        stack[index] = { state: copy(state), url: url ?? stack[index].url };
        setUrl(stack[index].url);
      }),
      go(steps) {
        Promise.resolve().then(() => {
          const target = index + steps;
          if (target < 0) {
            browser.left = true;
            return;
          }
          if (target >= stack.length || steps === 0) return;
          index = target;
          setUrl(stack[index].url);
          browser.dispatch('popstate', { state: copy(stack[index].state) });
        });
      },
      back() {
        this.go(-1);
      },
      forward() {
        this.go(1);
      },
    },
    window: {
      location,
      addEventListener: (type, listener) => {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(listener);
      },
      removeEventListener: (type, listener) =>
        listeners.get(type)?.delete(listener),
    },
    dispatch(type, event) {
      for (const listener of listeners.get(type) ?? []) listener(event);
    },
    // A reload: the same entries and position, a new page.
    reloaded() {
      return createBrowser({
        hash: location.hash,
        search: location.search,
        entries: stack.slice(),
      }).at(index);
    },
    at(position) {
      index = position;
      setUrl(stack[index].url);
      return browser;
    },
  };
  return browser;
}

function createStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

const VIEWS = ['chat', 'agents', 'skills', 'settings', 'debug'];

function setup({ browser = createBrowser(), storage, ...options } = {}) {
  const navigator = createNavigator({
    defaultView: 'chat',
    isKnownView: (view) => VIEWS.includes(view),
    browserHistory: browser.history,
    browserWindow: browser.window,
    storage: storage ?? createStorage(),
    ...options,
  });
  navigator.start();
  return { navigator, browser };
}

const settle = async () => {
  for (let round = 0; round < 6; round += 1) await Promise.resolve();
};

const shown = (navigator) => {
  const { view, place, extra } = navigator.location;
  return { view, place, extra };
};

describe('location URLs', () => {
  it('round-trips view and encoded place segments through the hash', () => {
    const location = {
      view: 'skills',
      place: ['agent:alpha', 'pkg/with space'],
      extra: null,
    };

    const hash = locationHash(location);

    expect(hash).toBe('#skills/agent:alpha/pkg%2Fwith%20space');
    expect(locationFromHash(hash)).toEqual(location);
    expect(locationFromHash('#/settings')).toEqual({
      view: 'settings',
      place: [],
      extra: null,
    });
  });

  it('takes a Session link out of the query and keeps other parameters', () => {
    expect(
      sessionLinkFromSearch(
        '?accessor=desktop&open_agent=a%40p&open_session=s',
      ),
    ).toEqual({
      target: { agentId: 'a@p', sessionId: 's' },
      search: '?accessor=desktop',
    });
    expect(sessionLinkFromSearch('?open_agent=a')).toEqual({
      target: null,
      search: '',
    });
    expect(sessionLinkFromSearch('?accessor=desktop')).toBeNull();
  });
});

describe('createNavigator', () => {
  it('starts at the hash location, or the default view for an unknown one', () => {
    const { navigator, browser } = setup({
      browser: createBrowser({ hash: '#skills/all/pkg' }),
    });
    expect(shown(navigator)).toEqual({
      view: 'skills',
      place: ['all', 'pkg'],
      extra: null,
    });
    expect(browser.stack).toHaveLength(1);

    const unknown = setup({ browser: createBrowser({ hash: '#nowhere/x' }) });
    expect(shown(unknown.navigator).view).toBe('chat');
    expect(unknown.browser.location.hash).toBe('#chat');
  });

  it('records each user step as one entry and corrections in place', () => {
    const { navigator, browser } = setup();
    const skills = navigator.view('skills');

    navigator.navigate('skills');
    skills.replace(['all']);
    skills.navigate(['all', 'pkg']);
    skills.navigate(['all', 'pkg']);

    expect(browser.stack.map((entry) => entry.url)).toEqual([
      '/#chat',
      '/#skills/all',
      '/#skills/all/pkg',
    ]);
    expect(skills.place).toEqual(['all', 'pkg']);
    expect(navigator.location.origin).toBe('view');
    expect(navigator.canGoBack).toBe(true);
    expect(navigator.canGoForward).toBe(false);
  });

  it('restores earlier and later places on Back and Forward', async () => {
    const { navigator, browser } = setup();
    navigator.navigate('agents', ['alpha']);
    navigator.navigate('settings', ['providers']);

    navigator.back();
    await settle();
    expect(shown(navigator)).toEqual({
      view: 'agents',
      place: ['alpha'],
      extra: null,
    });
    expect(navigator.location.origin).toBe('history');
    expect(navigator.canGoForward).toBe(true);

    navigator.forward();
    await settle();
    expect(shown(navigator).place).toEqual(['providers']);
    expect(browser.location.hash).toBe('#settings/providers');
  });

  it('returns to the last place of a view, or its start when already shown', () => {
    const { navigator } = setup();
    navigator.navigate('skills', ['all', 'pkg']);
    navigator.navigate('agents', ['alpha']);

    navigator.open('skills');
    expect(shown(navigator).place).toEqual(['all', 'pkg']);

    navigator.open('skills');
    expect(shown(navigator)).toEqual({
      view: 'skills',
      place: [],
      extra: null,
    });
  });

  it('goes up through Back when the parent is the previous entry', async () => {
    const { navigator, browser } = setup();
    const skills = navigator.view('skills');
    navigator.navigate('skills', ['all']);
    skills.navigate(['all', 'pkg']);

    skills.up(['all']);
    await settle();

    expect(shown(navigator).place).toEqual(['all']);
    expect(browser.stack).toHaveLength(3);
    expect(navigator.canGoForward).toBe(true);

    // Without that parent entry, going up is a new step.
    navigator.navigate('skills', ['shared', 'other']);
    skills.up(['shared']);
    expect(browser.stack.at(-1).url).toBe('/#skills/shared');
  });

  it('closes the topmost layer on Back or Forward and stays at the current place', async () => {
    const { navigator, browser } = setup();
    navigator.navigate('agents', ['alpha']);
    const below = vi.fn();
    const top = vi.fn();
    const releaseBelow = navigator.registerLayer({ close: below });
    const releaseTop = navigator.registerLayer({ close: top });

    browser.history.back();
    await settle();

    expect(top).toHaveBeenCalledTimes(1);
    expect(below).not.toHaveBeenCalled();
    expect(shown(navigator).view).toBe('agents');
    expect(browser.location.hash).toBe('#agents/alpha');
    expect(browser.index).toBe(1);

    // The app's own Back (keys, mouse buttons, the Desktop button) closes it
    // directly, also where no entry lies behind.
    expect(navigator.forward()).toBe(true);
    expect(top).toHaveBeenCalledTimes(2);
    expect(browser.index).toBe(1);

    releaseTop();
    releaseBelow();
    navigator.back();
    await settle();
    expect(shown(navigator).view).toBe('chat');
  });

  it('runs every navigation through the gate and applies Back once it opens', async () => {
    let held = null;
    const gate = vi.fn((action) => {
      if (held === false) return action();
      held = action;
      return false;
    });
    const { navigator, browser } = setup({ gate });
    held = false;
    navigator.navigate('agents', ['alpha']);
    held = null;

    browser.history.back();
    await settle();
    // Pending edits hold the view; its corrections cannot touch the entry.
    expect(shown(navigator).view).toBe('agents');
    expect(navigator.view('agents').replace(['beta'])).toBe(false);

    held();
    expect(shown(navigator).view).toBe('chat');
    expect(browser.location.hash).toBe('#chat');
    expect(gate).toHaveBeenCalledTimes(2);
  });

  it('keeps the Desktop app open when Back reaches its first entry', async () => {
    const { navigator, browser } = setup({ guardExit: true });
    expect(navigator.canGoBack).toBe(false);
    navigator.navigate('agents', ['alpha']);

    navigator.back();
    await settle();
    expect(navigator.back()).toBe(false);

    browser.history.back();
    await settle();

    expect(browser.left).toBe(false);
    expect(shown(navigator).view).toBe('chat');
    expect(browser.location.hash).toBe('#chat');
    expect(navigator.canGoForward).toBe(true);
  });

  it('moves with Alt+Arrow keys and mouse side buttons when handling input', async () => {
    const { navigator, browser } = setup({ handleInput: true });
    navigator.navigate('agents', ['alpha']);
    const key = {
      key: 'ArrowLeft',
      altKey: true,
      defaultPrevented: false,
      preventDefault: vi.fn(),
    };

    browser.dispatch('keydown', key);
    await settle();
    expect(key.preventDefault).toHaveBeenCalled();
    expect(shown(navigator).view).toBe('chat');

    const press = (type) => ({ type, button: 4, preventDefault: vi.fn() });
    const down = press('mousedown');
    browser.dispatch('mousedown', down);
    browser.dispatch('mouseup', press('mouseup'));
    await settle();
    expect(down.preventDefault).toHaveBeenCalled();
    expect(shown(navigator).view).toBe('agents');
  });

  it('keeps a startup Extension page link until the page catalog loads', () => {
    const known = new Set(VIEWS);
    const route = 'extension:swarm:swarms';
    const { navigator, browser } = setup({
      browser: createBrowser({ hash: `#${route}/swarms/s1` }),
      isKnownView: (view) => known.has(view),
    });

    expect(shown(navigator).view).toBe('chat');
    expect(browser.location.hash).toBe(`#${route}/swarms/s1`);

    known.add(route);
    expect(navigator.resolvePendingStart()).toBe(true);
    expect(shown(navigator)).toEqual({
      view: route,
      place: ['swarms', 's1'],
      extra: null,
    });
    expect(browser.stack).toHaveLength(1);
  });

  it('forgets a pending startup link once the user navigates', () => {
    const known = new Set(VIEWS);
    const route = 'extension:swarm:swarms';
    const { navigator, browser } = setup({
      browser: createBrowser({ hash: `#${route}` }),
      isKnownView: (view) => known.has(view),
    });

    navigator.navigate('settings');
    known.add(route);

    expect(navigator.resolvePendingStart()).toBe(false);
    expect(shown(navigator).view).toBe('settings');
    expect(browser.location.hash).toBe('#settings');
  });

  it('restores the place, extra state and forward entries after a reload, not in a new document', async () => {
    const storage = createStorage();
    const first = setup({ storage });
    first.navigator.navigate('chat', ['alpha', 's1'], {
      extra: { subAgent: true },
    });
    first.navigator.navigate('agents', ['alpha']);
    first.navigator.back();
    await settle();

    const { navigator } = setup({
      browser: first.browser.reloaded(),
      storage,
    });

    expect(shown(navigator)).toEqual({
      view: 'chat',
      place: ['alpha', 's1'],
      extra: { subAgent: true },
    });
    expect(navigator.canGoForward).toBe(true);

    // Opening the app again in the same tab starts a new stack.
    const opened = setup({ storage });
    expect(opened.navigator.canGoBack).toBe(false);
    expect(opened.navigator.canGoForward).toBe(false);
  });

  it('maps unavailable and renamed destinations when restoring', async () => {
    const renames = new Map();
    const { navigator } = setup({
      resolveView: (view) => (view === 'debug' ? 'settings' : view),
      remap: (location) => ({
        ...location,
        place: location.place.map((part) => renames.get(part) ?? part),
      }),
    });
    navigator.navigate('agents', ['alpha']);
    navigator.navigate('debug', ['trace-1']);
    expect(shown(navigator)).toEqual({
      view: 'settings',
      place: [],
      extra: null,
    });

    renames.set('alpha', 'alpha-2');
    navigator.remapAll();
    navigator.back();
    await settle();
    expect(shown(navigator).place).toEqual(['alpha-2']);
  });

  it('removes the Session link parameters from the address bar', () => {
    const browser = createBrowser({
      hash: '#chat',
      search: '?accessor=desktop&open_agent=alpha&open_session=s1',
    });
    const { navigator } = setup({ browser });

    expect(navigator.takeSessionLink()).toEqual({
      agentId: 'alpha',
      sessionId: 's1',
    });
    expect(browser.location.search).toBe('?accessor=desktop');
    expect(browser.stack).toHaveLength(1);
    expect(navigator.takeSessionLink()).toBeNull();
  });
});
