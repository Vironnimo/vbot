import { t } from '../../../../webui/src/lib/i18n.js';
import { tick } from 'svelte';
import { sameServerPath } from '$lib/pathPicker.js';
import { requestId } from './pagePresentation.js';
import { failureText } from './failureText.js';

const START_FIELDS = [
  ['profile', 'swarm-start-profile'],
  ['directory', 'swarm-start-directory'],
  ['goal', 'swarm-goal'],
];

export function createSwarmStartForm(context) {
  let selectedProfile = $state(null);

  let goal = $state('');

  let runDirectory = $state('');

  let defaultDirectory = $state('');

  let directoryLoading = $state(false);

  let startErrors = $state({});

  let directoryRequest = 0;

  let directoryFailed = false;

  const startDisabledReason = $derived(
    context.loading && !context.profiles.length
      ? t('swarm.loading')
      : !context.profiles.length
        ? t('swarm.start.noProfiles')
        : directoryLoading
          ? t('swarm.start.directoryLoading')
          : '',
  );

  function clearStartErrors(...fields) {
    if (fields.some((field) => startErrors[field]))
      startErrors = Object.fromEntries(
        Object.entries(startErrors).filter(
          ([field]) => !fields.includes(field),
        ),
      );
  }

  async function selectRunProfile(profile, { background = false } = {}) {
    const request = ++directoryRequest;
    selectedProfile = profile;
    runDirectory = '';
    defaultDirectory = '';
    directoryLoading = false;
    directoryFailed = false;
    clearStartErrors('profile', 'directory');
    if (!profile) return;
    const directory = profile.working_directory;
    if (directory.kind === 'directory') {
      runDirectory = defaultDirectory = directory.path;
      return;
    }
    directoryLoading = true;
    try {
      const nextCatalog = (await context.call('catalog')).catalog;
      if (context.disposed || request !== directoryRequest) return;
      const project = nextCatalog.projects?.find(
        (item) => item.id === directory.project_id,
      );
      runDirectory = defaultDirectory = project?.cwd ?? '';
    } catch (cause) {
      if (context.disposed || request !== directoryRequest) return;
      directoryFailed = true;
      if (background) context.refreshFailed(cause);
      else context.error = failureText(cause);
    } finally {
      if (!context.disposed && request === directoryRequest)
        directoryLoading = false;
    }
  }

  function unavailableModelsText(profile) {
    const known = (context.catalog.models ?? []).map((model) => model.id);
    const missing = (profile?.participants ?? [])
      .map((row) => row.model)
      .filter(
        (model, index, models) =>
          model && !known.includes(model) && models.indexOf(model) === index,
      );
    return missing.length
      ? t('swarm.error.model_unavailable.named', {
          models: missing.map((model) => `“${model}”`).join(', '),
        })
      : null;
  }

  async function startSwarm() {
    context.error = '';
    const errors = {
      profile: selectedProfile ? '' : t('swarm.start.profileRequired'),
      directory:
        directoryLoading || runDirectory.trim()
          ? ''
          : t('swarm.start.directoryRequired'),
      goal: goal.trim() ? '' : t('swarm.start.goalRequired'),
    };
    startErrors = Object.fromEntries(
      Object.entries(errors).filter(([, message]) => message),
    );
    const invalid = START_FIELDS.find(([field]) => errors[field]);
    if (invalid) {
      await tick();
      document.getElementById(invalid[1])?.focus();
      return;
    }
    if (directoryLoading || context.pending) return;
    context.pending = 'start';
    let missingDirectory = false;
    try {
      const result = await context.call('swarms.start', {
        profile_id: selectedProfile.id,
        expected_profile_revision: selectedProfile.revision,
        prompt: goal,
        request_id: requestId(),
        // The default spelled another way (`C:/work/`) is still the default.
        ...(sameServerPath(runDirectory, defaultDirectory)
          ? {}
          : { working_directory: runDirectory }),
      });
      goal = '';
      const id = result.swarm_id ?? result.id;
      context.focusRequest = { kind: 'run', id };
      await context.refresh();
      await context.stepTo(context.swarmRoute(id));
    } catch (cause) {
      // A folder the server does not find is a problem of its field.
      missingDirectory = cause?.message === 'directory_not_found';
      if (missingDirectory) startErrors = { directory: failureText(cause) };
      else if (cause?.message === 'model_unavailable')
        context.error =
          unavailableModelsText(selectedProfile) ?? failureText(cause);
      else context.error = failureText(cause);
    } finally {
      context.pending = '';
    }
    // Once the fields are enabled again.
    if (missingDirectory) {
      await tick();
      document.getElementById('swarm-start-directory')?.focus();
    }
  }

  return {
    get selectedProfile() {
      return selectedProfile;
    },
    set selectedProfile(value) {
      selectedProfile = value;
    },
    get goal() {
      return goal;
    },
    set goal(value) {
      goal = value;
      clearStartErrors('goal');
    },
    get runDirectory() {
      return runDirectory;
    },
    set runDirectory(value) {
      runDirectory = value;
      clearStartErrors('directory');
    },
    get defaultDirectory() {
      return defaultDirectory;
    },
    get directoryLoading() {
      return directoryLoading;
    },
    get startErrors() {
      return startErrors;
    },
    set startErrors(value) {
      startErrors = value;
    },
    get directoryFailed() {
      return directoryFailed;
    },
    get startDisabledReason() {
      return startDisabledReason;
    },
    selectRunProfile,
    startSwarm,
  };
}
