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
  let {
    settings,
    onCommit,
    onError,
    // Receives the latest save outcome for the caller's section save state:
    // 'idle', 'saving', 'saved' (and nothing pending) or 'error'.
    onSaveStatusChange = () => {},
  } = $props();

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
          ? t('settings.voice.transcriptionProfileCompatibility')
          : profile === 'high_quality'
            ? t('settings.voice.transcriptionProfileHighQuality')
            : t('settings.voice.transcriptionProfileCustom'),
    })),
  );

  let transcriptionFormatOptions = $derived(
    TRANSCRIPTION_AUDIO_FORMATS.map((format) => ({
      value: format,
      label:
        format === 'wav'
          ? t('settings.voice.transcriptionFormatWav')
          : t('settings.voice.transcriptionFormatFlac'),
    })),
  );

  let transcriptionSampleRateOptions = $derived(
    TRANSCRIPTION_AUDIO_SAMPLE_RATES.map((sampleRate) => ({
      value: String(sampleRate),
      label:
        sampleRate === 16000
          ? t('settings.voice.transcriptionSampleRate16')
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
      onError(`${t('settings.saveError')} ${error.message}`);
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
  let saveStatus = $derived(
    transcriptionSaveState === 'saved' && transcriptionAudioHasChanges()
      ? 'idle'
      : transcriptionSaveState,
  );
  $effect(() => {
    onSaveStatusChange(saveStatus);
  });
  onDestroy(unregisterAudioAutosave);
</script>

<!-- One group: the profile row and the two values it controls (editable with
     the Custom profile). -->
<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionProfile')}
      </div>
      <div class="s-row-desc">
        {t('settings.voice.transcriptionProfileDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={transcriptionAudio.profile}
        options={transcriptionProfileOptions}
        ariaLabel={t('settings.voice.transcriptionProfile')}
        onValueChange={handleTranscriptionProfileChange}
        disabled={transcriptionSaveState === 'saving'}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionFormat')}
      </div>
      <div class="s-row-desc">
        {t('settings.voice.transcriptionFormatDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={transcriptionAudio.format}
        options={transcriptionFormatOptions}
        ariaLabel={t('settings.voice.transcriptionFormat')}
        onValueChange={handleTranscriptionFormatChange}
        disabled={transcriptionAudio.profile !== 'custom' ||
          transcriptionSaveState === 'saving'}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionSampleRate')}
      </div>
      <div class="s-row-desc">
        {t('settings.voice.transcriptionSampleRateDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={String(transcriptionAudio.sample_rate_hz)}
        options={transcriptionSampleRateOptions}
        ariaLabel={t('settings.voice.transcriptionSampleRate')}
        onValueChange={handleTranscriptionSampleRateChange}
        disabled={transcriptionAudio.profile !== 'custom' ||
          transcriptionSaveState === 'saving'}
      />
    </div>
  </div>
</div>
