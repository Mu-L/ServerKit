// System info, settings, processes, nginx, PHP, Python, logs, cron,
// monitoring, backups, uptime, metrics, notifications, email, performance, mobile

// Admin - System Settings endpoints
export async function getSystemSettings() {
    const data = await this.request('/admin/settings');
    const result = {};
    if (data.settings && Array.isArray(data.settings)) {
        for (const setting of data.settings) {
            result[setting.key] = setting.value;
        }
    }
    return result;
}

export async function updateSystemSettings(settings) {
    return this.request('/admin/settings', {
        method: 'PUT',
        body: settings
    });
}

export async function getSystemSetting(key) {
    return this.request(`/admin/settings/${key}`);
}

export async function updateSystemSetting(key, value) {
    return this.request(`/admin/settings/${key}`, {
        method: 'PUT',
        body: { value }
    });
}

export async function getDomainDetection() {
    return this.request('/admin/settings/domain-detection');
}

export async function setCanonicalDomain(domain, httpsEnabled) {
    return this.request('/admin/settings/canonical-domain', {
        method: 'PUT',
        body: { domain, https_enabled: httpsEnabled }
    });
}

export async function getAdminStats() {
    return this.request('/admin/stats');
}

export async function getSystemNotices() {
    return this.request('/system/notices');
}

// Managed-sites domain & HTTPS (Phase 5)
export async function getSitesHttpsStatus() {
    return this.request('/admin/sites-https/status');
}

export async function setupSitesHttps(providerId, email, base) {
    return this.request('/admin/sites-https/setup', {
        method: 'POST',
        body: { provider_id: providerId, email, base }
    });
}

// Base-domain registry (managed sites can publish under several base domains)
export async function addSiteBaseDomain(domain, { dnsMode = 'wildcard', makeDefault = false } = {}) {
    return this.request('/admin/sites-https/base-domains', {
        method: 'POST',
        body: { domain, dns_mode: dnsMode, make_default: makeDefault }
    });
}

export async function removeSiteBaseDomain(domain) {
    return this.request(`/admin/sites-https/base-domains/${encodeURIComponent(domain)}`, {
        method: 'DELETE'
    });
}

export async function setDefaultSiteBaseDomain(domain) {
    return this.request(`/admin/sites-https/base-domains/${encodeURIComponent(domain)}/default`, {
        method: 'POST'
    });
}

export async function updateSiteBaseDomain(domain, { dnsMode } = {}) {
    return this.request(`/admin/sites-https/base-domains/${encodeURIComponent(domain)}`, {
        method: 'PATCH',
        body: { dns_mode: dnsMode }
    });
}

// System endpoints
export async function getSystemMetrics() {
    return this.request('/system/metrics');
}

export async function getSystemInfo() {
    return this.request('/system/info');
}

export async function getServerTime() {
    return this.request('/system/time');
}

export async function getTimezones() {
    return this.request('/system/timezones');
}

export async function setTimezone(timezone) {
    return this.request('/system/timezone', {
        method: 'PUT',
        body: { timezone }
    });
}

export async function getVersion() {
    return this.request('/system/version');
}

export async function checkUpdate() {
    return this.request('/system/check-update');
}

// ServerKit Cloud connect state and the managed presentation policy (plan 25).
// Both are admin-only on the backend; callers treat a refusal as "no profile".
export async function getConnectStatus() {
    return this.request('/connect/status');
}

export async function getManagedProfile() {
    return this.request('/connect/managed-profile');
}

export async function getPanelUpdateStatus() {
    return this.request('/system/update');
}

export async function startPanelUpdate() {
    return this.request('/system/update', {
        method: 'POST',
        body: { confirm: true }
    });
}

export async function getSystemProcesses() {
    return this.request('/system/processes');
}

export async function getServices() {
    return this.request('/system/services');
}

export async function healthCheck() {
    return this.request('/system/health');
}

// Process endpoints
export async function getProcesses(limit = 50, sortBy = 'cpu') {
    return this.request(`/processes?limit=${encodeURIComponent(limit)}&sort=${encodeURIComponent(sortBy)}`);
}

export async function getProcess(pid) {
    return this.request(`/processes/${pid}`);
}

