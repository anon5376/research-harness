# M6c: paced cloud launches

**Goal.** Cloud task creation never fans out blind. The supervisor keeps a minimum gap between creates and an hourly cap, both read from config. Rate-limit replies keep their existing backoff. Pattern: blind fan-out hitting HTTP 429.

**Files you write:** `rh/supervisor.py`, `rh/ledger.py`, `rh/config.py`, `tests/test_pacing.py`. Change only what this brief names; the diff is reviewed line by line.

**Interface (exact additions).**
```python
# rh/ledger.py
def launches_since(self, route_prefix: str, since: float) -> list[float]
    # ts of "state" events moving a job to "running" whose job's route starts with route_prefix, ascending
# rh/config.py DEFAULTS["cloud"] gains:
"min_gap_s": 60, "max_per_hour": 20
```
- In `Supervisor._launch_ready`, before each cloud launch, skip the launch for this tick (no state change, no event) when either holds:
  - the last cloud launch was under `min_gap_s` ago;
  - there were `max_per_hour` or more launches in the last 3600 s.
- The `launches_per_tick` budget stays.
- Attach (`rh cloud attach`) counts as a launch, because it moves a job to running.

**Tests that must pass** (`tests/test_pacing.py`, unittest, fake clock like `tests/test_supervisor.py`):
1. 10 ready cloud jobs, 30 s ticks, min_gap 60, max_per_hour 99: launches are at least 60 s apart, and all 10 launch within 11 min.
2. max_per_hour 5, min_gap 0: after 5 launches the 6th waits until 3600 s after the first.
3. While a job waits for pacing it stays `ready`, and no event or notify is written for it.
4. `launches_since` ignores local-shell jobs and events before `since`.
5. Every existing test in `tests/test_supervisor.py` still passes unchanged.
6. `python3 -I tests/run.py` passes as a whole.

**Do not change:**
- the job states and transitions;
- `route_fail` or `route_ok`;
- the stop and pause rules;
- any existing test.
