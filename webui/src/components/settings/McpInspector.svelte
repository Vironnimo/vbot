<script>
  import { t } from '$lib/i18n.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  let { state: panelState, controller, status } = $props();
  let capabilityQuery = $state('');
</script>

{#if panelState.inspector}
  <Modal
    title={t('mcp.inspectTitle', {
      name: panelState.inspector.id,
    })}
    onClose={controller.closeInspector}
    class="mcp-modal"
  >
    {#snippet body()}
      <div class="modal-body mcp-inspector">
        <form
          class="mcp-capability-search"
          onsubmit={(event) => {
            event.preventDefault();
            void controller.inspect(panelState.inspector.id, {
              query: capabilityQuery,
            });
          }}
        >
          <TextField
            ariaLabel={t('mcp.searchTools')}
            placeholder={t('mcp.searchTools')}
            value={capabilityQuery}
            onInput={(value) => {
              capabilityQuery = value;
            }}
          />
          <Button type="submit" variant="secondary">{t('common.search')}</Button
          >
          <Button
            variant="secondary"
            onClick={() => {
              capabilityQuery = '';
              void controller.inspect(panelState.inspector.id);
            }}>{t('mcp.showAll')}</Button
          >
        </form>
        {#if panelState.inspector.loading}
          <Banner variant="neutral" role="status">{t('common.loading')}</Banner>
        {:else if panelState.inspector.error}
          <Banner variant="error" role="alert">
            {panelState.inspector.error}
            <Button
              variant="secondary"
              onClick={() =>
                controller.inspect(panelState.inspector.id, {
                  query: panelState.inspector.query,
                  offset: panelState.inspector.offset,
                })}>{t('common.retry')}</Button
            >
          </Banner>
        {:else if panelState.inspector.data}
          {@const catalog = panelState.inspector.data}
          <div class="mcp-heading">
            <p>
              {t('mcp.accessHelp')}
            </p>
            <StatusChip variant={status(catalog).variant}
              >{status(catalog).label}</StatusChip
            >
          </div>
          {#if !catalog.catalog_available}
            <EmptyState
              density="compact"
              title={t('mcp.catalogMissing')}
              description={t('mcp.catalogMissingHint')}
            />
          {:else}
            {#if catalog.state !== 'connected'}
              <Banner variant="warn">{t('mcp.catalogStale')}</Banner>
            {/if}
            <h4>
              {t('mcp.availableTools', {
                count: catalog.total,
              })}
            </h4>
            {#if catalog.tools.length}
              <ul class="mcp-capability-list">
                {#each catalog.tools as tool (tool.target)}
                  <li>
                    <strong>{tool.name}</strong>
                    <p>
                      {tool.description.slice(0, 160)}{tool.description.length >
                      160
                        ? '…'
                        : ''}
                    </p>
                    {#if tool.description.length > 160}
                      <details>
                        <summary>{t('mcp.fullDescription')}</summary>
                        <p class="mcp-guidance">{tool.description}</p>
                      </details>
                    {/if}
                  </li>
                {/each}
              </ul>
              <div class="mcp-actions">
                <Button
                  variant="secondary"
                  disabled={catalog.previous_offset == null}
                  onClick={() =>
                    controller.inspect(catalog.id, {
                      query: panelState.inspector.query,
                      offset: catalog.previous_offset,
                    })}>{t('common.previous')}</Button
                >
                <Button
                  variant="secondary"
                  disabled={catalog.next_offset == null}
                  onClick={() =>
                    controller.inspect(catalog.id, {
                      query: panelState.inspector.query,
                      offset: catalog.next_offset,
                    })}>{t('common.next')}</Button
                >
              </div>
            {:else}
              <EmptyState
                density="compact"
                title={t('mcp.noToolsFound')}
                description={t('mcp.noToolsFoundHint')}
              />
            {/if}
            {#if catalog.instructions}
              <details>
                <summary>{t('mcp.serverGuidance')}</summary>
                <p class="mcp-guidance">{catalog.instructions}</p>
              </details>
            {/if}
            {#if catalog.prompts.length}
              <details>
                <summary>{t('mcp.serverPrompts')}</summary>
                <ul class="mcp-capability-list">
                  {#each catalog.prompts as prompt (prompt.name)}<li>
                      <strong>{prompt.name}</strong>
                      <p>{prompt.description}</p>
                    </li>{/each}
                </ul>
              </details>
            {/if}
          {/if}
        {/if}
      </div>
    {/snippet}
  </Modal>
{/if}