export async function killProcess(pid, force = false) {
    return this.request(`/processes/${pid}?force=${encodeURIComponent(force)}`, { method: 'DELETE' });
}

export async function getServicesStatus() {
    return this.request('/processes/services');
}

export async function controlService(serviceName, action) {
    return this.request(`/processes/services/${serviceName}`, {
        method: 'POST',
        body: { action }
    });
}

// Nginx endpoints
export async function getNginxStatus() {
    return this.request('/nginx/status');
}

export async function testNginxConfig() {
    return this.request('/nginx/test', { method: 'POST' });
}

export async function reloadNginx() {
    return this.request('/nginx/reload', { method: 'POST' });
}

export async function getNginxSites() {
    return this.request('/nginx/sites');
}

export async function createNginxSite(siteData) {
    return this.request('/nginx/sites', {
        method: 'POST',
        body: siteData
    });
}

export async function enableNginxSite(name) {
    return this.request(`/nginx/sites/${name}/enable`, { method: 'POST' });
}

export async function disableNginxSite(name) {
    return this.request(`/nginx/sites/${name}/disable`, { method: 'POST' });
}

export async function deleteNginxSite(name) {
    return this.request(`/nginx/sites/${name}`, { method: 'DELETE' });
}

// Nginx Advanced endpoints
export async function getNginxProxyRules(domain) {
    return this.request(`/nginx/advanced/proxy/${domain}`);
}

export async function createNginxProxy(data) {
    return this.request('/nginx/advanced/proxy', { method: 'POST', body: data });
}

export async function testAdvancedNginxConfig() {
    return this.request('/nginx/advanced/test', { method: 'POST' });
}

export async function reloadAdvancedNginx() {
    return this.request('/nginx/advanced/reload', { method: 'POST' });
}

export async function previewNginxDiff(domain, config) {
    return this.request('/nginx/advanced/diff', {
        method: 'POST', body: { domain, config }
    });
}

export async function getNginxVhostLogs(domain, type = 'access', lines = 100) {
    return this.request(`/nginx/advanced/logs/${domain}?type=${encodeURIComponent(type)}&lines=${encodeURIComponent(lines)}`);
}

export async function getNginxLBMethods() {
    return this.request('/nginx/advanced/lb-methods');
}

// Log endpoints
export async function getLogFiles() {
    return this.request('/logs');
}

export async function readLog(filepath, lines = 100) {
    return this.request(`/logs/read?path=${encodeURIComponent(filepath)}&lines=${encodeURIComponent(lines)}`);
}

export async function searchLog(filepath, pattern, lines = 100) {
    return this.request(`/logs/search?path=${encodeURIComponent(filepath)}&pattern=${encodeURIComponent(pattern)}&lines=${encodeURIComponent(lines)}`);
}

export async function getAppLogs(appName, type = 'access', lines = 100) {
    return this.request(`/logs/app/${appName}?type=${encodeURIComponent(type)}&lines=${encodeURIComponent(lines)}`);
}

export async function getJournalLogs(unit, lines = 100) {
    const params = new URLSearchParams({ lines });
    if (unit) params.append('unit', unit);
    return this.request(`/logs/journal?${params}`);
}

export async function clearLog(filepath) {
    return this.request('/logs/clear', {
        method: 'POST',
        body: { path: filepath }
    });
}

// PHP endpoints
export async function getPHPVersions() {
    return this.request('/php/versions');
}

export async function installPHPVersion(version) {
    return this.request(`/php/versions/${version}/install`, { method: 'POST' });
}

export async function setDefaultPHPVersion(version) {
    return this.request('/php/versions/default', {
        method: 'POST',
        body: { version }
    });
}

export async function getPHPExtensions(version) {
    return this.request(`/php/versions/${version}/extensions`);
}

export async function installPHPExtension(version, extension) {
    return this.request(`/php/versions/${version}/extensions`, {
        method: 'POST',
        body: { extension }
    });
}

export async function getPHPPools(version) {
    return this.request(`/php/versions/${version}/pools`);
}

export async function createPHPPool(version, poolData) {
    return this.request(`/php/versions/${version}/pools`, {
        method: 'POST',
        body: poolData
    });
}

