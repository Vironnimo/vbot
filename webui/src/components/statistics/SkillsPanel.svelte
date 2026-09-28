<script>
  import { t, tOr, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatDateTime,
    formatInteger,
    formatUsageRate,
    parseOrigin,
    rollupSkillActivationsByAgent,
  } from '$lib/statisticsView.js';
  import { statCard, agentCountTable } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());

  const skills = $derived(report?.skills ?? null);

  const skillActivationsByAgent = $derived(
    skills ? rollupSkillActivationsByAgent(skills.skills) : [],
  );

  function originLabel(origin) {
    const { scope, detail } = parseOrigin(origin);
    if (detail !== null) {
      return t(`statistics.skills.scopedOrigin.${scope}`, { detail });
    }
    return tOr(`statistics.skills.origin.${scope}`, scope);
  }
</script>

<div class="stats-panel">
  <div class="stats-grid">
    {@render statCard(
      t('statistics.skills.total'),
      formatInteger(skills.total_skills, locale),
    )}
    {@render statCard(
      t('statistics.skills.used'),
      formatInteger(skills.used_skills, locale),
    )}
    {@render statCard(
      t('statistics.skills.offeredUnactivated'),
      formatInteger(skills.offered_unactivated_skills, locale),
    )}
    {@render statCard(
      t('statistics.skills.withoutOfferData'),
      formatInteger(skills.skills_without_offer_data, locale),
    )}
  </div>
  <p class="stats-note">
    {t('statistics.skills.intro')}
  </p>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.skills.perSkill')}
    </h3>
    {#if skills.skills.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.skills.empty')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.skill')}</th>
              <th>{t('statistics.col.origins')}</th>
              <th>{t('statistics.col.offered')}</th>
              <th>{t('statistics.col.activated')}</th>
              <th>{t('statistics.col.usageRate')}</th>
              <th>{t('statistics.col.firstActivated')}</th>
              <th>{t('statistics.col.lastActivated')}</th>
            </tr>
          </thead>
          <tbody>
            {#each skills.skills as skill (skill.name)}
              {@const offeredUnactivated =
                skill.offered_sessions > 0 &&
                skill.activated_offered_sessions === 0}
              {@const withoutOfferData = skill.offered_sessions === 0}
              <tr
                class:stats-skill-row--candidate={offeredUnactivated}
                use:tooltip={offeredUnactivated
                  ? t('statistics.skills.neverUsedRowTitle')
                  : withoutOfferData
                    ? t('statistics.skills.noOfferDataRowTitle')
                    : ''}
              >
                <td class="stats-mono">
                  <span class="stats-skill-name">
                    <span>{skill.name}</span>
                    {#if offeredUnactivated}
                      <Badge variant="warn">
                        {t('statistics.skills.neverUsedBadge')}
                      </Badge>
                    {:else if withoutOfferData}
                      <Badge variant="neutral">
                        {t('statistics.skills.noOfferDataBadge')}
                      </Badge>
                    {/if}
                  </span>
                </td>
                <td>{@render skillOrigins(skill.origins)}</td>
                <td>{formatInteger(skill.offered_sessions, locale)}</td>
                <td>{formatInteger(skill.activated_sessions, locale)}</td>
                <td>{formatUsageRate(skill.usage_rate)}</td>
                <td>{formatDateTime(skill.first_activated, locale)}</td>
                <td>{formatDateTime(skill.last_activated, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="stats-columns">
    {@render agentCountTable(
      t('statistics.skills.byAgent'),
      skillActivationsByAgent,
    )}
  </div>
</div>

{#snippet skillOrigins(origins)}
  <span class="stats-origins">
    {#each origins ?? [] as origin (origin)}
      <Badge variant="neutral">{originLabel(origin)}</Badge>
    {/each}
  </span>
{/snippet}
