// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  button,
  calls,
  choose,
  createFactoryBridge,
  fill,
  render,
  settle,
  t,
} from './FactoryPage.support.js';

describe('FactoryPage', () => {
  it('sets up a new Factory for a Project without one', async () => {
    const { bridge, operation } = createFactoryBridge({ established: false });
    await render(bridge, '/project/project-b');

    expect(document.body.textContent).toContain(t('factory.noFactory'));
    button(t('factory.setup.button')).click();
    await settle();

    fill('factory-setup-leader-name', 'Atlas');
    await choose('factory-setup-model', 'demo/worker');
    fill('factory-setup-remit', 'Ship project B');
    button(
      t('factory.setup.button'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();

    expect(calls(operation, 'factory.create')[0][1]).toMatchObject({
      project_id: 'project-b',
      leader_name: 'Atlas',
      leader_model: 'demo/worker',
      remit: 'Ship project B',
    });
    expect(document.body.textContent).toContain('Beta Factory');
  });

  it('renders Overview with Leader Chat, Needs Attention reply, Running cancel, Upcoming, and Recent runs', async () => {
    const { bridge, operation } = createFactoryBridge();
    await render(bridge, '/factory/fac-one');

    // Leader chat: Markdown classes, Context Wheel, direct Open Session, New Session, no Edit Agent
    const chatSection = document.querySelector('.chat');
    expect(chatSection?.textContent).toContain('Ready to coordinate.');
    expect(
      chatSection?.querySelector('.messages.md-chat .msg-markdown'),
    ).not.toBeNull();
    expect(button(t('factory.chat.editAgent'), chatSection)).toBeUndefined();
    expect(button(t('factory.chat.new'), chatSection)).toBeDefined();
    expect(
      chatSection?.querySelector('.context-ring-trigger')?.textContent,
    ).toContain('10%');
    button(t('factory.chat.openSession'), chatSection).click();
    expect(bridge.openSession).toHaveBeenCalledWith({
      agent_id: 'planner',
      project_id: 'project-a',
      session_id: 'sess-leader',
    });
    expect(bridge.mountSession).not.toHaveBeenCalled();

    const chatInput = document.querySelector('.factory-chat-input');
    chatInput.value = 'Plan next release';
    chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    await settle();
    button(t('factory.chat.send')).click();
    await settle();
    expect(calls(operation, 'conversation.send')[0][1]).toMatchObject({
      project_id: 'project-a',
      text: 'Plan next release',
    });

    // Dashboard sections (Recent Job Runs only lists finished runs)
    expect(document.body.textContent).toContain(
      'Should I proceed with migration?',
    );
    expect(document.body.textContent).toContain('All 42 checks passed.');
    expect(document.body.textContent).toContain('0 9 * * 1-5');
    const recentPanel = document.querySelector(
      `[aria-label="${t('factory.dashboard.recent')}"]`,
    );
    expect(recentPanel?.textContent).toContain('All 42 checks passed.');
    expect(recentPanel?.textContent).not.toContain('Release Notes');

    // Inline reply on needs_attention run
    fill('reply-jrun-attn', 'Yes, proceed');
    button(t('factory.dashboard.reply')).click();
    await settle();
    expect(calls(operation, 'job_run.reply')[0][1]).toEqual({
      run_id: 'jrun-attn',
      text: 'Yes, proceed',
    });

    // Cancel on running run
    const runningPanel = document.querySelector(
      `[aria-label="${t('factory.dashboard.running')}"]`,
    );
    const activeRow = [
      ...runningPanel.querySelectorAll('.factory-dash-item'),
    ].find((node) => node.textContent.includes('Release Notes'));
    button(t('factory.dashboard.cancel'), activeRow).click();
    await settle();
    expect(calls(operation, 'job_run.cancel')[0][1]).toEqual({
      run_id: 'jrun-active',
    });
  });

  it('manages Jobs, triggers one-click runs, creates Jobs with triggers, and reveals webhook secrets', async () => {
    const { bridge, operation } = createFactoryBridge();
    await render(bridge, '/factory/fac-one/jobs');

    expect(document.body.textContent).toContain('Nightly Check');
    expect(document.body.textContent).toContain(t('factory.jobs.automatic'));
    expect(document.body.textContent).toContain('Release Notes');
    expect(document.body.textContent).toContain(t('factory.jobs.onDemand'));
    expect(document.body.textContent).toContain(
      t('factory.jobs.leaderDefault'),
    );

    // One-click Trigger button
    const firstJobCard = document.querySelector('[data-job-id="fjob-auto"]');
    button(t('factory.jobs.triggerNow'), firstJobCard).click();
    await settle();
    expect(calls(operation, 'job.trigger')[0][1]).toEqual({
      job_id: 'fjob-auto',
    });
    expect(document.body.textContent).toContain(t('factory.jobs.triggered'));

    // Create a new Job with a webhook trigger
    button(t('factory.jobs.new')).click();
    await settle();
    fill('job-name', 'PR Triage');
    fill('job-prompt', 'Triage incoming PR');
    fill('job-description', 'Runs on webhook');
    button(t('factory.jobs.addWebhook')).click();
    await settle();
    button(t('factory.jobs.save')).click();
    await settle();

    expect(calls(operation, 'job.create')[0][1]).toMatchObject({
      factory_id: 'fac-one',
      name: 'PR Triage',
      prompt: 'Triage incoming PR',
      description: 'Runs on webhook',
      agent_member_id: '',
      enabled: true,
    });

    // Edit newly created Job to reveal & rotate webhook secret
    const createdCard = [
      ...document.querySelectorAll('.factory-job-card'),
    ].find((node) => node.textContent.includes('PR Triage'));
    button(t('factory.jobs.edit'), createdCard).click();
    await settle();
    expect(document.querySelector('[data-webhook-url]')?.textContent).toContain(
      '/hooks/',
    );
    button(t('factory.trigger.showSecret')).click();
    await settle();
    expect(calls(operation, 'job.secret')[0][1]).toMatchObject({
      factory_id: 'fac-one',
    });
    expect(
      document.querySelector('[data-webhook-secret]')?.textContent,
    ).toContain('secret-');
  });

  it('renders Job Run detail with SessionEmbed, Open in Chat, reply, and cancel', async () => {
    const { bridge, operation } = createFactoryBridge();
    await render(bridge, '/job-run/jrun-attn');

    expect(document.body.textContent).toContain('Nightly Check');
    expect(document.body.textContent).toContain(
      'Should I proceed with migration?',
    );
    expect(bridge.mountSession).toHaveBeenCalled();

    button(t('factory.run.openInChat')).click();
    expect(bridge.openSession).toHaveBeenCalledWith({
      agent_id: 'builder',
      project_id: 'project-a',
      session_id: 'sess-attn',
    });

    fill('job-run-reply', 'Proceed now');
    button(t('factory.run.sendReply')).click();
    await settle();
    expect(calls(operation, 'job_run.reply')[0][1]).toEqual({
      run_id: 'jrun-attn',
      text: 'Proceed now',
    });
  });

  it('manages Team members and Factory Settings', async () => {
    const { bridge, operation } = createFactoryBridge();
    await render(bridge, '/factory/fac-one/team');

    expect(document.body.textContent).toContain('Planner');
    expect(document.body.textContent).toContain('Builder');
    expect(document.body.textContent).toContain('Coordinate work');

    // Member role & instructions editor omits redundant raw tool/skill boundary fields
    button(t('factory.team.editMembership')).click();
    await settle();
    expect(document.getElementById('member-role-mem-leader')).not.toBeNull();
    expect(
      document.getElementById('member-instructions-mem-leader'),
    ).not.toBeNull();
    expect(
      document.getElementById('member-allowed-skills-mem-leader'),
    ).toBeNull();
    expect(
      document.getElementById('member-allowed-tools-mem-leader'),
    ).toBeNull();
    expect(
      document.getElementById('member-denied-tools-mem-leader'),
    ).toBeNull();

    button(t('factory.team.addMember')).click();
    await settle();
    await choose('member-existing-agent', 'Reviewer');
    fill('member-role', 'Code Reviewer');
    button(
      t('factory.team.addMember'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();

    expect(calls(operation, 'member.add')[0][1]).toMatchObject({
      factory_id: 'fac-one',
      agent_id: 'reviewer',
      role: 'Code Reviewer',
    });
    expect(calls(operation, 'member.create')).toHaveLength(0);

    // Create a new Agent specialist
    button(t('factory.team.addMember')).click();
    await settle();
    document
      .querySelector(
        `[role="dialog"] [aria-label="${t('factory.team.newAgent')}"]`,
      )
      .click();
    await settle();
    fill('member-new-name', 'Tester');
    await choose('member-new-model', 'demo/worker');
    fill('member-role', 'QA');
    button(
      t('factory.team.addMember'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();

    expect(calls(operation, 'member.create')[0][1]).toMatchObject({
      factory_id: 'fac-one',
      name: 'Tester',
      model: 'demo/worker',
      role: 'QA',
      request_id: expect.any(String),
    });
    expect(document.body.textContent).toContain('Tester');

    // Settings tab & Archive (requires confirmation)
    button(t('factory.tabs.settings')).click();
    await settle();
    expect(document.getElementById('factory-settings-name')?.value).toBe(
      'Alpha Factory',
    );
    button(t('factory.settings.archive')).click();
    await settle();
    expect(calls(operation, 'factory.archive')).toHaveLength(0);
    button(
      t('factory.settings.archive'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();
    expect(calls(operation, 'factory.archive')[0][1]).toMatchObject({
      id: 'fac-one',
    });
  });

  it('reloads target data when navigating from Overview to a Job Run detail route', async () => {
    const { bridge, operation } = createFactoryBridge();
    await render(bridge, '/factory/fac-one');

    expect(calls(operation, 'job_run.get')).toHaveLength(0);

    button(t('factory.dashboard.inspect')).click();
    await settle();

    expect(calls(operation, 'job_run.get')).toHaveLength(1);
    expect(calls(operation, 'job_run.get')[0][1]).toEqual({
      run_id: 'jrun-attn',
    });
    expect(document.body.textContent).toContain(
      'Should I proceed with migration?',
    );
  });

  it('waits to mount SessionEmbed on queued Job Runs until session_address is set', async () => {
    const { bridge, state } = createFactoryBridge();
    state.runs.push({
      id: 'jrun-queued',
      factory_id: 'fac-one',
      job_id: 'fjob-manual',
      job_name: 'Release Notes',
      member_id: 'mem-leader',
      state: 'queued',
      origin: 'manual',
      group_id: 'fjob.jrun-queued',
      participant_id: 'worker',
      prompt: 'Draft release notes',
      input_note: '',
      question: '',
      reason: '',
      summary: '',
      session_address: null,
      revision: 1,
      created_at: '2026-11-25T09:10:00Z',
      updated_at: '2026-11-25T09:10:00Z',
      finished_at: null,
    });

    await render(bridge, '/job-run/jrun-queued');

    expect(bridge.mountSession).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain(
      t('factory.run.sessionPending'),
    );

    const run = state.runs.find((item) => item.id === 'jrun-queued');
    run.state = 'running';
    run.session_address = {
      agent_id: 'planner',
      project_id: 'project-a',
      session_id: 'sess-queued',
    };
    bridge.invalidate({ resource: 'job_runs' });
    await settle();

    expect(bridge.mountSession).toHaveBeenCalled();
  });

  it('strips Markdown noise while preserving identifiers in Recent Job Runs previews', async () => {
    const { bridge, state } = createFactoryBridge();
    const recent = state.runs.find((item) => item.id === 'jrun-done');
    recent.summary =
      '## Review: `get_for_session` **Verdict: approved** — see [details](https://example.com).';

    await render(bridge, '/factory/fac-one');

    const recentPanel = document.querySelector(
      `[aria-label="${t('factory.dashboard.recent')}"]`,
    );
    const details = [
      ...(recentPanel?.querySelectorAll('.factory-dash-detail') ?? []),
    ];
    const detail = details.find((node) =>
      node.textContent?.includes('get_for_session'),
    );
    expect(detail?.textContent?.trim()).toBe(
      'Review: get_for_session Verdict: approved — see details.',
    );
    expect(detail?.classList.contains('factory-dash-detail--clamped')).toBe(
      true,
    );
  });
});
