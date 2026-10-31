"""extract.write_tree must refuse a platform map that points outside the output dir (audit B-2).

Run: .venv/bin/python tests/test_extract_traversal.py   (no MSI needed)
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
import extract  # noqa: E402


def read(name):
    return b"payload"


def run(mapping):
    tmp = Path(tempfile.mkdtemp(prefix="spike-ext-"))
    staging = tmp / "box" / "app"
    try:
        extract.write_tree(staging, mapping, read)
        return None, tmp, staging
    except RuntimeError as e:
        return str(e), tmp, staging


def main():
    fails = 0

    def check(name, ok, extra=""):
        nonlocal fails
        print(("PASS " if ok else "FAIL ") + name, extra if not ok else "")
        fails += not ok

    err, tmp, st = run([{"isdir": False, "dir": "platform/web", "name": "ok.txt", "resource": "r1"}])
    check("normal entry extracted", err is None and (st / "platform/web/ok.txt").read_bytes() == b"payload", err)

    err, tmp, st = run([{"isdir": False, "dir": "platform", "name": "a.txt", "resource": "r1"},
                        {"isdir": False, "dir": "../../..", "name": "escaped.txt", "resource": "r1"}])
    escaped = [p for p in tmp.rglob("escaped.txt")] + [p for p in tmp.parent.glob("escaped.txt")]
    check("traversal entry refused", err is not None, err)
    check("nothing written when any entry escapes", not escaped and not (st / "platform/a.txt").exists(), escaped)

    err, tmp, st = run([{"isdir": True, "dir": "..", "name": "..", "resource": ""}])
    check("dir entry escaping refused", err is not None, err)

    err, tmp, st = run([{"isdir": False, "dir": "/etc", "name": "x", "resource": "r1"}])
    check("absolute dir is kept inside", err is None and (st / "etc/x").exists(), err)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
