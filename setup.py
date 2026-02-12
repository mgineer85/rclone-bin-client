import hashlib
import io
import os
import pathlib
import shutil
import zipfile

import requests
from packaging.tags import sys_tags
from setuptools import setup
from setuptools.command.build_py import build_py
from wheel.bdist_wheel import bdist_wheel

rclone_version = os.environ.get("RCLONE_VERSION", "1.73.0")
rclone_build_platform = os.environ.get("RCLONE_BUILD_PLATFORM", None)

PLATFORMS = {
    # (pypa platform tag) -> (rclone variant)
    "win_amd64": ("windows", "amd64"),
    "win_arm64": ("windows", "arm64"),
    "manylinux_2_28_x86_64": ("linux", "amd64"),
    "manylinux_2_28_aarch64": ("linux", "arm64"),
    "macosx_10_9_x86_64": ("osx", "amd64"),
    "macosx_11_0_arm64": ("osx", "arm64"),
}


def get_rclone_variant(platform_tag: str):
    try:
        return PLATFORMS[platform_tag]
    except KeyError:
        raise RuntimeError(f"Unsupported platform tag '{platform_tag}'. Supported: {sorted(PLATFORMS)}") from None


def detect_supported_platform_tag():
    """Return the first sys_tags() platform that we explicitly support."""
    for tag in sys_tags():
        if tag.platform in PLATFORMS:
            return tag.platform

    raise RuntimeError(f"No supported platform tag found for this interpreter. Supported: {sorted(PLATFORMS)}")


def rclone_download(system: str, arch: str, rclone_version: str, dest: pathlib.Path) -> pathlib.Path:
    shutil.rmtree(dest, ignore_errors=True)  # ensure clean bin dir, because build dir might be reused.
    dest.mkdir(parents=True, exist_ok=True)

    base_url = f"https://downloads.rclone.org/v{rclone_version}"
    filename = f"rclone-v{rclone_version}-{system}-{arch}.zip"
    url = f"{base_url}/{filename}"
    sums_url = f"{base_url}/SHA256SUMS"

    print(f"Downloading rclone from {url}")

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
                break

    assert bin_path.is_file(), f"Binary '{bin_name}' not found inside rclone archive {filename}"

    print(f"unpacked rclone to {bin_path}")

    if system != "windows":
        bin_path.chmod(0o755)

    print("unpacking done")

    return bin_path


class BuildWithRclone(build_py):
    def initialize_options(self):
        super().initialize_options()
        self.platform_tag = rclone_build_platform or detect_supported_platform_tag()

    def run(self):
        # 1. Run normal build first (creates build_lib)
        super().run()

        rclone_sys, rclone_arch = get_rclone_variant(self.platform_tag)

        # 2. Determine where to place the binary inside the wheel
        if self.editable_mode:
            print("editable installation!")
            pkg_dir = pathlib.Path(self.get_package_dir("rclone_api")) / "bin"

            bin_name = "rclone.exe" if rclone_sys == "windows" else "rclone"
            if pkg_dir.joinpath(bin_name).exists():
                print("not downloading rclone as it already exists.")
                return
        else:
            pkg_dir = pathlib.Path(self.build_lib) / "rclone_api/bin"

        print(pkg_dir)

        # 3. Download rclone

        print(f"Downloading binary for {rclone_sys}_{rclone_arch}")
        rclone_download(rclone_sys, rclone_arch, rclone_version, pkg_dir)


class PlatformWheel(bdist_wheel):
    def initialize_options(self):
        super().initialize_options()
        self.platform_tag = rclone_build_platform or detect_supported_platform_tag()

    def get_tag(self):
        return ("py3", "none", self.platform_tag)


setup(
    cmdclass={
        "build_py": BuildWithRclone,
        "bdist_wheel": PlatformWheel,
    },
)
