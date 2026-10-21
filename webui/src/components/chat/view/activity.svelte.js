import {
  agentActivityStatus,
  agentUnreadResults,
  visibleTimelineItemsForRender,
} from '$lib/chatState.js';
import { SESSION_WAITING, agentSessionActivity } from '$lib/sessionActivity.js';
import {
  backgroundTasks,
  runningSubAgentCommands,
} from '$lib/chatTimelinePresentation.js';
import { formatAgentAddress } from '$lib/agentAddress.js';
import { agentActivityState } from '../agentActivityTooltip.js';
import { untrack } from 'svelte';

// One displayed-work projection feeds every Chat surface and unread indicator.
export function createChatViewActivity(context) {
  const { chatState, target } = context;
  // The displayed Session's rendered rows, projected once per update and
  // shared by the timeline, the Activity panel and Sub-Agent reconciliation.
  let activeTimelineItems = $derived(
    visibleTimelineItemsForRender(target.activeSessionState),
  );
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
        activeTimelineItems,
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

  // Per Agent address: its status dot, unread results and what its working
  // or waiting Sessions do together (`work`, null while idle).
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
            status: agentActivityStatus(
              chatState,
              address,
              displayedSessionKey,
            ),
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
      has_unread_completion: sessionState.hasUnreadCompletion === true,
      latest_completion_run_id: sessionState.latestCompletionRunId || null,
      unread_run_id: sessionState.unreadRunId || null,
      unread_run_status: sessionState.unreadRunStatus || null,
      unread_run_at: sessionState.unreadRunAt || null,
    })),
  );

  // Unread results across every Agent on the Agent bar and in its All agents
  // list. While this Chat is shown, the displayed Session's result is being
  // read and does not count, like on the Agent bar.
  let unreadResultCount = $derived.by(() => {
    const displayedSessionKey = context.active
      ? target.displayedSessionKey()
      : '';
    return pickerAgentAddresses.reduce(
      (count, address) =>
        count +
        agentUnreadResults(chatState, address, displayedSessionKey).count,
      0,
    );
  });

  $effect(() => {
    const count = unreadResultCount;
    const indicator =
      count > 0
        ? {
            count,
            tone: 'unread',
            label: agentActivityState('unread', count),
          }
        : null;
    untrack(() => context.onNavigationIndicator(indicator));
  });
  return {
    get timelineItems() {
      return activeTimelineItems;
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
