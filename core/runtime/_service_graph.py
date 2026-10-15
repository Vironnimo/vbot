"""Runtime service storage, readiness and the read-only dependency surface.

This is the Runtime's service graph, not an independently started service.
Lifecycle assembly and cross-domain operations remain on Runtime.
"""

from __future__ import annotations

from datetime import datetime

from core.agents.agents import AgentStore
from core.agents.temporary import TemporaryAgentRegistry
from core.archive import ArchiveService
from core.attachments import AttachmentStore
from core.automation import (
    AutomationReferences,
    BootstrapService,
    CronService,
    GoalService,
    LearningChanges,
    LibrarianService,
    ReflectionService,
    TriggerService,
)
from core.calendar import CalendarService
from core.channels import ChannelService
from core.chat import ChatLoop, CommandDispatcher
from core.database import Database, SnapshotBarrier
from core.extensions import (
    ExtensionRegistry,
)
from core.extensions.runtime import ExtensionRuntime
from core.memory import MemoryService
from core.model_tasks import (
    EmbeddingService,
    ImageService,
    MusicService,
    SpeechService,
    TaskModelService,
    VideoService,
)
from core.model_tasks.decisions import DecisionService
from core.model_tasks.live import LiveVoiceService
from core.models.models import ModelRegistry
from core.performance import PerformanceService
from core.projects import (
    AgentResolver,
    ProjectStore,
)
from core.prompts import (
    SystemPromptManager,
)
from core.providers.providers import ProviderRegistry
from core.providers.runtime import ProviderRuntime
from core.providers.token_store import TokenStore
from core.providers.usage import ProviderUsageService
from core.recall import RecallBackend
from core.runs import ChatRunManager
from core.runtime._bootstrap import RuntimeStartupSummary
from core.runtime._config_backups import ConfigBackupSchedule
from core.runtime._extension_host import ExtensionHostFactory
from core.runtime._recall import RecallIntegration
from core.runtime._service_access import _StartedService
from core.runtime.interfaces import (
    ProviderCredentialResolverProtocol,
)
from core.runtime.keep_awake import KeepAwakeController
from core.sessions import ChatSessionManager
from core.sessions.titles import SessionTitleService
from core.skills.authoring import (
    SkillAuthoringService,
)
from core.skills.policy import SkillPolicyService
from core.skills.runtime import SkillRuntime
from core.skills.skills import SkillRegistry
from core.statistics import StatisticsIndex
from core.storage.storage import StorageManager
from core.subagents import SubAgentCoordinator
from core.tools import (
    ChangeTracker,
    FileReadState,
    UpdateHandoffs,
)
from core.tools.command_manager import CommandManager
from core.tools.live import LiveToolHosts
from core.tools.scratch_areas import ScratchAreas
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import ToolPromptBlockRegistry, ToolRegistry
from core.tools.web_fetch import PageBrowser
from core.usage import UsageRecorder
from core.utils.version import BuildIdentity


