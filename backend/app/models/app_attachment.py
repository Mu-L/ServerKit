"""An app's attachment to a service it uses (plan 86 §C2/§C4).

One row per (app, kind): the app's storage bucket on a Garage instance, or
the cache / queue it uses (env references only). ``details_json``
holds what the provisioner created (bucket, key id, the vault secret that
carries the key), so detaching can remove exactly that and nothing else. The
secret itself never lands here.

Both foreign keys have parent-side cascades: deleting either the app or the
service it is attached to takes the attachment row with it.
"""
from datetime import datetime

from app import db
from app.models.json_column_mixin import JsonColumnMixin


class AppAttachment(JsonColumnMixin, db.Model):
    __tablename__ = 'app_attachments'
    __table_args__ = (
        db.UniqueConstraint('app_id', 'kind', name='uq_app_attachments_app_kind'),
    )

    KINDS = ('storage', 'cache', 'queue')

    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(db.Integer, db.ForeignKey('applications.id', ondelete='CASCADE'),
                       nullable=False, index=True)
    service_app_id = db.Column(db.Integer, db.ForeignKey('applications.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)
    details_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    application = db.relationship(
        'Application', foreign_keys=[app_id],
        backref=db.backref('attachments', cascade='all, delete-orphan',
                           lazy='select'))
    service_app = db.relationship(
        'Application', foreign_keys=[service_app_id],
        backref=db.backref('attached_by', cascade='all, delete-orphan',
                           lazy='select'))

    @property
    def details(self):
        return self._json_read('details_json', {}, expect=dict)

    @details.setter
    def details(self, value):
        self._json_write('details_json', value)

    def to_dict(self):
        details = self.details
        return {
            'id': self.id,
            'app_id': self.app_id,
            'kind': self.kind,
            'service_app_id': self.service_app_id,
            'service_name': self.service_app.name if self.service_app else None,
            # Never the secret; the key id alone is safe to show.
            'bucket': details.get('bucket'),
            'access_key_id': details.get('access_key_id'),
            'env_keys': details.get('env_keys') or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
