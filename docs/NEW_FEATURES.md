# New Features — Endpoint & Page Reference

Reference for the current UI, Docker, WAF, and TLS surfaces on `dev`. All endpoints
are under `/api/v1`. "admin" requires an admin user; "edit" uses `_can_edit_app`
(owner, admin, or an editor grant); "auth" accepts any valid JWT. Migration
`093_container_scale_policies` versions the scaling-policy table.

---

## 1. Frontend surfaces

| Surface | Where | Notes |
|---|---|---|
| **Layout switcher** | User menu (sidebar footer) → "Layout": Sidebar / Compact / Top bar | No route; persisted to `localStorage['layout']` (`sidebar`\|`rail`\|`topbar`), applied as `data-layout` on `<html>`. Desktop only. |
| **GPU Monitor** | Route `/gpu` · sidebar nav "GPU Monitor" (System) | Per-GPU cards + compute-process table; empty state when no GPU. |
| **Dynamic DNS** | Route `/dynamic-dns` · sidebar nav "Dynamic DNS" (Infrastructure) | Host CRUD; shows the one-time token + ready update URL on create. |
| **Container Ops tab** | App detail page (Docker apps) | Image-update check/apply, auto-sleep. |
| **WAF tab** | App detail page (nginx-served apps: Docker + Python) | Install banner, mode/paranoia/anomaly, disabled-rule editor, apply, events. |

---

## 2. Endpoints by feature

### Dynamic DNS — `models: ddns_hosts` · `services/ddns_service.py`
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/ddns/hosts` | auth | List hosts (token masked). |
| POST | `/ddns/hosts` | admin | Body `{zone_id, record_name, label?, enabled?}` → returns host **incl. one-time `token`**. |
| DELETE | `/ddns/hosts/<id>` | admin | |
| POST | `/ddns/hosts/<id>/regenerate-token` | admin | Returns host with a fresh `token`. |
| GET/POST | `/ddns/update?token=&ip=` | **public (token)** | Updates the host's A/AAAA record. `ip` from `?ip=` or request source. Wrong token → 401, bad IP → 400. Reuses `DNSZoneService`, so a configured provider (e.g. Cloudflare) syncs automatically. |

### Image-digest update — `models: image_update_checks` · `services/image_update_service.py`
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/image-updates/applications/<id>/check` | admin | Compares local RepoDigest vs registry digest (`docker buildx imagetools inspect`). |
| GET | `/image-updates/applications/<id>` | auth | Latest check (or `null`). |
| POST | `/apps/<id>/image-update/apply` | edit | Pull + recreate (compose apps; `compose_pull`+`compose_up`, local/remote). Guarded to compose. |

App badge: `app.image_update = {status, update_available, checked_at}`.

### Container auto-sleep — `models: container_sleep_policies` · `services/container_sleep_service.py`
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/apps/<id>/sleep-policy` | auth | |
| PUT | `/apps/<id>/sleep-policy` | edit | Body `{enabled?, idle_timeout_minutes?}`. |
| POST | `/apps/<id>/sleep` | edit | Stop the app, mark asleep. |
| POST | `/apps/<id>/wake` | edit | Start the app, record activity. |
| POST | `/apps/sweep-idle` | admin | Sleep all enabled apps idle past their timeout. **Cron-drivable.** |

App badge: `app.sleep = {enabled, asleep, idle_timeout_minutes}`.
Idle is measured from `last_activity_at` (bumped on wake / `record_activity`); a
no-activity-baseline policy is never slept blind.

### GPU monitoring — `services/gpu_service.py` (no model)
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/gpu/` | auth | `{available, gpus:[{index,name,utilization_gpu,memory_used,memory_total,memory_percent,temperature,power_draw,power_limit,fan_speed,driver_version}], processes:[{gpu_uuid,pid,process_name,used_memory,container}]}`. Shells `nvidia-smi`; container resolved from `/proc/<pid>/cgroup`. |

### WAF (ModSecurity v3 + OWASP CRS) — `models: waf_policies` · `services/waf_service.py`
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/waf/applications/<id>/policy` | auth | |
| PUT | `/waf/applications/<id>/policy` | admin | Body `{mode, paranoia_level, anomaly_threshold, disabled_rule_ids}`; saves **and** best-effort applies. `mode` ∈ off\|detect\|block. |
| POST | `/waf/applications/<id>/apply` | admin | Writes per-app rules + injects nginx include + reloads. May return `manual_include` when no vhost is found. |
| GET | `/waf/applications/<id>/events?limit=` | auth | Parsed ModSecurity audit-log events. |
| GET | `/waf/status` | auth | `{installed}`. |
| POST | `/waf/install` | admin | Install libmodsecurity + connector + OWASP CRS (distro-aware, best-effort). |

---

## 3. New models / tables

| Table | Model | Key columns |
|---|---|---|
| `ddns_hosts` | `DdnsHost` | zone_id, record_name, token (unique), last_ip, enabled |
| `image_update_checks` | `ImageUpdateCheck` | application_id, current_digest, latest_digest, update_available, status |
| `container_sleep_policies` | `ContainerSleepPolicy` | application_id (unique), enabled, idle_timeout_minutes, last_activity_at, asleep |
| `container_scale_policies` | `ContainerScalePolicy` | application_id (unique), enabled, service_name, min/max_replicas, cpu_high/low_percent, cooldown_seconds, current_replicas |
| `waf_policies` | `WafPolicy` | application_id (unique), mode, paranoia_level, anomaly_threshold, disabled_rule_ids |

`Application.to_dict()` gained lightweight `image_update` and `sleep` badges.

---

## 4. Config flags & environment

| Setting | Default | Purpose |
|---|---|---|
| `encrypt_backups` (BackupService config) | `false` | Opt-in client-side backup encryption (Fernet, reuses `SERVERKIT_ENCRYPTION_KEY`). Encrypts each artifact (`.enc`) before `_auto_upload`; restore decrypts transparently. **Key loss = unrecoverable backups.** |
| `SERVERKIT_WAF_DIR` (env) | `/etc/nginx/serverkit-conf.d/waf` | Where per-app WAF rules/includes are written. |
| `SERVERKIT_MODSEC_AUDIT_LOG` (env) | `/var/log/modsec_audit.log` | ModSecurity audit log parsed for the WAF events view. |
| `localStorage['layout']` (browser) | `sidebar` | Shell geometry: `sidebar` \| `rail` \| `topbar`. |

---

## 5. Operational notes

- **Cron the sweep**: `POST /apps/sweep-idle` (auto-sleep) is an admin endpoint meant
  to be hit periodically. Wiring it to a built-in scheduler is a follow-up; for now
  drive it from cron.
- **Public DDNS endpoint**: `/ddns/update` is the only unauthenticated route added
  (the per-host token is the credential). Serve it over HTTPS.
- **WAF integration is additive**: per-app rules go to `serverkit-conf.d/waf` and an
  include is injected into the app vhost behind a `# serverkit-waf` marker; existing
  nginx/site generation is untouched. Enforcement needs nginx built with the
  ModSecurity connector + libmodsecurity + OWASP CRS on the host.

