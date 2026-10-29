// The visible Automation categories share execution settings and retain the
// existing Cron transport. Legacy record links resolve to their category.
export const SCHEDULES = 'schedules';
export const WEBHOOKS = 'webhooks';
export const INPUTS = 'inputs';

export function automationCategory(job) {
  if (job?.schedule_type === 'input') return INPUTS;
  return job?.schedule_type === 'webhook' ? WEBHOOKS : SCHEDULES;
}

export function automationPlace(job) {
  return [automationCategory(job), job.id];
}

export function readAutomationPlace(place, jobs) {
  const categorized =
    place[0] === SCHEDULES || place[0] === WEBHOOKS || place[0] === INPUTS;
  const target = place[categorized ? 1 : 0] ?? '';
  const job = jobs.find((entry) => entry.id === target);
  return {
    category: job
      ? automationCategory(job)
      : categorized
        ? place[0]
        : SCHEDULES,
    target,
  };
}
