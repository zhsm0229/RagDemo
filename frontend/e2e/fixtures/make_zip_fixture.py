"""生成 zip 批量导入 E2E 夹具（一次运行，产物 批量导入_混合样例.zip 提交入 git）。

包内两份 docx 内容独立撰写（不得与 e2e_seed 预置文档重复，否则 duplicate 干扰断言）：
- 客服_退货话术标准_20260101.docx → accepted（文件名符合 部门_主题_日期，自动补元数据）
- 乱命名无元数据.docx → skipped_metadata（stem 无下划线结构，解析不出部门/日期）

用法：.venv/Scripts/python.exe frontend/e2e/fixtures/make_zip_fixture.py
依赖：后端 dev 依赖里的 python-docx（与 tests/api/test_zip_import.py 同源）。
"""

import zipfile
from pathlib import Path

from docx import Document

OUT = Path(__file__).parent / "files" / "批量导入_混合样例.zip"

BODY = (
    "退货话术标准。客户提出退货申请时，客服应当首先核对订单号与购买日期，确认商品是否在"
    "七天无理由退货范围内。生鲜食品与定制类商品不支持无理由退货，需向客户说明并引导协商。"
    "退货申请通过后，客服需告知客户退货地址与包装要求：商品须保持原包装完好，附件、赠品、"
    "发票一并寄回。仓库收到退货后将在三个工作日内完成验收，验收合格后退款原路返回，"
    "预计一到五个工作日到账。若客户对退款进度有疑问，客服应引导客户在订单详情页查看"
    "退款状态，或登记工单由售后专员跟进。换货流程参照退货执行：客户需在收货后七天内提出，"
    "换货商品以同型号同规格为限，如需更换其他型号请按退货后重新下单办理。节假日与大促期间，"
    "退货申请量较大，处理时限可能顺延，客服应主动向客户说明预计等待时间并做好安抚。"
    "本标准自发布之日起执行，各班组应在晨会传达学习，执行中的问题统一反馈至运营组汇总修订。"
)


def make_docx(path_in_zip: str, buffer: zipfile.ZipFile) -> None:
    """python-docx 只认真实路径/文件对象，写 zip 内条目需经 BytesIO 中转。"""
    import io

    doc = Document()
    doc.add_heading("退货话术标准", level=1)
    doc.add_paragraph(BODY)
    bio = io.BytesIO()
    doc.save(bio)
    buffer.writestr(path_in_zip, bio.getvalue())


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        make_docx("客服_退货话术标准_20260101.docx", zf)
        make_docx("乱命名无元数据.docx", zf)
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
