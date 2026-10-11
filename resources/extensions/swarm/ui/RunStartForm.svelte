<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import PathField from '../../../../webui/src/components/ui/PathField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import SwarmIcon from './SwarmIcon.svelte';
  import './runStartForm.css';

  // The start page: a goal for a new Run of the chosen Swarm (profile) in a
  // working directory. Start checks the fields and shows each problem under
  // its field; the page's own error banner is for failed requests.
  let { model } = $props();

  const profileOptions = $derived(
    model.profiles.map((profile) => ({
      value: profile.id,
      label: profile.name,
    })),
  );

  // Start a Run from a Swarm's menu focuses the goal.
  $effect(() => {
    if (model.takeFocus('goal')) document.getElementById('swarm-goal')?.focus();
  });
</script>

<section class="swarm-start page-scroll" aria-labelledby="swarm-start-title">
  <header class="view-header swarm-start__header">
    <div class="view-header__intro">
      <h2 class="view-header__title" id="swarm-start-title">
        {t('swarm.startTitle')}
      </h2>
      <p class="view-header__subtitle">{t('swarm.startHelp')}</p>
    </div>
  </header>
  <div class="swarm-start__fields">
    <FormField
      controlId="swarm-start-profile"
      label={t('swarm.profile')}
      help={model.profiles.length || model.loading
        ? ''
        : t('swarm.noProfilesHelp')}
      error={model.startErrors.profile}
    >
      {#snippet children({ labelId, describedBy })}
        <div class="swarm-start__profile">
          <Dropdown
            id="swarm-start-profile"
            ariaLabelledby={labelId}
            ariaDescribedby={describedBy}
            value={model.selectedProfile?.id ?? ''}
            options={profileOptions}
            disabled={!model.profiles.length || model.pending === 'start'}
            onValueChange={(id) =>
              model.selectRunProfile(
                model.profiles.find((profile) => profile.id === id),
              )}
          />
          {#if model.selectedProfile}<Button
              variant="tertiary"
              onClick={() =>
                model.navigate(() => model.openProfile(model.selectedProfile))}
              >{t('swarm.editProfile')}</Button
            >{:else if !model.profiles.length && !model.loading}<Button
              variant="tertiary"
              onClick={() => model.navigate(() => model.openProfile())}
              >{t('swarm.newProfile')}</Button
            >{/if}
        </div>
      {/snippet}
    </FormField>
    <FormField
      controlId="swarm-start-directory"
      label={t('swarm.profile.directoryHeading')}
      error={model.startErrors.directory}
    >
      {#snippet children({ describedBy, invalid })}
        <PathField
          id="swarm-start-directory"
          mode="directory"
          listDirectory={model.client.listDirectory}
          value={model.runDirectory}
          {invalid}
          aria-describedby={describedBy}
          placeholder={model.directoryLoading
            ? t('swarm.start.directoryLoading')
            : ''}
          disabled={!model.selectedProfile ||
            model.directoryLoading ||
            model.pending === 'start'}
          onInput={(value) => (model.runDirectory = value)}
        />
      {/snippet}
    </FormField>
    <FormField
      controlId="swarm-goal"
      label={t('swarm.goal')}
      error={model.startErrors.goal}
    >
      {#snippet children({ describedBy, invalid })}
        <TextArea
          id="swarm-goal"
          value={model.goal}
          rows="4"
          autoGrow
          {invalid}
          aria-describedby={describedBy}
          disabled={model.pending === 'start'}
          placeholder={t('swarm.goalPlaceholder')}
          onInput={(value) => (model.goal = value)}
        />
      {/snippet}
    </FormField>
  </div>
  <div class="swarm-start__actions">
    <Button
      variant="primary"
      loading={model.pending === 'start'}
      disabled={Boolean(model.startDisabledReason)}
      disabledReason={model.startDisabledReason}
      onClick={model.startSwarm}
      ><SwarmIcon kind="play" />{model.pending === 'start'
        ? t('swarm.starting')
        : t('swarm.startButton')}</Button
    >
  </div>
</section>