export async function deletePHPPool(version, poolName) {
    return this.request(`/php/versions/${version}/pools/${poolName}`, { method: 'DELETE' });
}

export async function restartPHPFPM(version) {
    return this.request(`/php/versions/${version}/fpm/restart`, { method: 'POST' });
}

export async function getPHPFPMStatus(version) {
    return this.request(`/php/versions/${version}/fpm/status`);
}

// Python endpoints
export async function getPythonVersions() {
    return this.request('/python/versions');
}

export async function createFlaskApp(data) {
    return this.request('/python/apps/flask', {
        method: 'POST',
        body: data
    });
}

export async function createDjangoApp(data) {
    return this.request('/python/apps/django', {
        method: 'POST',
        body: data
    });
}

export async function createPythonVenv(appId) {
    return this.request(`/python/apps/${appId}/venv`, { method: 'POST' });
}

export async function getPythonPackages(appId) {
    return this.request(`/python/apps/${appId}/packages`);
}

export async function installPythonPackages(appId, packages) {
    return this.request(`/python/apps/${appId}/packages`, {
        method: 'POST',
        body: { packages }
    });
}

export async function freezePythonRequirements(appId) {
    return this.request(`/python/apps/${appId}/requirements`, { method: 'POST' });
}

export async function getPythonEnvVars(appId) {
    return this.request(`/python/apps/${appId}/env`);
}

export async function setPythonEnvVars(appId, envVars) {
    return this.request(`/python/apps/${appId}/env`, {
        method: 'PUT',
        body: { env_vars: envVars }
    });
}

export async function deletePythonEnvVar(appId, key) {
    return this.request(`/python/apps/${appId}/env/${key}`, { method: 'DELETE' });
}

export async function startPythonApp(appId) {
    return this.request(`/python/apps/${appId}/start`, { method: 'POST' });
}

export async function stopPythonApp(appId) {
    return this.request(`/python/apps/${appId}/stop`, { method: 'POST' });
}

export async function restartPythonApp(appId) {
    return this.request(`/python/apps/${appId}/restart`, { method: 'POST' });
}

export async function getPythonAppStatus(appId) {
    return this.request(`/python/apps/${appId}/status`);
}

export async function getGunicornConfig(appId) {
    return this.request(`/python/apps/${appId}/gunicorn`);
}

export async function updateGunicornConfig(appId, content) {
    return this.request(`/python/apps/${appId}/gunicorn`, {
        method: 'PUT',
        body: { content }
    });
}

export async function runPythonMigrations(appId) {
    return this.request(`/python/apps/${appId}/migrate`, { method: 'POST' });
}

export async function collectPythonStatic(appId) {
    return this.request(`/python/apps/${appId}/collectstatic`, { method: 'POST' });
}

export async function runPythonCommand(appId, command) {
    return this.request(`/python/apps/${appId}/run`, {
        method: 'POST',
        body: { command }
    });
}

export async function deletePythonApp(appId, removeFiles = false) {
    return this.request(`/python/apps/${appId}`, {
        method: 'DELETE',
        body: { remove_files: removeFiles }
    });
}

// Monitoring & Alerts endpoints
export async function getMonitoringStatus() {
    return this.request('/monitoring/status');
}

export async function getMonitoringMetrics() {
    return this.request('/monitoring/metrics');
}

export async function checkAlerts() {
    return this.request('/monitoring/alerts/check');
}

export async function getAlertHistory(limit = 100) {
    return this.request(`/monitoring/alerts/history?limit=${encodeURIComponent(limit)}`);
}

export async function getMonitoringConfig() {
    return this.request('/monitoring/config');
}

export async function updateMonitoringConfig(config) {
    return this.request('/monitoring/config', {
        method: 'PUT',
        body: config
    });
}

export async function getMonitoringThresholds() {
    return this.request('/monitoring/thresholds');
}

export async function updateMonitoringThresholds(thresholds) {
    return this.request('/monitoring/thresholds', {
        method: 'PUT',
        body: thresholds
    });
}

export async function startMonitoring() {
    return this.request('/monitoring/start', { method: 'POST' });
}

export async function stopMonitoring() {
    return this.request('/monitoring/stop', { method: 'POST' });
}

