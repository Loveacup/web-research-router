"""WRR 异常层级。

EngineError 通常可触发 fallback；FetchPolicyError 为终止性拒绝；AllEnginesFailedError 表示整链失败。
注：超时类命名 EngineTimeoutError，避免遮蔽内建 TimeoutError。
"""
from enum import Enum



class WRRError(Exception):
    """所有 WRR 异常基类。"""


class EngineError(WRRError):
    """单引擎调用失败（可触发 fallback）。"""


class RateLimitError(EngineError):
    """引擎限流（HTTP 429/432 等），可触发 fallback。"""


class EngineTimeoutError(EngineError):
    """引擎超时（可触发 fallback）。"""


class FetchPolicyError(EngineError):
    """Fetch authorization denial: terminal, never a fallback invitation."""

class TerminalReason(str, Enum):
    """Closed machine-readable reasons that must stop downstream fallback."""

    FETCH_POLICY_DENIED = "fetch_policy_denied"



class AllEnginesFailedError(WRRError):
    """fallback 链全部 provider 失败。"""

    def __init__(self, message: str, *, diagnostics=None,
                 terminal_reason: TerminalReason | None = None):
        if terminal_reason is not None and not isinstance(terminal_reason, TerminalReason):
            raise TypeError("terminal_reason must be a TerminalReason")
        super().__init__(message)
        self.diagnostics = diagnostics
        self.terminal_reason = terminal_reason
