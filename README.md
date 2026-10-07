# 儿童流感保护窗口推演台

面向区域儿童保健部门与校医的服务端推演工具：在“已接种”三个字之外，区分
接种后两到四周免疫空白期、剂次不足、保护衰减/到期、医学暂缓等未计入保护的
规则类别；结合年度指南版本、北方流行季假设、转班归属时段与班内暴露事件，
用**可调时钟**推演任意日期的个体与班级保护状态。

## 业务口径

- **保护窗口**：接种后 `gap_max_days`（默认 28 天，即四周上限）为免疫空白期，
  不计入保护；其后为完整保护期（默认 180 天），再进入衰减期（默认 60 天，
  仍计入班级覆盖），衰减结束即到期。
- **剂次规则**：首剂接种时年龄小于 `two_dose_under_months` 月龄（默认 9 岁以下
  口径 108 月龄）需两剂，两剂间隔不少于 `min_dose_interval_days`；间隔不足的
  第二剂暂不计入。
- **指南版本化**：指南带 `effective_from` 与地区。新版本只改变生效日之后的
  预测；历史接种事实与已经发出的通知永不改写（重复推演只做幂等去重）。
- **流行季**：地区流行季区间允许跨年（北方 10 月至次年 3 月）；非流行季班级
  结果为 `off_season`。
- **暴露事件**：班内病例后 `exposure_window_days` 观察窗内，班级风险上调一级。
- **最小群体人数**：面向班主任的结果必须达到 `min_class_size`，不足时只返回
  `insufficient`，不披露任何人数或个体信息；达标时班主任也**只看到班级风险
  级别**。
- **角色授权**：个体健康细节仅对 `school_doctor`、`child_health_office` 及持有
  有效家长授权的监护人开放；班主任访问个体细节直接被拒绝。
- **凭证去重与复核**：凭证按业务标识 `business_key` 幂等去重；同标识内容冲突
  时新凭证置 `conflict_pending` 并入隐私复核队列，原接种事实保持不变，复核
  通过后才修正事实。
- **增量重算**：转班、补种、暂缓变化只重算相关儿童与相关班级，过程写入
  `recompute_log`。
- **可恢复触发器**：空白期结束、进入衰减、保护到期、暂缓结束、流行季开始
  都落库为 `due_changes`；服务重启后 `run-due` 继续处理尚未触发的变化。

## 目录

- `src/flu_protection_window/domain.py` — 领域记录与规则类别常量
- `src/flu_protection_window/clock.py` — 可调时钟
- `src/flu_protection_window/engine.py` — 纯函数推演引擎（版本选择、个体分类、班级风险、区间合并、时点对比）
- `src/flu_protection_window/store.py` — SQLite 事务存储（含复核队列与触发器表）
- `src/flu_protection_window/service.py` — 应用服务（授权、去重、增量重算、通知）
- `src/flu_protection_window/cli.py` — 命令行入口
- `contracts/` — 输入契约说明；`data/scenario.json` — 可运行样例
- `tests/` — 业务行为测试

## 运行

测试：

```
PYTHONPATH=src python3 -m unittest discover -s tests
```

语法检查：

```
python3 -m compileall -q src tests
```

命令行推演（数据库为本地 SQLite 文件，重启后状态保留）：

```
PYTHONPATH=src python3 -m flu_protection_window.cli load data/scenario.json --db /tmp/flu.db
# 校医视图：提醒原因、采用指南、未计入人数及规则类别
PYTHONPATH=src python3 -m flu_protection_window.cli advise --class class-1-1 \
    --date 2026-10-12 --db /tmp/flu.db
# 班主任视图：只有风险级别
PYTHONPATH=src python3 -m flu_protection_window.cli advise --class class-1-1 \
    --date 2026-10-12 --role teacher --db /tmp/flu.db
# 处理尚未触发的窗口变化（重启后续跑同理）
PYTHONPATH=src python3 -m flu_protection_window.cli run-due --date 2026-11-15 --db /tmp/flu.db
# 比较提前/延后接种对班级覆盖区间的实际差别
PYTHONPATH=src python3 -m flu_protection_window.cli compare --class class-1-1 \
    --start 2026-09-01 --end 2027-03-31 --db /tmp/flu.db
# 隐私复核队列
PYTHONPATH=src python3 -m flu_protection_window.cli reviews --db /tmp/flu.db
```

项目只使用 Python 标准库与本地 SQLite。
