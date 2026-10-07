"""儿童流感保护窗口推演使用的领域记录。

所有记录刻意保持为不可变数据载体，业务规则在 ``engine`` 与 ``service`` 中实现。
日期在系统边界统一使用 ISO 字符串（``YYYY-MM-DD``），推演引擎内部使用
``datetime.date``。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# 兼容早期基础登记能力的最小记录（基线测试与旧契约仍在使用）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or datetime.now(timezone.utc).isoformat()
        return replace(self, created_at=value)


# ---------------------------------------------------------------------------
# 规则版本与地区假设
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Guideline:
    """年度流感接种指南版本。

    版本只对 ``effective_from`` 当日及之后的推演生效；历史接种事实与已经
    发出的通知不会因新版本而被改写。
    """

    guideline_id: str
    version: str
    effective_from: str
    # 接种后免疫空白期：[接种日, 接种日 + gap_max_days) 不计入保护，
    # 常见口径为两到四周，这里取上限作为保护开始日。
    gap_max_days: int = 28
    # 完整保护持续天数，之后进入衰减区间，衰减结束视为到期。
    protection_days: int = 180
    waning_days: int = 60
    # 小于该月龄（首次接种等情形）需要两剂，两剂之间的最小间隔。
    two_dose_under_months: int = 108
    min_dose_interval_days: int = 28
    # 面向班主任披露时班级最小群体人数（k 匿名）。
    min_class_size: int = 5
    # 班级风险阈值：覆盖率低于 high 为高风险，低于 medium 为中风险。
    high_risk_below: float = 0.5
    medium_risk_below: float = 0.75
    # 暴露事件（班内病例）对风险的影响窗口与升级天数。
    exposure_window_days: int = 14
    # 适用地区编码，None 表示通用版本（可被地区专用版本覆盖）。
    region: str | None = None


@dataclass(frozen=True)
class Season:
    """地区流行季假设，例如北方每年 10 月至次年 3 月。"""

    season_id: str
    region: str
    name: str
    start_date: str
    end_date: str
    intensity: str = "normal"  # normal / early / severe


# ---------------------------------------------------------------------------
# 人群与授权
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Child:
    child_id: str
    birth_date: str
    name: str = ""


@dataclass(frozen=True)
class Consent:
    """家长授权：决定个体健康细节可以向谁开放。"""

    child_id: str
    guardian_id: str
    granted: bool
    scope: str = "individual-health"  # individual-health / notification
    valid_from: str = ""
    valid_to: str | None = None


@dataclass(frozen=True)
class SchoolClass:
    class_id: str
    name: str
    region: str = "north"


@dataclass(frozen=True)
class Enrollment:
    """班级归属时段，转班通过新增一条时段表示，旧时段保持原样。"""

    child_id: str
    class_id: str
    valid_from: str
    valid_to: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# 接种事实与医学暂缓
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VaccineDose:
    child_id: str
    dose_seq: int
    vaccinated_on: str
    vaccine: str = "influenza"
    # 来源凭证业务标识，凭证冲突时事实不会被静默覆盖。
    cert_business_key: str = ""


@dataclass(frozen=True)
class Certificate:
    """家长补交的接种凭证，按 ``business_key`` 业务标识去重。"""

    cert_id: str
    business_key: str
    child_id: str
    dose_seq: int
    vaccinated_on: str
    vaccine: str
    content_hash: str
    submitted_at: str
    # accepted / duplicate / conflict_pending / approved / rejected
    status: str = "accepted"
    note: str = ""


@dataclass(frozen=True)
class MedicalDeferral:
    """医学暂缓时段，[start_date, end_date] 闭区间。"""

    deferral_id: str
    child_id: str
    start_date: str
    end_date: str
    reason: str = ""


# ---------------------------------------------------------------------------
# 流行季内的班级暴露事件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExposureEvent:
    event_id: str
    class_id: str
    occurred_on: str
    description: str = ""


# ---------------------------------------------------------------------------
# 通知（含因最小群体人数而被抑制的尝试）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Notification:
    notification_id: str
    scope: str  # class / child
    target_id: str
    audience: str  # teacher / school_doctor / child_health_office / guardian
    as_of_date: str
    guideline_version: str
    reason_code: str
    content: dict = field(default_factory=dict)
    # sent / suppressed
    status: str = "sent"
    created_at: str = ""


# ---------------------------------------------------------------------------
# 隐私复核与未来窗口变化触发器
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    business_key: str
    cert_id: str
    child_id: str
    reason_code: str  # conflicting_content
    payload: dict = field(default_factory=dict)
    # pending / approved / rejected
    status: str = "pending"
    created_at: str = ""
    resolved_at: str = ""


@dataclass(frozen=True)
class DueChange:
    """尚未触发的窗口变化，持久化保存，服务重启后继续处理。"""

    trigger_id: str
    fire_date: str
    child_id: str
    class_id: str
    kind: str  # gap_end / protection_wane / protection_expire / deferral_end / season_start
    payload: dict = field(default_factory=dict)
    status: str = "pending"  # pending / done
    created_at: str = ""
    fired_at: str = ""


# 个体未计入保护的规则类别（也是命令行“规则类别”列的取值）
RULE_CATEGORIES: dict[str, str] = {
    "gap": "接种后两到四周免疫空白期",
    "incomplete_schedule": "剂次不足或两剂间隔不足",
    "waning": "保护进入衰减期",
    "expired": "保护已到期",
    "unvaccinated": "无有效接种记录",
    "deferred": "医学暂缓期间",
    "unknown_guideline": "当日无生效指南版本",
}

# 需要授权才能查看个体健康细节的角色
AUTHORIZED_ROLES = frozenset({"school_doctor", "child_health_office"})
TEACHER_ROLE = "teacher"
