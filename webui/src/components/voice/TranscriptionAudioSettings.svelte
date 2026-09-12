<script>
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import { onDestroy, untrack } from 'svelte';
  import {
    normalizeTranscriptionAudio,
    TRANSCRIPTION_AUDIO_PROFILES,
    buildTranscriptionAudioSettingsPayload,
    TRANSCRIPTION_AUDIO_FORMATS,
    TRANSCRIPTION_AUDIO_SAMPLE_RATES,
  } from '$lib/settingsView.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { updateSettings } from '$lib/api.js';
  let { settings, onCommit, onError } = $props();

  let transcriptionAudio = $state(
    untrack(() => normalizeTranscriptionAudio(settings)),
  );

  let lastSavedTranscriptionAudio = $state(
    untrack(() => normalizeTranscriptionAudio(settings)),
  );

  let transcriptionSaveState = $state('idle');

  let transcriptionProfileOptions = $derived(
    TRANSCRIPTION_AUDIO_PROFILES.map((profile) => ({
      value: profile,
      label:
        profile === 'compatibility'
          ? t(
              'settings.voice.transcriptionProfileCompatibility',
              'Maximum compatibility (recommended)',
            )
          : profile === 'high_quality'
            ? t(
                'settings.voice.transcriptionProfileHighQuality',
                'High fidelity',
              )
            : t('settings.voice.transcriptionProfileCustom', 'Custom'),
    })),
  );

  let transcriptionFormatOptions = $derived(
    TRANSCRIPTION_AUDIO_FORMATS.map((format) => ({
      value: format,
      label:
        format === 'wav'
          ? t('settings.voice.transcriptionFormatWav', 'WAV (PCM16)')
          : t(
              'settings.voice.transcriptionFormatFlac',
              'FLAC (lossless PCM16)',
            ),
    })),
  );

  let transcriptionSampleRateOptions = $derived(
    TRANSCRIPTION_AUDIO_SAMPLE_RATES.map((sampleRate) => ({
      value: String(sampleRate),
      label:
        sampleRate === 16000
          ? t(
              'settings.voice.transcriptionSampleRate16',
              '16 kHz (recommended for speech)',
            )
          : `${sampleRate / 1000} kHz`,
    })),
  );

  const autosaveContext = useAutosaveContext();
  const audioAutosave = createAutosaveParticipant({
    getSnapshot: () => transcriptionAudio,
    hasChanges: transcriptionAudioHasChanges,
    save: persistCurrentTranscriptionAudio,
  });

  const unregisterAudioAutosave = autosaveContext.register(audioAutosave);

  function transcriptionAudioHasChanges() {
    return (
      transcriptionAudio.profile !== lastSavedTranscriptionAudio.profile ||
      transcriptionAudio.format !== lastSavedTranscriptionAudio.format ||
      transcriptionAudio.sample_rate_hz !==
        lastSavedTranscriptionAudio.sample_rate_hz
    );
  }

  function saveTranscriptionAudio() {
    return audioAutosave.runSave('manual');
  }

  async function persistCurrentTranscriptionAudio() {
    if (!transcriptionAudioHasChanges()) return true;
    const savedSnapshot = { ...transcriptionAudio };
    transcriptionSaveState = 'saving';
    onError('');
    try {
      const nextSettings = await updateSettings(
        buildTranscriptionAudioSettingsPayload(savedSnapshot),
      );
      lastSavedTranscriptionAudio = normalizeTranscriptionAudio(nextSettings);
      onCommit(nextSettings);
      transcriptionSaveState = 'saved';
      return true;
    } catch (error) {
      transcriptionSaveState = 'error';
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
    }
  }

  function handleTranscriptionProfileChange(profile) {
    transcriptionAudio = normalizeTranscriptionAudio({
      speech: {
        transcription_audio: {
          ...transcriptionAudio,
          profile,
        },
      },
    });
    void saveTranscriptionAudio();
  }

  function handleTranscriptionFormatChange(format) {
    transcriptionAudio = {
      ...transcriptionAudio,
      format,
    };
    void saveTranscriptionAudio();
  }

  function handleTranscriptionSampleRateChange(value) {
    const sampleRate = Number.parseInt(value, 10);
    if (!TRANSCRIPTION_AUDIO_SAMPLE_RATES.includes(sampleRate)) return;
    transcriptionAudio = {
      ...transcriptionAudio,
      sample_rate_hz: sampleRate,
    };
    void saveTranscriptionAudio();
  }
  onDestroy(unregisterAudioAutosave);
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.voice.transcriptionProfile', 'Transcription audio')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.voice.transcriptionProfileDescription',
        'The audio sent to the Speech-to-text Model from both the Chat microphone and a command recorded after a wake phrase. Local wakeword detection keeps its optimized 16 kHz stream.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Dropdown
      value={transcriptionAudio.profile}
      options={transcriptionProfileOptions}
      ariaLabel={t(
        'settings.voice.transcriptionProfile',
        'Transcription audio',
      )}
      onValueChange={handleTranscriptionProfileChange}
      disabled={transcriptionSaveState === 'saving'}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.voice.transcriptionFormat', 'Format')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.voice.transcriptionFormatDescription',
        'Mono, signed 16-bit audio. WAV has the broadest Provider support; FLAC is lossless and smaller.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Dropdown
      value={transcriptionAudio.format}
      options={transcriptionFormatOptions}
      ariaLabel={t('settings.voice.transcriptionFormat', 'Format')}
      onValueChange={handleTranscriptionFormatChange}
      disabled={transcriptionAudio.profile !== 'custom' ||
        transcriptionSaveState === 'saving'}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.voice.transcriptionSampleRate', 'Sample rate')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.voice.transcriptionSampleRateDescription',
        '16 kHz is the speech-focused default. Higher rates retain more source detail but create larger uploads.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Dropdown
      value={String(transcriptionAudio.sample_rate_hz)}
      options={transcriptionSampleRateOptions}
      ariaLabel={t('settings.voice.transcriptionSampleRate', 'Sample rate')}
      onValueChange={handleTranscriptionSampleRateChange}
      disabled={transcriptionAudio.profile !== 'custom' ||
        transcriptionSaveState === 'saving'}
    />
  </div>
</div>

<div class="voice-save-state" aria-live="polite">
  {#if transcriptionSaveState === 'saving'}
    {t('common.saving', 'Saving…')}
  {:else if transcriptionSaveState === 'saved' && !transcriptionAudioHasChanges()}
    {t('common.saved', 'Saved')}
  {:else if transcriptionSaveState === 'error'}
    {t('common.saveFailed', 'Not saved')}
  {/if}
</div>
