import { useEffect, useState } from 'react';
import { Layers } from 'lucide-react';
import api from '../../services/api';
import { useToast } from '../../contexts/useToast.js';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { useTranslation } from 'react-i18next';

// Must match app/services/deploy_settings.py DEFAULTS / _RULES.
const WATCH_DEFAULT = 60;
const WATCH_MAX = 900;
const STANDBY_DEFAULT = 10;
const STANDBY_MAX = 1440;

function intIn(value, min, max) {
    const n = Number(value);
    return Number.isInteger(n) && n >= min && n <= max ? n : null;
}

// A/B slot deploys (plan 87 §F): the opt-in and its knobs. Eligibility comes
// from the server with its reasons, so the toggle never promises what a
// deploy would then refuse.
const SlotDeploysPanel = ({ app, onChanged }) => {
    const { t } = useTranslation();
    const toast = useToast();
    const settings = app.deploy_settings || {};
    const [status, setStatus] = useState(null);
    const [toggling, setToggling] = useState(false);
    const [saving, setSaving] = useState(false);
    const [watch, setWatch] = useState(String(settings.watch_seconds ?? WATCH_DEFAULT));
    const [standby, setStandby] = useState(String(settings.standby_warm_minutes ?? STANDBY_DEFAULT));
    const [volumesOk, setVolumesOk] = useState(!!settings.slot_volumes_confirmed);

    useEffect(() => {
        let cancelled = false;
        api.getAppSlots(app.id)
            .then((data) => { if (!cancelled) setStatus(data); })
            .catch(() => {});
        return () => { cancelled = true; };
    }, [app.id, app.deploy_settings]);

    const watchValue = intIn(watch, 0, WATCH_MAX);
    const standbyValue = intIn(standby, 0, STANDBY_MAX);
    const dirty = (watchValue !== null && watchValue !== (settings.watch_seconds ?? WATCH_DEFAULT))
        || (standbyValue !== null && standbyValue !== (settings.standby_warm_minutes ?? STANDBY_DEFAULT))
        || volumesOk !== !!settings.slot_volumes_confirmed;

    async function handleToggle(next) {
        setToggling(true);
        try {
            const data = await api.setAppSlots(app.id, next);
            setStatus(data.slots);
            toast.success(next
                ? t('app.slotDeploysPanel.enabled', 'Slot deploys on. The running container is now slot A; the next deploy boots slot B.')
                : (data.note || t('app.slotDeploysPanel.disabled', 'Slot deploys off.')));
            onChanged?.();
        } catch (err) {
            toast.error(err.message || t('app.slotDeploysPanel.toggleFailed', 'Could not change slot deploys'));
        } finally {
            setToggling(false);
        }
    }

    async function handleSave() {
        setSaving(true);
        try {
            await api.updateApp(app.id, {
                deploy_settings: {
                    watch_seconds: watchValue,
                    standby_warm_minutes: standbyValue,
                    slot_volumes_confirmed: volumesOk,
                },
            });
            toast.success(t('app.slotDeploysPanel.saved', 'Rollout settings saved.'));
            onChanged?.();
        } catch (err) {
            toast.error(err.message || t('app.slotDeploysPanel.saveFailed', 'Failed to save rollout settings'));
        } finally {
            setSaving(false);
        }
    }

    if (!status) return null;
    const { eligibility } = status;
    const hasVolumes = (status.volumes || []).length > 0;

    return (
        <div className="app-panel">
            <div className="app-panel-header">
                <Layers />
                <span>{t('app.slotDeploysPanel.title', 'Slot deploys')}</span>
            </div>
            <div className="app-panel-body">
                <p className="app-panel-hint">
                    {t('app.slotDeploysPanel.intro', 'Each deploy boots the new release in a second slot on its own port while the live one keeps serving. It switches traffic only after the new release passes its health check, watches it for a while, and switches back on its own if it falls over. The previous release stays warm for an instant manual switch back.')}
                </p>

                <div className="settings-row">
                    <div className="settings-label">
                        <span>{t('app.slotDeploysPanel.enable', 'Use slot deploys')}</span>
                        <span className="settings-hint">
                            {eligibility.eligible
                                ? t('app.slotDeploysPanel.enableHint', 'Turning it on restarts nothing: the running container becomes slot A.')
                                : eligibility.reasons.join(' ')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Switch
                            checked={!!status.enabled}
                            onCheckedChange={handleToggle}
                            disabled={toggling || (!status.enabled && !eligibility.eligible)}
                            aria-label={t('app.slotDeploysPanel.enable', 'Use slot deploys')}
                        />
                    </div>
                </div>

                <div className="settings-row">
                    <div className="settings-label">
                        <label htmlFor={`slot-watch-${app.id}`}>
                            {t('app.slotDeploysPanel.watch', 'Watch window (seconds)')}
                        </label>
                        <span className="settings-hint">
                            {t('app.slotDeploysPanel.watchHint', 'How long a release is watched after it goes live. If it stops answering in that time, traffic goes back to the previous release. 0 turns the watch off.')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Input id={`slot-watch-${app.id}`} type="number" min={0} max={WATCH_MAX}
                            value={watch} onChange={(e) => setWatch(e.target.value)} disabled={saving} />
                    </div>
                </div>

                <div className="settings-row">
                    <div className="settings-label">
                        <label htmlFor={`slot-standby-${app.id}`}>
                            {t('app.slotDeploysPanel.standby', 'Keep the previous release warm (minutes)')}
                        </label>
                        <span className="settings-hint">
                            {t('app.slotDeploysPanel.standbyHint', 'While warm, switching back takes seconds. After that it is stopped: it no longer uses memory, and switching back starts it first.')}
                        </span>
                    </div>
                    <div className="settings-control">
                        <Input id={`slot-standby-${app.id}`} type="number" min={0} max={STANDBY_MAX}
                            value={standby} onChange={(e) => setStandby(e.target.value)} disabled={saving} />
                    </div>
                </div>

                {hasVolumes && (
                    <div className="settings-row">
                        <div className="settings-label">
                            <span>{t('app.slotDeploysPanel.volumes', 'Two copies may share the volumes')}</span>
                            <span className="settings-hint">
                                {t('app.slotDeploysPanel.volumesHint', 'For a few seconds around each switch both releases mount the same volumes. Leave this off if the app keeps something like SQLite there, which breaks when two processes write to it.')}
                            </span>
                        </div>
                        <div className="settings-control">
                            <Switch checked={volumesOk} onCheckedChange={setVolumesOk} disabled={saving}
                                aria-label={t('app.slotDeploysPanel.volumes', 'Two copies may share the volumes')} />
                        </div>
                    </div>
                )}

                <div className="settings-row">
                    <div className="settings-label" />
                    <div className="settings-control">
                        <Button size="sm" onClick={handleSave}
                            disabled={saving || !dirty || watchValue === null || standbyValue === null}>
                            {saving ? t('common.editing.saving', 'Saving…') : t('common.actions.save', 'Save')}
                        </Button>
                    </div>
                </div>

                <p className="app-panel-hint">
                    {t('app.slotDeploysPanel.schemaRule', 'Both releases use the same database during the switch. Keep schema changes backward-compatible for one deploy: add first, deploy, remove later.')}
                </p>
            </div>
        </div>
    );
};

export default SlotDeploysPanel;
