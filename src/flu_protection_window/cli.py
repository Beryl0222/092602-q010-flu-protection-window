"""保护窗口推演命令行。

示例：
    python -m flu_protection_window.cli load data/scenario.json --db data/app.db
    python -m flu_protection_window.cli advise --class class-1-1 \
        --date 2026-09-15 --db data/app.db
    python -m flu_protection_window.cli compare --class class-1-1 \
        --start 2026-09-01 --end 2027-03-31 --db data/app.db
    python -m flu_protection_window.cli run-due --date 2026-10-15 --db data/app.db

早期 ``validate`` 子命令保持兼容。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clock import Clock
from .service import PermissionError_, Service
from .store import Store

DEFAULT_DB = "flu_window.db"
RISK_LABEL = {"high": "高风险", "medium": "中风险", "low": "低风险",
              "off_season": "非流行季", "insufficient": "人数不足/不披露"}


def _service(args) -> Service:
    return Service(Store(args.db), Clock(args.date) if getattr(args, "date", None) else Clock())


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_validate(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    print(json.dumps(Service().register(payload), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_load(args) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    service = _service(args)
    counts = service.load_scenario(payload)
    payload_out = {"loaded": counts, "db": args.db,
                   "pending_triggers": service.pending_trigger_count()}
    if args.json:
        _print_json(payload_out)
    else:
        print(f"已载入情景 {args.file}（数据库 {args.db}）")
        for key, value in sorted(counts.items()):
            print(f"  - {key}: {value}")
        print(f"待触发窗口变化：{service.pending_trigger_count()} 条")
    return 0


def cmd_advise(args) -> int:
    service = _service(args)
    result = service.advise_class(args.cls, args.date, role=args.role)
    if args.json:
        _print_json(result)
        return 0
    print(f"推演日期：{result['as_of']}　班级：{args.cls}")
    if args.role == "teacher":
        # 班主任界面只呈现班级风险级别，不呈现指南版本、人数明细或个体通知。
        if result["risk_level"] == "insufficient":
            print("班级风险级别：暂不披露（在班人数低于最小群体人数）")
        else:
            print(f"班级风险级别：{RISK_LABEL.get(result['risk_level'], result['risk_level'])}")
        class_notes = [n for n in result.get("notifications", [])
                       if n.get("audience") == "teacher"]
        for item in class_notes:
            print(f"班级通知：[{item['status']}] {item['reason_code']}")
        return 0
    guideline = result["guideline"]
    if guideline:
        print(f"采用指南：{guideline['version']}"
              f"（{guideline['guideline_id']}，{guideline['effective_from']} 生效，"
              f"适用地区：{guideline['region']}）")
    else:
        print("采用指南：无（当日没有生效版本）")
    print(f"风险级别：{RISK_LABEL.get(result['risk_level'], result['risk_level'])}")
    print("提醒原因：")
    for reason in result["reasons"]:
        print(f"  - {reason}")
    if args.role != "teacher":
        if result.get("uncovered_by_rule"):
            total = sum(item["count"] for item in result["uncovered_by_rule"].values())
            print(f"未计入保护：{total} 人，规则类别：")
            for category, item in result["uncovered_by_rule"].items():
                print(f"  - {item['label']}（{category}）：{item['count']} 人")
        elif result["denominator"]:
            print("未计入保护：0 人")
        print(f"覆盖率：{result['covered']}/{result['denominator']}"
              f"（{result['coverage_rate']:.0%}，其中衰减期 "
              f"{result['covered_waning']} 人仍计入）")
    notifications = result.get("notifications", [])
    if notifications:
        print("通知处理：")
        for item in notifications:
            print(f"  - [{item['status']}] {item['audience']}：{item['reason_code']}"
                  f"（{item['notification_id']}）")
    return 0


def cmd_detail(args) -> int:
    service = _service(args)
    result = service.individual_detail(
        args.child, args.date, role=args.role, guardian_id=args.guardian_id
    )
    if args.json:
        _print_json(result)
        return 0
    print(f"推演日期：{service.clock.iso()}　儿童：{args.child}")
    print(f"状态：{result['category_label']}（{result['category']}）")
    print(f"说明：{result['detail']}")
    for key, label in (
        ("guideline_version", "采用指南"),
        ("class_id", "当日所在班级"),
        ("effective_dose_on", "末次有效接种"),
        ("protection_starts_on", "保护开始"),
        ("wanes_on", "进入衰减"),
        ("expires_on", "保护到期"),
    ):
        if result.get(key):
            print(f"{label}：{result[key]}")
    return 0


def cmd_compare(args) -> int:
    service = _service(args)
    result = service.compare_class(
        args.cls, args.start, args.end,
        early_days=args.early, late_days=args.late,
    )
    if args.json:
        _print_json(result)
        return 0
    span = result["range"]
    print(f"班级 {args.cls} 在 {span['start']} 至 {span['end']} 的接种时点对比：")
    order = ("high", "medium", "low", "off_season", "insufficient")
    for key in ("baseline", "early", "late"):
        scenario = result[key]
        level_days = scenario.get("risk_level_days", {})
        breakdown = "，".join(
            f"{RISK_LABEL.get(level, level)} {level_days[level]} 天"
            for level in order if level in level_days
        )
        print(f"  {scenario['label']}：{breakdown}")
    delta = result["covered_day_delta"]
    print(f"提前 {args.early} 天相比延后 {args.late} 天："
          f"班级低风险覆盖天数差 {delta:+d} 天")
    base_high = result["baseline"]["risk_level_days"].get("high", 0)
    early_high = result["early"]["risk_level_days"].get("high", 0)
    late_high = result["late"]["risk_level_days"].get("high", 0)
    print(f"高风险天数：提前 {early_high} 天 / 基准 {base_high} 天 / "
          f"延后 {late_high} 天（提前比延后少 {late_high - early_high} 天）")
    if result["early_vs_late_gained_days"]:
        gained = result["early_vs_late_gained_days"]
        print(f"  提前才覆盖的日期（{len(gained)} 天）："
              f"{gained[0]} … {gained[-1]}")
    if result["early_vs_late_lost_days"]:
        lost = result["early_vs_late_lost_days"]
        print(f"  延后才覆盖的日期（{len(lost)} 天）："
              f"{lost[0]} … {lost[-1]}")
    return 0


def cmd_run_due(args) -> int:
    service = _service(args)
    processed = service.run_due(args.date)
    if args.json:
        _print_json({"as_of": args.date, "processed": processed,
                     "pending_remaining": service.pending_trigger_count()})
        return 0
    print(f"推演至 {args.date}，处理到期窗口变化 {len(processed)} 条，"
          f"剩余待触发 {service.pending_trigger_count()} 条：")
    for item in processed:
        classes = ",".join(item["affected_classes"]) or "-"
        print(f"  - {item['fire_date']} {item['kind']} "
              f"儿童={item['child_id'] or '-'} 班级={classes}，"
              f"产生通知 {len(item['notifications'])} 条")
    return 0


def cmd_reviews(args) -> int:
    service = _service(args)
    items = service.pending_reviews()
    if args.json:
        _print_json(items)
        return 0
    print(f"待隐私复核凭证：{len(items)} 条")
    for item in items:
        print(f"  - {item['review_id']}：业务标识 {item['business_key']}，"
              f"儿童 {item['child_id']}，原因 {item['reason_code']}")
    return 0


def cmd_notifications(args) -> int:
    service = _service(args)
    items = service.list_notifications(args.target)
    if args.json:
        _print_json(items)
        return 0
    print(f"通知记录：{len(items)} 条")
    for item in items:
        print(f"  - [{item['status']}] {item['as_of_date']} "
              f"{item['audience']} <- {item['reason_code']} "
              f"（指南 {item['guideline_version']}，目标 {item['target_id']}）")
    return 0


def cmd_transfer(args) -> int:
    service = _service(args)
    result = service.transfer_child(args.child, args.to_class, args.date)
    _print_json(result)
    return 0


def cmd_resolve_review(args) -> int:
    service = _service(args)
    result = service.resolve_review(args.review_id, approve=not args.reject)
    _print_json(result)
    return 0


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="儿童流感保护窗口推演台")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="兼容旧契约：登记一条基础记录")
    p.add_argument("file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("load", help="批量载入情景数据")
    p.add_argument("file")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--date", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("advise", help="按日期推演班级提醒")
    p.add_argument("--class", dest="cls", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--role", default="school_doctor",
                   choices=("school_doctor", "child_health_office", "teacher"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_advise)

    p = sub.add_parser("detail", help="查看个体保护细节（需授权角色）")
    p.add_argument("--child", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--role", default="school_doctor",
                    choices=("school_doctor", "child_health_office", "guardian"))
    p.add_argument("--guardian", dest="guardian_id", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("compare", help="比较提前/延后接种对班级覆盖区间的差别")
    p.add_argument("--class", dest="cls", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--early", type=int, default=14)
    p.add_argument("--late", type=int, default=14)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("run-due", help="处理截至某日尚未触发的窗口变化")
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run_due)

    p = sub.add_parser("reviews", help="列出待隐私复核凭证")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reviews)

    p = sub.add_parser("resolve-review", help="处理隐私复核项")
    p.add_argument("review_id")
    p.add_argument("--reject", action="store_true", help="默认通过；加该参数为驳回")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--date", default=None)
    p.set_defaults(func=cmd_resolve_review)

    p = sub.add_parser("notifications", help="查看已发出与被抑制的通知")
    p.add_argument("--target", default=None)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_notifications)

    p = sub.add_parser("transfer", help="登记转班并只重算相关班级")
    p.add_argument("--child", required=True)
    p.add_argument("--to-class", dest="to_class", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_transfer)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PermissionError_ as exc:
        print(f"授权不足：{exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
