# evals/

Offline checks for the multi-agent migration. No model is called: everything here
is arithmetic over the prompt blocks and the tool registry, so it runs in CI on
every prompt change for free.

## differential.py

Cross-checks the S5 prompt split against the S4 tool binding, in two directions.

**Orphaned rule** — an agent's prompt names a tool that agent cannot call. The
instruction is intact and the tool is intact, but the agent is being told to do
something impossible. This is the defect class that killed the architecture
doc's original Sales/Bookings roster: `_SECOND_DRIVE_ANCHOR` fires from booking
state and says *"search_vehicles or get_vehicle_details first, every time"*.
**Must be zero.**

**Unguided tool** — an agent can call a tool no block in its prompt mentions.
Weaker, since some tools are self-describing through their schema, so this is
reported rather than failed.

Run it:

    python -m evals.differential          # exit 1 on any orphan

`tests/test_evals_differential.py` gates it in CI.

### What it found on its first run

Four real defects, none of which the 1023-test suite could see — only three test
files exercise both journeys in one flow, and none assert on prompt composition.

1. `get_booking` was bound to BookingManagement only, but `_BOOKINGS_ANCHOR` goes
   to *every* agent and says "call get_booking for it first". → moved to SHARED.
2. `get_showrooms` was SHARED for rule 10's "unless they ask to move it", but no
   BookingManagement block guides branch selection. Widening prose to match would
   be re-authoring. → TestDrive only.
3. `_SECOND_DRIVE_ANCHOR` reaches BookingManagement and names three tools it
   cannot call. → S7 binds the anchor to TestDrive.
4. `_OPTIONS_ANCHOR` carries a `compare_vehicles` paragraph that reaches
   BookingManagement. → S7 splits that paragraph out.

3 and 4 are allowlisted in the CI gate until S7, so a *fifth* orphan fails.

## Live replay — deliberately not built

A true output-level differential (same conversation through old and new, diff the
replies) needs real model calls. The suite is offline by construction — see the
network guard in `tests/conftest.py` — so that harness is out of scope here and
should be run deliberately, outside CI, against a key you have chosen to spend.
