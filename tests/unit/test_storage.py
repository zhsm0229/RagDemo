"""本地文件存储测试：UUID 两级目录落盘、sha256 指纹、"xb" 防覆盖。

断言表来源 task-5-brief：rel_path 以 .pdf 结尾且含 UUID（两级目录 ab/cd/）、
sha256 与手算一致、文件真实落盘可读回、同内容两次保存路径不同不覆盖、ext 无点自动补。
"""

import hashlib
import re
from pathlib import Path

import pytest

from app.core.storage import FileStorage, LocalFileStorage, StoredFile

CONTENT = b"pdf-bytes"
EXPECTED_SHA256 = hashlib.sha256(CONTENT).hexdigest()  # 手算参照值，与实现无关
UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(root_dir=tmp_path)


class TestSave:
    def test_rel_path_is_two_level_uuid_dir_with_pdf_ext(
        self, storage: LocalFileStorage
    ) -> None:
        stored = storage.save(CONTENT, ext=".pdf")
        first, second, name = Path(stored.rel_path).parts
        assert stored.rel_path.endswith(".pdf")
        assert len(first) == 2 and len(second) == 2
        assert first == name[:2] and second == name[2:4]  # 两级目录取 uuid 前 4 位
        assert UUID_HEX_RE.fullmatch(name.removesuffix(".pdf")) is not None

    def test_sha256_and_size_match_content(self, storage: LocalFileStorage) -> None:
        stored = storage.save(CONTENT, ext=".pdf")
        assert stored.sha256 == EXPECTED_SHA256
        assert stored.size == len(CONTENT)

    def test_file_written_to_disk_and_readable_back(
        self, storage: LocalFileStorage
    ) -> None:
        stored = storage.save(CONTENT, ext=".pdf")
        assert (storage.root_dir / stored.rel_path).read_bytes() == CONTENT

    def test_same_content_twice_yields_two_paths_no_overwrite(
        self, storage: LocalFileStorage
    ) -> None:
        first = storage.save(CONTENT, ext=".pdf")
        second = storage.save(CONTENT, ext=".pdf")
        assert first.rel_path != second.rel_path
        assert (storage.root_dir / first.rel_path).read_bytes() == CONTENT
        assert (storage.root_dir / second.rel_path).read_bytes() == CONTENT

    @pytest.mark.parametrize("ext", ["pdf", ".pdf"])
    def test_ext_without_dot_is_normalized(
        self, storage: LocalFileStorage, ext: str
    ) -> None:
        stored = storage.save(CONTENT, ext=ext)
        assert stored.rel_path.endswith(".pdf")


def test_local_storage_satisfies_file_storage_protocol(tmp_path: Path) -> None:
    fs: FileStorage = LocalFileStorage(root_dir=tmp_path)
    assert isinstance(fs.save(b"x", ".txt"), StoredFile)
