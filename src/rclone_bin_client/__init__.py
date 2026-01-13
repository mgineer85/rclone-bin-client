import platform
from pathlib import Path

bin_dir = Path(__file__).parent / "bin"
bin_name = "rclone.exe" if platform.system() == "Windows" else "rclone"
rclone = bin_dir / bin_name

if not rclone.is_file():
    raise RuntimeError("rclone binary missing!")

BINARY_PATH = rclone.absolute()