export async function testEmailAlert(email) {
    return this.request('/monitoring/test/email', {
        method: 'POST',
        body: { email }
    });
}

export async function testWebhookAlert(webhookUrl) {
    return this.request('/monitoring/test/webhook', {
        method: 'POST',
        body: { webhook_url: webhookUrl }
    });
}

// Backup System endpoints
export async function getBackups(type = null) {
    const params = type ? `?type=${encodeURIComponent(type)}` : '';
    return this.request(`/backups${params}`);
}

export async function getBackupStats() {
    return this.request('/backups/stats');
}

export async function getBackupConfig() {
    return this.request('/backups/config');
}

// Storage-cost rates ($/GB/month): local = your server disk (free by default),
// s3/b2 = your real cloud-provider rate.
export async function getBackupCostRates() {
    return this.request('/backups/cost-rates');
}

export async function updateBackupCostRates(rates) {
    return this.request('/backups/cost-rates', { method: 'PUT', body: { rates } });
}

export async function getBackupCostSummary() {
    return this.request('/backups/cost-summary');
}

export async function updateBackupConfig(config) {
    return this.request('/backups/config', {
        method: 'PUT',
        body: config
    });
}

export async function backupApplication(applicationId, includeDb = false, dbConfig = null) {
    return this.request('/backups/application', {
        method: 'POST',
        body: {
            application_id: applicationId,
            include_db: includeDb,
            db_config: dbConfig
        }
    });
}

export async function backupDatabase(dbType, dbName, user = null, password = null, host = 'localhost') {
    return this.request('/backups/database', {
        method: 'POST',
        body: { db_type: dbType, db_name: dbName, user, password, host }
    });
}

export async function restoreApplication(backupPath, restorePath = null) {
    return this.request('/backups/restore/application', {
        method: 'POST',
        body: { backup_path: backupPath, restore_path: restorePath }
    });
}

export async function restoreDatabase(backupPath, dbType, dbName, user = null, password = null, host = 'localhost') {
    return this.request('/backups/restore/database', {
        method: 'POST',
        body: { backup_path: backupPath, db_type: dbType, db_name: dbName, user, password, host }
    });
}

export async function deleteBackup(backupPath) {
    return this.request(`/backups/${encodeURIComponent(backupPath)}`, { method: 'DELETE' });
}

export async function cleanupBackups(retentionDays = null) {
    return this.request('/backups/cleanup', {
        method: 'POST',
        body: retentionDays ? { retention_days: retentionDays } : {}
    });
}

export async function getBackupSchedules() {
    return this.request('/backups/schedules');
}

export async function addBackupSchedule(name, backupType, target, scheduleTime, days = null, uploadRemote = false) {
    return this.request('/backups/schedules', {
        method: 'POST',
        body: { name, backup_type: backupType, target, schedule_time: scheduleTime, days, upload_remote: uploadRemote }
    });
}

export async function updateBackupSchedule(scheduleId, updates) {
    return this.request(`/backups/schedules/${scheduleId}`, {
        method: 'PUT',
        body: updates
    });
}

export async function removeBackupSchedule(scheduleId) {
    return this.request(`/backups/schedules/${scheduleId}`, { method: 'DELETE' });
}

export async function backupFiles(filePaths, name = null) {
    return this.request('/backups/files', {
        method: 'POST',
        body: { paths: filePaths, name }
    });
}

// Remote Storage
export async function getStorageConfig() {
    return this.request('/backups/storage');
}

export async function updateStorageConfig(config) {
    return this.request('/backups/storage', {
        method: 'PUT',
        body: config
    });
}

export async function testStorageConnection(config = null) {
    return this.request('/backups/storage/test', {
        method: 'POST',
        body: config || {}
    });
}

export async function uploadBackupToRemote(backupPath) {
    return this.request('/backups/upload', {
        method: 'POST',
        body: { backup_path: backupPath }
    });
}

export async function verifyRemoteBackup(remoteKey, localPath) {
    return this.request('/backups/verify', {
        method: 'POST',
        body: { remote_key: remoteKey, local_path: localPath }
    });
}

export async function listRemoteBackups(prefix = null) {
    const params = prefix ? `?prefix=${encodeURIComponent(prefix)}` : '';
    return this.request(`/backups/remote${params}`);
}

