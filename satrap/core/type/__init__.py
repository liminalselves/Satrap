"""Satrap 全项目公共数据契约的稳定导出入口

具体实现模块应依赖明确的领域叶模块, 避免通过本聚合入口形成导入环。
"""
from satrap.core.utils.attributes import (
    safe_getattr,
    safe_getattr_bool,
    safe_getattr_callable,
    safe_getattr_dict,
    safe_getattr_float,
    safe_getattr_int,
    safe_getattr_list,
    safe_getattr_str,
)
from satrap.core.type.archive import ArchiveMessage, MessageArchiveError, MessageScope
from satrap.core.type.control import (
    AdministratorRuntimeStatus,
    DiagnosticStatusBody,
    GroupAccountRecord,
    GroupAccountSummary,
    GroupAccountsResult,
    GroupActionPage,
    GroupActionRecord,
    GroupActionTypesResult,
    GroupBindingOptionsResult,
    GroupConfigRecord,
    GroupDirectoryRecord,
    GroupMembersPage,
    GroupSyncState,
    GroupSyncTicket,
    ManualWakeStatusResult,
    PlatformStoreRecovery,
    ReloadConfigResult,
    RequestDiagnosticDetail,
    RequestDiagnosticsPage,
    RequestDiagnosticsStats,
    ScopedSessionSummary,
    WakeSubmitResult,
)
from satrap.core.type.conversation import CallOrigin, ConversationRoute, HistoryWindow
from satrap.core.type.json_value import JsonValue
from satrap.core.type.model import (
    THINKING_CLOSE_VALUES,
    THINKING_LEVEL_VALUES,
    ContextUsageSnapshot,
    LLMCallRequest,
    LLMCallResponse,
    LLMCallStreamEvent,
    LLMErrorInfo,
    ModelContextRequestStats,
    ModelContextTurnStats,
    ModelProtocolPayload,
    RequestContextBlock,
    TokenUsage,
    UserCall,
    validate_runtime_thinking,
    validate_thinking_levels,
)
from satrap.core.type.model_config import (
    MODEL_CONFIG_CLASSES,
    ASRConfig,
    ASRResponse,
    EmbeddingConfig,
    ImgConfig,
    LLMConfig,
    ReRankConfig,
)
from satrap.core.type.platform import (
    BaseMessageComponent,
    CommandAction,
    ComponentType,
    Group,
    MessageMember,
    PlatformComponentType,
    PlatformError,
    PlatformMessage,
    PlatformMessageType,
    PlatformStatus,
)
from satrap.core.type.session import SessionConfig, UserInfo
from satrap.core.type.state import (
    CURRENT_SNAPSHOT_VERSION,
    SUPPORTED_SNAPSHOT_VERSIONS,
    JsonRow,
    MutationContext,
    RestoreOptions,
    SnapshotDomain,
    StateCheckpoint,
    StateScope,
    StateSnapshot,
)

from satrap.core.type.approval import (
    ApprovalMode,
    APPROVAL_MODES,
    Decision,
    ApprovalRequest,
    ApprovalResult,
)

from satrap.core.type.coding import (
    ReplacementPair,
)

from satrap.core.type.debug import (
    DebugMode,
    DebugData,
    ObjectPayload,
    EventSummary,
    EventPage,
    DebugObject,
    DebugEvent,
)

from satrap.core.type.flow.definition import (
    FlowDefinitionError,
    FlowBudget,
    FlowNodeInstance,
    FlowEdge,
    FlowDefaultsEntry,
    FlowDefinition,
    ConfigSource,
    ResolvedNodeConfig,
    FlowContractError,
)

from satrap.core.type.flow.events import (
    ConditionResult,
    FlowEvent,
    EventContext as FlowEventContext,
)

from satrap.core.type.flow.ports import (
    PortType,
    DATA_PORT_TYPES,
    PLUGIN_PORT_TYPES,
    SIGNAL_PORT_TYPES,
    PORT_TYPE_LABELS,
    EMPTY_PORT_VALUES,
    FlowMessage,
    ContextBlock,
    StateView,
    EventContextKind,
    FlowNode,
)

from satrap.core.type.flow.registry import (
    NodeRole,
    PortSpec,
    RenamedEntry,
    FlowNodeType,
)

from satrap.core.type.flow.validation import (
    IssueLevel,
    IssueCategory,
    Issue,
    Origin as FlowOrigin,
    WfInfo as FlowWfInfo,
    Analysis,
)

