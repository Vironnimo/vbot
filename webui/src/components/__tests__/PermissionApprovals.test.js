// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';

const resolve = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  resolvePermissionApproval: (...args) => resolve(...args),
}));
const { default: PermissionApprovals } =
  await import('../PermissionApprovals.svelte');
init('en');
let component;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.clearAllMocks();
});
function button(label) {
  return [...document.querySelectorAll('button')].find(
    (element) => element.textContent.trim() === label,
  );
}

it('reviews the complete prepared call and resolves it once', async () => {
  resolve.mockResolvedValue({ resolved: true });
  const request = {
    id: 'approval-one',
    display_redacted: true,
    agent_id: 'agent',
    project_id: 'project',
    session_id: 'session',
    tool_name: 'bash',
    execution: { computer_id: 'computer' },
    reason: 'Permission Rules',
    cwd: '/work',
    requirements: [
      { access: 'read', target: '/outside', subtree: true, filtered: true },
      {
        access: 'connect',
        target: 'example.test',
        host_aliases: ['example.test'],
      },
    ],
    arguments: { command: 'touch /outside/file' },
  };
  component = mount(PermissionApprovals, {
    target: document.body,
    props: { snapshot: { revision: 1, requests: [request] } },
  });
  flushSync();
  button(t('approvals.review')).click();
  flushSync();
  const dialog = document.querySelector('[role="dialog"]');
  for (const value of [
    'agent@project',
    'session',
    'bash',
    'computer',
    '/work',
    '/outside',
    'touch /outside/file',
  ])
    expect(dialog.textContent).toContain(value);
  expect(dialog.textContent).toContain(t('approvals.redacted'));
  expect(dialog.textContent).toContain(t('approvals.filtered'));
  expect(dialog.textContent).toContain(t('approvals.hostAliases'));
  expect(button(t('approvals.reject'))).toBeDefined();
  button(t('approvals.allowOnce')).click();
  await vi.waitFor(() =>
    expect(resolve).toHaveBeenCalledWith('approval-one', 'allow'),
  );
});
