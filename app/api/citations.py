r"""引用正文端点（T8 评审裁决 Rider，T9 前端按此消费）：GET /api/citations/content
?refType=doc|qa&refId=<id>，前端"查看原文"按引用定位取正文原文。

登录用户即可（ReadyUser 门，非 admin）：引用产自问答链路，读者即提问者。查询、
部门隔离与存活闸在 services/citations（§1.6 api 禁直写查询），本层只做参数接收与
DTO 组装；qa 分支 answer/question 映射 content/title，page/titlePath 恒空（问答对无
页码与标题路径概念）。refType 枚举外 / refId 非正整数由 FastAPI 参数校验 422/1003。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import DbSession, ReadyUser
from app.services import citations as citations_service

router = APIRouter(prefix="/citations", tags=["citations"])


@router.get("/content")
def citation_content(
    user: ReadyUser,
    db: DbSession,
    refType: Annotated[Literal["doc", "qa"], Query()],
    refId: Annotated[int, Query(gt=0)],
) -> dict:
    """引用正文（T8 裁决契约）：两分支各一条主键查询，越权与不存在统一 404/3003。"""
    if refType == "doc":
        chunk, title = citations_service.doc_content(db, refId, user.dept_id)
        data = {
            "refType": "doc",
            "refId": refId,
            "content": chunk.content,
            "title": title,
            "page": chunk.page_no,
            "titlePath": chunk.title_path,
        }
    else:
        qa = citations_service.qa_content(db, refId, user.dept_id)
        data = {
            "refType": "qa",
            "refId": refId,
            "content": qa.answer,
            "title": qa.question,
            "page": None,
            "titlePath": None,
        }
    return {"code": 0, "message": "ok", "data": data}
