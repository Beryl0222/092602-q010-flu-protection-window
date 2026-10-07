"""保护窗口推演应用服务。

关键业务约束都在这里收口：

- 可调时钟：所有推演日由 ``Clock`` 或参数给定，不读系统时钟；
- 指南版本只影响生效日之后的预测，历史接种事实与旧通知永不改写；
- 转班、补种、暂缓状态变化只重算相关儿童与相关班级（见 ``recompute_log``）；
- 班主任只看得到达最小群体人数的班级风险级别，个体健康细节仅对授权
  角色开放，家长通知以家长授权为前提；
- 凭证按业务标识去重，同标识内容冲突进入隐私复核队列；
- 未来窗口变化（空白期结束、衰减、到期、暂缓结束、流行季开始）落库为
  触发器，服务重启后 ``run_due`` 继续处理。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta

from . import engine
from .clock import Clock, now_iso, to_date
from .domain import (
    AUTHORIZED_ROLES,
    RULE_CATEGORIES,
    TEACHER_ROLE,
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
from .store import Store


class PermissionError_(PermissionError):
    """业务授权不足（区分内置异常仅用于可读的错误信息）。"""


# 班级风险级别到班主任通知原因码
RISK_REASON = {
    "high": "class_risk_high",
    "medium": "class_risk_medium",
    "low": "class_risk_low",
}

# 面向班主任展示的风险级别中文文案（不暴露具体判定口径数字）
RISK_TEXT = {"high": "高风险", "medium": "中风险", "low": "低风险",
             "off_season": "非流行季", "insufficient": "人数不足，不披露"}


class Service:
    def __init__(
        self, store: Store | None = None, clock: Clock | None = None
    ) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ------------------------------------------------------------------
    # 早期基础能力（保持兼容）
    # ------------------------------------------------------------------
    def health(self) -> dict[str, str]:
        return {"service": "flu_protection_window", "status": "ok"}

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        required = ("record_id", "owner_id", "state")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError("缺少必要字段：" + "、".join(missing))
        record = Record(
            record_id=str(payload["record_id"]), owner_id=str(payload["owner_id"]),
            state=str(payload["state"]), revision=int(payload.get("revision", 1)),
        )
        return self.store.add(record).__dict__.copy()

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get(record_id)
        return value.__dict__.copy() if value else None

    # ==================================================================
    # 情景装载
    # ==================================================================
    def load_scenario(self, payload: dict) -> dict:
        """批量载入一份情景数据，返回各类实体计数。"""
        counts: dict[str, int] = {}

        for item in payload.get("guidelines", []):
            self.store.add_guideline(self._guideline_from(item))
        counts["guidelines"] = len(payload.get("guidelines", []))

        for item in payload.get("seasons", []):
            self.store.add_season(Season(**item))
        counts["seasons"] = len(payload.get("seasons", []))

        for item in payload.get("children", []):
            self.store.add_child(Child(**item))
        counts["children"] = len(payload.get("children", []))

        for item in payload.get("consents", []):
            self.store.upsert_consent(self._consent_from(item))
        counts["consents"] = len(payload.get("consents", []))

        for item in payload.get("classes", []):
            self.store.add_class(SchoolClass(**item))
        counts["classes"] = len(payload.get("classes", []))

        for item in payload.get("enrollments", []):
            self.store.add_enrollment(Enrollment(**item))
        counts["enrollments"] = len(payload.get("enrollments", []))

        cert_count = 0
        for item in payload.get("certificates", []):
            result = self.submit_certificate(item)
            cert_count += 1
            counts.setdefault(f"certificate_{result.status}", 0)
            counts[f"certificate_{result.status}"] += 1
        counts["certificates"] = cert_count

        for item in payload.get("deferrals", []):
            self._add_deferral(item)
        counts["deferrals"] = len(payload.get("deferrals", []))

        for item in payload.get("exposures", []):
            self.store.add_exposure(ExposureEvent(**item))
        counts["exposures"] = len(payload.get("exposures", []))

        # 流行季开始是天然的未来窗口变化：为每个班安排尚未到达的触发器。
        today = self.clock.today()
        for klass in self.store.list_classes():
            for season in self.store.list_seasons():
                if season.region != klass.region:
                    continue
                if to_date(season.start_date) < today:
                    continue
                self._put_due(DueChange(
                    trigger_id=f"season_start:{klass.class_id}:{season.season_id}",
                    fire_date=season.start_date, child_id="",
                    class_id=klass.class_id, kind="season_start",
                    payload={"season_id": season.season_id},
                    created_at=now_iso(),
                ))
        return counts

    @staticmethod
    def _guideline_from(item: dict) -> Guideline:
        fields = Guideline.__dataclass_fields__
        return Guideline(**{k: v for k, v in item.items() if k in fields})

    @staticmethod
    def _consent_from(item: dict) -> Consent:
        value = dict(item)
        value.setdefault("scope", "individual-health")
        value.setdefault("valid_from", "1900-01-01")
        return Consent(**value)

    # ==================================================================
    # 凭证提交：业务标识去重 + 冲突隐私复核
    # ==================================================================
    def submit_certificate(self, payload: dict) -> Certificate:
        business_key = str(payload["business_key"])
        content_hash = self._content_hash(payload)
        existing = self.store.get_certificate_by_key(business_key)
        if existing is not None:
            if existing.content_hash == content_hash:
                # 同一凭证重复补交：幂等，不重复建接种事实、不重复推演。
                return existing
            # 同业务标识但内容冲突：进入隐私复核，事实保持原样。
            cert = Certificate(
                cert_id=payload.get("cert_id") or f"cert-{uuid.uuid4().hex[:12]}",
                business_key=business_key,
                child_id=str(payload["child_id"]),
                dose_seq=int(payload["dose_seq"]),
                vaccinated_on=str(payload["vaccinated_on"]),
                vaccine=str(payload.get("vaccine", "influenza")),
                content_hash=content_hash,
                submitted_at=self.clock.iso() if "submitted_at" not in payload
                else str(payload["submitted_at"]),
                status="conflict_pending",
                note=f"与已存凭证 {existing.cert_id} 内容冲突",
            )
            self.store.add_certificate(cert)
            self.store.add_review_item(ReviewItem(
                review_id=f"review-{uuid.uuid4().hex[:12]}",
                business_key=business_key, cert_id=cert.cert_id,
                child_id=cert.child_id, reason_code="conflicting_content",
                payload={"incoming": payload, "existing_cert_id": existing.cert_id,
                         "existing_hash": existing.content_hash},
                created_at=now_iso(),
            ))
            return cert

        cert = Certificate(
            cert_id=payload.get("cert_id") or f"cert-{uuid.uuid4().hex[:12]}",
            business_key=business_key,
            child_id=str(payload["child_id"]),
            dose_seq=int(payload["dose_seq"]),
            vaccinated_on=str(payload["vaccinated_on"]),
            vaccine=str(payload.get("vaccine", "influenza")),
            content_hash=content_hash,
            submitted_at=payload.get("submitted_at", now_iso()),
            status="accepted",
            note=payload.get("note", ""),
        )
        self.store.add_certificate(cert)
        self.store.add_dose(VaccineDose(
            child_id=cert.child_id, dose_seq=cert.dose_seq,
            vaccinated_on=cert.vaccinated_on, vaccine=cert.vaccine,
            cert_business_key=cert.business_key,
        ))
        self._schedule_dose_triggers(cert.child_id, cert.vaccinated_on)
        self._recompute_child(cert.child_id, "certificate_accepted")
        return cert

    @staticmethod
    def _content_hash(payload: dict) -> str:
        material = json.dumps(
            {k: payload.get(k) for k in
             ("child_id", "dose_seq", "vaccinated_on", "vaccine")},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def resolve_review(self, review_id: str, approve: bool) -> dict:
        review = self.store.get_review_item(review_id)
        if review is None:
            raise ValueError(f"复核项不存在：{review_id}")
        if review.status != "pending":
            raise ValueError(f"复核项已处理：{review_id}（{review.status}）")
        cert = self.store.get_certificate(review.cert_id)
        resolution = "approved" if approve else "rejected"
        self.store.resolve_review_item(review_id, resolution, now_iso())
        if cert is not None:
            self.store.update_certificate_status(cert.cert_id, resolution)
        if approve and cert is not None:
            # 只有复核通过的冲突凭证才能修正接种事实。
            if self.store.has_dose(cert.child_id, cert.dose_seq):
                self.store.replace_dose(VaccineDose(
                    child_id=cert.child_id, dose_seq=cert.dose_seq,
                    vaccinated_on=cert.vaccinated_on, vaccine=cert.vaccine,
                    cert_business_key=cert.business_key,
                ))
            else:
                self.store.add_dose(VaccineDose(
                    child_id=cert.child_id, dose_seq=cert.dose_seq,
                    vaccinated_on=cert.vaccinated_on, vaccine=cert.vaccine,
                    cert_business_key=cert.business_key,
                ))
            self._schedule_dose_triggers(cert.child_id, cert.vaccinated_on)
            self._recompute_child(cert.child_id, "review_approved")
        return {"review_id": review_id, "resolution": resolution,
                "cert_id": review.cert_id}

    def pending_reviews(self) -> list[dict]:
        return [item.__dict__.copy() for item in self.store.list_review_items()]

    # ==================================================================
    # 转班与暂缓：只重算相关儿童和班级
    # ==================================================================
    def transfer_child(
        self, child_id: str, new_class_id: str, on: str | None = None,
        reason: str = "转班"
    ) -> dict:
        on = on or self.clock.iso()
        if self.store.get_child(child_id) is None:
            raise ValueError(f"儿童不存在：{child_id}")
        if self.store.get_class(new_class_id) is None:
            raise ValueError(f"班级不存在：{new_class_id}")
        old_classes = {
            e.class_id for e in self.store.list_enrollments(child_id=child_id)
            if e.valid_to is None
        }
        if new_class_id in old_classes:
            return {"child_id": child_id, "changed": False,
                    "class_id": new_class_id}
        self.store.close_open_enrollments(child_id, on, reason)
        self.store.add_enrollment(Enrollment(
            child_id=child_id, class_id=new_class_id,
            valid_from=on, reason=reason,
        ))
        affected = sorted(old_classes | {new_class_id})
        for class_id in affected:
            self._recompute("class", class_id, f"transfer:{child_id}", on)
        self.store.log_recompute("child", child_id, "transfer", on, now_iso())
        return {"child_id": child_id, "changed": True,
                "affected_classes": affected, "as_of": on}

    def add_deferral(self, payload: dict) -> dict:
        self._add_deferral(payload)
        value = MedicalDeferral(**{k: v for k, v in payload.items()
                                   if k in MedicalDeferral.__dataclass_fields__})
        fire_date = to_date(value.end_date) + timedelta(days=1)
        if fire_date >= self.clock.today():
            self._put_due(DueChange(
                trigger_id=f"deferral_end:{value.deferral_id}",
                fire_date=fire_date.isoformat(),
                child_id=value.child_id, class_id="", kind="deferral_end",
                payload={"deferral_id": value.deferral_id},
                created_at=now_iso(),
            ))
        self._recompute_child(value.child_id, "deferral_started")
        return {"deferral_id": value.deferral_id,
                "child_id": value.child_id, "status": "recorded"}

    def _add_deferral(self, payload: dict) -> None:
        fields = MedicalDeferral.__dataclass_fields__
        self.store.add_deferral(MedicalDeferral(
            **{k: v for k, v in payload.items() if k in fields}
        ))

    # ==================================================================
    # 班级提醒推演（工作人员 / 班主任两种视图）
    # ==================================================================
    def advise_class(
        self, class_id: str, on: str | None = None,
        role: str = "school_doctor",
    ) -> dict:
        on_date = to_date(on) if on else self.clock.today()
        facts = self._facts(self._region_of(class_id))
        day = engine.evaluate_class_day(
            class_id, on_date, **facts,
            region=self._region_of(class_id),
        )
        guideline = engine.select_guideline(
            self.store.list_guidelines(), self._region_of(class_id), on_date
        )
        result = self._class_view(day, role)
        result["class_id"] = class_id
        result["as_of"] = on_date.isoformat()
        result["guideline"] = (
            {"version": guideline.version,
             "guideline_id": guideline.guideline_id,
             "effective_from": guideline.effective_from,
             "region": guideline.region or "general"}
            if guideline else None
        )
        # 班主任查询只生成班级级别通知；个体通知由工作人员（校医/儿保）推演时产生。
        result["notifications"] = self._emit_class_notifications(
            day, role, include_individual=role != TEACHER_ROLE)
        return result

    def _class_view(self, day, role: str) -> dict:
        base = {
            "risk_level": day.risk_level,
            "reasons": list(day.reasons),
            "season": day.season_name or None,
            "exposure_window": day.exposure,
        }
        if role == TEACHER_ROLE:
            # 班主任：必须达到最小群体人数，且只显示班级风险级别，
            # 不含覆盖率、规则类别人数或任何个体信息。
            if day.risk_level == "insufficient":
                return {**base, "risk_level": "insufficient",
                        "reasons": ["样本不足，暂不披露班级风险级别"],
                        "denominator": None, "covered": None,
                        "uncovered_by_rule": None, "individuals": None,
                        "season": None, "exposure_window": None}
            return {"risk_level": day.risk_level,
                    "reasons": [f"班级当前流感风险级别：{RISK_TEXT[day.risk_level]}"],
                    "season": None, "exposure_window": None,
                    "denominator": None, "covered": None,
                    "uncovered_by_rule": None, "individuals": None}
        if role not in AUTHORIZED_ROLES:
            raise PermissionError_(f"角色无权查看班级推演：{role}")
        return {
            **base,
            "denominator": day.denominator,
            "covered": day.covered,
            "covered_full": day.covered_full,
            "covered_waning": day.covered_waning,
            "coverage_rate": round(day.coverage_rate, 4),
            "uncovered_by_rule": {
                category: {"count": count, "label": RULE_CATEGORIES[category]}
                for category, count in sorted(day.uncovered_by_category.items())
            },
            "individuals": [self._individual_dict(s) for s in day.individuals],
        }

    def individual_detail(
        self, child_id: str, on: str | None = None, *,
        role: str, guardian_id: str | None = None,
    ) -> dict:
        on_date = to_date(on) if on else self.clock.today()
        if role == TEACHER_ROLE:
            raise PermissionError_("班主任不可查看个体健康细节")
        if role == "guardian":
            if not guardian_id or not self._consent_valid(
                child_id, guardian_id, on_date.isoformat()
            ):
                raise PermissionError_("缺少有效的家长授权，不可查看该儿童细节")
        elif role not in AUTHORIZED_ROLES:
            raise PermissionError_(f"角色无权查看个体健康细节：{role}")

        child = self.store.get_child(child_id)
        if child is None:
            raise ValueError(f"儿童不存在：{child_id}")
        classes = self.store.list_classes()
        region = self._region_of_child(child_id, classes, on_date)
        guideline = engine.select_guideline(
            self.store.list_guidelines(), region, on_date
        )
        status = engine.classify_individual(
            child, self.store.list_doses(child_id),
            self.store.list_deferrals(child_id), guideline, on_date,
        )
        result = self._individual_dict(status)
        result["class_id"] = self._class_of_child(child_id, on_date)
        return result

    @staticmethod
    def _individual_dict(status) -> dict:
        return {
            "child_id": status.child_id,
            "category": status.category,
            "category_label": status.category_label,
            "covered": status.covered,
            "guideline_version": status.guideline_version,
            "effective_dose_on": status.effective_dose_on.isoformat()
            if status.effective_dose_on else None,
            "protection_starts_on": status.protection_starts_on.isoformat()
            if status.protection_starts_on else None,
            "wanes_on": status.wanes_on.isoformat()
            if status.wanes_on else None,
            "expires_on": status.expires_on.isoformat()
            if status.expires_on else None,
            "needs_second_dose": status.needs_second_dose,
            "detail": status.detail,
        }

    # ==================================================================
    # 通知：历史通知永不改写；抑制（人数不足/缺授权）同样留痕
    # ==================================================================
    def _emit_class_notifications(
        self, day, actor_role: str, *, include_individual: bool = True
    ) -> list[dict]:
        emitted: list[dict] = []
        version = day.guideline_version or "none"
        as_of = day.day.isoformat()

        if day.risk_level == "insufficient":
            emitted.append(self._save_notification(Notification(
                notification_id=f"class:{day.class_id}:teacher:{as_of}:below_min_group",
                scope="class", target_id=day.class_id, audience=TEACHER_ROLE,
                as_of_date=as_of, guideline_version=version,
                reason_code="below_min_group",
                content={"reason": "未达到最小群体人数，风险级别抑制"},
                status="suppressed", created_at=now_iso(),
            )))
            return emitted

        reason = RISK_REASON.get(day.risk_level)
        if reason is not None and day.risk_level in ("high", "medium"):
            emitted.append(self._save_notification(Notification(
                notification_id=f"class:{day.class_id}:teacher:{as_of}:{reason}:{version}",
                scope="class", target_id=day.class_id, audience=TEACHER_ROLE,
                as_of_date=as_of, guideline_version=version,
                reason_code=reason,
                # 面向班主任的通知只含风险级别，不含个体或分类人数。
                content={"risk_level": day.risk_level},
                status="sent", created_at=now_iso(),
            )))

        # 个体提醒只对已授权家长发送；无论是否授权都留痕（含未接种儿童）。
        if not include_individual:
            return emitted
        for status in day.individuals:
            if status.covered or status.category == "unknown_guideline":
                continue
            consent = self.store.consent_granted(status.child_id, as_of)
            notification = Notification(
                notification_id=(
                    f"child:{status.child_id}:guardian:{as_of}:"
                    f"{status.category}:{version}"
                ),
                scope="child", target_id=status.child_id,
                audience="guardian", as_of_date=as_of,
                guideline_version=version,
                reason_code=f"individual_{status.category}",
                content={"category": status.category,
                         "label": status.category_label,
                         "detail": status.detail,
                         "class_id": day.class_id},
                status="sent" if consent else "suppressed",
                created_at=now_iso(),
            )
            if not consent:
                notification = Notification(
                    **{**notification.__dict__,
                       "content": {**notification.content,
                                   "suppress_reason": "guardian_consent_missing"}}
                )
            emitted.append(self._save_notification(notification))
        return emitted

    def _save_notification(self, value: Notification) -> dict:
        try:
            self.store.add_notification(value)
        except sqlite3.IntegrityError:
            # 同一日期、同一原因、同一指南版本的通知已存在：历史通知保持原样。
            existing = next(
                (n for n in self.store.list_notifications(value.target_id)
                 if n.notification_id == value.notification_id), None
            )
            return {"notification_id": value.notification_id,
                    "deduplicated": True,
                    "status": existing.status if existing else "exists",
                    "reason_code": value.reason_code,
                    "audience": value.audience}
        return {"notification_id": value.notification_id,
                "status": value.status, "reason_code": value.reason_code,
                "audience": value.audience}

    def list_notifications(self, target_id: str | None = None) -> list[dict]:
        return [n.__dict__.copy()
                for n in self.store.list_notifications(target_id)]

    # ==================================================================
    # 未来窗口变化触发器（重启后继续处理）
    # ==================================================================
    def _schedule_dose_triggers(self, child_id: str, dose_on: str) -> None:
        classes = self.store.list_classes()
        region = None
        for klass in classes:
            members = engine.members_on(
                klass.class_id, self.store.list_enrollments(), self.clock.today()
            )
            if child_id in members:
                region = klass.region
                break
        guideline = engine.select_guideline(
            self.store.list_guidelines(), region, self.clock.today()
        )
        if guideline is None:
            return
        base = to_date(dose_on)
        today = self.clock.today()
        gap_end = base + timedelta(days=guideline.gap_max_days)
        wane = base + timedelta(days=guideline.protection_days)
        expire = wane + timedelta(days=guideline.waning_days)
        for fire_date, kind in (
            (gap_end, "gap_end"), (wane, "protection_wane"),
            (expire, "protection_expire"),
        ):
            if fire_date < today:
                # 凭证迟交、窗口变化在登记前已经发生：当前推演即反映其状态，
                # 不再为过去日期补建触发器。
                continue
            self._put_due(DueChange(
                trigger_id=f"{kind}:{child_id}:{fire_date.isoformat()}",
                fire_date=fire_date.isoformat(), child_id=child_id,
                class_id="", kind=kind,
                payload={"guideline_id": guideline.guideline_id,
                         "guideline_version": guideline.version},
                created_at=now_iso(),
            ))

    def _put_due(self, value: DueChange) -> bool:
        before = self.store.pending_trigger_count()
        self.store.add_due_change(value)
        return self.store.pending_trigger_count() > before

    def pending_trigger_count(self) -> int:
        return self.store.pending_trigger_count()

    def run_due(self, on: str | None = None) -> list[dict]:
        """处理所有 fire_date <= 推演日 的待触发变化；重启后调用即可续跑。"""
        on_iso = on or self.clock.iso()
        processed: list[dict] = []
        for trigger in self.store.due_triggers(on_iso):
            self.store.mark_due_fired(trigger.trigger_id, now_iso())
            affected: list[str] = []
            notifications: list[dict] = []
            if trigger.kind == "season_start":
                affected.append(trigger.class_id)
                self._recompute(
                    "class", trigger.class_id,
                    f"trigger:{trigger.kind}", trigger.fire_date
                )
                day_view = self.advise_class(
                    trigger.class_id, trigger.fire_date,
                    role="school_doctor",
                )
                notifications = day_view["notifications"]
            else:
                class_id = self._class_of_child(
                    trigger.child_id, to_date(trigger.fire_date)
                )
                self._recompute_child(
                    trigger.child_id, f"trigger:{trigger.kind}",
                    trigger.fire_date
                )
                if class_id:
                    affected.append(class_id)
                    day_view = self.advise_class(
                        class_id, trigger.fire_date, role="school_doctor"
                    )
                    notifications = day_view["notifications"]
            processed.append({
                "trigger_id": trigger.trigger_id, "kind": trigger.kind,
                "fire_date": trigger.fire_date,
                "child_id": trigger.child_id or None,
                "affected_classes": affected,
                "notifications": notifications,
            })
        return processed

    # ==================================================================
    # 提前 / 延后接种对比
    # ==================================================================
    def compare_class(
        self, class_id: str, start: str, end: str,
        early_days: int = 14, late_days: int = 14,
    ) -> dict:
        region = self._region_of(class_id)
        return engine.compare_timing(
            class_id, to_date(start), to_date(end),
            early_days=early_days, late_days=late_days,
            doses=self.store.list_doses(),
            enrollments=self.store.list_enrollments(),
            children_by_id={c.child_id: c for c in self.store.list_children()},
            deferrals=self.store.list_deferrals(),
            guidelines=self.store.list_guidelines(),
            seasons=self.store.list_seasons(),
            exposures=self.store.list_exposures(),
            region=region,
        )

    # ==================================================================
    # 内部装配
    # ==================================================================
    def _facts(self, region: str | None) -> dict:
        return {
            "enrollments": self.store.list_enrollments(),
            "children_by_id": {c.child_id: c for c in self.store.list_children()},
            "doses": self.store.list_doses(),
            "deferrals": self.store.list_deferrals(),
            "guidelines": self.store.list_guidelines(),
            "seasons": self.store.list_seasons(),
            "exposures": self.store.list_exposures(),
        }

    def _region_of(self, class_id: str) -> str | None:
        klass = self.store.get_class(class_id)
        return klass.region if klass else None

    def _class_of_child(self, child_id: str, on) -> str | None:
        for enrollment in self.store.list_enrollments(child_id=child_id):
            if to_date(enrollment.valid_from) <= on and (
                enrollment.valid_to is None
                or to_date(enrollment.valid_to) > on
            ):
                return enrollment.class_id
        return None

    def _region_of_child(self, child_id: str, classes, on) -> str | None:
        class_id = self._class_of_child(child_id, on)
        for klass in classes:
            if klass.class_id == class_id:
                return klass.region
        return None

    def _consent_valid(
        self, child_id: str, guardian_id: str, on: str
    ) -> bool:
        for consent in self.store.list_consents(child_id):
            if consent.guardian_id != guardian_id or not consent.granted:
                continue
            if consent.valid_from <= on and (
                not consent.valid_to or consent.valid_to >= on
            ):
                return True
        return False

    def _recompute_child(
        self, child_id: str, reason: str, on: str | None = None
    ) -> None:
        on = on or self.clock.iso()
        classes = {
            enrollment.class_id
            for enrollment in self.store.list_enrollments(child_id=child_id)
        }
        for class_id in classes:
            self._recompute("class", class_id, f"{reason}:{child_id}", on)
        self._recompute("child", child_id, reason, on)

    def _recompute(
        self, entity_type: str, entity_id: str, reason: str, on: str
    ) -> None:
        self.store.log_recompute(entity_type, entity_id, reason, on, now_iso())

    def recompute_log(self) -> list[dict]:
        return self.store.recompute_log()
