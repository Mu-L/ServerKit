"""One of an app's two deploy slots (plan 87 §B).

The panel updater keeps two installs, ``/opt/serverkit-a`` and ``-b``, and
flips between them. An app with slot deploys enabled gets the same: rows
``(application_id, 'a')`` and ``(application_id, 'b')``. A deploy boots the
new release in the idle slot on its own loopback port, gates it, then points
nginx at it by setting ``app.port`` to that port — so the ~40 readers of
``app.port`` (drift, private URLs, monitors, domains) stay right without edits.
The slot that was live becomes the standby: warm for a while for an instant
switch back, then stopped (never removed) until the next deploy reuses it.

``state``: empty | building | booting | healthy | live | standby | stopped | failed
"""
from datetime import datetime

from app import db

SLOTS = ('a', 'b')
STATES = ('empty', 'building', 'booting', 'healthy', 'live', 'standby', 'stopped', 'failed')


def other(slot: str) -> str:
    return 'b' if slot == 'a' else 'a'


class AppSlot(db.Model):
    __tablename__ = 'app_slots'
    __table_args__ = (
        db.UniqueConstraint('application_id', 'slot', name='uq_app_slots_app_slot'),
    )

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('applications.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    slot = db.Column(db.String(1), nullable=False)
    # Loopback port nginx proxies to while this slot is live. Kept between
    # deploys so a slot always comes back on the same port.
    host_port = db.Column(db.Integer, nullable=True)
    # What the app listens on inside the container; the same for both slots.
    container_port = db.Column(db.Integer, nullable=True)
    container_name = db.Column(db.String(128), nullable=True)
    # Compose slots (§C): the slot's own compose project.
    project_name = db.Column(db.String(128), nullable=True)
    # Immutable image this slot runs (serverkit-app-<id>:d<deployment> or a digest).
    image_ref = db.Column(db.String(255), nullable=True)
    commit_sha = db.Column(db.String(40), nullable=True)
    deployment_id = db.Column(db.Integer, db.ForeignKey('deployments.id', ondelete='SET NULL'),
                              nullable=True, index=True)
    config_snapshot_id = db.Column(db.Integer, nullable=True)
    state = db.Column(db.String(16), nullable=False, default='empty')
    started_at = db.Column(db.DateTime, nullable=True)
    # When a warm standby is stopped (set when it becomes standby).
    standby_until = db.Column(db.DateTime, nullable=True)
    last_health = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    application = db.relationship(
        'Application',
        backref=db.backref('slots', cascade='all, delete-orphan', lazy='select'))
    deployment = db.relationship('Deployment', foreign_keys=[deployment_id])

    def to_dict(self):
        dep = self.deployment
        return {
            'slot': self.slot,
            'state': self.state,
            'host_port': self.host_port,
            'container_port': self.container_port,
            'container_name': self.container_name,
            'project_name': self.project_name,
            'image_ref': self.image_ref,
            'commit_sha': self.commit_sha,
            'deployment_id': self.deployment_id,
            'version': dep.version if dep else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'standby_until': self.standby_until.isoformat() if self.standby_until else None,
            'last_health': self.last_health,
        }

    def __repr__(self):
        return f'<AppSlot app={self.application_id} {self.slot} {self.state}>'
