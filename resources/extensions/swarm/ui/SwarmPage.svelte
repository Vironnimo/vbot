<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ProfileEditor from './ProfileEditor.svelte';
  import { provideNavigation } from '../../../../webui/src/lib/navigation.svelte.js';
  import { createSwarmPageModel } from './pageModel.svelte.js';
  // The shared rules load before the areas' own stylesheets.
  import './swarmPage.css';
  import SwarmSidebar from './SwarmSidebar.svelte';
  import RunStartForm from './RunStartForm.svelte';
  import RunView from './RunView.svelte';
  import BoardComposer from './BoardComposer.svelte';
  import RunDeleteDialog from './RunDeleteDialog.svelte';
  import SwarmDeleteDialog from './SwarmDeleteDialog.svelte';
  import CommunicationDialog from './CommunicationDialog.svelte';
  import RunDetailsDialog from './RunDetailsDialog.svelte';

  let { bridgeClient = null } = $props();
  const model = createSwarmPageModel({
    get bridgeClient() {
      return bridgeClient;
    },
  });
  // The page's dialogs register as layers of the app's navigation, so Back
  // and Forward close them before leaving the page's place.
  provideNavigation({ registerLayer: model.client.registerLayer });
</script>

<svelte:head><title>{t('swarm.title')}</title></svelte:head>
<main class="swarm-page">
  <SwarmSidebar {model} />
  <div class="workspace">
    <!-- The page's notices stay above the shown view while it scrolls. -->
    {#if model.error || model.refreshError || model.loading}<div
        class="swarm-notices"
      >
        {#if model.error}<Banner variant="error" role="alert"
            >{model.error}</Banner
          >{/if}
        {#if model.refreshError}<Banner
            variant="error"
            role={model.connected ? 'status' : 'alert'}
            ><p>{model.refreshError}</p>
            {#if model.connected}<Button
                variant="tertiary"
                onClick={model.retry}>{t('common.retry')}</Button
              >{/if}</Banner
          >{/if}
        {#if model.loading}<Banner variant="info" role="status"
            >{t('swarm.loading')}</Banner
          >{/if}
      </div>{/if}
    {#if model.editor}
      {#key model.editorKey}
        <ProfileEditor
          bind:this={model.profileEditor}
          profile={model.editor === 'new' ? null : model.editor}
          newDraft={model.newProfileDraft}
          catalog={model.catalog}
          bridgeClient={model.client}
          onSave={model.saveProfile}
          onCancel={model.openStart}
          onDelete={model.runActions.confirmProfileDelete}
          onDraftChange={(next) => (model.newProfileDraft = next)}
        />
      {/key}
    {:else if model.selectedSwarm}<RunView {model} />{:else}<RunStartForm
        {model}
      />{/if}
  </div>
</main>

<!-- Modals render in place: the dialogs stay outside the page's layout. -->
<BoardComposer board={model.board} {model} />
<RunDeleteDialog {model} />
<SwarmDeleteDialog {model} />
<CommunicationDialog {model} />
<RunDetailsDialog {model} />
