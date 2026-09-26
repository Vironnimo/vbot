import { describe, expect, it } from 'vitest';

import {
  ACTION_CHOICE_COMMAND,
  ACTION_CHOICE_LIVE_START,
  ACTION_CHOICE_LIVE_TOGGLE,
  buildVoiceConfigChanges,
  cloneVoiceConfig,
  effectiveVoiceAction,
  liveWakePhrases,
  overlappingPhraseConflicts,
  rebaseVoiceConfig,
  sameVoiceAction,
  voiceActionChoice,
  voiceActionForChoice,
  voiceConfigFromStatus,
} from '../wakewordSettings.js';

const NABU = 'builtin/okay_nabu';
const HEY_NABU = 'builtin/hey_nabu';
const JARVIS = 'builtin/hey_jarvis';
const DEFAULT_COMMAND = {
  type: 'command',
  agent_id: null,
  session_behavior: null,
};

function phrase(modelId, overrides = {}) {
  return {
    model_id: modelId,
    label: modelId,
    sensitivity: 0.5,
    action: DEFAULT_COMMAND,
    effective: {
      type: 'command',
      agent_id: 'main',
      session_behavior: 'active',
    },
    problem: null,
    ...overrides,
  };
}

function status(overrides = {}) {
  return {
    enabled: true,
    state: 'listening',
    sequence: 1,
    microphone: { index: 2, name: 'Desk mic', host_api: 'WASAPI' },
    echo_cancellation: { enabled: true, state: 'active' },
    default_agent_id: 'main',
    default_session_behavior: 'active',
    phrases: [phrase(NABU), phrase(HEY_NABU, { sensitivity: 0.7 })],
    ...overrides,
  };
}

describe('voiceConfigFromStatus', () => {
  it('projects the editable configuration out of a snapshot', () => {
    const config = voiceConfigFromStatus(
      status({
        default_session_behavior: 'new',
        phrases: [
          phrase(NABU, {
            action: {
              type: 'command',
              agent_id: 'writer',
              session_behavior: null,
            },
          }),
          phrase(JARVIS, {
            sensitivity: null,
            action: { type: 'live_voice', mode: 'start' },
          }),
        ],
      }),
    );

    expect(config).toEqual({
      microphone: { index: 2, name: 'Desk mic', host_api: 'WASAPI' },
      echo_cancellation: true,
      active_model_ids: [NABU, JARVIS],
      model_sensitivities: { [NABU]: 0.5 },
      default_agent_id: 'main',
      default_session_behavior: 'new',
      phrase_actions: {
        [NABU]: { type: 'command', agent_id: 'writer', session_behavior: null },
        [JARVIS]: { type: 'live_voice', mode: 'start' },
      },
    });
  });

  it('reads a missing snapshot as an empty configuration', () => {
    expect(voiceConfigFromStatus(null)).toEqual({
      microphone: null,
      echo_cancellation: true,
      active_model_ids: [],
      model_sensitivities: {},
      default_agent_id: null,
      default_session_behavior: 'active',
      phrase_actions: {},
    });
  });

  it('isolates the copy from the source', () => {
    const config = voiceConfigFromStatus(status());
    const copy = cloneVoiceConfig(config);

    copy.active_model_ids.pop();
    copy.model_sensitivities[NABU] = 0.9;
    copy.phrase_actions[NABU].agent_id = 'writer';
    copy.microphone.index = 7;

    expect(config.active_model_ids).toEqual([NABU, HEY_NABU]);
    expect(config.model_sensitivities[NABU]).toBe(0.5);
    expect(config.phrase_actions[NABU]).toEqual(DEFAULT_COMMAND);
    expect(config.microphone.index).toBe(2);
  });
});

