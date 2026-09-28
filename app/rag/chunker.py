r"""分块纯函数：标题层级分块 + 长度约束 + title_path 前缀，输入 ParsedDoc、输出 list[OutChunk]、无副作用。

贪心算法（task-7-brief）：① 按 heading 聚合——正文 <min_len 的节向后吸收同级紧邻
后继节（合并后 ≤max_len 才吸收），title_path 取首节；② 超长节按句切——以
。！？!? 为句边界贪心装包，句终止标点保留在句尾，无句边界的超长单句按 max_len
硬切兜底；③ 尾块 <min_len 并入前块（min_len 优先于 max_len，此时该块正文可能
超出 max_len）。title_path = `" > ".join(heading_path)`；content 前缀
`f"{title_path}｜"`（全角竖线）。min_len/max_len 均按正文计，不含前缀。
"""

from dataclasses import dataclass

_SENTENCE_ENDERS = "。！？!?"


@dataclass
class Block:
    """标题节：level 为标题层级，heading_path 为根到当前节的标题路径，text 为节正文。"""

    level: int
    heading_path: list[str]
    text: str


@dataclass
class ParsedDoc:
    """解析产物：文档标题与保序的标题节序列（title 供上游元数据使用，不参与分块）。"""

    title: str
    blocks: list[Block]


@dataclass
class OutChunk:
    """输出块：content 为带 `title_path｜` 前缀的正文（供引用溯源），position 为全局序号。"""

    content: str
    title_path: str
    position: int


@dataclass
class _Group:
    """聚合中间态：title_path/level 取首节，texts 为并入的各节正文（以换行连接）。"""

    title_path: str
    level: int
    texts: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


def _split_sentences(text: str) -> list[str]:
    """按句终止标点切句，标点保留在句尾；无终止标点的余文作为最后一句，字符零丢失。"""
    sentences: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _SENTENCE_ENDERS:
            sentences.append("".join(buf))
            buf = []
    if buf:
        sentences.append("".join(buf))
    return sentences


def _hard_split(sentence: str, max_len: int) -> list[str]:
    """无句边界的超长单句兜底：按 max_len 等分，保证装包后每块不超过 max_len。"""
    return [sentence[i : i + max_len] for i in range(0, len(sentence), max_len)]


def _pack(text: str, max_len: int) -> list[str]:
    """按句贪心装包：句依次入包，放不下即封包开新包；每包 ≤max_len，拼接后等于原文。"""
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for sentence in _split_sentences(text):
        for part in _hard_split(sentence, max_len):
            if cur and cur_len + len(part) > max_len:
                pieces.append("".join(cur))
                cur, cur_len = [], 0
            cur.append(part)
            cur_len += len(part)
    if cur:
        pieces.append("".join(cur))
    return pieces


def _aggregate(doc: ParsedDoc, min_len: int, max_len: int) -> list[_Group]:
    """贪心聚合：正文 <min_len 的节吸收同级紧邻后继节（合并后 ≤max_len 才吸收）；空白节丢弃。"""
    groups: list[_Group] = []
    for block in doc.blocks:
        if not block.text.strip():
            continue
        if groups:
            last = groups[-1]
            merged = "\n".join([*last.texts, block.text])
            if len(last.text) < min_len and block.level == last.level and len(merged) <= max_len:
                last.texts.append(block.text)
                continue
        groups.append(
            _Group(title_path=" > ".join(block.heading_path), level=block.level, texts=[block.text])
        )
    return groups


def split(doc: ParsedDoc, min_len: int = 300, max_len: int = 800) -> list[OutChunk]:
    """标题层级分块：聚合 → 超长按句切 → 尾块 <min_len 并入前块；position 从 0 连续递增。"""
    chunks: list[OutChunk] = []
    for group in _aggregate(doc, min_len=min_len, max_len=max_len):
        text = group.text
        pieces = [text] if len(text) <= max_len else _pack(text, max_len)
        if len(pieces) > 1 and len(pieces[-1]) < min_len:
            pieces[-2] += pieces[-1]
            pieces.pop()
        for piece in pieces:
            chunks.append(
                OutChunk(
                    content=f"{group.title_path}｜{piece}",
                    title_path=group.title_path,
                    position=len(chunks),
                )
            )
    return chunks
