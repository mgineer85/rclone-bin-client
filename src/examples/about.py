import time

from rclone_bin_client.client import RcloneClient

if __name__ == "__main__":
    rc = RcloneClient()
    rc.start()
    while not rc.operational():
        time.sleep(0.1)
    print(rc.version())
    rc.stop()
