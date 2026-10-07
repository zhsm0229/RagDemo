"""迁移结构测试：从 information_schema / pg_catalog 读回，断言 schema 与 specs/db-design.md 一致。

fixtures 见 tests/conftest.py：`pg`（session 级，对 5434 的 pg-test 执行 alembic upgrade head
并灌种子）、`session`（每测试独立事务，结束回滚，不污染库）。
"""

import pytest
from sqlalchemy import text

# ---- 每张表：列集合断言（与 specs/db-design.md DDL 逐列一致） ----

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "departments": ["id", "code", "name", "is_active", "created_at", "updated_at"],
    "users": [
        "id",
        "username",
        "password_hash",
        "dept_id",
        "role",
        "is_active",
        "must_change_password",
        "failed_attempts",
        "locked_until",
        "last_login_at",
        "memory_enabled",
        "created_at",
        "updated_at",
    ],
    "documents": [
        "id",
        "title",
        "dept_id",
        "original_filename",
        "file_path",
        "mime",
        "size_bytes",
        "content_hash",
        "simhash",
        "version",
        "effective_date",
        "expiry_date",
        "owner_name",
        "status",
        "parse_engine",
        "parse_error",
        "source_type",
        "review_status",
        "reject_reason",
        "is_active",
        "deleted_at",
        "uploaded_by",
        "created_at",
        "updated_at",
    ],
    "chunks": [
        "id",
        "document_id",
        "dept_id",
        "title_path",
        "content",
        "content_chars",
        "page_no",
        "position",
        "embedding",
        "embedding_model",
        "hit_count",
        "last_hit_at",
        "dup_of",
        "is_active",
        "created_at",
        "updated_at",
    ],
    "qa_pairs": [
        "id",
        "dept_id",
        "question",
        "answer",
        "embedding",
        "embedding_model",
        "status",
        "batch_no",
        "created_by",
        "created_at",
        "updated_at",
    ],
    "conversations": ["id", "user_id", "dept_id", "title", "is_active", "created_at", "updated_at"],
    "messages": [
        "id",
        "conversation_id",
        "user_id",
        "dept_id",
        "role",
        "content",
        "citations",
        "feedback",
        "feedback_comment",
        "feedback_category",
        "badcase_status",
        "is_eval",
        "answer_type",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "cache_hit",
        "is_active",
        "created_at",
        "updated_at",
    ],
    "eval_questions": [
        "id",
        "dept_id",
        "question",
        "expected_answer",
        "source_ref",
        "qtype",
        "is_active",
        "created_at",
        "updated_at",
    ],
    "eval_runs": [
        "id",
        "name",
        "config",
        "status",
        "total",
        "correct",
        "accuracy",
        "detail",
        "triggered_by",
        "created_at",
    ],
    "audit_logs": [
        "id",
        "actor_user_id",
        "action",
        "object_type",
        "object_id",
        "detail",
        "ip",
        "created_at",
    ],
    "user_memories": [
        "id",
        "user_id",
        "content",
        "source_message_id",
        "is_active",
        "created_at",
        "updated_at",
    ],
    "agent_runs": [
        "id",
        "user_id",
        "dept_id",
        "run_type",
        "status",
        "input",
        "output",
        "error",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ],
    "agent_steps": [
        "id",
        "run_id",
        "seq",
        "step_type",
        "tool_name",
        "args",
        "observation",
        "error",
        "latency_ms",
        "created_at",
    ],
}


def _columns(session, table: str) -> set[str]:
    rows = session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ),
        {"t": table},
    )
    return {r for r in rows.scalars()}


def _constraint_names(session, table: str) -> set[str]:
    rows = session.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid=to_regclass(:t)"),
        {"t": f"public.{table}"},
    )
    return {r for r in rows.scalars()}


def _indexdef(session, index_name: str) -> str:
    return session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname=:n"), {"n": index_name}
    ).scalar()


def _normalized_indexdef(session, index_name: str) -> str:
    """indexdef 归一化：PG15 对简单列谓词输出 WHERE is_active，复杂表达式才带括号。"""
    return _indexdef(session, index_name).replace("WHERE (", "WHERE ")


# ---- brief 指定的 4 条测试 ----


def test_all_tables_exist(session):
    rows = (
        session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        .scalars()
        .all()
    )
    for t in [
        "departments",
        "users",
        "documents",
        "chunks",
        "qa_pairs",
        "conversations",
        "messages",
        "eval_questions",
        "eval_runs",
        "audit_logs",
        "user_memories",
        "agent_runs",
        "agent_steps",
    ]:
        assert t in rows


