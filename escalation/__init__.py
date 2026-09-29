"""升学里程碑逾期升级库。

上层升学指导平台直接以库的形式调用，无接口层。
数据持久化在本地单个 SQLite 文件中。
"""

from .service import EscalationService, EvaluationDateError, InvalidOperationError

__all__ = ["EscalationService", "EvaluationDateError", "InvalidOperationError"]
