import subprocess
from pathlib import Path

THUMB_DIR = Path("/var/app/thumbs")


def make_thumbnail(filename: str) -> Path:
    target = THUMB_DIR / f"{Path(filename).stem}.png"
    subprocess.run(f"convert {filename} -resize 200x200 {target}", shell=True, check=True)
    return target
