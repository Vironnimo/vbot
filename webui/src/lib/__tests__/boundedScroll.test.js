// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { boundedScroll } from '../boundedScroll.js';

// A box of 300 px showing content of `geometry.contentHeight` px.
function scrollBox(geometry) {
  const node = document.createElement('div');
  node.append(document.createElement('div'));
  Object.defineProperty(node, 'clientHeight', { get: () => 300 });
  Object.defineProperty(node, 'scrollHeight', {
    get: () => geometry.contentHeight,
  });
  return node;
}

describe('boundedScroll', () => {
  it('is focusable only while its content overflows', () => {
    const geometry = { contentHeight: 200 };
    const node = scrollBox(geometry);
    const action = boundedScroll(node);
    expect(node.hasAttribute('tabindex')).toBe(false);

    geometry.contentHeight = 900;
    action.update();
    expect(node.tabIndex).toBe(0);
    expect(node.hasAttribute('data-overflowing')).toBe(true);

    geometry.contentHeight = 250;
    action.update();
    expect(node.hasAttribute('tabindex')).toBe(false);
    action.destroy();
  });

  it('follows growing content until the reader scrolls away from the end', () => {
    const geometry = { contentHeight: 900 };
    const node = scrollBox(geometry);
    const action = boundedScroll(node, { follow: true });
    expect(node.scrollTop).toBe(900);

    node.scrollTop = 100;
    node.dispatchEvent(new Event('scroll'));
    geometry.contentHeight = 1200;
    action.update({ follow: true });
    expect(node.scrollTop).toBe(100);

    node.scrollTop = 900;
    node.dispatchEvent(new Event('scroll'));
    geometry.contentHeight = 1500;
    action.update({ follow: true });
    expect(node.scrollTop).toBe(1500);
    action.destroy();
  });
});
