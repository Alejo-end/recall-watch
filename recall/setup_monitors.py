"""Create the Context.dev monitors of watch.yaml that do not exist yet.

    uv run python -m recall.setup_monitors          # shows what it would create
    uv run python -m recall.setup_monitors --create # creates them

Each new monitor costs credits on every run, also the first (docs.context.dev/account/credits).
"""

from __future__ import annotations

import sys

from .config import load_env, load_watch
from .contextdev import ContextDev


def main(argv: list[str]) -> None:
    load_env()
    cfg = load_watch()
    client = ContextDev()
    have = {m["name"] for m in client.monitors()}
    for spec in cfg["monitors"]:
        if spec["name"] in have:
            print(f"exists   {spec['name']}")
        elif "--create" in argv:
            m = client.create_monitor(spec)
            print(f"created  {spec['name']}  {m.get('id')}  baseline run {m.get('initial_run_id')}")
        else:
            print(f"missing  {spec['name']}  (add --create)")


if __name__ == "__main__":
    main(sys.argv[1:])