from satrap.core.type.flow.workflow import (
    FlowWorkflowDecl,
)

from satrap.core.type.group_actions import (
    ACTION_FIELDS,
)

from satrap.core.type.group_chat import (
    CAPABILITIES as GROUP_CHAT_CAPABILITIES,
    GroupChatError,
    MemberRecord,
    GroupRecord,
    VerifiedGroup,
    GroupSnapshot,
    MemberSnapshot,
    VerifiedMessage,
    VerifiedMember,
    GroupChatLimits,
)

from satrap.core.type.image import (
    ImageInput,
    CharacterPrompt,
    PreciseReference,
    VibeEncoding,
    VibeReference,
    ImageGenParams,
    NovelAIParams,
    GeneratedImage,
    ImageGenResponse,
    ImageGenError,
)

from satrap.core.type.model_protocol import (
    ModelProtocolError,
    StreamDecoder,
    ModelProtocol,
)

from satrap.core.type.plugin.authorization import (
    AuthorizationDecision,
    BoundPluginTool,
    PermissionGrant,
    PluginEntryBinding,
)

from satrap.core.type.plugin.catalog import (
    PluginCatalogEntry,
)

from satrap.core.type.plugin.compatibility import (
    PluginEnvironment,
    CompatibilityResult,
    PluginCompatibilityError,
)

from satrap.core.type.plugin.config import (
    PluginHostState,
    ConfigField,
    schema_to_payload,
)

from satrap.core.type.plugin.permissions import (
    ManagementPermission,
    CommandPermissions,
    PluginPermissions,
    EMPTY_PERMISSIONS,
)

from satrap.core.type.plugin.spec import (
    PluginConfigResolver,
    PluginSpec,
)

from satrap.core.type.plugin_interop import (
    CapabilityKind,
    CAPABILITY_KINDS,
    CapabilityNames,
    PluginInteractionError,
    ImageArtifact,
    ServiceValue,
    Dependency,
    InteropSpec,
    PluginInfo,
    HandlerInfo,
    SkillInfo,
)

from satrap.core.type.sandbox import (
    Operation as SandboxOperation,
    SandboxResult,
    Request as SandboxRequest,
    PolicyViolation as SandboxPolicyViolation,
    Violations as SandboxViolations,
)

from satrap.core.type.search import (
    SearchResult,
)

from satrap.core.type.session_provider import (
    SESSION_CLASS_PROVIDER,
    BindingState,
    BindingStatus,
    SessionProviderDefinition,
    SessionProvider,
)

from satrap.core.type.telemetry import (
    Service as TelemetryService,
    SourceState,
    Purpose as TelemetryPurpose,
    ServiceHealth,
    ServiceProbe,
    MessageCounts,
    UsageTotals,
    TelemetrySettings,
    MetricScope,
    ModelCall as TelemetryModelCall,
    MessageMetric,
    ActivityEvent,
    MetricWindow,
)


__all__ = [
    "ASRConfig",
    "ASRResponse",
    "AdministratorRuntimeStatus",
    "ArchiveMessage",
    "BaseMessageComponent",
    "CURRENT_SNAPSHOT_VERSION",
    "CallOrigin",
    "CommandAction",
    "ComponentType",
    "ContextUsageSnapshot",
    "ConversationRoute",
    "DiagnosticStatusBody",
    "EmbeddingConfig",
    "Group",
    "GroupAccountRecord",
    "GroupAccountSummary",
    "GroupAccountsResult",
    "GroupActionPage",
    "GroupActionRecord",
    "GroupActionTypesResult",
    "GroupBindingOptionsResult",
    "GroupConfigRecord",
    "GroupDirectoryRecord",
    "GroupMembersPage",
    "GroupSyncState",
    "GroupSyncTicket",
    "HistoryWindow",
    "ImgConfig",
    "JsonRow",
    "JsonValue",
    "LLMCallRequest",
    "LLMCallResponse",
    "LLMCallStreamEvent",
    "LLMConfig",
    "LLMErrorInfo",
    "MODEL_CONFIG_CLASSES",
    "ManualWakeStatusResult",
    "MessageArchiveError",
    "MessageMember",
    "MessageScope",
    "ModelContextRequestStats",
    "ModelContextTurnStats",
    "ModelProtocolPayload",
    "MutationContext",
    "PlatformComponentType",
    "PlatformError",
    "PlatformMessage",
    "PlatformMessageType",
    "PlatformStatus",
    "PlatformStoreRecovery",
    "ReRankConfig",
    "ReloadConfigResult",
    "RequestContextBlock",
    "RequestDiagnosticDetail",
    "RequestDiagnosticsPage",
    "RequestDiagnosticsStats",
    "RestoreOptions",
    "SUPPORTED_SNAPSHOT_VERSIONS",
    "ScopedSessionSummary",
    "SessionConfig",
    "SnapshotDomain",
    "StateCheckpoint",
    "StateScope",
    "StateSnapshot",
    "THINKING_CLOSE_VALUES",
    "THINKING_LEVEL_VALUES",
    "TokenUsage",
    "UserCall",
    "UserInfo",
    "WakeSubmitResult",
    "safe_getattr",
    "safe_getattr_bool",
    "safe_getattr_callable",
    "safe_getattr_dict",
    "safe_getattr_float",
    "safe_getattr_int",
    "safe_getattr_list",
    "safe_getattr_str",
    "validate_runtime_thinking",
    "validate_thinking_levels",
]

