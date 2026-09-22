"""Smoke test of a RUNNING deployment: health, auth is enforced, Mini App is served.

    python scripts/e2e_smoke.py https://bot.example.com

Needs only the standard library. Exit code 0 = all checks passed. It does NOT send a real video: the full
QA_CHECKLIST (upload -> plan -> render -> delivery) is still done by hand in Telegram.
"""

import json
import sys
import urllib.error
import urllib.request


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main(base: str) -> int:
    base = base.rstrip("/")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'ok' if ok else 'FAIL'}] {name} {detail}")
        if not ok:
            failures.append(name)

    status, body = fetch(f"{base}/health")
    check("GET /health is 200", status == 200, f"({status})")
    if status == 200:
        try:
            data = json.loads(body)
            check("health reports ok", data.get("status") == "ok", body[:120])
        except json.JSONDecodeError:
            check("health is JSON", False, body[:120])
    status, _ = fetch(f"{base}/api/me")
    check("GET /api/me without initData is rejected", status in (401, 403), f"({status})")
    status, body = fetch(f"{base}/app/")
    check("Mini App index is served", status == 200 and "<html" in body.lower(), f"({status})")
    print("FAILED" if failures else "ALL OK")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
