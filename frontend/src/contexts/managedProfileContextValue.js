import { createContext } from 'react';

// The managed-profile context value (plan 25). Kept in its own module so the
// provider (a component file) and useManagedProfile (the hook) share one
// context object without either exporting across the react-refresh boundary.
const ManagedProfileContext = createContext({
    loading: true,
    profile: null,
    active: false,
    lapsed: false,
    capabilities: [],
    hiddenSidebarIds: new Set(),
    capabilityLabel: () => null,
    cardForPath: () => null,
    isControlHeld: () => false,
    reload: () => {},
});

export default ManagedProfileContext;
