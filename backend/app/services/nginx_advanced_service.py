import os
import logging
from app.services.nginx_service import NginxService
from app.utils.system import run_unprivileged

logger = logging.getLogger(__name__)


class NginxAdvancedService:
    """Read-side nginx helpers: vhost config, config test, diff preview, logs.

    The reverse-proxy / load-balancer builder was removed with the scaling
    pieces (plan 86 §E2): one live copy per app, no upstream pools."""

    # NginxService owns these (plan 75 §G4). Hardcoding them here meant an
    # NGINX_CONF_DIR override — the env var NginxService honours, and what
    # tests redirect — silently did not apply to this service.
    NGINX_CONF_DIR = NginxService.NGINX_CONF_DIR
    SITES_AVAILABLE = NginxService.SITES_AVAILABLE
    SITES_ENABLED = NginxService.SITES_ENABLED

    @staticmethod
    def get_proxy_rules(domain):
        """Get reverse proxy rules for a virtual host."""
        conf_path = os.path.join(NginxAdvancedService.SITES_AVAILABLE, domain)
        if not os.path.isfile(conf_path):
            return {'error': 'Config not found'}
        try:
            with open(conf_path, 'r') as f:
                content = f.read()
            return {'domain': domain, 'config': content}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def test_config():
        """Test nginx config syntax — NginxService owns the call (plan 75 §G4).

        Kept as a distinct method because its result shape (``valid``/
        ``output``) is what this service's API consumers read.
        """
        result = NginxService.test_config()
        return {
            'valid': result['success'],
            'output': result.get('message') or result.get('error') or '',
        }

    @staticmethod
    def preview_diff(domain, new_config):
        """Preview config changes as a diff."""
        # `domain` is joined onto SITES_AVAILABLE below — refuse anything that
        # is not a plain filename so ../ can't turn this into an arbitrary
        # file read (the API layer gates admins, but never trust one layer).
        if not domain or os.path.basename(domain) != domain or domain in ('.', '..'):
            return {'error': 'invalid domain'}
        conf_path = os.path.join(NginxAdvancedService.SITES_AVAILABLE, domain)
        old_config = ''
        if os.path.isfile(conf_path):
            with open(conf_path, 'r') as f:
                old_config = f.read()

        import difflib
        diff = list(difflib.unified_diff(
            old_config.splitlines(keepends=True),
            new_config.splitlines(keepends=True),
            fromfile=f'{domain} (current)',
            tofile=f'{domain} (new)',
        ))
        return {'diff': ''.join(diff), 'has_changes': len(diff) > 0}

    @staticmethod
    def reload_nginx():
        """Reload nginx — NginxService owns it (plan 75 §G4).

        The private version ran `nginx -s reload` with no config test first, so
        a broken vhost written by this same service reloaded straight into
        production instead of being refused.
        """
        return NginxService.reload()

    @staticmethod
    def get_vhost_logs(domain, log_type='access', lines=100):
        """Get access or error log for a virtual host."""
        log_dir = '/var/log/nginx'
        if log_type == 'error':
            log_file = os.path.join(log_dir, f'{domain}.error.log')
        else:
            log_file = os.path.join(log_dir, f'{domain}.access.log')

        if not os.path.isfile(log_file):
            # Fallback to default logs
            log_file = os.path.join(log_dir, f'{log_type}.log')

        if not os.path.isfile(log_file):
            return {'lines': [], 'error': 'Log file not found'}

        try:
            result = run_unprivileged(['tail', '-n', str(lines), log_file])
            log_lines = result.get('stdout', '').strip().split('\n')
            return {'lines': log_lines, 'file': log_file}
        except Exception as e:
            return {'lines': [], 'error': str(e)}
