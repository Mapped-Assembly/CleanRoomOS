"""Transactional local QA inbox; no external delivery or messaging integration."""
from datetime import datetime, timezone
from hashlib import sha256
import sqlite3
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime

from cleanroom_os.contracts import Contract, Count, Identifier, QAReviewPackage, Text, parse_contract

NotificationStatus = Literal['awaiting_qa', 'approved', 'rejected', 'resolution_requested', 'superseded']


class QANotification(Contract):
    notification_id: Identifier
    audience: Literal['qa'] = 'qa'
    package_id: Identifier
    package_revision: Count
    plan_id: Identifier
    plan_revision: Count
    target: Text
    status: NotificationStatus
    completeness: Literal['incomplete', 'complete']
    action_required: Text
    created_at: AwareDatetime
    updated_at: AwareDatetime


class LocalQAQueue:
    """Use the controller transaction so package archival and notification are atomic."""

    @staticmethod
    def initialize(db: sqlite3.Connection) -> None:
        db.execute('CREATE TABLE IF NOT EXISTS qa_packages (package_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(package_id, revision))')
        db.execute('CREATE TABLE IF NOT EXISTS qa_notifications (notification_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, package_revision INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(package_id, package_revision))')

    @staticmethod
    def action(status: NotificationStatus, completeness: str) -> str:
        return {
            'awaiting_qa': ('Inspect incomplete evidence; reject or request resolution. Approval cannot waive obligations.'
                            if completeness == 'incomplete' else 'QA: review this exact package and approve, reject or request resolution.'),
            'approved': 'QA decision recorded. Any source or result change requires a new review.',
            'rejected': 'Review the QA rationale; correct source inputs or LIMS evidence before preparing a new package.',
            'resolution_requested': 'Manufacturing: resolve the cited inputs or result findings, then prepare a new QA package.',
            'superseded': 'Historical package only. Open the current notification before making a decision.',
        }[status]

    @classmethod
    def enqueue(cls, db: sqlite3.Connection, package: QAReviewPackage) -> QANotification:
        payload = package.model_dump_json()
        db.execute('INSERT OR IGNORE INTO qa_packages VALUES (?,?,?)', (package.package_id, package.revision, payload))
        existing = db.execute('SELECT payload FROM qa_packages WHERE package_id=? AND revision=?', (package.package_id, package.revision)).fetchone()[0]
        if parse_contract(QAReviewPackage, existing) != package:
            raise ValueError('An archived package revision cannot be overwritten')
        now = datetime.now(timezone.utc)
        nid = 'QA-' + sha256(f'{package.package_id}\0{package.revision}'.encode()).hexdigest()[:32]
        notification = QANotification(notification_id=nid, package_id=package.package_id,
            package_revision=package.revision, plan_id=package.plan.plan_id, plan_revision=package.plan.revision,
            target=f'cleanroom://qa/packages/{quote(package.package_id, safe="")}/revisions/{package.revision}',
            status='awaiting_qa', completeness=package.completeness,
            action_required=cls.action('awaiting_qa', package.completeness), created_at=now, updated_at=now)
        db.execute('INSERT OR IGNORE INTO qa_notifications VALUES (?,?,?,?)',
                   (nid, package.package_id, package.revision, notification.model_dump_json()))
        row = db.execute('SELECT payload FROM qa_notifications WHERE notification_id=?', (nid,)).fetchone()
        return parse_contract(QANotification, row[0])

    @staticmethod
    def list(db: sqlite3.Connection) -> list[QANotification]:
        return [parse_contract(QANotification, r[0]) for r in db.execute('SELECT payload FROM qa_notifications ORDER BY rowid')]

    @classmethod
    def synchronize(cls, db: sqlite3.Connection, package: QAReviewPackage | None, status: NotificationStatus) -> None:
        """Old evidence is visible but cannot appear to have a current approval."""
        if package is not None: cls.enqueue(db, package)
        for n in cls.list(db):
            current = package is not None and (n.package_id, n.package_revision) == (package.package_id, package.revision)
            desired = status if current else 'superseded'
            if n.status != desired:
                n.status = desired
                n.action_required = cls.action(desired, n.completeness)
                n.updated_at = datetime.now(timezone.utc)
                db.execute('UPDATE qa_notifications SET payload=? WHERE notification_id=?', (n.model_dump_json(), n.notification_id))
