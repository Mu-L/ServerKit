from app.utils.env import env_bool
import time
import urllib.request
import json
from flask import Blueprint, jsonify, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.error_reporting import unexpected_response
from app.exceptions import ValidationError
from app.models import User
from app.middleware.rbac import admin_required, get_current_user, viewer_required
from app.api.metrics import metrics_history_response
from app.services.system_service import SystemService
from app.services.resource_tier_service import ResourceTierService
from app.services import install_profile_service
from app.services.site_domain_service import SiteDomainService
from app.utils.domain import is_valid_canonical_domain
from app.utils.version import get_panel_version, get_install_dir
from app.middleware.rbac import require_admin_user

# Cache for update check (to avoid hitting GitHub API too often)
_update_cache = {
    'last_check': 0,
    'result': None,
    'ttl': 3600  # 1 hour cache
}

system_bp = Blueprint('system', __name__)


@system_bp.route('/version', methods=['GET'])
@jwt_required()
def get_version():
    """Get ServerKit version.

    Resolution (SERVERKIT_INSTALL_DIR override → own tree → /opt/serverkit)
    lives in app.utils.version so custom install dirs report honestly.
    """
    return jsonify({
        'version': get_panel_version(),
        'name': 'ServerKit',
        # Where the panel is installed — consumers (e.g. the File Manager's
        # "Stack" quick link) must not assume /opt/serverkit.
        'install_dir': get_install_dir()
    }), 200


