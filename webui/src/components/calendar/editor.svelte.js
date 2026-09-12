import { todayKey, eventById, eventToFormValues } from '$lib/calendarView.js';
import { t } from '$lib/i18n.js';

export function createCalendarEventEditor(context) {
  const EMPTY_FORM = () => ({
    title: '',
    notes: '',
    all_day: false,
    start_date: todayKey(context.viewState.systemTimeZone),
    start_time: '09:00',
    duration_minutes: 60,
    duration_days: 1,
    freq: 'none',
    interval: 1,
    by_weekday: ['mo', 'tu', 'we', 'th', 'fr'],
    end_mode: 'never',
    end_count: 10,
    end_until: '',
  });

  let formOpen = $state(false);

  let formMode = $state('create');

  let formEventId = $state('');

  let formValues = $state(EMPTY_FORM());

  let formError = $state('');

  let submitting = $state(false);

  let detailOpen = $state(false);

  let detailOccurrence = $state(null);

  let deleteTarget = $state(null);

  let deleteOccurrenceOnly = $state(false);

  function openCreate(dayKey = context.viewState.anchorKey) {
    formMode = 'create';
    formEventId = '';
    formValues = { ...EMPTY_FORM(), start_date: dayKey };
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
    formEventId = event.id;
    formValues = eventToFormValues(event, context.viewState.systemTimeZone);
    formError = '';
    detailOpen = false;
    formOpen = true;
  }

  async function submitForm() {
    if (!formValues.title.trim()) {
      formError = t(
        'calendar.errors.titleRequired',
        'Please give the event a title.',
      );
      return;
    }
    submitting = true;
    formError = '';
    try {
      const payload = formValuesToEventPayload(formValues);
      if (formMode === 'edit') {
        await context.controller.updateEvent(formEventId, payload);
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
      if (occurrence.recurring && deleteOccurrenceOnly) {
        await context.controller.excludeOccurrence(
          occurrence.event_id,
          occurrenceExdateValue(occurrence),
        );
      } else {
        await context.controller.deleteEvent(occurrence.event_id);
      }
    } catch (error) {
      context.onToast(error?.message ?? String(error));
    }
  }

  // The exclusion (RFC 5545 EXDATE) uses the event's own start form: a naive
  // local datetime for timed events, a plain date for all-day events. The
  // server renders it per occurrence in the event's anchor zone.
  function occurrenceExdateValue(occurrence) {
    return occurrence.occurrence_start;
  }

  function formValuesToEventPayload(values) {
    const payload = {
      title: values.title,
      notes: values.notes || null,
      all_day: values.all_day,
    };
    if (values.all_day) {
      payload.start = values.start_date;
      payload.duration_days = Number(values.duration_days) || 1;
    } else {
      payload.start = `${values.start_date}T${values.start_time || '09:00'}:00`;
      payload.duration_minutes = Number(values.duration_minutes) || 60;
    }
    if (values.freq !== 'none') {
      const rrule = {
        freq: values.freq,
        interval: Number(values.interval) || 1,
      };
      if (values.freq === 'weekly') {
        rrule.by_weekday = values.by_weekday?.length
          ? values.by_weekday
          : ['mo'];
      }
      if (values.end_mode === 'count') {
        rrule.count = Number(values.end_count) || 10;
      } else if (values.end_mode === 'until') {
        rrule.until = values.end_until || undefined;
      }
      payload.rrule = rrule;
    } else {
      payload.rrule = null;
    }
    return payload;
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
    set formMode(value) {
      formMode = value;
    },
    get formValues() {
      return formValues;
    },
    set formValues(value) {
      formValues = value;
    },
    get formError() {
      return formError;
    },
    set formError(value) {
      formError = value;
    },
    get submitting() {
      return submitting;
    },
    set submitting(value) {
      submitting = value;
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
    set detailOccurrence(value) {
      detailOccurrence = value;
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
    get openCreate() {
      return openCreate;
    },
    get openDetail() {
      return openDetail;
    },
    get openEdit() {
      return openEdit;
    },
    get submitForm() {
      return submitForm;
    },
    get requestDelete() {
      return requestDelete;
    },
    get confirmDelete() {
      return confirmDelete;
    },
  };
}
