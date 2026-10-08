r"""分块纯函数：标题层级分块 + 长度约束 + title_path 前缀，输入 ParsedDoc、输出 list[OutChunk]、无副作用。

聚合路径（task-7-brief）：① 按 heading 聚合——正文 <min_len 的节向后吸收同级紧邻
后继节（合并后 ≤max_len 才吸收），title_path 取首节；② 超长节按句切——以
。！？!? 为句边界贪心装包，句终止标点保留在句尾，无句边界的超长单句按 max_len
硬切兜底；③ 尾块 <min_len 并入前块（min_len 优先于 max_len，此时该块正文可能
超出 max_len）。title_path = `" > ".join(heading_path)`；content 前缀
`f"{title_path}｜"`（全角竖线）。min_len/max_len 均按正文计，不含前缀。
条款路径（metrics-balance §3.2）：`第X条` 处开块、单条超 max_clause_len 句切、
碎条并入下条、尾块归并前块；split_by_profile 按档位常量（doc_profile.PROFILE_PARAMS）
分派两条路径，入库管线与 golden 镜像共用该入口。
页码线程化（task-6b）：OutChunk.page 取组内首块 Block.page（跨页合并组不混页码），
无页码来源（docx/MinerU）为 None；页码只作引用定位，不参与聚合与切分判定。
"""

from dataclasses import dataclass, field

from app.rag.doc_profile import CLAUSE_PATTERN, AggregateParams, ClauseParams

_SENTENCE_ENDERS = "。！？!?"


@dataclass
class Block:
    """标题节：level 为标题层级，heading_path 为根到当前节的标题路径，text 为节正文。

    page 为块所在页码（1 起；\\f 分页约定见 parser.PDF_PAGE_SEPARATOR），无页码
    概念的来源（docx/MinerU）为 None——带默认值，既有构造点向后兼容。"""

    level: int
    heading_path: list[str]
    text: str
    page: int | None = None


@dataclass
class ParsedDoc:
    """解析产物：文档标题与保序的标题节序列（title 供上游元数据使用，不参与分块）。

    images/image_skipped（F-1017）：解析器抽取的内嵌图片与抽取期跳过数——由
    ingest_pipeline 在开关开启时做 OCR 编排；缺省空/0，既有构造点向后兼容。"""

    title: str
    blocks: list[Block]
    images: list = field(default_factory=list)  # list[ExtractedImage]  # app/rag/image_extract
    image_skipped: int = 0


@dataclass
class OutChunk:
    """输出块：content 为带 `title_path｜` 前缀的正文（供引用溯源），position 为全局序号。

    page 为组内首块页码（入库落 chunks.page_no），无页码来源为 None，与 position
    同为逐块直传字段。"""

    content: str
    title_path: str
    position: int
    page: int | None = None


@dataclass
class _Group:
    """聚合中间态：title_path/level/page 取首节，texts 为并入的各节正文（以换行连接）。"""

    title_path: str
    level: int
    texts: list[str]
    page: int | None

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
            _Group(
                title_path=" > ".join(block.heading_path),
                level=block.level,
                texts=[block.text],
                page=block.page,
            )
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
                    page=group.page,
                )
            )
    return chunks


# ---------- 条款边界切分（metrics-balance §3.2 policy_clause 档） ----------


@dataclass
class _Segment:
    """条款段中间态：text 为段原文（条款起点到下一处条号起点，携带原文换行等分隔符）。

    seq/tail_seq 为首尾片段所在块序号：归属（title_path/page）按起始块（跨节条款按
    起始位置归属），并段相邻判定按尾块——同块相邻子串直连保逐字，跨块补换行。"""

    text: str
    heading_path: list[str]
    page: int | None
    seq: int
    tail_seq: int

    def absorb(self, other: "_Segment") -> None:
        """后段并入本段：归属不变（块从起始处算），尾块序号随并接推进。"""
        sep = "" if other.seq == self.tail_seq else "\n"
        self.text = f"{self.text}{sep}{other.text}"
        self.tail_seq = other.tail_seq


def _clause_segments(doc: ParsedDoc) -> list[_Segment]:
    """全文档按条款编号切段：`第X条` 处开新段（§3.2）；条款前导文与无条款节各自成段。

    编号词表与分型检测同源（doc_profile.CLAUSE_PATTERN，单一事实源）；空白节丢弃
    （与聚合路径同口径，不产空段）。"""
    segments: list[_Segment] = []
    for seq, block in enumerate(doc.blocks):
        if not block.text.strip():
            continue
        starts = [match.start() for match in CLAUSE_PATTERN.finditer(block.text)]
        if not starts:
            segments.append(_Segment(block.text, block.heading_path, block.page, seq, seq))
            continue
        bounds = [*starts, len(block.text)]
        if bounds[0] > 0:  # 首个条款前的导语：随所在节成段，碎条规则照常适用
            segments.append(
                _Segment(block.text[: bounds[0]], block.heading_path, block.page, seq, seq)
            )
        segments.extend(
            _Segment(
                block.text[bounds[i] : bounds[i + 1]], block.heading_path, block.page, seq, seq
            )
            for i in range(len(bounds) - 1)
        )
    return segments


def _merge_fragments(segments: list[_Segment], merge_below: int) -> list[_Segment]:
    """碎条（<merge_below）并入下条，并入后允许超 max_clause_len（§3.2 落锤，同"min 优先于
    max"先例）；末段碎条无下条可并 → 对称归并前段；孤立碎条原样保留。"""
    merged: list[_Segment] = []
    pending: _Segment | None = None
    for segment in segments:
        if pending is None:
            pending = segment
        elif len(pending.text) < merge_below:
            pending.absorb(segment)
        else:
            merged.append(pending)
            pending = segment
    if pending is not None:
        if merged and len(pending.text) < merge_below:
            merged[-1].absorb(pending)
        else:
            merged.append(pending)
    return merged


def split_clauses(doc: ParsedDoc, params: ClauseParams) -> list[OutChunk]:
    """条款边界切分：`第X条` 开块 → 碎条归并 → 单条超 max_clause_len 按句切 →
    句切尾片 <merge_below 归并前片（此时该片可超上限，先例同上）；content 仍带
    `title_path｜` 前缀，title_path 取条款起始所在节 heading_path（不得为空）。"""
    chunks: list[OutChunk] = []
    for segment in _merge_fragments(_clause_segments(doc), params.merge_below):
        pieces = (
            [segment.text]
            if len(segment.text) <= params.max_clause_len
            else _pack(segment.text, params.max_clause_len)
        )
        if len(pieces) > 1 and len(pieces[-1]) < params.merge_below:
            pieces[-2] += pieces[-1]
            pieces.pop()
        title_path = " > ".join(segment.heading_path)
        for piece in pieces:
            chunks.append(
                OutChunk(
                    content=f"{title_path}｜{piece}",
                    title_path=title_path,
                    position=len(chunks),
                    page=segment.page,
                )
            )
    return chunks


def split_by_profile(doc: ParsedDoc, params: AggregateParams | ClauseParams) -> list[OutChunk]:
    """按档位参数分派切分（§3.2 档位表）：聚合档直传 min/max（general 档与既有 split
    逐字一致），条款档走条款边界；入库管线与 golden 镜像共用本入口。"""
    if isinstance(params, AggregateParams):
        return split(doc, params.min_len, params.max_len)
    return split_clauses(doc, params)
