// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import { init } from '../../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const { default: DebugBody } = await import('../DebugBody.svelte');

let component;
beforeEach(() => {
  init('en');
  document.body.innerHTML = '';
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  vi.restoreAllMocks();
});
function render(body) {
  component = mount(DebugBody, { target: document.body, props: { body } });
  flushSync();
}
function tab(id) {
  document.querySelector(`[id$="-tab-${id}"]`).click();
  flushSync();
}
function find(value) {
  const input = document.querySelector('input[type="search"]');
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  return input;
}

describe('DebugBody', () => {
  it('decodes strings for reading, lazily expands nested fields and keeps raw bytes of text', () => {
    const raw =
      ' {"instructions":"First line\\nSecond line","messages":[{"role":"user","content":"<script>alert(1)</script>"}],"number":9007199254740993}  ';
    render(raw);
    expect(document.querySelector('.json-string').textContent).toBe(
      'First line\nSecond line',
    );
    expect(document.querySelector('.json-children')).toBeNull();
    const node = document.querySelector('details');
    node.open = true;
    node.dispatchEvent(new Event('toggle'));
    flushSync();
    expect(document.querySelector('.json-children')).not.toBeNull();
    expect(document.querySelector('script')).toBeNull();
    tab('raw');
    expect(document.querySelector('.body-content pre').textContent).toBe(raw);
  });

  it('finds and navigates exact matches without removing any captured content', () => {
    const raw =
      'event: delta\r\ndata: {"text":"needle"}\r\n\r\ndata: needle\n\ndata: [DONE]\n\n';
    render(raw);
    const input = find('needle');
    const pre = document.querySelector('.body-content pre');
    expect(pre.textContent).toBe(raw);
    expect(pre.querySelector('mark').previousSibling.textContent).toBe(
      raw.slice(0, raw.indexOf('needle')),
    );
    input.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();
    expect(pre.querySelector('mark').previousSibling.textContent).toBe(
      raw.slice(0, raw.lastIndexOf('needle')),
    );
    find('not present');
    expect(pre.querySelector('mark')).toBeNull();
    expect(pre.textContent).toBe(raw);
  });

  it('searches all raw fields even when their readable branches are closed', () => {
    const raw = '{"nested":{"hidden":"target"}}';
    render(raw);
    find('target');
    expect(
      document.querySelector('[id$="-tab-raw"]').getAttribute('aria-selected'),
    ).toBe('true');
    expect(document.querySelector('mark').textContent).toBe('target');
    expect(document.querySelector('.body-content pre').textContent).toBe(raw);
  });

  it('copies the original body from every representation', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const raw = '{ "a" : "one\\ntwo" }';
    render(raw);
    document.querySelector('[aria-label="Copy raw body"]').click();
    await Promise.resolve();
    expect(writeText).toHaveBeenLastCalledWith(raw);
    tab('formatted');
    document.querySelector('.copy-button').click();
    await Promise.resolve();
    expect(writeText).toHaveBeenLastCalledWith(raw);
  });

  it('preserves non-JSON and WebSocket aggregates and supports wrapping without rewriting them', () => {
    const raw = '{"type":"delta"}\n{"type":"done"}\n';
    render(raw);
    expect(document.querySelector('[role="tablist"]')).toBeNull();
    const pre = document.querySelector('.body-content pre');
    expect(pre.textContent).toBe(raw);
    document.querySelector('[role="switch"]').click();
    flushSync();
    expect(pre.classList.contains('body-wrapped')).toBe(false);
    expect(pre.textContent).toBe(raw);
  });

  it('can reach fields beyond the initial rendering batch', () => {
    render(JSON.stringify(Array.from({ length: 55 }, (_, i) => `field-${i}`)));
    expect(document.querySelectorAll('.json-leaf')).toHaveLength(50);
    document.querySelector('.body-content button').click();
    flushSync();
    expect(document.querySelectorAll('.json-leaf')).toHaveLength(55);
    expect(document.querySelector('.body-content').textContent).toContain(
      'field-54',
    );
  });
});
