// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  listProjectsMock,
  showProjectMock,
  setProjectMock,
  ProjectsView,
  AUTO_SAVE_WAIT_MS,
  project,
  buttonByTestId,
  inputById,
  optionByText,
  setInputValue,
  wait,
  waitForCondition,
  selectDemo,
  setupProjectsViewSuite,
} from './ProjectsView.support.js';

describe('ProjectsView', () => {
  const suite = setupProjectsViewSuite();

  it('seeds the temperature field and thinking-effort dropdown from the project', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          default_temperature: 0.4,
          default_thinking_effort: 'high',
        }),
      ],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-temperature'));
    expect(inputById('project-edit-temperature').value).toBe('0.4');
    const trigger = document.getElementById('project-edit-thinking-effort');
    expect(trigger).toBeTruthy();
    expect(trigger.textContent).toContain('high');
  });

  it('saves a changed default temperature through project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-temperature'));
    setInputValue('project-edit-temperature', '0.2');

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_temperature: 0.2,
    });
  });

  it('saves a changed default thinking effort through project.set', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [project({ project_id: 'demo', display_name: 'Demo' })],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() =>
      document.getElementById('project-edit-thinking-effort'),
    );
    document.getElementById('project-edit-thinking-effort').click();
    flushSync();
    await waitForCondition(() => optionByText('low'));
    optionByText('low').click();
    flushSync();

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      default_thinking_effort: 'low',
    });
  });

  it('adds and removes auto-load files through the list and saves them', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          display_name: 'Demo',
          auto_load: ['AGENTS.md'],
        }),
      ],
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-auto-load'));
    setInputValue('project-edit-auto-load', 'docs/guide.md');
    buttonByTestId('project-auto-load-add').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="project-auto-load-remove-1"]'),
    );
    buttonByTestId('project-auto-load-remove-0').click();
    flushSync();

    buttonByTestId('project-save-demo').click();

    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      auto_load: ['docs/guide.md'],
    });
  });

  it.each([
    [0, 2, ['docs/guide.md', 'NOTES.md', 'AGENTS.md']],
    [2, 0, ['NOTES.md', 'AGENTS.md', 'docs/guide.md']],
  ])(
    'drags auto-load file %i to %i and auto-saves its order',
    async (from, to, expected) => {
      const original = project({
        project_id: 'demo',
        auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'],
      });
      const saved = { ...original, auto_load: expected };
      listProjectsMock.mockResolvedValue({ projects: [original] });
      setProjectMock.mockImplementation(async () => {
        listProjectsMock.mockResolvedValue({ projects: [saved] });
        return {
          project: saved,
          scan: { team: [], report: { clean: true, findings: [] } },
        };
      });
      suite.mountedComponent = mount(ProjectsView, { target: document.body });
      flushSync();
      await selectDemo();
      const handle = document.querySelector(
        `[data-auto-load-handle="${from}"]`,
      );
      const row = document.querySelectorAll('.projects-file-row')[to];
      const dataTransfer = {
        setData: vi.fn(),
        effectAllowed: '',
        dropEffect: '',
      };
      const start = new Event('dragstart', { bubbles: true });
      Object.defineProperty(start, 'dataTransfer', { value: dataTransfer });
      handle.dispatchEvent(start);
      const over = new Event('dragover', { bubbles: true, cancelable: true });
      row.dispatchEvent(over);
      flushSync();
      expect(over.defaultPrevented).toBe(true);
      expect(row.classList.contains('projects-file-row--drop')).toBe(true);
      expect(dataTransfer.effectAllowed).toBe('move');
      row.dispatchEvent(new Event('drop', { bubbles: true, cancelable: true }));
      flushSync();
      expect(
        [...document.querySelectorAll('.projects-file-name')].map(
          (node) => node.textContent,
        ),
      ).toEqual(expected);
      expect(document.querySelector('.projects-file-row--drop')).toBeNull();
      await new Promise((resolve) => setTimeout(resolve, AUTO_SAVE_WAIT_MS));
      await waitForCondition(() => setProjectMock.mock.calls.length === 1);
      expect(setProjectMock).toHaveBeenCalledWith('demo', {
        auto_load: expected,
      });
      flushSync();
      expect(
        [...document.querySelectorAll('.projects-file-name')].map(
          (node) => node.textContent,
        ),
      ).toEqual(expected);
    },
  );

  it('reorders auto-load files by keyboard, retains focus, and respects list boundaries', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'],
        }),
      ],
    });
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    const handle = (index) =>
      document.querySelector(`[data-auto-load-handle="${index}"]`);
    const press = (index, key) => {
      handle(index).focus();
      handle(index).dispatchEvent(
        new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }),
      );
      flushSync();
    };
    press(0, 'ArrowUp');
    press(2, 'ArrowDown');
    expect(setProjectMock).not.toHaveBeenCalled();
    press(0, 'ArrowDown');
    await waitForCondition(() => document.activeElement === handle(1));
    expect(
      [...document.querySelectorAll('.projects-file-name')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['docs/guide.md', 'AGENTS.md', 'NOTES.md']);
    expect(
      document.querySelector('[aria-live="polite"]').textContent,
    ).toContain('AGENTS.md');
    press(1, 'ArrowUp');
    await waitForCondition(() => document.activeElement === handle(0));
    expect(
      [...document.querySelectorAll('.projects-file-name')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['AGENTS.md', 'docs/guide.md', 'NOTES.md']);
  });

  it('ignores external, canceled, same-row, and stale auto-load drops', async () => {
    listProjectsMock.mockResolvedValue({
      projects: [
        project({
          project_id: 'demo',
          auto_load: ['AGENTS.md', 'docs/guide.md', 'NOTES.md'],
        }),
      ],
    });
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    const handle = document.querySelector('[data-auto-load-handle="0"]');
    const drop = (index) => {
      const row = document.querySelectorAll('.projects-file-row')[index];
      row.dispatchEvent(new Event('drop', { bubbles: true, cancelable: true }));
      flushSync();
    };
    drop(2);
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    handle.dispatchEvent(new Event('dragend', { bubbles: true }));
    drop(2);
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    drop(0);
    expect(
      [...document.querySelectorAll('.projects-file-name')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['AGENTS.md', 'docs/guide.md', 'NOTES.md']);
    expect(setProjectMock).not.toHaveBeenCalled();
    handle.dispatchEvent(new Event('dragstart', { bubbles: true }));
    buttonByTestId('project-auto-load-remove-1').click();
    flushSync();
    drop(1);
    expect(
      [...document.querySelectorAll('.projects-file-name')].map(
        (node) => node.textContent,
      ),
    ).toEqual(['AGENTS.md', 'NOTES.md']);
  });

  it('auto-saves edited fields after the debounce without a Save click', async () => {
    listProjectsMock
      .mockResolvedValueOnce({
        projects: [project({ project_id: 'demo', display_name: 'Demo' })],
      })
      .mockResolvedValue({
        projects: [project({ project_id: 'demo', display_name: 'Renamed' })],
      });
    setProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo', display_name: 'Renamed' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });

    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();

    await selectDemo();

    await waitForCondition(() => inputById('project-edit-name'));
    setInputValue('project-edit-name', 'Renamed');

    await wait(AUTO_SAVE_WAIT_MS);
    flushSync();
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);

    expect(setProjectMock).toHaveBeenCalledWith('demo', {
      display_name: 'Renamed',
    });
  });

  it('saves the same delta independently for different Projects', async () => {
    const records = {
      demo: project({ project_id: 'demo', display_name: 'Demo' }),
      other: project({ project_id: 'other', display_name: 'Other' }),
    };
    const scan = { team: [], report: { clean: true, findings: [] } };
    listProjectsMock.mockImplementation(async () => ({
      projects: Object.values(records),
    }));
    showProjectMock.mockImplementation(async (id) => ({
      project: records[id],
      scan,
    }));
    setProjectMock.mockImplementation(async (id, changes) => {
      records[id] = { ...records[id], ...changes };
      return { project: records[id], scan };
    });
    suite.mountedComponent = mount(ProjectsView, { target: document.body });
    flushSync();
    await selectDemo();
    await waitForCondition(
      () => inputById('project-edit-name')?.value === 'Demo',
    );
    setInputValue('project-edit-name', 'Shared name');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setProjectMock.mock.calls.length === 1);
    buttonByTestId('project-toggle-other').click();
    await waitForCondition(
      () => inputById('project-edit-name')?.value === 'Other',
    );
    setInputValue('project-edit-name', 'Shared name');
    await wait(AUTO_SAVE_WAIT_MS);
    await waitForCondition(() => setProjectMock.mock.calls.length === 2);
    expect(setProjectMock).toHaveBeenLastCalledWith('other', {
      display_name: 'Shared name',
    });
  });
});
