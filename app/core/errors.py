r"""业务错误基座：ApiError(code, message, http_status)——全仓库唯一业务异常形态
（standards §2.5：禁止裸抛字符串/字典；响应体 `{code, message, data}`）。

错误码取自共享枚举（standards §3.2：1xxx 参数/文件、2xxx 鉴权、3xxx 业务、5xxx
系统）；本模块现收敛 1xxx 首批两枚，2xxx 鉴权码随鉴权任务在此扩充，全局异常
处理器（ApiError → 统一响应体、兜底 5xxx + request_id）同样在装配任务落地。
"""

ERR_UNSUPPORTED_FORMAT: int = 1001  # 1xxx 文件类：上传格式不支持（.doc 请另存为 docx 后上传）
ERR_PARSE_FAILED: int = 1002  # 1xxx 文件类：语料解析失败（包体非法/超限/含危险声明）


class ApiError(Exception):
    """业务错误：code 为稳定错误码（见模块 docstring 枚举约定），message 为面向
    用户的中文提示（技术细节进日志不进界面），http_status 为响应状态码。"""

    def __init__(self, code: int, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code: int = code
        self.message: str = message
        self.http_status: int = http_status
