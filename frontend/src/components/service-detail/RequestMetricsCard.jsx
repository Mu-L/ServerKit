import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import api from '../../services/api';
import { Sparkline } from '@/components/ds';
import { formatCompactNumber, formatNumber, formatPercent } from '@/utils/intl';

// Minute points folded into this many bins, so a 24h window draws 48 points.
const BINS = 48;

function fold(points, pick, combine) {
    if (!points.length) return [];
    const size = Math.max(1, Math.ceil(points.length / BINS));
    const out = [];
    for (let i = 0; i < points.length; i += size) {
        out.push(combine(points.slice(i, i + size).map(pick)));
    }
    return out;
}

const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const max = (xs) => xs.reduce((a, b) => Math.max(a, b ?? 0), 0);

function formatMs(ms) {
    if (ms === null || ms === undefined) return '-';
    return ms >= 1000
        ? `${formatNumber(ms / 1000, { maximumFractionDigits: 1 })} s`
        : `${formatNumber(ms)} ms`;
}

// Requests / errors / latency / cache from the timed nginx log (plan 86 §A2).
// Hidden until the app has served a request in the window.
export default function RequestMetricsCard({ appId }) {
    const { t } = useTranslation();
    const [data, setData] = useState(null);

    useEffect(() => {
        let cancelled = false;
        api.getAppRequestMetrics(appId, '24h')
            .then((res) => { if (!cancelled) setData(res); })
            .catch(() => {});
        return () => { cancelled = true; };
    }, [appId]);

    const summary = data?.summary;
    if (!summary?.requests) return null;
    const points = data.points || [];

    const stats = [
        {
            key: 'requests',
            label: t('app.requestMetrics.requests', 'Requests'),
            value: formatCompactNumber(summary.requests),
            series: fold(points, (p) => p.requests, sum),
        },
        {
            key: 'errors',
            label: t('app.requestMetrics.errorRate', '5xx rate'),
            value: formatPercent((summary.error_rate ?? 0) * 100, { decimals: 1 }),
            series: fold(points, (p) => p.status_5xx, sum),
            tone: 'error',
        },
        {
            key: 'latency',
            label: t('app.requestMetrics.p95', 'p95 latency'),
            value: formatMs(summary.p95_ms),
            series: fold(points, (p) => p.p95_ms, max),
        },
        {
            key: 'cache',
            label: t('app.requestMetrics.cacheHit', 'Cache hit ratio'),
            value: summary.cache_hit_ratio === null
                ? t('app.requestMetrics.notCached', 'Not cached')
                : formatPercent(summary.cache_hit_ratio * 100),
        },
    ];

    return (
        <div className="overview-tab__card overview-tab__card--full request-metrics">
            <div className="overview-tab__card-header-row">
                <h3 className="overview-tab__card-title">
                    {t('app.requestMetrics.title', 'Traffic')}
                </h3>
                <span className="request-metrics__window">
                    {t('app.requestMetrics.last24h', 'Last 24 hours')}
                </span>
            </div>
            <div className="request-metrics__grid">
                {stats.map((stat) => (
                    <div key={stat.key} className="request-metrics__stat">
                        <span className="request-metrics__label">{stat.label}</span>
                        <span className="request-metrics__value">{stat.value}</span>
                        {stat.series?.some((v) => v > 0) && (
                            <Sparkline
                                data={stat.series}
                                width={160}
                                height={28}
                                color={stat.tone === 'error' ? 'var(--red)' : undefined}
                                className="request-metrics__spark"
                            />
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
