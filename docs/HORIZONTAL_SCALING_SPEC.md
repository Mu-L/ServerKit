# ServerKit — Per-Site Edge & Horizontal Scaling Spec

*Implementation record and remaining design for WordPress horizontal scaling.*

> **Status (2026-09-28): replicas and load balancing are not pursued.**
> ServerKit runs one live copy per app. The per-site edge (vhosts, micro-cache,
> the timed access log) is shipped and stays. The generic Compose replica
> controls, the auto-scaler and the nginx upstream/load-balancer builder were
> removed: nginx only ever proxied to one port and the scaler was never
> scheduled, so they could not work. Safe deploys move to A/B slot deploys
> instead of replicas. The sections below are kept as the design record.

---

## 1. Why this doc

`docs/WORDPRESS_ROADMAP.md` originally deferred **#34 (horizontal scaling)** on
the missing per-site reverse-proxy layer. ServerKit has since shipped that layer.
`SiteDomainService` publishes local managed sites through host nginx, the base-domain
registry manages DNS and wildcard HTTPS state, and `ContainerScaleService` changes a
local Compose service's replica count manually or from CPU thresholds.

So the real work is three layers, in order:

1. **The keystone**: a per-site edge from a public domain through an nginx vhost to
   the WordPress container. ServerKit now ships this layer for local managed sites.
2. **Stateless WP** — shared media + shared object cache so replicas are interchangeable.
3. **Replicas + upstream LB** — multiple WP containers behind the keystone vhost.

This doc specs all three as numbered phases (`K0–K4` keystone, `H0–H4` scaling), each with
**Goal / Do / Reuse / Acceptance / Verifiable-here? / Risk**, plus a dependency graph and a
real-host verification checklist.

---

## 2. Current reality (verified against the code)

| Area | State | Evidence and boundary |
|---|---|---|
| Base domains, DNS mode, wildcard HTTPS | **Shipped** | `SiteBaseDomain`, `SiteBaseDomainService`, `SitesHttpsService`, and the Settings UI manage multiple bases and provider bindings. Certificate issuance still needs a real DNS provider and Linux host. |
| WordPress site edge | **Shipped for local sites** | WordPress provisioning calls its routing path, which records a primary `Domain`; `SiteDomainService.write_app_vhost` renders and enables a Docker proxy vhost. The site URL falls back to `localhost:<port>` when no base domain exists. |
| Per-site logs and protection | **Shipped for local WordPress sites** | Nginx templates write per-site access/error logs. WordPress publishing adds exact-match `wp-login.php` and `xmlrpc.php` limits backed by shared zones partitioned by site and client IP. Bandwidth and Fail2ban services consume the same access log. |
| Generic Compose scaling | **Shipped for local apps** | `ContainerScaleService` and the Container Ops UI support policy read/write, manual replica changes, one policy evaluation, and an admin sweep. Migration `093_container_scale_policies` versions the policy table. `current_replicas` tracks ServerKit commands rather than live Docker reconciliation. |
| Generic load-balancer rendering | **Shipped as a separate primitive** | `NginxAdvancedService.create_reverse_proxy` renders round-robin, least-connections, or IP-hash upstreams. The scaler does not update this config. |
| WordPress shared state | **Shipped for the supported single-host topology** | The WordPress template mounts the full `/var/www/html` tree from one Compose named volume, so replicas in that project share uploads, plugins, themes, and Cache Enabler files. Redis supplies the shared object cache when enabled. Multi-host replicas still need NFS/CIFS or object offload. |
| WordPress replica orchestration and failover | **Open** | The WordPress stack still publishes one container port. ServerKit does not create internal WordPress replicas, register them in the vhost, drain unhealthy replicas, or prove traffic survives a replica loss. |

Generic replica scaling improves capacity management. It does not provide WordPress high
availability by itself because the request path, persistent files, and health lifecycle remain
outside the scaling policy.

---

## 3. Dependency graph

```
            ┌─────────────────────────── Part 1: KEYSTONE (per-site edge) ───────────────────────────┐
            │  K0 public base-domain + wildcard DNS/TLS                                               │
            │        │                                                                                │
            │        ▼                                                                                │
            │  K1 per-site reverse-proxy vhost ──► K2 auto-TLS per site ──► K3 preview URL (#21)      │
            │        │                                   │                                            │
            │        ▼                                   ▼                                            │
            │  K4 per-site access log + limit_req  ──► unblocks #30-jail, restores #25-nginx          │
            └────────┬────────────────────────────────────────────────────────────────────────────┘
                     │  (also closes: #15-TLS, #8-backend SSL wiring, #3 already done)
                     ▼
            ┌─────────────────────────── Part 2: #34 HORIZONTAL SCALING ─────────────────────────────┐
            │  H0 stateless WP (shared media + shared object cache)                                   │
            │        ▼                                                                                │
            │  H1 replica orchestration (internal network; edge publishes, not the container)        │
            │        ▼                                                                                │
            │  H2 upstream LB wiring ──► H3 health/rolling ──► H4 autoscale signal (optional)         │
            └────────────────────────────────────────────────────────────────────────────────────────┘
```

