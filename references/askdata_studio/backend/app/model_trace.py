"""Opt-in private, append-only model bodies; never loaded as runtime knowledge."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


SENSITIVE_KEY = re.compile(r'^(?:authorization|password|token|access_token|refresh_token|api_key|secret)$|(?:_api_key|_secret)$', re.I)
QUOTED_SECRET = re.compile(r'("(?:authorization|password|token|access_token|refresh_token|api_key|secret)"\s*:\s*)"(?:\\.|[^"\\])*"', re.I)


class ModelTrace:
    def __init__(self, directory: str, payload: dict, metadata: dict, secret: str):
        self.trace_id = uuid.uuid4().hex
        self.secret = secret
        self.redactions = 0
        self.metadata = metadata
        task = str(metadata.get('task_id') or 'unscoped')
        component = task if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task) else hashlib.sha256(task.encode()).hexdigest()
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = root / component
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write('request', {'trace_id': self.trace_id, 'recorded_at': self.now(),
            'state': 'prepared', **metadata, 'request_payload': payload,
            'scope': 'Provider JSON body only; no HTTP headers. Actual injected prompts, skills and schema are inside the payload. A missing outcome means completion is unknown, not a successful or unissued call.'})

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def _sanitize(self, value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if SENSITIVE_KEY.search(str(key)):
                    self.redactions += 1
                    result[key] = '[REDACTED]'
                else:
                    result[key] = self._sanitize(child)
            return result
        if isinstance(value, list):
            return [self._sanitize(child) for child in value]
        if isinstance(value, str):
            if self.secret and self.secret in value:
                self.redactions += value.count(self.secret)
                value = value.replace(self.secret, '[REDACTED]')
            # Prompt and response contents commonly contain another JSON
            # document as text. Preserve original text exactly unless redacted.
            try:
                nested = json.loads(value)
            except (ValueError, TypeError):
                nested = None
            if isinstance(nested, (dict, list)):
                before = self.redactions
                cleaned = self._sanitize(nested)
                if self.redactions != before:
                    value = json.dumps(cleaned, ensure_ascii=False)
            value, count = QUOTED_SECRET.subn(r'\1"[REDACTED]"', value)
            self.redactions += count
            value, count = re.subn(r'Bearer\s+[A-Za-z0-9_.~-]{8,}', 'Bearer [REDACTED]', value, flags=re.I)
            self.redactions += count
        return value

    def _write(self, suffix: str, data: dict):
        before = self.redactions
        safe = self._sanitize(data)
        safe['redaction_count'] = self.redactions - before
        safe['body_truncated'] = False
        encoded = json.dumps(safe, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
        target = self.directory / f'{self.trace_id}.{suffix}.json'
        staging = self.directory / f'.{self.trace_id}.{suffix}.writing'
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication without replacing a previous artifact. A crash
        # can leave .writing, which is explicitly not a completed JSON record.
        os.link(staging, target)
        staging.unlink()

    def finish(self, *, status: str, request_attempted: bool, response_body: str | None,
               response_received: bool, elapsed_seconds: float, http_status=None, error_type=None):
        self._write('outcome', {'trace_id': self.trace_id, 'recorded_at': self.now(),
            'status': status, 'request_attempted': request_attempted,
            'response_received': response_received, 'response_body': response_body,
            'response_body_complete': response_received,
            'response_partial': response_body is not None and not response_received,
            'elapsed_seconds': elapsed_seconds, 'http_status': http_status,
            'error_type': error_type,
            'response_scope': 'Provider-returned UTF-8 text, invalid bytes replaced; includes reasoning_content when supplied. Partial reads are marked. No reconstruction of unreturned output.'})