__all__ += [
    "ApprovalMode",
    "APPROVAL_MODES",
    "Decision",
    "ApprovalRequest",
    "ApprovalResult",
    "ReplacementPair",
    "DebugMode",
    "DebugData",
    "ObjectPayload",
    "EventSummary",
    "EventPage",
    "DebugObject",
    "DebugEvent",
    "FlowDefinitionError",
    "FlowBudget",
    "FlowNodeInstance",
    "FlowEdge",
    "FlowDefaultsEntry",
    "FlowDefinition",
    "ConfigSource",
    "ResolvedNodeConfig",
    "FlowContractError",
    "ConditionResult",
    "FlowEvent",
    "FlowEventContext",
    "PortType",
    "DATA_PORT_TYPES",
    "PLUGIN_PORT_TYPES",
    "SIGNAL_PORT_TYPES",
    "PORT_TYPE_LABELS",
    "EMPTY_PORT_VALUES",
    "FlowMessage",
    "ContextBlock",
    "StateView",
    "EventContextKind",
    "FlowNode",
    "NodeRole",
    "PortSpec",
    "RenamedEntry",
    "FlowNodeType",
    "IssueLevel",
    "IssueCategory",
    "Issue",
    "FlowOrigin",
    "FlowWfInfo",
    "Analysis",
    "FlowWorkflowDecl",
    "ACTION_FIELDS",
    "GROUP_CHAT_CAPABILITIES",
    "GroupChatError",
    "MemberRecord",
    "GroupRecord",
    "VerifiedGroup",
    "GroupSnapshot",
    "MemberSnapshot",
    "VerifiedMessage",
    "VerifiedMember",
    "GroupChatLimits",
    "ImageInput",
    "CharacterPrompt",
    "PreciseReference",
    "VibeEncoding",
    "VibeReference",
    "ImageGenParams",
    "NovelAIParams",
    "GeneratedImage",
    "ImageGenResponse",
    "ImageGenError",
    "ModelProtocolError",
    "StreamDecoder",
    "ModelProtocol",
    "AuthorizationDecision",
    "BoundPluginTool",
    "PermissionGrant",
    "PluginEntryBinding",
    "PluginCatalogEntry",
    "PluginEnvironment",
    "CompatibilityResult",
    "PluginCompatibilityError",
    "PluginHostState",
    "ConfigField",
    "schema_to_payload",
    "ManagementPermission",
    "CommandPermissions",
    "PluginPermissions",
    "EMPTY_PERMISSIONS",
    "PluginConfigResolver",
    "PluginSpec",
    "CapabilityKind",
    "CAPABILITY_KINDS",
    "CapabilityNames",
    "PluginInteractionError",
    "ImageArtifact",
    "ServiceValue",
    "Dependency",
    "InteropSpec",
    "PluginInfo",
    "HandlerInfo",
    "SkillInfo",
    "SandboxOperation",
    "SandboxResult",
    "SandboxRequest",
    "SandboxPolicyViolation",
    "SandboxViolations",
    "SearchResult",
    "SESSION_CLASS_PROVIDER",
    "BindingState",
    "BindingStatus",
    "SessionProviderDefinition",
    "SessionProvider",
    "TelemetryService",
    "SourceState",
    "TelemetryPurpose",
    "ServiceHealth",
    "ServiceProbe",
    "MessageCounts",
    "UsageTotals",
    "TelemetrySettings",
    "MetricScope",
    "TelemetryModelCall",
    "MessageMetric",
    "ActivityEvent",
    "MetricWindow",
]
