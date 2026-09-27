# Bucket: PER-APP read (plan 29 #9). Per-app bandwidth is scoped to callers who
# can access the app (can_access_app); the cross-app overview stays panel-wide.
"""REST surface for per-domain bandwidth accounting.

Mounted at /api/v1/bandwidth (registered in app/__init__.py).
"""
from flask import Blueprint, jsonify, request
from app.middleware.rbac import get_current_user
from app.error_reporting import unexpected_response
from app.exceptions import NotFoundError

from ..middleware.rbac import admin_required, viewer_required
from ..models import Application
from ..services.bandwidth_service import BandwidthService
from ..services.request_metrics_service import RequestMetricsService
from ..services.resource_grant_service import ResourceGrantService

bandwidth_bp = Blueprint('bandwidth', __name__)


@bandwidth_bp.route('/apps', methods=['GET'])
@viewer_required
def get_apps_bandwidth():
    """Month totals + 30-day sparkline series for every app with traffic —
    one call for the Services list."""
    try:
        data = BandwidthService.overview(days=30)
        # JSON object keys must be strings.
        return jsonify({'apps': {str(k): v for k, v in data.items()}})
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


@bandwidth_bp.route('/apps/<int:app_id>', methods=['GET'])
@viewer_required
def get_app_bandwidth(app_id):
    """Full daily series (default 90 days) + current-month total for one app,
    scoped to the app's workspace visibility (plan 29 #9 — foreign caller 404)."""
    app = Application.query_active().filter_by(id=app_id).first()
    user = get_current_user()
    if app is None or not ResourceGrantService.can_access_app(user, app):
        return jsonify({'error': 'Not found'}), 404
    try:
        days = request.args.get('days', 90, type=int)
        series = BandwidthService.series(app_id=app_id, days=days)
        return jsonify({
            'app_id': app_id,
            'days': len(series),
            'series': series,
            'month_bytes': BandwidthService.monthly_total(app_id),
        })
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)


@bandwidth_bp.route('/apps/<int:app_id>/requests', methods=['GET'])
@viewer_required
def get_app_request_metrics(app_id):
    """Request rate, status classes, latency percentiles and cache hit ratio
    from the timed access log (plan 86 §A2). ``period``: 1h, 24h, 7d or 30d."""
    # Same 404 for a missing and a foreign app (plan 29 #9); a bad period is a
    # ValidationError (400). Both go through the global handler.
    app = RequestMetricsService.live_app(app_id)
    if app is None or not ResourceGrantService.can_access_app(get_current_user(), app):
        raise NotFoundError('Not found')
    return jsonify(RequestMetricsService.series(
        app_id, period=request.args.get('period', '24h')))


@bandwidth_bp.route('/aggregate', methods=['POST'])
@admin_required
def run_aggregate():
    """Run the daily aggregation now (optionally for a specific day)."""
    try:
        payload = request.get_json(silent=True) or {}
        result = BandwidthService.aggregate(day=payload.get('day'))
        return jsonify(result)
    except ValueError:
        return jsonify({'error': 'day must be YYYY-MM-DD'}), 400
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return unexpected_response(exc)
