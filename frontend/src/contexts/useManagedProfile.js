import { useContext } from 'react';
import ManagedProfileContext from './managedProfileContextValue';

// Read the managed profile (plan 25) anywhere under DashboardLayout: whether
// it is active, the held capabilities, the sidebar ids it removes, the
// capability a path's managed card belongs to, and whether a named in-page
// control is held. Outside the provider the context default answers "no
// profile" — nothing is ever hidden on a guess.
export function useManagedProfile() {
    return useContext(ManagedProfileContext);
}

export default useManagedProfile;
