// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../../lib/i18n.js';
import { rpcBackedApiMock } from '../../__tests__/apiMock.js';
import {
  filterSkills,
  skillInstructionBody,
  skillSourceLabel,
} from '../skillsView.js';
const rpcMock = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));
const { default: SkillsView } = await import('../SkillsView.svelte');

const entry = (id, name, extra = {}) => ({
  id,
  name,
  description: `Purpose of ${name}`,
  origin: 'agent',
  owner_id: 'main',
  editable_scope: 'agent:main',
  shared: false,
  shared_with: [],
  disabled: false,
  status: 'available',
  missing: [],
  optional_missing: [],
  warnings: [],
  ...extra,
});
const base = () => [
  entry('bundled', 'teach', {
    origin: 'bundled',
    owner_id: null,
    editable_scope: null,
  }),
  entry('private', 'deploy'),
  entry('shared', 'notes', { shared: true, shared_with: ['reviewer'] }),
  entry('disabled', 'broken', {
    origin: 'global',
    owner_id: null,
    editable_scope: 'global',
    status: 'disabled',
    disabled: true,
    warnings: ['diagnostic-sentinel'],
  }),
];
const button = (name, root = document.body) =>
  [...root.querySelectorAll('button')].find(
    (el) => (el.getAttribute('aria-label') || el.textContent.trim()) === name,
  );
const click = (el) => {
  expect(el).toBeTruthy();
  el.click();
  flushSync();
};
const input = (el, value) => {
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
};
const rows = () => [...document.querySelectorAll('[data-skill-id]')];
const choose = (id) => click(document.querySelector(`[data-skill-id="${id}"]`));
const collection = (name) =>
  click(
    [...document.querySelectorAll('.skills-collection')].find(
      (el) => el.querySelector('.skills-collection-name').textContent === name,
    ),
  );
async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

let component, inventory, agents;
beforeEach(() => {
  init('en');
  document.body.innerHTML = '';
  inventory = base();
  agents = [
    { id: 'main', name: 'Main' },
    { id: 'reviewer', name: 'Reviewer' },
  ];
  rpcMock.mockReset();
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'skill.inventory')
      return { skills: inventory, stale_shared: [], policy_diagnostics: [] };
    if (method === 'agent.list') return { agents };
    if (method === 'skill.inspect')
      return {
        id: params.id,
        content: `# Instructions\n\ncontent-${params.id}`,
      };
    return {};
  });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});
async function render() {
  component = mount(SkillsView, {
    target: document.body,
    props: { settings: {}, skillsRefreshToken: 0 },
  });
  flushSync();
  await settle();
}

