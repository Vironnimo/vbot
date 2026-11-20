// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const { installSkill, installSkillArchive } = vi.hoisted(() => ({
  installSkill: vi.fn(),
  installSkillArchive: vi.fn(),
}));
vi.mock('$lib/api.js', () => ({ installSkill, installSkillArchive }));
const { default: SkillInstallDialog } =
  await import('../SkillInstallDialog.svelte');

let component;
const onInstalled = vi.fn();
const onClose = vi.fn();
const result = (extra = {}) => ({
  operation: 'installed',
  name: 'demo',
  scope: 'agent:main',
  package_path: '.',
  sha256: 'a'.repeat(64),
  warnings: [],
  ...extra,
});
const button = (name) =>
  [...document.querySelectorAll('button')].find(
    (el) => (el.getAttribute('aria-label') || el.textContent.trim()) === name,
  );
function click(element) {
  expect(element).toBeTruthy();
  element.click();
  flushSync();
}
function input(id, value) {
  const element = document.getElementById(id);
  element.value = value;
  element.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}
async function settle() {
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}
function render(listDirectory) {
  component = mount(SkillInstallDialog, {
    target: document.body,
    context: new Map([['pathServices', { listDirectory }]]),
    props: {
      initialScope: 'agent:main',
      scopeOptions: [
        { value: 'global', label: 'Global' },
        { value: 'agent:main', label: 'Main (private)' },
      ],
      onInstalled,
      onClose,
    },
  });
  flushSync();
}
beforeEach(() => {
  init('en');
  vi.resetAllMocks();
  localStorage.clear();
  installSkill.mockResolvedValue(result());
  installSkillArchive.mockResolvedValue(result());
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('Skill installation dialog', () => {
  it('installs a link directly in the selected scope', async () => {
    render();
    const action = button(t('skills.install.action'));
    expect(action.disabled).toBe(true);
    input('skill-install-source', 'https://example.test/demo.skill');
    click(action);
    await settle();
    expect(installSkill).toHaveBeenCalledExactlyOnceWith(
      {
        source: 'https://example.test/demo.skill',
        scope: 'agent:main',
        return_choices: true,
      },
      { signal: expect.any(AbortSignal) },
    );
    expect(onInstalled).toHaveBeenCalledExactlyOnceWith([result()]);
  });

  it('installs the picked folders together, confirming only a replacement and retrying only a failed source', async () => {
    const source = 'C:/skills';
    const dirs = ['a', 'b', 'c', 'd'].map((name) => ({
      name,
      kind: 'directory',
    }));
    render(async ({ path }) => ({
      path,
      separator: '/',
      entries: path === null ? [{ name: source, kind: 'directory' }] : dirs,
    }));
    installSkill.mockImplementation(async ({ source: path, replace }) => {
      if (path.endsWith('/d') && !replace)
        return result({ operation: 'confirm_replace', name: 'd' });
      if (path.endsWith('/a') && installSkill.mock.calls.length === 1)
        throw new Error('failed-a');
      return result({
        name: path.at(-1),
        operation: replace ? 'replaced' : 'installed',
      });
    });
    click(button(t('pathPicker.browse')));
    await settle();
    click(document.querySelector('.path-browser__row'));
    await settle();
    const rows = [...document.querySelectorAll('.path-browser__row')];
    click(rows[0]);
    rows[2].dispatchEvent(
      new MouseEvent('click', { bubbles: true, shiftKey: true }),
    );
    rows[1].dispatchEvent(
      new MouseEvent('click', { bubbles: true, ctrlKey: true }),
    );
    rows[3].dispatchEvent(
      new MouseEvent('click', { bubbles: true, metaKey: true }),
    );
    flushSync();
    expect(rows.map((row) => row.getAttribute('aria-selected'))).toEqual([
      'true',
      'false',
      'true',
      'true',
    ]);
    click(button(t('pathPicker.selectCount', { count: 3 })));
    click(button(t('skills.install.batchAction', { count: 3 })));
    await settle();
    expect(installSkill.mock.calls.map(([p]) => p.source)).toEqual([
      'C:/skills/a',
      'C:/skills/c',
      'C:/skills/d',
    ]);
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      'failed-a',
    );
    expect(onInstalled).not.toHaveBeenCalled();
    click(button(t('skills.install.replaceAction')));
    await settle();
    expect(installSkill).toHaveBeenLastCalledWith(
      expect.objectContaining({
        source: 'C:/skills/d',
        path: '.',
        replace: true,
        expected_sha256: result().sha256,
      }),
      expect.any(Object),
    );
    click(button(t('common.retry')));
    await settle();
    expect(onInstalled.mock.calls[0][0].map((r) => r.name)).toEqual([
      'a',
      'c',
      'd',
    ]);
    expect(installSkill).toHaveBeenCalledTimes(5);
  });

  it('asks for a package only when the source contains multiple candidates', async () => {
    installSkill.mockResolvedValueOnce({
      operation: 'candidates',
      candidates: [
        { name: 'one', path: 'skills/one' },
        { name: 'two', path: 'skills/two' },
      ],
    });
    render();
    input('skill-install-source', 'https://example.test/repo');
    click(button(t('skills.install.action')));
    await settle();
    click(button(t('skills.install.choose')));
    await settle();
    click([...document.querySelectorAll('[role="option"]')][1]);
    await settle();
    expect(installSkill).toHaveBeenLastCalledWith(
      expect.objectContaining({ path: 'skills/two', return_choices: true }),
      expect.any(Object),
    );
    expect(onInstalled).toHaveBeenCalledOnce();
  });

  it('uploads a selected archive once and aborts outstanding work on unmount', async () => {
    installSkillArchive.mockImplementation(() => new Promise(() => {}));
    render();
    click(button(t('skills.install.fileTab')));
    const file = new File(['archive-sentinel'], 'demo.skill');
    const picker = document.querySelector('input[type="file"]');
    Object.defineProperty(picker, 'files', { value: [file] });
    picker.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    click(button(t('skills.install.action')));
    expect(installSkillArchive).toHaveBeenCalledExactlyOnceWith(
      file,
      { scope: 'agent:main', return_choices: true },
      expect.any(Object),
    );
    expect(installSkill).not.toHaveBeenCalled();
    const signal = installSkillArchive.mock.calls[0][2].signal;
    await unmount(component);
    component = null;
    expect(signal.aborted).toBe(true);
  });
});
