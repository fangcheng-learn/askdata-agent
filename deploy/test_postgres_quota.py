"""Opt-in real Postgres checks using a disposable, isolated schema, no model calls."""
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_admin import configure, ROOT
from app.security import trial_quota as quota


@unittest.skipUnless(os.environ.get("INSIGHTPILOT_QUOTA_LIVE_TEST") == "1", "live DB test is opt-in")
class PostgresQuotaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configure(Path(os.environ["INSIGHTPILOT_QUOTA_SECRETS_FILE"]),
                  os.environ["INSIGHTPILOT_QUOTA_EXPECTED_PROJECT"])
        cls.schema = "insightpilot_trial_test_" + uuid4().hex[:12]
        cls.original = staticmethod(quota.transaction)
        with cls.original() as conn:
            conn.execute((ROOT / "deploy/quota_schema.sql").read_text().replace("insightpilot_trial", cls.schema))

    @classmethod
    def tearDownClass(cls):
        with cls.original() as conn:
            conn.execute("DROP SCHEMA " + cls.schema + " CASCADE")

    def setUp(self):
        schema = self.schema
        original = self.original
        class Connection:
            def __init__(self, conn): self.conn = conn
            def execute(self, sql, params=None):
                return self.conn.execute(sql.replace("insightpilot_trial.", schema + "."), params)
        @contextmanager
        def isolated():
            with original() as conn:
                yield Connection(conn)
        self.enterContext(patch.object(quota, "transaction", isolated))
        with quota.transaction() as conn:
            conn.execute("TRUNCATE insightpilot_trial.reservations, insightpilot_trial.visitors, insightpilot_trial.daily")
            conn.execute("UPDATE insightpilot_trial.gate SET run_id=NULL,lease_until=NULL")
        self.enterContext(patch.object(quota, "MAX_OUTPUT", 16384))

    def usage(self):
        with quota.transaction() as conn:
            row = conn.execute("SELECT COALESCE(sum(reserved_microyuan),0) FROM insightpilot_trial.daily").fetchone()
            return row[0]

    def start(self, run="run", visitor="visitor", **kwargs):
        quota.begin_run(run, visitor, **kwargs)
        self.token = quota.active_run.set(run)
        self.addCleanup(quota.active_run.reset, self.token)

    def test_two_starts_and_clarification(self):
        for run in ("first", "second"):
            quota.begin_run(run, "visitor")
            quota.finish_run(run)
        with self.assertRaises(quota.TrialLimit): quota.begin_run("third", "visitor")
        quota.begin_run("answer", "visitor", count_analysis=False)
        quota.finish_run("answer")
        quota.begin_run("different", "another-visitor")

    def test_concurrent_starts_only_one_winner(self):
        barrier = Barrier(2)
        def compete(n):
            barrier.wait()
            try:
                quota.begin_run("run-" + str(n), "visitor-" + str(n))
                return True
            except quota.TrialLimit:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(compete, range(2))), 1)
        with quota.transaction() as conn:
            self.assertEqual(conn.execute("SELECT sum(starts) FROM insightpilot_trial.visitors").fetchone()[0], 1)

    def test_parallel_reservations_never_exceed_ten_yuan(self):
        self.start()
        barrier = Barrier(12)
        payload = {"model": "qwen3.7-plus", "messages": [{"role":"user","content":"test"}]}
        def reserve(_):
            token = quota.active_run.set("run")
            try:
                barrier.wait()
                try: return quota.reserve(payload, "chat")
                except quota.TrialLimit: return None
            finally: quota.active_run.reset(token)
        with ThreadPoolExecutor(max_workers=12) as pool:
            tickets = list(pool.map(reserve, range(12)))
        self.assertTrue(any(tickets))
        self.assertTrue(any(t is None for t in tickets))
        self.assertLessEqual(self.usage(), quota.DAILY_LIMIT)
        with self.assertRaises(quota.TrialLimit): quota.reserve(payload, "chat")

    def test_settlement_idempotence_and_unknown_usage(self):
        self.start()
        ticket = quota.reserve({"model":"qwen3.7-plus"}, "chat")
        reserved = self.usage()
        quota.settle(ticket, None)
        self.assertEqual(self.usage(), reserved)
        quota.settle(ticket, {"prompt_tokens":100,"completion_tokens":20})
        self.assertEqual(self.usage(), 100*12+20*48)
        quota.settle(ticket, {"prompt_tokens":100,"completion_tokens":20})
        self.assertEqual(self.usage(), 100*12+20*48)

    def test_unknown_reservation_survives_new_connections(self):
        self.start()
        quota.reserve({"model":"text-embedding-v4","input":["test"]}, "embedding")
        reserved = self.usage()
        quota.finish_run("run")
        quota.begin_run("next-process", "visitor")
        self.assertEqual(self.usage(), reserved)
        quota.finish_run("next-process")
        with self.assertRaises(quota.TrialLimit): quota.begin_run("third", "visitor")

    def test_unexpected_overage_stays_halted_after_other_refund(self):
        self.start()
        first = quota.reserve({"model":"qwen3.7-plus"}, "chat")
        second = quota.reserve({"model":"qwen3.7-plus"}, "chat")
        quota.settle(first, {"prompt_tokens":1_000_000,"completion_tokens":1_000_000})
        quota.settle(second, {"prompt_tokens":0,"completion_tokens":0})
        with self.assertRaises(quota.TrialLimit): quota.reserve({"model":"text-embedding-v4"}, "embedding")
        quota.finish_run("run")
        with self.assertRaises(quota.TrialLimit): quota.begin_run("another", "visitor-two")

    def test_new_day_resets_start_allowance_without_erasing_history(self):
        with quota.transaction() as conn: day = quota._day(conn)
        for n in range(2):
            quota.begin_run(str(n), "visitor")
            quota.finish_run(str(n))
        with patch.object(quota, "_day", return_value=day+timedelta(days=1)):
            quota.begin_run("tomorrow", "visitor")
        with quota.transaction() as conn:
            counts = conn.execute("SELECT starts FROM insightpilot_trial.visitors ORDER BY day").fetchall()
        self.assertEqual(counts, [(2,), (1,)])


if __name__ == "__main__": unittest.main()
