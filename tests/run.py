"""Run unittest modules and print one success marker only when every test passed and at least one ran.

    python3 -I tests/run.py tests.test_lease tests.test_privacy
    python3 -I tests/run.py            # every tests/test_*.py

Exit 0 and "TESTS PASSED n=<count>" on success; exit 1 otherwise. Zero tests counts as failure, so a gate
cannot pass on a module that discovered nothing.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv):
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    if argv:
        suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in argv)
    else:
        suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    ran = result.testsRun - len(result.skipped)
    if result.wasSuccessful() and ran > 0:
        print(f"TESTS PASSED n={ran}")
        return 0
    print(f"TESTS NOT PASSED ran={ran} failures={len(result.failures)} errors={len(result.errors)}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
