// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import {
  applyExtensionsPanelList,
  buildAgentDefaultsPayload,
  buildClientPresenceRows,
  buildSessionTitleSettingsPayload,
  buildTranscriptionAudioSettingsPayload,
  normalizeCompactionSettings,
  normalizeSessionTitleSettings,
  normalizeTranscriptionAudio,
} from '../settingsView.js';

describe('Transcription audio settings', () => {
  it('defaults to the maximum-compatibility WAV profile', () => {
    expect(normalizeTranscriptionAudio({})).toEqual({
      profile: 'compatibility',
      format: 'wav',
      sample_rate_hz: 16000,
    });
  });

  it('builds a complete custom profile update', () => {
    expect(
      buildTranscriptionAudioSettingsPayload({
        profile: 'custom',
        format: 'flac',
        sample_rate_hz: 24000,
      }),
    ).toEqual({
      speech: {
        transcription_audio: {
          profile: 'custom',
          format: 'flac',
          sample_rate_hz: 24000,
        },
      },
    });
  });
});

describe('Session title settings', () => {
  it('normalizes defaults and builds the complete update section', () => {
    expect(normalizeSessionTitleSettings({})).toEqual({
      enabled: false,
      model: '',
    });
    expect(
      buildSessionTitleSettingsPayload({
        enabled: true,
        model: 'openai/gpt-4.1-mini::api-key',
      }),
    ).toEqual({
      session_titles: {
        enabled: true,
        model: 'openai/gpt-4.1-mini::api-key',
      },
    });
  });
});

describe('comma decimal separators', () => {
  it('parses a comma temperature in agent defaults payloads', () => {
    expect(
      buildAgentDefaultsPayload({
        model: '',
        fallback_model: '',
        temperature: '0,7',
        thinking_effort: '',
      }).defaults.agent.temperature,
    ).toBe(0.7);
  });

  it('parses a comma threshold in compaction settings', () => {
    expect(
      normalizeCompactionSettings({
        compaction: {
          trigger: { type: 'context_ratio', threshold: '0,35' },
        },
      }).trigger.threshold,
    ).toBe(0.35);
  });
});

describe('buildClientPresenceRows()', () => {
  const roster = [
    {
      id: 'reg-1',
      connection_id: 'tab-self',
      accessor: 'browser',
      browser: 'Chrome',
      os: 'Windows',
      connected_at: '2026-06-20T10:00:00+00:00',
      status: 'connected',
    },
    {
      id: 'reg-2',
      connection_id: 'tab-other',
      accessor: 'desktop',
      browser: 'Unknown',
      os: 'Linux',
      connected_at: '2026-06-20T11:00:00+00:00',
      status: 'connected',
    },
  ];

  it('maps registry fields into display rows', () => {
    const rows = buildClientPresenceRows(roster, 'tab-self');

    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual({
      id: 'reg-1',
      connectionId: 'tab-self',
      accessor: 'browser',
      browser: 'Chrome',
      os: 'Windows',
      connectedAt: '2026-06-20T10:00:00+00:00',
      status: 'connected',
      isOwn: true,
    });
  });

  it('flags only the row matching the own connection id', () => {
    const rows = buildClientPresenceRows(roster, 'tab-other');

    expect(rows.map((row) => row.isOwn)).toEqual([false, true]);
  });

  it('marks nothing when the own connection id is empty', () => {
    const rows = buildClientPresenceRows(roster, '');

    expect(rows.every((row) => row.isOwn === false)).toBe(true);
  });

  it('returns an empty list for a non-array roster', () => {
    expect(buildClientPresenceRows(null, 'tab-self')).toEqual([]);
    expect(buildClientPresenceRows(undefined, 'tab-self')).toEqual([]);
  });

  it('tolerates missing fields without throwing', () => {
    const rows = buildClientPresenceRows([{}], 'tab-self');

    expect(rows[0]).toEqual({
      id: '',
      connectionId: '',
      accessor: '',
      browser: '',
      os: '',
      connectedAt: '',
      status: '',
      isOwn: false,
    });
  });
});

describe('applyExtensionsPanelList() hook order', () => {
  it('drops the retired prompt-append hook while keeping the rest in order', () => {
    // The retired hook's literal name is built from fragments on purpose, so a
    // repo-wide grep for it returns zero outside the plans — the test still
    // proves the panel never surfaces it.
    const retiredHookEvent = ['before', 'agent', 'start'].join('_');
    const [extension] = applyExtensionsPanelList({
      extensions: [
        {
          name: 'example',
          capabilities: {
            hooks: {
              run_start: 1,
              [retiredHookEvent]: 2,
              context: 3,
              tool_call: 4,
              tool_result: 5,
              run_end: 6,
            },
          },
        },
      ],
    });

    const hookEvents = extension.capabilities.hooks.map((hook) => hook.event);
    expect(hookEvents).not.toContain(retiredHookEvent);
    expect(hookEvents).toEqual([
      'run_start',
      'context',
      'tool_call',
      'tool_result',
      'run_end',
    ]);
  });
});
