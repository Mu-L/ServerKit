# Registers the mapper hook that auto-cascades deletes onto NOT NULL child
# FKs (the alternative is an IntegrityError on every parent hard-delete).
from app.models import _delete_cascade_policy  # noqa: F401
from app.models.user import User
from app.models.revoked_session import RevokedSession
from app.models.application import Application
from app.models.domain import Domain
from app.models.env_variable import EnvironmentVariable, EnvironmentVariableHistory
from app.models.notification_preferences import NotificationPreferences
from app.models.deployment import Deployment, DeploymentDiff
from app.models.deployment_snapshot import DeploymentSnapshot
from app.models.project import Project
from app.models.environment import Environment
from app.models.shared_resource import ResourceTag, SharedVariableGroup, SharedVariable, SharedVariableGroupAttachment
from app.models.application_preview import ApplicationPreview, ApplicationPreviewSettings
from app.models.proxy_stack import ProxyStack
from app.models.site_base_domain import SiteBaseDomain
from app.models.deployment_job import DeploymentJob, DeploymentJobLog
from app.models.linked_panel import LinkedPanelConfig
from app.models.system_settings import SystemSettings
from app.models.audit_log import AuditLog
from app.models.error_log import ErrorLog
from app.models.host_snapshot import HostSnapshot
from app.models.metrics_history import MetricsHistory
from app.models.workflow import Workflow, WorkflowExecution, WorkflowLog
from app.models.webhook import GitWebhook, WebhookLog, GitDeployment
from app.models.server import Server, ServerGroup, ServerMetrics, ServerCommand, AgentSession, AgentVersion, AgentRollout
from app.models.server_onboarding_log import ServerOnboardingLog
from app.models.run_log import RunLogEntry
from app.models.security_alert import SecurityAlert
from app.models.wordpress_site import WordPressSite, DatabaseSnapshot, SyncJob, WordPressVulnerability
from app.models.wordpress_custom_plugin import WordPressCustomPlugin, WordPressSitePlugin
from app.models.environment_activity import EnvironmentActivity
from app.models.promotion_job import PromotionJob
from app.models.sanitization_profile import SanitizationProfile
from app.models.email import EmailDomain, EmailAccount, EmailAlias, EmailForwardingRule, DNSProviderConfig, EmailRelayConfig
from app.models.oauth_identity import OAuthIdentity
from app.models.source_connection import SourceConnection
from app.models.registrar_connection import RegistrarConnection
from app.models.container_registry import ContainerRegistry
from app.models.app_volume import AppVolume
from app.models.application_manifest import ApplicationManifest
from app.models.managed_database import ManagedDatabase
from app.models.api_key import ApiKey
from app.models.api_usage import ApiUsageLog, ApiUsageSummary
from app.models.event_subscription import EventSubscription, EventDelivery
from app.models.invitation import Invitation
from app.models.metric_alert import ServerAlertThreshold, MetricAlert
from app.models.agent_plugin import AgentPlugin, AgentPluginInstall
from app.models.server_template import ServerTemplate, ServerTemplateAssignment
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceApiKey, ResourceGrant
from app.models.dns_zone import DNSZone, DNSRecord
from app.models.managed_dns_record import ManagedDnsRecord
from app.models.dns_change import DnsChange
from app.models.cf_ops_change import CfOpsChange
from app.models.sandbox_run import SandboxRun
from app.models.tunnel import Tunnel
from app.models.exposed_service import ExposedService
from app.models.status_page import StatusPage, StatusComponent, HealthCheck, StatusIncident, StatusIncidentUpdate
from app.models.cloud_server import CloudProvider, CloudServer, CloudSnapshot
from app.models.pending_agent import PendingAgent
from app.models.plugin import InstalledPlugin
from app.models.ai import AiConversation, AiMessage, AiPendingAction, AiRun
from app.models.image_scan import ImageVulnerabilityScan, SbomArtifact
from app.models.passkey import PasskeyCredential
from app.models.secret_vault import SecretVault, Secret
from app.models.webhook_gateway import WebhookEndpoint, WebhookDelivery
from app.models.waf_policy import WafPolicy
from app.models.cloudflare_worker import CloudflareWorker
from app.models.cloudflare_tunnel import CloudflareTunnel
from app.models.backup_policy import BackupPolicy
from app.models.backup_run import BackupRun
from app.models.restore_drill import RestoreDrill
from app.queue_bus.models import QueueGroup, Queue, QueueMessage
from app.notifications.models import Notification, NotificationDelivery
from app.models.email_bounce import EmailBounceState
from app.models.email_provider import EmailProviderConnection
from app.models.chat_webhook import ChatWebhookConnection
from app.models.system_event import SystemEvent
from app.models.domain_registration import DomainRegistration
from app.models.login_link import LoginLink
from app.models.managed_database_user import ManagedDatabaseUser
from app.models.site_import import SiteImport
from app.models.site_bandwidth import SiteBandwidthDaily
from app.models.app_request_metric import AppRequestMetric
from app.models.app_attachment import AppAttachment
from app.models.app_slot import AppSlot
from app.models.cron_run import CronRun
from app.models.fleet_doctor_result import FleetDoctorResult
from app.models.server_survey import ServerSurvey
from app.models.dns_cutover_snapshot import DnsCutoverSnapshot
from app.models.theme import Theme
from app.models.plugin_store import PluginStore
from app.models.saved_view import SavedView
from app.models.restore_point import RestorePoint

