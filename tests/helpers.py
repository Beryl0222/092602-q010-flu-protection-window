"""测试共用构造工具。"""
from __future__ import annotations

from flu_protection_window.clock import Clock
from flu_protection_window.service import Service
from flu_protection_window.store import Store


def guideline_2025() -> dict:
    return {
        "guideline_id": "gl-2025", "version": "2025版",
        "effective_from": "2025-09-01",
        "gap_max_days": 28, "protection_days": 180, "waning_days": 60,
        "two_dose_under_months": 108, "min_dose_interval_days": 28,
        "min_class_size": 5, "high_risk_below": 0.5,
        "medium_risk_below": 0.75, "exposure_window_days": 14,
        "region": None,
    }


def guideline_2026_north() -> dict:
    value = guideline_2025()
    value.update({
        "guideline_id": "gl-2026-north", "version": "2026版-北方",
        "effective_from": "2026-11-01",
        "gap_max_days": 21, "waning_days": 45,
        "region": "north",
    })
    return value


def season() -> dict:
    return {
        "season_id": "north-2026-2027", "region": "north",
        "name": "北方2026-2027流行季",
        "start_date": "2026-10-01", "end_date": "2027-03-31",
    }


def make_service(path: str = ":memory:", clock: str = "2026-09-02") -> Service:
    return Service(Store(path), Clock(clock))


def seed_base(service: Service, *, class_id: str = "class-a",
              size: int = 6, small_class: bool = False) -> dict:
    """注册指南、流行季、班级与适龄单剂儿童，返回各 id。"""
    payload: dict = {
        "guidelines": [guideline_2025(), guideline_2026_north()],
        "seasons": [season()],
        "classes": [
            {"class_id": class_id, "name": "一班", "region": "north"},
        ],
        "children": [], "consents": [], "enrollments": [],
    }
    ids = [f"c{i}" for i in range(1, size + 1)]
    for cid in ids:
        payload["children"].append(
            {"child_id": cid, "birth_date": "2017-03-01", "name": cid}
        )
        payload["consents"].append(
            {"child_id": cid, "guardian_id": f"g-{cid}",
             "granted": True, "valid_from": "2026-01-01"}
        )
        payload["enrollments"].append(
            {"child_id": cid, "class_id": class_id,
             "valid_from": "2026-09-01", "reason": "开学分班"}
        )
    if small_class:
        payload["classes"].append(
            {"class_id": "class-small", "name": "小班", "region": "north"}
        )
        for cid in ("s1", "s2"):
            payload["children"].append(
                {"child_id": cid, "birth_date": "2017-03-01", "name": cid}
            )
            payload["enrollments"].append(
                {"child_id": cid, "class_id": "class-small",
                 "valid_from": "2026-09-01"}
            )
        ids_extra = ["s1", "s2"]
    else:
        ids_extra = []
    service.load_scenario(payload)
    return {"children": ids, "small": ids_extra}
