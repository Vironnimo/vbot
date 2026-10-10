<script>
  import { tokensUsed, usageCount } from './pagePresentation.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import './runUsage.css';

  // The Usage tab: the Run's id, its token and tool call totals and one row
  // per participant and model.
  let { usage, model } = $props();
</script>

<section class="panel" role="tabpanel">
  <h3>{t('swarm.usage')}</h3>
  <dl class="swarm-identity">
    <dt>{t('swarm.id')}</dt>
    <dd>{model.selectedSwarm.id}</dd>
  </dl>
  {#if usage.report?.usage}<dl class="usage-summary">
      <div>
        <dt>
          {t('swarm.usage.tokensUsed')}
        </dt>
        <dd>{tokensUsed(usage.report.usage.activity?.totals)}</dd>
      </div>
      <div>
        <dt>{t('swarm.usage.toolCalls')}</dt>
        <dd>
          {usageCount(usage.report.usage.activity?.tool_calls)}
        </dd>
      </div>
    </dl>
    {#if usage.rows.length}<div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t('swarm.usage.participant')}</th>
              <th>{t('swarm.usage.model')}</th>
              <th>{t('swarm.usage.tokensUsed')}</th>
              <th>{t('swarm.usage.toolCalls')}</th>
              <th>{t('swarm.usage.runs')}</th>
            </tr>
          </thead>
          <tbody>
            {#each usage.rows as row (row.id)}
              <tr>
                {#if row.participantRows}
                  <td rowspan={row.participantRows}>{row.participant}</td>
                {/if}
                <td>{row.modelName}</td>
                <td>{tokensUsed(row.model)}</td>
                {#if row.participantRows}
                  <td rowspan={row.participantRows}
                    >{usageCount(row.toolCalls)}</td
                  >
                {/if}
                <td>{usageCount(row.model?.runs)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>{/if}{:else if !usage.loading}<EmptyState
      density="compact"
      title={t('swarm.usageEmpty')}
    />{/if}
</section>
