# Rename heads-up

Recall Watch replaces Station Pulse. The complete runnable application must stay in `recall-watch/` and use the `recall` Python package.

Before committing, confirm that all of these are staged together:

- `watch.yaml`, `recall/replay.py`, `recall/setup_monitors.py`, the fixture data, and the app and engine tests;
- the renamed package metadata (`name = "recall-watch"`), documentation, and UI copy;
- the removal of the former `station-pulse/` tree.

The replay uses a checked-in snapshot. Run the test suite after any change to the matcher, snapshot, or shelf configuration.