__all__ = [
    'User', 'Application', 'Domain', 'EnvironmentVariable', 'EnvironmentVariableHistory',
    'NotificationPreferences', 'Deployment', 'DeploymentDiff', 'DeploymentSnapshot', 'DeploymentJob', 'DeploymentJobLog', 'SystemSettings', 'AuditLog',
    'MetricsHistory', 'Workflow', 'WorkflowExecution', 'WorkflowLog', 'GitWebhook', 'WebhookLog', 'GitDeployment',
    'ErrorLog', 'HostSnapshot',
    'Server', 'ServerGroup', 'ServerMetrics', 'ServerCommand', 'AgentSession', 'AgentVersion', 'AgentRollout', 'ServerOnboardingLog', 'SecurityAlert',
    'WordPressSite', 'DatabaseSnapshot', 'SyncJob', 'WordPressVulnerability',
    'WordPressCustomPlugin', 'WordPressSitePlugin',
    'EnvironmentActivity', 'PromotionJob', 'SanitizationProfile',
    'EmailDomain', 'EmailAccount', 'EmailAlias', 'EmailForwardingRule', 'DNSProviderConfig', 'EmailRelayConfig',
    'OAuthIdentity', 'SourceConnection', 'RegistrarConnection', 'ContainerRegistry', 'AppVolume', 'ManagedDatabase', 'ApiKey', 'ApiUsageLog', 'ApiUsageSummary',
    'EventSubscription', 'EventDelivery', 'Invitation',
    'ServerAlertThreshold', 'MetricAlert',
    'AgentPlugin', 'AgentPluginInstall',
    'ServerTemplate', 'ServerTemplateAssignment',
    'Workspace', 'WorkspaceMember', 'WorkspaceApiKey', 'ResourceGrant',
    'DNSZone', 'DNSRecord', 'ManagedDnsRecord', 'DnsChange', 'CfOpsChange',
    'Tunnel', 'ExposedService',
    'StatusPage', 'StatusComponent', 'HealthCheck', 'StatusIncident', 'StatusIncidentUpdate',
    'CloudProvider', 'CloudServer', 'CloudSnapshot',
    'PendingAgent',
    'InstalledPlugin',
    'AiConversation', 'AiMessage', 'AiPendingAction', 'AiRun',
    'ImageVulnerabilityScan', 'SbomArtifact',
    'PasskeyCredential',
    'SecretVault', 'Secret',
    'WebhookEndpoint', 'WebhookDelivery',
    'WafPolicy',
    'CloudflareWorker', 'CloudflareTunnel',
    'BackupPolicy', 'BackupRun', 'RestoreDrill',
    'QueueGroup', 'Queue', 'QueueMessage',
    'Notification', 'NotificationDelivery',
    'EmailBounceState',
    'EmailProviderConnection',
    'ChatWebhookConnection',
    'SystemEvent',
    'DomainRegistration',
    'Project', 'Environment',
    'ResourceTag', 'SharedVariableGroup', 'SharedVariable', 'SharedVariableGroupAttachment',
    'ApplicationPreview', 'ApplicationPreviewSettings',
    'ProxyStack',
    'SiteBaseDomain',
    'LoginLink',
    'ManagedDatabaseUser',
    'SiteImport',
    'CronRun',
    'SiteBandwidthDaily',
    'AppRequestMetric',
    'AppAttachment',
    'AppSlot',
    'FleetDoctorResult',
    'ServerSurvey',
    'DnsCutoverSnapshot',
    'Theme',
    'PluginStore',
    'SavedView',
    'RestorePoint',
]
