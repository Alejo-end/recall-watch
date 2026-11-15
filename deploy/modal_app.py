"""Serve Recall Watch on Modal, with a public link for the judges.

    uvx modal deploy deploy/modal_app.py        (from the repository root)

The link runs live: the Modal secret "recall-watch-context" holds CONTEXT_DEV_API_KEY, so the server reads
the Food Authority's list and re-reads the watched pages, as on a laptop. Create the secret once with only
that key in it:

    grep '^CONTEXT_DEV_API_KEY=' .env > /tmp/recall.env
    uvx modal secret create recall-watch-context --from-dotenv /tmp/recall.env --force
    rm /tmp/recall.env

The evidence lives on the Modal volume "recall-watch-data", mounted where the app keeps data/. On the first
start the volume is empty, and the server copies in the evidence from this machine (data/, which git ignores),
so the link opens on the stored case and adds new reads to it. The container sleeps when nobody uses the link,
so the page monitors run, and spend Context.dev credits, only while someone has it open.
"""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject(str(ROOT / "pyproject.toml"))
    .add_local_dir(ROOT / "data", remote_path="/root/seed", ignore=["**/*.bak"])
    .add_local_dir(ROOT, remote_path="/root/app",
                   ignore=[".venv", ".git", "**/__pycache__", ".pytest_cache", "docs", "tests", ".env", "data"])
)

data = modal.Volume.from_name("recall-watch-data", create_if_missing=True)
app = modal.App("recall-watch", image=image)


@app.function(min_containers=0, max_containers=1, timeout=3600, volumes={"/root/app/data": data},
              secrets=[modal.Secret.from_name("recall-watch-context")])
@modal.concurrent(max_inputs=50)
@modal.asgi_app(label="recall-watch")
def web():
    import os
    import shutil
    import sys

    if not any(Path("/root/app/data").iterdir()):
        shutil.copytree("/root/seed", "/root/app/data", dirs_exist_ok=True)
        data.commit()
    os.chdir("/root/app")
    sys.path.insert(0, "/root/app")
    from recall.app import app as fastapi_app
    return fastapi_app