K0 through K2 and K4 now exist for local managed sites. H1 and H2 remain the critical path
for WordPress: internal replicas first, then an upstream that tracks those replicas.

---

## 4. Part 1 — The keystone: per-site edge

### K0: Panel-wide public base domain + wildcard DNS/TLS  `[SHIPPED]`
- **Goal:** a configurable base domain (e.g. `apps.example.com`) with `*.apps.example.com` DNS and a wildcard cert, so *any* managed site can get a real public URL. This is the single setting whose absence makes every public-URL feature impossible today.
- **Shipped:** `SiteBaseDomain` stores multiple base domains, default selection, DNS mode, HTTPS state, and a DNS-provider binding. `SitesHttpsService` provisions wildcard DNS and certificates. Setup Health reports missing DNS and HTTPS prerequisites.
- **Reuse:** `advanced_ssl_service`, `ssl_service`, and encrypted DNS-provider connections.
- **Acceptance:** with a base domain configured, `dig *.base` resolves and a valid `*.base` cert exists.
- **Verification:** pytest covers registry behavior, publishing gaps, setup health, and setup reconciliation. DNS propagation and certificate issuance still require a real provider and host.
- **Risk:** registrar and DNS-provider behavior varies. A site falls back to `localhost:<port>` when the operator has not configured a base domain.

### K1: Per-site reverse-proxy vhost at create/attach  `[SHIPPED: local sites]`
- **Goal:** put an nginx vhost in front of each managed WP container so it answers on a real hostname instead of `localhost:PORT`.
- **Shipped:** local WordPress provisioning records a primary `Domain` and calls the shared site-vhost writer with the application's published port. Domain attachment uses the same `SiteDomainService` rendering path.
- **Reuse:** `NginxService.create_site`, `SiteDomainService.write_app_vhost`, and the WordPress URL-swap path.
- **Acceptance:** a freshly created site is reachable at `http://<hostname>` and `wp-admin` loads with correct URLs (no redirect loop, no mixed `localhost` links).
- **Verification:** `test_site_routing.py`, `test_dns_give_subdomain.py`, and the nginx rendering suites cover the database and config path. Live HTTP and redirect-loop checks still require a Linux host.
- **Risk:** the container still publishes one host port. H1 must change that topology without cutting off the existing vhost.

### K2: Auto-TLS per site  `[SHIPPED: wildcard base domains]`
- **Goal:** every site is HTTPS by default.
- **Shipped:** a base-domain row marked HTTPS-ready supplies the wildcard certificate to generated vhosts. HTTP redirects to HTTPS through the nginx SSL rendering path. Custom-domain certificate workflows remain separate from base-domain wildcard setup.
- **Reuse:** `NginxService.create_site`, `NginxService.add_ssl_to_site`, `SitesHttpsService`, and the SSL services.
- **Acceptance:** the site loads over HTTPS with a valid cert; HTTP redirects to HTTPS.
- **Verification:** unit tests cover certificate-path selection and vhost rendering. A real host must prove issuance, renewal, and the TLS handshake.
- **Risk:** custom-domain certs need DNS pointed first — keep a "pending DNS" state.

### K3: Preview URL before DNS (#21)  `[OPEN: exact design]`
- **Goal:** a working HTTPS preview link the instant a site exists, pre-DNS.
- **Current state:** a configured base domain gives new local sites an immediate managed hostname. ServerKit also ships PR preview environments for generic applications. It does not mint the separate `<site>.preview.<base>` WordPress URL described here.
- **Do:** add the separate namespace if product requirements still need it. The current managed hostname may satisfy the pre-custom-DNS workflow.
- **Reuse:** `environment_domain_service.generate_domain` (`:61`), K0 wildcard, `create_site`.
- **Acceptance:** every new site shows an immediately-working `https://….preview.base` link.
- **Verifiable here?** Hostname generation yes; live preview **real host only**.

