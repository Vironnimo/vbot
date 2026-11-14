import { isOperational } from '$lib/onboarding.js';
import { setApplicationTimeZone } from '$lib/dateTimePrefs.svelte.js';
import { finishSetup, getSettings, getSetup } from '$lib/api.js';
import { applyAppearanceSettings } from '$lib/appearancePrefs.svelte.js';
import { applyArchiveSettings } from '$lib/archiveRetention.svelte.js';

export function createAppSetup(context) {
  let settings = $state(null);
  let setupState = $state(null);
  let onboardingActive = $state(false);
  let lastRefreshToken = null;
  let lastSetupToken = null;
  let settingsRequest = 0;
  let setupRequest = 0;
  let disposed = false;
  let evaluated = false;
  let operational = $derived(isOperational(settings));

  async function loadSetup() {
    const request = ++setupRequest;
    try {
      const result = await getSetup();
      if (disposed || request !== setupRequest) return;
      setupState = result;
      if (!evaluated) {
        evaluated = true;
        onboardingActive = result.auto_show === true;
      }
      // Completion is durable, but an open guide stays open while the user
      // chooses optional components after saving their first Model.
    } catch {
      // A failed read is not evidence that setup is pending.
    }
  }

  async function loadAppSettings() {
    const request = ++settingsRequest;
    const setupRead = loadSetup();
    try {
      const result = await getSettings();
      if (disposed || request !== settingsRequest) return;
      settings = result;
      setApplicationTimeZone(result?.general?.timezone);
      applyAppearanceSettings(result?.appearance);
      applyArchiveSettings(result?.archive);
    } catch {
      // Keep the last settings projection during a connection failure.
    } finally {
      await setupRead;
    }
  }

  $effect(() => {
    const token = context.modelsRefreshToken;
    if (lastRefreshToken !== null && token !== lastRefreshToken)
      void loadAppSettings();
    lastRefreshToken = token;
  });

  $effect(() => {
    const token = context.setupRefreshToken;
    if (lastSetupToken !== null && token !== lastSetupToken) void loadSetup();
    lastSetupToken = token;
  });

  const closeOnboarding = () => {
    onboardingActive = false;
    evaluated = true;
    void loadAppSettings();
  };

  async function dismissOnboarding() {
    if (!onboardingActive) return true;
    const result = await finishSetup();
    if (!disposed) {
      setupState = result;
      closeOnboarding();
    }
    return true;
  }

  const reopenOnboarding = () => {
    evaluated = true;
    onboardingActive = true;
  };

  return {
    get settings() {
      return settings;
    },
    set settings(value) {
      settings = value;
    },
    get state() {
      return setupState;
    },
    get onboardingActive() {
      return onboardingActive;
    },
    get operational() {
      return operational;
    },
    loadAppSettings,
    closeOnboarding,
    completeOnboarding() {
      closeOnboarding();
      context.selectView('chat');
    },
    dismissOnboarding,
    reopenOnboarding,
    acceptState(result) {
      setupRequest += 1;
      setupState = result;
    },
    destroy() {
      disposed = true;
      settingsRequest += 1;
      setupRequest += 1;
    },
  };
}
