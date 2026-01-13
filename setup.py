import hashlib
import io
import os
import pathlib
import platform
import zipfile

import requests
from setuptools import setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

SYS_SET = {"windows", "linux", "darwin"}
ARCH_SET = {"x86_64", "amd64", "aarch64"}

ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

SYS_MAP = {
    "windows": "windows",
    "linux": "linux",
    "darwin": "osx",
}


def _norm_arch(arch: str) -> str:
    key = arch.lower()
    try:
        return ARCH_MAP[key]
    except KeyError:
        raise OSError(f"{arch} is not supported.") from None


def _norm_sys(sys: str) -> str:
    key = sys.lower()
    try:
        return SYS_MAP[key]
    except KeyError:
        raise OSError(f"{sys} is not supported.") from None


rclone_version = os.environ.get("BUILD_RCLONE_VERSION", "1.72.1")
system = os.environ.get("BUILD_SYSTEM", platform.system()).lower()
arch = os.environ.get("BUILD_ARCH", platform.machine()).lower()
system_rclone_bin = _norm_sys(system)
arch_rclone_bin = _norm_arch(arch)

if system_rclone_bin not in SYS_SET:
    raise RuntimeError(f"Invalid system: {system_rclone_bin}, must be {SYS_SET}")

if arch_rclone_bin not in ARCH_SET:
    raise RuntimeError(f"Invalid arch: {arch_rclone_bin}, must be {ARCH_SET}")


def rclone_download(system: str, arch: str, rclone_version: str, dest: pathlib.Path) -> pathlib.Path:
    # shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    base_url = f"https://downloads.rclone.org/v{rclone_version}"
    filename = f"rclone-v{rclone_version}-{system}-{arch}.zip"
    url = f"{base_url}/{filename}"
    sums_url = f"{base_url}/SHA256SUMS"

    print(f"Downloading rclone from {url}")
    # with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
    #     shutil.copyfileobj(r, f)

    req_session = requests.Session()
    resp = req_session.get(url)
    resp.raise_for_status()
    assert resp.content
    zip_bytes = resp.content

    try:
        hash_valid = None
        resp = req_session.get(sums_url)
        resp.raise_for_status()
        assert resp.text
        sums_text = resp.text

        for line in sums_text.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == filename:
                hash_valid = parts[0]
                break

        if not hash_valid:
            raise RuntimeError(f"{filename} not found in SHA256SUMS")

        hash = hashlib.sha256(zip_bytes).hexdigest()
        if hash != hash_valid.lower():
            raise RuntimeError(f"rclone checksum mismatch: expected {hash_valid}, got {hash}")

    except Exception as e:
        raise RuntimeError(f"Failed to verify rclone checksum: {e}") from e
    else:
        print("download verified successfully")

    bin_name = "rclone.exe" if system == "windows" else "rclone"
    bin_path = dest / bin_name

    bin_path.unlink(missing_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.filelist:
            if pathlib.Path(member.filename).name == bin_name:
                data = zf.read(member)
                bin_path.write_bytes(data)

    assert bin_path.is_file()

    print(f"unpacked rclone to {bin_path}")

    if system != "windows":
        bin_path.chmod(0o755)

    print("unpacking done")

    return bin_path


class BuildWithRclone(build_py):
    def run(self):
        # 1. Run normal build first (creates build_lib)
        super().run()

        # 2. Determine where to place the binary inside the wheel
        if self.editable_mode:
            print("editable installation!")
            pkg_dir = pathlib.Path("src/rclone_bin_client/bin")

            bin_name = "rclone.exe" if platform.system() == "Windows" else "rclone"
            if pkg_dir.joinpath(bin_name).exists():
                print("not downloading rclone as it already exists.")
                return
        else:
            pkg_dir = pathlib.Path(self.build_lib) / "rclone_bin_client/bin"

        print(pkg_dir)
        print()

        # 3. Download rclone
        print(f"Downloading binary for {system_rclone_bin}_{arch_rclone_bin}")
        rclone_download(system_rclone_bin, arch_rclone_bin, rclone_version, pkg_dir)


class PlatformWheel(bdist_wheel):
    def get_tag(self):
        # Override wheel tag
        return ("py3", "none", f"{system}_{arch}")


setup(
    cmdclass={
        "build_py": BuildWithRclone,
        "bdist_wheel": PlatformWheel,
    },
)
