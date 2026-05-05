<script>
  import { t } from '$lib/i18n.js';

  const panels = [
    {
      id: 'general',
      label: () => t('settings.general.title', 'General'),
      subtitle: () =>
        t(
          'settings.general.subtitle',
          'Server connection and workspace path settings.',
        ),
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title', 'Providers'),
      subtitle: () =>
        t(
          'settings.providers.subtitle',
          'API keys and connection status for LLM providers.',
        ),
    },
    {
      id: 'appearance',
      label: () => t('settings.appearance.title', 'Appearance'),
      subtitle: () =>
        t('settings.appearance.subtitle', 'Display and language preferences.'),
    },
  ];

  let activePanelId = $state('general');
  let autoScrollEnabled = $state(true);
  let tokenCountsEnabled = $state(true);
  let languageDropdownOpen = $state(false);
  let selectedLanguage = $state('English');

  const languageOptions = [
    'English',
    'Deutsch',
    'Français',
    'Español',
    'Português',
    '日本語',
    '中文',
    '한국어',
  ];

  let activePanel = $derived(
    panels.find((panel) => panel.id === activePanelId) ?? panels[0],
  );

  function selectPanel(panelId) {
    activePanelId = panelId;
    languageDropdownOpen = false;
  }

  function toggleAutoScroll() {
    autoScrollEnabled = !autoScrollEnabled;
  }

  function toggleTokenCounts() {
    tokenCountsEnabled = !tokenCountsEnabled;
  }

  function selectLanguage(language) {
    selectedLanguage = language;
    languageDropdownOpen = false;
  }
</script>

