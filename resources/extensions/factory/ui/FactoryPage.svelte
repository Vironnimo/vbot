<script module>
  const loadNative = () => import('./NativeFactory.svelte');
  const loadBoard = () => import('./FactoryBoard.svelte');
  const loadDetail = () => import('./CardDetail.svelte');
  const loadWorkflows = () => import('./FactoryWorkflows.svelte');
</script>

<script>
  import { untrack } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import { provideNavigation } from '@vbot/extension-sdk/lib/navigation.svelte.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import LazyContent from '@vbot/extension-sdk/ui/LazyContent.svelte';
  import { createFactoryPageModel } from './pageModel.svelte.js';
  import './factoryPage.css';

  let { bridgeClient = null } = $props();
  const model = createFactoryPageModel(untrack(() => bridgeClient));
  provideNavigation({ registerLayer: model.client.registerLayer });
  // route.push enters the App navigator's shared autosave gate. The host owns
  // failed-save / still-saving / release behavior, including internal routes.
  const navigate = (route) => model.navigate(route);
  let scrollport = $state(null);
  let boardScroll = { top: 0, left: 0 };
  function rememberBoardScroll() {
    if (model.view === 'board' && scrollport)
      boardScroll = { top: scrollport.scrollTop, left: scrollport.scrollLeft };
  }
  function routeScroll(node, route) {
    let current = route;
    const restore = (next) => {
      const parent = node.closest('.factory-scrollport');
      if (parent) {
        parent.scrollTop = next === '/cards' ? boardScroll.top : 0;
        parent.scrollLeft = next === '/cards' ? boardScroll.left : 0;
      }
    };
    restore(route);
    return {
      update(next) {
        if (next !== current) {
          current = next;
          restore(next);
        }
      },
    };
  }
</script>

<svelte:head><title>{t('factory.title')}</title></svelte:head>
<main class="factory-page">
  <header class="factory-header">
    <div class="factory-brand"><h1>{t('factory.title')}</h1></div>
    <nav class="factory-navigation" aria-label={t('factory.title')}>
      <Button
        variant={model.view === 'native' ? 'secondary' : 'tertiary'}
        aria-current={model.view === 'native' ? 'page' : undefined}
        onClick={() => navigate('')}>{t('factory.native.factories')}</Button
      >
      <Button
        variant={model.view === 'board' ? 'secondary' : 'tertiary'}
        onClick={() => navigate('/cards')}
        >{t('factory.native.workflowCards')}</Button
      >
      <Button
        variant={model.view === 'workflows' ? 'secondary' : 'tertiary'}
        aria-current={model.view === 'workflows' ? 'page' : undefined}
        onClick={() => navigate('/workflows')}>{t('factory.workflows')}</Button
      >
    </nav>
    {#if model.view !== 'native'}<span class="factory-wip" role="status"
        >{t('factory.running', {
          running: model.wip.running,
          limit: model.wip.limit,
        })}</span
      >{/if}
  </header>
  <div
    class="factory-scrollport"
    bind:this={scrollport}
    onscroll={rememberBoardScroll}
  >
    {#if model.error}<Banner variant="error" role="alert">{model.error}</Banner
      >{/if}
    {#if model.refreshError}
      <Banner variant="error" role="status"
        >{model.refreshError}<Button
          variant="tertiary"
          onClick={() => model.refresh({ full: true })}
          >{t('common.retry')}</Button
        ></Banner
      >
    {/if}
    {#if model.loading}
      <p class="factory-loading" role="status">{t('common.loading')}</p>
    {:else if model.view === 'native'}
      <LazyContent load={loadNative}
        >{#snippet children(Component)}<Component
            {model}
            onNavigate={navigate}
          />{/snippet}</LazyContent
      >
    {:else if model.view === 'board'}
      <LazyContent load={loadBoard}
        >{#snippet children(Component)}<div use:routeScroll={model.route}>
            <Component {model} onNavigate={navigate} />
          </div>{/snippet}</LazyContent
      >
    {:else if model.view === 'card'}
      {#if model.detail?.card.id === model.cardId}
        {#key model.cardId}<LazyContent load={loadDetail}
            >{#snippet children(Component)}<div use:routeScroll={model.route}>
                <Component {model} onNavigate={navigate} />
              </div>{/snippet}</LazyContent
          >{/key}
      {:else if model.detailLoading}
        <p class="factory-loading" role="status">{t('common.loading')}</p>
      {:else}<p>{t('factory.cardMissing')}</p>{/if}
    {:else}
      <LazyContent load={loadWorkflows}
        >{#snippet children(Component)}<div use:routeScroll={model.route}>
            <Component {model} onNavigate={navigate} />
          </div>{/snippet}</LazyContent
      >
    {/if}
  </div>
</main>
