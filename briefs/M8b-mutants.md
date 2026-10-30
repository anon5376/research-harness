# M8b: the mutant runner

**Goal.** Every job's acceptance includes a list of one-line mutants that its tests must kill. Today the lead writes a throwaway script per job. Make it one tool that reads a list, applies each mutant to a temporary copy of the repository, runs the named tests, and says which survived.

**Files you write:** `tools/mutants.py` and `tests/test_mutants.py`. Nothing else.

**Mutant lists** (`mutants/<job>.toml`, written by the lead; two exist):
```toml
[[mutant]]
name = "exit 75 is capacity"
file = "rh/collect.py"
find = 'return "capacity" if exit_code == 75 else "failed", receipt('
replace = 'return "failed", receipt('
tests = ["tests.test_hardening.SmallItemsTest"]
```

**Interface (exact).** `python3 -I tools/mutants.py [--repo DIR] [--timeout SEC] LIST.toml`
- Copies the files `git ls-files` lists in `--repo` (default: the current directory) to a new temporary directory. The source checkout is never written to.
- For each mutant, in order: restore the copy, require `find` to occur exactly once in `file` (otherwise the mutant is `error`), replace it, and run `python3 -I -B tests/run.py <tests...>` in the copy with `--timeout` (default 300 s). Exit code non-zero is `killed`, zero is `survived`, a timeout is `killed`.
- Prints one line per mutant, `KILLED|SURVIVED|ERROR  <name>`, then `MUTANTS KILLED n=<killed>/<total>`. Exits 0 only when every mutant is killed.
- Standard library only.

**Tests that must pass** (`tests/test_mutants.py`, unittest, against a small temporary git repository with its own `tests/run.py`): a mutant the tests catch is KILLED; one they miss is SURVIVED and the exit is 1; a `find` that occurs twice or not at all is ERROR and the exit is 1; the source repository's files are byte-identical after the run; `python3 -I tools/mutants.py mutants/m7b-serve.toml` from this repository prints `MUTANTS KILLED n=5/5`; and `python3 -I tests/run.py` passes as a whole.
