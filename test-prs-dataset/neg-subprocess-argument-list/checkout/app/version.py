import subprocess

VERSION = "1.4.0"


def git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def full_version() -> str:
    return f"{VERSION}+{git_revision()}"
