"""评测层：golden 数据集、四层指标、LLM-as-Judge 校准、回归门禁。

四个子模块各管一件事：

* :mod:`ai_service.eval.golden`  —— 从平台标注构造 golden 集，并记录哪一层指标不可用
* :mod:`ai_service.eval.metrics` —— 四层指标计算 + baseline 回归门禁（纯函数，可单测）
* :mod:`ai_service.eval.judge`   —— 四维 rubric 打分（离线确定性版 / 在线模型版），含校准
* :mod:`ai_service.eval.report`  —— 把上面三样组装成一份可读报告

指标计算刻意与 CLI 分离：CLI 只负责取数、打印、写文件，
指标本身是纯函数 —— 这样「退化 >5% 要告警」这类规则可以被直接单测，
不用去跑一整套评测。
"""
