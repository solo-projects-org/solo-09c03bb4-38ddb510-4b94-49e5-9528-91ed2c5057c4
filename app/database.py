"""SQLite 连接管理、建表与并发控制。

设计要点
--------
- 开启 WAL, 读不阻塞写、写不阻塞读, 保证一次核量的整块查询和边缘查询
  能在同一个读事务 (同一份已提交快照) 内完成。
- 进程内写锁 ``_write_lock`` 串行化补测写入; SQLite 层再以 BEGIN IMMEDIATE
  抢占写锁, 配合 busy timeout, 保证并发补测不会漏算受影响的块。
- 读数 (readings) 与分块统计 (block_stats) 在同一个事务内提交,
  任一步失败整批回滚, 保留原结果。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

# 进程内写锁: 同一应用进程内的补测事务严格串行
_write_lock = threading.RLock()


def _resolve_db_path(db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    # 动态读取环境变量, 便于测试在导入后切换数据库
    raw = os.environ.get("DB_PATH")
    return Path(raw) if raw else config.DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开一个配置好的连接 (每个请求/线程独立连接)。"""
    path = _resolve_db_path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # 容器外本机调试且默认 /data 不可写时, 回退到临时目录
        if db_path is None and not os.environ.get("DB_PATH"):
            path = Path(tempfile.gettempdir()) / "dredging" / "dredging.db"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            raise
    conn = sqlite3.connect(
        str(path),
        timeout=config.BUSY_TIMEOUT_S,
        isolation_level=None,  # 事务完全由代码显式控制
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(config.BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
-- 现行读数: 同一期同一格只有一行, 由最高测次号决定
CREATE TABLE IF NOT EXISTS readings (
    phase      TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    survey_no  INTEGER NOT NULL,
    depth_cm   INTEGER,              -- NULL = 空水深 (测过但无有效水深)
    PRIMARY KEY (phase, x, y)
);

-- 分块统计: 每块每期一行, 补测只重算受影响的块
CREATE TABLE IF NOT EXISTS block_stats (
    bx              INTEGER NOT NULL,
    by              INTEGER NOT NULL,
    phase           TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    n_cells         INTEGER NOT NULL,  -- 该期该块内有读数 (含空水深) 的格数
    n_nonnull       INTEGER NOT NULL,  -- 其中水深非空的格数
    sum_depth_cm    INTEGER NOT NULL,  -- 非空水深之和 (厘米)
    -- 两期配对指标 (before/after 行存相同值, 便于任一时期补测后整行重写):
    -- 前后都有非空水深的格数, 以及这些格的 (后测-前测) 厘米差之和
    n_matched       INTEGER NOT NULL DEFAULT 0,
    diff_cm         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bx, by, phase)
);

-- 增量变更日志: 每 (期, 格) 只保留一行, 记录该格最后一次变更时的数据版本。
-- 补测反复覆盖同一格只 UPDATE 这一行, 行数上界 = 出现过的格数, 不随补测次数增长。
-- 批次被整批退回时事务回滚 / 或只写留痕, 本表不会留下任何行。
CREATE TABLE IF NOT EXISTS cell_changes (
    phase      TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    version    INTEGER NOT NULL,     -- 该格最后一次变更所属的数据版本
    survey_no  INTEGER NOT NULL,     -- 与 version 同一份已提交快照的读数
    depth_cm   INTEGER,              -- NULL = 空水深, 同样覆盖旧值并随增量下发
    PRIMARY KEY (phase, x, y)
);
CREATE INDEX IF NOT EXISTS idx_cell_changes_version
    ON cell_changes(version);

-- 已接收批次留痕 (成功与失败都记录, 失败批次不改读数/统计)
CREATE TABLE IF NOT EXISTS ingest_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase           TEXT NOT NULL,
    survey_no       INTEGER NOT NULL,
    row_count       INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    reject_reason   TEXT,
    blocks_touched  TEXT,               -- JSON: 受影响块坐标列表
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 数据版本: 每提交一次补测 +1, 便于网页/调用方确认快照
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """建表并初始化数据版本。幂等, 含旧库补列/补增量日志迁移。"""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # 旧版本数据库补配对指标列 (全新库跳过)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(block_stats)")}
    if "n_matched" not in cols:
        conn.execute("ALTER TABLE block_stats ADD COLUMN n_matched INTEGER NOT NULL DEFAULT 0")
    if "diff_cm" not in cols:
        conn.execute("ALTER TABLE block_stats ADD COLUMN diff_cm INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')"
    )
    _backfill_cell_changes(conn)


def _backfill_cell_changes(conn: sqlite3.Connection) -> None:
    """旧库存量读数补登增量日志 (各记一次"存在"变更)。

    升级前已提交的读数没有变更记录; 用当前数据版本补一条, 保证初次
    全量加载后从该版本续增量时仍能看到它们。以 ``meta`` 里的
    ``cell_changes_backfilled`` 标记保证只做一次 (不在每次启动都全表扫描),
    多进程同时启动时事务串行、INSERT OR IGNORE 幂等也不会写重。
    """
    done = conn.execute(
        "SELECT value FROM meta WHERE key = 'cell_changes_backfilled'"
    ).fetchone()
    if done is not None:
        return
    version = max(get_data_version(conn), 1)
    with write_transaction(conn, bump_version=False) as tx:
        # 已存在变更记录的格不覆盖 (它们本就带着自己的版本号)
        tx.execute(
            "INSERT OR IGNORE INTO cell_changes"
            "(phase, x, y, version, survey_no, depth_cm) "
            "SELECT r.phase, r.x, r.y, ?, r.survey_no, r.depth_cm FROM readings r",
            (version,),
        )
        tx.execute(
            "INSERT OR REPLACE INTO meta(key, value) "
            "VALUES ('cell_changes_backfilled', '1')"
        )


def get_data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"]) if row else 0


def next_data_version(conn: sqlite3.Connection) -> int:
    """本次写事务提交后将生效的数据版本。

    必须在 ``BEGIN IMMEDIATE`` 之后调用: 写事务已串行化, 期间不会有别的
    写者推进版本, 因此事务内算出的值就是提交时写入的值; 事务回滚则一并作废。
    """
    return get_data_version(conn) + 1


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务: WAL 下读到的是事务开始时的已提交版本。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextmanager
def write_transaction(
    conn: sqlite3.Connection, bump_version: bool = True
) -> Iterator[sqlite3.Connection]:
    """写事务: 进程锁 + BEGIN IMMEDIATE, 串行化所有补测。

    读数更新、分块重算、版本号递增在同一事务内, 异常即整体回滚。
    仅写留痕 (如批次退回) 时传 bump_version=False, 不推进数据版本。
    """
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if bump_version:
                conn.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                    "WHERE key = 'data_version'"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