export async function downloadRemoteBackup(remoteKey, localPath = null) {
    return this.request('/backups/remote/download', {
        method: 'POST',
        body: { remote_key: remoteKey, local_path: localPath }
    });
}

// Cron Job endpoints
export async function getCronStatus() {
    return this.request('/cron/status');
}

export async function getCronJobs() {
    return this.request('/cron/jobs');
}

export async function createCronJob(data) {
    return this.request('/cron/jobs', {
        method: 'POST',
        body: data
    });
}

export async function updateCronJob(jobId, data) {
    return this.request(`/cron/jobs/${jobId}`, {
        method: 'PUT',
        body: data
    });
}

export async function deleteCronJob(jobId) {
    return this.request(`/cron/jobs/${jobId}`, { method: 'DELETE' });
}

export async function toggleCronJob(jobId, enabled) {
    return this.request(`/cron/jobs/${jobId}/toggle`, {
        method: 'POST',
        body: { enabled }
    });
}

export async function runCronJob(jobId) {
    return this.request(`/cron/jobs/${jobId}/run`, { method: 'POST' });
}

export async function getCronPresets() {
    return this.request('/cron/presets');
}

export async function previewCronSchedule(schedule) {
    return this.request('/cron/preview', {
        method: 'POST',
        body: { schedule }
    });
}

export async function getCronJobsForApp(appId) {
    return this.request(`/cron/jobs/for-app/${appId}`);
}

export async function setCronTracking(jobId, { enabled, alertOnFailure } = {}) {
    const body = { enabled };
    if (alertOnFailure !== undefined) body.alert_on_failure = alertOnFailure;
    return this.request(`/cron/jobs/${jobId}/track`, { method: 'POST', body });
}

export async function getCronJobRuns(jobId) {
    return this.request(`/cron/jobs/${jobId}/runs`);
}

// Uptime Tracking endpoints
export async function getCurrentUptime() {
    return this.request('/uptime/current');
}

export async function getUptimeStats() {
    return this.request('/uptime/stats');
}

export async function getUptimeGraph(period = '24h') {
    return this.request(`/uptime/graph?period=${encodeURIComponent(period)}`);
}

export async function getUptimeHistory(hours = 24) {
    return this.request(`/uptime/history?hours=${encodeURIComponent(hours)}`);
}

export async function startUptimeTracking() {
    return this.request('/uptime/tracking/start', { method: 'POST' });
}

export async function stopUptimeTracking() {
    return this.request('/uptime/tracking/stop', { method: 'POST' });
}

export async function getUptimeTrackingStatus() {
    return this.request('/uptime/tracking/status');
}

// Metrics History endpoints
export async function getMetricsHistory(period = '1h') {
    return this.request(`/system/performance-history?period=${encodeURIComponent(period)}`);
}

export async function getMetricsStats() {
    return this.request('/metrics/stats');
}

export async function startMetricsCollection() {
    return this.request('/metrics/collection/start', { method: 'POST' });
}

export async function stopMetricsCollection() {
    return this.request('/metrics/collection/stop', { method: 'POST' });
}

export async function triggerMetricsAggregation() {
    return this.request('/metrics/aggregate', { method: 'POST' });
}

// Notification Webhooks endpoints
export async function getNotificationsStatus() {
    return this.request('/notifications/status');
}

export async function getNotificationsConfig() {
    return this.request('/notifications/config');
}

export async function updateNotificationChannel(channel, settings) {
    return this.request(`/notifications/config/${channel}`, {
        method: 'PUT',
        body: settings
    });
}

export async function testNotificationChannel(channel) {
    return this.request(`/notifications/test/${channel}`, {
        method: 'POST'
    });
}

export async function testAllNotifications() {
    return this.request('/notifications/test', {
        method: 'POST'
    });
}

// User notification preferences
export async function getUserNotificationPreferences() {
    return this.request('/notifications/preferences');
}

export async function updateUserNotificationPreferences(preferences) {
    return this.request('/notifications/preferences', {
        method: 'PUT',
        body: preferences
    });
}

export async function testUserNotification() {
    return this.request('/notifications/preferences/test', {
        method: 'POST'
    });
}