<section class="settings-layout view active" aria-labelledby="settings-title">
  <nav
    class="settings-nav"
    aria-label={t('settings.sections', 'Settings sections')}
  >
    <div class="settings-nav-title">{t('settings.title', 'Settings')}</div>
    {#each panels as panel (panel.id)}
      <button
        class:snav-item--active={panel.id === activePanelId}
        class="snav-item"
        type="button"
        aria-current={panel.id === activePanelId ? 'page' : undefined}
        onclick={() => selectPanel(panel.id)}
      >
        {panel.label()}
      </button>
    {/each}
  </nav>

  <div class="settings-content">
    <div class="s-panel">
      <h2 id="settings-title" class="s-panel-title">{activePanel.label()}</h2>
      <p class="s-panel-sub">{activePanel.subtitle()}</p>

      {#if activePanelId === 'general'}
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.general.serverHost', 'Server host')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.general.serverHostDescription',
                'Address and port the vBot server listens on.',
              )}
            </div>
          </div>
          <input class="s-input" type="text" value="127.0.0.1:8017" disabled />
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.general.dataDirectory', 'Data directory')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.general.dataDirectoryDescription',
                'Root path for agents, sessions, and workspace files.',
              )}
            </div>
          </div>
          <input class="s-input" type="text" value="~/.vbot" disabled />
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.general.autoScroll', 'Auto-scroll chat')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.general.autoScrollDescription',
                'Scroll to bottom as new tokens arrive.',
              )}
            </div>
          </div>
          <button
            class:off={!autoScrollEnabled}
            class="toggle on"
            type="button"
            role="switch"
            aria-checked={autoScrollEnabled}
            aria-label={t('settings.general.autoScroll', 'Auto-scroll chat')}
            onclick={toggleAutoScroll}
          >
            <span class="t-knob"></span>
          </button>
        </div>
      {:else if activePanelId === 'providers'}
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">OpenRouter</div>
            <div class="s-row-desc">
              {t('settings.providers.openRouterDescription', 'API key via')}
              <code>~/.vbot/.env</code>
            </div>
          </div>
          <span class="chip chip-amber"
            >{t('settings.placeholder', 'Placeholder')}</span
          >
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">Anthropic</div>
            <div class="s-row-desc">
              {t(
                'settings.providers.anthropicDescription',
                'Direct Anthropic Messages API.',
              )}
            </div>
          </div>
          <span class="chip chip-amber"
            >{t('settings.placeholder', 'Placeholder')}</span
          >
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">Ollama</div>
            <div class="s-row-desc">
              {t(
                'settings.providers.ollamaDescription',
                'Local model server at localhost:11434',
              )}
            </div>
          </div>
          <span class="chip chip-amber"
            >{t('settings.placeholder', 'Placeholder')}</span
          >
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.providers.customEndpoint', 'Custom endpoint')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.providers.customEndpointDescription',
                'Add an OpenAI-compatible API endpoint.',
              )}
            </div>
          </div>
          <button class="btn-outline" type="button" disabled>
            {t('settings.providers.configure', 'Configure…')}
          </button>
        </div>
      {:else}
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.appearance.language', 'Language')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.appearance.languageDescription',
                'Interface language.',
              )}
            </div>
          </div>
          <div
            class:open={languageDropdownOpen}
            class="dropdown settings-language"
          >
            <button
              class="dropdown-trigger"
              type="button"
              aria-expanded={languageDropdownOpen}
              onclick={() => (languageDropdownOpen = !languageDropdownOpen)}
            >
              <span>{selectedLanguage}</span>
              <svg
                class="dropdown-chevron"
                viewBox="0 0 12 12"
                aria-hidden="true"
              >
                <path d="M2 4l4 4 4-4" />
              </svg>
            </button>
            <div class="dropdown-list">
              {#each languageOptions as language (language)}
                <button
                  class:selected={language === selectedLanguage}
                  class="dropdown-option"
                  type="button"
                  onclick={() => selectLanguage(language)}
                >
                  {language}
                </button>
              {/each}
            </div>
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.appearance.showTokenCounts', 'Show token counts')}
            </div>
            <div class="s-row-desc">
              {t(
                'settings.appearance.showTokenCountsDescription',
                'Display token usage in the chat header.',
              )}
            </div>
          </div>
          <button
            class:off={!tokenCountsEnabled}
            class="toggle on"
            type="button"
            role="switch"
            aria-checked={tokenCountsEnabled}
            aria-label={t(
              'settings.appearance.showTokenCounts',
              'Show token counts',
            )}
            onclick={toggleTokenCounts}
          >
            <span class="t-knob"></span>
          </button>
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .settings-layout {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background: var(--surface);
  }

  .settings-nav {
    display: flex;
    width: 168px;
    min-width: 168px;
    flex-shrink: 0;
    flex-direction: column;
    gap: 1px;
    padding: 20px 0;
    border-right: 1px solid var(--border);
  }

  .settings-nav-title {
    padding: 0 16px 10px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .snav-item {
    width: 100%;
    padding: 8px 16px;
    border-radius: 0;
    color: var(--text-lo);
    background: transparent;
    font-size: 13.5px;
    font-weight: 500;
    text-align: left;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .snav-item:hover,
  .snav-item:focus-visible {
    color: var(--text-med);
    background: rgba(255, 255, 255, 0.03);
    outline: none;
  }

  .snav-item--active {
    color: var(--accent);
    background: var(--accent-dim);
  }

  .settings-content {
    min-width: 0;
    flex: 1;
    overflow-y: auto;
    padding: 24px 32px;
  }

  .s-panel-title {
    margin-bottom: 4px;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .s-panel-sub {
    margin-bottom: 24px;
    color: var(--text-lo);
    font-size: 12.5px;
  }

  .s-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }

  .s-row:last-child {
    border-bottom: 0;
  }

  .s-row-info {
    flex: 1;
    min-width: 0;
  }

  .s-row-label {
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 500;
  }

  .s-row-desc {
    margin-top: 2px;
    color: var(--text-lo);
    font-size: 12px;
    line-height: 1.4;
  }

  .s-row-desc :global(code) {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
  }

  .s-input {
    min-width: 180px;
  }

  .toggle.off {
    border-color: var(--border-2);
    background: var(--surface-3);
  }

  .toggle.off .t-knob {
    left: 2px;
  }

  .settings-language {
    min-width: 160px;
  }

  .dropdown-trigger {
    width: 100%;
  }

  .dropdown-option {
    display: block;
    width: 100%;
    border: 0;
    background: transparent;
    text-align: left;
  }

  @media (max-width: 760px) {
    .settings-layout {
      flex-direction: column;
      overflow: auto;
    }

    .settings-nav {
      width: 100%;
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }

    .s-row {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
