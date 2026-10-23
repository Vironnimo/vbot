<script>
  // The Change check section of a repeating job's form: nothing, or a command
  // or URL checked before each Run, which then starts only when the output
  // changed, or at every due time with the output. One-time and event
  // schedules cannot watch, so the view shows this section only for cron and
  // interval schedules.
  import { t } from '$lib/i18n.js';
  import { CRON_WATCH_COMMAND, CRON_WATCH_URL } from '$lib/cronView.js';
  import { watchKindOptions, watchRunsOptions } from './presentation.js';
  import TextField from '../ui/TextField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';

  let { editor } = $props();
  const watchKinds = watchKindOptions();
  const watchRuns = watchRunsOptions();

  let values = $derived(editor.formValues);
  let isCommand = $derived(values.watch_kind === CRON_WATCH_COMMAND);
  let isUrl = $derived(values.watch_kind === CRON_WATCH_URL);
</script>

<section class="s-section" aria-labelledby="cron-section-watch">
  <header class="s-section__head">
    <h3 class="s-section__title" id="cron-section-watch">
      {t('cron.sections.watch')}
    </h3>
  </header>
  <p class="s-section__desc">
    {t('cron.sections.watchSubtitle')}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="cron-job-watch">
            {t('cron.form.watch')}
          </label>
        </div>
        <div class="s-row-control">
          <Dropdown
            id="cron-job-watch"
            value={values.watch_kind}
            options={watchKinds}
            ariaLabel={t('cron.form.watch')}
            disabled={editor.isCreating && editor.submittingForm}
            triggerClass="cron-dropdown"
            listClass="cron-dropdown-list"
            onValueChange={(next) => editor.updateFormField('watch_kind', next)}
          />
        </div>
      </div>

      {#if isCommand || isUrl}
        <div class="s-row">
          <div class="s-row-info">
            <label
              class="s-row-label cron-label-with-hint"
              for="cron-job-watch-target"
            >
              {isCommand
                ? t('cron.form.watchCommand')
                : t('cron.form.watchUrl')}
              <span class="cron-required" aria-hidden="true">*</span>
              <InfoHint
                text={isCommand
                  ? t('cron.form.watchCommandHelp')
                  : t('cron.form.watchUrlHelp')}
              />
            </label>
          </div>
          <div class="s-row-control">
            <TextField
              id="cron-job-watch-target"
              code
              value={values.watch_target}
              placeholder={isCommand
                ? t('cron.form.watchCommandPlaceholder')
                : t('cron.form.watchUrlPlaceholder')}
              disabled={editor.isCreating && editor.submittingForm}
              onInput={(next) => editor.updateFormField('watch_target', next)}
            />
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-watch-runs">
              {t('cron.form.watchRuns')}
            </label>
          </div>
          <div class="s-row-control">
            <Dropdown
              id="cron-job-watch-runs"
              value={values.watch_always ? 'always' : 'on_change'}
              options={watchRuns}
              ariaLabel={t('cron.form.watchRuns')}
              disabled={editor.isCreating && editor.submittingForm}
              triggerClass="cron-dropdown"
              listClass="cron-dropdown-list"
              onValueChange={(next) =>
                editor.updateFormField('watch_always', next === 'always')}
            />
          </div>
        </div>
      {/if}
    </div>
  </div>
</section>
