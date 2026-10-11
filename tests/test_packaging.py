"""Packaging invariants: every runtime package ships in the image.

The worker service once crashed at boot with "No module named worker": the
Dockerfile copied domain/adapters/api and pyproject listed the same three,
but the new worker/ package was added to neither. A Docker build is too slow
for the unit suite, so this test pins the two files a build reads instead.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PACKAGES = ("domain", "adapters", "api", "worker")


def test_every_runtime_package_is_installed_by_pip() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    included = config["tool"]["setuptools"]["packages"]["find"]["include"]

    for package in RUNTIME_PACKAGES:
        assert f"{package}*" in included, (
            f"pyproject.toml packages.find must include {package}*, "
            "or pip install . drops it from the image"
        )


def test_the_dockerfile_copies_every_runtime_package() -> None:
    """pip install runs on the copied sources, and python -m worker / uvicorn
    resolve imports from /app, so every package must be COPYed."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    copied = {
        match.group(1) for match in re.finditer(r"^COPY\s+(\S+)\s+", dockerfile, re.MULTILINE)
    }

    for package in RUNTIME_PACKAGES:
        assert package in copied, (
            f"Dockerfile must COPY {package}, or the image cannot import it "
            "(the worker service died with 'No module named worker' when it "
            "was missing)"
        )


def test_the_dockerfile_ships_the_migration_tree() -> None:
    """The manual migration gate runs inside the container
    (docker compose exec api alembic upgrade head), so alembic.ini and the
    alembic/ tree must be in the image."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    copied = {
        match.group(1)
        for match in re.finditer(r"^COPY\s+(\S+)\s+", dockerfile, re.MULTILINE)
    }

    assert "alembic.ini" in copied
    assert "alembic" in copied
