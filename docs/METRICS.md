# ServerKit measurements

The README tables are a dated snapshot, generated from the same JSON files for
English, Spanish, Portuguese and Chinese. They separate source inventory from
build sizes and runtime measurements. A count of collected tests is not a count
of passing tests, and compressed asset bytes are not a page-load benchmark.

## Recorded snapshot

- [Source inventory](measurements/repository.json): measured on September 5,
  2026 with Python 3.11.7 on Windows. The clean collection count was also checked
  on Linux. The base source revision, whether measured source has uncommitted
  changes, and a hash of that measured source are recorded. The hash describes
  the working files, so a pre-commit snapshot is not attributed to an unchanged
  historical revision.
- [Production build inventory](measurements/frontend-build.json): Node version,
  per-asset byte/gzip sizes and a content hash are recorded. Built from an
  isolated copy of tracked/new frontend source after the request/form cleanup,
  excluding ignored files and reusing the installed dependencies, with
  `npm run build`. Its code hash matched the ordinary working-checkout build.
- Runtime memory and container image bytes are **unmeasured in this snapshot**.
  The previous ~180 MB RAM and 501 MB image figures had no reproducible
  environment or image digest, so they were removed instead of repeated.

### What each number includes

| Measurement | Definition |
| --- | --- |
| Core route declarations | Route decorators in Git-tracked Python under `backend/app/api/`. A function with two route decorators counts twice. This is source inventory, not the set of enabled runtime endpoints. |
| Core blueprint declarations | `Blueprint(...)` declarations in the same files. Extension blueprints and conditionally loaded routes are outside this count. |
| Explicit method/route pairs | Literal route methods, excluding automatic HEAD/OPTIONS. The JSON separately reports declarations whose methods cannot be resolved statically. |
| App templates | Git-tracked YAML files directly inside `backend/templates/`; nested database-extension templates are excluded. |
| Backend tests | Pytest collection from `backend/tests` using the existing clean-collection instrument; ignored installed extension copies do not inflate the count. Skipped/opt-in tests can still be collected. |
| HTML-linked code | JS/MJS/CSS referenced by script, stylesheet or modulepreload tags in production `index.html`. Later runtime requests, fonts and images are excluded. |
| All built code | Every JS/MJS/CSS file in the production output, including lazy chunks, translations, extension code and public vendor shims. HTML, fonts, images, source maps and other assets are excluded. |
| Gzip bytes | Sum of each file compressed independently with gzip level 9. MB means 1,000,000 bytes. These are reproducible compression estimates; actual transfer depends on server compression and browser caching. |

The old “1.75 MB web UI” number had no precise asset scope. The new HTML-linked
and all-code totals must not be interpreted as a measured speedup over that
old figure. Non-English locale chunks are already loaded on demand; their
build-size warnings alone do not show that every visitor downloads them.

## Refresh the source and build measurements

Use the backend Python environment with its dependencies installed. From the
repository root:

```bash
python scripts/measure-repository.py --collect-tests --output docs/measurements/repository.json
cd frontend
npm run build
npm run measure:build -- --output ../docs/measurements/frontend-build.json
cd ..
python scripts/update-readme-measurements.py --write
python scripts/update-readme-measurements.py
```

On Windows, `backend\venv\Scripts\python.exe` can replace `python`. Review the
snapshot and README diffs together. The final command checks agreement between
all translated tables and the snapshots; it does not pretend an old build
snapshot is a fresh measurement. Rebuild when publishing new size claims.

## Measure API latency and database work

The panel already records API timings through its analytics middleware. For a
bounded investigation, enable `SERVERKIT_PROFILE_REQUESTS=true` on an authorized
local or staging panel and restart it. This adds a `Server-Timing` response
header containing application elapsed time, SQL elapsed time and statement
count. It records neither SQL text nor parameters and creates no new database
table. The profiler is off by default and installs no SQL listeners when off.

Use the real HTTP sampler against the running panel:

```bash
python scripts/profile-api.py --base-url http://127.0.0.1:47927 --path /api/v1/system/health --samples 20 --warmup 2 --output .reviews/api-profile.json
```

For protected endpoints, supply a short-lived JWT through
`SERVERKIT_PROFILE_TOKEN`, or `SERVERKIT_PROFILE_API_KEY` only for routes that
already support API keys. Do not supply both. The sampler makes GET requests,
refuses redirects, and does not include bodies, credentials or query values in
its report. HTTP/transport failures are recorded and make the command fail.
Missing profiler headers produce `null` fields, never invented zero costs.

Reports contain sample count, response sizes/statuses, min/p50/p95/max latency,
and optional application/SQL distributions. Warmup is excluded from the latency
summary, but its requests and failures remain visible. Turn profiling off when
the investigation is finished.

