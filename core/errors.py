"""统一异常定义。

所有 core 模块抛出的异常都从这里取，避免循环导入。
"""

from __future__ import annotations


class ZcyltError(Exception):
    """项目内所有异常的基类。"""


class BackendError(ZcyltError):
    """后端（ADB / 屏幕截取）连接或操作失败。"""


class VisionUnavailable(ZcyltError):
    """OpenCV / numpy 未安装，无法进行模板匹配。"""


class TaskSchemaError(ZcyltError):
    """任务 JSON 校验失败。errors 里是致命错误，warnings 里是提示。"""

    def __init__(self, errors, warnings=None, source=""):
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        self.source = source
        msg = "任务配置校验失败"
        if source:
            msg += f"（{source}）"
        msg += ":\n" + "\n".join(f"  - {e}" for e in self.errors[:20])
        if len(self.errors) > 20:
            msg += f"\n  ... 共 {len(self.errors)} 条"
        super().__init__(msg)


class TemplateMissing(ZcyltError):
    """模板图片不存在。"""


class AbortRun(ZcyltError):
    """中止整个流程（on_fail=abort 或收到停止信号）。"""


class NextTask(ZcyltError):
    """跳过当前任务，继续下一个（on_fail=next_task）。"""


class StepFailed(ZcyltError):
    """单步执行失败。"""
