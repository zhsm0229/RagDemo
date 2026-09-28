r"""程序化生成样例 pdf（裸写最小 2 页 PDF，标准 14 字体 Helvetica，ASCII 文本）。

不引入 reportlab 等新依赖：手工拼装对象 + 计算 xref 偏移，pypdf 可直接提取。
用法：
    python tests/fixtures/tools/make_sample_pdf.py   # 写出 tests/fixtures/samples/sample.pdf
单测直接 import build_sample_pdf() 取字节。
"""

import io
from pathlib import Path

import pypdf


def _stream_obj(text: str) -> bytes:
    """内容流对象：单行 Helvetica 文本，/Length 按实际字节计。"""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    return f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"


def build_sample_pdf() -> bytes:
    """构造 2 页最小 pdf：第 1 页两句、第 2 页一句，对象 1-7（目录/页树/两页/两内容流/字体）。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
            b"/Resources << /Font << /F1 6 0 R >> >> >>"
        ),
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 7 0 R "
            b"/Resources << /Font << /F1 6 0 R >> >> >>"
        ),
        _stream_obj("Chapter 1 Overview. Total expense lines."),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream_obj("Appendix contact table."),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


def extract_pages(data: bytes) -> list[str]:
    """用 pypdf 提取各页文本（工具自带验尸入口，供生成后人工核对）。"""
    return [page.extract_text() or "" for page in pypdf.PdfReader(io.BytesIO(data)).pages]


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "samples" / "sample.pdf"
    out.write_bytes(build_sample_pdf())
    print(f"written: {out}\npages: {extract_pages(out.read_bytes())}")
