import { onDestroy } from 'svelte';
import {
  createCronFormValues,
  cronFormFingerprint,
  CRON_PRESET_CUSTOM,
  CRON_SCHEDULE_TYPE_CRON,
  CRON_SCHEDULE_TYPE_INTERVAL,
  CRON_SCHEDULE_TYPE_ONCE,
  describeCronExpression,
  buildCronPresetOptions,
  cronPresetForExpression,
  applyCronListResponse,
  cronPresetExpression,
  buildCreateCronPayload,
  buildUpdateCronPayload,
  CRON_STATUS_ACTIVE,
  CRON_STATUS_COMPLETED,
  CRON_STATUS_MISSED,
} from '$lib/cronView.js';
import { useAutosaveContext, createDebouncedAutosave } from '$lib/autosave.js';
import { t } from '$lib/i18n.js';
import {
  createCronJob,
  updateCronJob,
  disableCronJob,
  enableCronJob,
  deleteCronJob,
} from '$lib/api.js';

export function createCronEditor(context) {
  const initialFormValues = createCronFormValues();

  // The detail pane edits either an existing job (a selected id) or a fresh
  // create draft (`isCreating`). Its form + validation state is panel-local; the
  // list is the master. Selecting a row, or starting a job, reseeds these.
  let selectedJobId = $state('');

  let isCreating = $state(false);

  let formValues = $state(initialFormValues);

  let formBaseline = $state(cronFormFingerprint(initialFormValues));

  let selectedPreset = $state(CRON_PRESET_CUSTOM);

  let formErrorMessage = $state('');

  let submittingForm = $state(false);

  // The id of the job whose enable/disable/delete mutation is in flight, so its
  // controls disable without freezing the whole pane.
  let mutatingJobId = $state('');

  // The cron job awaiting delete confirmation (null = dialog closed).
  let deleteConfirmJob = $state(null);

  let pendingDiscardAction = null;

  let showDiscardConfirm = $state(false);

  let pendingJobsResult = null;

  let selectedJob = $derived(
    context.jobs.find((job) => job.id === selectedJobId) ?? null,
  );

  // The detail form is shown when creating, or when a real job is selected.
  let showDetailForm = $derived(isCreating || Boolean(selectedJob));

  let isDirty = $derived(
    showDetailForm && cronFormFingerprint(formValues) !== formBaseline,
  );

  const autosaveContext = useAutosaveContext();

  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ jobId: selectedJobId, values: formValues }),
    hasChanges: () => !isCreating && isDirty,
    save: (reason) => persistForm(null, reason),
  });

  const unregisterAutosave = autosaveContext.register(autosave.participant);

  $effect(() => {
    if (isCreating || !isDirty || submittingForm) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  function submitForm(event) {
    event.preventDefault();
    if (isCreating) return persistForm();
    return autosave.participant.runSave('manual', { force: true });
  }

  let isCronSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_CRON,
  );

  let isIntervalSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL,
  );

  let isOnceSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_ONCE,
  );

  let cronExpressionPreview = $derived(
    describeCronExpression(formValues.cron_expression),
  );

  let detailTitle = $derived(
    isCreating
      ? t('cron.detail.createTitle', 'Create Scheduled Run')
      : selectedJob?.name || t('cron.detail.editTitle', 'Edit Scheduled Run'),
  );

  let presetOptions = $derived(
    buildCronPresetOptions((key) =>
      t(`cron.presets.${key}`, key === CRON_PRESET_CUSTOM ? 'Custom' : key),
    ),
  );

  function selectJob(job) {
    if (!job?.id) {
      return;
    }
    if (!isCreating && job.id === selectedJobId) {
      return;
    }
    requestFormTransition(() => selectJobNow(job));
  }

  function selectJobNow(job) {
    isCreating = false;
    selectedJobId = job.id;
    formValues = createCronFormValues(job, context.viewState.systemTimezone);
    if (!formValues.agent_id) {
      formValues.agent_id = context.viewState.agents[0]?.id ?? '';
    }
    selectedPreset = cronPresetForExpression(formValues.cron_expression);
    formBaseline = cronFormFingerprint(formValues);
    formErrorMessage = '';
    context.loadProjectTeams();
  }

  function startCreate() {
    requestFormTransition(startCreateNow);
  }

  function startCreateNow() {
    isCreating = true;
    formValues = createCronFormValues(null, context.viewState.systemTimezone);
    formValues.agent_id = context.viewState.agents[0]?.id ?? '';
    selectedPreset = CRON_PRESET_CUSTOM;
    formBaseline = cronFormFingerprint(formValues);
    formErrorMessage = '';
    context.loadProjectTeams();
  }

  // Cancel a create draft and return to the previously selected job (if any).
  function cancelCreate() {
    if (submittingForm) {
      return;
    }
    requestFormTransition(() => {
      isCreating = false;
      if (selectedJob) {
        selectJobNow(selectedJob);
      } else if (context.jobs.length > 0) {
        selectJobNow(context.jobs[0]);
      }
    });
  }

  function requestFormTransition(action) {
    if (!isCreating) return autosaveContext.requestTransition(action);
    if (!isDirty) {
      action();
      return;
    }
    pendingDiscardAction = action;
    showDiscardConfirm = true;
  }

  function cancelDiscard() {
    pendingDiscardAction = null;
    showDiscardConfirm = false;
  }

  function confirmDiscard() {
    const action = pendingDiscardAction;
    pendingDiscardAction = null;
    showDiscardConfirm = false;
    if (pendingJobsResult) {
      applyCronListResponse(context.viewState, pendingJobsResult);
      pendingJobsResult = null;
    }
    action?.();
  }

  function setScheduleType(scheduleType) {
    formValues.schedule_type = scheduleType;
    formErrorMessage = '';
  }

  function updateFormField(fieldName, value) {
    formValues[fieldName] = value;
    formErrorMessage = '';
  }

  // Selecting a preset fills its expression; the field stays editable and the
  // live preview keeps working. Custom (or an unknown key) fills nothing.
  function applyPreset(presetKey) {
    selectedPreset = presetKey;
    if (presetKey === CRON_PRESET_CUSTOM) {
      return;
    }
    const expression = cronPresetExpression(presetKey);
    if (expression) {
      formValues.cron_expression = expression;
    }
    formErrorMessage = '';
  }

  // Hand-editing the expression re-derives the preset selection, flipping it to
  // Custom when the text no longer matches the chosen preset.
  function updateCronExpression(value) {
    formValues.cron_expression = value;
    selectedPreset = cronPresetForExpression(value);
    formErrorMessage = '';
  }

  function validateFormValues() {
    const hasCoreValues =
      formValues.agent_id.trim().length > 0 &&
      formValues.prompt.trim().length > 0;
    let hasScheduleValue = formValues.run_at.trim().length > 0;
    if (isCronSchedule) {
      hasScheduleValue = formValues.cron_expression.trim().length > 0;
    } else if (isIntervalSchedule) {
      const intervalMinutes = Number(formValues.interval_minutes);
      hasScheduleValue =
        Number.isInteger(intervalMinutes) && intervalMinutes > 0;
    }
    const repeat = formValues.repeat.trim();
    const repeatValue = Number(repeat);
    const hasValidRepeat =
      (repeat.length === 0 && (isCreating || !isOnceSchedule)) ||
      (Number.isInteger(repeatValue) &&
        repeatValue > 0 &&
        (!isOnceSchedule || repeatValue === 1));

    if (!hasCoreValues || !hasScheduleValue || !hasValidRepeat) {
      formErrorMessage = t(
        'cron.errors.missingRequired',
        'Agent, prompt, and valid schedule details are required. Repeat must be a positive integer; one-time schedules allow only 1.',
      );
      return false;
    }

    return true;
  }

  async function persistForm(event = null, reason = 'manual') {
    event?.preventDefault();

    if (submittingForm || !validateFormValues()) {
      return false;
    }

    if (!isCreating && !isDirty) {
      if (reason === 'manual')
        context.showToast(t('common.alreadySaved', 'Already saved'));
      return true;
    }
    const submitted = cronFormFingerprint(formValues);
    const creating = isCreating;
    submittingForm = true;
    formErrorMessage = '';

    try {
      let targetJobId = selectedJobId;
      if (creating) {
        const created = await createCronJob(buildCreateCronPayload(formValues));
        targetJobId = typeof created?.id === 'string' ? created.id : '';
        if (targetJobId) {
          formValues.id = targetJobId;
        }
        context.showToast(t('cron.messages.created', 'Cron job created.'));
      } else {
        await updateCronJob(buildUpdateCronPayload(formValues));
        if (reason === 'manual')
          context.showToast(t('cron.messages.updated', 'Cron job updated.'));
      }

      if (context.destroyed) {
        return;
      }

      isCreating = false;
      if (targetJobId) {
        selectedJobId = targetJobId;
      }
      await context.loadJobs({ silent: true });
      const savedJob = context.jobs.find((job) => job.id === targetJobId);
      if (creating && savedJob) {
        selectJobNow(savedJob);
      } else {
        formBaseline = savedJob
          ? cronFormFingerprint(
              createCronFormValues(savedJob, context.viewState.systemTimezone),
            )
          : submitted;
      }
      return true;
    } catch (error) {
      formErrorMessage = `${t('cron.errors.save', 'Cron job could not be saved.')} ${context.errorMessageText(error, t('common.unknown', 'Unknown'))}`;
      return false;
    } finally {
      if (!context.destroyed) {
        submittingForm = false;
      }
    }
  }

  async function toggleJob(job) {
    if (
      !job?.id ||
      job.status === CRON_STATUS_COMPLETED ||
      job.status === CRON_STATUS_MISSED
    ) {
      return;
    }

    mutatingJobId = job.id;

    try {
      if (job.status === CRON_STATUS_ACTIVE) {
        await disableCronJob(job.id);
        context.showToast(t('cron.messages.disabled', 'Cron job disabled.'));
      } else {
        await enableCronJob(job.id);
        context.showToast(t('cron.messages.enabled', 'Cron job enabled.'));
      }

      await context.loadJobs({ silent: true });
    } catch (error) {
      context.showToast(
        t('cron.errors.toggle', 'Cron job status could not be updated.'),
        'error',
        error,
      );
    } finally {
      mutatingJobId = '';
    }
  }

  function deleteJob(job) {
    if (!job?.id) {
      return;
    }
    deleteConfirmJob = job;
  }

  function cancelDeleteJob() {
    deleteConfirmJob = null;
  }

  async function confirmDeleteJob() {
    const job = deleteConfirmJob;
    deleteConfirmJob = null;
    if (!job?.id) {
      return;
    }

    mutatingJobId = job.id;

    try {
      await deleteCronJob(job.id);
      context.showToast(t('cron.messages.deleted', 'Cron job deleted.'));
      if (selectedJobId === job.id) {
        selectedJobId = '';
      }
      await context.loadJobs({ silent: true });
    } catch (error) {
      context.showToast(
        t('cron.errors.delete', 'Cron job could not be deleted.'),
        'error',
        error,
      );
    } finally {
      mutatingJobId = '';
    }
  }
  return {
    get selectedJobId() {
      return selectedJobId;
    },
    set selectedJobId(value) {
      selectedJobId = value;
    },
    get isCreating() {
      return isCreating;
    },
    set isCreating(value) {
      isCreating = value;
    },
    get formValues() {
      return formValues;
    },
    set formValues(value) {
      formValues = value;
    },
    get selectedPreset() {
      return selectedPreset;
    },
    set selectedPreset(value) {
      selectedPreset = value;
    },
    get formErrorMessage() {
      return formErrorMessage;
    },
    set formErrorMessage(value) {
      formErrorMessage = value;
    },
    get submittingForm() {
      return submittingForm;
    },
    set submittingForm(value) {
      submittingForm = value;
    },
    get mutatingJobId() {
      return mutatingJobId;
    },
    set mutatingJobId(value) {
      mutatingJobId = value;
    },
    get deleteConfirmJob() {
      return deleteConfirmJob;
    },
    set deleteConfirmJob(value) {
      deleteConfirmJob = value;
    },
    get showDiscardConfirm() {
      return showDiscardConfirm;
    },
    set showDiscardConfirm(value) {
      showDiscardConfirm = value;
    },
    get pendingJobsResult() {
      return pendingJobsResult;
    },
    set pendingJobsResult(value) {
      pendingJobsResult = value;
    },
    get selectedJob() {
      return selectedJob;
    },
    set selectedJob(value) {
      selectedJob = value;
    },
    get showDetailForm() {
      return showDetailForm;
    },
    set showDetailForm(value) {
      showDetailForm = value;
    },
    get isDirty() {
      return isDirty;
    },
    set isDirty(value) {
      isDirty = value;
    },
    get submitForm() {
      return submitForm;
    },
    get isCronSchedule() {
      return isCronSchedule;
    },
    set isCronSchedule(value) {
      isCronSchedule = value;
    },
    get isIntervalSchedule() {
      return isIntervalSchedule;
    },
    set isIntervalSchedule(value) {
      isIntervalSchedule = value;
    },
    get isOnceSchedule() {
      return isOnceSchedule;
    },
    set isOnceSchedule(value) {
      isOnceSchedule = value;
    },
    get cronExpressionPreview() {
      return cronExpressionPreview;
    },
    set cronExpressionPreview(value) {
      cronExpressionPreview = value;
    },
    get detailTitle() {
      return detailTitle;
    },
    set detailTitle(value) {
      detailTitle = value;
    },
    get presetOptions() {
      return presetOptions;
    },
    set presetOptions(value) {
      presetOptions = value;
    },
    get selectJob() {
      return selectJob;
    },
    get selectJobNow() {
      return selectJobNow;
    },
    get startCreate() {
      return startCreate;
    },
    get cancelCreate() {
      return cancelCreate;
    },
    get cancelDiscard() {
      return cancelDiscard;
    },
    get confirmDiscard() {
      return confirmDiscard;
    },
    get setScheduleType() {
      return setScheduleType;
    },
    get updateFormField() {
      return updateFormField;
    },
    get applyPreset() {
      return applyPreset;
    },
    get updateCronExpression() {
      return updateCronExpression;
    },
    get toggleJob() {
      return toggleJob;
    },
    get deleteJob() {
      return deleteJob;
    },
    get cancelDeleteJob() {
      return cancelDeleteJob;
    },
    get confirmDeleteJob() {
      return confirmDeleteJob;
    },
  };
}
