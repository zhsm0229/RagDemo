r"""文件名元数据建议（F-1008）：`部门_主题_日期.ext` → 上传表单预填建议三元组。

只读文件名，不读文件内容——从内容提取日期属超范围工程，文件名规则即验收口径
（"20 个规范命名 ≥18 正确"的主要面），本模块因此保持纯函数、无 IO。规则与 zip
批量导入（F-1007 parse_entry_meta）同源：主题可含下划线（自两端向中间拆解）、
日期白名单 YYYYMMDD / YYYY-MM-DD（严格位数 + 真实日期，拒 2026-9-1 缺前导零与
20261332 之类不存在的日期）；差异点：部门段中文名（客服/人力/全员）与编码
（service/hr/all）均收、归一为编码——建议供表单预填，提交仍走编码。部门别名
镜像种子枚举（app/db/seed 三部门），与 _resolve_dept 的枚举文案同源维护。任一段
不符返回 None：调用方呈现空建议，绝不产出错误建议（错建议比无建议更有害）。
"""

import re
from datetime import date
from pathlib import PureWindowsPath
from typing import NamedTuple

# 日期白名单：先严格位数（拒 2026-9-1 这类缺前导零），再验真实日期（同 F-1007 口径）
_DATE_PATTERNS = (re.compile(r"[0-9]{8}"), re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"))

# 部门别名 → 编码（编码与中文双收）：镜像种子枚举 service/hr/all，防越权注入的
# 第一道闸——枚举之外（如 legal）不映射即整体不符。zip 批量导入（F-1007）共用本表
# 归一部门段，两入口口径单一事实源，改动只落此处
DEPT_ALIASES: dict[str, str] = {
    "service": "service",
    "客服": "service",
    "hr": "hr",
    "人力": "hr",
    "all": "all",
    "全员": "all",
}


class SuggestedMeta(NamedTuple):
    """建议三元组：部门编码（库内枚举规范值）/ 主题 / 生效日期。"""

    dept_code: str
    title: str
    effective_date: date


def parse_filename_metadata(filename: str) -> SuggestedMeta | None:
    """解析文件名为表单预填建议；任一段不符返回 None（不产出错误建议）。

    管辖边界（§F-1008 防越权注入）：建议仅作表单预填，最终提交仍经上传端点的
    部门校验（_resolve_dept）；端点限 AdminUser——试点管理员管辖全部部门，故
    枚举内部门均可建议；部门→ACL 演进时此处须收紧为按上传人管辖部门过滤（§1.7）。
    """
    stem = PureWindowsPath(filename).stem
    left, _, date_token = stem.rpartition("_")
    if not left or not _valid_date_token(date_token):
        return None
    dept_token, sep, title = left.partition("_")
    dept_code = DEPT_ALIASES.get(dept_token)
    if not sep or not title or dept_code is None:
        return None
    return SuggestedMeta(dept_code, title, _parse_date(date_token))


def _valid_date_token(token: str) -> bool:
    """日期白名单两道闸：严格位数正则 + 真实存在的日期（同 F-1007 口径）。"""
    return (
        any(pattern.fullmatch(token) for pattern in _DATE_PATTERNS)
        and _parse_date(token) is not None
    )


def _parse_date(token: str) -> date | None:
    """白名单格式解析为日期；不存在日期（如 20261332）返回 None。"""
    try:
        if len(token) == 8:
            return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
        return date(int(token[:4]), int(token[5:7]), int(token[8:10]))
    except ValueError:
        return None
