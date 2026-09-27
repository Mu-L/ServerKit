"""
Environment Domain Service

Manages per-environment domains and Nginx configurations for WordPress multidev.
Generates subdomains (staging.example.com, dev.example.com, branch-name.example.com)
and creates corresponding Nginx virtual host configs.
"""

import os
import re
from typing import Dict

from app.services.nginx_service import NginxService
from app.utils.slug import slugify as _slugify
from app.utils.system import run_checked, run_privileged, write_privileged_file


class EnvironmentDomainService:
    """Service for managing per-environment domains and Nginx configs."""

    # Not redeclared: NginxService owns where vhosts live (plan 75 §G4). Four
    # files used to hardcode these, so a test that redirected NginxService's
    # paths left the other three writing to the real /etc/nginx.
    SITES_AVAILABLE = NginxService.SITES_AVAILABLE
    SITES_ENABLED = NginxService.SITES_ENABLED

    # Nginx template for environment proxy (based on NginxService.DOCKER_SITE_TEMPLATE)
    ENV_SITE_TEMPLATE = '''server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    access_log /var/log/nginx/{site_name}.access.log serverkit_timed;
    error_log /var/log/nginx/{site_name}.error.log;

{extra_headers}
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 86400;
        proxy_connect_timeout 60;
        proxy_send_timeout 60;
    }}
{robots_block}
}}
'''

    # Extra headers for non-production environments
    NON_PROD_HEADERS = '    add_header X-Robots-Tag "noindex, nofollow" always;'

    # Robots.txt block for non-production environments
    ROBOTS_BLOCK = '''
    location = /robots.txt {{
        return 200 "User-agent: *\\nDisallow: /\\n";
        add_header Content-Type text/plain;
    }}
'''

    @classmethod
    def generate_domain(cls, production_domain: str, env_type: str,
                        branch_name: str = None) -> str:
        """Generate a domain name for an environment.

        Args:
            production_domain: The production domain (e.g., example.com)
            env_type: Environment type (production, staging, development, multidev)
            branch_name: Branch name for multidev environments

        Returns:
            Domain string for the environment
        """
        if env_type == 'production':
            return production_domain

        if not production_domain:
            # Fallback for sites without a domain: use the managed-sites base domain
            # or the panel domain instead of a useless .localhost address.
            from app.services.site_domain_service import SiteDomainService
            base = SiteDomainService.base_domain() or SiteDomainService.panel_origin()
            if base:
                from urllib.parse import urlparse
                base_host = urlparse(base).hostname or base
                slug = cls.slugify(branch_name) if branch_name else env_type
                return f'{env_type}-{slug}.{base_host}'
            # Last resort only when absolutely no domain is configured
            slug = cls.slugify(branch_name) if branch_name else env_type
            return f'{env_type}-{slug}.localhost'

        if env_type == 'staging':
            return f'staging.{production_domain}'

        if env_type == 'development':
            return f'dev.{production_domain}'

        if env_type == 'multidev' and branch_name:
            slug = cls.slugify(branch_name)
            return f'{slug}.{production_domain}'

        # Fallback
        return f'{env_type}.{production_domain}'

    @classmethod
    def create_nginx_config(cls, site_name: str, domain: str, upstream_port: int,
                            env_type: str = 'production') -> Dict:
        """Create and enable an Nginx virtual host for an environment.

        Args:
            site_name: Unique site name for the config file
            domain: Domain name to serve
            upstream_port: Port the Docker container listens on
            env_type: Environment type (controls noindex headers)

        Returns:
            Dict with success status
        """
        try:
            is_production = env_type == 'production'

            # Build template variables
            extra_headers = '' if is_production else cls.NON_PROD_HEADERS
            robots_block = '' if is_production else cls.ROBOTS_BLOCK

            config = cls.ENV_SITE_TEMPLATE.format(
                domain=domain,
                site_name=site_name,
                port=upstream_port,
                extra_headers=extra_headers,
                robots_block=robots_block,
            )

            # write + enable + nginx -t + rollback + reload, all owned by
            # NginxService (plan 75 §G4). This block used to re-implement it
            # with hardcoded `sudo`, which is the founding bug class of plan 75:
            # it breaks on a panel running as root without sudo, and never
            # rolled back a vhost that failed the config test.
            result = NginxService.write_vhost(site_name, config)
            if not result['success']:
                return result

            return {
                'success': True,
                'config_path': result['path'],
                'domain': domain,
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def remove_nginx_config(cls, site_name: str) -> Dict:
        """Remove an Nginx virtual host config and reload.

        Args:
            site_name: The config file name to remove

        Returns:
            Dict with success status
        """
        try:
            NginxService.delete_site(site_name)
            return {'success': True, 'message': f'Nginx config for {site_name} removed'}

        except Exception as e:
            return {'success': False, 'error': str(e)}

    # ==================== BASIC AUTH ====================

    HTPASSWD_DIR = '/etc/nginx/htpasswd'

    @classmethod
    def enable_basic_auth(cls, site_name: str, username: str, password: str) -> Dict:
        """Enable HTTP Basic Auth on an Nginx site.

        Generates an htpasswd file and modifies the Nginx config to require auth.

        Args:
            site_name: The Nginx config file name
            username: Basic Auth username
            password: Plain text password (will be hashed)

        Returns:
            Dict with success status and credentials
        """
        try:
            # Ensure htpasswd directory exists
            run_privileged(['mkdir', '-p', cls.HTPASSWD_DIR])

            # Generate password hash using openssl
            hash_result = run_checked(['openssl', 'passwd', '-apr1', password],
                                      timeout=10)
            if not hash_result['success']:
                return {'success': False,
                        'error': f"Failed to hash password: {hash_result['error']}"}

            password_hash = hash_result['output'].strip()
            htpasswd_content = f'{username}:{password_hash}\n'

            # Write htpasswd file
            htpasswd_path = os.path.join(cls.HTPASSWD_DIR, site_name)
            written = write_privileged_file(htpasswd_path, htpasswd_content, mode='644')
            if not written['success']:
                return {'success': False,
                        'error': f"Failed to write htpasswd: {written['error']}"}

            # Modify Nginx config to add auth_basic directives
            config_content = NginxService.read_vhost(site_name)
            if config_content is None:
                return {'success': False, 'error': 'Failed to read Nginx config'}

            # Check if auth_basic already exists
            if 'auth_basic' in config_content:
                # Already has basic auth, just update htpasswd file
                pass
            else:
                # Insert auth_basic directives after server_name line
                auth_block = (
                    f'\n    auth_basic "Restricted Access";\n'
                    f'    auth_basic_user_file {htpasswd_path};\n'
                )
                config_content = re.sub(
                    r'(server_name\s+[^;]+;)',
                    r'\1' + auth_block,
                    config_content,
                    count=1
                )

                # Write updated config (tested + rolled back by NginxService)
                result = NginxService.write_vhost(site_name, config_content)
                if not result['success']:
                    return result

            return {
                'success': True,
                'username': username,
                'password_hash': password_hash,
                'htpasswd_path': htpasswd_path,
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def disable_basic_auth(cls, site_name: str) -> Dict:
        """Disable HTTP Basic Auth on an Nginx site.

        Removes auth_basic directives from config and deletes the htpasswd file.

        Args:
            site_name: The Nginx config file name

        Returns:
            Dict with success status
        """
        try:
            # Remove htpasswd file
            htpasswd_path = os.path.join(cls.HTPASSWD_DIR, site_name)
            run_privileged(['rm', '-f', htpasswd_path])

            # Remove auth_basic directives from Nginx config
            config_content = NginxService.read_vhost(site_name)
            if config_content is None:
                return {'success': False, 'error': 'Failed to read Nginx config'}

            # Remove auth_basic lines
            config_content = re.sub(r'\n\s*auth_basic\s+"[^"]*";\n', '\n', config_content)
            config_content = re.sub(r'\s*auth_basic_user_file\s+[^;]+;\n', '', config_content)

            # Write updated config (tested + rolled back by NginxService)
            result = NginxService.write_vhost(site_name, config_content)
            if not result['success']:
                return result

            return {'success': True, 'message': 'Basic Auth disabled'}

        except Exception as e:
            return {'success': False, 'error': str(e)}

    @classmethod
    def slugify(cls, text: str) -> str:
        """Convert text to a URL-safe slug.

        Args:
            text: Input string (e.g., branch name)

        Returns:
            Lowercase slug with only alphanumeric chars and hyphens
        """
        return _slugify(text)

    @classmethod
    def _reload_nginx(cls) -> Dict:
        """Test and reload Nginx — NginxService owns both (plan 75 §G4).

        Kept as a thin alias so callers outside this module keep working; the
        private re-implementation it replaced hardcoded ``sudo`` and called
        ``nginx`` by bare name, which the panel's sbin-less unit PATH cannot
        resolve.
        """
        return NginxService.reload()