// Email Server management moved into the serverkit-email extension
// (builtin-extensions/serverkit-email/frontend/services/email.js) with the
// Email page — plan 52 Phase 2. Core keeps only the two email-adjacent
// surfaces that core Settings → Connections renders: the DNS-provider
// connections below (served by the core dns_providers blueprint, which owns
// the historical /email/dns-providers prefix) and the outbound relay tile
// (served by the extension's API; the tile degrades when it is absent).

// DNS Providers
export async function getEmailDNSProviders() { return this.request('/email/dns-providers'); }
export async function addEmailDNSProvider(data) { return this.request('/email/dns-providers', { method: 'POST', body: JSON.stringify(data) }); }
export async function deleteEmailDNSProvider(providerId) { return this.request(`/email/dns-providers/${providerId}`, { method: 'DELETE' }); }
export async function testEmailDNSProvider(providerId) { return this.request(`/email/dns-providers/${providerId}/test`, { method: 'POST' }); }
export async function getEmailDNSZones(providerId) { return this.request(`/email/dns-providers/${providerId}/zones`); }

// Outbound SMTP relay (smarthost)
export async function getEmailRelay() { return this.request('/email/relay'); }
export async function updateEmailRelay(data) { return this.request('/email/relay', { method: 'PUT', body: JSON.stringify(data) }); }
export async function testEmailRelay(data) { return this.request('/email/relay/test', { method: 'POST', body: JSON.stringify(data) }); }
export async function disableEmailRelay() { return this.request('/email/relay', { method: 'DELETE' }); }

// Performance endpoints
export async function getCacheStats() {
    return this.request('/performance/cache/stats');
}

export async function flushCache() {
    return this.request('/performance/cache/flush', { method: 'POST' });
}

export async function getBackgroundJobs() {
    return this.request('/performance/jobs');
}

export async function getJobStatus(jobId) {
    return this.request(`/performance/jobs/${jobId}`);
}

export async function getBackgroundJobStats() {
    return this.request('/performance/jobs/stats');
}

export async function cleanupJobs() {
    return this.request('/performance/jobs/cleanup', { method: 'POST' });
}

// Mobile / PWA endpoints
export async function registerPushDevice(subscription, deviceName) {
    return this.request('/mobile/push/register', {
        method: 'POST', body: { subscription, device_name: deviceName }
    });
}

export async function unregisterPushDevice(endpoint) {
    return this.request('/mobile/push/unregister', {
        method: 'POST', body: { endpoint }
    });
}

export async function getQuickActions() {
    return this.request('/mobile/quick-actions');
}

export async function executeQuickAction(actionId, params = {}) {
    return this.request(`/mobile/quick-actions/${actionId}`, {
        method: 'POST', body: params
    });
}

export async function getMobileSummary() {
    return this.request('/mobile/summary');
}

export async function getOfflineCache() {
    return this.request('/mobile/offline-cache');
}

// Marketplace endpoints. (The legacy DB-seeded Extension catalog was retired,
// #51 — Browse merges builtins + the registry + installed plugin state.)
// Remote/bundled registry catalog. Returns:
//   { extensions: [ { slug, display_name, description, version, category,
//                     author, first_party, permissions, min_panel_version,
//                     max_panel_version, source, sha256, icon, screenshots,
//                     installed, status, installed_version, source_kind } ],
//     source: 'bundled'|'remote'|null }
export async function getRegistryExtensions() {
    return this.request('/marketplace/registry');
}

// One-click install for a registry extension by slug (admin-only). Pass
// `{ acknowledge_risk: true }` to confirm an unreviewed community extension
// after the 409 `requires_acknowledgment` gate.
export async function installRegistryExtension(slug, body) {
    return this.request(`/marketplace/registry/${encodeURIComponent(slug)}/install`, {
        method: 'POST',
        ...(body ? { body } : {}),
    });
}

// Feature module toggles (Email + WordPress verticals). getModules() is
// available to any authenticated user; setModule() is admin-only.
export async function getModules() {
    return this.request('/modules');
}

export async function setModule(name, enabled) {
    return this.request(`/modules/${name}`, {
        method: 'PUT', body: { enabled }
    });
}
