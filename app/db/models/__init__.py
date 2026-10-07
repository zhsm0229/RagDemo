"""ORM 模型包：与 specs/db-design.md v1.0 的 DDL 逐列映射（枚举一律 TEXT+CHECK）。

原 app/db/models.py 超 300 行红线（standards §1.4-4，约束 app/ 生产代码），按域拆分：
catalog（departments/users）、content（documents/chunks/qa_pairs/document_images）、
chat（conversations/messages）、eval（eval_questions/eval_runs）、memory（user_memories）、
ops（audit_logs）、types（Vector 列类型与 _ts 时间戳工厂）。各类声明逐字保留。

建表唯一入口是 Alembic 迁移（alembic/versions/0001_init.py，DDL 逐字照抄 spec）；
各文件中的 FK / UNIQUE / CHECK / Index 声明与迁移产物逐一同名（具名 FK 与 PG 默认
fkey 名均按库内实际约束名），保证 target_metadata autogenerate 零差异；声明不参与建表。
vector 列用 UserDefinedType 声明（不引入 pgvector-python 依赖，embedding 的
Python 侧读写适配留给检索任务）；messages 为声明式分区父表，ORM 直接对父表读写，
行路由由 PostgreSQL 完成；messages_default / messages_YYYY_MM 等分区子表不进
metadata（见 autogenerate 的 include_object 过滤）。

本 `__init__` re-export 全部模型名：`from app.db.models import X` 与
`from app.db import models`（alembic/env.py 注册 metadata）导入路径与拆包前一致，
T4 零差异守卫（test_autogenerate_diff_is_empty）依赖此兼容性。
"""

from app.db.models.agent import AgentRun, AgentStep
from app.db.models.catalog import Department, User
from app.db.models.chat import Conversation, Message
from app.db.models.content import Chunk, Document, DocumentImage, QaPair
from app.db.models.eval import EvalQuestion, EvalRun
from app.db.models.memory import UserMemory
from app.db.models.ops import AuditLog
from app.db.models.types import Vector

__all__ = [
    "AgentRun",
    "AgentStep",
    "AuditLog",
    "Chunk",
    "Conversation",
    "Department",
    "Document",
    "DocumentImage",
    "EvalQuestion",
    "EvalRun",
    "Message",
    "QaPair",
    "User",
    "UserMemory",
    "Vector",
]
