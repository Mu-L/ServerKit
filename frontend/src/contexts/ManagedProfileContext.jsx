import { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import ManagedProfileContext from './managedProfileContextValue';
import {
    MANAGED_CAPABILITY_MAP,
    managedCapabilityForPath,
    managedControlHeld,
    managedSidebarIds,
} from '../components/sidebarItems';

// The managed profile (plan 25). One fetch per panel load, next to the
// connect status: ServerKit Cloud's signed presentation policy, naming the
// capabilities it holds for this customer. Everything the UI needs derives
// from it here so the sidebar, the palette, the route gate and the in-page
// controls all read the same answer.
//
// The endpoint is admin-only; a refusal (a viewer, an older backend, a panel
// that was never paired) reads as "no profile", which is the safe default:
// nothing is ever hidden on a guess.
export function ManagedProfileProvider({ children }) {
    const [profile, setProfile] = useState(null);
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        try {
            const data = await api.getManagedProfile();
            setProfile(data && data.profile !== 'none' ? data : null);
        } catch {
            setProfile(null);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const value = useMemo(() => {
        const active = Boolean(profile?.active);
        const capabilities = active ? (profile.capabilities || []) : [];
        const hiddenSidebarIds = managedSidebarIds(capabilities);
        return {
            loading,
            profile,
            active,
            // The document lapsed: the full panel is back, with a banner.
            lapsed: Boolean(profile?.expired),
            capabilities,
            hiddenSidebarIds,
            capabilityLabel: (cap) => MANAGED_CAPABILITY_MAP[cap]?.label || cap,
            cardForPath: (pathname) => managedCapabilityForPath(pathname, capabilities),
            isControlHeld: (control) => managedControlHeld(control, capabilities),
            reload: load,
        };
    }, [loading, profile, load]);

    return (
        <ManagedProfileContext.Provider value={value}>
            {children}
        </ManagedProfileContext.Provider>
    );
}

export default ManagedProfileProvider;