describe('Skills manager', () => {
  it('shows direct actions and exposes descriptions only through hover or focus', async () => {
    await render();
    expect(rows()).toHaveLength(4);
    expect(document.querySelector('.skills-row-description')).toBeNull();
    for (const row of rows()) {
      expect(row.closest('.skills-row').textContent).not.toContain(
        'Purpose of',
      );
      expect(row.querySelector('button')).toBeNull();
      expect(
        row.closest('.skills-row').querySelector('[role="switch"]'),
      ).toBeTruthy();
    }
    const row = document.querySelector('[data-skill-id="private"]');
    row.dispatchEvent(new MouseEvent('pointerenter'));
    await new Promise((resolve) => setTimeout(resolve, 180));
    expect(document.querySelector('.app-tooltip--visible').textContent).toBe(
      'Purpose of deploy',
    );
    row.dispatchEvent(new MouseEvent('pointerleave'));
    expect(document.querySelector('.app-tooltip--visible')).toBeNull();
    row.focus();
    await new Promise((resolve) => setTimeout(resolve, 180));
    expect(document.querySelector('.app-tooltip--visible').textContent).toBe(
      'Purpose of deploy',
    );
    row.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    expect(document.querySelector('.app-tooltip--visible')).toBeNull();
    expect(button('Share deploy')).toBeTruthy();
    expect(button('Delete deploy')).toBeTruthy();
    expect(button('Share teach')).toBeUndefined();
    expect(button('Delete teach')).toBeUndefined();
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'skill.inspect'),
    ).toBe(false);
  });
  it('bounds 750 skills to 50 rows, reaches the last page, and searches the whole collection', async () => {
    agents = Array.from({ length: 15 }, (_, i) => ({
      id: `agent-${i}`,
      name: `Agent ${i}`,
    }));
    inventory = agents.flatMap((agent, i) =>
      Array.from({ length: 50 }, (_, j) =>
        entry(`id-${i}-${j}`, `skill-${String(i * 50 + j).padStart(3, '0')}`, {
          owner_id: agent.id,
          editable_scope: `agent:${agent.id}`,
        }),
      ),
    );
    await render();
    expect(rows()).toHaveLength(50);
    for (let i = 0; i < 14; i++) click(button('Next page'));
    expect(rows().at(-1).dataset.skillId).toBe('id-14-49');
    expect(button('Next page').disabled).toBe(true);
    input(document.querySelector('input[type="search"]'), 'skill-749');
    expect(rows()).toHaveLength(1);
    expect(rows()[0].dataset.skillId).toBe('id-14-49');
    input(document.querySelector('input[type="search"]'), '');
    collection('Agent 14');
    expect(rows()).toHaveLength(50);
    expect(
      rows().every((row) => row.dataset.skillId.startsWith('id-14-')),
    ).toBe(true);
  });
  it('keeps shared originals with their owner and also finds them in Shared skills', async () => {
    await render();
    collection('Main');
    expect(rows().map((row) => row.dataset.skillId)).toEqual([
      'private',
      'shared',
    ]);
    collection('Shared skills');
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['shared']);
    collection('Reviewer');
    expect(rows()).toHaveLength(0);
  });
  it('opens the exact duplicate-name package and rejects late detail responses', async () => {
    inventory = [
      entry('first', 'duplicate'),
      entry('second', 'duplicate', {
        origin: 'project:Second',
        owner_id: null,
        editable_scope: null,
      }),
    ];
    let finish;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'skill.inventory') return { skills: inventory };
      if (method === 'agent.list') return { agents };
      if (method === 'skill.inspect' && params.id === 'first')
        return new Promise((resolve) => {
          finish = resolve;
        });
      return { id: params.id, content: 'second-sentinel' };
    });
    await render();
    choose('first');
    await settle();
    choose('second');
    await settle();
    finish({ id: 'first', content: 'stale-sentinel' });
    await settle();
    expect(document.querySelector('.skills-content').textContent).toContain(
      'second-sentinel',
    );
    expect(document.querySelector('.skills-content').textContent).not.toContain(
      'stale-sentinel',
    );
    expect(button('Edit instructions')).toBeUndefined();
    expect(rpcMock).toHaveBeenCalledWith('skill.inspect', { id: 'second' });
  });
  it('edits a shared original using its owner scope', async () => {
    await render();
    choose('shared');
    await settle();
    click(button('Edit instructions'));
    const dialog = document.querySelector('[role="dialog"]');
    input(dialog.querySelector('textarea'), 'updated-content-sentinel');
    click(button('Save', dialog));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.update', {
      scope: 'agent:main',
      name: 'notes',
      content: 'updated-content-sentinel',
    });
  });
  it('allows stopping sharing by deselecting all recipients', async () => {
    await render();
    click(button('Share notes'));
    const dialog = document.querySelector('[role="dialog"]');
    click(dialog.querySelector('[role="switch"]'));
    click(button('Save', dialog));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.share', {
      agent_id: 'main',
      name: 'notes',
      shared: false,
      receivers: [],
    });
  });
  it('shares a private original with the selected receiver only', async () => {
    await render();
    click(button('Share deploy'));
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.querySelectorAll('[role="switch"]')).toHaveLength(1);
    click(dialog.querySelector('[role="switch"]'));
    click(button('Save', dialog));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.share', {
      agent_id: 'main',
      name: 'deploy',
      shared: true,
      receivers: ['reviewer'],
    });
  });
  it('creates in the selected Agent scope and retains draft content during inventory refresh', async () => {
    await render();
    collection('Main');
    click(button('+ Add skills'));
    click(button('Create a custom skill…'));
    const dialog = document.querySelector('[role="dialog"]');
    input(dialog.querySelector('#new-skill-name'), 'new-sentinel');
    input(dialog.querySelector('#new-skill-description'), 'Use for: reports');
    input(dialog.querySelector('#new-skill-content'), 'draft-sentinel');
    // Resource refresh uses the same workflow as Refresh; an open draft stays mounted.
    click(button('Refresh'));
    await settle();
    expect(dialog.querySelector('textarea').value).toBe('draft-sentinel');
    click(button('Create skill', dialog));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.create', {
      scope: 'agent:main',
      name: 'new-sentinel',
      content:
        '---\nname: "new-sentinel"\ndescription: "Use for: reports"\n---\n\ndraft-sentinel',
    });
  });
  it('disables directly from the list using the master policy and reconciles all same-name copies', async () => {
    inventory.push(
      entry('duplicate', 'deploy', {
        owner_id: null,
        origin: 'bundled',
        editable_scope: null,
      }),
    );
    await render();
    let finish;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'skill.set_disabled')
        return new Promise((resolve) => {
          finish = () => {
            inventory = inventory.map((item) =>
              item.name === params.name
                ? { ...item, disabled: true, status: 'disabled' }
                : item,
            );
            resolve({});
          };
        });
      if (method === 'skill.inventory') return { skills: inventory };
      return {};
    });
    const toggle = document
      .querySelector('[data-skill-id="private"]')
      .closest('.skills-row')
      .querySelector('[role="switch"]');
    click(toggle);
    expect(toggle.disabled).toBe(true);
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    toggle.click();
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'skill.set_disabled'),
    ).toHaveLength(1);
    finish();
    await settle();
    for (const id of ['private', 'duplicate']) {
      const control = document
        .querySelector(`[data-skill-id="${id}"]`)
        .closest('.skills-row')
        .querySelector('[role="switch"]');
      expect(control.getAttribute('aria-checked')).toBe('false');
      expect(control.disabled).toBe(false);
    }
    expect(rpcMock).toHaveBeenCalledWith('skill.set_disabled', {
      name: 'deploy',
      disabled: true,
    });
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'skill.inspect'),
    ).toBe(false);
  });
  it('retains authoritative state and restores actions when disabling fails', async () => {
    await render();
    rpcMock.mockRejectedValueOnce(new Error('mutation-sentinel'));
    const toggle = document
      .querySelector('[data-skill-id="private"]')
      .closest('.skills-row')
      .querySelector('[role="switch"]');
    click(toggle);
    await settle();
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(toggle.disabled).toBe(false);
    expect(document.querySelector('.skills-detail')).toBeNull();
  });
  it('re-enables a disabled Skill directly from the list', async () => {
    await render();
    click(
      document
        .querySelector('[data-skill-id="disabled"]')
        .closest('.skills-row')
        .querySelector('[role="switch"]'),
    );
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.set_disabled', {
      name: 'broken',
      disabled: false,
    });
  });
  it('keeps the selected Skill actions above its scrolling content', async () => {
    await render();
    choose('private');
    await settle();
    const detail = document.querySelector('.skills-detail');
    expect(button('Delete deploy', detail)).toBeTruthy();
    expect(button('Share deploy', detail)).toBeTruthy();
    expect(
      detail.querySelector('.skills-detail-scroll .skills-actions'),
    ).toBeNull();
    expect(detail.querySelector('.skills-description')).toBeNull();
    expect(detail.querySelector('.skills-management')).toBeNull();
  });
  it('opens folder setup from the primary action without showing an authoring form', async () => {
    await render();
    click(button('+ Add skills'));
    const directories = document.querySelector('.skills-directories');
    expect(directories.hidden).toBe(false);
    expect(directories.querySelector('input:not([readonly])')).toBeTruthy();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(
      button('Create a custom skill…').classList.contains('btn-tertiary'),
    ).toBe(true);
    click(button('Back to list'));
    expect(rows()).toHaveLength(4);
  });
  it('refreshes the list immediately after connecting a Skill folder', async () => {
    await render();
    click(button('+ Add skills'));
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'settings.update') {
        inventory = [
          ...inventory,
          entry('folder-sentinel', 'folder-skill', {
            origin: 'global',
            owner_id: null,
            editable_scope: null,
          }),
        ];
        return { skills: { directories: params.skills.directories } };
      }
      if (method === 'skill.inventory') return { skills: inventory };
      return {};
    });
    input(
      document.querySelector('.skills-directory-add input'),
      '/skills/folder-sentinel',
    );
    click(button('Add directory'));
    click(button('Save'));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      skills: { directories: ['/skills/folder-sentinel'] },
    });
    click(button('Back to list'));
    expect(rows().map((row) => row.dataset.skillId)).toContain(
      'folder-sentinel',
    );
    expect(button('Delete folder-skill')).toBeUndefined();
  });
  it('confirms deletion directly from the owner row without inspection', async () => {
    await render();
    click(button('Delete notes'));
    const dialog = document.querySelector('[role="dialog"]');
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'skill.inspect'),
    ).toBe(false);
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'skill.delete'),
    ).toBe(false);
    click(button('Delete', dialog));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.delete', {
      scope: 'agent:main',
      name: 'notes',
    });
  });
  it('returns focus to the selected row and switches content tabs with the keyboard', async () => {
    await render();
    choose('private');
    await settle();
    expect(document.activeElement).toBe(
      document.querySelector('.skills-detail'),
    );
    const tab = document.querySelector('[role="tab"]');
    tab.focus();
    tab.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }),
    );
    flushSync();
    expect(
      document
        .querySelector('[role="tab"][aria-selected="true"]')
        .textContent.trim(),
    ).toBe('Original text');
    click(button('Back to list'));
    await settle();
    expect(document.activeElement.dataset.skillId).toBe('private');
  });
  it('keeps the inventory visible after refresh failure and supports retry', async () => {
    await render();
    rpcMock.mockRejectedValueOnce(new Error('refresh-sentinel'));
    // Fail the inventory call specifically (Refresh also loads the roster).
    rpcMock.mockImplementation(async (method) => {
      if (method === 'skill.inventory') throw new Error('refresh-sentinel');
      return { agents };
    });
    click(button('Refresh'));
    await settle();
    expect(rows()).toHaveLength(4);
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(button('Retry')).toBeTruthy();
  });
  it('shows a retryable content error without opening an empty editor', async () => {
    await render();
    rpcMock.mockImplementation(async (method) => {
      if (method === 'skill.inspect') throw new Error('inspect-sentinel');
      return {};
    });
    choose('private');
    await settle();
    expect(button('Edit instructions').disabled).toBe(true);
    expect(
      document.querySelector('.skills-content [role="alert"]').textContent,
    ).toContain('inspect-sentinel');
  });
});

