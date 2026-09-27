"""CTE（Continuous Test Evolution）：证据驱动、验证门控的持续测试演进。

定位
----
**测试资产的孵化器，不是第二套测试系统。** 本包只拥有「演进过程证据」
（events / predictions / retros / candidates / validated / rejected / reports）,
正式测试数据的主权仍归现有体系：promotion 后的产物分别进
``tests/``、threat 数据集、golden 集、eval baseline，**不在这里另存一份**。

边界
----
CTE 可以发现、分析、提议、验证；不能修改 Ground Truth、Evaluator、
安全策略或生产业务规则。这条边界的落地方式不是文件权限（单人仓库里
那只是自欺），而是：**本包不存在指向这些资产的写路径**。
见 :mod:`test_evolution.schema` 的模块 docstring。

阶段
----
CTE-0 是本包（骨架 + schema + 验证矩阵）。
CTE-1 是第一个闭环：Knowledge / Threat 面的事件跑通
Event → Predict → Execute → Compare → Reflect → Candidate → Validate → Promote。
"""

from test_evolution.schema import (
    BLOCKED_SURFACES,
    CANDIDATE_TYPES,
    EVENT_SOURCES,
    EVENT_SURFACES,
    VALIDATION_MATRIX,
    Candidate,
    Event,
    Prediction,
    SchemaError,
)

__all__ = (
    "BLOCKED_SURFACES",
    "CANDIDATE_TYPES",
    "EVENT_SOURCES",
    "EVENT_SURFACES",
    "VALIDATION_MATRIX",
    "Candidate",
    "Event",
    "Prediction",
    "SchemaError",
)