### K4: Per-site access log + `limit_req` jail  `[SHIPPED: local WordPress sites]`
- **Goal:** a per-site nginx access log on the host + brute-force protection — the exact signals #25 and #30 had to defer because the container model exposes none.
- **Shipped:** generated vhosts write per-site access and error logs. WordPress vhosts add exact locations for `wp-login.php` and `xmlrpc.php`, return HTTP 429 after their burst allowance, and reference shared http-level zones keyed by `$server_name$binary_remote_addr`. The site key prevents one site's traffic from consuming another site's allowance. Bandwidth analytics and the WordPress Fail2ban jail use the canonical access-log path. Existing vhosts appear as drift until the operator runs the normal nginx repair, which regenerates them with the policy.
- **Reuse:** `NginxService.create_site`, `NginxService.site_access_log_path`, and `Fail2banJailService`.
- **Acceptance:** the generated WordPress vhost contains both endpoint limits; repeated login hits get throttled on a real host; per-site visits, status codes, and bandwidth come from the vhost log.
- **Verification:** `test_wordpress_ha_foundations.py` covers zone rendering, WordPress-only vhost injection, idempotent zone installation, publishing-path detection, and the generic-Docker boundary. The opt-in real-nginx suite parses the generated vhost. Live throttling and log emission still require a Linux host.

**After Part 1:** #15-TLS ✅, #21 ✅, #8-backend ✅, and #30-jail ✅. The vhost log supplies #25 traffic/status/bandwidth data; response-time and slow-page metrics still need a timed log format.

---

## 5. Part 2 — #34 horizontal scaling

> Built entirely on the keystone. The mental shift: in H1 the WP container stops publishing a
> host port; the **edge vhost (K1)** becomes the only public entry and load-balances across
> internal replicas.

### H0: Share WordPress application state  `[SHIPPED: single-host topology]`
- **Goal:** replicas must be interchangeable within the supported local Compose project.
- **Shipped:** the template mounts one `wordpress_html` named volume at `/var/www/html`. Every replica of the Compose `wordpress` service mounts that same volume, so uploads, plugins, themes, and Cache Enabler's disk cache are shared instead of replica-local. The database service is shared, and the optional Redis object cache already points every replica at the same `redis` service.
- **Boundary:** Docker named volumes are host-local. A future multi-host topology must replace this volume with NFS/CIFS or an object-storage media offload. That is not required for ServerKit's current local-only scaler.
- **Acceptance:** uploading media on replica A is immediately served by replica B.
- **Verification:** `test_wordpress_ha_foundations.py` pins the template's full-tree shared-volume mount. Cross-replica file visibility still needs the real-host H1 test because the current WordPress service cannot start multiple replicas yet.
- **Risk:** concurrent plugin, theme, and core updates must remain serialized once H1 ships. Multi-host scale needs a different storage backend.

