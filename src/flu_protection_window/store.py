"""保护窗口推演的 SQLite 存储。

除早期 ``records`` 表外，保存：指南版本、地区流行季、儿童、家长授权、
班级与归属时段、接种凭证与接种事实、医学暂缓、暴露事件、通知、隐私复核
队列以及未来窗口变化触发器（``due_changes``）。触发器持久化，使服务重启
后仍会处理尚未触发的窗口变化。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import (
    Certificate,
    Child,
    Consent,
    DueChange,
    Enrollment,
    ExposureEvent,
    Guideline,
    MedicalDeferral,
    Notification,
    Record,
    ReviewItem,
    SchoolClass,
    Season,
    VaccineDose,
)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS records (
        record_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        state TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS guidelines (
        guideline_id TEXT PRIMARY KEY,
        version TEXT NOT NULL,
        effective_from TEXT NOT NULL,
        gap_max_days INTEGER NOT NULL,
        protection_days INTEGER NOT NULL,
        waning_days INTEGER NOT NULL,
        two_dose_under_months INTEGER NOT NULL,
        min_dose_interval_days INTEGER NOT NULL,
        min_class_size INTEGER NOT NULL,
        high_risk_below REAL NOT NULL,
        medium_risk_below REAL NOT NULL,
        exposure_window_days INTEGER NOT NULL,
        region TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seasons (
        season_id TEXT PRIMARY KEY,
        region TEXT NOT NULL,
        name TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        intensity TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS children (
        child_id TEXT PRIMARY KEY,
        birth_date TEXT NOT NULL,
        name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consents (
        child_id TEXT NOT NULL,
        guardian_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        granted INTEGER NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        PRIMARY KEY (child_id, guardian_id, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classes (
        class_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        region TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enrollments (
        child_id TEXT NOT NULL,
        class_id TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (child_id, class_id, valid_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS certificates (
        cert_id TEXT PRIMARY KEY,
        -- 业务标识用于去重，但同标识的冲突凭证也要留存待复核，因此不加 UNIQUE。
        business_key TEXT NOT NULL,
        child_id TEXT NOT NULL,
        dose_seq INTEGER NOT NULL,
        vaccinated_on TEXT NOT NULL,
        vaccine TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_certificates_business_key
        ON certificates(business_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS vaccine_doses (
        child_id TEXT NOT NULL,
        dose_seq INTEGER NOT NULL,
        vaccinated_on TEXT NOT NULL,
        vaccine TEXT NOT NULL,
        cert_business_key TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (child_id, dose_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deferrals (
        deferral_id TEXT PRIMARY KEY,
        child_id TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exposures (
        event_id TEXT PRIMARY KEY,
        class_id TEXT NOT NULL,
        occurred_on TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        target_id TEXT NOT NULL,
        audience TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        guideline_version TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        content TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_items (
        review_id TEXT PRIMARY KEY,
        business_key TEXT NOT NULL,
        cert_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS due_changes (
        trigger_id TEXT PRIMARY KEY,
        fire_date TEXT NOT NULL,
        child_id TEXT NOT NULL,
        class_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        fired_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_due_changes_fire
        ON due_changes(status, fire_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS recompute_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
]


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        for statement in SCHEMA:
            self.connection.execute(statement)
        self.connection.commit()

    # ------------------------------------------------------------------
    # 早期基础记录（保持兼容）
    # ------------------------------------------------------------------
    def add(self, record: Record) -> Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at) "
                "VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state,
                 value.revision, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at "
            "FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # 通用写入
    # ------------------------------------------------------------------
    def _insert(self, table: str, row: dict) -> None:
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.connection:
            self.connection.execute(
                f"INSERT INTO {table}({columns}) VALUES({marks})",
                tuple(row.values()),
            )

    def _fetch_all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    # ------------------------------------------------------------------
    # 指南版本
    # ------------------------------------------------------------------
    def add_guideline(self, value: Guideline) -> Guideline:
        self._insert("guidelines", {
            "guideline_id": value.guideline_id,
            "version": value.version,
            "effective_from": value.effective_from,
            "gap_max_days": value.gap_max_days,
            "protection_days": value.protection_days,
            "waning_days": value.waning_days,
            "two_dose_under_months": value.two_dose_under_months,
            "min_dose_interval_days": value.min_dose_interval_days,
            "min_class_size": value.min_class_size,
            "high_risk_below": value.high_risk_below,
            "medium_risk_below": value.medium_risk_below,
            "exposure_window_days": value.exposure_window_days,
            "region": value.region,
        })
        return value

    def list_guidelines(self) -> list[Guideline]:
        rows = self._fetch_all(
            "SELECT * FROM guidelines ORDER BY effective_from, version"
        )
        return [Guideline(**{k: row[k] for k in row.keys()
                            if k in Guideline.__dataclass_fields__}) for row in rows]

    # ------------------------------------------------------------------
    # 流行季
    # ------------------------------------------------------------------
    def add_season(self, value: Season) -> Season:
        self._insert("seasons", {
            "season_id": value.season_id, "region": value.region,
            "name": value.name, "start_date": value.start_date,
            "end_date": value.end_date, "intensity": value.intensity,
        })
        return value

    def list_seasons(self) -> list[Season]:
        return [Season(**dict(row))
                for row in self._fetch_all("SELECT * FROM seasons")]

    # ------------------------------------------------------------------
    # 儿童
    # ------------------------------------------------------------------
    def add_child(self, value: Child) -> Child:
        self._insert("children", {
            "child_id": value.child_id,
            "birth_date": value.birth_date,
            "name": value.name,
        })
        return value

    def get_child(self, child_id: str) -> Child | None:
        row = self.connection.execute(
            "SELECT child_id, birth_date, name FROM children WHERE child_id=?",
            (child_id,),
        ).fetchone()
        return Child(**dict(row)) if row else None

    def list_children(self) -> list[Child]:
        return [Child(**dict(row))
                for row in self._fetch_all("SELECT * FROM children")]

    # ------------------------------------------------------------------
    # 家长授权
    # ------------------------------------------------------------------
    def upsert_consent(self, value: Consent) -> Consent:
        with self.connection:
            self.connection.execute(
                "INSERT INTO consents(child_id, guardian_id, scope, granted, "
                "valid_from, valid_to) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(child_id, guardian_id, scope) DO UPDATE SET "
                "granted=excluded.granted, valid_from=excluded.valid_from, "
                "valid_to=excluded.valid_to",
                (value.child_id, value.guardian_id, value.scope,
                 1 if value.granted else 0,
                 value.valid_from, value.valid_to),
            )
        return value

    def list_consents(self, child_id: str | None = None) -> list[Consent]:
        if child_id is None:
            rows = self._fetch_all("SELECT * FROM consents")
        else:
            rows = self._fetch_all(
                "SELECT * FROM consents WHERE child_id=?", (child_id,)
            )
        return [
            Consent(
                child_id=row["child_id"], guardian_id=row["guardian_id"],
                granted=bool(row["granted"]), scope=row["scope"],
                valid_from=row["valid_from"], valid_to=row["valid_to"],
            )
            for row in rows
        ]

    def consent_granted(self, child_id: str, on: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM consents WHERE child_id=? AND granted=1 "
            "AND valid_from<=? AND (valid_to IS NULL OR valid_to='') "
            "ORDER BY valid_from DESC LIMIT 1",
            (child_id, on),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 班级与归属时段
    # ------------------------------------------------------------------
    def add_class(self, value: SchoolClass) -> SchoolClass:
        self._insert("classes", {
            "class_id": value.class_id, "name": value.name,
            "region": value.region,
        })
        return value

    def get_class(self, class_id: str) -> SchoolClass | None:
        row = self.connection.execute(
            "SELECT class_id, name, region FROM classes WHERE class_id=?",
            (class_id,),
        ).fetchone()
        return SchoolClass(**dict(row)) if row else None

    def list_classes(self) -> list[SchoolClass]:
        return [SchoolClass(**dict(row))
                for row in self._fetch_all("SELECT * FROM classes")]

    def add_enrollment(self, value: Enrollment) -> Enrollment:
        self._insert("enrollments", {
            "child_id": value.child_id, "class_id": value.class_id,
            "valid_from": value.valid_from, "valid_to": value.valid_to,
            "reason": value.reason,
        })
        return value

    def close_open_enrollments(
        self, child_id: str, valid_to: str, reason: str = ""
    ) -> int:
        """转班时关闭该儿童仍开放的旧归属时段，返回关闭条数。"""
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE enrollments SET valid_to=? "
                "WHERE child_id=? AND valid_to IS NULL AND valid_from<=?",
                (valid_to, child_id, valid_to),
            )
            return cursor.rowcount

    def list_enrollments(
        self, child_id: str | None = None, class_id: str | None = None
    ) -> list[Enrollment]:
        sql = "SELECT * FROM enrollments"
        clauses, params = [], []
        if child_id is not None:
            clauses.append("child_id=?")
            params.append(child_id)
        if class_id is not None:
            clauses.append("class_id=?")
            params.append(class_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [Enrollment(**dict(row))
                for row in self._fetch_all(sql, tuple(params))]

    # ------------------------------------------------------------------
    # 凭证（业务标识去重）
    # ------------------------------------------------------------------
    def add_certificate(self, value: Certificate) -> Certificate:
        self._insert("certificates", {
            "cert_id": value.cert_id,
            "business_key": value.business_key,
            "child_id": value.child_id,
            "dose_seq": value.dose_seq,
            "vaccinated_on": value.vaccinated_on,
            "vaccine": value.vaccine,
            "content_hash": value.content_hash,
            "submitted_at": value.submitted_at,
            "status": value.status,
            "note": value.note,
        })
        return value

    def get_certificate_by_key(self, business_key: str) -> Certificate | None:
        row = self.connection.execute(
            "SELECT * FROM certificates WHERE business_key=?", (business_key,)
        ).fetchone()
        return Certificate(**dict(row)) if row else None

    def get_certificate(self, cert_id: str) -> Certificate | None:
        row = self.connection.execute(
            "SELECT * FROM certificates WHERE cert_id=?", (cert_id,)
        ).fetchone()
        return Certificate(**dict(row)) if row else None

    def list_certificates(self, status: str | None = None) -> list[Certificate]:
        if status is None:
            rows = self._fetch_all("SELECT * FROM certificates ORDER BY submitted_at")
        else:
            rows = self._fetch_all(
                "SELECT * FROM certificates WHERE status=? ORDER BY submitted_at",
                (status,),
            )
        return [Certificate(**dict(row)) for row in rows]

    def update_certificate_status(
        self, cert_id: str, status: str, note: str = ""
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE certificates SET status=?, note=? WHERE cert_id=?",
                (status, note, cert_id),
            )

    # ------------------------------------------------------------------
    # 接种事实
    # ------------------------------------------------------------------
    def add_dose(self, value: VaccineDose) -> VaccineDose:
        self._insert("vaccine_doses", {
            "child_id": value.child_id, "dose_seq": value.dose_seq,
            "vaccinated_on": value.vaccinated_on, "vaccine": value.vaccine,
            "cert_business_key": value.cert_business_key,
        })
        return value

    def has_dose(self, child_id: str, dose_seq: int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM vaccine_doses WHERE child_id=? AND dose_seq=?",
            (child_id, dose_seq),
        ).fetchone() is not None

    def replace_dose(self, value: VaccineDose) -> VaccineDose:
        """隐私复核通过后修正接种事实（仅此路径允许覆盖历史剂次）。"""
        with self.connection:
            self.connection.execute(
                "UPDATE vaccine_doses SET vaccinated_on=?, vaccine=?, "
                "cert_business_key=? WHERE child_id=? AND dose_seq=?",
                (value.vaccinated_on, value.vaccine, value.cert_business_key,
                 value.child_id, value.dose_seq),
            )
        return value

    def list_doses(self, child_id: str | None = None) -> list[VaccineDose]:
        if child_id is None:
            rows = self._fetch_all("SELECT * FROM vaccine_doses")
        else:
            rows = self._fetch_all(
                "SELECT * FROM vaccine_doses WHERE child_id=? ORDER BY dose_seq",
                (child_id,),
            )
        return [VaccineDose(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 医学暂缓
    # ------------------------------------------------------------------
    def add_deferral(self, value: MedicalDeferral) -> MedicalDeferral:
        self._insert("deferrals", {
            "deferral_id": value.deferral_id,
            "child_id": value.child_id,
            "start_date": value.start_date,
            "end_date": value.end_date,
            "reason": value.reason,
        })
        return value

    def list_deferrals(self, child_id: str | None = None) -> list[MedicalDeferral]:
        if child_id is None:
            rows = self._fetch_all("SELECT * FROM deferrals")
        else:
            rows = self._fetch_all(
                "SELECT * FROM deferrals WHERE child_id=?", (child_id,)
            )
        return [MedicalDeferral(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 暴露事件
    # ------------------------------------------------------------------
    def add_exposure(self, value: ExposureEvent) -> ExposureEvent:
        self._insert("exposures", {
            "event_id": value.event_id, "class_id": value.class_id,
            "occurred_on": value.occurred_on, "description": value.description,
        })
        return value

    def list_exposures(self, class_id: str | None = None) -> list[ExposureEvent]:
        if class_id is None:
            rows = self._fetch_all("SELECT * FROM exposures")
        else:
            rows = self._fetch_all(
                "SELECT * FROM exposures WHERE class_id=?", (class_id,)
            )
        return [ExposureEvent(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def add_notification(self, value: Notification) -> Notification:
        self._insert("notifications", {
            "notification_id": value.notification_id,
            "scope": value.scope, "target_id": value.target_id,
            "audience": value.audience, "as_of_date": value.as_of_date,
            "guideline_version": value.guideline_version,
            "reason_code": value.reason_code,
            "content": json.dumps(value.content, ensure_ascii=False),
            "status": value.status, "created_at": value.created_at,
        })
        return value

    def list_notifications(
        self, target_id: str | None = None, status: str | None = None
    ) -> list[Notification]:
        sql = "SELECT * FROM notifications"
        clauses, params = [], []
        if target_id is not None:
            clauses.append("target_id=?")
            params.append(target_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY as_of_date, notification_id"
        rows = self._fetch_all(sql, tuple(params))
        return [
            Notification(
                notification_id=row["notification_id"], scope=row["scope"],
                target_id=row["target_id"], audience=row["audience"],
                as_of_date=row["as_of_date"],
                guideline_version=row["guideline_version"],
                reason_code=row["reason_code"],
                content=json.loads(row["content"]),
                status=row["status"], created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 隐私复核队列
    # ------------------------------------------------------------------
    def add_review_item(self, value: ReviewItem) -> ReviewItem:
        self._insert("review_items", {
            "review_id": value.review_id,
            "business_key": value.business_key,
            "cert_id": value.cert_id, "child_id": value.child_id,
            "reason_code": value.reason_code,
            "payload": json.dumps(value.payload, ensure_ascii=False),
            "status": value.status, "created_at": value.created_at,
            "resolved_at": value.resolved_at,
        })
        return value

    def get_review_item(self, review_id: str) -> ReviewItem | None:
        row = self.connection.execute(
            "SELECT * FROM review_items WHERE review_id=?", (review_id,)
        ).fetchone()
        return self._review_from_row(row) if row else None

    def list_review_items(self, status: str = "pending") -> list[ReviewItem]:
        rows = self._fetch_all(
            "SELECT * FROM review_items WHERE status=? ORDER BY created_at",
            (status,),
        )
        return [self._review_from_row(row) for row in rows]

    @staticmethod
    def _review_from_row(row: sqlite3.Row) -> ReviewItem:
        return ReviewItem(
            review_id=row["review_id"], business_key=row["business_key"],
            cert_id=row["cert_id"], child_id=row["child_id"],
            reason_code=row["reason_code"], payload=json.loads(row["payload"]),
            status=row["status"], created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def resolve_review_item(
        self, review_id: str, status: str, resolved_at: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review_items SET status=?, resolved_at=? WHERE review_id=?",
                (status, resolved_at, review_id),
            )

    # ------------------------------------------------------------------
    # 未来窗口变化触发器
    # ------------------------------------------------------------------
    def add_due_change(self, value: DueChange) -> DueChange:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO due_changes(trigger_id, fire_date, "
                "child_id, class_id, kind, payload, status, created_at, fired_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (value.trigger_id, value.fire_date, value.child_id,
                 value.class_id, value.kind,
                 json.dumps(value.payload, ensure_ascii=False),
                 value.status, value.created_at, value.fired_at),
            )
        return value

    def due_triggers(self, on_or_before: str) -> list[DueChange]:
        rows = self._fetch_all(
            "SELECT * FROM due_changes WHERE status='pending' AND fire_date<=? "
            "ORDER BY fire_date, trigger_id",
            (on_or_before,),
        )
        return [self._due_from_row(row) for row in rows]

    def pending_trigger_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM due_changes WHERE status='pending'"
        ).fetchone()
        return int(row[0])

    def mark_due_fired(self, trigger_id: str, fired_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE due_changes SET status='done', fired_at=? "
                "WHERE trigger_id=?",
                (fired_at, trigger_id),
            )

    @staticmethod
    def _due_from_row(row: sqlite3.Row) -> DueChange:
        return DueChange(
            trigger_id=row["trigger_id"], fire_date=row["fire_date"],
            child_id=row["child_id"], class_id=row["class_id"],
            kind=row["kind"], payload=json.loads(row["payload"]),
            status=row["status"], created_at=row["created_at"],
            fired_at=row["fired_at"],
        )

    # ------------------------------------------------------------------
    # 增量重算审计
    # ------------------------------------------------------------------
    def log_recompute(
        self, entity_type: str, entity_id: str, reason: str,
        as_of_date: str, created_at: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO recompute_log(entity_type, entity_id, reason, "
                "as_of_date, created_at) VALUES(?,?,?,?,?)",
                (entity_type, entity_id, reason, as_of_date, created_at),
            )

    def recompute_log(self) -> list[dict]:
        rows = self._fetch_all(
            "SELECT * FROM recompute_log ORDER BY id"
        )
        return [dict(row) for row in rows]
