"""Serialize dashboard self-heal launch-marker transitions."""
from hook_provenance import _exclusive


def claim(facade):
    try:
        facade.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        guard = facade.SNAPSHOT_DIR / "dashboard.heal.claim.guard"
        with _exclusive(guard, timeout=0.05) as held:
            if not held:
                try:
                    fd = facade.os.open(str(guard), facade.os.O_RDWR)
                    facade.os.close(fd)
                except OSError:
                    return True
                return False
            if facade._dashboard_selfheal_inflight():
                return False
            marker = facade.SNAPSHOT_DIR / facade._DASHBOARD_HEAL_LOCK_NAME
            try:
                fd = facade.os.open(str(marker), facade.os.O_CREAT | facade.os.O_EXCL | facade.os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if facade.time.time() - marker.stat().st_mtime <= facade._DASHBOARD_HEAL_LOCK_STALE_SECONDS:
                        return False
                    facade.os.unlink(str(marker))
                    fd = facade.os.open(str(marker), facade.os.O_CREAT | facade.os.O_EXCL | facade.os.O_WRONLY, 0o600)
                except OSError:
                    return False
            try:
                facade.os.write(fd, str(facade.os.getpid()).encode("ascii"))
            finally:
                facade.os.close(fd)
            return True
    except OSError:
        return True