### H1: Replica orchestration  `[OPEN for WordPress; generic scaler shipped]`
- **Goal:** run N WP containers for one site.
- **Current state:** Container Ops can run `docker compose up --scale <service>=N` for a local, scale-capable Compose application. Policies support minimum and maximum replicas, CPU thresholds, cooldown, one evaluation, and a cron-driven sweep. The service must avoid fixed host ports and `container_name`. The WordPress template does not meet that topology yet.
- **Do:** change the stack so the WP service is replica-able (`deploy.replicas` under swarm, or N enumerated services / `docker compose up --scale` on a compose host) on an **internal** network — **drop the `${HTTP_PORT}:80` host publish**; only the edge nginx reaches them. This is a **breaking change to the single-container model**, so it needs a per-site opt-in + a migration path (existing sites stay single-container until "Enable scaling").
- **Reuse:** the `wordpress.yaml` template + the `_ensure_*_in_stack` additive-compose pattern (#23 redis injection is the proven precedent).
- **Acceptance:** `docker ps` shows N WP replicas for the site, none publishing a host port.
- **Verifiable here?** Template rendering yes; actual replicas **real host only**.
- **Risk:** the publish→internal flip must be coordinated with K1 (edge must exist first or the site goes dark); MySQL stays single (not replicated here — that's a separate epic).

### H2: Upstream load-balancing wiring  `[OPEN integration]`
- **Goal:** the edge vhost balances across the H1 replicas.
- **Do:** upgrade the K1 vhost to an upstream-backed one: feed the replica endpoints to `create_reverse_proxy(upstreams=[…], method=…)`. Anonymous WP is stateless → round-robin; logged-in/cart needs affinity → `ip_hash` or cookie-sticky (expose via `get_load_balancing_methods`).
- **Reuse:** `nginx_advanced_service.create_reverse_proxy` (`upstream{}` at `:47`, `proxy_pass` at `:95`), `get_load_balancing_methods` (`:196`).
- **Acceptance:** requests distribute across replicas; killing one replica doesn't drop traffic.
- **Verifiable here?** Upstream-config rendering yes; live balancing/failover **real host only**.
- **Risk:** session affinity for WooCommerce carts; cache coherence across replicas (mitigated by H0 shared Redis).

### H3: Health checks + rolling deploys  `[OPEN integration]`
- **Goal:** safe scale up/down and zero-downtime deploys.
- **Do:** per-replica health probe; drain a replica out of the upstream before stop; a scale API on the `WordPressSite`. Tie deploys (#13/#29) into a rolling pattern: update one replica, health-check, proceed.
- **Reuse:** `environment_health_service` (#26 poller), `nginx_advanced_service` (rewrite upstream on scale), #29 safe-update health gate.
- **Acceptance:** scaling and deploys cause no failed requests.
- **Verifiable here?** Orchestration logic yes; zero-downtime claim **real host only**.

### H4: Autoscale signal (optional)  `[PARTIAL: generic CPU policy shipped]`
- **Goal:** scale on load.
- **Shipped:** generic local Compose applications can scale from average service CPU with minimum, maximum, and cooldown bounds. Raising the minimum now forces the next evaluation to restore that floor without waiting for high CPU.
- **Open:** connect an internal scheduler, health gates, and the WordPress upstream lifecycle. Per-site traffic metrics do not drive the policy.
- **Reuse:** #25 analytics, H3 scale API.
- **Acceptance:** sustained load adds a replica; quiet removes one.

---

## 6. Verification record

Automated coverage now checks these paths:

| Layer | Coverage |
|---|---|
| Frontend policy form | `autoScalePolicy.test.mjs` covers defaults, request normalization, the one-replica floor, and use of the API's applied replica count. |
| Frontend API contract | `containerOps.test.mjs` pins the four scaling paths, methods, and request bodies used by the UI. |
| Backend decision logic | `test_container_scale.py` covers threshold scale-up/down, bounds, cooldown, missing metrics, invalid policy input, and minimum-floor restoration. |
| API workflow | `test_policy_to_evaluation_to_manual_scale_workflow` drives GET policy, PUT policy, POST evaluation, POST manual scale, and a final persisted GET through Flask and SQLite. |
| Authorization and schema | The route authorization suites cover the mutating endpoints. The migration drift test includes migration `093_container_scale_policies`. |
| WordPress shared state | `test_wordpress_ha_foundations.py` pins the shared `/var/www/html` named-volume contract used by uploads and disk caches. |
| WordPress edge protection | `test_wordpress_ha_foundations.py` covers endpoint-specific limits, shared-zone isolation, idempotent installation, publishing-path detection, and the generic-Docker boundary. |

A Linux host with Docker and nginx must still prove the runtime behavior. Use a disposable
Compose application whose target service has no `container_name` or fixed host port:

1. Open the application's Settings tab, save a policy with minimum 2, maximum 3, and the target service name.
2. Call `POST /api/v1/apps/<id>/scale/evaluate`, then confirm `docker compose ps <service>` reports two running replicas.
3. Send load until CPU exceeds the high threshold, run another evaluation after cooldown, and confirm a third replica starts.
4. Send traffic through the public hostname and confirm each replica receives requests. This step will fail until an upstream-aware vhost is configured; the generic scaler does not create one.
5. Stop one replica while requests continue. Record any failed requests. ServerKit cannot claim failover until this test passes through a ServerKit-managed upstream.
6. For WordPress, upload media through one replica and request it through another. The template provides the shared volume, but ServerKit cannot claim WordPress HA until H1 makes this live test possible and it passes.

Steps 4 through 6 define the remaining HA acceptance gate. Passing the automated suite proves
control-plane behavior, not traffic continuity.

---

## 7. Remaining sequence

1. Add a WordPress-specific opt-in that changes the single published container into internal replicas.
2. Make the site vhost derive its upstream list from those replicas and update it during scale operations.
3. Add per-replica health checks and draining, then run the real-host continuity test in section 6.
4. Connect the existing CPU policy to the internal scheduler after health-based scaling works.

#34 reaches its acceptance condition after one site runs multiple replicas behind a managed
balancer, serves consistent media and cache state, and survives a replica loss without failed
requests.

---

## 8. Open decisions (resolve at execution)

- **Multi-host media:** keep the shipped named volume for local Compose; choose NFS/CIFS or object offload before adding a multi-host topology.
- **Port model:** the K1-publishes-a-port to H1-internal-only change needs a migration for existing sites and a clear opt-in.
- **MySQL:** stays single-instance in this spec. DB replication/clustering is a distinct epic, explicitly out of scope for #34.
- **Panel vs sites:** the *panel's* single-gevent-worker constraint (agent gateway, see `CLAUDE.md`/`ARCHITECTURE.md`) is unrelated — this LB is for the **managed sites**, not the panel. Don't conflate.
- **Plugin thesis:** media-offload (Option A) and any disk→Redis page-cache move may need plugins; weigh against the WP-CLI-over-plugin preference.

---

*Cross-references: `docs/WORDPRESS_ROADMAP.md` #34, #21, #15, #8, #25, #30, #22, #23. This spec is the
"split before starting" the roadmap asks for on every `XL`.*
