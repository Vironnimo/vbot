<script>
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import { t } from '$lib/i18n.js';
  import StatePreview from './StatePreview.svelte';
  let { record } = $props();
  const percent = (n) => `${(n * 100).toFixed(1)}%`;
</script>

{#if record}
  <section class="jev-result" aria-label={t('jev.result', 'Evaluation result')}>
    <div class="jev-row">
      <strong>{t(`jev.status.${record.status}`, record.status)}</strong><span
        class="jev-help"
        >{formatDateTimeInApplicationZone(record.created_at, undefined, {
          dateStyle: 'short',
          timeStyle: 'medium',
        })}</span
      >
    </div>
    {#if record.status === 'running'}
      <p role="status">{t('jev.evaluating', 'Evaluating the saved input…')}</p>
    {:else if record.error}
      <p role="alert">{record.error.message}</p>
    {:else if ['cancelled', 'interrupted'].includes(record.status) && record.snapshot.mode !== 'control'}
      <p>
        {t(
          'jev.interruptedHelp',
          'No completed result was retained. The Provider may already have processed and charged this request.',
        )}
      </p>
    {/if}
    {#if record.snapshot.mode !== 'control'}
      <StatePreview state={record.snapshot.state} />
    {/if}
    {#if record.result?.mode === 'control'}
      <p>
        {t('jev.stepsCompleted', 'Completed steps')}: {record.result
          .steps_completed} · {t(
          `jev.phase.${record.result.phase}`,
          record.result.phase,
        )}
      </p>
      {#each [...record.result.steps].reverse() as step (step.number)}
        <article class="jev-answer">
          <div class="jev-row">
            <strong>#{step.number} · {step.action}</strong><span
              >{step.duration_ms ?? '…'} ms</span
            >
          </div>
          <p class="jev-help">
            {step.status === 'executing' && record.status !== 'running'
              ? t('jev.actionUnconfirmed', 'Action outcome unconfirmed')
              : t(`jev.status.${step.status}`, step.status)}
          </p>
          <StatePreview
            state={step.state}
            label={t('jev.observedState', 'Observed state')}
          />
          <details>
            <summary
              >{t(
                'jev.stepDetails',
                'State, decision and command output',
              )}</summary
            >
            <pre>{JSON.stringify(step, null, 2)}</pre>
          </details>
        </article>
      {/each}
      <p class="jev-help">
        {t(
          'jev.retainedSteps',
          'The most recent steps are retained. An interrupted action may already have taken effect; starting again always reads fresh state.',
        )}
      </p>
    {:else if record.result}
      <div class="jev-metrics">
        <span>{record.result.duration_ms} ms</span>
        <span
          >{record.result.usage.input_tokens}
          {t('jev.inputTokens', 'input tokens')}</span
        >
        <span
          >{record.result.usage.output_tokens}
          {t('jev.outputTokens', 'output tokens')}</span
        >
        <span
          >{record.result.usage.cost === undefined
            ? t('jev.costUnknown', 'Cost unavailable')
            : `$${record.result.usage.cost.toFixed(7)}`}</span
        >
      </div>
      <p class="jev-help jev-model">{record.result.model}</p>
      {#each record.snapshot.questions as question, index (question.id)}
        {@const answer = record.result.answers[question.id]}
        <article class="jev-answer">
          <div class="jev-row">
            <strong
              >{t('jev.questionNumber', 'Question {number}', {
                number: index + 1,
              })}</strong
            ><span class="jev-answer-value"
              >{answer.type === 'noul'
                ? percent(answer.noul)
                : answer.type === 'choice'
                  ? answer.choice
                  : answer.score.toFixed(3)}</span
            >
          </div>
          <p>{question.instructions}</p>
          {#if answer.type === 'noul'}
            <meter
              min="0"
              max="1"
              value={answer.noul}
              aria-label={t('jev.yesProbability', 'Probability of yes')}
            ></meter>
          {:else if answer.probabilities}
            {#each Object.entries(answer.probabilities) as [key, value] (key)}
              <div class="jev-probability">
                <span
                  >{answer.type === 'score'
                    ? `${key} · ${question.criteria[Number(key)]}`
                    : `${key} · ${question.criteria[key]}`}</span
                ><span>{percent(value)}</span>
              </div>
              <meter min="0" max="1" {value} aria-label={key}></meter>
            {/each}
          {:else}
            <p class="jev-help">
              {t(
                'jev.noDistribution',
                'The Provider did not supply a probability distribution.',
              )}
            </p>
          {/if}
          {#if answer.confidence !== undefined}<p class="jev-help">
              {t('jev.confidence', 'Confidence')}: {percent(answer.confidence)}
            </p>{/if}
        </article>
      {/each}
      <p class="jev-help">
        {t(
          'jev.confidenceHelp',
          'Confidence describes how concentrated the answer distribution is. It is not a guarantee that the decision is correct.',
        )}
      </p>
    {/if}
    <details>
      <summary>{t('jev.exactInput', 'Exact input and model')}</summary>
      <pre>{JSON.stringify(record.snapshot, null, 2)}</pre>
    </details>
  </section>
{/if}
