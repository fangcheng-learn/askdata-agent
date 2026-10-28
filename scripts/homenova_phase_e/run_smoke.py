#!/usr/bin/env python3
"""Capture one natural-language relative-date request; never load answer assets."""
import argparse
import json
from pathlib import Path
import time
import uuid
from run_agent_tests import credential, request, save, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/homenova_phase_e/private/ui_smoke_api.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output filename to retain earlier evidence")
    base = "http://127.0.0.1:8001"
    password = credential("admin")
    _, auth = request(base, "/api/auth/login", payload={"username": "admin", "password": password})
    token = auth["access_token"]
    payload = {"query": "查询本月成功支付订单量和商品GMV，金额用欧分。",
               "session_id": "homenova-e-smoke-" + uuid.uuid4().hex,
               "workspace": {"datasource_id": "homenova_20260831"}}
    started = time.perf_counter()
    status, response = request(base, "/api/query", token=token, payload=payload, timeout=300)
    result = {"request": payload, "http_status": status, "response": response,
              "elapsed_seconds": round(time.perf_counter() - started, 3),
              "answer_assets_loaded": False, "endpoint_same_as_ui": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save(args.output, result, (password, token))
    request(base, "/api/auth/logout", token=token, payload={})
    print(json.dumps({"http_status": status, "agent_status": response.get("status"),
                      "route": response.get("route"), "elapsed_seconds": result["elapsed_seconds"],
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
