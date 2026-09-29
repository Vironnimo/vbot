// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { SvelteMap } from 'svelte/reactivity';
import { fileURLToPath } from 'node:url';
import { readStyleSheet } from '../../../__tests__/styles.support.js';
import { init, t } from '../../../lib/i18n.js';
import { createStandaloneNavigation } from '../../../lib/navigation.svelte.js';
import { rpcBackedApiMock } from '../../__tests__/apiMock.support.js';
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
vi.mock(
  'svelte/reactivity',
  async () =>
    import('../../../../node_modules/svelte/src/reactivity/index-client.js'),
);
const { default: SkillsView } = await import('../SkillsView.svelte');

const entry = (id, name, extra = {}) => ({
  id,
  name,
  description: `Purpose of ${name}`,
  origin: 'agent',
  owner_id: 'main',
  project_id: null,
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
const grant = (name, packageId, kind, own = kind === 'own') => ({
  name,
  package_id: packageId,
  own,
  grant: kind,
  available: true,
});
const agent = (id, name, allowed, skills) => ({
  id,
  name,
  root_project_id: null,
  allowed_skills: allowed,
  excluded_skills: [],
  mode: allowed.includes('*') ? 'all' : 'selected',
  skills,
});
// Main owns deploy and notes (shared with Reviewer, whose selection does not
// allow it); both Agents allow the bundled teach, which Repo does not activate.
const baseAgents = () => [
  agent(
    'main',
    'Main',
    ['*'],
    [
      grant('deploy', 'private', 'own'),
      grant('notes', 'shared', 'own'),
      grant('teach', 'bundled', 'allowed'),
    ],
  ),
  agent(
    'reviewer',
    'Reviewer',
    ['teach'],
    [
      grant('notes', 'shared', 'not_selected'),
      grant('teach', 'bundled', 'allowed'),
    ],
  ),
];
const baseProjects = () => [
  {
    project_id: 'repo',
    name: 'Repo',
    skills_project_disabled: [],
    skills_global_enabled: [],
    skills_bundled_enabled: [],
    skills: [
      {
        name: 'teach',
        package_id: 'bundled',
        source: 'bundled',
        active: false,
      },
    ],
  },
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
const key = (el, name) => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true }));
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
const texts = (selector, root = document) =>
  [...root.querySelectorAll(selector)].map((el) => el.textContent.trim());
const calls = (method) =>
  rpcMock.mock.calls.filter(([name]) => name === method).map(([, p]) => p);
async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
}

let component, inventory, agents, projects;
const onToast = vi.fn();
const refreshState = new SvelteMap();
function notifySkillsChanged() {
  flushSync(() => refreshState.set('token', refreshState.get('token') + 1));
}
const snapshot = () => ({
  skills: inventory,
  agents,
  projects,
  stale_shared: [],
  policy_diagnostics: [],
});
function defaultRpc(method, params) {
  if (method === 'skill.inventory') return snapshot();
  if (method === 'skill.inspect')
    return {
      id: params.id,
      content: `# Instructions\n\ncontent-${params.id}`,
    };
  return {};
}
beforeEach(() => {
  init('en');
  refreshState.set('token', 0);
  document.body.innerHTML = '';
  inventory = base();
  agents = baseAgents();
  projects = baseProjects();
  onToast.mockReset();
  rpcMock.mockReset();
  rpcMock.mockImplementation(async (method, params) =>
    defaultRpc(method, params),
  );
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});
async function render(props = {}) {
  component = mount(SkillsView, {
    target: document.body,
    props: {
      ...props,
      settings: {},
      onToast,
      get skillsRefreshToken() {
        return refreshState.get('token');
      },
    },
  });
  flushSync();
  await settle();
}
async function openAddMenu() {
  click(button('Add skills'));
  await settle();
  return document.querySelector('[role="menu"]');
}
async function addMenuItem(label) {
  const menu = await openAddMenu();
  click(button(label, menu));
  await settle();
}

