"""Initialize only this deployment's quota schema; never print credentials."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "references/askdata_studio/backend"))
from app.config import load_env


def configure(secrets_file: Path, expected_project: str) -> None:
    prefix = "INSIGHTPILOT_QUOTA_DATABASE_"
    values = load_env(secrets_file, {prefix + key for key in ("PASSWORD", "HOST", "PORT", "USER", "NAME")})
    fields = {key: values.get(prefix + key, "") for key in ("PASSWORD", "HOST", "PORT", "USER", "NAME")}
    if not all(fields.values()) or fields["PASSWORD"] == "[YOUR-PASSWORD]":
        raise ValueError("Private database configuration is incomplete")
    if fields["USER"] != "postgres." + expected_project:
        raise ValueError("Database configuration does not match the requested project")
    if not fields["HOST"].endswith(".pooler.supabase.com") or fields["PORT"] != "5432":
        raise ValueError("Expected the supplied Supabase session-pooler connection")
    os.environ["INSIGHTPILOT_QUOTA_DATABASE_URL"] = (
        "postgresql://" + quote(fields["USER"], safe="") + ":" + quote(fields["PASSWORD"], safe="")
        + "@" + fields["HOST"] + ":" + fields["PORT"] + "/" + quote(fields["NAME"], safe=""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "initialize"])
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--project-ref", required=True)
    args = parser.parse_args()
    try:
        configure(args.secrets_file, args.project_ref)
        from app.security.trial_quota import transaction
        with transaction() as conn:
            if args.action == "initialize":
                conn.execute((ROOT / "deploy/quota_schema.sql").read_text())
            conn.execute("SELECT 1").fetchone()
            tables = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='insightpilot_trial'").fetchone()[0]
        print(f"Database reachable. Quota tables: {tables}. Action: {args.action}.")
        return 0
    except Exception as exc:
        print(f"Operation failed ({type(exc).__name__}); no credential details printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
