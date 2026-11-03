"""Persist explicit prompt/Skill overrides independently from application sources.

The store is deliberately unaware of prompt contents, runtime agents, secrets,
and task execution.  A caller supplies the fixed configuration IDs it supports.
Reading a configuration never runs a model or silently replaces a damaged file
with defaults.  Writers use a revision and atomic replacement so an old browser
tab cannot overwrite a newer edit.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from typing import Any, Iterable, Iterator
import uuid


class ConfigurationConflict(ValueError):
    """The editor must reload the latest revision before saving its changes."""


class ConfigurationValidationError(ValueError):
    """A submitted override is not valid for the registered configuration IDs."""


class ConfigurationStorageError(RuntimeError):
    """A configuration cannot be safely read or durably written."""


class AgentConfigurationStore:
    DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "agent_configuration" / "overrides.json"
    DEFAULT_MAX_TEXT_CHARS = 100_000
    DEFAULT_MAX_FILE_BYTES = 2_000_000
    _ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
    _locks: dict[Path, Any] = {}
    _locks_guard = RLock()

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        prompt_ids: Iterable[str] = (),
        skill_ids: Iterable[str] = (),
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.path = Path(path or self.DEFAULT_PATH).expanduser().resolve()
        self.prompt_ids = self._registered_ids(prompt_ids, "prompt")
        self.skill_ids = self._registered_ids(skill_ids, "skill")
        for name, value in (("max_text_chars", max_text_chars), ("max_file_bytes", max_file_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigurationValidationError(name + "必须为正整数")
        self.max_text_chars, self.max_file_bytes = max_text_chars, max_file_bytes
        with self._locks_guard:
            self._lock = self._locks.setdefault(self.path, RLock())

    @classmethod
    def _registered_ids(cls, values: Iterable[str], label: str) -> frozenset[str]:
        if isinstance(values, (str, bytes)):
            raise ConfigurationValidationError(label + "白名单必须是名称集合")
        try:
            entries = list(values)
        except TypeError as exc:
            raise ConfigurationValidationError(label + "白名单必须是名称集合") from exc
        if any(not isinstance(value, str) or not cls._ID.fullmatch(value) for value in entries):
            raise ConfigurationValidationError(label + "白名单包含无效名称")
        return frozenset(entries)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"revision": "0", "updated_at": None, "updated_by": None, "prompts": {}, "skills": {}}

    def snapshot(self) -> dict[str, Any]:
        """Return a detached view of the latest file; a default read writes nothing."""
        with self._lock:
            return deepcopy(self._read())

    def update(
        self,
        *,
        expected_revision: str,
        prompts: dict[str, str | None] | None = None,
        skills: dict[str, str | None] | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """Apply only named changes; null deletes an override, restoring default.

        A no-op keeps the same revision and does not create a configuration file.
        All validation happens before replacement, and the previously committed
        file is left intact if a new temporary file cannot be written.
        """
        if (not isinstance(expected_revision, str) or not expected_revision.strip()
                or len(expected_revision) > 128):
            raise ConfigurationValidationError("expected_revision必须为非空版本号")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise ConfigurationValidationError("配置修改人必须为非空身份标识")
        changes = {
            "prompts": self._validate_changes(prompts, self.prompt_ids, "prompts"),
            "skills": self._validate_changes(skills, self.skill_ids, "skills"),
        }
        with self._lock, self._writer_lock():
            previous = self._read()
            if previous["revision"] != expected_revision:
                raise ConfigurationConflict("配置已被其他页面修改，请重新加载后再保存")
            updated = deepcopy(previous)
            for category, patch in changes.items():
                for name, text in patch.items():
                    if text is None:
                        updated[category].pop(name, None)
                    else:
                        updated[category][name] = text
            if all(updated[key] == previous[key] for key in ("prompts", "skills")):
                return deepcopy(previous)
            updated.update(revision=uuid.uuid4().hex,
                           updated_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                           updated_by=actor.strip())
            document = {"schema_version": 1, **updated}
            raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(raw) > self.max_file_bytes:
                raise ConfigurationValidationError("配置总大小超过允许上限")
            self._write(raw)
            return deepcopy(updated)

    def _validate_changes(self, changes: Any, allowed: frozenset[str], category: str) -> dict:
        if changes is None:
            return {}
        if not isinstance(changes, dict):
            raise ConfigurationValidationError(category + "必须是按名称保存的对象")
        result = {}
        for name, text in changes.items():
            if not isinstance(name, str) or name not in allowed:
                raise ConfigurationValidationError(category + "包含未登记的配置名称")
            if text is not None and (
                not isinstance(text, str) or not text.strip() or len(text) > self.max_text_chars or "\x00" in text
            ):
                raise ConfigurationValidationError(
                    category + "." + name + "必须为非空正文且不超过" + str(self.max_text_chars) + "字符；恢复默认请传null"
                )
            result[name] = text
        return result

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("配置文件包含重复字段")
            result[name] = value
        return result

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("rb") as source:
                raw = source.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise ConfigurationStorageError("无法读取Agent配置文件，未回退默认配置") from exc
        if len(raw) > self.max_file_bytes:
            raise ConfigurationStorageError("Agent配置文件超过大小上限，未回退默认配置")
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=self._unique_object)
            expected = {"schema_version", "revision", "updated_at", "updated_by", "prompts", "skills"}
            if not isinstance(document, dict) or set(document) != expected:
                raise ValueError("配置文件结构不完整或包含未知字段")
            if type(document["schema_version"]) is not int or document["schema_version"] != 1:
                raise ValueError("配置文件版本不受支持")
            if (not isinstance(document["revision"], str) or not document["revision"].strip()
                    or len(document["revision"]) > 128):
                raise ValueError("配置文件修订号无效")
            if not isinstance(document["updated_by"], str) or not document["updated_by"].strip():
                raise ValueError("配置文件修改人无效")
            if not isinstance(document["updated_at"], str):
                raise ValueError("配置文件修改时间无效")
            timestamp = datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("配置文件修改时间缺少时区")
            for category, allowed in (("prompts", self.prompt_ids), ("skills", self.skill_ids)):
                values = self._validate_changes(document[category], allowed, category)
                if not isinstance(document[category], dict) or any(value is None for value in values.values()):
                    raise ValueError("持久化配置正文无效")
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            raise ConfigurationStorageError("Agent配置文件损坏或格式无效，未回退默认配置") from exc
        return {key: value for key, value in document.items() if key != "schema_version"}

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        """The process-wide RLock and file lock protect read/compare/replace."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ConfigurationStorageError("无法锁定Agent配置文件，配置未保存") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise ConfigurationStorageError("无法锁定Agent配置文件，配置未保存") from exc
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _write(self, raw: bytes) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".agent-config-", suffix=".tmp",
                                             dir=self.path.parent, delete=False) as target:
                temporary = target.name
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ConfigurationStorageError("无法保存Agent配置文件，原配置保持不变") from exc
        finally:
            if temporary:
                with suppress(OSError):
                    Path(temporary).unlink()
