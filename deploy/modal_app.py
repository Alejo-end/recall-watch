"""Serve Recall Watch on Modal, with a public link for the judges.

    uvx modal deploy deploy/modal_app.py        (from the repository root)

The link runs without a Context.dev key, so it shows the stored evidence read only: every row is
a stored Context.dev read of a real page, and no credits are spent. The evidence lives in data/,
which git ignores, so the image takes it from this machine at deploy time. Writes on the link
(a name, a decision) last until the container stops.
"""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(str(ROOT / "pyproject.toml"))
    .add_local_dir(ROOT, remote_path="/root/app",
                   ignore=[".venv", ".git", "**/__pycache__", ".pytest_cache", "docs", "tests", ".env", "**/*.bak"])
)

app = modal.App("recall-watch", image=image)


@app.function(min_containers=0, max_containers=1, timeout=600)
@modal.concurrent(max_inputs=50)
@modal.asgi_app(label="recall-watch")
def web():
    import os
    import sys

    os.chdir("/root/app")
    sys.path.insert(0, "/root/app")
    from recall.app import app as fastapi_app
    return fastapi_app
