import {
  agentActivityStatus,
  agentUnreadResults,
  visibleTimelineItemsForRender,
} from '$lib/chatState.js';
import { timelineToolRuns } from '$lib/chatTimeline.js';
import { SESSION_WAITING, agentSessionActivity } from '$lib/sessionActivity.js';
import {
  backgroundTasks,
  runningSubAgentCommands,
} from '$lib/chatTimelinePresentation.js';
import { formatAgentAddress, qualifyAgentAddress } from '$lib/agentAddress.js';
import { agentActivityState } from '../agentActivityTooltip.js';
import { untrack } from 'svelte';
import { approvalSessions } from '$lib/permissionApprovals.js';
import { t } from '$lib/i18n.js';

// One displayed-work projection feeds every Chat surface and unread indicator.
export function createChatViewActivity(context) {
  const { chatState, target } = context;
  // The displayed Session's rendered rows, projected once per update and
  // shared by the timeline, the Activity panel and Sub-Agent reconciliation.
  let activeTimelineItems = $derived(
    visibleTimelineItemsForRender(target.activeSessionState),
  );
  let previousToolRuns = [];
  const toolRuns = $derived.by(() => {
    previousToolRuns = timelineToolRuns(activeTimelineItems, previousToolRuns);
    return previousToolRuns;
  });
  // The live state of the displayed Session's own background commands; a
  // fork's History names its source's commands, which never run for it.
  let displayedCommandStatuses = $derived(
    chatState.commandStatuses[target.activeSessionState?.sessionId] ?? {},
  );
  // The background commands the displayed Session's Sub-Agents (at every
  // depth) still run, by the id of its own Sub-Agent above them; each follows
  // the live state of the Session that runs it.
  let displayedSubAgentCommands = $derived(
    runningSubAgentCommands(
      target.activeSessionState?.subAgentBackgroundCommands,
      chatState.commandStatuses,
    ),
  );
  // The server covers all work, including directly opened Terminals and
  // descendants absent from the loaded History. Local rows bridge the first
  // read only; an accepted empty projection retires stale local work hints.
  // A Session waiting for work that will wake it (App's Session activity)
  // offers Stop all too.
  let backgroundWorkRunning = $derived.by(() => {
    const key = target.activeSessionState?.key;
    if (key && chatState.sessionActivity?.[key]?.state === SESSION_WAITING) {
      return true;
    }
    const work = target.activeSessionState?.workProjection;
    if (work) return work.background_work_running || work.run_active;
    return (
      Object.keys(displayedSubAgentCommands).length > 0 ||
      backgroundTasks(
        toolRuns,
        chatState.subAgentStatuses,
        target.activeSessionState?.backgroundCommandStatuses,
        displayedCommandStatuses,
      ).some((task) => task.dotStatus === 'running')
    );
  });
  // Every Agent the picker offers: Identity Agents, then each Project Team.
  let pickerAgentAddresses = $derived([
    ...chatState.agents.map((agent) => agent.id),
    ...target.projectGroups.flatMap((group) =>
      group.members.map((member) =>
        formatAgentAddress(member.agent_id, group.projectId),
      ),
    ),
  ]);

  // The Agent addresses with a Tool call waiting for the user's approval.
  let approvalAddresses = $derived(
    (context.approvalRequests ?? []).flatMap((request) =>
      approvalSessions(request).map((session) =>
        qualifyAgentAddress(session.agent_id, session.project_id),
      ),
    ),
  );
  const approvalCount = $derived((context.approvalRequests ?? []).length);

  // Per Agent address: its status dot, unread results and what its working
  // or waiting Sessions do together (`work`, null while idle). A Tool call
  // waiting for the user's approval outranks every other state.
  let pickerAgentActivity = $derived.by(() => {
    const displayedSessionKey = target.displayedSessionKey();
    return Object.fromEntries(
      pickerAgentAddresses.map((address) => {
        const unreadResults = agentUnreadResults(
          chatState,
          address,
          displayedSessionKey,
        );
        return [
          address,
          {
            status: approvalAddresses.includes(address)
              ? 'approval'
              : agentActivityStatus(chatState, address, displayedSessionKey),
            unreadCount: unreadResults.count,
            latestUnreadAt: unreadResults.latestAt,
            work: agentSessionActivity(chatState.sessionActivity, address),
          },
        ];
      }),
    );
  });

  // The held Sessions' unread state over the Session list's rows; whether a
  // row works or waits comes from App's Session activity.
  let sessionDrawerActivity = $derived.by(() =>
    Object.values(chatState.sessions).map((sessionState) => ({
      agent_address: sessionState.agentId,
      session_id: sessionState.sessionId,
      local_unread_count: sessionState.localUnreadCount,
      notification_generation_id: sessionState.notificationGeneration,
      notification_revision: sessionState.notificationRevision,
      latest_completion_run_id: sessionState.latestCompletionRunId || null,
      unread_run_status: sessionState.unreadRunStatus || null,
      unread_run_at: sessionState.unreadRunAt || null,
    })),
  );

  // Unread results across every Agent on the Agent bar and in its All agents
  // list. While this Chat is shown, the displayed Session's result is being
  // read and does not count, like on the Agent bar.
  const countUnreadResults = (displayedSessionKey) =>
    pickerAgentAddresses.reduce(
      (count, address) =>
        count +
        agentUnreadResults(chatState, address, displayedSessionKey).count,
      0,
    );
  let unreadResultCount = $derived(
    countUnreadResults(context.active ? target.displayedSessionKey() : ''),
  );
  // The same count with the displayed Session's result included: what is
  // unread while nobody looks at the page (the Desktop's taskbar badge).
  let unattendedResultCount = $derived(countUnreadResults(''));

  $effect(() => {
    const count = unattendedResultCount + approvalCount;
    untrack(() => context.onUnattendedResults(count));
  });

  $effect(() => {
    const count = unreadResultCount + approvalCount;
    const approvalLabel =
      approvalCount === 1
        ? t('approvals.pendingOne')
        : t('approvals.pendingMany', { count: approvalCount });
    const indicator =
      count > 0
        ? {
            count,
            tone: approvalCount ? 'running' : 'unread',
            label: approvalCount
              ? [
                  approvalLabel,
                  unreadResultCount
                    ? agentActivityState('unread', unreadResultCount)
                    : '',
                ]
                  .filter(Boolean)
                  .join(' · ')
              : agentActivityState('unread', count),
          }
        : null;
    untrack(() => context.onNavigationIndicator(indicator));
  });
  return {
    get timelineItems() {
      return activeTimelineItems;
    },
    get toolRuns() {
      return toolRuns;
    },
    get commandStatuses() {
      return displayedCommandStatuses;
    },
    get subAgentCommands() {
      return displayedSubAgentCommands;
    },
    get backgroundWorkRunning() {
      return backgroundWorkRunning;
    },
    get pickerAgentAddresses() {
      return pickerAgentAddresses;
    },
    get pickerAgentActivity() {
      return pickerAgentActivity;
    },
    get sessionDrawerActivity() {
      return sessionDrawerActivity;
    },
  };
}
