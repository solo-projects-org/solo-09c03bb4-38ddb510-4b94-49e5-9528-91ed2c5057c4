"""增量刷新格网: 只传变更、覆盖语义、退回不入增量、存储不累积、同快照。"""

import threading

from app.database import connect, init_db
from app.ingest import IngestError, ingest_batch, parse_csv, record_rejection
from app.query import changed_cells, select_rectangle

from tests._base import DBTestCase, GRID, after_depth, before_depth, csv_text


def _log_rows(conn):
    return conn.execute("SELECT COUNT(*) n FROM cell_changes").fetchone()["n"]


class IncrementalTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.seed_both()

    def test_initial_since_zero_returns_all_cells(self):
        # 初次全量: 两期各 1600 格
        r = changed_cells(self.conn, 0)
        self.assertFalse(r.full_reload)
        self.assertEqual(len(r.changes), 2 * GRID * GRID)
        self.assertGreater(r.data_version, 0)

    def test_incremental_only_returns_changed_cells(self):
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        ingest_batch(self.conn, "after",
                     parse_csv(csv_text([(3, 4, 2, 250), (8, 9, 2, 260)])))
        r = changed_cells(self.conn, v0)
        self.assertFalse(r.full_reload)
        self.assertEqual(r.data_version, v0 + 1)
        got = {(c["x"], c["y"], c["phase"]) for c in r.changes}
        self.assertEqual(got, {(3, 4, "after"), (8, 9, "after")})
        # 未改动的读数 (before 期全部、after 期其余格) 不在增量里
        self.assertNotIn("before", {c["phase"] for c in r.changes})

    def test_empty_depth_appears_in_change_with_null(self):
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        ingest_batch(self.conn, "after", parse_csv(csv_text([(2, 2, 3, None)])))
        r = changed_cells(self.conn, v0)
        self.assertEqual(len(r.changes), 1)
        c = r.changes[0]
        self.assertEqual((c["x"], c["y"], c["depth_cm"]), (2, 2, None))
        # 读数本身也已被 NULL 覆盖
        row = self.conn.execute(
            "SELECT depth_cm FROM readings WHERE phase='after' AND x=2 AND y=2"
        ).fetchone()
        self.assertIsNone(row["depth_cm"])

    def test_later_increments_only_carry_latest_value(self):
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        ingest_batch(self.conn, "after", parse_csv(csv_text([(1, 1, 2, 300)])))
        v1 = changed_cells(self.conn, v0).data_version
        ingest_batch(self.conn, "after", parse_csv(csv_text([(1, 1, 3, 310)])))
        r = changed_cells(self.conn, v1)
        # 同一格只出现一次, 且是最新值; 不会把上一版的旧值再传一遍
        self.assertEqual(len(r.changes), 1)
        self.assertEqual(r.changes[0]["depth_cm"], 310)
        self.assertEqual(r.changes[0]["survey_no"], 3)

    def test_extra_storage_does_not_grow_with_repeated_supplements(self):
        # 同一格连续补测 200 次, 变更日志只保留一行
        base = _log_rows(self.conn)
        for sno in range(2, 202):
            ingest_batch(self.conn, "after",
                         parse_csv(csv_text([(5, 5, sno, 300 + sno)])))
        self.assertEqual(_log_rows(self.conn), base)
        row = self.conn.execute(
            "SELECT version, survey_no, depth_cm FROM cell_changes "
            "WHERE phase='after' AND x=5 AND y=5"
        ).fetchone()
        self.assertEqual((row["survey_no"], row["depth_cm"]), (201, 501))

    def test_rejected_batch_absent_from_increment(self):
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        with self.assertRaises(IngestError):
            ingest_batch(self.conn, "after",
                         parse_csv(csv_text([(1, 1, 1, 999), (2, 2, 1, 200)])))
        record_rejection(self.conn, "after", None, "测试")
        r = changed_cells(self.conn, v0)
        self.assertEqual(r.changes, [])
        self.assertEqual(r.data_version, v0)  # 退回不推进版本

    def test_version_and_changes_same_snapshot(self):
        # 边写边读: 每次增量返回的版本必须能解释其携带的全部变更
        stop = threading.Event()
        errors = []

        def writer():
            conn = connect(self.db_path)
            i = 0
            try:
                while not stop.is_set():
                    x, y = (i * 3) % GRID, (i * 5) % GRID
                    ingest_batch(conn, "after", parse_csv(csv_text(
                        [(x, y, 200 + i, after_depth(x, y) + i % 7)])))
                    i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            since = 0
            for _ in range(60):
                r = changed_cells(self.conn, since)
                # 此次响应里每格的版本 <= 响应版本; 下一轮从响应版本继续
                self.assertLessEqual(since, r.data_version)
                since = r.data_version
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [])

    def test_stale_since_triggers_full_reload(self):
        # 库被重置: 客户端记录的版本比当前库还新 -> 要求整图重载
        future = changed_cells(self.conn, 10_000)
        self.assertTrue(future.full_reload)
        self.assertEqual(future.changes, [])
        # 正常 since 仍是增量
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        ingest_batch(self.conn, "after", parse_csv(csv_text([(1, 1, 2, 300)])))
        self.assertFalse(changed_cells(self.conn, v0).full_reload)

    def test_pagination_covers_all_changes_exactly_once(self):
        v0 = select_rectangle(self.conn, 0, 0, 0, 0).data_version
        rows = [(x, 0, 2, 300 + x) for x in range(50)]
        ingest_batch(self.conn, "after", parse_csv(csv_text(rows)))
        seen = []
        cursor = None
        since = v0
        for _ in range(20):
            r = changed_cells(self.conn, since, limit=7, cursor=cursor)
            self.assertFalse(r.full_reload)
            seen.extend((c["x"], c["y"]) for c in r.changes)
            cursor = r.next_cursor
            if not cursor:
                break
        self.assertEqual(len(seen), 50)
        self.assertEqual(len(set(seen)), 50)  # 无重复
        self.assertEqual({x for x, _ in seen}, set(range(50)))  # 无遗漏

    def test_cell_changes_consistent_with_readings(self):
        # 变更日志里的 (survey_no, depth_cm) 必须等于 readings 的现行值
        ingest_batch(self.conn, "after", parse_csv(csv_text([(1, 1, 2, 300)])))
        ingest_batch(self.conn, "before", parse_csv(csv_text([(2, 2, 2, None)])))
        mismatch = self.conn.execute(
            "SELECT COUNT(*) n FROM cell_changes c JOIN readings r "
            "ON r.phase=c.phase AND r.x=c.x AND r.y=c.y "
            "WHERE r.survey_no <> c.survey_no OR r.depth_cm IS NOT c.depth_cm"
        ).fetchone()["n"]
        self.assertEqual(mismatch, 0)

    def test_migration_backfills_old_readings(self):
        # 模拟旧库: 有读数但无变更日志 (也从未做过补登)
        conn = connect(self.db_path)
        init_db(conn)
        conn.execute("DELETE FROM cell_changes")
        conn.execute("DELETE FROM meta WHERE key = 'cell_changes_backfilled'")
        conn.close()
        init_db(self.conn)
        # 存量读数补登进日志 (各记一次"存在"变更)
        self.assertEqual(_log_rows(self.conn), 2 * GRID * GRID)
        # 补登后从 0 取增量即可拿到全部存量读数, 不丢格
        r0 = changed_cells(self.conn, 0)
        self.assertFalse(r0.full_reload)
        self.assertEqual(len(r0.changes), 2 * GRID * GRID)
        # 标记只补登一次: 再调用一次 init_db 不会重复扫描写入, 行数不变
        init_db(self.conn)
        self.assertEqual(_log_rows(self.conn), 2 * GRID * GRID)
        # 之后从该版本继续可正常增量
        from app.query import full_grid
        full = full_grid(self.conn)
        ingest_batch(self.conn, "after", parse_csv(csv_text([(4, 4, 9, 333)])))
        r1 = changed_cells(self.conn, full["data_version"])
        self.assertFalse(r1.full_reload)
        self.assertEqual(len(r1.changes), 1)
        self.assertEqual(r1.changes[0]["depth_cm"], 333)
