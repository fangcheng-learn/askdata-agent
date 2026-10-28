"""No network: validate the persistent two-question development cost cap."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PaidRunCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[4] / 'scripts/homenova_harness_v1/run_budgeted_acceptance.py'
        spec = importlib.util.spec_from_file_location('budgeted_acceptance', path)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def fixture(self, directory):
        directory = Path(directory)
        path = directory / 'paid_runs.json'
        path.write_text(json.dumps({'limit': 2, 'questions': ['问题一', '问题二'], 'runs': []}))
        return directory, path

    def test_distinct_questions_persist_and_duplicates_or_third_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory, path = self.fixture(temp)
            with patch.object(self.runner, 'DIRECTORY', directory), patch.object(self.runner, 'LEDGER', path), patch.object(self.runner, 'request') as http:
                self.runner.reserve(1)
                with self.assertRaises(RuntimeError): self.runner.reserve(1)
                self.runner.reserve(2)
                with self.assertRaises(RuntimeError): self.runner.reserve(2)
                self.assertEqual(len(json.loads(path.read_text())['runs']), 2)
                http.assert_not_called()

    def test_prior_billing_error_preserves_remaining_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory, path = self.fixture(temp)
            with patch.object(self.runner, 'DIRECTORY', directory), patch.object(self.runner, 'LEDGER', path):
                first = self.runner.reserve(1)
                Path(first['output']).write_text(json.dumps({'error': 'Arrearage'}))
                with self.assertRaises(RuntimeError): self.runner.reserve(2)
                self.assertEqual(len(json.loads(path.read_text())['runs']), 1)

    def test_existing_output_prevents_replay_after_a_ledger_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            directory, path = self.fixture(temp)
            (directory / 'case_1.json').write_text('{}')
            with patch.object(self.runner, 'DIRECTORY', directory), patch.object(self.runner, 'LEDGER', path):
                with self.assertRaises(RuntimeError): self.runner.reserve(1)


if __name__ == '__main__': unittest.main()
