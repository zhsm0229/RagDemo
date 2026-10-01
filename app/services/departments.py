r"""部门解析服务（规范审查收敛项）：全员部门 id 与 code→id 的单一事实来源。

此前 api 层 4 份同形状实现（api/chat.py._all_dept_id、api/citations.py._all_dept_id、
api/documents.py._resolve_dept、api/admin_eval.py._resolve_perspective_dept）——重复且
把查询留在 api 层（§1.6 api 禁直写查询）。本模块收口后各端点只调用这里；两种失败
语义有意分开：
- all_dept_id：F-502 隔离面恒含全员部门，缺失属环境装配错误 → 500 兜底拒绝；
- resolve_dept_id：调用方传入的 code 是用户/管理员输入，枚举外 → 422/1003 字段级。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_INTERNAL, ERR_PARAM_INVALID, ApiError
from app.db.models import Department

ALL_DEPT_CODE = "all"  # 全员部门 code（F-502：检索范围恒为 [用户部门, 全员]）
_MSG_SEED_MISSING = "部门种子缺失，请联系管理员"


def find_dept_id(db: Session, code: str) -> int | None:
    """code → dept_id，不存在返回 None（不做错误转换，供调用方按语义分支）。"""
    return db.scalar(select(Department.id).where(Department.code == code))


def all_dept_id(db: Session) -> int:
    """全员部门 id（F-502）：种子幂等存在，缺失属环境装配错误按 500 兜底拒绝。"""
    dept_id = find_dept_id(db, ALL_DEPT_CODE)
    if dept_id is None:
        raise ApiError(ERR_INTERNAL, _MSG_SEED_MISSING, 500)
    return dept_id


def resolve_dept_id(db: Session, code: str) -> int:
    """部门 code → dept_id：枚举外 422 中文原因（上传表单/管理端选择的输入校验口径）。"""
    dept_id = find_dept_id(db, code)
    if dept_id is None:
        raise ApiError(ERR_PARAM_INVALID, f"部门编码无效：{code}（可选 service/hr/all）", 422)
    return dept_id
