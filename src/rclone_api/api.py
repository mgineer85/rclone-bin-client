import atexit
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

from . import BINARY_PATH
from .dto import AsyncJobResponse, ConfigListremotes, CoreStats, CoreVersion, JobList, JobStatus, LsJsonEntry, PubliclinkResponse
from .exceptions import RcloneConnectionException, RcloneProcessException, RclonePublicLinkNotSupportedException


class RcloneApi:
    def __init__(
        self,
        bind="localhost:5572",
        log_file: Path | None = None,
        log_level: Literal["DEBUG", "INFO", "NOTICE", "ERROR"] = "NOTICE",
        transfers: int = 4,
        checkers: int = 4,
        enable_webui: bool = False,
        bwlimit: str | None = None,
    ):
        self.__bind_addr = bind
        self.__log_file = log_file
        self.__log_level = log_level
        self.__transfers = transfers
        self.__checkers = checkers
        self.__enable_webui = enable_webui
        self.__bwlimit = bwlimit
        self.__connect_addr = f"http://{bind}"
        self.__process = None
        self.__rclone_bin = BINARY_PATH

        atexit.register(self._cleanup)

    # -------------------------
    # Lifecycle
    # -------------------------
    def start(self, startup_timeout: float | None = 5):
        if self.__process:
            return

        if self.__log_file:
            self.__log_file.parent.mkdir(parents=True, exist_ok=True)

        self.__process = subprocess.Popen(
            [
                str(self.__rclone_bin),
                "rcd",
                f"--rc-addr={self.__bind_addr}",
                "--rc-no-auth",  # TODO: add auth.
                *(["--rc-web-gui"] if self.__enable_webui else []),
                "--rc-web-gui-no-open-browser",
                # The server needs to accept at least transfers+checkers connections, otherwise sync might fail!
                # The connections could be limited, but it could cause deadlocks, so it's preferred to change transfers/checkers only
                f"--transfers={self.__transfers}",
                f"--checkers={self.__checkers}",
                *([f"--log-file={self.__log_file}"] if self.__log_file else []),
                f"--log-level={self.__log_level}",
                *([f"--bwlimit={self.__bwlimit}"] if self.__bwlimit else []),
            ],
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # for _cleanup
        )
        # during dev you might want to start on cli separately:
        # rclone rcd --rc-no-auth --rc-addr=localhost:5572 --rc-web-gui --transfers=4 --checkers=4 --bwlimit=5K

        if startup_timeout:
            self.wait_until_operational(startup_timeout)

    def wait_until_operational(self, timeout: float = 5) -> None:
        deadline = time.time() + timeout

        while time.time() < deadline:
            if not self.__process:
                # maybe instance created already but code using the api did not start yet.
                continue

            # If rclone died immediately, capture stderr and raise
            ret = self.__process.poll()
            if ret is not None:
                stderr = self.__process.stderr.read()  # type: ignore
                raise RuntimeError(f"rclone failed to start (exit={ret}): {stderr.strip()}")

            if ret is None and self.operational():
                return

            time.sleep(0.1)

        raise RuntimeError(f"rclone did not become operational after {timeout}s")

    def stop(self):
        if self.__process:
            self.__process.terminate()
            self.__process.wait(timeout=5)

            self.__process = None

    def _cleanup(self):
        # if forgot to call stop, we still kill a possible rclone instance at py program exit.
        if self.__process and self.__process.poll() is None:
            os.killpg(self.__process.pid, signal.SIGTERM)

    # -------------------------
    # Internal helper
    # -------------------------

    @staticmethod
    def _valid_fs_remote(fs: str, remote: str):
        # Remote backend: fs ends with ":" → remote must NOT be absolute

        assert not (fs.endswith(":") and Path(remote).is_absolute()), f"remote must be relative when fs is a remote: {fs=} {remote=}"

        # Local backend: fs does NOT end with ":" → remote must be absolute
        assert not (not fs.endswith(":") and not Path(fs).is_absolute()), f"fs must be absolute when remote is a local path: {fs=} {remote=}"

    def _post(self, endpoint: str, data: dict[str, Any] | None = None):
        req = urllib.request.Request(
            url=f"{self.__connect_addr}/{endpoint}",
            data=json.dumps(data or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw: bytes = resp.read()
                response_json = json.loads(raw.decode("utf-8"))

        except urllib.error.HTTPError as exc:  # non 200 HTTP codes
            raw: bytes = exc.read()
            response_json = json.loads(raw.decode("utf-8"))
            raise RcloneProcessException.from_dict(response_json) from exc
        except Exception as exc:  # all other errors
            raise RcloneConnectionException(f"Issue connecting to rclone RC server, error: {exc}") from exc
        else:
            return response_json

    def _noopauth(self, input: dict):
        return self._post("rc/noopauth", input)

    def wait_for_jobs(self, job_ids: list[int]):
        _job_ids = set(job_ids)

        while self.__process:  # only wait if there is a process running
            running = set(self.job_list().runningIds)
            if _job_ids.isdisjoint(running):
                return

            time.sleep(0.05)

    # -------------------------
    # Operations
    # -------------------------

    def deletefile(self, fs: str, remote: str) -> None:
        self._valid_fs_remote(fs, remote)
        self._post("operations/deletefile", {"fs": fs, "remote": remote})

    def copyfile(self, src_fs: str, src_remote: str, dst_fs: str, dst_remote: str) -> None:
        self._valid_fs_remote(src_fs, src_remote)
        self._valid_fs_remote(dst_fs, dst_remote)
        self._post("operations/copyfile", {"srcFs": src_fs, "srcRemote": src_remote, "dstFs": dst_fs, "dstRemote": dst_remote})

    def copyfile_async(self, src_fs: str, src_remote: str, dst_fs: str, dst_remote: str) -> AsyncJobResponse:
        self._valid_fs_remote(src_fs, src_remote)
        self._valid_fs_remote(dst_fs, dst_remote)
        result = self._post(
            "operations/copyfile", {"_async": True, "srcFs": src_fs, "srcRemote": src_remote, "dstFs": dst_fs, "dstRemote": dst_remote}
        )
        return AsyncJobResponse.from_dict(result)

    def copy(self, src_fs: str, dst_fs: str, create_empty_src_dirs: bool = False) -> None:
        self._post("sync/copy", {"srcFs": src_fs, "dstFs": dst_fs, "createEmptySrcDirs": create_empty_src_dirs})

    def copy_async(self, src_fs: str, dst_fs: str, create_empty_src_dirs: bool = False) -> AsyncJobResponse:
        result = self._post("sync/copy", {"_async": True, "srcFs": src_fs, "dstFs": dst_fs, "createEmptySrcDirs": create_empty_src_dirs})
        return AsyncJobResponse.from_dict(result)

    def sync(self, src_fs: str, dst_fs: str, create_empty_src_dirs: bool = False) -> None:
        self._post("sync/sync", {"srcFs": src_fs, "dstFs": dst_fs, "createEmptySrcDirs": create_empty_src_dirs})

    def sync_async(self, src_fs: str, dst_fs: str, create_empty_src_dirs: bool = False) -> AsyncJobResponse:
        result = self._post("sync/sync", {"_async": True, "srcFs": src_fs, "dstFs": dst_fs, "createEmptySrcDirs": create_empty_src_dirs})
        return AsyncJobResponse.from_dict(result)

    def publiclink(self, fs: str, remote: str, unlink: bool = False, expire: str | None = None) -> PubliclinkResponse:
        self._valid_fs_remote(fs, remote)
        result = self._post("operations/publiclink", {"fs": fs, "remote": remote, "unlink": unlink, **({"expire": expire} if expire else {})})

        try:
            publiclink = PubliclinkResponse.from_dict(result)
        except KeyError:
            raise RclonePublicLinkNotSupportedException(
                f"public link generation failed for remote {fs}{remote}. Maybe the remote doesn't support links."
            ) from None
        else:
            return publiclink

    def ls(self, fs: str, remote: str) -> list[LsJsonEntry]:
        self._valid_fs_remote(fs, remote)
        response: dict = self._post("operations/list", {"fs": fs, "remote": remote})
        ls: list[dict] = response["list"]
        return [LsJsonEntry.from_dict(x) for x in ls]

    # -------------------------
    # Utilities
    # -------------------------
    def job_status(self, jobid: int) -> JobStatus:
        return JobStatus.from_dict(self._post("job/status", {"jobid": jobid}))

    def job_list(self) -> JobList:
        return JobList.from_dict(self._post("job/list"))

    # def abort_job(self, jobid: int) -> None:
    #     self._post("job/stop", {"jobid": jobid})

    # def abort_jobgroup(self, group: str) -> None:
    #     self._post("job/stopgroup", {"group": group})

    def core_stats(self) -> CoreStats:
        return CoreStats.from_dict(self._post("core/stats"))

    def version(self) -> CoreVersion:
        return CoreVersion.from_dict(self._post("core/version"))

    def config_create(self, name: str, type: str, parameters: dict[str, Any]) -> None:
        return self._post("config/create", {"name": name, "type": type, "parameters": parameters})

    def config_delete(self, name: str) -> None:
        return self._post("config/delete", {"name": name})

    def config_listremotes(self) -> ConfigListremotes:
        return ConfigListremotes.from_dict(self._post("config/listremotes"))

    def operational(self) -> bool:
        chk_input = {"op": True}
        try:
            return self._noopauth(chk_input) == chk_input
        except Exception:
            return False
