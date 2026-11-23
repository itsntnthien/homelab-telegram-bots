#!/usr/bin/env python3
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
STATE_DIR = Path(os.environ.get("CONTROL_STATE_DIR", str(Path.home() / ".local/state/m720q-control-bot"))).expanduser()
STATE_FILE = STATE_DIR / "state.json"
BACKUP_ROOT = Path(os.environ.get("CONTROL_BACKUP_DIR", str(Path.home() / "homelab/backups"))).expanduser()
ADGUARD_CONTAINER = os.environ.get("ADGUARD_CONTAINER", "adguard-adguardhome-1")
ADGUARD_DNS_SERVER = os.environ.get("ADGUARD_DNS_SERVER", "127.0.0.1")
ADGUARD_CONF_VOLUME = os.environ.get("ADGUARD_CONF_VOLUME", "adguard_adguard_conf")
ADGUARD_WORK_VOLUME = os.environ.get("ADGUARD_WORK_VOLUME", "adguard_adguard_work")
PENDING_TTL = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("m720q-control-bot")


def api(method: str, payload: dict) -> object:
    request = urllib.request.Request(
        f"{BASE_URL}/{method}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram network error: {exc.reason}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
    return data.get("result")


def send(chat_id: str, text: str) -> None:
    for start in range(0, len(text) or 1, 4000):
        api("sendMessage", {"chat_id": chat_id, "text": text[start : start + 4000] or " "})


def run(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    output = (result.stdout or result.stderr).strip()
    if result.returncode:
        raise RuntimeError(output or f"Lệnh thất bại ({result.returncode})")
    return output


def command_of(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def status() -> str:
    uptime_seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    days, remainder = divmod(uptime_seconds, 86400)
    hours, minutes = divmod(remainder, 3600)[0], divmod(remainder, 3600)[1] // 60
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0])
    used_gib = (memory["MemTotal"] - memory["MemAvailable"]) / 1024**2
    total_gib = memory["MemTotal"] / 1024**2
    disk = shutil.disk_usage("/")
    running = run("docker", "ps", "-q").count("\n") + bool(run("docker", "ps", "-q"))
    return (
        "M720Q đang hoạt động\n"
        f"Uptime: {days} ngày {hours} giờ {minutes} phút\n"
        f"Load: {os.getloadavg()[0]:.2f}\n"
        f"RAM: {used_gib:.1f}/{total_gib:.1f} GiB\n"
        f"Disk: {disk.used / 1024**3:.1f}/{disk.total / 1024**3:.1f} GiB\n"
        f"Containers: {running} đang chạy"
    )


def services() -> str:
    output = run("docker", "ps", "--format", "{{.Names}}|{{.Status}}")
    return "Dịch vụ:\n" + "\n".join(line.replace("|", ": ") for line in output.splitlines())


def adguard() -> str:
    state = run("docker", "inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}", ADGUARD_CONTAINER)
    blocked = run("dig", "+short", f"@{ADGUARD_DNS_SERVER}", "doubleclick.net", "A")
    leases_raw = run("docker", "exec", ADGUARD_CONTAINER, "cat", "/opt/adguardhome/work/data/leases.json")
    leases = json.loads(leases_raw)
    return (
        f"AdGuard: {state.split('|', 1)[0]}\n"
        f"DHCP leases: {len(leases)}\n"
        f"Kiểm tra quảng cáo: {blocked or 'NXDOMAIN'}"
    )


def backup_adguard() -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / f"{stamp}-adguard-bot"
    target.mkdir(mode=0o700, parents=True)
    run("docker", "exec", ADGUARD_CONTAINER, "sync")
    for volume, filename in (
        (ADGUARD_CONF_VOLUME, "adguard_conf.tgz"),
        (ADGUARD_WORK_VOLUME, "adguard_work.tgz"),
    ):
        run(
            "docker", "run", "--rm",
            "-v", f"{volume}:/data:ro",
            "-v", f"{target}:/backup",
            "alpine:latest", "tar", "czf", f"/backup/{filename}", "-C", "/data", ".",
            timeout=120,
        )
    (target / "adguard-inspect.json").write_text(
        run("docker", "inspect", ADGUARD_CONTAINER), encoding="utf-8"
    )
    run("docker", "run", "--rm", "-v", f"{target}:/backup", "alpine:latest", "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/backup")
    files = [target / "adguard_conf.tgz", target / "adguard_work.tgz", target / "adguard-inspect.json"]
    checksums = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        checksums.append(f"{digest}  {path.name}")
        run("tar", "-tzf", str(path), timeout=120) if path.suffix == ".tgz" else None
    (target / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    for path in target.iterdir():
        path.chmod(0o600)
    size = sum(path.stat().st_size for path in target.iterdir()) / 1024**2
    return f"Backup hoàn tất ({size:.1f} MiB)\n{target}"


HELP = """Bot quản trị M720Q
/status - CPU/RAM/disk/uptime
/services - trạng thái container
/adguard - trạng thái AdGuard/DHCP
/backup_adguard - tạo backup
/restart_adguard - yêu cầu restart AdGuard
/reboot - yêu cầu reboot M720Q
/cancel - hủy xác nhận"""


def dispatch(chat_id: str, text: str, pending: dict) -> str | None:
    command, args = command_of(text)
    if command in ("/start", "/help"):
        return HELP
    if command == "/status":
        return status()
    if command == "/services":
        return services()
    if command == "/adguard":
        return adguard()
    if command == "/backup_adguard":
        return backup_adguard()
    if command in ("/restart_adguard", "/reboot"):
        action = command[1:]
        pending[chat_id] = (action, time.monotonic() + PENDING_TTL)
        return f"Xác nhận trong {PENDING_TTL} giây:\n/confirm {action}\nHoặc /cancel"
    if command == "/cancel":
        pending.pop(chat_id, None)
        return "Đã hủy."
    if command == "/confirm":
        expected = pending.pop(chat_id, None)
        if not expected or expected[1] < time.monotonic() or args != [expected[0]]:
            return "Không có yêu cầu hợp lệ để xác nhận."
        if expected[0] == "restart_adguard":
            run("docker", "restart", ADGUARD_CONTAINER, timeout=60)
            return "AdGuard đã khởi động lại."
        send(chat_id, "M720Q đang khởi động lại…")
        run("sudo", "-n", "/usr/bin/systemctl", "reboot")
        return None
    return HELP


def load_offset() -> int | None:
    try:
        return int(json.loads(STATE_FILE.read_text())["offset"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def save_offset(offset: int) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    STATE_FILE.chmod(0o600)


def self_test() -> None:
    assert command_of(" /STATUS@M720QBot ") == ("/status", [])
    assert command_of("/confirm reboot") == ("/confirm", ["reboot"])
    pending = {"1": ("reboot", time.monotonic() - 1)}
    assert dispatch("1", "/confirm reboot", pending) == "Không có yêu cầu hợp lệ để xác nhận."
    print("self-test: OK")


def main() -> None:
    if not TOKEN or not ADMIN_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID are required")
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (STATE_DIR / "bot.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    api("deleteWebhook", {"drop_pending_updates": False})
    offset = load_offset()
    if offset is None:
        backlog = api("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["message"]})
        offset = backlog[-1]["update_id"] + 1 if backlog else 0
        save_offset(offset)
    pending: dict[str, tuple[str, float]] = {}
    log.info("bot started")
    while True:
        try:
            updates = api("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
            for update in updates:
                offset = update["update_id"] + 1
                save_offset(offset)
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "")
                if chat_id != ADMIN_CHAT_ID or not text:
                    log.warning("ignored unauthorized message")
                    continue
                try:
                    reply = dispatch(chat_id, text, pending)
                    if reply:
                        send(chat_id, reply)
                except Exception as exc:
                    log.error("command failed: %s", type(exc).__name__)
                    send(chat_id, f"Lệnh thất bại: {exc}")
        except Exception as exc:
            log.error("poll failed: %s", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