describe('buildVoiceConfigChanges', () => {
  it('is empty when nothing differs', () => {
    const baseline = voiceConfigFromStatus(status());
    expect(
      buildVoiceConfigChanges(cloneVoiceConfig(baseline), baseline),
    ).toEqual({});
  });

  it('sends only the changed values and phrases', () => {
    const baseline = voiceConfigFromStatus(
      status({
        phrases: [
          phrase(NABU, {
            action: { type: 'live_voice', mode: 'toggle' },
          }),
          phrase(HEY_NABU, {
            action: {
              type: 'command',
              agent_id: 'writer',
              session_behavior: null,
            },
          }),
        ],
      }),
    );
    const draft = cloneVoiceConfig(baseline);
    draft.microphone = { index: 5, name: 'Headset', host_api: 'MME' };
    draft.echo_cancellation = false;
    draft.active_model_ids = [NABU, HEY_NABU, JARVIS];
    draft.model_sensitivities[HEY_NABU] = 0.8;
    draft.model_sensitivities[JARVIS] = 0.6;
    draft.default_agent_id = 'writer';
    draft.default_session_behavior = 'new';
    // Back to the default command: removes the stored action.
    draft.phrase_actions[NABU] = DEFAULT_COMMAND;
    // Only the Session override is left.
    draft.phrase_actions[HEY_NABU] = {
      type: 'command',
      agent_id: null,
      session_behavior: 'new',
    };
    draft.phrase_actions[JARVIS] = { type: 'live_voice', mode: 'start' };

    expect(buildVoiceConfigChanges(draft, baseline)).toEqual({
      microphone: { index: 5, name: 'Headset', host_api: 'MME' },
      echo_cancellation: false,
      active_model_ids: [NABU, HEY_NABU, JARVIS],
      model_sensitivities: { [HEY_NABU]: 0.8, [JARVIS]: 0.6 },
      default_agent_id: 'writer',
      default_session_behavior: 'new',
      phrase_actions: {
        [NABU]: null,
        [HEY_NABU]: { type: 'command', session_behavior: 'new' },
        [JARVIS]: { type: 'live_voice', mode: 'start' },
      },
    });
  });

  it('treats a missing action and the default command as the same', () => {
    const baseline = voiceConfigFromStatus(status());
    const draft = cloneVoiceConfig(baseline);
    draft.active_model_ids = [NABU, HEY_NABU, JARVIS];
    draft.phrase_actions[JARVIS] = DEFAULT_COMMAND;

    expect(buildVoiceConfigChanges(draft, baseline)).toEqual({
      active_model_ids: [NABU, HEY_NABU, JARVIS],
    });
  });
});

describe('rebaseVoiceConfig', () => {
  it('keeps unsaved edits and takes everything else from the newer snapshot', () => {
    const previous = voiceConfigFromStatus(status());
    const draft = cloneVoiceConfig(previous);
    draft.model_sensitivities[NABU] = 0.9;
    draft.phrase_actions[HEY_NABU] = { type: 'live_voice', mode: 'start' };

    const next = voiceConfigFromStatus(
      status({
        default_agent_id: 'writer',
        echo_cancellation: { enabled: false, state: 'off' },
        phrases: [
          phrase(NABU, { sensitivity: 0.3 }),
          phrase(HEY_NABU, { sensitivity: 0.4 }),
        ],
      }),
    );

    expect(rebaseVoiceConfig(draft, previous, next)).toEqual({
      ...next,
      model_sensitivities: { [NABU]: 0.9, [HEY_NABU]: 0.4 },
      phrase_actions: {
        [NABU]: DEFAULT_COMMAND,
        [HEY_NABU]: { type: 'live_voice', mode: 'start' },
      },
    });
  });

  it('drops entries of phrases the newer snapshot no longer has', () => {
    const previous = voiceConfigFromStatus(status());
    const next = voiceConfigFromStatus(status({ phrases: [phrase(NABU)] }));

    const rebased = rebaseVoiceConfig(
      cloneVoiceConfig(previous),
      previous,
      next,
    );

    expect(rebased.active_model_ids).toEqual([NABU]);
    expect(rebased.model_sensitivities).toEqual({ [NABU]: 0.5 });
    expect(rebased.phrase_actions).toEqual({ [NABU]: DEFAULT_COMMAND });
  });
});

describe('phrase actions', () => {
  it('compares actions with a missing action as the default command', () => {
    expect(sameVoiceAction(undefined, DEFAULT_COMMAND)).toBe(true);
    expect(
      sameVoiceAction(
        { type: 'command', agent_id: '' },
        { type: 'command', agent_id: null, session_behavior: null },
      ),
    ).toBe(true);
    expect(
      sameVoiceAction(
        { type: 'live_voice', mode: 'start' },
        { type: 'live_voice', mode: 'toggle' },
      ),
    ).toBe(false);
    expect(
      sameVoiceAction(
        { type: 'command', agent_id: 'writer' },
        { type: 'command', agent_id: 'main' },
      ),
    ).toBe(false);
  });

  it('resolves command defaults for the effective action', () => {
    const config = voiceConfigFromStatus(
      status({
        default_agent_id: 'main',
        default_session_behavior: 'new',
        phrases: [
          phrase(NABU),
          phrase(HEY_NABU, {
            action: {
              type: 'command',
              agent_id: 'writer',
              session_behavior: 'active',
            },
          }),
          phrase(JARVIS, { action: { type: 'live_voice', mode: 'toggle' } }),
        ],
      }),
    );

    expect(effectiveVoiceAction(config, NABU)).toEqual({
      type: 'command',
      agent_id: 'main',
      session_behavior: 'new',
    });
    expect(effectiveVoiceAction(config, HEY_NABU)).toEqual({
      type: 'command',
      agent_id: 'writer',
      session_behavior: 'active',
    });
    expect(effectiveVoiceAction(config, JARVIS)).toEqual({
      type: 'live_voice',
      mode: 'toggle',
    });
  });

  it('maps actions to select values and back', () => {
    const override = {
      type: 'command',
      agent_id: 'writer',
      session_behavior: 'new',
    };
    expect(voiceActionChoice(undefined)).toBe(ACTION_CHOICE_COMMAND);
    expect(voiceActionChoice(override)).toBe(ACTION_CHOICE_COMMAND);
    expect(voiceActionChoice({ type: 'live_voice', mode: 'start' })).toBe(
      ACTION_CHOICE_LIVE_START,
    );
    expect(voiceActionChoice({ type: 'live_voice', mode: 'toggle' })).toBe(
      ACTION_CHOICE_LIVE_TOGGLE,
    );

    expect(voiceActionForChoice(ACTION_CHOICE_LIVE_START, override)).toEqual({
      type: 'live_voice',
      mode: 'start',
    });
    expect(voiceActionForChoice(ACTION_CHOICE_LIVE_TOGGLE, override)).toEqual({
      type: 'live_voice',
      mode: 'toggle',
    });
    // Choosing "command" again keeps the command's overrides.
    expect(voiceActionForChoice(ACTION_CHOICE_COMMAND, override)).toEqual(
      override,
    );
    expect(
      voiceActionForChoice(ACTION_CHOICE_COMMAND, {
        type: 'live_voice',
        mode: 'start',
      }),
    ).toEqual(DEFAULT_COMMAND);
  });
});