def test_messages_partitioned_and_default(session):
    kind = session.execute(
        text("SELECT partstrat FROM pg_partitioned_table WHERE partrelid='messages'::regclass")
    ).scalar()
    assert kind == "r"
    assert (
        session.execute(
            text("SELECT count(*) FROM pg_inherits WHERE inhparent='messages'::regclass")
        ).scalar()
        >= 1
    )


def test_seed_departments_and_admin(session):
    codes = {r for r in session.execute(text("SELECT code FROM departments")).scalars()}
    assert codes == {"service", "hr", "all"}
    admin = session.execute(text("SELECT username FROM users WHERE role='admin'")).scalar()
    assert admin == "admin"


def test_unique_active_doc_index(session):
    sql = _normalized_indexdef(session, "uq_doc_active")
    assert "WHERE is_active" in sql and "deleted_at IS NULL" not in sql


# ---- 逐表结构断言（列存在） ----


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_columns(session, table):
    cols = _columns(session, table)
    missing = set(EXPECTED_COLUMNS[table]) - cols
    assert not missing, f"{table} 缺列: {sorted(missing)}"


def test_audit_logs_has_no_updated_at(session):
    """审计表只增不改：无 updated_at（specs/db-design.md §10）。"""
    assert "updated_at" not in _columns(session, "audit_logs")


def test_chunks_embedding_is_vector_type(session):
    rows = session.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name='chunks' AND column_name='embedding'"
        )
    ).scalar()
    assert rows == "vector"


# ---- 逐表约束断言 ----


def test_departments_unique_code(session):
    names = _constraint_names(session, "departments")
    assert "departments_code_key" in names
    assert "departments_pkey" in names


def test_users_role_check_and_partial_index(session):
    names = _constraint_names(session, "users")
    assert "users_role_check" in names
    sql = _indexdef(session, "idx_users_dept")
    assert "(dept_id)" in sql and "WHERE is_active" in sql


def test_documents_constraints_and_partial_unique_indexes(session):
    names = _constraint_names(session, "documents")
    assert "uq_doc_title_dept_ver" in names
    assert "documents_status_check" in names
    assert "documents_content_hash_check" not in names  # content_hash 无 CHECK
    sql = _indexdef(session, "uq_doc_content_hash")
    assert "UNIQUE" in sql and "WHERE (deleted_at IS NULL)" in sql
    list_sql = _indexdef(session, "idx_documents_list")
    assert "(dept_id, is_active, created_at DESC)" in list_sql


def test_documents_draft_columns_and_checks(session):
    """0007（agent-evolution #05）：source_type 默认 upload + 双 CHECK 具名在案。

    检索红线不依赖新列：草稿登记期 status='pending' 且 is_active=False，
    检索侧"仅 ready 且 is_active"过滤天然不命中（test_draft_review.py 读回断言）。"""
    names = _constraint_names(session, "documents")
    assert "documents_source_type_check" in names
    assert "documents_review_status_check" in names
    dflt = session.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='documents' AND column_name='source_type'"
        )
    ).scalar()
    assert "'upload'" in (dflt or "")


def test_chunks_hnsw_index_and_partial_dept_index(session):
    sql = _normalized_indexdef(session, "idx_chunks_embedding")
    assert "USING hnsw" in sql
    assert "vector_cosine_ops" in sql
    assert "m='16'" in sql or "m=16" in sql.replace("'", "")
    assert "ef_construction='64'" in sql or "ef_construction=64" in sql.replace("'", "")
    assert "WHERE is_active" in sql
    dept_sql = _normalized_indexdef(session, "idx_chunks_dept")
    assert "WHERE is_active" in dept_sql
    assert _indexdef(session, "idx_chunks_doc") is not None


def test_qa_pairs_checks_and_hnsw_index(session):
    names = _constraint_names(session, "qa_pairs")
    assert "qa_pairs_question_check" in names
    assert "qa_pairs_answer_check" in names
    assert "qa_pairs_status_check" in names
    sql = _indexdef(session, "idx_qa_embedding")
    assert "USING hnsw" in sql and "WHERE (status = 'enabled'::text)" in sql
    dept_sql = _indexdef(session, "idx_qa_dept")
    assert "(dept_id, status)" in dept_sql


def test_conversations_partial_index_and_default_title(session):
    sql = _normalized_indexdef(session, "idx_conv_user")
    assert "(user_id, updated_at DESC)" in sql and "WHERE is_active" in sql
    dflt = session.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='conversations' AND column_name='title'"
        )
    ).scalar()
    assert "新会话" in (dflt or "")


