import { useMemo } from 'react';
import { useAuth } from '../contexts/useAuth.js';
import { SIDEBAR_ITEMS } from '../components/sidebarItems';
import { useWorkspace } from '../contexts/useWorkspace.js';
import { useManagedProfile } from '../contexts/useManagedProfile';

// Sidebar items that can never be hidden (Dashboard, Marketplace) — the palette
// mirrors the sidebar's `alwaysVisible` so a workspace nav map can't hide them.
const ALWAYS_VISIBLE = new Set(
    SIDEBAR_ITEMS.filter((i) => i.alwaysVisible).map((i) => i.id),
);

/**
 * The palette's authz gate (plan 41, Phase 1 #3). Mirrors exactly what the
 * sidebar shows so a member's palette can't surface admin surface:
 *   - `adminOnly` items require an admin.
 *   - Items tied to a sidebar `navId` respect the active workspace's per-role
 *     nav-permission map (same source as applyWorkspaceNavPermissions), which
 *     only ever NARROWS a member's view.
 *   - Items whose capability ServerKit Cloud holds under the managed profile
 *     (plan 25) leave the palette too, same as they leave every sidebar preset.
 *
 * Note: this intentionally does NOT apply personal sidebar-preset hiding — a page
 * you hid from your sidebar for tidiness stays reachable via the palette.
 */
export default function usePaletteAuthz() {
    const { isAdmin, hasPermission } = useAuth();
    const { activeWorkspace } = useWorkspace();
    const { hiddenSidebarIds: managedHidden } = useManagedProfile();

    return useMemo(() => {
        const navMap = activeWorkspace?.settings?.nav || null;
        const role = activeWorkspace?.my_effective_role || activeWorkspace?.my_role || 'member';

        const allowNav = (navId) => {
            if (!navId) return true;
            if (managedHidden.has(navId)) return false;
            if (isAdmin) return true;
            if (ALWAYS_VISIBLE.has(navId)) return true;
            // Per-user feature permissions: files.read=false 403s every files
            // endpoint, so don't offer the File Manager in the palette either.
            if (navId === 'files' && !hasPermission('files', 'read')) return false;
            if (!navMap) return true;
            const allowed = navMap[role];
            if (!Array.isArray(allowed) || allowed.length === 0) return true;
            return allowed.includes(navId);
        };

        const allowItem = (item) => {
            if (item.adminOnly && !isAdmin) return false;
            return allowNav(item.navId);
        };

        return { isAdmin, allowNav, allowItem };
    }, [isAdmin, hasPermission, activeWorkspace, managedHidden]);
}
