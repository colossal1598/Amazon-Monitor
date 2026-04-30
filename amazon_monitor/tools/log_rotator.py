from pathlib import Path


def cleanup_old_logs(log_dir: str = "logs", keep: int = 5) -> None:
    path = Path(log_dir)
    if not path.exists():
        return
    files = sorted(path.glob("monitor.log*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


if __name__ == "__main__":
    cleanup_old_logs()

