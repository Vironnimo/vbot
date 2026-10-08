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
  let formOpen = $state(false);

  let formMode = $state('create');

  // The occurrence an edit started from; its event is the edited series.
  let formOccurrence = $state(null);

  // 'series' edits the whole event, 'occurrence' only `formOccurrence`.
  let editScope = $state('series');

  let formValues = $state(
    emptyEventForm(todayKey(context.viewState.systemTimeZone)),
  );

  let formError = $state('');

  let submitting = $state(false);

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
    formMode = 'create';
    formOccurrence = null;
    editScope = 'series';
    formValues = emptyEventForm(dayKey);
    formError = '';
    formOpen = true;
  }

  function openDetail(occurrence) {
    detailOccurrence = occurrence;
    detailOpen = true;
  }

  function openEdit(occurrence) {
    const event = eventById(context.viewState.events, occurrence.event_id);
    if (!event) {
      return;
    }
    formMode = 'edit';
    formOccurrence = occurrence;
    editScope = 'series';
    formValues = eventFormValues(event, context.viewState.systemTimeZone);
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
    try {
      const payload = eventFormPayload(formValues, { repeats });
      if (formMode === 'edit') {
        await context.controller.updateEvent(
          repeats ? formOccurrence.event_id : formOccurrence.id,
          payload,
        );
      } else {
        const result = await context.controller.createEvent(payload);
        const occurrence = context.viewState.occurrences.find(
          (item) => item.event_id === result.event?.id,
        );
        if (occurrence) openDetail(occurrence);
      }
      formOpen = false;
    } catch (error) {
      formError = error?.message ?? String(error);
    } finally {
      submitting = false;
    }
  }

  function requestDelete(occurrence) {
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

  return {
    get formOpen() {
      return formOpen;
    },
    set formOpen(value) {
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
      detailOpen = value;
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