@system_bp.route('/check-update', methods=['GET'])
@jwt_required()
def check_update():
    """Check for updates from GitHub releases."""
    global _update_cache

    # Return cached result if still valid
    if _update_cache['result'] and (time.time() - _update_cache['last_check']) < _update_cache['ttl']:
        return jsonify(_update_cache['result']), 200

    # Get current version (shared resolver — honors SERVERKIT_INSTALL_DIR)
    current_version = get_panel_version()

    result = {
        'current_version': current_version,
        'latest_version': None,
        'update_available': False,
        'release_url': None,
        'release_notes': None,
        'error': None
    }

    try:
        # Fetch latest release from GitHub API
        req = urllib.request.Request(
            'https://api.github.com/repos/jhd3197/ServerKit/releases/latest',
            headers={'User-Agent': 'ServerKit-UpdateChecker'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

            latest_version = data.get('tag_name', '').lstrip('v')
            result['latest_version'] = latest_version
            result['release_url'] = data.get('html_url')
            result['release_notes'] = data.get('body', '')[:500]  # Truncate release notes

            # Simple version comparison (works for semver-like versions)
            def parse_version(v):
                try:
                    return tuple(int(x) for x in v.split('.'))
                except:
                    return (0, 0, 0)

            if parse_version(latest_version) > parse_version(current_version):
                result['update_available'] = True

    except urllib.error.HTTPError as e:
        if e.code == 404:
            result['error'] = 'No releases found'
        else:
            result['error'] = f'GitHub API error: {e.code}'
    except Exception as e:
        result['error'] = f'Failed to check for updates: {str(e)}'

    # Cache the result
    _update_cache['last_check'] = time.time()
    _update_cache['result'] = result

    return jsonify(result), 200


@system_bp.route('/update', methods=['GET'])
@admin_required
def get_panel_update_status():
    """Self-update capability + live progress (poll target for the UI).

    Stateless by design: the update restarts this very process, so the answer
    is rebuilt each call from the transient unit's state and the updater's own
    log file (see panel_update_service).
    """
    from app.services import panel_update_service as svc
    try:
        return jsonify(svc.get_status()), 200
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


@system_bp.route('/update', methods=['POST'])
@admin_required
def start_panel_update():
    """Launch scripts/update.sh detached from the panel's own systemd unit.

    Body must carry ``{"confirm": true}`` — this restarts the panel backend.
    """
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        raise ValidationError('Confirmation required: pass {"confirm": true}.')

    user = get_current_user()
    from app.services import panel_update_service as svc
    result = svc.start_update()
    _audit('panel.update', user.id, target_type='host', target_id='panel',
           details={'unit': result['unit'],
                    'from_version': get_panel_version()})
    return jsonify(result), 202


@system_bp.route('/metrics', methods=['GET'])
@jwt_required()
def get_metrics():
    require_admin_user()

    metrics = SystemService.get_all_metrics()
    return jsonify(metrics), 200


@system_bp.route('/performance-history', methods=['GET'])
@viewer_required
def get_performance_history():
    """Get local metrics history through a content-blocker-safe route."""
    return metrics_history_response()


@system_bp.route('/info', methods=['GET'])
@jwt_required()
def get_system_info():
    """Get system information (hostname, IP, kernel, CPU info)."""
    require_admin_user()

    return jsonify(SystemService.get_system_info()), 200


@system_bp.route('/cpu', methods=['GET'])
@jwt_required()
def get_cpu_metrics():
    require_admin_user()

    return jsonify(SystemService.get_cpu_metrics()), 200


@system_bp.route('/memory', methods=['GET'])
@jwt_required()
def get_memory_metrics():
    require_admin_user()

    return jsonify(SystemService.get_memory_metrics()), 200


@system_bp.route('/disk', methods=['GET'])
@jwt_required()
def get_disk_metrics():
    require_admin_user()

    return jsonify(SystemService.get_disk_metrics()), 200


@system_bp.route('/storage', methods=['GET'])
@admin_required
def get_storage_overview():
    """Where the panel host's disk goes, plus the retention settings that
    bound ServerKit's own share. Reads only."""
    from app.services import storage_overview_service

    try:
        return jsonify(storage_overview_service.overview()), 200
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


@system_bp.route('/disk/reclaim/report', methods=['GET'])
@admin_required
def get_disk_reclaim_report():
    """Measure every safe reclaim candidate. Reads only — deletes nothing."""
    from app.services import disk_reclaim_service as svc

    try:
        return jsonify(svc.scan()), 200
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


@system_bp.route('/disk/reclaim', methods=['POST'])
@admin_required
def reclaim_disk_space():
    """Reclaim a curated safe set of disk-space candidates.

    Body must carry ``{"confirm": true, "keys": [...]}``; every key is
    re-validated against a fresh scan server-side (safe candidates only).
    ``?wait=false`` enqueues the work as a background job instead of running
    it inline — reclaims can take minutes when Docker or a VACUUM is involved.
    """
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        raise ValidationError('Confirmation required: pass {"confirm": true}.')
    keys = data.get('keys')
    if (not isinstance(keys, list) or not keys
            or not all(isinstance(k, str) and k for k in keys)):
        raise ValidationError("Body must carry a non-empty 'keys' list.")

    user = get_current_user()
    from app.services import disk_reclaim_service as svc

    try:
        keys, error = svc.validate_reclaim_keys(keys)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)
    if error:
        raise ValidationError(error)

    try:
        wait = request.args.get('wait', 'true').lower() != 'false'
        if not wait:
            from app.jobs.service import JobService
            job = JobService.enqueue(
                svc.DISK_RECLAIM_JOB_KIND,
                payload={'keys': keys},
                max_attempts=1,
                owner_type='system',
                owner_id='disk',
            )
            _audit('disk.reclaim', user.id, target_type='job',
                   target_id=str(job.id), details={'keys': keys, 'queued': True})
            return jsonify({'job_id': job.id, 'kind': svc.DISK_RECLAIM_JOB_KIND}), 202

        result = svc.reclaim(keys)
        _audit('disk.reclaim', user.id, target_type='host', target_id='disk',
               details={'keys': keys, 'freed_bytes': result.get('freed_bytes')})
        return jsonify(result), 200
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


def _audit(action, user_id, target_type=None, target_id=None, details=None):
    """Best-effort audit entry — never breaks the request."""
    try:
        from app.services.audit_service import AuditService
        AuditService.log(action, user_id=user_id, target_type=target_type,
                         target_id=target_id, details=details)
    except Exception:  # noqa: BLE001
        pass


@system_bp.route('/network', methods=['GET'])
@jwt_required()
def get_network_metrics():
    require_admin_user()

    return jsonify(SystemService.get_network_metrics()), 200


@system_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint - no auth required"""
    from app.utils.crypto import is_encryption_configured
    from app.services.settings_service import SettingsService
    from app.utils.domain import canonical_origin

    canonical_domain = SettingsService.get('canonical_domain', '') or ''
    https_enabled = bool(SettingsService.get('canonical_https_enabled', False))

    # Staging instances (plan 37 testbed) run with SERVERKIT_STAGING set. Echo
    # it here so the verify step can prove WHICH instance answered and the UI
    # can show its STAGING banner.
    staging = env_bool('SERVERKIT_STAGING')

    return jsonify({
        'status': 'healthy',
        'service': 'serverkit-api',
        'encryption_configured': is_encryption_configured(),
        'canonical_domain': canonical_domain,
        'canonical_https_enabled': https_enabled,
        'canonical_origin': canonical_origin(canonical_domain, https_enabled) if canonical_domain else None,
        'staging': staging,
    }), 200


@system_bp.route('/notices', methods=['GET'])
@jwt_required()
def get_system_notices():
    """Return dismissible system notices for common misconfigurations."""
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)

    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    notices = []
    canonical_domain = (SiteDomainService.panel_origin() or '').replace('https://', '').replace('http://', '')
    base_domain = SiteDomainService.base_domain()
    server_ip = SiteDomainService.server_ip()
    https_enabled = SiteDomainService.https_enabled()

    if not canonical_domain or not is_valid_canonical_domain(canonical_domain):
        notices.append({
            'id': 'missing_canonical_domain',
            'level': 'warning',
            'title': 'Canonical domain not configured',
            'message': 'Set the panel\'s canonical domain so install commands, agents, and app URLs point to the right place.',
            'action_label': 'Fix in Settings',
            'action_path': '/settings/site',
            'setting_key': 'canonical_domain',
        })

    if not base_domain:
        notices.append({
            'id': 'missing_sites_base_domain',
            'level': 'warning',
            'title': 'Managed sites base domain not set',
            'message': 'New sites will fall back to localhost:<port>. Add a base domain to publish sites at <name>.<domain>.',
            'action_label': 'Fix in Settings',
            'action_path': '/settings/site',
            'setting_key': 'sites_base_domain',
        })

    if not server_ip:
        notices.append({
            'id': 'missing_server_public_ip',
            'level': 'info',
            'title': 'Server public IP not configured',
            'message': 'Set the server public IP so ServerKit can auto-create DNS records for managed sites and custom domains.',
            'action_label': 'Fix in Settings',
            'action_path': '/settings/site',
            'setting_key': 'server_public_ip',
        })

    if base_domain and server_ip and not https_enabled:
        notices.append({
            'id': 'managed_sites_https_not_setup',
            'level': 'info',
            'title': 'Managed sites HTTPS is not set up',
            'message': f'Wildcard HTTPS for *.{base_domain} is not active. Set it up so published sites serve HTTPS.',
            'action_label': 'Set up wildcard HTTPS',
            'action_path': '/settings/site',
            'setting_key': 'sites_https_enabled',
        })

    return jsonify({'notices': notices}), 200


@system_bp.route('/resource-tier', methods=['GET'])
@jwt_required()
def get_resource_tier():
    """
    Get server resource tier information.

    Returns tier, specs, and feature permissions based on server resources.
    Results are cached for 1 hour unless refresh=true is passed.
    """
    require_admin_user()

    force_refresh = request.args.get('refresh', 'false').lower() == 'true'
    tier_info = ResourceTierService.get_tier_info(force_refresh=force_refresh)

    return jsonify(tier_info), 200


@system_bp.route('/host-snapshot', methods=['GET'])
@jwt_required()
def get_host_snapshot():
    """Per-filesystem inventory, storage advisories, and the last spec delta.

    Unlike /disk — which lists partitions and stops — this says which
    filesystem the panel actually writes to, which mounts are missing from
    /etc/fstab, and what changed since the previous boot.
    """
    require_admin_user()

    from app.services import host_snapshot_service
    return jsonify(host_snapshot_service.current_state()), 200


@system_bp.route('/host-snapshot/history', methods=['GET'])
@jwt_required()
def get_host_snapshot_history():
    """Recent host snapshots, newest first."""
    require_admin_user()

    from app.models.host_snapshot import HostSnapshot

    try:
        limit = min(max(int(request.args.get('limit', 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20

    rows = (HostSnapshot.query
            .order_by(HostSnapshot.captured_at.desc(), HostSnapshot.id.desc())
            .limit(limit)
            .all())
    return jsonify({'snapshots': [row.to_dict() for row in rows]}), 200


@system_bp.route('/capacity', methods=['GET'])
@jwt_required()
def get_capacity():
    """
    Server capacity: live headroom, install profile, and probed capabilities.

    This is the endpoint the setup wizard and the dashboard should read.
    /resource-tier remains for callers that only want the tier label; this one
    additionally answers "what did we install" and "can this box host apps".
    """
    require_admin_user()

    force_refresh = request.args.get('refresh', 'false').lower() == 'true'
    tier_info = ResourceTierService.get_tier_info(force_refresh=force_refresh)
    profile_info = install_profile_service.get_profile_info(force_refresh=force_refresh)

    return jsonify({
        **tier_info,
        **profile_info,
        'recommended_profile': install_profile_service.recommend_profile(
            tier_info['specs']
        ),
    }), 200


@system_bp.route('/capacity/profile', methods=['PUT'])
@jwt_required()
def set_capacity_profile():
    """Record an operator's profile change made after install."""
    user = require_admin_user()

    data = request.get_json() or {}
    profile = data.get('profile')

    try:
        install_profile_service.set_profile(profile, user_id=user.id)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    return jsonify(install_profile_service.get_profile_info()), 200


@system_bp.route('/time', methods=['GET'])
@jwt_required()
def get_server_time():
    """Get current server time and timezone."""
    return jsonify(SystemService.get_server_time()), 200


@system_bp.route('/timezones', methods=['GET'])
@jwt_required()
def get_timezones():
    """Get list of available timezones."""
    require_admin_user()

    timezones = SystemService.get_available_timezones()
    return jsonify({'timezones': timezones}), 200


@system_bp.route('/timezone', methods=['PUT'])
@jwt_required()
def set_timezone():
    """Set server timezone (admin only)."""
    require_admin_user()

    data = request.get_json()
    if not data or 'timezone' not in data:
        return jsonify({'error': 'Timezone is required'}), 400

    result = SystemService.set_timezone(data['timezone'])

    if result['success']:
        return jsonify(result), 200
    else:
        return jsonify(result), 400


@system_bp.route('/processes', methods=['GET'])
@jwt_required()
def get_processes():
    require_admin_user()

    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            pinfo = proc.info
            if pinfo['cpu_percent'] > 0 or pinfo['memory_percent'] > 0.1:
                processes.append({
                    'pid': pinfo['pid'],
                    'name': pinfo['name'],
                    'cpu_percent': pinfo['cpu_percent'],
                    'memory_percent': round(pinfo['memory_percent'], 2),
                    'status': pinfo['status']
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Sort by CPU usage
    processes.sort(key=lambda x: x['cpu_percent'], reverse=True)

    return jsonify({
        'processes': processes[:50]  # Top 50 processes
    }), 200


@system_bp.route('/services', methods=['GET'])
@jwt_required()
def get_services():
    require_admin_user()

    # Common services to check
    service_names = ['nginx', 'mysql', 'postgresql', 'redis', 'docker', 'php-fpm']
    services = []

    for name in service_names:
        running = False
        for proc in psutil.process_iter(['name']):
            try:
                if name.lower() in proc.info['name'].lower():
                    running = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        services.append({
            'name': name,
            'status': 'running' if running else 'stopped'
        })

    return jsonify({'services': services}), 200
