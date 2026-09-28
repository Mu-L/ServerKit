// Per-app container operations (image-update, auto-sleep) + GPU.

// --- Image updates ---
export async function checkImageUpdate(appId) {
    return this.request(`/image-updates/applications/${appId}/check`, { method: 'POST' });
}

export async function getImageUpdate(appId) {
    return this.request(`/image-updates/applications/${appId}`);
}

export async function applyImageUpdate(appId) {
    return this.request(`/apps/${appId}/image-update/apply`, { method: 'POST' });
}

// --- Auto-sleep ---
export async function getSleepPolicy(appId) {
    return this.request(`/apps/${appId}/sleep-policy`);
}

export async function updateSleepPolicy(appId, data) {
    return this.request(`/apps/${appId}/sleep-policy`, { method: 'PUT', body: data });
}

export async function sleepApp(appId) {
    return this.request(`/apps/${appId}/sleep`, { method: 'POST' });
}

export async function wakeApp(appId) {
    return this.request(`/apps/${appId}/wake`, { method: 'POST' });
}

// --- GPU ---
export async function getGpuInfo() {
    return this.request('/gpu/');
}

// --- WAF (per-app ModSecurity) ---
export async function getWafPolicy(appId) {
    return this.request(`/waf/applications/${appId}/policy`);
}

export async function updateWafPolicy(appId, data) {
    return this.request(`/waf/applications/${appId}/policy`, { method: 'PUT', body: data });
}

export async function applyWaf(appId) {
    return this.request(`/waf/applications/${appId}/apply`, { method: 'POST' });
}

export async function getWafEvents(appId, limit = 50) {
    return this.request(`/waf/applications/${appId}/events?limit=${encodeURIComponent(limit)}`);
}

export async function getWafStatus() {
    return this.request('/waf/status');
}

export async function installWaf() {
    return this.request('/waf/install', { method: 'POST' });
}
