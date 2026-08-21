"""记忆系统的 SQLite 持久化层。

本模块只认识 SQLite 和 :class:`MemoryEntry`，不认识 Agent、Prompt、工具、
TUI 状态或模型适配器。所有连接都必须在本模块内关闭，所有跨多条 SQL
的业务写入都必须由显式事务包住。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Callable, TypeVar

from .models import MemoryEntry, Scope, Status, VerificationEvidence


SCHEMA_VERSION = 2
T = TypeVar("T")


class MemoryStore:
    """提供确定性连接生命周期和事务语义的轻量 SQLite 仓库。"""

    def __init__(self, db_path: str | Path) -> None:
        """初始化数据库路径并确保 schema 已创建或升级。"""
        if not db_path:
            raise ValueError("db_path must be explicit")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """打开一条配置好外键和 busy timeout 的独立 SQLite 连接。"""
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """打开一条连接并保证退出时确定性关闭。

        ``sqlite3.Connection`` implements a transaction context manager, but
        that context manager does not close the connection.  Keeping this
        wrapper separate from :meth:`transaction` makes read-only paths obey
        the same close-on-exit rule without opening a transaction implicitly.
        """

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        """创建表和索引，并在同一事务内完成旧 schema 的升级。"""
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL CHECK (scope IN ('global', 'project', 'branch')),
                    kind TEXT NOT NULL CHECK (kind IN ('preference', 'decision', 'constraint', 'lesson')),
                    key TEXT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'active', 'archived', 'superseded', 'rejected')
                    ),
                    project_key TEXT,
                    branch_name TEXT,
                    source_type TEXT NOT NULL CHECK (
                        source_type IN ('explicit_user', 'verified_task', 'migration')
                    ),
                    source_session_id TEXT,
                    source_turn_id TEXT,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    supersedes_id TEXT REFERENCES memory_entries(id) ON DELETE SET NULL,
                    content_hash TEXT NOT NULL,
                    signal_count INTEGER NOT NULL DEFAULT 1 CHECK (signal_count >= 1),
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_status
                    ON memory_entries(status);
                CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memory_entries(scope);
                CREATE INDEX IF NOT EXISTS idx_memory_project
                    ON memory_entries(project_key);
                CREATE INDEX IF NOT EXISTS idx_memory_branch
                    ON memory_entries(branch_name);
                CREATE INDEX IF NOT EXISTS idx_memory_content_hash
                    ON memory_entries(content_hash);
                CREATE INDEX IF NOT EXISTS idx_memory_scope_context
                    ON memory_entries(scope, project_key, branch_name);
                CREATE INDEX IF NOT EXISTS idx_memory_key_context
                    ON memory_entries(scope, project_key, branch_name, key);
                """
            )
            # Version 1 already exists in user installations.  Make the
            # complete v1 -> v2 change and its metadata marker one explicit
            # transaction.  SQLite cannot add the original CHECK constraint
            # through ALTER TABLE, but the model and service enforce it too.
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(memory_entries)").fetchall()
                }
                if "signal_count" not in columns:
                    connection.execute(
                        "ALTER TABLE memory_entries ADD COLUMN signal_count INTEGER NOT NULL DEFAULT 1"
                    )
                if "evidence_json" not in columns:
                    connection.execute(
                        "ALTER TABLE memory_entries ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """打开 `BEGIN IMMEDIATE` 事务，成功提交、异常回滚，最后关闭连接。"""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def run_in_transaction(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        """在完整的提交/回滚/关闭边界内执行一个数据库回调。"""
        with self.transaction() as connection:
            return callback(connection)

    @staticmethod
    def _entry_from_row(row: sqlite3.Row | None) -> MemoryEntry | None:
        if row is None:
            return None
        try:
            raw_evidence = json.loads(str(row["evidence_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_evidence = []
        evidence = tuple(
            VerificationEvidence.from_dict(item)
            for item in raw_evidence
            if isinstance(item, dict)
        )
        return MemoryEntry(
            id=row["id"],
            scope=Scope(row["scope"]),
            kind=row["kind"],
            key=row["key"],
            content=row["content"],
            status=Status(row["status"]),
            project_key=row["project_key"],
            branch_name=row["branch_name"],
            source_type=row["source_type"],
            source_session_id=row["source_session_id"],
            source_turn_id=row["source_turn_id"],
            version=row["version"],
            supersedes_id=row["supersedes_id"],
            content_hash=row["content_hash"],
            signal_count=row["signal_count"],
            evidence=evidence,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _insert_sql() -> str:
        return """
            INSERT INTO memory_entries(
                id, scope, kind, key, content, status, project_key,
                branch_name, source_type, source_session_id, source_turn_id,
                version, supersedes_id, content_hash, signal_count, evidence_json,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

    def insert(self, entry: MemoryEntry, connection: sqlite3.Connection | None = None) -> None:
        """插入一条已经由 Service 校验过的记录，可复用调用方事务。"""
        values = (
            entry.id,
            entry.scope.value,
            entry.kind.value,
            entry.key,
            entry.content,
            entry.status.value,
            entry.project_key,
            entry.branch_name,
            entry.source_type.value,
            entry.source_session_id,
            entry.source_turn_id,
            entry.version,
            entry.supersedes_id,
            entry.content_hash,
            entry.signal_count,
            json.dumps([item.to_dict() for item in entry.evidence], ensure_ascii=False, separators=(",", ":")),
            entry.created_at,
            entry.updated_at,
            entry.expires_at,
        )
        if connection is not None:
            connection.execute(self._insert_sql(), values)
            return
        self.run_in_transaction(lambda conn: conn.execute(self._insert_sql(), values))

    def get(self, entry_id: str, connection: sqlite3.Connection | None = None) -> MemoryEntry | None:
        """按 ID 读取记录；传入连接时不自行提交或关闭调用方事务。"""
        query = "SELECT * FROM memory_entries WHERE id = ?"
        if connection is not None:
            return self._entry_from_row(connection.execute(query, (entry_id,)).fetchone())
        with self._connection() as conn:
            return self._entry_from_row(conn.execute(query, (entry_id,)).fetchone())

    def list_entries(
        self,
        *,
        statuses: tuple[Status, ...] | None = None,
        scopes: tuple[Scope, ...] | None = None,
        project_key: str | None = None,
        branch_name: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[MemoryEntry]:
        """按状态、作用域和上下文读取记录，并使用稳定时间/ID顺序返回。"""
        clauses: list[str] = []
        params: list[object] = []
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(status.value for status in statuses)
        if scopes:
            clauses.append("scope IN (" + ",".join("?" for _ in scopes) + ")")
            params.extend(scope.value for scope in scopes)
        if project_key is not None:
            clauses.append("project_key = ?")
            params.append(project_key)
        if branch_name is not None:
            clauses.append("branch_name = ?")
            params.append(branch_name)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM memory_entries" + where + " ORDER BY created_at ASC, id ASC"

        if connection is not None:
            rows = connection.execute(query, params).fetchall()
        else:
            with self._connection() as conn:
                rows = conn.execute(query, params).fetchall()
        return [entry for row in rows if (entry := self._entry_from_row(row)) is not None]

    def find_by_hash(
        self,
        *,
        scope: Scope,
        content_hash: str,
        project_key: str | None,
        branch_name: str | None,
        connection: sqlite3.Connection | None = None,
    ) -> MemoryEntry | None:
        """查找同 scope/context 下正文哈希相同的记录，用于幂等去重。"""
        query = """
            SELECT * FROM memory_entries
            WHERE scope = ? AND content_hash = ?
              AND project_key IS ? AND branch_name IS ?
            ORDER BY version DESC, updated_at DESC, id ASC
            LIMIT 1
        """
        params = (scope.value, content_hash, project_key, branch_name)
        if connection is not None:
            return self._entry_from_row(connection.execute(query, params).fetchone())
        with self._connection() as conn:
            return self._entry_from_row(conn.execute(query, params).fetchone())

    def find_active_by_key(
        self,
        *,
        scope: Scope,
        key: str,
        project_key: str | None,
        branch_name: str | None,
        connection: sqlite3.Connection | None = None,
    ) -> MemoryEntry | None:
        """查找同 scope/context/key 的 active 记录，保护现有真值不被覆盖。"""
        query = """
            SELECT * FROM memory_entries
            WHERE scope = ? AND key = ? AND status = 'active'
              AND project_key IS ? AND branch_name IS ?
            ORDER BY version DESC, updated_at DESC, id ASC
            LIMIT 1
        """
        params = (scope.value, key, project_key, branch_name)
        if connection is not None:
            return self._entry_from_row(connection.execute(query, params).fetchone())
        with self._connection() as conn:
            return self._entry_from_row(conn.execute(query, params).fetchone())

    def find_pending_by_key(
        self,
        *,
        scope: Scope,
        key: str,
        project_key: str | None,
        branch_name: str | None,
        connection: sqlite3.Connection | None = None,
    ) -> MemoryEntry | None:
        """查找同 scope/context/key 当前唯一的 pending 审核候选。"""

        query = """
            SELECT * FROM memory_entries
            WHERE scope = ? AND key = ? AND status = 'pending'
              AND project_key IS ? AND branch_name IS ?
            ORDER BY version DESC, updated_at DESC, id ASC
            LIMIT 1
        """
        params = (scope.value, key, project_key, branch_name)
        if connection is not None:
            return self._entry_from_row(connection.execute(query, params).fetchone())
        with self._connection() as conn:
            return self._entry_from_row(conn.execute(query, params).fetchone())

    def find_by_source(
        self,
        *,
        source_type: str,
        source_session_id: str,
        source_turn_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> MemoryEntry | None:
        """按来源 session/turn 查找系统经验，防止一个 Turn 重复生成候选。"""

        query = """
            SELECT * FROM memory_entries
            WHERE source_type = ? AND source_session_id = ? AND source_turn_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        """
        params = (source_type, source_session_id, source_turn_id)
        if connection is not None:
            return self._entry_from_row(connection.execute(query, params).fetchone())
        with self._connection() as conn:
            return self._entry_from_row(conn.execute(query, params).fetchone())

    def update_status(
        self,
        entry_id: str,
        status: Status,
        *,
        updated_at: float,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """更新状态字段；传入连接时由调用方决定事务边界。"""
        query = "UPDATE memory_entries SET status = ?, updated_at = ? WHERE id = ?"
        params = (status.value, updated_at, entry_id)
        if connection is not None:
            return connection.execute(query, params).rowcount == 1
        return bool(self.run_in_transaction(lambda conn: conn.execute(query, params).rowcount == 1))

    def update_version_link(
        self,
        entry_id: str,
        *,
        status: Status,
        updated_at: float,
        connection: sqlite3.Connection,
    ) -> None:
        """在调用方事务内更新旧版本的状态链接。"""
        connection.execute(
            "UPDATE memory_entries SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, updated_at, entry_id),
        )

    def replace(self, entry: MemoryEntry, *, connection: sqlite3.Connection) -> None:
        """在调用方事务内完整替换一条记录，不自行提交事务。"""

        connection.execute(
            """
            UPDATE memory_entries SET
                scope = ?, kind = ?, key = ?, content = ?, status = ?,
                project_key = ?, branch_name = ?, source_type = ?,
                source_session_id = ?, source_turn_id = ?, version = ?,
                supersedes_id = ?, content_hash = ?, signal_count = ?,
                evidence_json = ?, created_at = ?, updated_at = ?, expires_at = ?
            WHERE id = ?
            """,
            (
                entry.scope.value,
                entry.kind.value,
                entry.key,
                entry.content,
                entry.status.value,
                entry.project_key,
                entry.branch_name,
                entry.source_type.value,
                entry.source_session_id,
                entry.source_turn_id,
                entry.version,
                entry.supersedes_id,
                entry.content_hash,
                entry.signal_count,
                json.dumps([item.to_dict() for item in entry.evidence], ensure_ascii=False, separators=(",", ":")),
                entry.created_at,
                entry.updated_at,
                entry.expires_at,
                entry.id,
            ),
        )

    def increment_signal_count(
        self,
        entry_id: str,
        *,
        updated_at: float,
        connection: sqlite3.Connection,
    ) -> MemoryEntry:
        """增加候选的独立佐证信号数，并在同一事务中读回最新对象。"""

        connection.execute(
            "UPDATE memory_entries SET signal_count = signal_count + 1, updated_at = ? WHERE id = ?",
            (updated_at, entry_id),
        )
        entry = self.get(entry_id, connection=connection)
        if entry is None:
            raise KeyError(entry_id)
        return entry

    def delete(self, entry_id: str, connection: sqlite3.Connection | None = None) -> bool:
        """物理删除记录；Service 只有在明确确认 purge 时才会调用。"""
        query = "DELETE FROM memory_entries WHERE id = ?"
        if connection is not None:
            return connection.execute(query, (entry_id,)).rowcount == 1
        return bool(self.run_in_transaction(lambda conn: conn.execute(query, (entry_id,)).rowcount == 1))

    def get_metadata(self, key: str, connection: sqlite3.Connection | None = None) -> str | None:
        """读取 schema/migration 等元数据，不把 marker 混入记忆记录。"""
        if connection is not None:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return row["value"] if row is not None else None
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def set_metadata(self, key: str, value: str, connection: sqlite3.Connection | None = None) -> None:
        """写入或覆盖 metadata；迁移时必须复用当前事务连接。"""
        query = """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
        params = (key, value)
        if connection is not None:
            connection.execute(query, params)
        else:
            self.run_in_transaction(lambda conn: conn.execute(query, params))

    def count_by_scope_and_status(self) -> dict[str, dict[str, int]]:
        """统计各作用域和状态的记录数量，用于状态页与验收。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT scope, status, COUNT(*) AS count
                FROM memory_entries
                GROUP BY scope, status
                """
            ).fetchall()
        result = {scope.value: {status.value: 0 for status in Status} for scope in Scope}
        for row in rows:
            result.setdefault(row["scope"], {})[row["status"]] = int(row["count"])
        return result
