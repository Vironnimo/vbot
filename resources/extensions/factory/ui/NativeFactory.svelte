<script>
  import { untrack } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import NativeRecordEditor from './NativeRecordEditor.svelte';
  import CardSession from './CardSession.svelte';
  import NativeActions from './NativeActions.svelte';
  import { createNativeFactoryModel } from './nativeModel.svelte.js';
  import { waitingLabel, eventLabel, timestamp } from './presentation.js';
  let { model, onNavigate } = $props();
  const native = createNativeFactoryModel(untrack(() => model));
  let project = $state(''),
    actions = $state(null),
    historyError = $state(''),
    editor = $state(null),
    memberEdit = $state(''),
    showChildren = $state(false),
    inspected = $state(null);
  const agentName = (member) =>
    model.catalog.agents?.find((item) => item.id === member.agent_id)?.name ||
    member.agent_name ||
    member.name ||
    member.agent_id;
  const projectName = (id) =>
    model.catalog.projects?.find((item) => item.id === id)?.name || id;
  const projects = $derived(
    (model.catalog.projects || []).map((item) => ({
      value: item.id,
      label: item.name || item.id,
    })),
  );
  const factoryRoute = (tab = '') =>
    `/factory/${encodeURIComponent(native.factoryId)}${tab ? '/' + tab : ''}`;
  const actorName = (actor) =>
    actor?.startsWith('member:')
      ? memberName(actor.slice(7))
      : actor === 'user'
        ? t('factory.origin.user')
        : actor;
  const sessionName = (session) => {
    const member = native.members.find(
      (item) => item.agent_id === session.session?.agent_id,
    );
    return member
      ? agentName(member)
      : session.display_name ||
          session.session?.agent_id ||
          session.participant_id;
  };
  const memberName = (id) =>
    native.members.find((item) => item.id === id)?.role ||
    native.members.find((item) => item.id === id)?.agent_id ||
    id ||
    t('factory.native.unassigned');
  const task = $derived(native.detail?.task);
  const topic = $derived(native.topic?.topic);
  const currentProject = $derived(native.factory?.project_id || project);
  const statuses = [
    { id: 'backlog', label: () => t('factory.native.backlog') },
    { id: 'ready', label: () => t('factory.native.ready') },
    { id: 'working', label: () => t('factory.native.working') },
    { id: 'blocked', label: () => t('factory.native.blocked') },
    { id: 'paused', label: () => t('factory.native.paused') },
    { id: 'review', label: () => t('factory.native.review') },
    { id: 'done', label: () => t('factory.native.done') },
    { id: 'cancelled', label: () => t('factory.native.cancelled') },
  ];
  const statusLabel = (state) =>
    statuses.find((item) => item.id === state)?.label() || state;
  function reasonLabel(reason) {
    switch (reason) {
      case 'dependencies':
        return t('factory.native.waitDependencies');
      case 'input':
        return t('factory.native.waitInput');
      case 'review_input':
        return t('factory.native.waitReview');
      case 'workflow':
        return t('factory.native.waitWorkflow');
      case 'capacity':
        return t('factory.native.waitCapacity');
      case 'factory_paused':
        return t('factory.native.factoryPaused');
      case 'changes_requested':
        return t('factory.native.changesRequested');
      case 'reviewer_retired':
        return t('factory.native.reviewerRetired');
      case 'workflow_failed':
        return t('factory.native.workflowFailed');
      case 'workflow_missing':
        return t('factory.native.workflowMissing');
      case 'review':
        return t('factory.native.review');
      default:
        return waitingLabel(reason);
    }
  }
  async function chooseProject(value) {
    project = value;
    native.selectProject(value);
    const found = native.factories.find(
      (item) => item.project_id === value && item.state !== 'archived',
    );
    await onNavigate(found ? `/factory/${encodeURIComponent(found.id)}` : '');
    if (!found) await native.refresh();
  }
  async function act(operation, record, extra = {}) {
    if (editor && !(await editor.flush())) return;
    const latest = operation.startsWith('task.')
      ? native.detail?.task
      : operation.startsWith('member.')
        ? native.members.find((item) => item.id === record.id)
        : operation.startsWith('factory.')
          ? native.factory
          : record;
    await native.mutate(operation, {
      id: record.id,
      expected_revision: latest?.revision ?? record.revision,
      ...extra,
    });
  }
  async function editMember(id) {
    if (editor && !(await editor.flush())) return;
    memberEdit = memberEdit === id ? '' : id;
  }
  async function taskModal(kind, values = {}) {
    if (editor && !(await editor.flush())) return;
    actions?.open(kind, values);
  }
