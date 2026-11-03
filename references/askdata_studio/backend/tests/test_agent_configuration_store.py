"""Offline persistence/concurrency tests; these do not call application models."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from app.services.agent_configuration import (
    AgentConfigurationStore,
    ConfigurationConflict,
    ConfigurationStorageError,
    ConfigurationValidationError,
)


class AgentConfigurationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "agent_configuration" / "overrides.json"
        self.store = self.new_store()

    def new_store(self, **options) -> AgentConfigurationStore:
        return AgentConfigurationStore(self.path, prompt_ids={"preprocess", "react", "sql.generate"},
                                       skill_ids={"business_diagnosis", "database_query"}, **options)

    def update(self, **changes) -> dict:
        return self.store.update(expected_revision=self.store.snapshot()["revision"], actor="admin", **changes)

    def test_initial_snapshot_is_empty_and_creates_no_file_or_directory(self) -> None:
        self.assertEqual(self.store.snapshot(), {
            "revision": "0", "updated_at": None, "updated_by": None, "prompts": {}, "skills": {}})
        self.assertFalse(self.path.parent.exists())

    def test_save_is_persistent_and_keeps_exact_body(self) -> None:
        text = "系统提示词\n保留缩进\n  SQL只使用真实字段。\n"
        saved = self.update(prompts={"react": text}, skills={"business_diagnosis": "经营方法正文"})
        self.assertNotEqual(saved["revision"], "0")
        self.assertEqual(saved["updated_by"], "admin")
        self.assertTrue(saved["updated_at"].endswith("Z"))
        self.assertEqual(saved["prompts"]["react"], text)
        self.assertEqual(self.new_store().snapshot(), saved)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["schema_version"], 1)

    def test_snapshot_and_saved_return_are_detached(self) -> None:
        saved = self.update(prompts={"react": "真实正文"})
        saved["prompts"]["react"] = "返回对象被修改"
        view = self.store.snapshot()
        view["skills"]["business_diagnosis"] = "内存里的伪配置"
        self.assertEqual(self.store.snapshot()["prompts"], {"react": "真实正文"})
        self.assertEqual(self.store.snapshot()["skills"], {})

    def test_patch_preserves_unmentioned_entries_and_null_restores_default(self) -> None:
        first = self.update(prompts={"react": "自定义调查", "preprocess": "自定义路由"},
                            skills={"business_diagnosis": "自定义方法"})
        restored = self.update(prompts={"react": None}, skills={"business_diagnosis": None})
        self.assertNotEqual(first["revision"], restored["revision"])
        self.assertEqual(restored["prompts"], {"preprocess": "自定义路由"})
        self.assertEqual(restored["skills"], {})

    def test_stale_browser_cannot_overwrite_another_save(self) -> None:
        first = self.update(prompts={"react": "刚保存的内容"})
        with self.assertRaises(ConfigurationConflict):
            self.new_store().update(expected_revision="0", prompts={"react": "旧页面内容"}, actor="admin")
        self.assertEqual(self.store.snapshot(), first)

    def test_noop_preserves_revision_and_no_initial_config_file(self) -> None:
        unchanged = self.update(prompts={"react": None})
        self.assertEqual(unchanged["revision"], "0")
        self.assertFalse(self.path.exists())
        saved = self.update(prompts={"react": "内容未变"})
        self.assertEqual(self.update(prompts={"react": "内容未变"}), saved)

    def test_unknown_names_and_paths_are_not_accepted(self) -> None:
        original = self.update(prompts={"react": "保留内容"})
        for category, values in [("prompts", {"../../.env": "不能写"}),
                                 ("prompts", {"unregistered": "不能添加"}),
                                 ("skills", {"preprocess": "不能混用目录"})]:
            with self.subTest(category=category, values=list(values)):
                with self.assertRaises(ConfigurationValidationError):
                    self.update(**{category: values})
                self.assertEqual(self.store.snapshot(), original)

    def test_validation_failure_does_not_partially_apply_patch(self) -> None:
        original = self.update(prompts={"react": "旧内容"})
        with self.assertRaises(ConfigurationValidationError):
            self.update(prompts={"react": "本应不提交的新内容", "preprocess": "   "})
        self.assertEqual(self.store.snapshot(), original)

    def test_body_limits_and_types_are_explicit(self) -> None:
        tiny = self.new_store(max_text_chars=5)
        for text in ("", " \n", "123456", 42, True, ["123"], {"text": "123"}, "a\x00b"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ConfigurationValidationError):
                    tiny.update(expected_revision="0", prompts={"react": text}, actor="admin")
        with self.assertRaises(ConfigurationValidationError):
            tiny.update(expected_revision="0", prompts=["react"], actor="admin")
        self.assertFalse(self.path.exists())

    def test_revision_and_actor_are_required(self) -> None:
        for changes in ({"expected_revision": None}, {"expected_revision": ""},
                        {"actor": ""}, {"actor": 1}):
            args = {"expected_revision": "0", "actor": "admin", "prompts": {"react": "内容"}, **changes}
            with self.subTest(changes=changes):
                with self.assertRaises(ConfigurationValidationError):
                    self.store.update(**args)

    def test_configuration_size_limit_does_not_publish_partial_document(self) -> None:
        tiny = self.new_store(max_file_bytes=100)
        with self.assertRaises(ConfigurationValidationError):
            tiny.update(expected_revision="0", prompts={"react": "正常正文"}, actor="admin")
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self) -> None:
        original = self.update(prompts={"react": "原版本"})
        old_bytes = self.path.read_bytes()
        with patch("app.services.agent_configuration.os.replace", side_effect=OSError("模拟磁盘写入失败")):
            with self.assertRaises(ConfigurationStorageError):
                self.update(prompts={"react": "不能发布的新版本"})
        self.assertEqual(self.path.read_bytes(), old_bytes)
        self.assertEqual(self.store.snapshot(), original)
        self.assertFalse(list(self.path.parent.glob(".agent-config-*.tmp")))

    def test_corrupted_files_are_reported_instead_of_silently_using_defaults(self) -> None:
        self.path.parent.mkdir(parents=True)
        for raw in (b"not json", b"\xff", b"{}", b"[]", b'{"revision":"1","revision":"2"}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ConfigurationStorageError):
                    self.store.snapshot()
                with self.assertRaises(ConfigurationStorageError):
                    self.store.update(expected_revision="0", actor="admin", prompts={"react": "新内容"})
                self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_persisted_metadata_or_override_is_not_ignored(self) -> None:
        self.update(prompts={"react": "完整有效正文"})
        valid = json.loads(self.path.read_text())
        invalid_documents = [
            {**valid, "schema_version": True}, {**valid, "schema_version": 2},
            {**valid, "updated_at": "2026-10-29T10:00:00"},
            {**valid, "updated_by": None}, {**valid, "extra": "未知字段"},
            {**valid, "prompts": {"unknown": "未登记正文"}},
            {**valid, "skills": {"business_diagnosis": None}},
            {**valid, "prompts": None},
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ConfigurationStorageError):
                    self.store.snapshot()

    def test_two_editors_with_same_revision_have_one_successful_commit(self) -> None:
        barrier = Barrier(2)
        def submit(body):
            editor = self.new_store()
            revision = editor.snapshot()["revision"]
            barrier.wait(timeout=5)
            try:
                return editor.update(expected_revision=revision, prompts={"react": body}, actor="admin")
            except ConfigurationConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, ("编辑器一", "编辑器二")))
        self.assertEqual(outcomes.count("conflict"), 1)
        winner = next(value for value in outcomes if isinstance(value, dict))
        self.assertEqual(self.store.snapshot(), winner)

    def test_constructor_validates_whitelists_and_limits(self) -> None:
        for options in ({"prompt_ids": "react"}, {"prompt_ids": {"../../secret"}},
                        {"max_text_chars": True}, {"max_file_bytes": 0}):
            with self.subTest(options=options):
                with self.assertRaises(ConfigurationValidationError):
                    AgentConfigurationStore(self.path, **options)


if __name__ == "__main__":
    unittest.main()
