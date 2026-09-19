import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
    MANAGED_CAPABILITY_MAP,
    managedCapabilityForPath,
    managedControlHeld,
    managedSidebarIds,
} from '../../components/managedCapabilities.js';
import { CORE_ROUTES } from '../routeManifest.js';

// The drift guard for the managed capability map (plan 25 §Risks): the mapping
// from capability to sidebar ids, routes and in-page controls lives in ONE
// table (components/managedCapabilities.js, re-exported from sidebarItems.js),
// and this test is what keeps it true as pages move. A failure here means the
// map points at something that no longer exists — fix the map, not this test,
// unless the page itself went away deliberately.
//
// sidebarItems.js itself cannot be imported under plain node (it pulls in JSX
// tab modules), so the sidebar ids and alwaysVisible flags are read from its
// source text: the SIDEBAR_ITEMS array is the section between its declaration
// and the ADVANCED_ITEM_IDS export, and each item is one `id: '...'` entry.

const sidebarItemsPath = fileURLToPath(new URL('../../components/sidebarItems.js', import.meta.url));
const sidebarSource = await readFile(sidebarItemsPath, 'utf8');
const itemsSection = sidebarSource.slice(
    sidebarSource.indexOf('export const SIDEBAR_ITEMS'),
    sidebarSource.indexOf('export const ADVANCED_ITEM_IDS'),
);

const sidebarItems = [];
for (const match of itemsSection.matchAll(/^\s+id: '([a-z0-9-]+)',$/gm)) {
    const start = match.index;
    const next = itemsSection.indexOf("\n        id: '", start + 1);
    const block = itemsSection.slice(start, next === -1 ? undefined : next);
    sidebarItems.push({ id: match[1], alwaysVisible: /alwaysVisible:\s*true/.test(block) });
}
const sidebarIds = new Set(sidebarItems.map((item) => item.id));

// Match a route manifest path against a concrete pathname: ':param' segments
// match one segment each.
function manifestMatches(pattern, pathname) {
    const wanted = pattern.split('/').filter(Boolean);
    const got = pathname.split('/').filter(Boolean);
    if (wanted.length !== got.length) return false;
    return wanted.every((segment, i) => segment.startsWith(':') || segment === got[i]);
}

// Route prefixes that are real but contributed by builtin extensions rather
// than the core manifest (the Cloud Servers tab comes from
// serverkit-cloud-provision).
const EXTENSION_ROUTES = ['/cloud'];

function routeExists(prefix) {
    if (EXTENSION_ROUTES.includes(prefix)) return true;
    return CORE_ROUTES.some((route) => manifestMatches(route.path, prefix));
}

test('sidebarItems.js was parsed into items', () => {
    assert.ok(sidebarIds.has('servers') && sidebarIds.has('dashboard'),
        `expected sidebar items, got: ${[...sidebarIds].join(', ')}`);
});

test('every mapped sidebar id exists and is hideable', () => {
    for (const [capability, entry] of Object.entries(MANAGED_CAPABILITY_MAP)) {
        for (const id of entry.sidebarIds) {
            const item = sidebarItems.find((candidate) => candidate.id === id);
            assert.ok(item, `${capability}: sidebar id ${id} is not in SIDEBAR_ITEMS`);
            assert.ok(!item.alwaysVisible, `${capability}: ${id} is alwaysVisible and cannot be hidden`);
        }
    }
});

test('every mapped route prefix is a real route', () => {
    for (const [capability, entry] of Object.entries(MANAGED_CAPABILITY_MAP)) {
        for (const prefix of entry.routePrefixes) {
            assert.ok(routeExists(prefix), `${capability}: no route matches ${prefix}`);
        }
    }
});

test('route prefix matching respects segment boundaries', () => {
    assert.equal(managedCapabilityForPath('/fleet', ['fleet']), 'fleet');
    assert.equal(managedCapabilityForPath('/servers/3', ['fleet']), 'fleet');
    assert.equal(managedCapabilityForPath('/fleet-proxy', ['provisioning']), null);
    // /fleet-proxy is its own fleet route, not a sub-path of /fleet.
    assert.equal(managedCapabilityForPath('/fleet-proxy', ['fleet']), 'fleet');
    assert.equal(managedCapabilityForPath('/monitoring', ['monitoring_alerts']), null);
    assert.equal(managedCapabilityForPath('/monitoring/rules', ['monitoring_alerts']), 'monitoring_alerts');
    assert.equal(managedCapabilityForPath('/backups', ['backups_schedule']), null,
        'the backups page stays; only its scheduling controls collapse');
});

test('helpers derive from the one table', () => {
    assert.deepEqual([...managedSidebarIds(['fleet'])], ['servers']);
    assert.deepEqual([...managedSidebarIds(['updates'])], []);
    assert.equal(managedControlHeld('templates-deploy', ['fleet']), true);
    assert.equal(managedControlHeld('templates-deploy', []), false);
    assert.equal(managedControlHeld('backup-scheduling', ['backups_schedule']), true);
    assert.equal(managedControlHeld('self-update', ['updates']), true);
});

test('the launch capability set matches the plan', () => {
    assert.deepEqual(Object.keys(MANAGED_CAPABILITY_MAP).sort(), [
        'backups_schedule', 'config_templates', 'firewall', 'fleet',
        'monitoring_alerts', 'provisioning', 'updates',
    ]);
});