describe('skill projections', () => {
  it('combines owner words, scope and diagnostics without mistaking sharing for ownership', () => {
    const entries = base();
    expect(
      filterSkills(entries, 'main deploy', 'all', 'all', [
        { id: 'main', name: 'Main' },
      ]).map((item) => item.id),
    ).toEqual(['private']);
    expect(
      filterSkills(entries, '', 'all', 'attention').map((item) => item.id),
    ).toEqual(['disabled']);
    expect(filterSkills(entries, '', 'agent:reviewer')).toEqual([]);
  });
  it('labels owner, Project, and external source without presenting them as authors', () => {
    expect(
      skillSourceLabel(entry('a', 'a'), t, [
        { id: 'main', name: 'Owner sentinel' },
      ]),
    ).toBe('Agent: Owner sentinel');
    expect(
      skillSourceLabel(
        entry('b', 'b', { owner_id: null, origin: 'project:Repo sentinel' }),
        t,
        [],
      ),
    ).toBe('Project: Repo sentinel');
    expect(
      skillSourceLabel(
        entry('c', 'c', {
          owner_id: null,
          origin: 'global',
          source_label: 'Folder sentinel',
        }),
        t,
        [],
      ),
    ).toBe('Source: Folder sentinel');
  });
  it('omits only a complete frontmatter block for presentation', () => {
    expect(skillInstructionBody('---\nname: x\n---\n# Body')).toBe('# Body');
    expect(skillInstructionBody('---\nno-closing-marker')).toBe(
      '---\nno-closing-marker',
    );
  });
});
