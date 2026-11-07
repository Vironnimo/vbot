<script module>
  const loadBoard = () => import('./FactoryBoard.svelte');
  const loadDetail = () => import('./CardDetail.svelte');
  const loadSetups = () => import('./FactorySetups.svelte');
  const loadLibrary = () => import('./FactoryLibrary.svelte');
</script>

<script>
  import { untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { provideNavigation } from '../../../../webui/src/lib/navigation.svelte.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import LazyContent from '../../../../webui/src/components/ui/LazyContent.svelte';
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
        parent.scrollTop = next === '' ? boardScroll.top : 0;
        parent.scrollLeft = next === '' ? boardScroll.left : 0;
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
        variant={['board', 'card'].includes(model.view)
          ? 'secondary'
          : 'tertiary'}
        aria-current={['board', 'card'].includes(model.view)
          ? 'page'
          : undefined}
        onClick={() => navigate('')}>{t('factory.board')}</Button
      >
      <Button
        variant={model.view === 'library' ? 'secondary' : 'tertiary'}
        aria-current={model.view === 'library' ? 'page' : undefined}
        onClick={() => navigate('/library')}>{t('factory.library')}</Button
      >
      <Button
        variant={model.view === 'setups' ? 'secondary' : 'tertiary'}
        aria-current={model.view === 'setups' ? 'page' : undefined}
        onClick={() => navigate('/setups')}>{t('factory.setups')}</Button
      >
    </nav>
    <span class="factory-wip" role="status"
      >{t('factory.running', {
        running: model.wip.running,
        limit: model.wip.limit,
      })}</span
    >
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
    {:else if model.view === 'library'}
      <LazyContent load={loadLibrary}
        >{#snippet children(Component)}<div use:routeScroll={model.route}>
            <Component {model} onNavigate={navigate} />
          </div>{/snippet}</LazyContent
      >
    {:else}
      <LazyContent load={loadSetups}
        >{#snippet children(Component)}<div use:routeScroll={model.route}>
            <Component {model} onNavigate={navigate} />
          </div>{/snippet}</LazyContent
      >
    {/if}
  </div>
</main>
