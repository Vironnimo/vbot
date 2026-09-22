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
const preview = (extra = {}) => ({
  operation: 'preview',
  name: 'demo',
  scope: 'agent:main',
  package_path: '.',
  files: 3,
  sha256: 'a'.repeat(64),
  warnings: [],
  candidates: [
    {
      name: 'demo',
      description: 'description-sentinel',
      path: '.',
      exists: false,
      unchanged: false,
    },
  ],
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
function render() {
  component = mount(SkillInstallDialog, {
    target: document.body,
    props: {
      initialScope: 'agent:main',
      scopeOptions: [
        { value: 'global', label: 'Global' },
        { value: 'agent:main', label: 'Main (private)' },
      ],
      onInstalled,
      onClose,
      onLocations: vi.fn(),
      onCreate: vi.fn(),
    },
  });
  flushSync();
}
beforeEach(() => {
  init('en');
  vi.clearAllMocks();
  installSkill.mockResolvedValue(preview());
  installSkillArchive.mockResolvedValue(preview());
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

describe('Skill installation dialog', () => {
  it('dismisses the destination selector without discarding the reviewed installation', async () => {
    installSkill.mockResolvedValue(
      preview({ candidates: [{ exists: true, unchanged: false }] }),
    );
    render();
    input('skill-install-source', 'https://example.test/demo.skill');
    click(button(t('skills.install.check')));
    await settle();
    click(document.querySelector('[role="switch"]'));
    const destination = button(t('skills.install.destination'));
    click(destination);
    await settle();
    const list = document.querySelector('[role="listbox"]');
    expect(document.activeElement).toBe(list);
    list.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      }),
    );
    flushSync();
    expect(onClose).not.toHaveBeenCalled();
    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(destination);
    expect(button(t('skills.install.replaceAction')).disabled).toBe(false);
    destination.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('previews a link in the selected private scope and installs exactly the reviewed package', async () => {
    render();
    expect(button(t('skills.install.check')).disabled).toBe(true);
    input('skill-install-source', 'https://example.test/demo.skill');
    click(button(t('skills.install.check')));
    await settle();
    expect(installSkill).toHaveBeenCalledWith(
      {
        source: 'https://example.test/demo.skill',
        scope: 'agent:main',
        dry_run: true,
      },
      { signal: expect.any(AbortSignal) },
    );
    expect(
      document.querySelector('.skills-install-preview h3').textContent,
    ).toBe('demo');
    installSkill.mockResolvedValue({ ...preview(), operation: 'installed' });
    click(button(t('skills.install.action')));
    await settle();
    expect(installSkill).toHaveBeenLastCalledWith(
      {
        source: 'https://example.test/demo.skill',
        scope: 'agent:main',
        dry_run: false,
        replace: false,
        path: '.',
        expected_sha256: 'a'.repeat(64),
      },
      { signal: expect.any(AbortSignal) },
    );
    expect(onInstalled).toHaveBeenCalledWith(
      expect.objectContaining({ operation: 'installed' }),
    );
  });

  it('requires an explicit replacement choice and clears approval when inputs change', async () => {
    installSkill.mockResolvedValue(
      preview({ candidates: [{ exists: true, unchanged: false }] }),
    );
    render();
    input('skill-install-source', 'https://example.test/demo.skill');
    click(button(t('skills.install.check')));
    await settle();
    expect(button(t('skills.install.replaceAction')).disabled).toBe(true);
    click(document.querySelector('[role="switch"]'));
    expect(button(t('skills.install.replaceAction')).disabled).toBe(false);
    input('skill-install-source', 'https://example.test/changed.skill');
    expect(document.querySelector('.skills-install-preview')).toBeNull();
    click(button(t('skills.install.check')));
    await settle();
    expect(
      document.querySelector('[role="switch"]').getAttribute('aria-checked'),
    ).toBe('false');
    click(document.querySelector('[role="switch"]'));
    click(button(t('skills.install.replaceAction')));
    await settle();
    expect(installSkill).toHaveBeenLastCalledWith(
      expect.objectContaining({ replace: true, dry_run: false }),
      expect.any(Object),
    );
  });

  it('selects a package from a repository before allowing installation', async () => {
    installSkill.mockResolvedValueOnce({
      operation: 'candidates',
      candidates: [
        { name: 'one', path: 'skills/one' },
        { name: 'two', path: 'skills/two' },
      ],
    });
    render();
    input('skill-install-source', 'https://github.com/example/repo');
    click(button(t('skills.install.check')));
    await settle();
    expect(button(t('skills.install.action'))).toBeUndefined();
    click(button(t('skills.install.choose')));
    await settle();
    click([...document.querySelectorAll('[role="option"]')][1]);
    await settle();
    expect(installSkill).toHaveBeenLastCalledWith(
      expect.objectContaining({ path: 'skills/two', dry_run: true }),
      expect.any(Object),
    );
    expect(document.querySelector('.skills-install-preview')).not.toBeNull();
  });

  it('uploads the selected client file for preview and installation without a server path', async () => {
    render();
    click(button(t('skills.install.fileTab')));
    const file = new File(['archive-sentinel'], 'demo.skill', {
      type: 'application/octet-stream',
    });
    const picker = document.querySelector('input[type="file"]');
    Object.defineProperty(picker, 'files', { value: [file] });
    picker.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    click(button(t('skills.install.check')));
    await settle();
    expect(installSkillArchive).toHaveBeenCalledWith(
      file,
      { scope: 'agent:main', dry_run: true },
      expect.any(Object),
    );
    expect(installSkill).not.toHaveBeenCalled();
    click(button(t('skills.install.action')));
    await settle();
    expect(installSkillArchive).toHaveBeenLastCalledWith(
      file,
      expect.objectContaining({
        dry_run: false,
        expected_sha256: 'a'.repeat(64),
      }),
      expect.any(Object),
    );
  });

  it('retains failed input for retry and does not leave stale install approval', async () => {
    installSkill.mockRejectedValueOnce(new Error('error-sentinel'));
    render();
    input('skill-install-source', 'https://example.test/demo.skill');
    click(button(t('skills.install.check')));
    await settle();
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      'error-sentinel',
    );
    expect(document.getElementById('skill-install-source').value).toBe(
      'https://example.test/demo.skill',
    );
    expect(button(t('skills.install.check')).disabled).toBe(false);
    expect(onInstalled).not.toHaveBeenCalled();
    click(button(t('skills.install.check')));
    await settle();
    click(button(t('skills.install.destination')));
    await settle();
    click(document.querySelector('[role="option"]'));
    expect(document.querySelector('.skills-install-preview')).toBeNull();
  });

  it('disables repeated submission while checking and aborts the request when unmounted', async () => {
    installSkill.mockImplementation(() => new Promise(() => {}));
    render();
    input('skill-install-source', 'https://example.test/demo.skill');
    click(button(t('skills.install.check')));
    expect(button(t('skills.install.check')).disabled).toBe(true);
    expect(document.getElementById('skill-install-source').disabled).toBe(true);
    const signal = installSkill.mock.calls[0][1].signal;
    await unmount(component);
    component = null;
    expect(signal.aborted).toBe(true);
  });
});
