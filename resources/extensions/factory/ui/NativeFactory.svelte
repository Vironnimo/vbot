<script>
  import { untrack } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Checkbox from '@vbot/extension-sdk/ui/Checkbox.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import NativeRecordEditor from './NativeRecordEditor.svelte';
  import CardSession from './CardSession.svelte';
  import { createNativeFactoryModel } from './nativeModel.svelte.js';
  import { isChatModel } from '@vbot/extension-sdk/lib/modelSelection.js';
  import { waitingLabel, eventLabel, timestamp } from './presentation.js';
  import { normalizeLimits, limitsProblem } from './limits.js';
  let { model, onNavigate } = $props();
  const native = createNativeFactoryModel(untrack(() => model));
  let project = $state(''),
    modal = $state(''),
    form = $state({}),
    formError = $state(''),
    historyError = $state(''),
    editor = $state(null),
    memberEdit = $state(''),
    showChildren = $state(false),
    inspected = $state(null);
  const active = $derived(
    native.members.filter((item) => item.state === 'active'),
  );
  const memberOptions = $derived([
    { value: '', label: t('factory.native.unassigned') },
    ...active.map((item) => ({
      value: item.id,
      label: item.role || item.agent_id,
    })),
  ]);
  const chatModels = $derived((model.catalog.models || []).filter(isChatModel));
  const agentName = (member) =>
    model.catalog.agents?.find((item) => item.id === member.agent_id)?.name ||
    member.agent_name ||
    member.name ||
    member.agent_id;
  const projectName = (id) =>
    model.catalog.projects?.find((item) => item.id === id)?.name || id;
  const agents = $derived(
    (model.catalog.agents || []).map((item) => ({
      value: item.id,
      label: item.name || item.id,
    })),
  );
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
  function open(kind, values = {}) {
    modal = kind;
    formError = '';
    form = {
      request_id: crypto.randomUUID(),
      agent_id: '',
      name: '',
      model: '',
      role: '',
      instructions: '',
      leader_agent_id: '',
      leader_name: '',
      title: '',
      objective: '',
      conditions: '',
      assignee_id: '',
      parent_id: '',
      dependencies: [],
      priority: 0,
      reviewer_id: '',
      user_approval: false,
      workflow_id: '',
      limits: {},
      text: '',
      member_ids: [],
      task_id: '',
      outcome: '',
      evidence: '',
      ...values,
    };
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
  async function submit() {
    formError = '';
    const id = native.factoryId;
    let operation, args;
    if (modal === 'setup') {
      if (
        !currentProject ||
        (!form.leader_agent_id && (!form.leader_name.trim() || !form.model))
      ) {
        formError = t('factory.native.leaderRequired');
        return;
      }
      operation = 'factory.create';
      args = {
        project_id: currentProject,
        remit: form.instructions,
        ...(form.leader_agent_id
          ? { leader_agent_id: form.leader_agent_id }
          : {
              leader_name: form.leader_name.trim(),
              leader_model: form.model,
              request_id: form.request_id,
            }),
      };
    } else if (modal === 'member') {
      if (form.new ? !form.name.trim() : !form.agent_id) {
        formError = t('factory.native.required');
        return;
      }
      operation = form.new ? 'member.create' : 'member.add';
      args = {
        factory_id: id,
        ...(form.new ? {} : { agent_id: form.agent_id }),
        role: form.role,
        instructions: form.instructions,
        ...(form.new
          ? {
              name: form.name.trim(),
              ...(form.model ? { model: form.model } : {}),
              request_id: form.request_id,
            }
          : {}),
      };
    } else if (modal === 'task') {
      if (
        !form.title.trim() ||
        !form.objective.trim() ||
        limitsProblem(form.limits)
      ) {
        formError = limitsProblem(form.limits) || t('factory.native.required');
        return;
      }
      operation = 'task.create';
      args = {
        factory_id: id,
        ...Object.fromEntries(
          [
            'title',
            'objective',
            'conditions',
            'assignee_id',
            'parent_id',
            'dependencies',
            'reviewer_id',
            'user_approval',
            'workflow_id',
          ].map((key) => [key, form[key]]),
        ),
        priority: Number(form.priority),
        limits: normalizeLimits(form.limits),
      };
    } else if (modal === 'topic') {
      if (!form.title.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'topic.create';
      args = {
        factory_id: id,
        title: form.title,
        task_id: form.task_id,
        member_ids: form.member_ids,
        text: form.text,
      };
    } else if (modal === 'invite') {
      operation = 'topic.invite';
      args = {
        id: topic.id,
        expected_revision: topic.revision,
        member_ids: form.member_ids,
        text: form.text,
      };
    } else if (modal === 'post') {
      if (!form.text.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'topic.post';
      args = {
        id: topic.id,
        text: form.text,
        recipients: form.member_ids,
        source_receipt: form.request_id,
      };
    } else if (modal === 'assign') {
      operation = 'task.assign';
      args = {
        id: task.id,
        expected_revision: task.revision,
        assignee_id: form.assignee_id,
      };
    } else if (modal === 'complete') {
      if (!form.outcome.trim() || !form.evidence.trim()) {
        formError = t('factory.native.resultRequired');
        return;
      }
      operation = 'task.complete';
      args = {
        id: task.id,
        expected_revision: task.revision,
        outcome: form.outcome,
        evidence: form.evidence,
      };
    } else if (modal === 'changes') {
      if (!form.text.trim()) {
        formError = t('factory.native.required');
        return;
      }
      operation = 'task.review';
      args = {
        id: task.id,
        expected_revision: task.revision,
        approve: false,
        findings: form.text,
      };
    }
    if (!operation) return;
    const result = await native.mutate(operation, args);
    if (!result) return;
    modal = '';
    if (operation === 'factory.create')
      await onNavigate(`/factory/${encodeURIComponent(result.factory.id)}`);
    if (operation === 'task.create')
      await onNavigate(`/task/${encodeURIComponent(result.task.id)}`);
    if (operation === 'topic.create')
      await onNavigate(`/topic/${encodeURIComponent(result.topic.id)}`);
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
    open(kind, values);
  }
  const modalTitle = $derived(
    modal === 'setup'
      ? t('factory.native.setup')
      : modal === 'member'
        ? t('factory.native.addMember')
        : modal === 'task'
          ? t('factory.native.newTask')
          : modal === 'topic'
            ? t('factory.native.newTopic')
            : modal === 'invite'
              ? t('factory.native.invite')
              : modal === 'post'
                ? t('factory.native.post')
                : modal === 'assign'
                  ? t('factory.native.reassign')
                  : modal === 'complete'
                    ? t('factory.native.complete')
                    : t('factory.native.changes'),
  );
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
      onClick={() => open('setup')}>{t('factory.native.setup')}</Button
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
        <Button variant="primary" onClick={() => open('task')}
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
      <Button variant="primary" onClick={() => open('member')}
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
      <Button variant="primary" onClick={() => open('topic')}
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
        <Button onClick={() => open('topic', { task_id: task.id })}
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
        {#if topic.state === 'open'}<Button onClick={() => open('invite')}
            >{t('factory.native.invite')}</Button
          ><Button variant="primary" onClick={() => open('post')}
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
{#if modal}<Modal
    title={modalTitle}
    onClose={() => (modal = '')}
    closeDisabled={Boolean(native.pending)}
    >{#snippet body()}<div class="modal-body factory-form">
        {#if modal === 'setup'}
          <label for="native-leader">{t('factory.native.existingAgent')}</label
          ><Dropdown
            id="native-leader"
            ariaLabel={t('factory.native.existingAgent')}
            value={form.leader_agent_id}
            options={[
              { value: '', label: t('factory.native.newLeader') },
              ...agents,
            ]}
            onValueChange={(value) => (form.leader_agent_id = value)}
          />
          {#if !form.leader_agent_id}<label for="native-leader-name"
              >{t('factory.native.agentName')}</label
            ><TextField
              id="native-leader-name"
              value={form.leader_name}
              onInput={(value) => (form.leader_name = value)}
            />{@render modelField()}{/if}
          <label for="native-setup-remit">{t('factory.native.remit')}</label
          ><TextArea
            id="native-setup-remit"
            value={form.instructions}
            onInput={(value) => (form.instructions = value)}
          />
        {:else if modal === 'member'}
          <label class="native-check"
            >{t('factory.native.newAgent')}<Toggle
              checked={Boolean(form.new)}
              ariaLabel={t('factory.native.newAgent')}
              onChange={(value) => {
                form.new = value;
                form.agent_id = '';
              }}
            /></label
          >
          {#if form.new}<label for="native-agent-name"
              >{t('factory.native.agentName')}</label
            ><TextField
              id="native-agent-name"
              value={form.name}
              onInput={(value) => (form.name = value)}
            />{@render modelField()}{:else}<Dropdown
              id="native-agent"
              ariaLabel={t('factory.native.existingAgent')}
              options={agents}
              value={form.agent_id}
              onValueChange={(value) => (form.agent_id = value)}
            />{/if}
          <label for="native-member-role">{t('factory.native.role')}</label
          ><TextField
            id="native-member-role"
            value={form.role}
            onInput={(value) => (form.role = value)}
          /><label for="native-member-instructions"
            >{t('factory.native.instructions')}</label
          ><TextArea
            id="native-member-instructions"
            value={form.instructions}
            onInput={(value) => (form.instructions = value)}
          />
        {:else if modal === 'task'}
          <label for="native-create-title">{t('factory.cardTitle')}</label
          ><TextField
            id="native-create-title"
            value={form.title}
            onInput={(value) => (form.title = value)}
          />
          <label for="native-create-objective"
            >{t('factory.native.objective')}</label
          ><TextArea
            id="native-create-objective"
            value={form.objective}
            onInput={(value) => (form.objective = value)}
          />
          <label for="native-create-conditions"
            >{t('factory.native.criteria')}</label
          ><TextArea
            id="native-create-conditions"
            value={form.conditions}
            onInput={(value) => (form.conditions = value)}
          />
          {@render ownerField()}
          <label for="native-create-priority"
            >{t('factory.native.priority')}</label
          ><TextField
            id="native-create-priority"
            type="number"
            value={form.priority}
            onInput={(value) => (form.priority = value)}
          />
          <label for="native-create-parent">{t('factory.native.parent')}</label
          ><Dropdown
            id="native-create-parent"
            ariaLabel={t('factory.native.parent')}
            value={form.parent_id}
            options={[
              { value: '', label: t('factory.native.none') },
              ...native.tasks.map((item) => ({
                value: item.id,
                label: item.title,
              })),
            ]}
            onValueChange={(value) => (form.parent_id = value)}
          />
          <fieldset>
            <legend>{t('factory.native.dependencies')}</legend
            >{#each native.tasks as item (item.id)}<Checkbox
                checked={form.dependencies.includes(item.id)}
                onChange={(checked) =>
                  (form.dependencies = checked
                    ? [...form.dependencies, item.id]
                    : form.dependencies.filter((id) => id !== item.id))}
                >{item.title}</Checkbox
              >{/each}
          </fieldset>
          <label for="native-create-reviewer"
            >{t('factory.native.reviewer')}</label
          ><Dropdown
            id="native-create-reviewer"
            ariaLabel={t('factory.native.reviewer')}
            value={form.reviewer_id}
            options={memberOptions}
            onValueChange={(value) => (form.reviewer_id = value)}
          />
          <label class="native-check"
            >{t('factory.native.userApproval')}<Toggle
              ariaLabel={t('factory.native.userApproval')}
              checked={form.user_approval}
              onChange={(value) => (form.user_approval = value)}
            /></label
          >
          <label for="native-create-workflow"
            >{t('factory.native.workflowOptional')}</label
          ><Dropdown
            id="native-create-workflow"
            ariaLabel={t('factory.native.workflowOptional')}
            value={form.workflow_id}
            options={[
              { value: '', label: t('factory.native.none') },
              ...model.workflows
                .filter((item) => item.enabled)
                .map((item) => ({ value: item.id, label: item.name })),
            ]}
            onValueChange={(value) => (form.workflow_id = value)}
          />
          <LimitsEditor
            bind:value={form.limits}
            prefix="native-create-limit"
            label={t('factory.native.budget')}
          />
        {:else if modal === 'assign'}{@render ownerField()}
        {:else if modal === 'complete'}<label for="native-outcome"
            >{t('factory.native.outcome')}</label
          ><TextArea
            id="native-outcome"
            value={form.outcome}
            onInput={(value) => (form.outcome = value)}
          /><label for="native-evidence">{t('factory.native.evidence')}</label
          ><TextArea
            id="native-evidence"
            value={form.evidence}
            onInput={(value) => (form.evidence = value)}
          />
        {:else}
          {#if modal === 'topic'}<label for="native-topic-title"
              >{t('factory.native.topicTitle')}</label
            ><TextField
              id="native-topic-title"
              value={form.title}
              onInput={(value) => (form.title = value)}
            /><label for="native-topic-task"
              >{t('factory.native.taskLink')}</label
            ><Dropdown
              id="native-topic-task"
              ariaLabel={t('factory.native.taskLink')}
              value={form.task_id}
              options={[
                { value: '', label: t('factory.native.none') },
                ...native.tasks.map((item) => ({
                  value: item.id,
                  label: item.title,
                })),
              ]}
              onValueChange={(value) => (form.task_id = value)}
            />{/if}
          {#if modal !== 'changes'}<fieldset>
              <legend>{t('factory.native.participants')}</legend
              >{#each active as member (member.id)}<Checkbox
                  checked={form.member_ids.includes(member.id)}
                  onChange={(checked) =>
                    (form.member_ids = checked
                      ? [...form.member_ids, member.id]
                      : form.member_ids.filter((id) => id !== member.id))}
                  >{memberName(member.id)}</Checkbox
                >{/each}
            </fieldset>{/if}
          <label for="native-message">{t('factory.native.message')}</label
          ><TextArea
            id="native-message"
            value={form.text}
            onInput={(value) => (form.text = value)}
          />
        {/if}
        {#if formError || native.error}<Banner variant="error" role="alert"
            >{formError || native.error}</Banner
          >{/if}
      </div>{/snippet}{#snippet footer()}<Button
        variant="secondary"
        disabled={Boolean(native.pending)}
        onClick={() => (modal = '')}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={Boolean(native.pending)}
        onClick={submit}>{modalTitle}</Button
      >{/snippet}</Modal
  >{/if}
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
{#snippet ownerField()}<label for="native-owner"
    >{t('factory.native.owner')}</label
  ><Dropdown
    id="native-owner"
    ariaLabel={t('factory.native.owner')}
    options={memberOptions}
    value={form.assignee_id}
    onValueChange={(value) => (form.assignee_id = value)}
  />{/snippet}
{#snippet modelField()}<label for="native-new-model">{t('factory.model')}</label
  ><Dropdown
    id="native-new-model"
    ariaLabel={t('factory.model')}
    value={form.model}
    options={[
      ...(form.new
        ? [{ value: '', label: t('factory.native.inheritedModel') }]
        : []),
      ...chatModels.map((item) => ({
        value: item.id,
        label: item.name || item.id,
      })),
    ]}
    onValueChange={(value) => (form.model = value)}
  />{/snippet}

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
  fieldset {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
  }
  @media (max-width: 600px) {
    .native-members {
      grid-template-columns: 1fr;
    }
  }
</style>
