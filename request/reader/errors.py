class ReaderError(RuntimeError):
    """搜索或读取笔记失败。"""


class LoginRequiredError(ReaderError):
    """需要在独立浏览器中登录。"""


class VerificationRequiredError(ReaderError):
    """网页要求完成交互验证。"""


class DownloadError(ReaderError):
    """媒体下载失败。"""


class NetworkReadError(ReaderError):
    """有限重试后网络仍失败，当前任务应停止。"""
