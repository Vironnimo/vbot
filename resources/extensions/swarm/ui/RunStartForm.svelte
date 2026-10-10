<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import PathField from '../../../../webui/src/components/ui/PathField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import SwarmIcon from './SwarmIcon.svelte';

  // The start page: a goal for a new Run of the chosen Swarm (profile) in a
  // working directory.
  let { model } = $props();
</script>

<section class="start">
  <header class="view-header">
    <div class="view-header__intro">
      <h2 class="view-header__title">
        {t('swarm.startTitle')}
      </h2>
      <p class="view-header__subtitle">
        {t('swarm.startHelp')}
      </p>
    </div>
    <div class="view-header__actions">
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('common.refresh')}
        tooltip={t('common.refresh')}
        disabled={model.loading}
        onClick={() => model.refresh()}><SwarmIcon kind="refresh" /></Button
      >
    </div>
  </header>
  <div class="start-profile">
    <FormField controlId="swarm-start-profile" label={t('swarm.profile')}>
      <Dropdown
        id="swarm-start-profile"
        ariaLabel={t('swarm.profile')}
        value={model.selectedProfile?.id ?? ''}
        options={model.profiles.map((profile) => ({
          value: profile.id,
          label: profile.name,
        }))}
        onValueChange={(id) =>
          model.selectRunProfile(
            model.profiles.find((profile) => profile.id === id),
          )}
      />
    </FormField>
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('common.edit')}
      tooltip={t('swarm.editProfile')}
      disabled={!model.selectedProfile}
      onClick={() => model.openProfile(model.selectedProfile)}
      ><SwarmIcon kind="edit" /></Button
    >
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('common.delete')}
      tooltip={t('swarm.delete.title')}
      disabled={!model.selectedProfile}
      onClick={() => model.confirmProfileDelete(model.selectedProfile)}
      ><SwarmIcon kind="trash" /></Button
    >
  </div>
  <FormField
    controlId="swarm-start-directory"
    label={t('swarm.profile.directoryHeading')}
  >
    <PathField
      id="swarm-start-directory"
      mode="directory"
      listDirectory={model.client.listDirectory}
      value={model.runDirectory}
      disabled={!model.selectedProfile ||
        model.directoryLoading ||
        model.pending === 'start'}
      onInput={(value) => (model.runDirectory = value)}
    />
  </FormField>
  <FormField controlId="swarm-goal" label={t('swarm.goal')}>
    <TextArea
      id="swarm-goal"
      value={model.goal}
      onInput={(value) => (model.goal = value)}
      rows="6"
      placeholder={t('swarm.goalPlaceholder')}
    />
  </FormField><Button
    variant="primary"
    loading={model.pending === 'start'}
    disabled={!model.selectedProfile ||
      model.directoryLoading ||
      !model.runDirectory.trim()}
    onClick={model.startSwarm}
    ><SwarmIcon kind="play" />{model.pending === 'start'
      ? t('swarm.starting')
      : t('swarm.startButton')}</Button
  >
</section>