</script>

<div class="native-factory">
  <div class="factory-board-toolbar">
    <Dropdown
      id="native-project"
      ariaLabel={t('factory.native.selectProject')}
      placeholder={t('factory.native.selectProject')}
      options={projects}
      value={currentProject}
      onOpenChange={model.pickerOpen}
      onValueChange={chooseProject}
    />
    {#if native.factory}<span
        >{native.factory.name &&
        native.factory.name !== native.factory.project_id
          ? native.factory.name
          : projectName(native.factory.project_id)}</span
      ><span
        >{native.factory.state === 'active'
          ? t('factory.native.active')
          : native.factory.state === 'paused'
            ? t('factory.native.paused')
            : t('factory.native.archived')}</span
      ><span role="status" class="factory-wip"
        >{t('factory.native.runningTasks', {
          running: native.tasks.filter((item) => item.state === 'working')
            .length,
          limit: native.factory.capacity,
        })}</span
      >{/if}
  </div>
  {#if native.cursors.factory}<Button onClick={() => native.more('factory')}
      >{t('factory.native.more')}</Button
    >{/if}
  {#if historyError}<Banner variant="error" role="alert">{historyError}</Banner
    >{/if}
  {#if native.error}<Banner variant="error" role="alert"
      >{native.error}<Button variant="tertiary" onClick={native.refresh}
        >{t('common.retry')}</Button
      ></Banner
    >{/if}
  {#if native.loading}<p role="status">{t('factory.native.loading')}</p>
  {:else if !native.factory}
    <p>
      {projects.length
        ? t('factory.native.noFactory')
        : t('factory.native.noProjects')}
    </p>
    <Button
      variant="primary"
      disabled={!currentProject}
      onClick={() => actions?.open('setup')}>{t('factory.native.setup')}</Button
    >
  {:else}
    <nav class="native-tabs" aria-label={t('factory.title')}>
      <Button
        variant={native.view === 'board' ? 'secondary' : 'tertiary'}
        onClick={() => onNavigate(factoryRoute())}>{t('factory.board')}</Button
      >
      <Button
        variant={native.view === 'team' ? 'secondary' : 'tertiary'}
        onClick={() => onNavigate(factoryRoute('team'))}
        >{t('factory.native.team')}</Button
      >
      <Button
        variant={native.view === 'discussions' ? 'secondary' : 'tertiary'}
        onClick={() => onNavigate(factoryRoute('discussions'))}
        >{t('factory.native.discussions')}</Button
      >
      <Button
        variant={native.view === 'settings' ? 'secondary' : 'tertiary'}
        onClick={() => onNavigate(factoryRoute('settings'))}
        >{t('factory.native.settings')}</Button
      >
    </nav>
    {#if native.view === 'board'}
      <div class="factory-board-toolbar">
        <Button variant="primary" onClick={() => actions?.open('task')}
          >{t('factory.native.newTask')}</Button
        ><label class="native-check"
          >{t('factory.native.showChildren')}<Toggle
            ariaLabel={t('factory.native.showChildren')}
            checked={showChildren}
            onChange={(value) => (showChildren = value)}
          /></label
        >
      </div>
      {#if !native.tasks.length}<p>{t('factory.native.noTasks')}</p>{/if}
      <div class="factory-board native-board">
        {#each statuses as state (state.id)}<section
            class="factory-column"
            data-native-state={state.id}
          >
            <header><h2>{state.label()}</h2></header>
            <ul class="factory-card-list">
              {#each native.tasks.filter((item) => item.state === state.id && (showChildren || !item.parent_id)) as item (item.id)}<li
                >
                  <button
                    class="factory-card"
                    onclick={() =>
                      onNavigate(`/task/${encodeURIComponent(item.id)}`)}
                    ><strong>{item.title}</strong><span
                      >{memberName(item.assignee_id)}</span
                    >{#if item.reason || item.state === 'ready'}<span
                        >{reasonLabel(item.reason || 'capacity')}</span
                      >{/if}{#if item.parent_id}<span
                        >{native.tasks.find((row) => row.id === item.parent_id)
                          ?.title || item.parent_id}</span
                      >{/if}{#if native.tasks.some((child) => child.parent_id === item.id)}<span
                        >{native.tasks.filter(
                          (child) =>
                            child.parent_id === item.id &&
                            child.state === 'done',
                        ).length}/{native.tasks.filter(
                          (child) => child.parent_id === item.id,
                        ).length}
                        {t('factory.native.children')}</span
                      >{/if}</button
                  >
                </li>{/each}
            </ul>
          </section>{/each}
      </div>
    {:else if native.view === 'settings'}
      {#key native.factory.id}<NativeRecordEditor
          bind:this={editor}
          kind="factory"
          record={native.factory}
          {native}
          {model}
        />{/key}
      {#if ['active', 'paused'].includes(native.factory.state)}
        <div class="native-actions">
          <Button
            onClick={() =>
              act('factory.update', native.factory, {
                state: native.factory.state === 'paused' ? 'active' : 'paused',
              })}
            >{native.factory.state === 'paused'
              ? t('factory.native.resume')
              : t('factory.native.pause')}</Button
          ><Button
            variant="danger"
            onClick={() => act('factory.archive', native.factory)}
            >{t('factory.native.archive')}</Button
          >
        </div>
      {/if}
    {:else if native.view === 'team'}
      <Button variant="primary" onClick={() => actions?.open('member')}
        >{t('factory.native.addMember')}</Button
      >
      <div class="native-members">
        {#each native.members as member (member.id)}<section
            class="native-panel"
          >
            <h2>
              {agentName(
                member,
              )}{#if member.role && member.role !== agentName(member)}
                · {member.role}{:else if member.id === native.factory.leader_member_id}
                · {t('factory.native.leader')}{/if}
            </h2>
            <p>
              {member.state === 'active'
                ? t('factory.native.active')
                : t('factory.native.retired')}
            </p>
            <p>
              {member.created_by_factory
                ? t('factory.native.createdAgent')
                : t('factory.native.linkedAgent')}
            </p>
            <Button onClick={() => editMember(member.id)}
              >{t('common.edit')}</Button
            >{#if member.state === 'active' && member.id !== native.factory.leader_member_id}<Button
                variant="tertiary"
                onClick={() => act('member.retire', member)}
                >{t('factory.native.retire')}</Button
              >{/if}{#if memberEdit === member.id}{#key member.id}<NativeRecordEditor
                  bind:this={editor}
                  kind="member"
                  record={member}
                  {native}
                  {model}
                />{/key}{/if}
          </section>{/each}
      </div>
    {:else if native.view === 'discussions'}
      <Button variant="primary" onClick={() => actions?.open('topic')}
        >{t('factory.native.newTopic')}</Button
      >{#if !native.topics.length}<p>{t('factory.native.noTopics')}</p>{/if}
      <ul class="native-list">
        {#each native.topics as item (item.id)}<li>
            <Button
              variant="tertiary"
              onClick={() =>
                onNavigate(`/topic/${encodeURIComponent(item.id)}`)}
              >{item.title}</Button
            ><span
              >{item.state === 'closed'
                ? t('factory.native.closed')
                : t('factory.native.open')}</span
            >
          </li>{/each}
      </ul>
    {:else if native.view === 'task' && task}
      <div class="native-actions">
        <Button onClick={() => onNavigate(factoryRoute())}
          >{t('factory.native.back')}</Button
        ><span>{statusLabel(task.state)} · {memberName(task.assignee_id)}</span
        >{#if task.reason}<span>{reasonLabel(task.reason)}</span>{/if}
      </div>
      {#key task.id}<NativeRecordEditor
          bind:this={editor}
          kind="task"
          record={task}
          {native}
          {model}
        />{/key}
      <div class="native-actions">
        {#if !['done', 'cancelled'].includes(task.state)}<Button
            onClick={() =>
              taskModal('assign', { assignee_id: task.assignee_id || '' })}
            >{t('factory.native.reassign')}</Button
          >{/if}{#if !['done', 'cancelled', 'paused'].includes(task.state)}<Button
            onClick={() => act('task.pause', task)}
            >{t('factory.native.pause')}</Button
          >{/if}{#if ['paused', 'blocked'].includes(task.state)}<Button
            onClick={() => act('task.resume', task)}
            >{t('factory.native.resume')}</Button
          >{/if}{#if ['working', 'ready', 'blocked'].includes(task.state)}<Button
            onClick={() => taskModal('complete')}
            >{t('factory.native.complete')}</Button
          >{/if}{#if !['done', 'cancelled'].includes(task.state)}<Button
            onClick={() => act('task.cancel', task)}
            >{t('factory.native.cancel')}</Button
          >{:else}<Button onClick={() => act('task.reopen', task)}
            >{t('factory.native.reopen')}</Button
          >{/if}{#if task.state === 'review'}<Button
            onClick={() => act('task.review', task, { approve: true })}
            >{t('factory.native.approve')}</Button
          ><Button onClick={() => taskModal('changes')}
            >{t('factory.native.changes')}</Button
          >{/if}
      </div>
      {#if typeof task.result === 'string' ? task.result.trim() : task.result?.outcome || task.result?.evidence}<section
          class="native-panel"
        >
          <h2>{t('factory.native.outcome')}</h2>
          <MarkdownContent
            source={typeof task.result === 'string'
              ? task.result
              : task.result.outcome || ''}
          />
          {#if task.result?.evidence}<h3>{t('factory.native.evidence')}</h3>
            <MarkdownContent source={task.result.evidence} />{/if}
        </section>{/if}
      <section class="native-panel">
        <h2>{t('factory.native.children')}</h2>
        <Button onClick={() => taskModal('task', { parent_id: task.id })}
          >{t('factory.native.newTask')}</Button
        >
        <ul>
          {#each native.detail.children || [] as child (child.id)}<li>
              <Button
                variant="tertiary"
                onClick={() =>
                  onNavigate(`/task/${encodeURIComponent(child.id)}`)}
                >{child.title}</Button
              >{memberName(child.assignee_id)} · {statusLabel(child.state)}
            </li>{/each}
        </ul>
      </section>
      <section class="native-panel">
        <h2>{t('factory.native.discussions')}</h2>
        <Button onClick={() => actions?.open('topic', { task_id: task.id })}
          >{t('factory.native.newTopic')}</Button
        >{#each native.detail.topics || [] as item (item.id)}<Button
            variant="tertiary"
            onClick={() => onNavigate(`/topic/${encodeURIComponent(item.id)}`)}
            >{item.title}</Button
          >{/each}
      </section>
      {#if task.workflow_card_id}<Button
          onClick={() =>
            onNavigate(`/card/${encodeURIComponent(task.workflow_card_id)}`)}
          >{t('factory.native.workflowCards')}</Button
        >{/if}
      <section class="native-panel">
        <h2>{t('factory.native.results')}</h2>
        <ol class="native-posts" data-native-results>
          {#each native.detail.results || [] as result (result.id)}<li>
              <strong>{actorName(result.actor)}</strong><time
                datetime={result.created_at}
                >{timestamp(result.created_at)}</time
              >{#if result.outcome}<MarkdownContent
                  source={result.outcome}
                />{/if}{#if result.evidence}<MarkdownContent
                  source={result.evidence}
                />{/if}{#if result.comment}<MarkdownContent
                  source={result.comment}
                />{/if}
            </li>{/each}
        </ol>
        {#if native.cursors.results}<Button
            onClick={() => native.more('results')}
            >{t('factory.native.moreResults')}</Button
          >{/if}
        <ol class="native-posts" data-native-events>
          {#each native.detail.events || [] as event (event.id)}<li>
              <strong>{eventLabel(event.event)}</strong> · {actorName(
                event.actor,
              )}
              <time datetime={event.created_at}
                >{timestamp(event.created_at)}</time
              >{#if event.detail?.reason}<p>
                  {reasonLabel(event.detail.reason)}
                </p>{/if}{#if event.detail?.text}<MarkdownContent
                  source={event.detail.text}
                />{/if}
            </li>{/each}
        </ol>
      </section>
      {#if native.cursors.events}<Button onClick={() => native.more('events')}
          >{t('factory.native.moreEvents')}</Button
        >{/if}
      <section class="native-panel">
        <h2>{t('factory.native.history')}</h2>
        {#each native.detail.sessions || [] as session (session.group_id)}{@render historyLink(
            session,
          )}{/each}
      </section>
    {:else if native.view === 'topic' && topic}
      <h2>{topic.title}</h2>
      <p>{(topic.participants || []).map(memberName).join(', ')}</p>
      {#if topic.task_id}<Button
          variant="tertiary"
          onClick={() =>
            onNavigate(`/task/${encodeURIComponent(topic.task_id)}`)}
          >{t('factory.native.task')}</Button
        >{/if}
      <div class="native-actions">
        {#if topic.state === 'open'}<Button
            onClick={() => actions?.open('invite')}
            >{t('factory.native.invite')}</Button
          ><Button variant="primary" onClick={() => actions?.open('post')}
            >{t('factory.native.post')}</Button
          ><Button onClick={() => act('topic.close', topic)}
            >{t('factory.native.closeTopic')}</Button
          >{:else}<span>{t('factory.native.closed')}</span>{/if}
      </div>
      {#if native.topic.sessions?.length}<section class="native-panel">
          <h2>{t('factory.native.history')}</h2>
          {#each native.topic.sessions as session (session.group_id)}{@render historyLink(
              session,
            )}{/each}
        </section>{/if}
      <ol class="native-posts">
        {#each native.topic.posts || [] as post (post.id)}<li>
            <strong
              >{post.author?.member_id
                ? memberName(post.author.member_id)
                : typeof post.author === 'string'
                  ? post.author.startsWith('member:')
                    ? memberName(post.author.slice(7))
                    : t('factory.origin.user')
                  : t('factory.origin.user')}</strong
            ><MarkdownContent source={post.text} />
          </li>{/each}
      </ol>
    {/if}
  {/if}
  {#if native.view === 'board' && native.cursors.task}<Button
      onClick={() => native.more('task')}>{t('factory.native.more')}</Button
    >{/if}
  {#if native.view === 'team' && native.cursors.member}<Button
      onClick={() => native.more('member')}>{t('factory.native.more')}</Button
    >{/if}
  {#if native.view === 'discussions' && native.cursors.topic}<Button
      onClick={() => native.more('topic')}>{t('factory.native.more')}</Button
    >{/if}
  {#if native.view === 'topic' && native.cursors.post}<Button
      onClick={() => native.more('post')}>{t('factory.native.more')}</Button
    >{/if}
</div>
{#if inspected}<CardSession
    client={model.client}
    groupId={inspected.group_id}
    participant={inspected}
    onError={(value) => (historyError = value)}
    onClose={() => (inspected = null)}
  />{/if}
<NativeActions
  bind:this={actions}
  {model}
  {native}
  {currentProject}
  {onNavigate}
/>
{#snippet historyLink(session)}<div class="native-actions">
    <span>{sessionName(session)}</span><Button
      onClick={() => model.openSession(session.session)}
      >{t('factory.native.openSession')}</Button
    >{#if session.group_id && session.participant_id}<Button
        variant="tertiary"
        onClick={() => (inspected = session)}
        >{t('factory.native.readHistory')}</Button
      >{/if}
  </div>{/snippet}

<style>
  .native-factory {
    max-width: 1600px;
    margin: 0 auto;
  }
  .native-tabs,
  .native-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
    align-items: center;
  }
  .native-members {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
    margin-top: 16px;
  }
  .native-panel {
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px;
    margin: 16px 0;
    background: var(--surface);
  }
  .native-panel h2 {
    font-size: 16px;
    margin: 0 0 12px;
  }
  .native-list,
  .native-posts {
    list-style: none;
    padding: 0;
  }
  .native-list li {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px;
    border-bottom: 1px solid var(--border);
  }
  .native-posts li {
    padding: 16px;
    border-bottom: 1px solid var(--border);
  }
  .native-check {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 8px 0;
    color: var(--text-hi);
  }
  .native-board {
    grid-auto-columns: minmax(220px, 1fr);
  }

  @media (max-width: 600px) {
    .native-members {
      grid-template-columns: 1fr;
    }
  }
</style>
