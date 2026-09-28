"""The manifest apply engine (plan 17, Phase 2).

resolve → plan → apply. Turns a normalized v1 manifest into an ordered action
plan and executes it through the existing imperative services, inside a
DeploymentJob for logging + before/after snapshots. Single-server for now;
Phase 5 adds fleet targeting.

`plan()` is pure enough to be a dry-run (no writes). `apply()` executes and is
idempotent — a second plan against a just-applied manifest is empty.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app import db
from app.models.application import Application
from app.models.application_manifest import (
    ApplicationManifest, STATUS_APPLIED, STATUS_ERROR,
)
from app.models.domain import Domain
from app.models.environment import Environment
from app.models.managed_database import ManagedDatabase
from app.models.project import Project


# manifest backup schedule -> cron
_SCHEDULE_CRON = {
    'hourly': '0 * * * *',
    'daily': '0 2 * * *',
    'weekly': '0 3 * * 0',
    'monthly': '0 4 1 * *',
}

# db engines the managed-database layer can actually record
_MANAGED_DB_ENGINES = {'postgresql', 'mysql'}
# manifest db types provisioned by installing the engine template of the same
# id (plan 86 §C3); consumers bind with fromService over the shared network
_ENGINE_TEMPLATE_TYPES = {'redis': 'redis'}

# ordering weight per step type (dbs before consumers; domains last)
_STEP_ORDER = {
    'provision_db': 0,
    'provision_engine': 0,
    'warn': 1,
    'create_app': 2,
    'update_app': 3,
    'open_port': 4,
    'set_env': 5,
    'set_env_ref': 5,
    'ensure_volume': 6,
    'bootstrap': 7,
    'upsert_backup_policy': 8,
    'attach_domain': 9,
}


class ManifestApplyService:

    # -- resolve (#7) -------------------------------------------------------

    @classmethod
    def resolve_service(cls, svc: Dict[str, Any]) -> Dict[str, Any]:
        """Normalized manifest service -> desired-state dict."""
        literal_env: Dict[str, Any] = {}
        env_refs: List[Dict[str, Any]] = []
        for var in svc.get('env_vars', []):
            if var['source'] == 'value':
                literal_env[var['key']] = var['value']
            else:
                env_refs.append(var)

        containers = svc.get('containers') or []
        ports = list(svc.get('ports') or [])
        disks = list(svc.get('disks') or [])
        bootstraps: List[Dict[str, Any]] = []
        if svc.get('bootstrap'):
            bootstraps.append({'service': svc['name'], **svc['bootstrap']})
        host_requirements = list(
            [svc['host_requirements']] if svc.get('host_requirements') else [])
        # unit resources (#8 `_effective_resources`): a unit is ONE Application,
        # so per-container publishes/bootstraps/host-requirements flatten up to
        # the unit. Container DISKS stay inside the generated compose (synthetic
        # per-container named volumes) to avoid mount-path collisions — so unit
        # disks are NOT folded into `disks` here; their backups route via the
        # docker volume name instead.
        unit_backups: List[Dict[str, Any]] = []
        for c in containers:
            ports.extend(c.get('ports') or [])
            if c.get('bootstrap'):
                bootstraps.append({'service': c['name'], **c['bootstrap']})
            if c.get('host_requirements'):
                host_requirements.append(c['host_requirements'])
            for var in c.get('env_vars') or []:
                if var['source'] == 'value':
                    literal_env.setdefault(var['key'], var['value'])
                else:
                    env_refs.append(var)
            for disk in c.get('disks') or []:
                if disk.get('backup') and disk.get('mount_path'):
                    unit_backups.append({
                        'container': c['name'],
                        'mount_path': disk['mount_path'],
                        'docker_volume': f'{svc["name"]}-{c["name"]}-'
                                         f'{disk.get("name") or cls._slug(disk["mount_path"])}',
                        'backup': disk['backup'],
                    })

        return {
            'name': svc['name'],
            'kind': svc['kind'],
            'app_type': svc.get('app_type'),
            'db_engine': svc.get('db_engine'),
            'engine_version': svc.get('engine_version'),
            'runtime': svc.get('runtime'),
            'port': svc.get('port'),
            'ports': ports,
            'bootstrap': svc.get('bootstrap'),
            'bootstraps': bootstraps,
            'image': svc.get('image'),
            'registry': svc.get('registry'),
            'host_requirements': host_requirements,
            'containers': containers,
            'unit_backups': unit_backups,
            'healthcheck_path': svc.get('healthcheck_path'),
            'build_command': svc.get('build_command'),
            'start_command': svc.get('start_command'),
            'dockerfile_path': svc.get('dockerfile_path'),
            'cpu': cls._stringify(svc.get('cpu')),
            'memory': cls._stringify(svc.get('memory')),
            'auto_deploy': svc.get('auto_deploy', False),
            'env': literal_env,
            'env_refs': env_refs,
            'disks': disks,
            'server': svc.get('server'),
        }

    # -- multi-container unit seams (#9) ------------------------------------

    @classmethod
    def unit_compose(cls, app) -> Optional[Dict[str, Any]]:
        """The generated compose project for a manifest-managed UNIT app, or None
        when the app is not a unit. Lets a deploy/UI render the unit verbatim."""
        resolved = cls.resolved_for_app(app)
        if not resolved or not resolved.get('containers'):
            return None
        from app.services.unit_compose_service import UnitComposeService
        return UnitComposeService.render(app.name, resolved['containers'])

    @classmethod
    def unit_container_names(cls, app) -> List[str]:
        """Container names of a unit app (``{app}-{container}``), or []."""
        resolved = cls.resolved_for_app(app)
        if not resolved or not resolved.get('containers'):
            return []
        return [f'{app.name}-{c["name"]}' for c in resolved['containers']]

    # -- drift comparison (#18) --------------------------------------------

    @classmethod
    def resolved_for_app(cls, app) -> Optional[Dict[str, Any]]:
        """The normalized+resolved manifest service that governs ``app``, or None
        when the app is not manifest-managed."""
        if not getattr(app, 'project_id', None):
            return None
        row = ApplicationManifest.query.filter_by(project_id=app.project_id).first()
        if not row:
            return None
        normalized = row.get_normalized()
        if not normalized:
            return None
        svc = next((s for s in normalized.get('services', [])
                    if s.get('name') == app.name and s.get('kind') == 'app'), None)
        if not svc:
            return None
        resolved = cls.resolve_service(svc)
        resolved['_domains'] = sorted(
            d['host'] for d in normalized.get('domains', [])
            if d.get('service') == app.name and d.get('host'))
        return resolved

    @classmethod
    def drift_pair(cls, app, resolved: Dict[str, Any]):
        """(expected, observed) dicts over ONLY the manifest-declared surface.

        The manifest is an overlay: extra live env vars/domains are not drift.
        We therefore project live state onto what the manifest declares, so the
        pair is equal iff every declared item is present and matches.
        """
        from app.models.env_variable import EnvironmentVariable
        expected: Dict[str, Any] = {}
        observed: Dict[str, Any] = {}

        if resolved.get('port') is not None:
            expected['port'] = resolved['port']
            observed['port'] = app.port
        if resolved.get('healthcheck_path'):
            expected['healthcheck_path'] = resolved['healthcheck_path']
            observed['healthcheck_path'] = app.healthcheck_path

        # Appliance tier (plan 35): track raw ports + BYO image.
        declared_ports = resolved.get('ports') or []
        if declared_ports:
            from app.services.app_port_service import AppPortService
            def _pkey(p):
                return (f'{p.get("host_port")}:{p.get("container_port")}'
                        f'/{p.get("protocol") or "tcp"}/{p.get("expose") or "public"}')
            expected['ports'] = sorted(_pkey(p) for p in declared_ports)
            observed['ports'] = sorted(_pkey(p) for p in AppPortService.get_ports(app))
        if resolved.get('image'):
            expected['image'] = resolved['image']
            observed['image'] = app.docker_image
        if resolved.get('dockerfile_path'):
            from app.services.build_service import BuildService
            build_cfg = BuildService.get_app_build_config(app.id) or {}
            expected['dockerfile_path'] = resolved['dockerfile_path']
            observed['dockerfile_path'] = build_cfg.get('dockerfile_path')

        declared_env = sorted(list(resolved.get('env', {}).keys())
                              + [r['key'] for r in resolved.get('env_refs', [])])
        if declared_env:
            live_keys = {ev.key for ev in
                         EnvironmentVariable.query.filter_by(application_id=app.id).all()}
            expected['env_keys'] = declared_env
            observed['env_keys'] = sorted(k for k in declared_env if k in live_keys)

        declared_vols = sorted(d['mount_path'] for d in resolved.get('disks', [])
                               if d.get('mount_path'))
        if declared_vols:
            live_mounts = {v.mount_path for v in (app.volumes or [])}
            expected['volumes'] = declared_vols
            observed['volumes'] = sorted(m for m in declared_vols if m in live_mounts)

        declared_domains = resolved.get('_domains', [])
        if declared_domains:
            live_hosts = {d.name for d in app.live_domains}
            expected['domains'] = declared_domains
            observed['domains'] = sorted(h for h in declared_domains if h in live_hosts)

        return expected, observed

    # -- plan (#8) ----------------------------------------------------------

    @classmethod
    def plan(cls, project: Project, normalized: Dict[str, Any],
             environment: Optional[Environment] = None) -> Dict[str, Any]:
        """Diff desired manifest vs live state -> ordered action plan."""
        env = environment or cls._default_environment(project)
        default_server = normalized.get('server')
        steps: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []

        services = normalized.get('services', [])
        resolved_all: List[Dict[str, Any]] = []
        for svc in services:
            resolved = cls.resolve_service(svc)
            resolved['server'] = resolved.get('server') or default_server
            resolved_all.append(resolved)
            if resolved['kind'] == 'database':
                steps.extend(cls._plan_db(resolved))
            else:
                steps.extend(cls._plan_app(project, env, resolved, issues))

        steps.extend(cls._plan_domains(project, normalized.get('domains', [])))

        # Appliance-tier blockers (plan 35): plan-time refusals distinct from
        # advisory `issues`. Apply refuses (nothing executed) while any exist.
        blockers, appliance_issues = cls._appliance_blockers(project, resolved_all)
        issues.extend(appliance_issues)

        steps.sort(key=lambda s: (_STEP_ORDER.get(s['type'], 99), s.get('service') or ''))
        return {
            'project_id': project.id,
            'environment_id': env.id if env else None,
            'steps': steps,
            'step_count': len(steps),
            'issues': issues,
            'blockers': blockers,
            'summary': cls._summarize_plan(steps),
        }

    @classmethod
    def _plan_db(cls, resolved: Dict[str, Any]) -> List[Dict[str, Any]]:
        engine = resolved['db_engine']
        name = resolved['name']
        if engine in _ENGINE_TEMPLATE_TYPES:
            if Application.query_active().filter_by(name=name).first() is not None:
                return []   # already installed; fromService refs bind to it
            return [{
                'type': 'provision_engine', 'service': name,
                'description': f'Install {engine} engine `{name}`',
                'payload': {'template_id': _ENGINE_TEMPLATE_TYPES[engine], 'name': name,
                            'version': resolved.get('engine_version')},
            }]
        if engine not in _MANAGED_DB_ENGINES:
            return [{
                'type': 'warn', 'service': name,
                'description': f'{engine or resolved.get("app_type")} databases are not '
                               f'managed by ServerKit yet — declared but not provisioned',
                'payload': {},
            }]
        existing = ManagedDatabase.query_active().filter_by(engine=engine, name=name).first()
        steps: List[Dict[str, Any]] = []
        if not existing:
            steps.append({
                'type': 'provision_db', 'service': name,
                'description': f'Provision {engine} database `{name}`',
                'payload': {'engine': engine, 'name': name,
                            'version': resolved.get('engine_version')},
            })
        # backup declaration on the db's single disk
        for disk in resolved.get('disks', []):
            backup = disk.get('backup')
            if backup and not cls._backup_policy_current(
                    'database', existing.id if existing else None, backup):
                steps.append(cls._db_backup_step(name, engine, backup, exists=bool(existing)))
        return steps

    @classmethod
    def _plan_app(cls, project: Project, env: Optional[Environment],
                  resolved: Dict[str, Any],
                  issues: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        issues = issues if issues is not None else []
        name = resolved['name']
        app = cls._find_app(project, name)

        if app is None:
            payload = {k: resolved[k] for k in
                       ('app_type', 'port', 'healthcheck_path', 'runtime',
                        'build_command', 'start_command', 'cpu', 'memory')}
            payload['name'] = name
            # BYO image (plan 35): stamp the declared image + private registry
            if resolved.get('image'):
                payload['image'] = resolved['image']
                if resolved.get('registry'):
                    reg = cls._resolve_registry(resolved['registry'])
                    payload['registry_id'] = reg.id if reg else None
            # Dockerfile build: carry the path + autoDeploy so create can wire
            # the git deployment and build config (monorepo: repo root = context)
            if resolved.get('dockerfile_path'):
                payload['dockerfile_path'] = resolved['dockerfile_path']
                payload['auto_deploy'] = resolved.get('auto_deploy', False)
            # fleet targeting (#21): route the app to a server when declared
            server_ref = resolved.get('server')
            if server_ref:
                server_id = cls._resolve_server_id(server_ref)
                if server_id:
                    payload['server_id'] = server_id
                else:
                    issues.append({'service': name, 'kind': 'unknown_server',
                                   'server': server_ref})
            target = ' @ ' + server_ref if server_ref else ''
            built = (f' — built from `{resolved["dockerfile_path"]}`'
                     if resolved.get('dockerfile_path') else '')
            steps.append({
                'type': 'create_app', 'service': name,
                'description': f'Create app `{name}` ({resolved["app_type"]}){target}{built}',
                'payload': payload,
            })
        else:
            changes = cls._app_field_changes(app, resolved)
            if changes:
                steps.append({
                    'type': 'update_app', 'service': name,
                    'description': f'Update app `{name}`: ' + ', '.join(changes.keys()),
                    'payload': {'app_id': app.id, 'changes': changes},
                })

        # typed L4 port publishes (appliance tier, plan 35)
        ports = resolved.get('ports') or []
        if ports:
            from app.services.app_port_service import AppPortService
            current = AppPortService.get_ports(app) if app else []
            if app is None or cls._ports_differ(current, ports):
                steps.append({
                    'type': 'open_port', 'service': name,
                    'description': f'Publish {len(ports)} port(s) on `{name}`',
                    'payload': {'app_id': app.id if app else None, 'ports': ports},
                })

        # env (literal values only; references bind in Phase 3)
        desired_env = resolved['env']
        if desired_env:
            live_env = cls._live_env(app) if app else {}
            missing = {k: v for k, v in desired_env.items() if str(live_env.get(k)) != str(v)}
            if missing or app is None:
                to_set = desired_env if app is None else missing
                if to_set:
                    steps.append({
                        'type': 'set_env', 'service': name,
                        'description': f'Set {len(to_set)} env var(s) on `{name}`',
                        'payload': {'app_id': app.id if app else None, 'env': to_set},
                    })

        # env references (fromSecret / fromService / generate) — Phase 3
        live_vars = cls._live_env_vars(app) if app else {}
        for ref in resolved.get('env_refs', []):
            key = ref['key']
            source = ref['source']
            if source == 'generate':
                desired = {'kind': 'secret', 'secret': cls._generated_secret_name(name, key)}
                live = live_vars.get(key)
                # a generated secret is created once; leave it in place afterwards
                if live is not None and (live.get_reference() or {}).get('kind') == 'secret':
                    continue
                steps.append(cls._set_env_ref_step(name, app, key, desired, generate=True))
            elif source == 'secret':
                desired = {'kind': 'secret', 'secret': ref['secret_name']}
                if not cls._secret_exists(ref['secret_name']):
                    issues.append({'service': name, 'key': key, 'kind': 'missing_secret',
                                   'secret': ref['secret_name']})
                if cls._ref_changed(live_vars.get(key), desired):
                    steps.append(cls._set_env_ref_step(name, app, key, desired))
            elif source == 'service':
                sr = ref['service_ref']
                desired = {'kind': 'service', 'service': sr['name'], 'property': sr['property']}
                if cls._ref_changed(live_vars.get(key), desired):
                    steps.append(cls._set_env_ref_step(name, app, key, desired))
            elif source == 'server':
                sv = ref['server_ref']
                desired = {'kind': 'server', 'property': sv['property']}
                if cls._ref_changed(live_vars.get(key), desired):
                    steps.append(cls._set_env_ref_step(name, app, key, desired))

        # disks
        for disk in resolved.get('disks', []):
            mount = disk.get('mount_path')
            if not mount:
                continue
            has_vol = bool(app) and any(v.mount_path == mount for v in (app.volumes or []))
            if not has_vol:
                steps.append({
                    'type': 'ensure_volume', 'service': name,
                    'description': f'Ensure volume `{disk.get("name") or mount}` on `{name}`',
                    'payload': {'app_id': app.id if app else None,
                                'name': disk.get('name') or cls._slug(mount),
                                'mount_path': mount, 'size': disk.get('size')},
                })
            backup = disk.get('backup')
            if backup and not cls._backup_policy_current(
                    'files', app.id if app else None, backup):
                steps.append(cls._files_backup_step(name, app, mount, backup))

        # unit disk backups (plan 35) — the bytes live in a synthetic per-container
        # docker volume, so the backup routes at that volume name.
        for ub in resolved.get('unit_backups') or []:
            if not cls._backup_policy_current('files', app.id if app else None, ub['backup']):
                steps.append(cls._unit_backup_step(name, app, ub))

        # first-boot bootstrap (appliance tier, plan 35) — once per app; a unit
        # runs each of its containers' bootstraps under the one bootstrap_done flag
        bootstraps = resolved.get('bootstraps') or []
        if bootstraps and (app is None or not getattr(app, 'bootstrap_done', False)):
            suffix = f' ({len(bootstraps)} container(s))' if len(bootstraps) > 1 else ''
            steps.append({
                'type': 'bootstrap', 'service': name,
                'description': f'Run first-boot bootstrap on `{name}`{suffix}',
                'payload': {'app_id': app.id if app else None, 'bootstraps': bootstraps},
            })

        return steps

    @classmethod
    def _plan_domains(cls, project: Project, domains: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        steps = []
        for dom in domains:
            host = dom.get('host')
            svc_name = dom.get('service')
            app = cls._find_app(project, svc_name) if svc_name else None
            # live_domains, not domains: a tombstoned Domain would make this look
            # attached and the apply would skip the vhost/DNS/cert work entirely.
            already = bool(app) and any(d.name == host for d in app.live_domains)
            if already:
                continue
            steps.append({
                'type': 'attach_domain', 'service': svc_name,
                'description': f'Attach domain `{host}`'
                               + (f' to `{svc_name}`' if svc_name else ''),
                'payload': {'host': host, 'service': svc_name, 'ssl': dom.get('ssl', 'auto'),
                            'app_id': app.id if app else None},
            })
        return steps

    @classmethod
    def _db_backup_step(cls, name, engine, backup, exists):
        return {
            'type': 'upsert_backup_policy', 'service': name,
            'description': f'Backup policy for db `{name}` ({backup["schedule"]})',
            'payload': {'target': 'database', 'engine': engine, 'db_name': name,
                        'schedule': backup['schedule'], 'retain': backup['retain']},
        }

    @classmethod
    def _files_backup_step(cls, name, app, mount, backup):
        return {
            'type': 'upsert_backup_policy', 'service': name,
            'description': f'Backup policy for disk `{mount}` on `{name}` ({backup["schedule"]})',
            'payload': {'target': 'files', 'app_id': app.id if app else None,
                        'mount_path': mount, 'volume_mount': mount,
                        'schedule': backup['schedule'], 'retain': backup['retain']},
        }

    @classmethod
    def _unit_backup_step(cls, name, app, ub):
        backup = ub['backup']
        return {
            'type': 'upsert_backup_policy', 'service': name,
            'description': f'Backup policy for unit disk `{ub["mount_path"]}` on '
                           f'`{name}` ({backup["schedule"]})',
            'payload': {'target': 'files', 'app_id': app.id if app else None,
                        'mount_path': ub['mount_path'],
                        'docker_volume': ub['docker_volume'],
                        'schedule': backup['schedule'], 'retain': backup['retain']},
        }

    # -- blockers engine (appliance tier, plan 35) --------------------------

    # An appliance FEATURE is anything the manifest can now express that a
    # target may be unable to provide. Later phases extend `_service_features`.
    @classmethod
    def _service_features(cls, resolved: Dict[str, Any]) -> List[str]:
        feats: List[str] = []
        if resolved.get('ports'):
            feats.append('raw port publishes')
        if resolved.get('host_requirements'):
            feats.append('host requirements')
        if resolved.get('bootstraps'):
            feats.append('a first-boot bootstrap')
        if resolved.get('containers'):
            feats.append('a multi-container unit')
        if resolved.get('dockerfile_path'):
            feats.append('a Dockerfile build')
        return feats

    @classmethod
    def _appliance_blockers(cls, project, resolved_all: List[Dict[str, Any]]):
        """Return (blockers, issues). Blockers refuse apply; issues are advisory.

        Message contract: "service X needs Y; target Z can't provide it — <fix>".
        """
        blockers: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []
        fw_state = None  # resolved lazily, once
        for resolved in resolved_all:
            if resolved.get('kind') != 'app':
                continue
            name = resolved['name']
            server_ref = resolved.get('server')

            # fromServer publicIp must resolve (plan 35) — independent of the
            # other appliance features; a service may bind only its own IP.
            needs_ip = any(
                r.get('source') == 'server'
                and (r.get('server_ref') or {}).get('property') == 'publicIp'
                for r in resolved.get('env_refs') or [])
            if needs_ip and not cls._server_public_ip(server_ref):
                where = f'server {server_ref}' if server_ref else 'the panel host'
                blockers.append({
                    'kind': 'fromserver_no_ip', 'service': name,
                    'message': f'service {name} binds its public IP via fromServer, but '
                               f'{where} has no recorded public IP — set it, then re-apply.'})

            # A Dockerfile build needs a repository to clone. Plan-time, like a
            # missing secret: the stored manifest's provenance, or a git
            # deployment already configured on a sibling app in the project.
            if resolved.get('dockerfile_path') and cls._project_source(project) is None:
                blockers.append({
                    'kind': 'dockerfile_no_source', 'service': name,
                    'message': f'service {name} builds from `{resolved["dockerfile_path"]}`, '
                               f'but the project has no source repository on record — import '
                               f'the project from its repository (or configure a git deployment '
                               f'on one of its apps), then re-apply.'})

            feats = cls._service_features(resolved)
            if not feats:
                continue
            need = ' and '.join(feats)

            # Remote target: appliance apply runs on the panel host only (plan 17's
            # remote-dispatch deferral). Say so instead of half-deploying.
            if server_ref:
                srv = cls._resolve_server_obj(server_ref)
                if srv is not None and getattr(srv, 'management_mode', None) == 'observed':
                    blockers.append({
                        'kind': 'observed_server', 'service': name, 'server': server_ref,
                        'message': f'service {name} needs {need}; target {server_ref} is an '
                                   f'observed (read-only) server — ServerKit will not mutate it. '
                                   f'Switch it to managed, or apply on the panel host.'})
                    continue
                blockers.append({
                    'kind': 'remote_target', 'service': name, 'server': server_ref,
                    'message': f'service {name} needs {need}; target {server_ref} is a remote '
                               f'server — remote appliance apply is not supported yet. Apply on '
                               f'the panel host, or provision {server_ref} manually.'})
                continue

            # Local panel host: port conflicts + firewall detection.
            public = [p for p in (resolved.get('ports') or [])
                      if (p.get('expose') or 'public') == 'public']
            for p in public:
                hp = p.get('host_port')
                if hp is not None and cls._port_bound(int(hp)):
                    blockers.append({
                        'kind': 'port_conflict', 'service': name, 'port': hp,
                        'message': f'service {name} needs public port {hp}/{p.get("protocol","tcp")}; '
                                   f'the panel host already has it bound — free the port or choose '
                                   f'another in the manifest.'})
            if public:
                if fw_state is None:
                    fw_state = cls._firewall_state()
                if fw_state == 'undetected':
                    blockers.append({
                        'kind': 'firewall_undetected', 'service': name,
                        'message': f'service {name} publishes public ports but the panel host '
                                   f'firewall state could not be determined — verify ufw/firewalld '
                                   f'is reachable, then re-apply.'})
                elif fw_state == 'none':
                    issues.append({
                        'service': name, 'kind': 'firewall_none',
                        'message': f'no manageable firewall detected — public ports for {name} '
                                   f'will be published but not firewall-managed.'})

            # BYO image (plan 35): a private registry must be known + credentialed.
            for ref in cls._image_registry_refs(resolved):
                reg_name = ref.get('registry')
                if not reg_name:
                    continue  # anonymous pull is fine
                reg = cls._resolve_registry(reg_name)
                if reg is None or not cls._registry_has_credential(reg):
                    blockers.append({
                        'kind': 'registry_credential', 'service': name, 'registry': reg_name,
                        'message': f'service {name} pulls `{ref["image"]}` from registry '
                                   f'`{reg_name}`, which is '
                                   + ('unknown' if reg is None else 'missing a stored credential')
                                   + f' — add the registry credential, then re-apply.'})

            # Host requirements (plan 35): listed in plain words, never silent.
            for hr in resolved.get('host_requirements') or []:
                for phrase in cls._hostreq_phrases(hr):
                    issues.append({'service': name, 'kind': 'host_requirement',
                                   'message': f'{name} requests {phrase}'})
                for mod in hr.get('kernel_modules') or []:
                    if not cls._kernel_module_present(mod):
                        issues.append({
                            'service': name, 'kind': 'kernel_module',
                            'message': f'{name} needs kernel module `{mod}` — not confirmed '
                                       f'loaded on the host (advisory; verify before relying on it).'})
        return blockers, issues

    # -- blocker seams (patched in tests) -----------------------------------

    @staticmethod
    def _port_bound(port: int) -> bool:
        """True when ``port`` is already bound on the panel host (0.0.0.0)."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(('0.0.0.0', int(port)))
            return False
        except OSError:
            return True
        except Exception:
            return False
        finally:
            sock.close()

    @staticmethod
    def _firewall_state() -> str:
        """'active' (a manageable firewall exists) | 'none' | 'undetected'."""
        try:
            from app.services.firewall_service import FirewallService
            status = FirewallService.get_status()
        except Exception:
            return 'undetected'
        if not isinstance(status, dict):
            return 'undetected'
        if status.get('active_firewall'):
            return 'active'
        fwd = status.get('firewalld') or {}
        ufw = status.get('ufw') or {}
        if fwd.get('installed') or ufw.get('installed'):
            return 'active'
        return 'none'

    @staticmethod
    def _resolve_server_obj(server_ref: Optional[str]):
        if not server_ref:
            return None
        try:
            from app.models.server import Server
        except Exception:
            return None
        return (Server.query.filter_by(id=server_ref).first()
                or Server.query.filter_by(name=server_ref).first())

    @staticmethod
    def _image_registry_refs(resolved: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Every (image, registry) pair a service pulls — top-level + containers."""
        refs: List[Dict[str, Any]] = []
        if resolved.get('image'):
            refs.append({'image': resolved['image'], 'registry': resolved.get('registry')})
        for c in resolved.get('containers') or []:
            if c.get('image'):
                refs.append({'image': c['image'], 'registry': c.get('registry')})
        return refs

    @staticmethod
    def _resolve_registry(name: Optional[str]):
        if not name:
            return None
        try:
            from app.models.container_registry import ContainerRegistry
        except Exception:
            return None
        return (ContainerRegistry.query.filter_by(id=name).first()
                if str(name).isdigit() else None) \
            or ContainerRegistry.query.filter_by(name=name).first()

    @classmethod
    def _server_public_ip(cls, server_ref: Optional[str]) -> Optional[str]:
        """Advertised public IP of the manifest target (panel host by default)."""
        if server_ref:
            srv = cls._resolve_server_obj(server_ref)
            return getattr(srv, 'ip_address', None) if srv else None
        try:
            from app.services.site_domain_service import SiteDomainService
            return SiteDomainService.server_ip()
        except Exception:
            return None

    @classmethod
    def _project_source(cls, project) -> Optional[Dict[str, str]]:
        """The repository a Dockerfile-built service clones: the stored
        manifest's provenance, else the git deployment of a sibling app in the
        project. None when neither exists (a plan-time blocker)."""
        row = ApplicationManifest.query.filter_by(project_id=project.id).first()
        if row is not None and row.source_repo:
            return {'repo_url': row.source_repo, 'branch': row.source_ref or 'main'}
        try:
            from app.services.git_service import GitService
            deploy_configs = GitService.get_config().get('apps', {})
        except Exception:
            return None
        for sibling in Application.query_active().filter_by(project_id=project.id).all():
            cfg = deploy_configs.get(str(sibling.id))
            if cfg and cfg.get('repo_url'):
                return {'repo_url': cfg['repo_url'], 'branch': cfg.get('branch') or 'main'}
        return None

    @staticmethod
    def _registry_has_credential(reg) -> bool:
        # ECR authenticates via a key-pair exchange, so no stored secret needed.
        if getattr(reg, 'provider', None) == 'ecr':
            return True
        return bool(getattr(reg, 'secret_encrypted', None))

    @staticmethod
    def _hostreq_phrases(hr: Dict[str, Any]) -> List[str]:
        phrases: List[str] = []
        if hr.get('privileged'):
            phrases.append('a PRIVILEGED container')
        for cap in hr.get('cap_add') or []:
            phrases.append(f'capability {cap}')
        for k, v in (hr.get('sysctls') or {}).items():
            phrases.append(f'sysctl {k}={v}')
        for dev in hr.get('devices') or []:
            phrases.append(f'device {dev}')
        return phrases

    @staticmethod
    def _kernel_module_present(module: str) -> bool:
        """Advisory /proc/modules check. Unverifiable (non-Linux / unreadable)
        returns False so the plan warns rather than silently passing."""
        try:
            with open('/proc/modules') as fh:
                loaded = {line.split()[0] for line in fh if line.strip()}
            return module in loaded
        except Exception:
            return False

    @staticmethod
    def _ports_differ(current: List[Dict[str, Any]], desired: List[Dict[str, Any]]) -> bool:
        def key(p):
            return (p.get('host_port'), p.get('container_port'),
                    (p.get('protocol') or 'tcp'), (p.get('expose') or 'public'))
        return sorted(map(key, current)) != sorted(map(key, desired))

    # -- apply (#9) ---------------------------------------------------------

    @classmethod
    def apply(cls, project: Project, normalized: Dict[str, Any],
              user_id: Optional[int] = None,
              environment: Optional[Environment] = None,
              manifest_row: Optional[ApplicationManifest] = None) -> Dict[str, Any]:
        """Execute the plan inside a DeploymentJob with before/after snapshots."""
        env = environment or cls._default_environment(project)
        plan = cls.plan(project, normalized, env)

        # Appliance-tier blockers (plan 35): refuse before executing anything.
        # No force flag — the operator must clear the cause and re-apply.
        if plan.get('blockers'):
            if manifest_row is not None:
                manifest_row.status = STATUS_ERROR
                manifest_row.last_error = plan['blockers'][0]['message']
                db.session.commit()
            return {
                'success': False, 'refused': True,
                'blockers': plan['blockers'], 'issues': plan.get('issues', []),
                'applied': 0, 'results': [], 'plan': plan,
            }

        job = cls._create_job(project, user_id, plan)

        cls._snapshot_existing(project, normalized, tag='before')

        results = []
        failed = False
        for idx, step in enumerate(plan['steps']):
            if failed:
                results.append({**cls._step_ref(step), 'status': 'skipped'})
                continue
            try:
                outcome = cls._execute_step(project, env, step, user_id)
                results.append({**cls._step_ref(step), 'status': 'ok', 'result': outcome})
                cls._log(job, idx, 'info', step['description'], outcome)
            except Exception as exc:  # stop the plan, report per-step
                failed = True
                results.append({**cls._step_ref(step), 'status': 'error', 'error': str(exc)})
                cls._log(job, idx, 'error', f'{step["description"]}: {exc}')

        cls._snapshot_existing(project, normalized, tag='after')

        cls._finish_job(job, failed, results)

        # Host requirements never apply silently (plan 35): audit every one.
        hr_issues = [i for i in plan.get('issues', []) if i.get('kind') == 'host_requirement']
        if hr_issues:
            try:
                from app.services.audit_service import AuditService
                AuditService.log('manifest.host_requirements', user_id=user_id,
                                 target_type='project', target_id=project.id,
                                 details={'requirements': [i['message'] for i in hr_issues]})
            except Exception:
                pass

        if manifest_row is not None:
            manifest_row.status = STATUS_ERROR if failed else STATUS_APPLIED
            if not failed:
                manifest_row.applied_at = datetime.utcnow()
                manifest_row.last_error = None
            else:
                manifest_row.last_error = next(
                    (r.get('error') for r in results if r.get('status') == 'error'), None)
            db.session.commit()

        return {
            'success': not failed,
            'job_id': job.id,
            'applied': sum(1 for r in results if r['status'] == 'ok'),
            'issues': plan.get('issues', []),
            'results': results,
            'plan': plan,
        }

    @classmethod
    def apply_stored(cls, project_id: int, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Load and re-apply the manifest stored for a project (drift repair)."""
        project = Project.query.get(project_id)
        if not project:
            return {'success': False, 'error': 'project not found'}
        row = ApplicationManifest.query.filter_by(project_id=project_id).first()
        normalized = row.get_normalized() if row else None
        if not normalized:
            return {'success': False, 'error': 'no stored manifest'}
        return cls.apply(project, normalized, user_id=user_id, manifest_row=row)

    @classmethod
    def _execute_step(cls, project, env, step, user_id) -> Dict[str, Any]:
        stype = step['type']
        # thread the step's service name into the payload so handlers can resolve
        # an app that was created earlier in the same apply (app_id was None at
        # plan time for brand-new services).
        payload = dict(step.get('payload', {}))
        payload.setdefault('service', step.get('service'))
        handler = getattr(cls, f'_do_{stype}', None)
        if handler is None:
            return {'noop': True}
        return handler(project, env, payload, user_id) or {}

    # -- step handlers ------------------------------------------------------

    @classmethod
    def _do_warn(cls, project, env, payload, user_id):
        return {'warned': True}

    @classmethod
    def _do_provision_db(cls, project, env, payload, user_id):
        from app.services.managed_database_service import ManagedDatabaseService
        managed = ManagedDatabaseService.record_provisioned(
            engine=payload['engine'], name=payload['name'],
            workspace_id=project.workspace_id,
        )
        return {'managed_database_id': managed.id}

    @classmethod
    def _do_provision_engine(cls, project, env, payload, user_id):
        """Install the engine template under the service's name, through the
        same job pipeline as the Databases page. The install is asynchronous;
        consumers' fromService refs resolve once it finishes."""
        from app.services import database_engine_service as engines
        from app.services.deployment_job_service import DeploymentJobService

        plan = engines.plan_install(template_id=payload['template_id'],
                                    version=payload.get('version'),
                                    instance_name=payload['name'])
        if 'error' in plan:
            raise RuntimeError(plan['error'])
        result = DeploymentJobService.install_template(
            template_id=payload['template_id'], app_name=plan['app_name'],
            user_variables=plan['variables'], user_id=user_id)
        if not result.get('success'):
            raise RuntimeError(result.get('error') or 'engine install failed')
        return {'job_id': result.get('job_id'), 'app_name': plan['app_name']}

    @classmethod
    def _do_create_app(cls, project, env, payload, user_id):
        import json
        overrides = {}
        if payload.get('build_command'):
            overrides['build_command'] = payload['build_command']
        if payload.get('start_command'):
            overrides['start_command'] = payload['start_command']
        app = Application(
            name=payload['name'],
            app_type=payload.get('app_type') or 'docker',
            status='stopped',
            port=payload.get('port'),
            healthcheck_path=payload.get('healthcheck_path'),
            cpu_limit=payload.get('cpu'),
            memory_limit=payload.get('memory'),
            docker_image=payload.get('image'),
            registry_id=payload.get('registry_id'),
            buildpack_overrides=json.dumps(overrides) if overrides else None,
            source='manifest',
            user_id=user_id,
            project_id=project.id,
            environment_id=env.id if env else None,
            server_id=payload.get('server_id'),
        )
        db.session.add(app)
        db.session.commit()
        if payload.get('dockerfile_path'):
            cls._wire_dockerfile_source(project, app, payload)
        return {'app_id': app.id, 'server_id': app.server_id}

    @classmethod
    def _wire_dockerfile_source(cls, project, app, payload):
        """Give a Dockerfile-built manifest service real source: clone the
        project's repository into the managed apps dir and write the same git
        deployment + build config the import wizard does, so the existing
        deploy pipeline builds `dockerfile_path` with the repo root as context."""
        import os
        from app import paths
        from app.services.git_service import GitService
        from app.services.build_service import BuildService

        source = cls._project_source(project)
        if not source:
            raise RuntimeError('no source repository on record for the project')

        base_dir = os.path.abspath(paths.APPS_DIR)
        app_path = os.path.abspath(os.path.join(base_dir, app.name))
        if app_path == base_dir or not app_path.startswith(base_dir + os.sep):
            raise RuntimeError('invalid application path')

        # idempotent: a re-apply after a partial failure reuses the checkout
        if not os.path.isdir(os.path.join(app_path, '.git')):
            cloned = GitService.clone_repository(app_path, source['repo_url'],
                                                 source['branch'])
            if not cloned.get('success'):
                raise RuntimeError(cloned.get('error') or 'clone failed')

        app.root_path = app_path
        db.session.commit()

        deploy = GitService.configure_deployment(
            app_id=app.id, app_path=app_path,
            repo_url=source['repo_url'], branch=source['branch'],
            auto_deploy=bool(payload.get('auto_deploy')))
        if not deploy.get('success'):
            raise RuntimeError(deploy.get('error') or 'failed to configure deployment')

        built = BuildService.configure_build(
            app_id=app.id, app_path=app_path,
            build_method='dockerfile',
            dockerfile_path=payload['dockerfile_path'])
        if not built.get('success'):
            raise RuntimeError(built.get('error') or 'failed to configure build')

    @classmethod
    def _do_update_app(cls, project, env, payload, user_id):
        import json
        app = Application.query_active().filter_by(id=payload['app_id']).first()
        if not app:
            raise RuntimeError('app not found')
        changes = payload['changes']
        if 'port' in changes:
            app.port = changes['port']
        if 'healthcheck_path' in changes:
            app.healthcheck_path = changes['healthcheck_path']
        if 'cpu' in changes:
            app.cpu_limit = changes['cpu']
        if 'memory' in changes:
            app.memory_limit = changes['memory']
        if 'image' in changes:
            app.docker_image = changes['image']
        if 'build_command' in changes or 'start_command' in changes:
            overrides = {}
            if app.buildpack_overrides:
                try:
                    overrides = json.loads(app.buildpack_overrides) or {}
                except Exception:
                    overrides = {}
            if 'build_command' in changes:
                overrides['build_command'] = changes['build_command']
            if 'start_command' in changes:
                overrides['start_command'] = changes['start_command']
            app.buildpack_overrides = json.dumps(overrides)
        if 'dockerfile_path' in changes:
            from app.services.build_service import BuildService
            cfg = BuildService.get_config()
            entry = cfg.get('apps', {}).get(str(app.id))
            if entry:
                entry['dockerfile_path'] = changes['dockerfile_path']
                entry['build_method'] = 'dockerfile'
                BuildService.save_config(cfg)
            else:
                # app existed without a build config (e.g. created pre-manifest)
                BuildService.configure_build(
                    app_id=app.id, app_path=app.root_path,
                    build_method='dockerfile',
                    dockerfile_path=changes['dockerfile_path'])
        db.session.commit()
        return {'app_id': app.id, 'changed': list(changes.keys())}

    @classmethod
    def _do_set_env(cls, project, env, payload, user_id):
        from app.services.env_service import EnvService
        app_id = payload.get('app_id') or cls._resolve_app_id(project, payload)
        count, errors = EnvService.bulk_set_env_vars(app_id, payload['env'], user_id)
        if errors:
            raise RuntimeError('; '.join(errors))
        return {'set': count}

    @classmethod
    def _do_set_env_ref(cls, project, env, payload, user_id):
        from app.services.env_service import EnvService
        app_id = payload.get('app_id') or cls._resolve_app_id(project, payload)
        ref = payload['ref']
        if payload.get('generate'):
            secret_name = ref['secret']
            cls._ensure_generated_secret(secret_name)
            ref = {'kind': 'secret', 'secret': secret_name}
        _var, _created, err = EnvService.set_env_reference(app_id, payload['key'], ref, user_id)
        if err:
            raise RuntimeError(err)
        return {'key': payload['key'], 'ref': ref}

    @classmethod
    def _do_ensure_volume(cls, project, env, payload, user_id):
        from app.services.volume_service import VolumeService
        app = Application.query_active().filter_by(
            id=payload.get('app_id') or cls._resolve_app_id(project, payload)).first()
        if not app:
            raise RuntimeError('app not found')
        if any(v.mount_path == payload['mount_path'] for v in (app.volumes or [])):
            return {'exists': True}
        vol = VolumeService.create(app, payload['name'], payload['mount_path'])
        # persist the manifest-declared size cap (plan 35); measured usage stays
        # on size_bytes.
        declared = payload.get('size')
        if declared is not None and vol is not None:
            vol.declared_size = str(declared)
            db.session.commit()
        return {'volume_id': getattr(vol, 'id', None), 'declared_size': declared}

    @classmethod
    def _do_bootstrap(cls, project, env, payload, user_id):
        from app.services.bootstrap_service import BootstrapService
        app = Application.query_active().filter_by(
            id=payload.get('app_id') or cls._resolve_app_id(project, payload)).first()
        if not app:
            raise RuntimeError('app not found')
        if getattr(app, 'bootstrap_done', False):
            return {'skipped': 'already bootstrapped'}
        bootstraps = payload.get('bootstraps')
        if bootstraps is None and payload.get('command'):
            bootstraps = [{'service': app.name, 'command': payload['command'],
                           'timeout_seconds': payload.get('timeout_seconds')}]
        outputs = []
        for b in bootstraps or []:
            result = BootstrapService.run_once(
                app, b['command'], timeout_seconds=b.get('timeout_seconds'),
                service=b.get('service'))
            if not result.get('success'):
                raise RuntimeError(result.get('error')
                                   or f'bootstrap failed for {b.get("service")}')
            outputs.append(result.get('output'))
        app.bootstrap_done = True
        db.session.commit()
        return {'bootstrapped': len(bootstraps or []), 'outputs': outputs}

    @classmethod
    def _do_open_port(cls, project, env, payload, user_id):
        from app.services.app_port_service import AppPortService
        app = Application.query_active().filter_by(
            id=payload.get('app_id') or cls._resolve_app_id(project, payload)).first()
        if not app:
            raise RuntimeError('app not found')
        ports = payload['ports']
        AppPortService.set_ports(app, ports)
        db.session.commit()
        firewall = AppPortService.open_firewall(ports)
        return {'ports': len(ports), 'firewall': firewall}

    @classmethod
    def _do_upsert_backup_policy(cls, project, env, payload, user_id):
        from app.services.backup_policy_service import BackupPolicyService
        schedule_cron = _SCHEDULE_CRON.get(payload['schedule'], '0 2 * * *')
        fields = {'enabled': True, 'schedule_cron': schedule_cron,
                  'retention_count': payload['retain']}
        if payload['target'] == 'database':
            from app.services.managed_database_service import ManagedDatabaseService
            managed = ManagedDatabase.query_active().filter_by(
                engine=payload['engine'], name=payload['db_name']).first()
            if not managed:
                raise RuntimeError('managed database not found for backup policy')
            ManagedDatabaseService.protect(managed, fields=fields)
            return {'policy': 'database', 'db': payload['db_name']}
        # files — for a manifest disk the real bytes live in the named docker
        # volume, so record the volume mount so backup resolves the live HOST
        # mountpoint (plan 35). `paths` stays as a graceful fallback.
        app_id = payload.get('app_id') or cls._resolve_app_id(project, payload)
        meta = {'paths': [payload['mount_path']], 'managed_by': 'manifest'}
        if payload.get('volume_mount'):
            meta['volume_mount'] = payload['volume_mount']
            meta['app_id'] = app_id
        if payload.get('docker_volume'):
            meta['docker_volume'] = payload['docker_volume']
        policy = BackupPolicyService.get_or_create_policy(
            target_type='files', target_id=app_id, target_subtype='pathlist',
            target_meta=meta)
        BackupPolicyService.update_policy(policy, fields)
        return {'policy': 'files', 'policy_id': policy.id}

    @classmethod
    def _do_attach_domain(cls, project, env, payload, user_id):
        from app.services.domain_attach_service import DomainAttachService
        app_id = payload.get('app_id') or (
            cls._resolve_app_id(project, payload) if payload.get('service') else None)
        if not app_id:
            return {'skipped': 'no target service'}
        app = Application.query_active().filter_by(id=app_id).first()
        if not app:
            raise RuntimeError('app not found for domain')
        result = DomainAttachService.attach(app, payload['host'], ssl=payload.get('ssl', 'auto'))
        return result

    # -- snapshots + job ----------------------------------------------------

    @classmethod
    def _snapshot_existing(cls, project, normalized, tag):
        from app.services.configuration_service import ConfigurationService
        for svc in normalized.get('services', []):
            if svc['kind'] != 'app':
                continue
            app = cls._find_app(project, svc['name'])
            if app is None:
                continue
            try:
                ConfigurationService.create_snapshot(app)
            except Exception:
                db.session.rollback()

    @classmethod
    def _create_job(cls, project, user_id, plan):
        from app.models.deployment_job import DeploymentJob
        job = DeploymentJob(
            id=str(uuid.uuid4()), kind='manifest.apply', status='running',
            requested_by=user_id, trigger='manual', started_at=datetime.utcnow(),
        )
        job.set_plan({'steps': plan['steps']})
        job.total_steps = plan['step_count']
        db.session.add(job)
        db.session.commit()
        return job

    @classmethod
    def _finish_job(cls, job, failed, results):
        job.status = 'failed' if failed else 'succeeded'
        job.completed_at = datetime.utcnow()
        job.current_step = sum(1 for r in results if r['status'] in ('ok', 'error'))
        job.set_result({'results': results})
        db.session.commit()

    @classmethod
    def _log(cls, job, idx, level, message, data=None):
        # Best-effort per-step log. Wrapped in a savepoint so a logging failure
        # (e.g. SQLite BigInteger PK) never poisons the apply transaction. The
        # authoritative per-step record also lives in job.result.
        from app.models.deployment_job import DeploymentJobLog
        import json
        try:
            with db.session.begin_nested():
                db.session.add(DeploymentJobLog(
                    job_id=job.id, step_index=idx, level=level, message=message,
                    data=json.dumps(data) if data else None))
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------

    @classmethod
    def _app_field_changes(cls, app: Application, resolved: Dict[str, Any]) -> Dict[str, Any]:
        import json
        changes = {}
        if resolved.get('port') is not None and app.port != resolved['port']:
            changes['port'] = resolved['port']
        if resolved.get('healthcheck_path') and app.healthcheck_path != resolved['healthcheck_path']:
            changes['healthcheck_path'] = resolved['healthcheck_path']
        if resolved.get('cpu') and app.cpu_limit != resolved['cpu']:
            changes['cpu'] = resolved['cpu']
        if resolved.get('memory') and app.memory_limit != resolved['memory']:
            changes['memory'] = resolved['memory']
        if resolved.get('image') and app.docker_image != resolved['image']:
            changes['image'] = resolved['image']
        if resolved.get('dockerfile_path'):
            from app.services.build_service import BuildService
            build_cfg = BuildService.get_app_build_config(app.id) or {}
            if build_cfg.get('dockerfile_path') != resolved['dockerfile_path']:
                changes['dockerfile_path'] = resolved['dockerfile_path']
        overrides = {}
        if app.buildpack_overrides:
            try:
                overrides = json.loads(app.buildpack_overrides) or {}
            except Exception:
                overrides = {}
        if resolved.get('build_command') and overrides.get('build_command') != resolved['build_command']:
            changes['build_command'] = resolved['build_command']
        if resolved.get('start_command') and overrides.get('start_command') != resolved['start_command']:
            changes['start_command'] = resolved['start_command']
        return changes

    @classmethod
    def _backup_policy_current(cls, target_type: str, target_id: Optional[int],
                               backup: Dict[str, Any]) -> bool:
        """True when an enabled policy already matches the declared schedule/retain."""
        if not target_id:
            return False
        from app.models.backup_policy import BackupPolicy
        pol = BackupPolicy.query.filter_by(
            target_type=target_type, target_id=target_id, enabled=True).first()
        if not pol:
            return False
        want_cron = _SCHEDULE_CRON.get(backup['schedule'], '0 2 * * *')
        return pol.schedule_cron == want_cron and pol.retention_count == backup['retain']

    @classmethod
    def _set_env_ref_step(cls, name, app, key, ref, generate=False):
        verb = 'Generate secret for' if generate else 'Bind reference'
        return {
            'type': 'set_env_ref', 'service': name,
            'description': f'{verb} `{key}` on `{name}`',
            'payload': {'app_id': app.id if app else None, 'key': key,
                        'ref': ref, 'generate': generate},
        }

    @staticmethod
    def _live_env_vars(app: Application) -> Dict[str, Any]:
        from app.models.env_variable import EnvironmentVariable
        return {ev.key: ev for ev in
                EnvironmentVariable.query.filter_by(application_id=app.id).all()}

    @staticmethod
    def _ref_changed(live_var, desired: Dict[str, Any]) -> bool:
        if live_var is None:
            return True
        current = live_var.get_reference()
        return current != desired

    @staticmethod
    def _secret_exists(name: str) -> bool:
        from app.services.env_reference_service import EnvReferenceResolver
        return EnvReferenceResolver.secret_exists(name)

    @staticmethod
    def _generated_secret_name(service: str, key: str) -> str:
        import re
        return re.sub(r'[^a-z0-9]+', '_', f'{service}_{key}'.lower()).strip('_')

    @staticmethod
    def _ensure_generated_secret(secret_name: str):
        """Get-or-create a random vault secret in the `manifest` vault."""
        import secrets as _secrets
        from app.models.secret_vault import SecretVault, Secret
        from app.utils.crypto import encrypt_secret
        existing = Secret.query.filter_by(name=secret_name).first()
        if existing:
            return existing
        vault = SecretVault.query.filter_by(slug='manifest').first()
        if not vault:
            vault = SecretVault(name='Manifest', slug='manifest',
                                description='Auto-generated manifest secrets')
            db.session.add(vault)
            db.session.flush()
        secret = Secret(vault_id=vault.id, name=secret_name,
                        encrypted_value=encrypt_secret(_secrets.token_urlsafe(32)),
                        description='Generated by a serverkit.yaml manifest')
        db.session.add(secret)
        db.session.flush()
        return secret

    @staticmethod
    def _resolve_server_id(server_ref: str) -> Optional[str]:
        """Resolve a fleet target name/id to a Server.id (#21). None if unknown."""
        if not server_ref:
            return None
        try:
            from app.models.server import Server
        except Exception:
            return None
        server = Server.query.filter_by(id=server_ref).first() \
            or Server.query.filter_by(name=server_ref).first()
        return server.id if server else None

    @staticmethod
    def _find_app(project: Project, name: Optional[str]) -> Optional[Application]:
        if not name:
            return None
        # Name-keyed, and every apply step routes through here: a tombstone
        # sharing the name would shadow the live app and the apply would attach
        # domains / create DNS / start containers against the deleted one.
        return Application.query_active().filter_by(project_id=project.id, name=name).first()

    @classmethod
    def _resolve_app_id(cls, project, payload):
        app = cls._find_app(project, payload.get('service'))
        if not app:
            raise RuntimeError(f'app `{payload.get("service")}` not found')
        return app.id

    @staticmethod
    def _live_env(app: Application) -> Dict[str, Any]:
        from app.services.env_service import EnvService
        try:
            return EnvService.get_env_dict(app.id)
        except Exception:
            return {}

    @staticmethod
    def _default_environment(project: Project) -> Optional[Environment]:
        env = Environment.query.filter_by(project_id=project.id, is_default=True).first()
        if env:
            return env
        return Environment.query.filter_by(project_id=project.id).first()

    @staticmethod
    def _summarize_plan(steps: List[Dict[str, Any]]) -> str:
        if not steps:
            return 'no changes — live state matches the manifest'
        counts: Dict[str, int] = {}
        for s in steps:
            counts[s['type']] = counts.get(s['type'], 0) + 1
        return ', '.join(f'{v} {k.replace("_", " ")}' for k, v in counts.items())

    @staticmethod
    def _step_ref(step):
        return {'type': step['type'], 'service': step.get('service'),
                'description': step['description']}

    @staticmethod
    def _stringify(value):
        return None if value is None else str(value)

    @staticmethod
    def _slug(text):
        import re
        return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-') or 'vol'
