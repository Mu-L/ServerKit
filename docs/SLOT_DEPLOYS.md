# Slot deploys

A slot deploy never replaces a working release with a broken one. Each app that
opts in has two slots, A and B. A deploy boots the new release in the idle slot
while the live one keeps serving, checks that it answers, and only then points
nginx at it. The previous release stays ready for an instant switch back.

This is the same idea the panel uses to update itself (`/opt/serverkit-a` ↔
`/opt/serverkit-b`). There is still exactly one live copy of each app. Slot
deploys add no load balancer, no replicas and no traffic splitting.

Turn it on under **Service → Settings → Health & Rollout**. The Overview tab
shows which slot is live, which is on standby, and a **Switch back** button.

## What a deploy does

1. **Preflight and build.** The image is built, or the compose project is
   validated, built and pulled, while the live slot serves. A broken build never
   touches the site.
2. **Snapshot.** Every database the app owns is dumped. A failed snapshot is a
   warning. It never blocks the deploy.
3. **Release.** The release command, if there is one, runs once in a throwaway
   container of the new image (see below). If it fails, the deploy stops and
   the live release is untouched.
4. **Boot.** The new release starts in the idle slot on its own loopback port.
5. **Health gate.** The release must answer its health check path with a 2xx or
   3xx three times in a row. A container with a Docker `HEALTHCHECK` must also
   report healthy. If the release never passes, it is removed, the site keeps
   serving the old release, and admins get a notification.
6. **Switch.** nginx is pointed at the new slot's port. `nginx -t` runs first,
   the reload is graceful, and a config that fails the test is rolled back.
7. **Watch.** The release is probed directly and through nginx for the watch
   window (60 s by default). If it stops answering, traffic switches back on
   its own.
8. **Standby.** The old slot stays running for 10 minutes by default, so a
   switch back takes seconds. After that it is stopped, not removed. A stopped
   standby uses disk, not memory, and a switch back starts it again first.

Each deploy's image is tagged with its deployment id, so rolling back to a
release older than the standby redeploys the image that actually ran. The
newest three deployments' images are kept (`keep_images`), and so is anything a
slot still uses.

## Which apps qualify

The Settings page shows the reasons when an app does not qualify. The check runs
again on every deploy. If an app stops qualifying (for example, its last domain
is removed), the deploy fails instead of falling back to an in-place deploy.

- **Single-container apps** (Dockerfile, build pack, image) qualify.
- **Compose apps with only stateless services** qualify. Each slot is its own
  compose project, `<name>-a` / `<name>-b`.
- **Compose apps with a database, cache or queue** qualify after a one-time
  split. The stateful services move into a shared `<name>-data` project that
  both slots reach by the same service names, on the same volumes. The split
  stops the app once. Settings shows the exact compose files before you confirm.
- **Not covered:** PHP and static sites (nginx serves the files), systemd
  Python apps, apps on remote servers, and apps not published on a domain or
  private URL.

When the web service has volumes, both releases mount them for a few seconds
around each switch. Confirm in Settings that the app handles this. SQLite and
similar single-writer stores do not.

## The release command

The command comes from, in order:

1. the app's **Release command** setting,
2. the `release:` line of its `Procfile`,
3. `release:` (or `deploy.release:`) in its `serverkit.yaml`.

It runs before the switch, against the database the live release is still
using. It does not run when you roll back.

## Schema changes: expand, deploy, contract

Both releases use the same database around the switch, and the release command
migrates it while the old release still serves. Adding things is safe. Removing
or renaming things is not. Keep every schema change backward-compatible for one
deploy:

1. **Expand:** add the new column or table. Deploy.
2. Deploy the code that uses it.
3. **Contract:** remove the old column in a later deploy.

When a change cannot follow this rule, turn on **Stop the live release before
the release command**. The site is down from the release command until the new
release passes its health check. If anything fails, the old release starts
again. This is an announced outage, not a hidden one.

## Rolling back

- **Switch back** on the Overview tab makes the standby live again. While it is
  warm this takes seconds. When it is stopped, ServerKit starts it, runs the
  health gate, then switches.
- Rolling back further, from the deployment history, is a normal slot deploy of
  that deployment's image.

Rolling code back never rolls the database back. If the release you left ran
its release command, the slot card offers **Restore the database** as a
separate step. It restores the snapshot taken before that release, and
everything written since is lost.
