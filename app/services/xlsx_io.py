r"""Excel 整批校验基建：问答对导入（Task 3）与评测题导入（Task 5）共用。

职责单一（裁决）：read_rows 只做结构级校验与单元格取值——表头行=第 1 行，单元格
strip 后与 headers 逐字匹配（顺序不限），缺列/多列/重复列/空白表头一律结构级报错
（row=1，reason 指名哪一列）；数据行号从 2 起；空行跳过、不计行数、不报错；全空
文件报"文件无数据"（row=0）；超 max_rows 报"文件超过 N 行上限"（row=0），且先计数
后解析（read_only 迭代计数，防大文件整体载入的内存放大）。required 仅约束"列出现
在表头"这一结构事实，**单元格值空否由调用方业务校验**，本模块不感知字段语义。

所有单元格取值统一为 strip 后字符串（None→""，数字/日期 str()），调用方拿到的
就是"用户在 Excel 里写了什么"。错误聚合为 XlsxBatchError 一次性抛出，由 API 层
统一转 422/错误码 1003；本模块保持纯业务，禁止 import fastapi。
"""

from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet


class XlsxBatchError(Exception):
    """整批校验失败：聚合全部错误行后统一抛出。

    errors 元素形如 {"row": int, "field": str | None, "reason": str}；row 为
    1 基 Excel 行号（表头=1，数据从 2 起），文件级错误约定 row=0、field=None。
    """

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__(f"xlsx 整批校验失败，共 {len(errors)} 处")


def read_rows(
    data: bytes, headers: list[str], max_rows: int, *, required: set[str]
) -> list[dict[str, str]]:
    """读取 xlsx 字节流为行字典列表（表头名 → strip 后字符串）。

    校验优先级：结构（表头）→ 行数上限 → 无数据 → 逐行解析。结构错误按
    空白表头 → 缺列（按 headers 序）→ 多余列（按文件列序）→ 重复列（按文件
    列序）→ 缺必填列（按字母序）聚合返回，一次报全，避免用户改一轮传一轮。
    ragged 行（M2 评审记录）：短行尾部补空串，数据行超过表头列数的部分被截断。
    """
    found = _header_names(data)
    if not found:  # 0 行文件（或表头行整行为空）视同无数据，不走缺列报错
        raise XlsxBatchError([_file_error("文件无数据")])

    structural = _header_errors(headers, found, required)
    if structural:
        raise XlsxBatchError(structural)

    data_count = _count_data_rows(data)
    if data_count > max_rows:
        raise XlsxBatchError([_file_error(f"文件超过 {max_rows} 行上限")])
    if data_count == 0:
        raise XlsxBatchError([_file_error("文件无数据")])
    return _parse_data_rows(data, found)


def write_template(headers: list[str], sample_rows: list[list[str]]) -> bytes:
    """生成导入模板：新建 workbook，首行写表头，其后写示例行，返回 xlsx 字节流。"""
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.append(list(headers))
    for row in sample_rows:
        ws.append(list(row))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _file_error(reason: str) -> dict:
    return {"row": 0, "field": None, "reason": reason}


def _cell_str(value: object) -> str:
    """单元格 → 展示字符串：None→空串；字符串 strip；数字/日期等 str() 后 strip。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _open_sheet(data: bytes) -> tuple[Workbook, Worksheet]:
    """每次校验环节独立打开只读工作表，调用方负责 wb.close()（read_only 不落内存）。"""
    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    return wb, wb.worksheets[0]


def _header_names(data: bytes) -> list[str]:
    """首行表头（strip 后）；截掉尾部空列——Excel 清空单元格后维度残留会产生幽灵尾列。"""
    wb, ws = _open_sheet(data)
    try:
        row = next(ws.iter_rows(values_only=True), None)
    finally:
        wb.close()
    names = [_cell_str(v) for v in row] if row is not None else []
    while names and names[-1] == "":
        names.pop()
    return names


def _header_errors(headers: list[str], found: list[str], required: set[str]) -> list[dict]:
    """表头结构错误聚合：空白表头短路（列匹配已失真），其余分类逐列报全。"""
    if "" in found:
        return [{"row": 1, "field": None, "reason": "表头存在空白单元格"}]

    errors: list[dict] = []
    found_set = set(found)
    expected = set(headers)
    for col in headers:
        if col not in found_set:
            errors.append({"row": 1, "field": col, "reason": f"表头缺少列「{col}」"})
    for col in found:
        if col not in expected:
            errors.append({"row": 1, "field": col, "reason": f"表头多余列「{col}」"})
    seen: set[str] = set()
    for col in found:
        if col in seen:
            errors.append({"row": 1, "field": col, "reason": f"表头重复列「{col}」"})
        seen.add(col)
    for col in sorted(required - found_set):
        errors.append({"row": 1, "field": col, "reason": f"表头缺少必填列「{col}」"})
    return errors


def _count_data_rows(data: bytes) -> int:
    """有效数据行数（非空行）：先计数后解析的第一遍，只迭代不建对象。"""
    wb, ws = _open_sheet(data)
    try:
        rows = ws.iter_rows(values_only=True)
        next(rows, None)  # 跳过表头行
        return sum(1 for row in rows if any(_cell_str(v) != "" for v in row))
    finally:
        wb.close()


def _parse_data_rows(data: bytes, found: list[str]) -> list[dict[str, str]]:
    """第二遍解析：空行跳过；短行补空串保证键齐全；行序即文件序。"""
    wb, ws = _open_sheet(data)
    try:
        rows = ws.iter_rows(values_only=True)
        next(rows, None)  # 跳过表头行
        parsed: list[dict[str, str]] = []
        for row in rows:
            cells = [_cell_str(v) for v in row]
            if not any(cells):
                continue
            cells += [""] * (len(found) - len(cells))
            parsed.append(dict(zip(found, cells)))
        return parsed
    finally:
        wb.close()
