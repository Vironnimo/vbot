<script>
  import { t } from '$lib/i18n.js';
  import { updateAgent } from '$lib/api.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import {
    agentPlacement,
    isChatAgent,
    privateAgents,
    agentDisplayOrder,
    agentDisplayName,
  } from '$lib/agentPlacement.js';
  import { subagentAllowedAgents } from '$lib/agentForm.js';
  import Dropdown from '../Dropdown.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import Banner from '../ui/Banner.svelte';
  import AgentSelectionGroup from './AgentSelectionGroup.svelte';

  let { agent, agents = [], onChanged = async () => {} } = $props();
  const autosave = useAutosaveContext();
  let saving = $state(false);
  let error = $state('');
  let pending = $state(null);
  let placement = $derived(agentPlacement(agent));
  let owners = $derived(
    agents.filter(
      (candidate) => candidate.id !== agent.id && isChatAgent(candidate),
    ),
  );
  let ownerOptions = $derived(
    owners.map((owner) => ({ value: owner.id, label: owner.name || owner.id })),
  );
  let restriction = $derived(
    privateAgents(agents, agent.id).length
      ? t('agents.placement.hasPrivateAgents')
      : isChatAgent(agent) && agents.filter(isChatAgent).length < 2
        ? t('agents.placement.lastChatAgent')
        : '',
  );
  let options = $derived([
    { value: 'chat', label: t('agents.placement.chat') },
    {
      value: 'helper',
      label: t('agents.placement.helper'),
      disabled: Boolean(restriction),
      secondaryLabel: restriction,
    },
    {
      value: 'private',
      label: t('agents.placement.private'),
      disabled: Boolean(restriction) || owners.length === 0,
      secondaryLabel: restriction,
    },
  ]);
  let holders = $derived(
    agents.filter(
      (candidate) =>
        candidate.id !== agent.id &&
        subagentAllowedAgents(candidate.tools).includes(agent.id),
    ),
  );
  let usedBy = $derived(
    agentDisplayOrder(agents)
      .filter((candidate) => candidate.id !== agent.id)
      .map((candidate) => ({
        name: candidate.id,
        detailTitle: agentDisplayName(candidate, agents),
        allowed: subagentAllowedAgents(candidate.tools).includes(agent.id),
        locked: saving,
      })),
  );

  function errorText(failure) {
    switch (failure?.details?.data?.reason) {
      case 'owner_invalid':
        return t('agents.placement.ownerInvalid');
      case 'has_private_agents':
        return t('agents.placement.hasPrivateAgents');
      case 'last_chat_agent':
        return t('agents.placement.lastChatAgent');
      default:
        return failure?.message || t('agents.saveError');
    }
  }
  async function savePlacement(next) {
    saving = true;
    error = '';
    try {
      await updateAgent({ id: agent.id, placement: next });
      await onChanged();
    } catch (failure) {
      error = errorText(failure);
    } finally {
      saving = false;
    }
  }
  function losingHolders(next) {
    const groupIds = new Set([
      next.owner_id,
      ...privateAgents(agents, next.owner_id).map((member) => member.id),
    ]);
    return holders.filter((holder) => !groupIds.has(holder.id));
  }

  function choose(next) {
    if (saving) return;
    return autosave.requestTransition(() => {
      if (next.kind === 'private' && losingHolders(next).length) pending = next;
      else return savePlacement(next);
    });
  }
  function chooseKind(kind) {
    if (kind === placement.kind) return;
    choose(kind === 'private' ? { kind, owner_id: owners[0]?.id } : { kind });
  }
  function toggleHolder(id, allowed) {
    if (saving) return;
    return autosave.requestTransition(async () => {
      const holder = agents.find((candidate) => candidate.id === id);
      if (!holder) return;
      const next = subagentAllowedAgents(holder.tools).filter(
        (name) => name !== agent.id,
      );
      if (allowed) next.push(agent.id);
      saving = true;
      error = '';
      try {
        await updateAgent({ id, subagent_allow: next });
        await onChanged();
      } catch (failure) {
        error = errorText(failure);
      } finally {
        saving = false;
      }
    });
  }
</script>

<section class="s-section" aria-labelledby="agent-placement-title">
  <header class="s-section__head">
    <h3 class="s-section__title" id="agent-placement-title">
      {t('agents.placement.title')}
    </h3>
  </header>
  <p class="s-section__desc">
    {placement.orphaned
      ? t('agents.placement.orphanedHelp')
      : placement.kind === 'chat'
        ? t('agents.placement.chatHelp')
        : placement.kind === 'helper'
          ? t('agents.placement.helperHelp')
          : t('agents.placement.privateHelp')}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="agent-placement"
            >{t('agents.placement.title')}</label
          >{#if restriction}<p class="s-row-desc">{restriction}</p>{/if}
        </div>
        <div class="s-row-control">
          <Dropdown
            id="agent-placement"
            ariaLabel={t('agents.placement.title')}
            value={placement.kind}
            {options}
            disabled={saving}
            onValueChange={chooseKind}
          />
        </div>
      </div>
      {#if placement.kind === 'private'}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-placement-owner"
              >{t('agents.placement.owner')}</label
            >
          </div>
          <div class="s-row-control">
            <Dropdown
              id="agent-placement-owner"
              ariaLabel={t('agents.placement.owner')}
              value={placement.owner_id}
              options={ownerOptions}
              disabled={saving}
              onValueChange={(owner_id) =>
                choose({ kind: 'private', owner_id })}
            />
          </div>
        </div>
      {/if}
    </div>
    {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
  </div>
</section>
{#if placement.kind === 'helper'}
  <section class="s-section" aria-labelledby="agent-used-by-title">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-used-by-title">
        {t('agents.usedBy.title')}
      </h3>
    </header>
    <p class="s-section__desc">{t('agents.usedBy.help')}</p>
    <div class="s-section__body">
      <AgentSelectionGroup
        title={t('agents.usedBy.title')}
        titleId="agent-used-by-group"
        items={usedBy}
        groupToggle={false}
        toggleLabel={(name) => t('agents.access.toggleAgent', { name })}
        onToggle={toggleHolder}
      />
    </div>
  </section>
{/if}
{#if pending}
  <ConfirmDialog
    title={t('agents.placement.privateConfirmTitle', {
      name: agent.name || agent.id,
      owner:
        owners.find((owner) => owner.id === pending.owner_id)?.name ||
        pending.owner_id,
    })}
    body={t('agents.placement.privateConfirm', {
      holders: losingHolders(pending)
        .map((holder) => holder.name || holder.id)
        .join(', '),
    })}
    confirmLabel={t('common.confirm')}
    onCancel={() => (pending = null)}
    onConfirm={() => {
      const next = pending;
      pending = null;
      return savePlacement(next);
    }}
  />
{/if}
