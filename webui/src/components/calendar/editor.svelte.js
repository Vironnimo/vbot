import { onDestroy } from 'svelte';
import { useAutosaveContext } from '$lib/autosave.js';
import { todayKey, eventById } from '$lib/calendarView.js';
import {
  emptyEventForm,
  eventFormPayload,
  eventFormProblem,
  eventFormValues,
  monthlyChoices,
  moveEventStart,
  occurrenceFormValues,
  weekdayCode,
} from '$lib/calendarEventForm.js';
import { t } from '$lib/i18n.js';

const FORM_PROBLEMS = {
  title: () => t('calendar.errors.titleRequired'),
  date: () => t('calendar.errors.dateRequired'),
  end: () => t('calendar.errors.endBeforeStart'),
  recurrence: () => t('calendar.errors.recurrence'),
};

// The event dialogs of the Calendar: the create and edit form, the details of
// one occurrence and the delete confirmation. Editing a repeating event
// changes the whole series or, by choice, only the shown occurrence.
export function createCalendarEventEditor(context) {
  const autosaveContext = useAutosaveContext();
  let formOpen = $state(false);

  let formMode = $state('create');

  let formEventId = $state(null);

  // The occurrence an edit started from; its event is the edited series.
  let formOccurrence = $state(null);

  // 'series' edits the whole event, 'occurrence' only `formOccurrence`.
  let editScope = $state('series');

  let formValues = $state(
    emptyEventForm(todayKey(context.viewState.systemTimeZone)),
  );

  let formError = $state('');

  let submitting = $state(false);
  let formGeneration = 0;
  let formBaseline = null;

  onDestroy(() => {
    formGeneration += 1;
  });

  function replaceForm() {
    formGeneration += 1;
    submitting = false;
  }

  let detailOpen = $state(false);

  let detailOccurrence = $state(null);

  let deleteTarget = $state(null);

  let deleteOccurrenceOnly = $state(false);

  // A repeating event's edit offers the choice between the series and the
  // occurrence it started from.
  let canEditOccurrence = $derived(
    formMode === 'edit' && Boolean(formOccurrence?.recurring),
  );

  function openCreate(dayKey = context.viewState.anchorKey) {
    return autosaveContext.requestTransition(() => openCreateNow(dayKey));
  }

  function openCreateNow(dayKey) {
    replaceForm();
    formMode = 'create';
    formEventId = null;
    formOccurrence = null;
    editScope = 'series';
    formValues = emptyEventForm(dayKey);
    formBaseline = null;
    formError = '';
    formOpen = true;
  }

  function openDetail(occurrence) {
    return autosaveContext.requestTransition(() => openDetailNow(occurrence));
  }

  function openDetailNow(occurrence) {
    detailOccurrence = occurrence;
    detailOpen = true;
  }

  function openEdit(occurrence) {
    return autosaveContext.requestTransition(() => openEditNow(occurrence));
  }

  function openEditNow(occurrence) {
    const event = eventById(context.viewState.events, occurrence.event_id);
    if (!event) {
      return;
    }
    replaceForm();
    formMode = 'edit';
    formEventId = occurrence.event_id;
    formOccurrence = occurrence;
    editScope = 'series';
    formValues = eventFormValues(event, context.viewState.systemTimeZone);
    formBaseline = $state.snapshot(formValues);
    formError = '';
    detailOpen = false;
    formOpen = true;
  }

  // Switching what an edit changes shows that target's own values.
  function setEditScope(scope) {
    if (!canEditOccurrence || scope === editScope) {
      return;
    }
    const zone = context.viewState.systemTimeZone;
    if (scope === 'occurrence') {
      formValues = occurrenceFormValues(formOccurrence, zone);
    } else {
      const event = eventById(
        context.viewState.events,
        formOccurrence.event_id,
      );
      if (!event) {
        return;
      }
      formValues = eventFormValues(event, zone);
    }
    replaceForm();
    formBaseline = $state.snapshot(formValues);
    editScope = scope;
    formError = '';
  }

  // A new start keeps the event's length.
  function setStart(patch) {
    moveEventStart(formValues, patch);
    keepMonthlyChoice();
    formError = '';
  }

  function setAllDay(allDay) {
    formValues.all_day = allDay;
    if (formValues.end_date < formValues.start_date) {
      formValues.end_date = formValues.start_date;
    }
    formError = '';
  }

  // Weekly repetition starts on the start's weekday.
  function setFrequency(freq) {
    formValues.freq = freq;
    if (freq === 'weekly' && formValues.by_weekday.length === 0) {
      formValues.by_weekday = [weekdayCode(formValues.start_date)];
    }
    keepMonthlyChoice();
    formError = '';
  }

  function toggleWeekday(code) {
    formValues.by_weekday = formValues.by_weekday.includes(code)
      ? formValues.by_weekday.filter((day) => day !== code)
      : [...formValues.by_weekday, code];
  }

  // A monthly weekday choice the new start day no longer offers falls back
  // to its day of the month.
  function keepMonthlyChoice() {
    if (
      !monthlyChoices(formValues.start_date).includes(formValues.monthly_by)
    ) {
      formValues.monthly_by = 'day';
    }
  }

  async function submitForm() {
    if (submitting) {
      return;
    }
    const repeats = editScope === 'series';
    const problem = eventFormProblem(formValues, { repeats });
    if (problem) {
      formError = FORM_PROBLEMS[problem]();
      return;
    }
    submitting = true;
    formError = '';
    const generation = formGeneration;
    const isCurrent = () => generation === formGeneration;
    const submitted = $state.snapshot(formValues);
    const unchanged = () =>
      JSON.stringify(formValues) === JSON.stringify(submitted);
    try {
      const payload = eventFormPayload(submitted, {
        repeats,
        baseline: formBaseline,
      });
      let createdOccurrence = null;
      if (formMode === 'edit') {
        if (Object.keys(payload).length) {
          await context.controller.updateEvent(
            repeats ? formEventId : formOccurrence.id,
            payload,
          );
        }
      } else {
        const result = await context.controller.createEvent(payload);
        if (!isCurrent()) return;
        formMode = 'edit';
        formEventId = result.event.id;
        createdOccurrence = context.viewState.occurrences.find(
          (item) => item.event_id === formEventId,
        );
      }
      if (!isCurrent()) return;
      // The response commits only the submitted snapshot. Later typing stays
      // editable; after creation its next save updates the created event.
      formBaseline = submitted;
      if (!unchanged()) return;
      if (createdOccurrence) {
        await autosaveContext.requestTransition(() => {
          if (!isCurrent() || !unchanged()) return;
          openDetailNow(createdOccurrence);
          formOpen = false;
        });
      } else {
        formOpen = false;
      }
    } catch (error) {
      if (isCurrent()) formError = error?.message ?? String(error);
    } finally {
      if (isCurrent()) submitting = false;
    }
  }

  function requestDelete(occurrence) {
    return autosaveContext.requestTransition(() =>
      requestDeleteNow(occurrence),
    );
  }

  function requestDeleteNow(occurrence) {
    detailOpen = false;
    deleteOccurrenceOnly = false;
    deleteTarget = occurrence;
  }

  async function confirmDelete() {
    const occurrence = deleteTarget;
    deleteTarget = null;
    if (!occurrence) {
      return;
    }
    try {
      await context.controller.deleteEvent(
        occurrence.recurring && deleteOccurrenceOnly
          ? occurrence.id
          : occurrence.event_id,
      );
    } catch (error) {
      context.onToast({
        title: t('calendar.errors.delete'),
        message: error?.message ?? String(error),
        variant: 'error',
      });
    }
  }

  function closeDetail() {
    return autosaveContext.requestTransition(() => (detailOpen = false));
  }

  return {
    closeDetail,
    get formOpen() {
      return formOpen;
    },
    set formOpen(value) {
      if (!value) replaceForm();
      formOpen = value;
    },
    get formMode() {
      return formMode;
    },
    get formValues() {
      return formValues;
    },
    get formError() {
      return formError;
    },
    get submitting() {
      return submitting;
    },
    get editScope() {
      return editScope;
    },
    get canEditOccurrence() {
      return canEditOccurrence;
    },
    get detailOpen() {
      return detailOpen;
    },
    set detailOpen(value) {
      if (value) detailOpen = true;
      else closeDetail();
    },
    get detailOccurrence() {
      return detailOccurrence;
    },
    get deleteTarget() {
      return deleteTarget;
    },
    set deleteTarget(value) {
      deleteTarget = value;
    },
    get deleteOccurrenceOnly() {
      return deleteOccurrenceOnly;
    },
    set deleteOccurrenceOnly(value) {
      deleteOccurrenceOnly = value;
    },
    openCreate,
    openDetail,
    openEdit,
    setEditScope,
    setStart,
    setAllDay,
    setFrequency,
    toggleWeekday,
    submitForm,
    requestDelete,
    confirmDelete,
  };
}
