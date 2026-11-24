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

    // Leader chat
    expect(document.body.textContent).toContain('Ready to coordinate.');
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

    // Dashboard sections
    expect(document.body.textContent).toContain(
      'Should I proceed with migration?',
    );
    expect(document.body.textContent).toContain('All 42 checks passed.');
    expect(document.body.textContent).toContain('0 9 * * 1-5');

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

    button(t('factory.team.addMember')).click();
    await settle();
    await choose('member-existing-agent', 'Reviewer');
    fill('member-role', 'Code Reviewer');
    button(
      t('factory.team.addMember'),
      document.querySelector('[role="dialog"]'),
    ).click();
    await settle();

    expect(calls(operation, 'member.create')[0][1]).toMatchObject({
      factory_id: 'fac-one',
      agent_id: 'reviewer',
      role: 'Code Reviewer',
    });

    // Settings tab & Archive
    button(t('factory.tabs.settings')).click();
    await settle();
    expect(document.getElementById('factory-settings-name')?.value).toBe(
      'Alpha Factory',
    );
    button(t('factory.settings.archive')).click();
    await settle();
    expect(calls(operation, 'factory.archive')[0][1]).toMatchObject({
      id: 'fac-one',
    });
  });
});
