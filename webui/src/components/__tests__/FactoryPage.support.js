// @vitest-environment jsdom
import { afterEach, expect, vi } from 'vitest';
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { mount, unmount, tick, flushSync } = await import('svelte');
const { registerCatalog, t } = await import('../../lib/i18n.js');
const { default: catalog } =
  await import('../../../../resources/extensions/factory/ui/i18n.js');
registerCatalog(catalog);
const { default: FactoryPage } =
  await import('../../../../resources/extensions/factory/ui/FactoryPage.svelte');
let mounted;

const clone = (value) => JSON.parse(JSON.stringify(value));

export function createFactoryBridge({ established = true } = {}) {
  const state = {
    factories: established
      ? [
          {
            id: 'fac-one',
            project_id: 'project-a',
            name: 'Alpha Factory',
            leader_member_id: 'mem-leader',
            remit: 'Maintain project A',
            state: 'active',
            capacity: 2,
            leader_available: true,
            config: {},
            revision: 1,
          },
        ]
      : [],
    members: established
      ? [
          {
            id: 'mem-leader',
            factory_id: 'fac-one',
            agent_id: 'planner',
            agent_name: 'Planner',
            role: 'Leader',
            instructions: 'Coordinate work',
            agent_proof: 'leader-stamp',
            profile_available: true,
            created_by_factory: false,
            config: {},
            capacity: 1,
            state: 'active',
            revision: 1,
          },
          {
            id: 'mem-worker',
            factory_id: 'fac-one',
            agent_id: 'builder',
            agent_name: 'Builder',
            role: 'Specialist',
            instructions: 'Implement changes',
            agent_proof: 'builder-stamp',
            profile_available: true,
            created_by_factory: true,
            config: {},
            capacity: 1,
            state: 'active',
            revision: 1,
          },
        ]
      : [],
    jobs: established
      ? [
          {
            id: 'fjob-auto',
            factory_id: 'fac-one',
            name: 'Nightly Check',
            prompt: 'Run nightly checks',
            description: 'Checks tests every morning',
            agent_member_id: 'mem-worker',
            enabled: true,
            revision: 1,
            triggers: [
              {
                id: 'trg-cron',
                type: 'schedule',
                title: 'Weekday morning',
                schedule: '0 9 * * 1-5',
                enabled: true,
                status: 'active',
                next_fire: '2026-11-26T09:00:00Z',
                last_fire: null,
                last_error: null,
              },
            ],
          },
          {
            id: 'fjob-manual',
            factory_id: 'fac-one',
            name: 'Release Notes',
            prompt: 'Draft release notes',
            description: '',
            agent_member_id: '',
            enabled: true,
            revision: 1,
            triggers: [],
          },
        ]
      : [],
    runs: established
      ? [
          {
            id: 'jrun-attn',
            factory_id: 'fac-one',
            job_id: 'fjob-auto',
            job_name: 'Nightly Check',
            member_id: 'mem-worker',
            state: 'needs_attention',
            origin: 'schedule',
            group_id: 'fjob.jrun-attn',
            participant_id: 'worker',
            prompt: 'Run nightly checks',
            input_note: '',
            question: 'Should I proceed with migration?',
            reason: 'Waiting for operator confirmation',
            summary: '',
            session_address: {
              agent_id: 'builder',
              project_id: 'project-a',
              session_id: 'sess-attn',
            },
            revision: 1,
            created_at: '2026-11-25T08:00:00Z',
            updated_at: '2026-11-25T08:01:00Z',
            finished_at: null,
          },
          {
            id: 'jrun-active',
            factory_id: 'fac-one',
            job_id: 'fjob-manual',
            job_name: 'Release Notes',
            member_id: 'mem-leader',
            state: 'running',
            origin: 'manual',
            group_id: 'fjob.jrun-active',
            participant_id: 'worker',
            prompt: 'Draft release notes',
            input_note: '',
            question: '',
            reason: '',
            summary: '',
            revision: 1,
            created_at: '2026-11-25T08:05:00Z',
            updated_at: '2026-11-25T08:05:00Z',
            finished_at: null,
          },
          {
            id: 'jrun-done',
            factory_id: 'fac-one',
            job_id: 'fjob-auto',
            job_name: 'Nightly Check',
            member_id: 'mem-worker',
            state: 'done',
            origin: 'schedule',
            group_id: 'fjob.jrun-done',
            participant_id: 'worker',
            prompt: 'Run nightly checks',
            input_note: '',
            question: '',
            reason: '',
            summary: 'All 42 checks passed.',
            revision: 2,
            created_at: '2026-11-24T09:00:00Z',
            updated_at: '2026-11-24T09:02:00Z',
            finished_at: '2026-11-24T09:02:00Z',
          },
        ]
      : [],
    secrets: {},
    conversation: {
      topic: { id: 'top-1', title: 'Overview chat', state: 'open' },
      posts: [
        {
          id: 'post-1',
          author: 'member:mem-leader',
          author_name: 'Planner',
          text: 'Ready to coordinate.',
        },
      ],
      session: {
        agent_id: 'planner',
        project_id: 'project-a',
        session_id: 'sess-leader',
      },
      session_binding: {
        group_id: 'native.top-1',
        participant_id: 'mem-leader',
      },
      member_name: 'Planner',
      profile_available: true,
      agent_id: 'planner',
      agent_model: 'demo/worker',
      agent_proof: 'leader-stamp',
      factory_id: 'fac-one',
    },
  };

  const contexts = new Set();
  const invalidations = new Set();
  const layers = [];
  let autosave = null;
  let context = null;

  const findFactory = (id) => state.factories.find((item) => item.id === id);
  const findMember = (id) => state.members.find((item) => item.id === id);
  const findJob = (id) => state.jobs.find((item) => item.id === id);
  const findRun = (id) => state.runs.find((item) => item.id === id);

  const enrichJob = (job) => ({
    ...clone(job),
    triggers: (job.triggers || []).map((trg) => ({
      ...clone(trg),
      status: trg.enabled ? 'active' : 'paused',
      next_fire: trg.type === 'schedule' ? '2026-11-26T09:00:00Z' : null,
      last_fire: null,
      last_error: null,
      webhook_url:
        trg.type === 'webhook'
          ? `https://vbot.test/hooks/${job.id}/${trg.id}`
          : null,
    })),
  });

  const operation = vi.fn(async (name, args = {}) => {
    if (name === 'catalog') {
      return {
        triggers_available: true,
        catalog: {
          permission_profiles: ['full_access', 'restricted'],
          projects: [
            { id: 'project-a', name: 'Project A', pending_factory_agents: [] },
            { id: 'project-b', name: 'Project B', pending_factory_agents: [] },
          ],
          agents: [
            { id: 'planner', name: 'Planner' },
            { id: 'reviewer', name: 'Reviewer' },
          ],
          models: [
            {
              id: 'demo/worker',
              name: 'Demo Worker',
              context_window: 128000,
              capabilities: { tools: true },
            },
            {
              id: 'demo/reviewer',
              name: 'Demo Reviewer',
              context_window: 128000,
              capabilities: { tools: true },
            },
          ],
        },
      };
    }

    if (name === 'factory.list') {
      return { factories: clone(state.factories) };
    }

    if (name === 'factory.get') {
      const f = findFactory(args.id);
      if (!f) throw Object.assign(new Error('missing'), { code: 'not_found' });
      return { factory: clone(f) };
    }

    if (name === 'factory.create') {
      const created = {
        id: `fac-${state.factories.length + 1}`,
        project_id: args.project_id,
        name: args.project_id === 'project-b' ? 'Beta Factory' : 'New Factory',
        leader_member_id: 'mem-new-leader',
        remit: args.remit || '',
        state: 'active',
        capacity: 2,
        leader_available: true,
        config: {},
        revision: 1,
      };
      state.factories.push(created);
      state.members.push({
        id: 'mem-new-leader',
        factory_id: created.id,
        agent_id: args.leader_agent_id || 'new-leader',
        agent_name: args.leader_name || 'Reviewer',
        role: 'Leader',
        instructions: '',
        agent_proof: 'new-leader-stamp',
        profile_available: true,
        created_by_factory: !args.leader_agent_id,
        config: {},
        capacity: 1,
        state: 'active',
        revision: 1,
      });
      return { factory: clone(created) };
    }

    if (name === 'factory.update') {
      const f = findFactory(args.id);
      if (f.revision !== args.expected_revision) {
        throw Object.assign(new Error('conflict'), {
          code: 'revision_conflict',
        });
      }
      const rest = { ...args };
      delete rest.id;
      delete rest.expected_revision;
      delete rest.clear_fields;
      Object.assign(f, rest, { revision: f.revision + 1 });
      return { factory: clone(f) };
    }

    if (name === 'factory.archive') {
      const f = findFactory(args.id);
      f.state = 'archived';
      f.revision += 1;
      return { factory: clone(f) };
    }

    if (name === 'member.get') {
      return { member: clone(findMember(args.id)) };
    }

    if (name === 'member.add') {
      if (!args.factory_id || !args.agent_id) {
        throw Object.assign(new Error('invalid_request'), {
          code: 'invalid_request',
        });
      }
      const agentNames = { planner: 'Planner', reviewer: 'Reviewer' };
      const created = {
        id: `mem-${state.members.length + 1}`,
        factory_id: args.factory_id,
        agent_id: args.agent_id,
        agent_name: agentNames[args.agent_id] || args.agent_id,
        role: args.role || '',
        instructions: args.instructions || '',
        agent_proof: 'linked-stamp',
        profile_available: true,
        created_by_factory: false,
        config: {},
        capacity: 1,
        state: 'active',
        revision: 1,
      };
      state.members.push(created);
      return { member: clone(created) };
    }

    if (name === 'member.create') {
      if (!args.factory_id || !args.name || !args.request_id) {
        throw Object.assign(new Error('invalid_request'), {
          code: 'invalid_request',
        });
      }
      const created = {
        id: `mem-${state.members.length + 1}`,
        factory_id: args.factory_id,
        agent_id: args.agent_id || `custom-${state.members.length + 1}`,
        agent_name: args.name,
        role: args.role || '',
        instructions: args.instructions || '',
        agent_proof: 'spec-stamp',
        profile_available: true,
        created_by_factory: true,
        config: {},
        capacity: 1,
        state: 'active',
        revision: 1,
      };
      state.members.push(created);
      return { member: clone(created) };
    }

    if (name === 'member.update') {
      const m = findMember(args.id);
      const rest = { ...args };
      delete rest.id;
      delete rest.expected_revision;
      delete rest.clear_fields;
      Object.assign(m, rest, { revision: m.revision + 1 });
      return { member: clone(m) };
    }

    if (name === 'member.retire') {
      const m = findMember(args.id);
      m.state = 'retired';
      m.revision += 1;
      return { member: clone(m) };
    }

    if (name === 'conversation.get') {
      return clone(state.conversation);
    }

    if (name === 'conversation.send') {
      state.conversation.posts.push({
        id: `post-${state.conversation.posts.length + 1}`,
        author: 'user',
        text: args.text,
      });
      return clone(state.conversation);
    }

    if (name === 'dashboard.get') {
      const f = findFactory(args.factory_id);
      if (!f) throw Object.assign(new Error('missing'), { code: 'not_found' });
      const fMembers = state.members.filter((item) => item.factory_id === f.id);
      const fJobs = state.jobs
        .filter((item) => item.factory_id === f.id)
        .map(enrichJob);
      const fRuns = state.runs.filter((item) => item.factory_id === f.id);
      const upcoming = [];
      for (const job of fJobs) {
        if (!job.enabled) continue;
        for (const trg of job.triggers || []) {
          if (trg.type === 'schedule' && trg.enabled && trg.next_fire) {
            upcoming.push({
              job_id: job.id,
              job_name: job.name,
              trigger_id: trg.id,
              trigger_title: trg.title,
              schedule: trg.schedule,
              next_fire: trg.next_fire,
              agent_member_id: job.agent_member_id,
            });
          }
        }
      }
      return {
        factory: clone(f),
        members: clone(fMembers),
        jobs: fJobs,
        needs_attention: clone(
          fRuns.filter((item) =>
            ['needs_attention', 'failed'].includes(item.state),
          ),
        ),
        running: clone(
          fRuns.filter((item) => ['running', 'queued'].includes(item.state)),
        ),
        upcoming,
        recent_runs: clone(
          fRuns.filter((item) =>
            ['done', 'failed', 'cancelled'].includes(item.state),
          ),
        ),
      };
    }

    if (name === 'job.create') {
      const created = {
        id: `fjob-${state.jobs.length + 1}`,
        factory_id: args.factory_id,
        name: args.name,
        prompt: args.prompt,
        description: args.description || '',
        agent_member_id: args.agent_member_id || '',
        enabled: args.enabled !== false,
        triggers: clone(args.triggers || []),
        revision: 1,
      };
      state.jobs.push(created);
      return { job: enrichJob(created) };
    }

    if (name === 'job.update') {
      const job = findJob(args.job_id);
      if (
        args.expected_revision != null &&
        job.revision !== args.expected_revision
      ) {
        throw Object.assign(new Error('conflict'), {
          code: 'revision_conflict',
        });
      }
      const rest = { ...args };
      delete rest.job_id;
      delete rest.expected_revision;
      Object.assign(job, rest, { revision: job.revision + 1 });
      return { job: enrichJob(job) };
    }

    if (name === 'job.delete') {
      state.jobs = state.jobs.filter((item) => item.id !== args.job_id);
      return { deleted: true, job_id: args.job_id };
    }

    if (name === 'job.trigger') {
      const job = findJob(args.job_id);
      const run = {
        id: `jrun-${state.runs.length + 1}`,
        factory_id: job.factory_id,
        job_id: job.id,
        job_name: job.name,
        member_id: args.member_id || job.agent_member_id || 'mem-leader',
        state: 'queued',
        origin: 'manual',
        group_id: `fjob.jrun-${state.runs.length + 1}`,
        participant_id: 'worker',
        prompt: job.prompt,
        input_note: args.input_note || '',
        question: '',
        reason: '',
        summary: '',
        revision: 1,
        created_at: '2026-11-25T09:00:00Z',
        updated_at: '2026-11-25T09:00:00Z',
        finished_at: null,
      };
      state.runs.unshift(run);
      return { run: clone(run) };
    }

    if (name === 'job.secret') {
      const key = `${args.job_id}/${args.trigger_id}`;
      if (args.rotate || !state.secrets[key]) {
        state.secrets[key] =
          `secret-${Object.keys(state.secrets).length + (args.rotate ? 10 : 1)}`;
      }
      return { secret: state.secrets[key] };
    }

    if (name === 'job_run.get') {
      const run = findRun(args.run_id);
      if (!run)
        throw Object.assign(new Error('missing'), { code: 'not_found' });
      return { run: clone(run) };
    }

    if (name === 'job_run.reply') {
      const run = findRun(args.run_id);
      run.state = 'queued';
      run.question = '';
      run.reason = '';
      run.revision += 1;
      return { run: clone(run) };
    }

    if (name === 'job_run.cancel') {
      const run = findRun(args.run_id);
      run.state = 'cancelled';
      run.revision += 1;
      return { run: clone(run) };
    }

    throw new Error(`Unexpected test operation: ${name}`);
  });

  const goTo = (route) => {
    context = { ...context, route, locale: 'en', timezone: 'UTC', theme: {} };
    for (const callback of contexts) callback(context);
    return Promise.resolve({});
  };

  const bridge = {
    operation,
    get context() {
      return context;
    },
    show: (route = '') => goTo(route),
    pushRoute: vi.fn(async (route) => {
      if (!autosave || (await autosave.flush())) await goTo(route);
      return {};
    }),
    replaceRoute: vi.fn(goTo),
    onContext(callback) {
      contexts.add(callback);
      return () => contexts.delete(callback);
    },
    onInvalidation(callback) {
      invalidations.add(callback);
      return () => invalidations.delete(callback);
    },
    invalidate(change = null, reason = null) {
      for (const callback of invalidations) callback({ reason, change });
    },
    registerLayer(layer) {
      layers.push(layer);
      return () => {
        const index = layers.indexOf(layer);
        if (index >= 0) layers.splice(index, 1);
      };
    },
    registerAutosave: vi.fn((participant) => {
      autosave = participant;
      return () => {
        if (autosave === participant) autosave = null;
      };
    }),
    notifyAutosave: vi.fn(),
    openSession: vi.fn().mockResolvedValue({}),
    openAgent: vi.fn().mockResolvedValue({}),
    openLink: vi.fn().mockResolvedValue({}),
    readModelFavorites: vi.fn().mockResolvedValue({ models: [] }),
    setModelFavorite: vi.fn().mockResolvedValue({ models: [] }),
    readHistory: vi.fn().mockResolvedValue({
      messages: [],
      context_usage: { tokens: 12_800, context_window: 128_000 },
      session_usage: { input_tokens: 12_800, output_tokens: 1_200 },
      run_usage: null,
    }),
    mountSession: vi.fn(() => vi.fn()),
    dispose: vi.fn(),
  };

  return { bridge, operation, state };
}

export async function settle() {
  await vi.dynamicImportSettled();
  await tick();
  flushSync();
  await Promise.resolve();
  await tick();
  flushSync();
}

export async function render(bridge, route = '') {
  mounted = mount(FactoryPage, {
    target: document.body,
    props: { bridgeClient: bridge },
  });
  flushSync();
  bridge.show(route);
  await settle();
}

export function button(label, root = document) {
  return [...root.querySelectorAll('button')].find(
    (node) =>
      (node.getAttribute('aria-label') || node.textContent).trim() === label,
  );
}

export function fill(id, value) {
  const input = document.getElementById(id);
  expect(input).not.toBeNull();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  return input;
}

export async function choose(id, text) {
  document.getElementById(id).click();
  await tick();
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (node) => node.textContent.trim().startsWith(text),
  );
  expect(option).toBeDefined();
  option.click();
  await tick();
}

export const calls = (operation, name) =>
  operation.mock.calls.filter(([called]) => called === name);

export { t, tick, flushSync };

afterEach(async () => {
  if (mounted) {
    await unmount(mounted);
    mounted = null;
  }
  document.body.innerHTML = '';
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
