// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  createBridge,
  render,
  settle,
  button,
  fill,
  choose,
  calls,
  setup,
  step,
  t,
  tick,
} from './FactoryPage.support.js';

const clone = (value) => JSON.parse(JSON.stringify(value));
const template = (extra = {}) => ({
  id: 'flt-review',
  kind: 'step',
  name: 'Shared review',
  description: '',
  version: 1,
  latest_version: 1,
  revision: 1,
  archived: false,
  versions: [1],
  workflow: {
    id: 'review',
    name: 'Shared review',
    description: '',
    steps: [step('review', 'Review')],
  },
  ...extra,
});
const source = (extra = {}) => ({
  id: 'fls-review',
  template_id: 'flt-review',
  version: 1,
  step_map: { review: 'review' },
  base: {
    name: 'Shared review',
    description: '',
    steps: [step('review', 'Review')],
  },
  whole_workflow: false,
  after: ['implement'],
  connections: {},
  ...extra,
});
const taskId = () => document.querySelector('.factory-step-task textarea').id;

describe('Factory Library', () => {
  it('publishes without an assigned Agent, preserves pending edits and opens each new template cleanly', async () => {
    const { bridge, operation, state } = createBridge();
    let complete;
    state.handlers['library.publish'] = (args) =>
      new Promise((resolve) => {
        complete = () => {
          const item = template({ ...clone(args), id: 'flt-new' });
          delete item.expected_revision;
          state.library = [item];
          resolve({ template: clone(item) });
        };
      });
    await render(bridge, '/library/new');
    fill('factory-template-name', 'Review');
    await choose('factory-template-kind', t('factory.library.kindStep'));
    fill(taskId(), 'Check the evidence');
    button(t('factory.library.publish')).click();
    await tick();
    fill('factory-template-name', 'Review with local follow-up');
    complete();
    await settle();
    expect(calls(operation, 'library.publish')[0][1]).toMatchObject({
      name: 'Review',
      kind: 'step',
      expected_revision: 0,
    });
    expect(
      calls(operation, 'library.publish')[0][1].workflow.steps[0].agent,
    ).toBe('');
    expect(document.getElementById('factory-template-name').value).toBe(
      'Review with local follow-up',
    );
    expect(button(t('factory.library.publishVersion')).disabled).toBe(false);
    await bridge.show('');
    await settle();
    await bridge.show('/library/flt-new');
    await settle();
    expect(document.getElementById('factory-template-name').value).toBe(
      'Review with local follow-up',
    );
    await bridge.show('/library/new');
    await settle();
    expect(document.getElementById('factory-template-name').value).toBe('');
    fill('factory-template-name', 'Another review');
    fill(taskId(), 'A different check');
    button(t('factory.library.publish')).click();
    await tick();
    expect(calls(operation, 'library.publish')[1][1].expected_revision).toBe(0);
    complete();
    await settle();
  });

  it('keeps unpublished edits and archive retains version history without advancing stale drafts', async () => {
    const { bridge, state, operation } = createBridge();
    state.library = [template()];
    state.libraryVersions['flt-review:1'] = template();
    await render(bridge, '/library/flt-review');
    fill('factory-template-name', 'Unpublished name');
    await bridge.show('');
    await settle();
    await bridge.show('/library/flt-review');
    await settle();
    expect(document.getElementById('factory-template-name').value).toBe(
      'Unpublished name',
    );
    expect(button(t('factory.library.publishVersion')).disabled).toBe(false);
    state.library[0] = template({
      version: 2,
      latest_version: 2,
      revision: 2,
      versions: [2, 1],
    });
    bridge.invalidate({ resource: 'library' });
    await settle();
    expect(document.getElementById('factory-template-name').value).toBe(
      'Unpublished name',
    );
    expect(document.body.textContent).toContain(
      t('factory.library.newerDraft'),
    );
    button(t('factory.library.archive')).click();
    await tick();
    button(
      t('factory.library.archive'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(state.library[0].archived).toBe(true);
    await choose(
      'factory-template-history',
      t('factory.library.version', { version: 1 }),
    );
    await settle();
    expect(document.querySelector('.factory-section').textContent).toContain(
      'test-owned task for Review',
    );
    expect(button(t('factory.library.restore'))).toBeDefined();
    button(t('factory.library.restore')).click();
    await tick();
    button(
      t('factory.library.restore'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(document.body.textContent).toContain(
      t('factory.library.newerDraft'),
    );
    button(t('factory.library.publishVersion')).click();
    await settle();
    expect(calls(operation, 'library.publish')[0][1].expected_revision).toBe(1);
    expect(document.querySelector('[role="alert"]').textContent).toContain(
      t('factory.error.conflict'),
    );
    button(t('factory.library.discardDraft')).click();
    await tick();
    expect(button(t('factory.library.publishVersion')).disabled).toBe(true);
  });

  it('requires explicit conflict resolution and persists the pinned source version with Project overrides', async () => {
    vi.useFakeTimers();
    const { bridge, operation, state } = createBridge();
    state.library = [
      template({
        version: 2,
        latest_version: 2,
        revision: 2,
        versions: [2, 1],
      }),
    ];
    state.setups[0].workflows[0].sources = [source()];
    state.setups[0].workflows[0].steps[1].task = 'Local evidence requirement';
    const merge = (args) => {
      const workflow = clone(args.workflow);
      workflow.sources[0].version = 2;
      if (args.resolutions?.task === 'template')
        workflow.steps[1].task = 'New shared requirement';
      return {
        workflow,
        version: 2,
        valid: true,
        problems: [],
        changes: [
          {
            step: 'review',
            field: 'task',
            base: 'Old requirement',
            template: 'New shared requirement',
          },
        ],
        conflicts: [
          {
            id: 'task',
            step: 'review',
            field: 'task',
            base: 'Old requirement',
            local: 'Local evidence requirement',
            template: 'New shared requirement',
            resolution: args.resolutions?.task ?? '',
          },
        ],
      };
    };
    state.handlers['library.preview_update'] = merge;
    state.handlers['library.apply_update'] = merge;
    await render(bridge, '/setups/project-a');
    button(t('factory.library.reviewUpdate', { version: 2 })).click();
    await settle();
    expect(button(t('factory.library.applyUpdate')).disabled).toBe(true);
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Local evidence requirement',
    );
    await choose('factory-conflict-0', t('factory.library.keepLocal'));
    await settle();
    button(t('factory.library.applyUpdate')).click();
    await settle();
    await vi.advanceTimersByTimeAsync(900);
    await settle();
    expect(calls(operation, 'library.apply_update')[0][1]).toMatchObject({
      version: 2,
      source_id: 'fls-review',
      resolutions: { task: 'local' },
    });
    const saved = state.setups[0].workflows[0];
    expect(saved.sources[0]).toMatchObject({
      id: 'fls-review',
      version: 2,
      connections: {},
    });
    expect(saved.steps[1]).toMatchObject({
      task: 'Local evidence requirement',
      agent: 'critic',
    });
  });

  it.each([false, true])(
    'publishes selected steps without duplication, replacing complete existing links: %s',
    async (linked) => {
      vi.useFakeTimers();
      const { bridge, operation, state } = createBridge();
      if (linked)
        state.setups[0].workflows[0].sources = [
          source({ step_map: { review: 'review', integrate: 'integrate' } }),
        ];
      let completeExtract;
      state.handlers['library.extract'] = ({ workflow, step_ids }) =>
        new Promise((resolve) => {
          completeExtract = () =>
            resolve({
              workflow: {
                ...clone(workflow),
                steps: workflow.steps
                  .filter((entry) => step_ids.includes(entry.id))
                  .map((entry) => ({ ...entry, after: [], returns_to: '' })),
              },
            });
        });
      state.handlers['library.link'] = ({ workflow }) => ({
        workflow: {
          ...clone(workflow),
          sources: [source({ template_id: 'flt-new' })],
        },
      });
      await render(bridge, '/setups/project-a');
      button(t('factory.library.saveTemplate')).click();
      await tick();
      await choose('factory-publish-kind', t('factory.library.kindStep'));
      document.getElementById('factory-publish-step-implement').click();
      document.getElementById('factory-publish-step-integrate').click();
      await tick();
      if (linked) {
        button(t('factory.library.publish')).click();
        await tick();
        expect(calls(operation, 'library.publish')).toHaveLength(0);
        expect(calls(operation, 'library.extract')).toHaveLength(0);
        expect(document.querySelector('[role="alert"]').textContent).toContain(
          t('factory.library.partialReplacement'),
        );
        await choose('factory-publish-kind', t('factory.library.kindSequence'));
        document.getElementById('factory-publish-step-integrate').click();
        await tick();
        expect(document.querySelector('[role="dialog"]').textContent).toContain(
          t('factory.library.replaceLinks'),
        );
      }
      button(t('factory.library.publish')).click();
      await tick();
      expect(document.getElementById('factory-publish-kind').disabled).toBe(
        true,
      );
      expect(document.getElementById('factory-publish-name').disabled).toBe(
        true,
      );
      expect(
        document.getElementById('factory-publish-step-review').disabled,
      ).toBe(true);
      completeExtract();
      await settle();
      await vi.advanceTimersByTimeAsync(900);
      await settle();
      const selection = linked ? ['review', 'integrate'] : ['review'];
      expect(calls(operation, 'library.extract')[0][1].step_ids).toEqual(
        selection,
      );
      expect(calls(operation, 'library.link')[0][1]).toMatchObject({
        id: 'flt-new',
        version: 1,
        step_ids: selection,
        ...(linked ? { replace_sources: true } : {}),
      });
      expect(state.setups[0].workflows[0].steps).toHaveLength(3);
      expect(state.setups[0].workflows[0].sources[0].template_id).toBe(
        'flt-new',
      );
    },
  );

  it.each([1, 2])(
    'publishes changed membership using a chosen source among %i template uses',
    async (uses) => {
      const { bridge, operation, state } = createBridge();
      state.library = [template({ kind: 'sequence' })];
      state.setups[0].workflows[0].sources = Array.from(
        { length: uses },
        (_, index) =>
          source({
            id: `fls-${index}`,
            step_map: { review: index ? 'integrate' : 'review' },
          }),
      );
      state.handlers['library.extract'] = ({ workflow }) => ({ workflow });
      await render(bridge, '/setups/project-a');
      button(t('factory.library.saveTemplate')).click();
      await tick();
      await choose('factory-publish-kind', t('factory.library.kindSequence'));
      await choose('factory-publish-target', 'Shared review');
      if (uses > 1) {
        button(t('factory.library.publish')).click();
        await settle();
        expect(calls(operation, 'library.publish')).toHaveLength(0);
        await choose(
          'factory-publish-source',
          t('factory.library.sourceNumber', { number: 1 }),
        );
      }
      button(t('factory.library.publish')).click();
      await settle();
      expect(calls(operation, 'library.extract')[0][1]).toMatchObject({
        source_id: 'fls-0',
        step_ids: setup.workflows[0].steps.map((entry) => entry.id),
      });
      expect(calls(operation, 'library.publish')[0][1]).toMatchObject({
        id: 'flt-review',
        expected_revision: 1,
      });
    },
  );
});
