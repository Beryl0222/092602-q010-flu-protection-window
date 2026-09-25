# 儿童流感保护窗口推演

本项目提供儿童流感保护窗口推演的服务端领域基础，当前包含领域记录、SQLite 事务存储、登记与查询服务、JSON 样例和命令行入口。基础能力刻意保持精简，便于后续围绕真实业务状态扩展，同时保留可执行的契约和测试。

## 目录

- `src/flu_protection_window/domain.py` 定义已有记录。
- `src/flu_protection_window/store.py` 管理 SQLite 表和事务写入。
- `src/flu_protection_window/service.py` 提供登记、查询与健康检查。
- `contracts/record.json` 说明现有输入契约。
- `data/sample.json` 提供本地冒烟数据。
- `tests/` 覆盖登记、查询和重复编号边界。

## 运行

运行测试：`PYTHONPATH=src python3 -m unittest discover -s tests`

检查源码：`python3 -m compileall -q src tests`

验证样例：`PYTHONPATH=src python3 -m flu_protection_window.cli validate data/sample.json`

项目只使用 Python 标准库和本地 SQLite 文件，不需要连接其他运行服务。
