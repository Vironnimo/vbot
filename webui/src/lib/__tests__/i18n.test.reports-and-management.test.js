import { describe, expect, it } from 'vitest';
import { englishCatalog, t } from '../i18n.js';
import { expectCatalogKeys } from './i18n.support.js';

describe('i18n t()', () => {
  it('contains Logs tab copy for navigation, filters, and states', () => {
    const requiredKeys = [
      'navigation.logs',
      'logs.title',
      'logs.eyebrow',
      'logs.subtitle',
      'logs.file',
      'logs.emptyOption',
      'logs.levelFilter',
      'logs.sort',
      'logs.sort.newest',
      'logs.sort.oldest',
      'logs.level.all',
      'logs.level.info',
      'logs.level.warn',
      'logs.level.warning',
      'logs.level.error',
      'logs.level.unknown',
      'logs.search',
      'logs.searchPlaceholder',
      'logs.resultsCount',
      'logs.currentFile',
      'logs.entries',
      'logs.copyEntry',
      'logs.copied',
      'logs.loadingCatalog',
      'logs.loadingFile',
      'logs.emptyTitle',
      'logs.emptySubtitle',
      'logs.fileEmptyTitle',
      'logs.fileEmptySubtitle',
      'logs.noMatchesTitle',
      'logs.noMatchesSubtitle',
      'logs.catalogLoadError',
      'logs.readError',
      'logs.streamError',
      'logs.stream.connecting',
      'logs.stream.connected',
      'logs.stream.reconnecting',
      'logs.stream.error',
      'logs.stream.idle',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.logs')).toBe('Logs');
    expect(t('logs.resultsCount', undefined, { count: 7 })).toBe(
      '7 visible entries',
    );
    expect(t('logs.currentFile', undefined, { file: '2026-05-11.log' })).toBe(
      'Current file: 2026-05-11.log',
    );
    expect(t('logs.level.warn')).toBe('WARN');
    expect(t('logs.level.error')).toBe('ERROR');
    expect(t('logs.sort')).toBe('Order');
    expect(t('logs.sort.newest')).toBe('Newest first');
    expect(t('logs.sort.oldest')).toBe('Oldest first');
    expect(t('logs.searchPlaceholder')).toContain('logger');
    expect(t('logs.stream.connected')).toBe('Live');
    expect(t('logs.stream.error')).toBe('Live update error');
    expect(t('logs.copyEntry')).toBe('Copy log line');
    expect(t('logs.copied')).toBe('Copied');
  });

  it('contains Debug i18n copy with a meaningful empty heading and matching interpolation tokens', () => {
    const requiredKeys = [
      'debug.eyebrow',
      'debug.title',
      'debug.subtitle',
      'debug.statusCount',
      'debug.traceLimit',
      'debug.localWarning',
      'debug.emptyHeader',
      'debug.emptyState',
      'debug.clearConfirm',
      'debug.traceList',
      'debug.metadata',
      'debug.request',
      'debug.requestMethod',
      'debug.requestUrl',
      'debug.requestHeaders',
      'debug.requestBody',
      'debug.response',
      'debug.responseStatus',
      'debug.responseHeaders',
      'debug.responseBody',
      'debug.streamRaw',
      'debug.streamParsed',
      'debug.modelProbe',
      'debug.modelProbe.provider',
      'debug.modelProbe.connection',
      'debug.modelProbe.selectProvider',
      'debug.modelProbe.selectConnection',
      'debug.modelProbe.run',
      'debug.modelProbe.rawResponse',
      'debug.modelProbe.normalizedPreview',
      'debug.modelProbe.modelCount',
      'debug.expandRow',
      'debug.collapseRow',
    ];

    expectCatalogKeys(requiredKeys);

    // The removed stream-event copy must not return to the catalog.
    expect(englishCatalog['debug.streamEvents']).toBeUndefined();
    expect(englishCatalog['debug.streamEventIndex']).toBeUndefined();
    expect(englishCatalog['debug.noStreamEvents']).toBeUndefined();
    expect(t('debug.streamEvents')).toBe('debug.streamEvents');
    expect(t('debug.streamEventIndex')).toBe('debug.streamEventIndex');
    expect(t('debug.noStreamEvents')).toBe('debug.noStreamEvents');

    // The empty heading must be meaningful copy, never the bogus "(none)" placeholder.
    expect(t('debug.emptyHeader')).not.toBe('(none)');
    expect(t('debug.emptyHeader').trim().length).toBeGreaterThan(0);
    expect(t('debug.emptyHeader')).toBe('No traces captured yet');

    // The Debug subtitle must describe request/response inspection and must
    // not instruct users to inspect individual stream events.
    expect(t('debug.subtitle')).toMatch(/request/i);
    expect(t('debug.subtitle')).toMatch(/response/i);
    expect(t('debug.subtitle').toLowerCase()).not.toContain('stream event');

    expect(t('debug.statusCount', undefined, { count: 4, limit: 50 })).toBe(
      'Traces: 4 / 50',
    );
    expect(t('debug.modelProbe.modelCount', undefined, { count: 12 })).toBe(
      '12 models',
    );

    expect(t('debug.emptyState')).toContain('debug');
    expect(t('debug.emptyState').length).toBeGreaterThan(20);

    // Trace row expand/collapse aria labels must be non-empty catalog copy so
    // screen readers and tooltips don't fall back to the component hardcoded
    // default strings.
    expect(t('debug.expandRow').trim().length).toBeGreaterThan(0);
    expect(t('debug.collapseRow').trim().length).toBeGreaterThan(0);
    expect(t('debug.expandRow')).toBe('Expand row');
    expect(t('debug.collapseRow')).toBe('Collapse row');
    expect(t('debug.expandRow')).not.toBe(t('debug.collapseRow'));
  });

  it('contains Statistics tab copy for navigation, sub-views, and metrics', () => {
    const requiredKeys = [
      'navigation.statistics',
      'statistics.eyebrow',
      'statistics.title',
      'statistics.subtitle',
      'statistics.loading',
      'statistics.loadError',
      'statistics.empty',
      'statistics.none',
      'statistics.generatedAt',
      'statistics.estimatedBadge',
      'statistics.estimatedHint',
      'statistics.derivedHint',
      'statistics.subview.overview',
      'statistics.subview.usage',
      'statistics.subview.runs',
      'statistics.subview.tools',
      'statistics.subview.skills',
      'statistics.granularity.day',
      'statistics.granularity.week',
      'statistics.granularity.month',
      'statistics.status.completed',
      'statistics.status.failed',
      'statistics.status.cancelled',
      'statistics.role.assistant',
      'statistics.role.run_summary',
      'statistics.role.agent_takeover',
      'statistics.overview.agents',
      'statistics.overview.runs',
      'statistics.overview.chatMessages',
      'statistics.overview.chatMessagesByRole',
      'statistics.overview.sessionRecords',
      'statistics.overview.sessionRecordsHint',
      'statistics.overview.runHealth',
      'statistics.overview.totalRuns',
      'statistics.overview.completedLabel',
      'statistics.overview.statusAria',
      'statistics.overview.nonCompleted',
      'statistics.overview.activityReliability',
      'statistics.overview.activityWindow.day',
      'statistics.overview.activityWindow.week',
      'statistics.overview.activityWindow.month',
      'statistics.overview.noActivityPeriod',
      'statistics.overview.periodRuns',
      'statistics.overview.completionRate',
      'statistics.overview.peak',
      'statistics.overview.weekOf',
      'statistics.overview.activityTooltip',
      'statistics.overview.activityAria',
      'statistics.overview.chatMessagesHint',
      'statistics.overview.modelSteps',
      'statistics.overview.modelStepsHint',
      'statistics.usage.measuredTokens',
      'statistics.usage.estimatedTokens',
      'statistics.usage.cacheIntro',
      'statistics.usage.runAttributionHint',
      'statistics.usage.providers',
      'statistics.usage.models',
      'statistics.runs.p50Hint',
      'statistics.runs.p90Hint',
      'statistics.runs.p95Hint',
      'statistics.runs.cancelRate',
      'statistics.runs.failureRate',
      'statistics.runs.fallbackRuns',
      'statistics.runs.avgAgentMessagesPerRun',
      'statistics.runs.avgAgentMessagesHint',
      'statistics.runs.avgModelStepsPerRun',
      'statistics.runs.avgModelStepsHint',
      'statistics.runs.longest',
      'statistics.errors.byKind',
      'statistics.errors.byHour',
      'statistics.errors.scopeHint',
      'statistics.tools.perTool',
      'statistics.tools.outcomeNote',
      'statistics.skills.total',
      'statistics.skills.used',
      'statistics.skills.offeredUnactivated',
      'statistics.skills.withoutOfferData',
      'statistics.skills.intro',
      'statistics.skills.perSkill',
      'statistics.skills.empty',
      'statistics.skills.neverUsedBadge',
      'statistics.skills.neverUsedRowTitle',
      'statistics.skills.noOfferDataBadge',
      'statistics.skills.noOfferDataRowTitle',
      'statistics.skills.byAgent',
      'statistics.skills.origin.bundled',
      'statistics.skills.origin.global',
      'statistics.skills.origin.agent',
      'statistics.skills.origin.project',
      'statistics.col.tokens',
      'statistics.col.share',
      'statistics.col.skill',
      'statistics.col.origins',
      'statistics.col.offered',
      'statistics.col.activated',
      'statistics.col.usageRate',
      'statistics.col.firstActivated',
      'statistics.col.lastActivated',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.statistics')).toBe('Statistics');
    expect(t('statistics.generatedAt', undefined, { time: '12:00' })).toBe(
      'Generated 12:00',
    );
    expect(t('statistics.estimatedBadge')).toContain('estimated');
    expect(t('statistics.subview.runs')).toBe('Runs & errors');
    expect(t('statistics.subview.skills')).toBe('Skills');
    // The scoped origin labels interpolate the agent id / project name detail.
    expect(
      t('statistics.skills.origin.agent', undefined, { detail: 'assistant' }),
    ).toBe('agent: assistant');
    expect(
      t('statistics.skills.origin.project', undefined, { detail: 'vBot' }),
    ).toBe('project: vBot');
  });

  it('contains the project-agent badge label for the statistics tab', () => {
    expect(englishCatalog['statistics.agent.projectBadgeTitle']).toBeTruthy();
    expect(
      t('statistics.agent.projectBadgeTitle', undefined, { project: 'vbot' }),
    ).toBe('Project: vbot');
  });

  it('contains cron project-agent dropdown group labels', () => {
    expectCatalogKeys([
      'cron.form.agentGroup.identity',
      'cron.form.agentGroup.project',
    ]);
    expect(t('cron.form.agentGroup.identity')).toBe('Identity agents');
    expect(t('cron.form.agentGroup.project')).toBe('Project agents');
  });

  it('contains cron master-detail labels, info rows, and schedule presets', () => {
    expectCatalogKeys([
      'cron.list.ariaLabel',
      'cron.detail.createTitle',
      'cron.detail.editTitle',
      'cron.detail.status',
      'cron.detail.lastFired',
      'cron.detail.nextFire',
      'cron.form.preset',
      'cron.presets.custom',
      'cron.presets.every15Minutes',
      'cron.presets.hourly',
      'cron.presets.dailyMorning',
      'cron.presets.weekdayMornings',
      'cron.presets.mondayMornings',
      'cron.presets.monthlyFirst',
    ]);
    expect(t('cron.list.ariaLabel')).toBe('Scheduled Runs');
    expect(t('cron.detail.createTitle')).toBe('Create Scheduled Run');
    expect(t('cron.detail.editTitle')).toBe('Edit Scheduled Run');
    expect(t('cron.form.preset')).toBe('Schedule preset');
    expect(t('cron.presets.custom')).toBe('Custom');
    expect(t('cron.presets.every15Minutes')).toBe('Every 15 minutes');
    expect(t('cron.presets.monthlyFirst')).toBe('Monthly on the 1st at 9:00');
  });

  it('drops the retired cron table headers, modal titles, and edit action', () => {
    // The table+modal rebuild removed these — the master-detail view reuses none
    // of them, and the create/edit heading moved under `cron.detail.*`.
    for (const retiredKey of [
      'cron.table.caption',
      'cron.table.agent',
      'cron.table.prompt',
      'cron.table.schedule',
      'cron.table.timezone',
      'cron.table.status',
      'cron.table.lastFired',
      'cron.table.nextFire',
      'cron.table.actions',
      'cron.detail.timezone',
      'cron.form.timezone',
      'cron.form.timezonePlaceholder',
      'cron.form.timezoneSearch',
      'cron.form.timezoneHelp',
      'cron.modal.createTitle',
      'cron.modal.editTitle',
      'cron.actions.editJob',
    ]) {
      expect(englishCatalog[retiredKey], retiredKey).toBeUndefined();
    }
  });

  it('contains Projects tab copy for navigation, add, list, manage, report, and re-point', () => {
    const requiredKeys = [
      'navigation.projects',
      'projects.eyebrow',
      'projects.title',
      'projects.subtitle',
      'projects.loading',
      'projects.loadError',
      'projects.emptyTitle',
      'projects.emptySubtitle',
      'projects.add.title',
      'projects.add.subtitle',
      'projects.add.cwd',
      'projects.add.cwdPlaceholder',
      'projects.add.cwdHelp',
      'projects.add.displayName',
      'projects.add.submit',
      'projects.add.submitting',
      'projects.add.missingCwd',
      'projects.add.error',
      'projects.add.success',
      'projects.list.title',
      'projects.manage.displayName',
      'projects.manage.defaultAgent',
      'projects.manage.defaultModel',
      'projects.manage.autoLoad',
      'projects.manage.save',
      'projects.manage.saving',
      'projects.manage.saveError',
      'projects.manage.saveSuccess',
      'projects.manage.unavailableToolHint',
      'projects.remove',
      'projects.remove.confirm',
      'projects.remove.error',
      'projects.remove.success',
      'projects.remove.busy',
      'projects.remove.inUse',
      'projects.team.title',
      'projects.repository.rescan',
      'projects.repository.rescanning',
      'projects.team.empty',
      'projects.team.noModel',
      'projects.report.title',
      'projects.report.findingCount',
      'projects.report.group.slug_collision',
      'projects.report.group.unslugifiable_name',
      'projects.report.group.bad_model',
      'projects.report.group.orphan',
      'projects.report.group.unavailable_tool',
      'projects.report.finding.agent',
      'projects.report.finding.source',
      'projects.rePoint.title',
      'projects.rePoint.description',
      'projects.rePoint.cwd',
      'projects.rePoint.submit',
      'projects.rePoint.submitting',
      'projects.rePoint.missingCwd',
      'projects.rePoint.error',
      'projects.rePoint.success',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.projects')).toBe('Projects');
    expect(t('projects.report.findingCount', undefined, { count: 3 })).toBe(
      '3 issues found',
    );
    expect(
      t('projects.report.finding.agent', undefined, { agentId: 'builder' }),
    ).toBe('Agent builder');
  });

  it('contains two-bar project chat copy for the dropdown, team bar, and scan banner', () => {
    const requiredKeys = [
      'chat.project.none',
      'chat.personalBarLabel',
      'chat.personalBarHint',
      'chat.project.selectAria',
      'chat.project.teamLabel',
      'chat.project.teamBarHint',
      'chat.project.teamEmpty',
      'chat.project.loadError',
      'chat.project.sessionError',
      'chat.project.scanBanner',
      'chat.project.scanBannerCount',
      'chat.project.scanBannerLink',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('chat.project.none')).toBe('No project selected');
    expect(t('chat.personalBarLabel')).toBe('Personal');
    expect(
      t('chat.project.scanBannerCount', undefined, { count: 2 }),
    ).toContain('2');
    expect(t('chat.project.scanBannerLink').toLowerCase()).toContain('project');
  });

  it('contains confirm-dialog titles, verbs, and consequence bodies', () => {
    const requiredKeys = [
      'common.reset',
      'projects.remove.confirmTitle',
      'sessions.delete_confirm_title',
      'cron.deleteConfirmTitle',
      'settings.channels.delete_confirm_title',
      'settings.channels.delete_confirm',
      'settings.skills.deleteConfirmTitle',
      'settings.skills.deleteConfirm',
      'systemPrompt.fragmentEditor.resetConfirmTitle',
      'systemPrompt.blockList.removeConfirmTitle',
      'systemPrompt.blockList.resetLayoutConfirmTitle',
    ];

    expectCatalogKeys(requiredKeys);

    // Confirm buttons carry the action verb; titles name the entity.
    expect(t('common.reset')).toBe('Reset');
    expect(t('projects.remove.confirmTitle')).toBe('Remove project');
    expect(t('sessions.delete_confirm_title')).toBe('Delete session');
    expect(t('cron.deleteConfirmTitle')).toBe('Delete Scheduled Run');
    expect(t('settings.channels.delete_confirm_title')).toBe('Delete channel');
    expect(t('settings.skills.deleteConfirmTitle')).toBe('Delete skill');
    expect(t('systemPrompt.fragmentEditor.resetConfirmTitle')).toBe(
      'Reset block',
    );
    expect(t('systemPrompt.blockList.removeConfirmTitle')).toBe('Remove block');
    expect(t('systemPrompt.blockList.resetLayoutConfirmTitle')).toBe(
      'Reset layout',
    );

    // The rewritten permanent-delete bodies state the consequence honestly and
    // interpolate the entity id/name.
    expect(
      t('settings.channels.delete_confirm', undefined, { id: 'tg-main' }),
    ).toBe(
      'Delete channel "tg-main" permanently? vBot stops listening on it and its configuration is removed.',
    );
    expect(
      t('settings.skills.deleteConfirm', undefined, { name: 'deploy' }),
    ).toBe(
      'Delete skill “deploy” permanently? The skill file is removed from disk.',
    );
  });

  it('contains the Extensions reload help', () => {
    expectCatalogKeys(['settings.extensions.reloadHelp']);
    expect(t('settings.extensions.reloadHelp')).toBe(
      'Rebuilds all extensions from disk — picks up code edits, new and removed extensions.',
    );
  });

  it('contains the shared toggle-chip allow-list copy', () => {
    expectCatalogKeys([
      'access.searchPlaceholder',
      'access.count',
      'access.allOn',
      'access.allOff',
      'access.noMatches',
      'access.toggle',
      'access.lockedAuto',
    ]);
    expect(t('access.count', undefined, { on: 3, total: 12 })).toBe(
      '3 / 12 on',
    );
    expect(t('access.toggle', undefined, { name: 'bash' })).toBe('Toggle bash');
    expect(t('access.allOn')).toBe('all on');
    expect(t('access.allOff')).toBe('all off');
  });

  it('does not expose Components showcase labels in the live catalog', () => {
    expect(englishCatalog['components.title']).toBeUndefined();
    expect(englishCatalog['components.toast.errorMessage']).toBeUndefined();
    expect(t('components.title')).toBe('components.title');
  });
});