---

## 6. SSL/TLS hardening & optional HTTPS (security follow-up — `02afdec … a09bf0f`)

HTTPS is **optional and best-effort** — the installer never blocks on SSL, and the
panel never forces it. Hardening is applied so that *when* TLS is served, it's
strong; it stays inert in HTTP-only mode.

**Install/update (`install.sh`, `scripts/update.sh`)**
- Best-effort cert: tries Let's Encrypt (webroot); on failure falls back to plain
  HTTP instead of failing the install. `SERVERKIT_SKIP_SSL=1` forces HTTP.
- Two panel vhosts: `serverkit.conf` (HTTPS + HTTP→HTTPS redirect) and
  `serverkit-insecure.conf` (HTTP-only). The choice persists to
  `/etc/serverkit/ssl-mode` and is preserved across updates.
- **Server-wide TLS floor**: `harden_global_tls()` rewrites/injects
  `ssl_protocols TLSv1.2 TLSv1.3` + AEAD-only `ssl_ciphers` in the system
  `nginx.conf` `http{}`, so the default server and any non-ServerKit vhost are
  covered too. Edited in place to avoid nginx's duplicate-directive error on
  Debian/Ubuntu. `update.sh` re-applies it to existing installs.
- Cloudflare-aware configs (Origin CA option + dashboard guidance) in
  `nginx/sites-available/*`.

**Per-app vhosts (`services/nginx_service.py` `SSL_BLOCK`)** — HSTS
(`includeSubDomains; preload`), CSP, `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, and `ssl_ecdh_curve`, in addition to the TLS 1.2/1.3 + AEAD set.

**Auto-CAA on certificate issuance** — `ssl_service.obtain_certificate` calls
`DNSProviderService.ensure_caa_record(domain)` (best-effort, never fails the cert).
It creates `CAA 0 issue "letsencrypt.org"` at the **zone apex** via whichever
connected provider manages the domain (Cloudflare / DigitalOcean / Route53), and
degrades to manual instructions otherwise. Cloudflare/DigitalOcean need CAA's
structured `data` object (not a flat string) — handled. The cert response gains a
`caa: {created, provider, zone, record, …}` field. The default DNS zone
`web-hosting` preset also seeds a CAA record.

**HSTS gating (panel)** — the Flask security middleware emits HSTS **only when the
deployment terminates real HTTPS**, resolved as `SSL_MODE` / `HSTS_ENABLED` in
`config.py`. Behind nginx/Cloudflare, Flask can't tell real TLS from a Cloudflare
Flexible edge via `X-Forwarded-Proto`, so it trusts the operator's recorded choice
rather than the request scheme. The nginx edge emits HSTS independently in its
secure server block.

| Setting | Where | Purpose |
|---|---|---|
| `SERVERKIT_SKIP_SSL=1` | env (installer) | Skip HTTPS entirely; run on plain HTTP. |
| `SERVERKIT_SSL_MODE` | env / `.env` | `secure`\|`insecure` — gates the panel's HSTS header. |
| `/etc/serverkit/ssl-mode` | file | Persisted SSL mode (read by installer, updater, and `config._resolve_ssl_mode`). |
| `/etc/serverkit/panel-domain` | file | Persisted panel domain so `update.sh` re-applies the cert path without the old (broken) `.env` scrape. |

Proving tests: `backend/tests/test_dns_caa.py`, `backend/tests/test_security_headers.py`.

---

## 7. Known follow-ups

- Migration import (cPanel/CyberPanel) — not started; needs real archive samples.
- Backups: additional remote targets (WebDAV/Azure/Dropbox/SFTP), streaming
  encryption for very large archives, and restore drills.
- Auto-sleep: traffic-based idle detection (feed `record_activity` from the nginx
  log) and request-triggered wake-on-demand; remote-server sleep.
- Periodic scheduler wiring for the sleep/scale sweeps.
- A human visual pass over all of this session's new UI in a running environment.