class RuntimeServiceGraph:
    """Initialize/clear owned references and expose only ready services."""

    _speech_upload_max_size_bytes: int
    _snapshot_barrier: SnapshotBarrier

    def _clear_service_references(self) -> None:
        """Initialize or release every service reference in one lifecycle-owned place."""
        self._skill_reload_generation = object()
        self._credential_reload_generation = object()
        self._provider_reload_generation = object()
        self._skill_reload_required = False
        self._extension_host_factory: ExtensionHostFactory | None = None
        self._started: bool = False
        self._started_at: datetime | None = None
        self._startup_id: str | None = None
        self._build: BuildIdentity | None = None
        self._startup_summary: RuntimeStartupSummary | None = None
        self._provider_runtime: ProviderRuntime | None = None
        self._providers: ProviderRegistry | None = None
        self._provider_credentials: ProviderCredentialResolverProtocol | None = None
        self._provider_usage: ProviderUsageService | None = None
        self._performance: PerformanceService | None = None
        self._token_store: TokenStore | None = None
        self._fallback_environment: dict[str, str] = {}
        self._models: ModelRegistry | None = None
        self._model_tasks: TaskModelService | None = None
        self._speech: SpeechService | None = None
        self._image: ImageService | None = None
        self._video: VideoService | None = None
        self._music: MusicService | None = None
        self._embeddings: EmbeddingService | None = None
        self._decisions: DecisionService | None = None
        self._live_voice: LiveVoiceService | None = None
        self._live_tool_hosts: LiveToolHosts | None = None
        self._storage: StorageManager | None = None
        self._attachment_store: AttachmentStore | None = None
        self._keep_awake: KeepAwakeController | None = None
        self._page_browser: PageBrowser | None = None
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._memory_service: MemoryService | None = None
        self._file_state: FileReadState | None = None
        self._update_handoffs: UpdateHandoffs | None = None
        self._terminal_manager: TerminalManager | None = None
        self._command_manager: CommandManager | None = None
        self._scratch_areas: ScratchAreas | None = None
        self._skills: SkillRegistry | None = None
        self._skill_authoring: SkillAuthoringService | None = None
        self._skill_policy: SkillPolicyService | None = None
        self._skill_runtime: SkillRuntime | None = None
        self._extensions: ExtensionRegistry | None = None
        self._extension_runtime: ExtensionRuntime | None = None
        self._chat_sessions: ChatSessionManager | None = None
        self._statistics_index: StatisticsIndex | None = None
        self._usage_recorder: UsageRecorder | None = None
        self._projects: ProjectStore | None = None
        self._agent_resolver: AgentResolver | None = None
        self._temporary_agents: TemporaryAgentRegistry | None = None
        self._recall: RecallIntegration | None = None
        self._channel_service: ChannelService | None = None
        self._cron_service: CronService | None = None
        self._calendar_service: CalendarService | None = None
        self._bootstrap_service: BootstrapService | None = None
        self._automation_references: AutomationReferences | None = None
        self._archive: ArchiveService | None = None
        self._config_backups: ConfigBackupSchedule | None = None
        self._trigger_service: TriggerService | None = None
        self._goal_service: GoalService | None = None
        self._reflection_service: ReflectionService | None = None
        self._learning_changes: LearningChanges | None = None
        self._librarian_service: LibrarianService | None = None
        self._session_title_service: SessionTitleService | None = None
        self._subagent_coordinator: SubAgentCoordinator | None = None
        self._chat_loop: ChatLoop | None = None
        self._command_dispatcher: CommandDispatcher | None = None
        self._chat_run_manager: ChatRunManager | None = None
        self.chat_runs: ChatRunManager | None = None
        self._system_prompts: SystemPromptManager | None = None
        self._change_tracker: ChangeTracker | None = None
        self._tool_prompt_blocks: ToolPromptBlockRegistry | None = None

    providers: _StartedService[ProviderRegistry] = _StartedService(
        lambda runtime: runtime._providers, "Provider registry not available"
    )

    models: _StartedService[ModelRegistry] = _StartedService(
        lambda runtime: runtime._models, "Model registry not available"
    )

    provider_credentials: _StartedService[ProviderCredentialResolverProtocol] = _StartedService(
        lambda runtime: runtime._provider_credentials, "Provider credential service not available"
    )

    model_tasks: _StartedService[TaskModelService] = _StartedService(
        lambda runtime: runtime._model_tasks, "Task-model service not available"
    )

    speech: _StartedService[SpeechService] = _StartedService(
        lambda runtime: runtime._speech, "Speech service not available"
    )

    image: _StartedService[ImageService] = _StartedService(
        lambda runtime: runtime._image, "Image service not available"
    )

    video: _StartedService[VideoService] = _StartedService(
        lambda runtime: runtime._video, "Video service not available"
    )

    music: _StartedService[MusicService] = _StartedService(
        lambda runtime: runtime._music, "Music service not available"
    )

    live_voice: _StartedService[LiveVoiceService] = _StartedService(
        lambda runtime: runtime._live_voice, "Live voice service not available"
    )

    embeddings: _StartedService[EmbeddingService] = _StartedService(
        lambda runtime: runtime._embeddings, "Embedding service not available"
    )

    token_store: _StartedService[TokenStore] = _StartedService(
        lambda runtime: runtime._token_store, "Token store not available"
    )

    storage: _StartedService[StorageManager] = _StartedService(
        lambda runtime: runtime._storage, "Storage service not available"
    )

    build: _StartedService[BuildIdentity] = _StartedService(
        lambda runtime: runtime._build, "Build identity not available"
    )

    attachment_store: _StartedService[AttachmentStore] = _StartedService(
        lambda runtime: runtime._attachment_store, "Attachment store not available"
    )

    @property
    def startup_summary(self) -> RuntimeStartupSummary:
        """What this Runtime served when its startup finished."""

        self._ensure_started()
        assert self._startup_summary is not None
        return self._startup_summary

    @property
    def speech_upload_max_size_bytes(self) -> int:
        """Maximum accepted uploaded audio size for speech transcription."""

        self._ensure_started()
        return self._speech_upload_max_size_bytes

    agents: _StartedService[AgentStore] = _StartedService(
        lambda runtime: runtime._agents, "Agent service not available"
    )

    memory: _StartedService[MemoryService] = _StartedService(
        lambda runtime: runtime._memory_service, "Memory service not available"
    )

    tools: _StartedService[ToolRegistry] = _StartedService(
        lambda runtime: runtime._tools, "Tool service not available"
    )

    update_handoffs: _StartedService[UpdateHandoffs] = _StartedService(
        lambda runtime: runtime._update_handoffs, "Update handoff service not available"
    )

    terminal_manager: _StartedService[TerminalManager] = _StartedService(
        lambda runtime: runtime._terminal_manager, "Terminal manager service not available"
    )

    command_manager: _StartedService[CommandManager] = _StartedService(
        lambda runtime: runtime._command_manager, "Command manager service not available"
    )

    file_read_state: _StartedService[FileReadState] = _StartedService(
        lambda runtime: runtime._file_state, "File read state service not available"
    )

    change_tracker: _StartedService[ChangeTracker] = _StartedService(
        lambda runtime: runtime._change_tracker, "Change tracker service not available"
    )

    scratch_areas: _StartedService[ScratchAreas] = _StartedService(
        lambda runtime: runtime._scratch_areas, "Scratch Areas are not available"
    )

    skills: _StartedService[SkillRegistry] = _StartedService(
        lambda runtime: runtime._skills, "Skill service not available"
    )

    skill_authoring: _StartedService[SkillAuthoringService] = _StartedService(
        lambda runtime: runtime._skill_authoring, "Skill authoring service not available"
    )

    skill_policy: _StartedService[SkillPolicyService] = _StartedService(
        lambda runtime: runtime._skill_policy, "Skill policy service not available"
    )

    @property
    def extensions(self) -> ExtensionRegistry | None:
        return self._extensions

    @property
    def snapshot_barrier(self) -> SnapshotBarrier:
        """Keeps data snapshots apart from this Runtime's compound mutations.

        Every change of a canonical database together with a durable JSON
        document enters it shared as one unit; a data snapshot of this data
        directory passes it to ``create_data_snapshot``.
        """
        return self._snapshot_barrier

    def canonical_databases(self) -> tuple[Database, ...]:
        """Every canonical database this Runtime has open.

        Data snapshots copy these through their open handles and data-store
        status reads their owner health; registered databases that are not
        open here are read from their files.
        """
        databases: list[Database] = []
        if self._chat_sessions is not None:
            databases.append(self._chat_sessions.database)
        if self._usage_recorder is not None:
            databases.append(self._usage_recorder.database)
        if self._provider_usage is not None:
            provider_usage = self._provider_usage.history_database
            if provider_usage is not None:
                databases.append(provider_usage)
        if self._channel_service is not None:
            databases.append(self._channel_service.database)
        if self._extension_host_factory is not None:
            databases.extend(self._extension_host_factory.databases.open_databases())
        return tuple(databases)

    chat_sessions: _StartedService[ChatSessionManager] = _StartedService(
        lambda runtime: runtime._chat_sessions, "Chat session service not available"
    )

    statistics_index: _StartedService[StatisticsIndex] = _StartedService(
        lambda runtime: runtime._statistics_index, "Statistics index is not available"
    )

    usage_recorder: _StartedService[UsageRecorder] = _StartedService(
        lambda runtime: runtime._usage_recorder, "Usage accounting is not available"
    )

    subagents: _StartedService[SubAgentCoordinator] = _StartedService(
        lambda runtime: runtime._subagent_coordinator, "Sub-agent coordinator is not available"
    )

    projects: _StartedService[ProjectStore] = _StartedService(
        lambda runtime: runtime._projects, "Project service not available"
    )

    agent_resolver: _StartedService[AgentResolver] = _StartedService(
        lambda runtime: runtime._agent_resolver, "Agent resolver service not available"
    )

    recall_backend: _StartedService[RecallBackend] = _StartedService(
        lambda runtime: runtime._recall.backend if runtime._recall is not None else None,
        "Recall backend is not available",
    )

    recall: _StartedService[RecallIntegration] = _StartedService(
        lambda runtime: runtime._recall, "Recall is not available"
    )

    chat_run_manager: _StartedService[ChatRunManager] = _StartedService(
        lambda runtime: runtime._chat_run_manager, "Chat run manager service not available"
    )

    command_dispatcher: _StartedService[CommandDispatcher] = _StartedService(
        lambda runtime: runtime._command_dispatcher, "Command dispatcher service not available"
    )

    chat_loop: _StartedService[ChatLoop] = _StartedService(
        lambda runtime: runtime._chat_loop, "Chat loop service not available"
    )

    trigger_service: _StartedService[TriggerService] = _StartedService(
        lambda runtime: runtime._trigger_service, "Trigger service not available"
    )

    goals: _StartedService[GoalService] = _StartedService(
        lambda runtime: runtime._goal_service, "Goal service not available"
    )

    reflection: _StartedService[ReflectionService] = _StartedService(
        lambda runtime: runtime._reflection_service, "Reflection service not available"
    )

    learning_changes: _StartedService[LearningChanges] = _StartedService(
        lambda runtime: runtime._learning_changes, "Learning changes service not available"
    )

    librarian: _StartedService[LibrarianService] = _StartedService(
        lambda runtime: runtime._librarian_service, "Librarian service not available"
    )

    channel_service: _StartedService[ChannelService] = _StartedService(
        lambda runtime: runtime._channel_service, "Channel service not available"
    )

    cron_service: _StartedService[CronService] = _StartedService(
        lambda runtime: runtime._cron_service, "Cron service not available"
    )

    calendar_service: _StartedService[CalendarService] = _StartedService(
        lambda runtime: runtime._calendar_service, "Calendar service not available"
    )

    bootstrap_service: _StartedService[BootstrapService] = _StartedService(
        lambda runtime: runtime._bootstrap_service, "Bootstrap service not available"
    )

    automation_references: _StartedService[AutomationReferences] = _StartedService(
        lambda runtime: runtime._automation_references, "Automation references not available"
    )

    archive: _StartedService[ArchiveService] = _StartedService(
        lambda runtime: runtime._archive, "Archive is not available"
    )

    provider_usage: _StartedService[ProviderUsageService] = _StartedService(
        lambda runtime: runtime._provider_usage, "Provider usage service not available"
    )

    performance: _StartedService[PerformanceService] = _StartedService(
        lambda runtime: runtime._performance, "Performance service not available"
    )

    system_prompts: _StartedService[SystemPromptManager] = _StartedService(
        lambda runtime: runtime._system_prompts, "System prompt service not available"
    )

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")
