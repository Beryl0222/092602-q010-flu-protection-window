"""保护窗口推演引擎（纯函数）。

输入领域记录与某个推演日，输出个体/班级的保护状态。引擎不读写数据库、
不接触系统时钟，所有“当前日期”由调用方传入，因此同一份历史事实在不同
指南版本下可以复现不同预测，而历史事实本身保持不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .clock import to_date
from .domain import (
    RULE_CATEGORIES,
    Child,
    Guideline,
    Season,
    VaccineDose,
)

DAYS = timedelta(days=1)


# ---------------------------------------------------------------------------
# 版本与流行季选择
# ---------------------------------------------------------------------------
def select_guideline(
    guidelines, region: str | None, on: date
) -> Guideline | None:
    """返回 ``on`` 当日对地区生效的指南：地区专用优先，其次最新通用版本。"""
    usable = [
        g for g in guidelines
        if to_date(g.effective_from) <= on and (g.region is None or g.region == region)
    ]
    if not usable:
        return None
    usable.sort(
        key=lambda g: (g.region == region, to_date(g.effective_from)),
        reverse=True,
    )
    return usable[0]


def active_season(seasons, region: str | None, on: date) -> Season | None:
    """流行季区间允许跨年（北方典型为 10 月至次年 3 月）。"""
    for season in seasons:
        if region is not None and season.region != region:
            continue
        start = to_date(season.start_date)
        end = to_date(season.end_date)
        if start <= end:
            if start <= on <= end:
                return season
        else:  # 跨年区间
            if on >= start or on <= end:
                return season
    return None


# ---------------------------------------------------------------------------
# 个体推演
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IndividualStatus:
    child_id: str
    day: date
    category: str          # full / waning / gap / incomplete_schedule / expired / unvaccinated / deferred / unknown_guideline
    covered: bool
    guideline_id: str = ""
    guideline_version: str = ""
    effective_dose_on: date | None = None
    protection_starts_on: date | None = None
    wanes_on: date | None = None
    expires_on: date | None = None
    needs_second_dose: bool = False
    detail: str = ""

    @property
    def category_label(self) -> str:
        if self.category in ("full",):
            return "完整保护期"
        if self.category == "waning":
            return "保护衰减期（仍计入覆盖）"
        return RULE_CATEGORIES.get(self.category, self.category)


def _age_months(birth: date, on: date) -> int:
    return (on.year - birth.year) * 12 + on.month - birth.month - (
        1 if on.day < birth.day else 0
    )


def effective_schedule(
    child: Child, doses, guideline: Guideline, as_of: date
) -> tuple[date | None, bool, bool]:
    """返回 (末次有效接种日, 是否需要两剂, 两剂间隔是否曾不足)。

    - 首次接种时年龄达到 ``two_dose_under_months`` 月龄：单剂即构成有效程序；
    - 否则需要与首剂间隔不少于 ``min_dose_interval_days`` 的第二剂；
      间隔不足的第二剂不构成有效程序，以其后合格剂次为准。
    仅统计 ``as_of`` 当日及之前发生的接种。
    """
    past = sorted(
        (to_date(d.vaccinated_on) for d in doses if d.child_id == child.child_id
         and to_date(d.vaccinated_on) <= as_of),
    )
    if not past:
        return None, False, False
    birth = to_date(child.birth_date)
    first = past[0]
    needs_two = _age_months(birth, first) < guideline.two_dose_under_months
    if not needs_two:
        return past[-1], False, False
    short_interval = False
    effective = None
    for second in past[1:]:
        gap = (second - first).days
        if gap >= guideline.min_dose_interval_days:
            effective = second
        else:
            short_interval = True
    return effective, True, short_interval


def classify_individual(
    child: Child,
    doses,
    deferrals,
    guideline: Guideline | None,
    on: date,
) -> IndividualStatus:
    """计算某儿童在 ``on`` 当日的保护类别。"""
    if guideline is None:
        return IndividualStatus(
            child_id=child.child_id, day=on, category="unknown_guideline",
            covered=False, detail="当日没有生效的指南版本",
        )

    for deferral in deferrals:
        if deferral.child_id != child.child_id:
            continue
        if to_date(deferral.start_date) <= on <= to_date(deferral.end_date):
            return IndividualStatus(
                child_id=child.child_id, day=on, category="deferred",
                covered=False, guideline_id=guideline.guideline_id,
                guideline_version=guideline.version,
                detail=f"医学暂缓：{deferral.reason or '未注明原因'}",
            )

    effective, needs_two, short_interval = effective_schedule(child, doses, guideline, on)
    common = dict(
        guideline_id=guideline.guideline_id,
        guideline_version=guideline.version,
        needs_second_dose=needs_two,
        effective_dose_on=effective,
    )
    if effective is None:
        first_dose = sorted(
            (to_date(d.vaccinated_on) for d in doses
             if d.child_id == child.child_id and to_date(d.vaccinated_on) <= on)
        )
        if first_dose:
            detail = "两剂间隔不足，第二剂暂不计入" if short_interval else "已接种首剂，尚需第二剂"
            return IndividualStatus(
                child_id=child.child_id, day=on,
                category="incomplete_schedule", covered=False, detail=detail, **common
            )
        return IndividualStatus(
            child_id=child.child_id, day=on, category="unvaccinated",
            covered=False, detail="尚无接种记录", **common
        )

    starts = effective + timedelta(days=guideline.gap_max_days)
    wanes = effective + timedelta(days=guideline.protection_days)
    expires = wanes + timedelta(days=guideline.waning_days)
    common.update(protection_starts_on=starts, wanes_on=wanes, expires_on=expires)

    if on < starts:
        return IndividualStatus(
            child_id=child.child_id, day=on, category="gap", covered=False,
            detail=f"接种后 {guideline.gap_max_days} 天免疫空白期，"
                   f"{starts.isoformat()} 起计入保护", **common
        )
    if on < wanes:
        return IndividualStatus(
            child_id=child.child_id, day=on, category="full", covered=True,
            detail="完整保护期", **common
        )
    if on < expires:
        return IndividualStatus(
            child_id=child.child_id, day=on, category="waning", covered=True,
            detail="保护衰减期，仍计入班级覆盖", **common
        )
    return IndividualStatus(
        child_id=child.child_id, day=on, category="expired", covered=False,
        detail=f"保护已于 {expires.isoformat()} 到期，建议补种", **common
    )


# ---------------------------------------------------------------------------
# 班级推演
# ---------------------------------------------------------------------------
def members_on(
    class_id: str, enrollments, on: date
) -> list[str]:
    """返回当日在班儿童。归属时段为半开区间 [valid_from, valid_to)：

    转班当天旧班时段在转班日零点结束、新班时段同日开始，避免重复计数。
    """
    result = []
    for item in enrollments:
        if item.class_id != class_id:
            continue
        if to_date(item.valid_from) <= on and (
            item.valid_to is None or to_date(item.valid_to) > on
        ):
            result.append(item.child_id)
    return result


def exposure_active(
    class_id: str, exposures, on: date, window_days: int
) -> ExposureEvent | None:
    for event in exposures:
        if event.class_id != class_id:
            continue
        delta = (on - to_date(event.occurred_on)).days
        if 0 <= delta < window_days:
            return event
    return None


@dataclass(frozen=True)
class ClassDay:
    class_id: str
    day: date
    risk_level: str
    denominator: int
    covered: int
    covered_full: int
    covered_waning: int
    uncovered_by_category: dict = field(default_factory=dict)
    individuals: list[IndividualStatus] = field(default_factory=list)
    season_name: str = ""
    guideline_id: str = ""
    guideline_version: str = ""
    exposure: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def coverage_rate(self) -> float:
        return self.covered / self.denominator if self.denominator else 0.0


def _risk_for_rate(rate: float, guideline: Guideline) -> str:
    if rate < guideline.high_risk_below:
        return "high"
    if rate < guideline.medium_risk_below:
        return "medium"
    return "low"


def _escalate(level: str) -> str:
    return {"low": "medium", "medium": "high"}.get(level, level)


def evaluate_class_day(
    class_id: str,
    on: date,
    *,
    enrollments,
    children_by_id,
    doses,
    deferrals,
    guidelines,
    seasons,
    exposures,
    region: str | None = None,
) -> ClassDay:
    member_ids = members_on(class_id, enrollments, on)
    guideline = select_guideline(guidelines, region, on)
    season = active_season(seasons, region, on)
    exposure = (
        exposure_active(class_id, exposures, on, guideline.exposure_window_days)
        if guideline is not None else None
    )
    individuals = [
        classify_individual(children_by_id[cid], doses, deferrals, guideline, on)
        for cid in member_ids if cid in children_by_id
    ]
    denominator = len(member_ids)
    uncovered: dict[str, int] = {}
    full = waning = 0
    for status in individuals:
        if status.category == "full":
            full += 1
        elif status.category == "waning":
            waning += 1
        else:
            uncovered[status.category] = uncovered.get(status.category, 0) + 1
    covered = full + waning

    reasons: list[str] = []
    if guideline is None:
        level = "insufficient"
        reasons.append("当日无生效指南版本，无法判定保护状态")
    elif denominator < guideline.min_class_size:
        level = "insufficient"
        reasons.append(
            f"班级在籍 {denominator} 人，低于最小群体人数 {guideline.min_class_size}，"
            "为避免识别个体不披露班级风险"
        )
    elif season is None:
        level = "off_season"
        reasons.append("当前不在地区流行季假设区间内")
    else:
        level = _risk_for_rate(covered / denominator if denominator else 0.0, guideline)
        reasons.append(
            f"{season.name}：覆盖率 {covered}/{denominator}，"
            f"采用指南 {guideline.version}（{guideline.effective_from} 生效）"
        )
        for category, count in sorted(uncovered.items()):
            reasons.append(f"{RULE_CATEGORIES[category]}：{count} 人")
        exposed = exposure_active(class_id, exposures, on, guideline.exposure_window_days)
        if exposed:
            before = level
            level = _escalate(level)
            if level != before:
                reasons.append(
                    f"{exposed.occurred_on} 出现班内暴露事件（{exposed.description or '流感病例'}），"
                    f"{guideline.exposure_window_days} 天观察窗内风险上调一级"
                )
            else:
                reasons.append(
                    f"{exposed.occurred_on} 出现班内暴露事件，维持 {level} 风险"
                )

    return ClassDay(
        class_id=class_id, day=on, risk_level=level, denominator=denominator,
        covered=covered, covered_full=full, covered_waning=waning,
        uncovered_by_category=uncovered, individuals=individuals,
        season_name=season.name if season else "",
        guideline_id=guideline.guideline_id if guideline else "",
        guideline_version=guideline.version if guideline else "",
        exposure=exposure is not None,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 区间推演与提前/延后接种对比
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageInterval:
    start: date
    end: date
    covered: bool
    risk_level: str = ""
    uncovered_category: str = ""

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def class_intervals(
    class_id: str, start: date, end: date, **facts
) -> list[CoverageInterval]:
    """逐日推演并把连续相同状态合并成区间（供覆盖区间展示与对比）。"""
    if end < start:
        return []
    intervals: list[CoverageInterval] = []
    cursor = start
    previous: ClassDay | None = None
    span_start = start

    def is_protected(day_result: ClassDay) -> bool:
        # 班级覆盖区间口径：低风险视为已覆盖；非流行季没有流行暴露，同样记为覆盖。
        return day_result.risk_level in ("low", "off_season")

    def flush(span_end: date, day_result: ClassDay | None) -> None:
        if day_result is None:
            return
        category = ""
        if not is_protected(day_result) and day_result.denominator:
            # 区间内未覆盖主类别取人数最多者
            category = max(
                day_result.uncovered_by_category.items(),
                key=lambda kv: kv[1], default=("", 0),
            )[0]
        intervals.append(CoverageInterval(
            start=span_start, end=span_end,
            covered=is_protected(day_result),
            risk_level=day_result.risk_level, uncovered_category=category,
        ))

    while cursor <= end:
        current = evaluate_class_day(class_id, cursor, **facts)
        same = (
            previous is not None
            and is_protected(current) == is_protected(previous)
            and current.risk_level == previous.risk_level
        )
        if not same:
            flush(cursor - DAYS, previous)
            span_start = cursor
        previous = current
        cursor += DAYS
    flush(end, previous)
    return intervals


def shift_doses(doses, delta_days: int):
    """返回把所有接种日整体平移 delta 天后的剂次（假设情景，不落库）。"""
    return [
        VaccineDose(
            child_id=d.child_id, dose_seq=d.dose_seq,
            vaccinated_on=(to_date(d.vaccinated_on) + timedelta(days=delta_days)).isoformat(),
            vaccine=d.vaccine, cert_business_key=d.cert_business_key,
        )
        for d in doses
    ]


def compare_timing(
    class_id: str,
    start: date,
    end: date,
    *,
    early_days: int = 14,
    late_days: int = 14,
    doses,
    **facts,
) -> dict:
    """比较提前/延后接种对班级覆盖区间的实际差别。

    返回基准、提前、延后三套区间与覆盖天数，以及提前相对延后多覆盖的日期。
    """
    def scenario(label: str, shifted_doses) -> dict:
        intervals = class_intervals(
            class_id, start, end, doses=shifted_doses, **facts
        )
        covered_days = sum(i.days for i in intervals if i.covered)
        level_days: dict[str, int] = {}
        for item in intervals:
            level_days[item.risk_level] = (
                level_days.get(item.risk_level, 0) + item.days
            )
        return {
            "label": label,
            "covered_days": covered_days,
            "risk_level_days": level_days,
            "intervals": [
                {
                    "start": i.start.isoformat(), "end": i.end.isoformat(),
                    "covered": i.covered, "risk_level": i.risk_level,
                    "uncovered_category": i.uncovered_category,
                    "days": i.days,
                }
                for i in intervals
            ],
        }

    baseline = scenario("基准", doses)
    early = scenario(f"提前 {early_days} 天", shift_doses(doses, -early_days))
    late = scenario(f"延后 {late_days} 天", shift_doses(doses, late_days))

    early_days_set: set[date] = set()
    late_days_set: set[date] = set()
    for group, target in ((early, early_days_set), (late, late_days_set)):
        for item in group["intervals"]:
            if not item["covered"]:
                continue
            cursor = to_date(item["start"])
            stop = to_date(item["end"])
            while cursor <= stop:
                target.add(cursor)
                cursor += DAYS
    gained = sorted(early_days_set - late_days_set)
    lost = sorted(late_days_set - early_days_set)
    return {
        "class_id": class_id,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "baseline": baseline,
        "early": early,
        "late": late,
        "early_vs_late_gained_days": [d.isoformat() for d in gained],
        "early_vs_late_lost_days": [d.isoformat() for d in lost],
        "covered_day_delta": early["covered_days"] - late["covered_days"],
    }
