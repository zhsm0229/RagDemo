"""格式准入预检测试（F-1013/F-1014）：新格式白名单、OOXML 判型防改名混入、宏拒收、
文本类 UTF-8/NUL 校验。check_sync 为纯函数（无库依赖）。"""

import io
import zipfile

from app.rag.precheck import check_sync

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _ooxml_with_content_type(family: str, vba: bool = False) -> bytes:
    """构造最小 OOXML 包（真 zip）：[Content_Types].xml 含家族标记，可选 vbaProject 宏条目。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{family}</Types>")
        if vba:
            archive.writestr("word/vbaProject.bin", b"macro-payload")
    return buffer.getvalue()


class TestNewFormatWhitelist:
    def test_md_txt_csv_html_text_content_accepted(self) -> None:
        for ext, mime, body in [
            (".md", "text/markdown", "# 标题\n正文".encode()),
            (".txt", "text/plain", "纯文本内容".encode()),
            (".csv", "text/csv", "列1,列2\n值1,值2".encode()),
            (".html", "text/html", "<p>正文</p>".encode()),
        ]:
            result = check_sync(f"样例{ext}", mime, body)
            assert result.ok, (ext, result.reason)

    def test_md_mime_variant_accepted(self) -> None:
        """md 的 text/plain 变体放行（不同工具给出的 MIME 不一致）。"""
        assert check_sync("说明.md", "text/plain", "内容".encode()).ok

    def test_txt_with_nul_rejected_as_binary(self) -> None:
        result = check_sync("伪装.txt", "text/plain", b"text\x00binary")
        assert not result.ok and result.reason == "magic_mismatch"

    def test_gbk_txt_rejected(self) -> None:
        result = check_sync("gbk.txt", "text/plain", "中文".encode("gbk"))
        assert not result.ok and result.reason == "magic_mismatch"


class TestOoxmlFamilyTyping:
    def test_xlsx_pptx_accepted(self) -> None:
        assert check_sync("表.xlsx", XLSX_MIME, _ooxml_with_content_type("spreadsheetml")).ok
        assert check_sync("片.pptx", PPTX_MIME, _ooxml_with_content_type("presentationml")).ok

    def test_renamed_xlsx_to_docx_rejected(self) -> None:
        """改名混入洞（F-1014）：xlsx 内容改名 .docx → 判型不符 422。"""
        result = check_sync("改名.docx", DOCX_MIME, _ooxml_with_content_type("spreadsheetml"))
        assert not result.ok and result.reason == "magic_mismatch"

    def test_renamed_docx_to_pptx_rejected(self) -> None:
        result = check_sync("改名.pptx", PPTX_MIME, _ooxml_with_content_type("wordprocessingml"))
        assert not result.ok and result.reason == "magic_mismatch"

    def test_docx_still_accepted(self) -> None:
        """既有 docx 行为不回归：家族匹配放行。"""
        assert check_sync("文档.docx", DOCX_MIME, _ooxml_with_content_type("wordprocessingml")).ok


class TestMacroRejection:
    def test_macro_extensions_rejected(self) -> None:
        for ext, mime in [
            (".xlsm", XLSX_MIME),
            (".docm", DOCX_MIME),
            (".pptm", PPTX_MIME),
        ]:
            result = check_sync(f"含宏{ext}", mime, b"PK\x03\x04")
            assert not result.ok and result.reason == "macro_not_allowed", ext

    def test_vba_entry_in_package_rejected(self) -> None:
        """扩展名合规但包内带 vbaProject.bin → 判型扫描拒收（双闸第二道）。"""
        result = check_sync(
            "带宏.xlsx", XLSX_MIME, _ooxml_with_content_type("spreadsheetml", vba=True)
        )
        assert not result.ok and result.reason == "macro_not_allowed"


class TestLegacyFormatsEnterConvertChain:
    def test_doc_ppt_xls_ole_magic_accepted(self) -> None:
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"payload"
        assert check_sync("旧文档.doc", "application/msword", ole).ok
        assert check_sync("旧表.xls", "application/vnd.ms-excel", ole).ok
        assert check_sync("旧稿.ppt", "application/vnd.ms-powerpoint", ole).ok

    def test_doc_wrong_magic_rejected(self) -> None:
        result = check_sync("坏.doc", "application/msword", b"not-ole")
        assert not result.ok and result.reason == "magic_mismatch"