Record panel revision, OS/architecture, Python version, database engine,
server/resource counts, cache state, concurrency and whether the panel is idle
or loaded. Compare the same dataset and environment before and after a change.
The sampler is sequential and does not establish throughput under concurrency.
Profiler SQL counts cover statements on the HTTP request thread through response
construction; background jobs and streamed body iteration are not included.

## Measure memory, images and browser loading separately

### Passive Linux runtime sampler

`scripts/measure-runtime.py` reads Linux **cgroup v2** counters for explicitly
selected systemd units or Docker containers. It uses Python's standard library,
does not restart services or generate HTTP traffic, and saves timestamped raw
samples plus median/p95/sampled-maximum summaries. Run it on the host, outside
the services being measured. It rejects overlapping scopes and reports missing
counters or replaced cgroups instead of inventing zero usage.

For an installed host panel, first verify the unit actually runs the backend
(not a oneshot wrapper that starts Docker). Record the revision actually running:

```bash
python3 scripts/measure-runtime.py --unit serverkit.service --unit nginx.service \
  --scenario idle-dashboard-closed --revision "$RUNNING_SERVERKIT_REVISION" \
  --notes 'Record DB engine, enabled extensions, app count, warmup and other host activity here' \
  --duration 600 --interval 5 --output runtime-idle.json
```

For the panel container, use `--container serverkit` instead of `--unit`.
Repeat `--unit` / `--container` to include separate supporting services (for
example Docker, containerd, or an external panel database); results remain per
target. Newly created application/build containers are **not automatically
included**. The JSON output is created exclusively so an earlier baseline cannot
be accidentally overwritten. Ctrl+C saves a partial report with exit code 130;
failed samples return exit code 1. Optional unavailable counters stay null.

- Memory is `memory.current`, including descendants, charged file cache and
  kernel memory. It is **not process RSS or Docker CLI's cache-adjusted number**.
  Raw `memory.stat` is retained; its fields overlap, so do not sum them all.
- CPU is the change in `cpu.stat` usage divided by actual elapsed time; 100%
  means one logical CPU, and values can exceed 100% on multicore systems.
- Swap, task counts, cumulative per-device I/O and memory-event counters are
  retained where available. Device counters can overlap with stacked storage;
  inspect deltas per device rather than summing them blindly. Memory events are
  lifetime counters; compare the first and last samples for events in the run.
- Peaks are **sampled**, not exact instantaneous high-water marks. Host available
  memory provides context but includes unrelated workloads. Record machine
  specs, cgroup limits and background activity alongside any published result.

Definitions follow the [Linux cgroup-v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html).
Start with a 10-minute quiet baseline after startup settles, then repeat with the
dashboard open and a fixed set of apps. On a disposable test server, separately
observe a deployment and backup while also measuring application response times.
Use the same hardware, limits, versions, data and workload before/after a fix.
This sampler measures resource usage, not application latency or throughput.

### Automatic observations in full distro installs

The VM harness and Test Sandbox **full** mode invoke
`scripts/test/measure-installed-runtime.py` after the health check, before other
API tests. It settles for 60 seconds, then observes for 120 seconds at 5-second
intervals. The panel and active nginx/Docker/containerd units are recorded
separately. Each run saves a runtime artifact and an explicit measured,
unavailable, failed or interrupted status; install success does not imply that
measurement succeeded. No arbitrary RAM/CPU pass threshold is enforced yet.

VM results include `runtime.json` and a table in the HTML report. Test Sandbox
keeps `<distro>.runtime.json` next to its log, includes compact measurement data
in the existing results API, and prints the summary into the log. The VM overlay
archive SHA-256 identifies uncommitted source; full container installs identify
the installed checkout/release (which can differ from the local installer).

Quick/provision distro probes do not run ServerKit, so they cannot produce a
panel runtime baseline. Full systemd containers share the host kernel; parallel
installs and startup jobs can affect these short observations. Use an isolated
VM/VPS and longer repeated runs for performance claims. Cgroup-v1 environments
are explicitly unavailable rather than silently measured with different rules.

### Other measurement boundaries

- **Memory:** record the exact process/container, workload, uptime and sampling
  interval. Process RSS and Docker memory accounting are different measurements.
  Include workers and enabled services; do not describe one idle sample as a
  universal minimum or capacity guarantee.
- **Image size:** record image digest, architecture, build arguments and whether
  bytes describe the local uncompressed image or registry transfer. A cached
  image from a different commit is not a measurement of the current source.
- **Browser loading:** record cold/warm cache, device, connection, route, locale
  and extension set. Use a production build and inspect actual requests and
  navigation timings; source-file counts and total dist size cannot substitute
  for this. Large assets should be optimized when that trace shows they affect
  a relevant user flow.

No production latency, memory, capacity or percentage-speedup claim is made by
the source/build snapshot. Existing fleet tests enforce one metrics query per
reader at multiple fleet sizes; the opt-in profiler makes that kind of query
budget observable on a running panel too.