describe('overlappingPhraseConflicts', () => {
  const models = [
    { id: NABU, overlaps: [HEY_NABU] },
    { id: HEY_NABU, overlaps: [] },
    { id: JARVIS, overlaps: [] },
  ];

  it('flags overlapping active phrases that do different things', () => {
    const config = voiceConfigFromStatus(
      status({
        phrases: [
          phrase(NABU),
          phrase(HEY_NABU, { action: { type: 'live_voice', mode: 'toggle' } }),
          phrase(JARVIS, { action: { type: 'live_voice', mode: 'start' } }),
        ],
      }),
    );

    const conflicts = overlappingPhraseConflicts(config, models);

    expect(Object.fromEntries(conflicts)).toEqual({
      [NABU]: [HEY_NABU],
      [HEY_NABU]: [NABU],
    });
  });

  it('accepts overlapping phrases with the same effective action', () => {
    const config = voiceConfigFromStatus(
      status({
        default_agent_id: 'main',
        phrases: [
          phrase(NABU),
          // The explicit default Agent does the same as no override.
          phrase(HEY_NABU, {
            action: {
              type: 'command',
              agent_id: 'main',
              session_behavior: null,
            },
          }),
        ],
      }),
    );

    expect(overlappingPhraseConflicts(config, models).size).toBe(0);
  });

  it('ignores an inactive overlapping phrase', () => {
    const config = voiceConfigFromStatus(status({ phrases: [phrase(NABU)] }));

    expect(overlappingPhraseConflicts(config, models).size).toBe(0);
  });
});

describe('liveWakePhrases', () => {
  const command = {
    type: 'command',
    agent_id: 'main',
    session_behavior: 'active',
  };

  it('lists the command phrases, including those with a problem', () => {
    expect(
      liveWakePhrases(
        status({
          phrases: [
            phrase(NABU, { label: 'Okay Nabu', effective: command }),
            phrase(JARVIS, {
              label: 'Hey Jarvis',
              effective: { type: 'live_voice', mode: 'toggle' },
            }),
            phrase(HEY_NABU, {
              label: 'Hey Nabu',
              effective: command,
              problem: 'speech_to_text_unconfigured',
            }),
          ],
        }),
      ),
    ).toEqual(['Okay Nabu', 'Hey Nabu']);
  });

  it('lists the phrases whenever Voice is enabled, whatever its state', () => {
    const phrases = [phrase(NABU, { label: 'Okay Nabu', effective: command })];
    expect(liveWakePhrases(null)).toEqual([]);
    expect(liveWakePhrases(status({ enabled: false, phrases }))).toEqual([]);
    for (const state of ['off', 'starting', 'microphone_disconnected', 'error'])
      expect(
        liveWakePhrases(
          status({ state, error_code: 'engine_start_failed', phrases }),
        ),
      ).toEqual(['Okay Nabu']);
  });

  it('cleans, shortens, deduplicates and bounds the labels', () => {
    const long = 'x'.repeat(70);
    const phrases = [
      phrase('a', { label: ' Okay​  Nabu\n', effective: command }),
      phrase('b', { label: 'OKAY NABU', effective: command }),
      phrase('c', { label: '\u0007', effective: command }),
      phrase('d', { label: long, effective: command }),
      ...Array.from({ length: 10 }, (_, index) =>
        phrase(`m${index}`, { label: `Phrase ${index}`, effective: command }),
      ),
    ];

    const result = liveWakePhrases(status({ phrases }));

    expect(result).toHaveLength(8);
    expect(result[0]).toBe('Okay Nabu');
    expect(result[1]).toBe('x'.repeat(60));
    expect(result.slice(2)).toEqual([
      'Phrase 0',
      'Phrase 1',
      'Phrase 2',
      'Phrase 3',
      'Phrase 4',
      'Phrase 5',
    ]);
  });
});
