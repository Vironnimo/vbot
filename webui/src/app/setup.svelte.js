import { isOperational } from '$lib/onboarding.js';
import { setApplicationTimeZone } from '$lib/dateTimePrefs.svelte.js';
import { getSettings } from '$lib/api.js';
import { init } from '$lib/i18n.js';
import {
  setChatWidth,
  setChatWorkingMode,
} from '$lib/appearancePrefs.svelte.js';

export function createAppSetup(context) {
  // Accessor-local UI state only: whether the user set the first-run wizard
  // aside this browser. The real trigger stays the live operational state — a
  // credential removal clears this flag and brings the wizard back on its own.
  const ONBOARDING_DISMISSED_KEY = 'vbot.onboardingDismissed';

  const readOnboardingDismissed = () => {
    try {
      if (typeof localStorage === 'undefined') {
        return false;
      }
      return localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1';
    } catch {
      return false;
    }
  };

  const writeOnboardingDismissed = (dismissed) => {
    try {
      if (typeof localStorage === 'undefined') {
        return;
      }
      if (dismissed) {
        localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1');
      } else {
        localStorage.removeItem(ONBOARDING_DISMISSED_KEY);
      }
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  };

  // Application settings, fetched on mount and re-fetched on a provider/model
  // change. Drives the first-run onboarding decision. Null until first loaded.
  let settings = $state(null);

  let onboardingDismissed = $state(readOnboardingDismissed());

  // Sticky once shown: the wizard stays until completed/dismissed, so the
  // connect flip (operational → true) never yanks it before the model step.
  let onboardingActive = $state(false);

  let lastSettingsModelsToken = null;

  let operational = $derived(isOperational(settings));

  // Slim re-entry banner: shown only once the wizard was set aside while the
  // system is still not operational. It disappears the instant a provider is
  // connected (operational flips true).
  let showFinishSetup = $derived(
    settings !== null &&
      !operational &&
      onboardingDismissed &&
      !onboardingActive,
  );

  // Fetch application settings and seed the app-wide appearance from them. Also
  // the source of the operational state that drives onboarding.
  const loadAppSettings = async () => {
    try {
      const result = await getSettings();
      settings = result;
      setApplicationTimeZone(result?.general?.timezone);
      setChatWidth(result?.appearance?.chat_width);
      setChatWorkingMode(result?.appearance?.chat_working_mode);
      const language = result?.appearance?.language;
      if (typeof language === 'string' && language.length > 0) {
        init(language);
      }
      maybeStartOnboarding();
    } catch {
      // settings RPC unavailable — keep the comfortable defaults and leave the
      // onboarding decision untriggered (settings stays null).
    }
  };

  // The guided setup shows once, on the first successful settings load, when
  // vBot is not operational and the user has neither dismissed it nor already
  // navigated elsewhere. It is a one-shot decision (not a reactive latch), so a
  // late settings response never pops the wizard over a view the user opened in
  // the meantime; re-entry afterwards is explicit (the "Finish setup" banner).
  let onboardingEvaluated = false;

  function maybeStartOnboarding() {
    if (onboardingEvaluated || settings === null) {
      return;
    }
    onboardingEvaluated = true;
    if (!operational && !onboardingDismissed) {
      onboardingActive = true;
    }
  }

  // A live operational state clears a stale dismiss, so removing credentials
  // later re-triggers the wizard on its own.
  $effect(() => {
    if (operational && onboardingDismissed) {
      onboardingDismissed = false;
      writeOnboardingDismissed(false);
    }
  });

  // Re-fetch settings on a provider/model change (the same signal that bumps
  // `modelsRefreshToken`) so the operational state stays live.
  $effect(() => {
    if (lastSettingsModelsToken === null) {
      lastSettingsModelsToken = context.modelsRefreshToken;
      return;
    }
    if (context.modelsRefreshToken !== lastSettingsModelsToken) {
      lastSettingsModelsToken = context.modelsRefreshToken;
      void loadAppSettings();
    }
  });

  const completeOnboarding = () => {
    onboardingActive = false;
    onboardingDismissed = false;
    writeOnboardingDismissed(false);
    context.selectView('chat');
    void loadAppSettings();
  };

  const dismissOnboarding = () => {
    onboardingActive = false;
    onboardingDismissed = true;
    writeOnboardingDismissed(true);
  };

  const reopenOnboarding = () => {
    onboardingActive = true;
  };
  return {
    get settings() {
      return settings;
    },
    set settings(value) {
      settings = value;
    },
    get onboardingActive() {
      return onboardingActive;
    },
    get operational() {
      return operational;
    },
    get showFinishSetup() {
      return showFinishSetup;
    },
    get loadAppSettings() {
      return loadAppSettings;
    },
    get completeOnboarding() {
      return completeOnboarding;
    },
    get dismissOnboarding() {
      return dismissOnboarding;
    },
    get reopenOnboarding() {
      return reopenOnboarding;
    },
  };
}