def test_messages_pk_sequence_fks_and_indexes(session):
    # 显式 sequence 发号（PG<17 分区表不支持 identity）
    seq = session.execute(
        text(
            "SELECT count(*) FROM information_schema.sequences WHERE sequence_name='messages_id_seq'"
        )
    ).scalar()
    assert seq == 1
    # 分区键必须入主键：PK(id, created_at)
    pk_def = session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='messages'::regclass AND contype='p'"
        )
    ).scalar()
    assert pk_def is not None and "id" in pk_def and "created_at" in pk_def
    # 具名外键
    names = _constraint_names(session, "messages")
    assert {"fk_msg_conv", "fk_msg_user", "fk_msg_dept"} <= names
    # role / feedback / badcase_status / answer_type CHECK
    assert {
        "messages_role_check",
        "messages_feedback_check",
        "messages_badcase_status_check",
        "messages_answer_type_check",
    } <= names
    assert _indexdef(session, "idx_msg_conv") is not None
    dept_time = _indexdef(session, "idx_msg_dept_time")
    assert "(dept_id, created_at DESC)" in dept_time
    feedback = _indexdef(session, "idx_msg_feedback")
    assert "WHERE (feedback = 'down'::text)" in feedback


def test_messages_partitions_present(session):
    rows = (
        session.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid=i.inhrelid WHERE i.inhparent='messages'::regclass"
            )
        )
        .scalars()
        .all()
    )
    assert set(rows) == {"messages_default", "messages_2026_10"}


def test_eval_questions_qtype_check(session):
    assert "eval_questions_qtype_check" in _constraint_names(session, "eval_questions")


def test_eval_runs_status_check_and_accuracy_type(session):
    assert "eval_runs_status_check" in _constraint_names(session, "eval_runs")
    rows = session.execute(
        text(
            "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
            "WHERE table_name='eval_runs' AND column_name='accuracy'"
        )
    ).one()
    assert (rows[0], rows[1]) == (5, 4)


def test_audit_logs_composite_pk_and_indexes(session):
    pk_def = session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='audit_logs'::regclass AND contype='p'"
        )
    ).scalar()
    assert pk_def is not None and "id" in pk_def and "created_at" in pk_def
    assert _indexdef(session, "idx_audit_time") is not None
    assert _indexdef(session, "idx_audit_actor") is not None


def test_user_memories_constraints_and_partial_index(session):
    """0006（F-210）：content 1-200 CHECK、user_id 具名 FK、source_message_id 无 FK、
    users.memory_enabled 默认 true、注入部分索引 (user_id, created_at DESC) WHERE is_active。"""
    names = _constraint_names(session, "user_memories")
    assert "user_memories_content_check" in names
    assert "user_memories_user_id_fkey" in names
    assert "user_memories_source_message_id_fkey" not in names  # 逻辑引用不建物理 FK（分区表限制）
    sql = _normalized_indexdef(session, "idx_user_memories_user")
    assert "(user_id, created_at DESC)" in sql and "WHERE is_active" in sql
    dflt = session.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='users' AND column_name='memory_enabled'"
        )
    ).scalar()
    assert "true" in (dflt or "")


def test_agent_steps_constraints_index_and_append_only(session):
    """0009（agent-evolution #06）：step_type 具名 CHECK、run_id 具名 FK、回放索引
    (run_id, seq)、无 updated_at（只增不改，audit_logs 同口径）。"""
    names = _constraint_names(session, "agent_steps")
    assert "agent_steps_step_type_check" in names
    assert "agent_steps_run_id_fkey" in names
    sql = _indexdef(session, "idx_agent_steps_run")
    assert "(run_id, seq)" in sql
    assert "updated_at" not in _columns(session, "agent_steps")


def test_autogenerate_diff_is_empty(pg):
    """alembic autogenerate 零差异守卫：Base.metadata 与迁移产物必须逐一同名同构。

    防 Important#1 类回归：模型漏声明约束时，首次 autogenerate 会生成
    DROP CONSTRAINT / DROP TABLE（messages_* 分区子表由 include_object 过滤，
    与 alembic/env.py 共用 app.db.base.autogenerate_include_object 策略）。
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from app.db.base import Base, autogenerate_include_object

    with pg.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": autogenerate_include_object}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"autogenerate 检出模型与迁移产物漂移: {diff}"
