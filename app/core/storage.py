"""文件存储：FileStorage 协议 + 本地卷实现。

落盘约定：uuid4 十六进制命名，前 4 位拆成两级目录 `ab/cd/`；内容指纹 sha256；
`open(..., "xb")` 独占创建，保证同名内容多次保存各得新路径、绝不覆盖旧文件。
ext 白名单 `.字母数字{1,10}`（缺点自动补后再校验），不匹配抛 ValueError，防路径逃逸。
"""

import hashlib
import re
import uuid
from pathlib import Path
from typing import NamedTuple, Protocol


class StoredFile(NamedTuple):
    """一次成功保存的元数据：卷内相对路径、内容指纹、字节数。"""

    rel_path: str
    sha256: str
    size: int


class FileStorage(Protocol):
    def save(self, content: bytes, ext: str) -> StoredFile: ...


class LocalFileStorage:
    """本地卷实现：`ab/cd/<uuid4hex><ext>` 两级目录，ext 缺点自动补。"""

    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir)

    def save(self, content: bytes, ext: str) -> StoredFile:
        if not ext.startswith("."):
            ext = f".{ext}"
        # 白名单防御：ext 会拼进 rel_path，仅允许 `.字母数字{1,10}`，其余拒绝
        if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext) is None:
            raise ValueError("invalid extension")
        stem = uuid.uuid4().hex
        rel_path = f"{stem[:2]}/{stem[2:4]}/{stem}{ext}"
        dst = self.root_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "xb") as f:  # 独占创建：路径已存在则抛 FileExistsError
            f.write(content)
        return StoredFile(
            rel_path=rel_path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
