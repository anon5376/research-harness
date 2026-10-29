# M6d: event wake

**Goal.** The supervisor wakes on events, not only on its clock. A new job, a stop or resume, an attach or a harvest gets acted on within about 2 s, not after a full interval. Pattern: clock wakeups that find nothing, and work queued while the lead sleeps. Lands after M6c, because both change `rh/supervisor.py`.

**Files you write:** `rh/supervisor.py`, `rh/ledger.py`, `tests/test_wake.py`. Change only what this brief names.

**Interface (exact additions).**
```python
# rh/ledger.py
def version(self) -> int        # PRAGMA data_version: changes when another connection commits
# rh/supervisor.py
def run(self, once=False, ticks=None, sleep=time.sleep, wake_poll=1.0)
```
- Between ticks, `run` sleeps in steps of `wake_poll` seconds up to `cfg["supervisor"]["interval"]`.
- It ticks early when `Ledger.version()` differs from its value at the end of the last tick, or when the file `$RH_HOME/wake` exists (it deletes that file).
- The supervisor's own writes during a tick must not cause an early wake on their own: compare versions taken after the tick.

**Tests that must pass** (`tests/test_wake.py`, unittest):
1. With interval 60 and wake_poll 0.05, a job added from a second Ledger connection launches within 2 s of the add. Use real time, with a local-shell `true` job.
2. With nothing changing, there is exactly one tick per interval (fake sleep).
3. Touching `$RH_HOME/wake` causes one early tick, and the file is removed.
4. `python3 -I tests/run.py` passes as a whole; no existing test changes.

**Do not change** tick order, the states, the stop rules, or `rh/cli.py`.
