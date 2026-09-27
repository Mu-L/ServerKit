// Per-domain bandwidth accounting — backs the Services list sparklines and
// the service detail "Bandwidth this month" stat.

export async function getBandwidthApps() {
    return this.request('/bandwidth/apps');
}

export async function getAppBandwidth(appId, days = 90) {
    return this.request(`/bandwidth/apps/${appId}?days=${encodeURIComponent(days)}`);
}

export async function runBandwidthAggregate(day = null) {
    return this.request('/bandwidth/aggregate', {
        method: 'POST',
        body: JSON.stringify(day ? { day } : {}),
    });
}

// Per-app request rate, status classes, latency percentiles and cache hit
// ratio from the timed access log. period: '1h' | '24h' | '7d' | '30d'.
export async function getAppRequestMetrics(appId, period = '24h') {
    return this.request(`/bandwidth/apps/${appId}/requests?period=${encodeURIComponent(period)}`);
}