describe('Skills manager', () => {
  it.each([false, true])(
    'opens the installed package even if an event supersedes its refresh (%s)',
    async (superseded) => {
      let deferInventory = false;
      const pendingReads = [];
      await render();
      collection('Main');
      await addMenuItem('Install from link or file…');
      input(
        document.querySelector('#skill-install-source'),
        'https://example.test/demo.skill',
      );
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'skill.install') {
          if (params.dry_run)
            return {
              operation: 'preview',
              name: 'demo',
              package_path: '.',
              files: 2,
              sha256: 'a'.repeat(64),
              candidates: [{ exists: false }],
            };
          inventory = [...inventory, entry('installed-private', 'demo')];
          return { operation: 'installed', scope: 'agent:main', name: 'demo' };
        }
        if (method === 'skill.inventory' && deferInventory)
          return new Promise((resolve) => pendingReads.push(resolve));
        if (method === 'skill.inspect')
          return { id: params.id, content: 'installed-instructions-sentinel' };
        return defaultRpc(method, params);
      });
      click(button(t('skills.install.check')));
      await settle();
      notifySkillsChanged();
      await settle();
      expect(document.querySelector('#skill-install-source').value).toBe(
        'https://example.test/demo.skill',
      );
      deferInventory = superseded;
      click(button(t('skills.install.action')));
      await settle();
      if (superseded) {
        notifySkillsChanged();
        await settle();
        expect(pendingReads).toHaveLength(2);
        pendingReads[0](snapshot());
        await settle();
        pendingReads[1](snapshot());
      }
      await settle();
      expect(rpcMock).toHaveBeenCalledWith(
        'skill.install',
        expect.objectContaining({ scope: 'agent:main', dry_run: false }),
      );
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(document.querySelector('.skills-content').textContent).toContain(
        'installed-instructions-sentinel',
      );
    },
  );
  it('opens a package page in place of its collection and returns to the collection as it was left', async () => {
    const styles = document.createElement('style');
    styles.textContent = ['../skills.css', '../../../styles/app.css']
      .map((path) =>
        readStyleSheet(fileURLToPath(new URL(path, import.meta.url))),
      )
      .join('\n');
    document.body.append(styles);
    await render();

    const view = document.querySelector('.skills-view');
    const collectionPage = view.querySelector('.skills-collection-page');
    expect(getComputedStyle(view).display).toBe('flex');
    expect(getComputedStyle(view).flexDirection).toBe('row');
    input(document.querySelector('input[type="search"]'), 'deploy');
    const returns = [
      () => key(document.activeElement, 'Escape'),
      () => click(button('All skills', view.querySelector('.skills-crumbs'))),
      () => collection('All skills'),
      () => click(button('Back to All skills')),
    ];
    for (const leave of returns) {
      choose('private');
      await settle();
      // Navigation and one content column: the package page replaces the
      // collection page, whose filters stay as they were.
      expect(getComputedStyle(collectionPage).display).toBe('none');
      expect(view.querySelector('.skills-main > .skills-page')).not.toBeNull();
      expect(document.activeElement).toBe(view.querySelector('.skills-page'));
      expect(texts('.skills-crumbs__trail li')).toEqual([
        'All skills',
        'deploy',
      ]);
      expect(view.querySelector('.skills-content').textContent).toContain(
        'content-private',
      );
      leave();
      await settle();
      expect(view.querySelector('.skills-page')).toBeNull();
      expect(getComputedStyle(collectionPage).display).not.toBe('none');
      expect(document.querySelector('input[type="search"]').value).toBe(
        'deploy',
      );
      expect(document.activeElement.dataset.skillId).toBe('private');
      expect(document.activeElement.classList).toContain('skills-row--current');
    }

    // Escape that another layer consumed, or typed in a text field, stays.
    choose('private');
    await settle();
    const page = view.querySelector('.skills-page');
    page.addEventListener('keydown', (event) => event.preventDefault(), {
      once: true,
    });
    page.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
        cancelable: true,
      }),
    );
    flushSync();
    expect(view.querySelector('.skills-page')).not.toBeNull();
  });

  it('shows collection, package and folder pages as its places', async () => {
    const navigation = createStandaloneNavigation(['all', 'private']);
    await render({ navigation });
    expect(document.querySelector('.skills-content').textContent).toContain(
      'content-private',
    );
    click(button('Back to All skills'));
    await settle();
    expect(navigation.place).toEqual(['all']);
    expect(document.querySelector('.skills-page')).toBeNull();

    collection('Main');
    await settle();
    expect(navigation.place).toEqual(['agent:main']);
    await addMenuItem('Manage skill folders…');
    expect(navigation.place).toEqual(['directories']);

    // A package that no longer exists leaves its collection page shown.
    navigation.navigate(['global', 'gone']);
    await settle();
    expect(navigation.place).toEqual(['global']);
    expect(document.querySelector('#skills-title').textContent.trim()).toBe(
      'Global',
    );
  });

  it('groups collections into library, Agents and Projects and filters each library source', async () => {
    await render();
    expect(texts('.skills-nav-label')).toEqual([
      'Library',
      'Agents',
      'Projects',
    ]);
    expect(
      [...document.querySelectorAll('.skills-collection')].map((el) => [
        el.querySelector('.skills-collection-name').textContent,
        el.querySelector('.skills-count').textContent,
      ]),
    ).toEqual([
      ['All skills', '4'],
      ['Global', '1'],
      ['Bundled', '1'],
      ['Shared skills', '1'],
      ['Main', '3'],
      ['Reviewer', '1'],
      ['Repo', '0'],
    ]);
    collection('Global');
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['disabled']);
    collection('Bundled');
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['bundled']);
    collection('Shared skills');
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['shared']);
    collection('All skills');
    expect(rows()).toHaveLength(4);

    click(button('Skill collections'));
    expect(texts('.dropdown-primitive__group-label')).toEqual([
      'Library',
      'Agents',
      'Projects',
    ]);
    const options = [...document.querySelectorAll('[role="option"]')];
    expect(
      options.map((el) => [
        el.querySelector('.dropdown-primitive__option-label').textContent,
        el.querySelector('.dropdown-primitive__option-meta').textContent.trim(),
      ]),
    ).toEqual([
      ['All skills', '4'],
      ['Global', '1'],
      ['Bundled', '1'],
      ['Shared skills', '1'],
      ['Main', '3'],
      ['Reviewer', '1'],
      ['Repo', '0'],
    ]);
    click(options[1]);
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['disabled']);
  });

  it('lists each package on one line with its source and who gets it, and its description only as a tooltip', async () => {
    await render();
    const list = document.querySelector('.skills-list');
    expect(
      list.querySelectorAll('[role="switch"], [role="checkbox"]'),
    ).toHaveLength(0);
    expect(
      rows().map((row) => [
        row.dataset.skillId,
        row.querySelector('.skills-row-source').textContent,
        row.querySelector('.skills-row-summary').textContent,
        row.querySelector('button'),
      ]),
    ).toEqual([
      ['disabled', 'Global', 'Off everywhere', null],
      ['private', 'Private', 'Main only', null],
      ['shared', 'Private', 'Main + 0 shared (1 blocked)', null],
      ['bundled', 'Bundled', '2 of 2 Agents', null],
    ]);
    // Explicit user requirement: no skill list renders descriptions inline.
    expect(list.textContent).not.toContain('Purpose of');
    key(document.body, 'Tab');
    rows()[1].focus();
    expect(document.getElementById('app-tooltip').textContent).toBe(
      'Purpose of deploy',
    );
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'skill.inspect'),
    ).toBe(false);
  });

  it('offers install, create and folder setup from one keyboard-operable add menu', async () => {
    await render();
    const trigger = button('Add skills');
    expect(trigger.closest('.skills-toolbar')).toBeTruthy();
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
    const menu = await openAddMenu();
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(texts('[role="menuitem"]', menu)).toEqual([
      'Install from link or file…',
      'Create skill…',
      'Manage skill folders…',
    ]);
    expect(document.activeElement.textContent).toBe(
      'Install from link or file…',
    );
    key(document.activeElement, 'ArrowDown');
    expect(document.activeElement.textContent).toBe('Create skill…');
    key(document.activeElement, 'End');
    expect(document.activeElement.textContent).toBe('Manage skill folders…');
    key(document.activeElement, 'Escape');
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);

    collection('Bundled');
    await addMenuItem('Manage skill folders…');
    const directories = document.querySelector('.skills-directories');
    expect(directories.hidden).toBe(false);
    expect(document.querySelector('#skills-title').textContent.trim()).toBe(
      'Skill folders',
    );
    expect(document.activeElement).toBe(
      directories.querySelector('.skills-directory-add input'),
    );
    input(document.activeElement, '/draft/location');
    click(button('← Back'));
    await settle();
    expect(directories.hidden).toBe(true);
    expect(rows().map((row) => row.dataset.skillId)).toEqual(['bundled']);
    expect(document.activeElement).toBe(button('Add skills'));
    await addMenuItem('Manage skill folders…');
    expect(document.activeElement.value).toBe('/draft/location');
  });

  it('bounds 750 skills to 50 rows, reaches the last page, and searches the whole collection', async () => {
    agents = Array.from({ length: 15 }, (_, i) =>
      agent(`agent-${i}`, `Agent ${i}`, ['*'], []),
    );
    inventory = agents.flatMap((owner, i) =>
      Array.from({ length: 50 }, (_, j) =>
        entry(`id-${i}-${j}`, `skill-${String(i * 50 + j).padStart(3, '0')}`, {
          owner_id: owner.id,
          editable_scope: `agent:${owner.id}`,
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
  });
  it('opens the exact duplicate-name package and rejects late detail responses', async () => {
    inventory = [
      entry('first', 'duplicate'),
      entry('second', 'duplicate', {
        origin: 'project:Second',
        owner_id: null,
        project_id: 'second',
        editable_scope: null,
      }),
    ];
    let finish;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'skill.inspect' && params.id === 'first')
        return new Promise((resolve) => {
          finish = resolve;
        });
      if (method === 'skill.inspect')
        return { id: params.id, content: 'second-sentinel' };
      return defaultRpc(method, params);
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
    expect(document.querySelector('.skills-page-note').textContent).toBe(
      'Also exists as Main’s private copy.',
    );
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
  it('creates in the selected Agent scope and retains draft content during inventory refresh', async () => {
    await render();
    collection('Main');
    await addMenuItem('Create skill…');
    const dialog = document.querySelector('[role="dialog"]');
    input(dialog.querySelector('#new-skill-name'), 'new-sentinel');
    input(dialog.querySelector('#new-skill-description'), 'Use for: reports');
    input(dialog.querySelector('#new-skill-content'), 'draft-sentinel');
    const callsBefore = calls('skill.inventory').length;
    notifySkillsChanged();
    await settle();
    expect(calls('skill.inventory')).toHaveLength(callsBefore + 1);
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

  it('saves an Agent skill selection immediately from its collection', async () => {
    await render();
    collection('Main');
    expect(document.querySelector('.skills-list')).toBeNull();
    expect(texts('.s-check-group__title')).toEqual(['Own skills', 'Bundled']);
    // Rows are single-line; descriptions only appear in the tooltip.
    expect(
      document.querySelector('.skills-selection').textContent,
    ).not.toContain('Purpose of');
    // Own Skills turn off through the exclusions, like any other Skill.
    click(button('Toggle skill deploy'));
    await settle();
    click(button('Toggle skill teach'));
    await settle();
    expect(calls('agent.update')).toEqual([
      { id: 'main', excluded_skills: ['deploy'] },
      { id: 'main', excluded_skills: ['teach'] },
    ]);

    collection('Reviewer');
    expect(
      document
        .querySelector('.skills-selection__auto [role="switch"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
    click(document.querySelector('.skills-selection__auto [role="switch"]'));
    await settle();
    // Turning on auto-add keeps the unticked shared Skill off.
    expect(calls('agent.update').at(-1)).toEqual({
      id: 'reviewer',
      allowed_skills: ['*'],
      excluded_skills: ['notes'],
    });

    click(document.querySelector('[data-item-key="teach"]'));
    await settle();
    expect(rpcMock).toHaveBeenCalledWith('skill.inspect', { id: 'bundled' });
    expect(document.querySelector('#skill-page-title').textContent.trim()).toBe(
      'teach',
    );
    expect(texts('.skills-crumbs__trail li')).toEqual(['Reviewer', 'teach']);
  });

  it('saves Project activation from the Project collection', async () => {
    await render();
    collection('Repo');
    click(button('Toggle skill teach'));
    await settle();
    expect(calls('project.set')).toEqual([
      { project_id: 'repo', skills_bundled_enabled: ['teach'] },
    ]);
  });

  it('offers library, Agent and Project row actions in context menus', async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    agents[1].root_project_id = 'repo';
    agents[1].skills[1] = grant('teach', 'bundled', 'project');
    const rightClick = (el) => {
      el.dispatchEvent(
        new MouseEvent('contextmenu', {
          bubbles: true,
          cancelable: true,
          clientX: 40,
          clientY: 40,
        }),
      );
      flushSync();
    };
    // Items in order, a separator as '|', a disabled item with its hint.
    const menu = () =>
      [...document.querySelector('.context-menu').children].map((el) =>
        el.getAttribute('role') === 'separator'
          ? '|'
          : [el.textContent.trim(), el.disabled ? 'disabled' : '']
              .filter(Boolean)
              .join(' '),
      );
    const pick = (label) =>
      click(
        [...document.querySelectorAll('.context-menu [role="menuitem"]')].find(
          (el) =>
            el.querySelector('.context-menu__label').textContent === label,
        ),
      );
    await render();

    rightClick(document.querySelector('[data-skill-id="shared"]'));
    expect(
      document.querySelector('.context-menu').getAttribute('aria-label'),
    ).toBe('Actions for notes');
    expect(menu()).toEqual([
      'Open',
      'Edit instructions',
      'Copy name',
      '|',
      'Turn off everywhere',
      '|',
      'Delete…',
    ]);
    pick('Copy name');
    await settle();
    expect(writeText).toHaveBeenCalledWith('notes');
    expect(onToast).toHaveBeenCalledWith({
      title: 'Copied notes',
      variant: 'success',
    });
    // The keyboard opens the same menu; read-only packages offer no Edit or
    // Delete, and a package that is off offers Turn on everywhere.
    key(document.querySelector('[data-skill-id="bundled"]'), 'ContextMenu');
    expect(menu()).toEqual(['Open', 'Copy name', '|', 'Turn off everywhere']);
    key(document.querySelector('.context-menu'), 'Escape');
    key(document.querySelector('[data-skill-id="disabled"]'), 'ContextMenu');
    expect(menu()).toContain('Turn on everywhere');
    pick('Turn on everywhere');
    await settle();
    expect(calls('skill.set_disabled')).toEqual([
      { name: 'broken', disabled: false },
    ]);
    // Edit opens the package page, then its editor with the loaded content.
    rightClick(document.querySelector('[data-skill-id="shared"]'));
    pick('Edit instructions');
    await settle();
    expect(document.querySelector('#skill-page-title').textContent.trim()).toBe(
      'notes',
    );
    expect(document.querySelector('[role="dialog"] textarea').value).toContain(
      'content-shared',
    );
    click(button('Cancel', document.querySelector('[role="dialog"]')));
    click(button('Back to All skills'));
    await settle();

    // An Agent row turns the Skill on or off for that Agent; a Project grant
    // stays fixed and says where it is managed.
    collection('Main');
    rightClick(
      document
        .querySelector('[data-item-key="deploy"]')
        .closest('.s-check-item'),
    );
    expect(menu()).toEqual([
      'Turn off for Main',
      'Open skill',
      'Copy name',
      '|',
      'Turn off everywhere',
    ]);
    pick('Turn off for Main');
    await settle();
    expect(calls('agent.update')).toEqual([
      { id: 'main', excluded_skills: ['deploy'] },
    ]);
    collection('Reviewer');
    key(document.querySelector('[data-item-key="teach"]'), 'ContextMenu');
    expect(menu()[0]).toBe(
      'Turn off for Reviewer Managed in project Repo disabled',
    );
    key(document.querySelector('.context-menu'), 'Escape');

    collection('Repo');
    rightClick(
      document
        .querySelector('[data-item-key="teach"]')
        .closest('.s-check-item'),
    );
    expect(menu()).toEqual(['Activate in Repo', 'Open skill', 'Copy name']);
    pick('Activate in Repo');
    await settle();
    expect(calls('project.set')).toEqual([
      { project_id: 'repo', skills_bundled_enabled: ['teach'] },
    ]);
  });

  it('changes Agent and Project access from the Skill detail', async () => {
    await render();
    choose('bundled');
    await settle();
    const access = document.querySelector('.skills-access');
    expect(button('Allow for Main', access).getAttribute('aria-checked')).toBe(
      'true',
    );
    click(button('Allow for Main', access));
    await settle();
    expect(calls('agent.update')).toEqual([
      { id: 'main', excluded_skills: ['teach'] },
    ]);
    click(button('Active in project Repo', access));
    await settle();
    expect(calls('project.set')).toEqual([
      { project_id: 'repo', skills_bundled_enabled: ['teach'] },
    ]);
  });

  it('shares a private Skill through its Agent rows and allows a blocked receiver', async () => {
    await render();
    choose('shared');
    await settle();
    let access = document.querySelector('.skills-access');
    const owner = button('Allow for Main', access);
    expect(owner.querySelector('.s-check-row__state').textContent).toBe(
      'Owner',
    );
    click(owner);
    await settle();
    const share = button('Share with Reviewer', access);
    expect(share.getAttribute('aria-checked')).toBe('true');
    expect(share.textContent).toContain(
      'Blocked by this Agent’s skill selection',
    );
    click(button('Allow notes for Reviewer', access));
    await settle();
    expect(calls('agent.update')).toEqual([
      { id: 'main', excluded_skills: ['notes'] },
      { id: 'reviewer', allowed_skills: ['teach', 'notes'] },
    ]);
    click(button('Share with Reviewer', access));
    await settle();
    choose('private');
    await settle();
    access = document.querySelector('.skills-access');
    click(button('Share with Reviewer', access));
    await settle();
    expect(calls('skill.share')).toEqual([
      { agent_id: 'main', name: 'notes', shared: false, receivers: [] },
      {
        agent_id: 'main',
        name: 'deploy',
        shared: true,
        receivers: ['reviewer'],
      },
    ]);
  });

  it('turns a Skill off everywhere and back on from its page header', async () => {
    await render();
    choose('private');
    await settle();
    rpcMock.mockRejectedValueOnce(new Error('mutation-sentinel'));
    click(button('Turn off everywhere'));
    await settle();
    expect(onToast).toHaveBeenCalledWith({
      title: `${t('skills.toggleError')} mutation-sentinel`,
      variant: 'error',
    });
    let finish;
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'skill.set_disabled')
        return new Promise((resolve) => {
          finish = resolve;
        });
      return defaultRpc(method, params);
    });
    click(button('Turn off everywhere'));
    expect(button('Turn off everywhere').disabled).toBe(true);
    finish({});
    await settle();
    expect(calls('skill.set_disabled')).toEqual([
      { name: 'deploy', disabled: true },
      { name: 'deploy', disabled: true },
    ]);

    click(button('Back to All skills'));
    await settle();
    choose('disabled');
    await settle();
    const detail = document.querySelector('.skills-page');
    expect(button('Turn off everywhere', detail)).toBeUndefined();
    expect(
      [...detail.querySelectorAll('.skills-access [role="checkbox"]')].every(
        (box) => box.disabled,
      ),
    ).toBe(true);
    click(button('Turn on', detail));
    await settle();
    expect(calls('skill.set_disabled').at(-1)).toEqual({
      name: 'broken',
      disabled: false,
    });
  });
  it('refreshes the list immediately after connecting a Skill folder', async () => {
    await render();
    await addMenuItem('Manage skill folders…');
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
      return defaultRpc(method, params);
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
    collection('All skills');
    expect(rows().map((row) => row.dataset.skillId)).toContain(
      'folder-sentinel',
    );
  });
  it('confirms deletion from the page header', async () => {
    await render();
    choose('shared');
    await settle();
    click(button('Delete notes', document.querySelector('.skills-page')));
    const dialog = document.querySelector('[role="dialog"]');
    expect(calls('skill.delete')).toEqual([]);
    click(button('Delete', dialog));
    await settle();
    expect(calls('skill.delete')).toEqual([
      { scope: 'agent:main', name: 'notes' },
    ]);
  });
  it('opens a Skill page with its source, description, keyboard content tabs, and focus return', async () => {
    await render();
    choose('private');
    await settle();
    const detail = document.querySelector('.skills-page');
    expect(document.activeElement).toBe(detail);
    expect(button('Delete deploy', detail)).toBeTruthy();
    expect(button('Turn off everywhere', detail)).toBeTruthy();
    expect(detail.querySelector('.skills-page-source').textContent.trim()).toBe(
      'Private skill of Main',
    );
    // The package page is the one place showing the description as content.
    expect(
      detail.querySelector('.skills-page-description').textContent.trim(),
    ).toBe('Purpose of deploy');

    const tab = document.querySelector('[role="tab"]');
    tab.focus();
    key(tab, 'ArrowRight');
    expect(
      document
        .querySelector('[role="tab"][aria-selected="true"]')
        .textContent.trim(),
    ).toBe(t('skills.original'));
    click(button('Back to All skills'));
    await settle();
    expect(document.activeElement.dataset.skillId).toBe('private');
  });
  it('keeps the inventory visible after refresh failure and supports retry', async () => {
    await render();
    rpcMock.mockImplementation(async (method) => {
      if (method === 'skill.inventory') throw new Error('refresh-sentinel');
      return {};
    });
    notifySkillsChanged();
    await settle();
    expect(rows()).toHaveLength(4);
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(button('Retry')).toBeTruthy();
    inventory = [...inventory, entry('arrived', 'new-skill')];
    rpcMock.mockImplementation(async (method, params) =>
      defaultRpc(method, params),
    );
    click(button('Retry'));
    await settle();
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(rows()).toHaveLength(5);
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
    expect(filterSkills(entries, '', 'shared').map((item) => item.id)).toEqual([
      'shared',
    ]);
  });
  it.each([
    ['a private package', {}, 'Private'],
    [
      'a Project package',
      { owner_id: null, origin: 'project:Repo sentinel' },
      'Project',
    ],
    ['a bundled package', { owner_id: null, origin: 'bundled' }, 'Bundled'],
    [
      'an added folder or Extension',
      { owner_id: null, origin: 'global', source_label: 'computer_use' },
      'Computer use',
    ],
    ['the global folder', { owner_id: null, origin: 'global' }, 'Global'],
  ])('labels %s by where it lives', (_label, extra, text) => {
    expect(skillSourceLabel(entry('a', 'a', extra))).toBe(text);
  });
  it('omits only a complete frontmatter block for presentation', () => {
    expect(skillInstructionBody('---\nname: x\n---\n# Body')).toBe('# Body');
    expect(skillInstructionBody('---\nno-closing-marker')).toBe(
      '---\nno-closing-marker',
    );
  });
});
