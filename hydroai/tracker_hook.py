import ctypes
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .storage import Storage, save_session, load_session
from .watercalc import (
    estimate_water,
    format_water,
    identify_tool,
    API_PATTERNS,
)

HERE = Path(__file__).parent
C_SOURCE = HERE / "tracker_ldpreload.c"
SO_PATH = Path(tempfile.gettempdir()) / "libwatermeter.so"

SHM_PATH = Path(tempfile.gettempdir()) / "watermeter_shm"
SHM_SIZE = 16392

KNOWN_HOSTNAMES = set()
for api, hosts in API_PATTERNS.items():
    for h in hosts:
        KNOWN_HOSTNAMES.add(h)

KNOWN_PROCS = ["opencode", "claude"]

SHM_ENTRY_SIZE = 64
SHM_MAGIC_OFFSET = 0
SHM_ENTRIES_OFFSET = 8


def compile_so():
    if SO_PATH.exists():
        so_mtime = SO_PATH.stat().st_mtime
        c_mtime = C_SOURCE.stat().st_mtime if C_SOURCE.exists() else 0
        if c_mtime <= so_mtime:
            return str(SO_PATH)

    cmd = [
        "gcc",
        "-shared",
        "-fPIC",
        "-o", str(SO_PATH),
        str(C_SOURCE),
        "-ldl",
        "-lpthread",
        "-O2",
        "-Wall",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   Compilation failed:\n{result.stderr}", file=sys.stderr)
        return None
    return str(SO_PATH)


def resolve_ai_ips():
    ips = set()
    for hostname in KNOWN_HOSTNAMES:
        try:
            addrs = socket.getaddrinfo(hostname, 443, socket.AF_INET)
            for addr in addrs:
                ips.add(addr[4][0])
        except OSError:
            pass
    return ips


def create_shm():
    if SHM_PATH.exists():
        SHM_PATH.unlink()
    fd = os.open(str(SHM_PATH), os.O_CREAT | os.O_RDWR, 0o666)
    os.ftruncate(fd, SHM_SIZE)
    mmap_data = b"\x00" * SHM_SIZE
    os.write(fd, mmap_data)
    os.close(fd)
    return str(SHM_PATH)


def read_shm(shm_path: str) -> list[dict]:
    try:
        with open(shm_path, "rb") as f:
            data = f.read(SHM_SIZE)
    except OSError:
        return []

    entries = []
    for i in range(256):
        offset = SHM_ENTRIES_OFFSET + i * SHM_ENTRY_SIZE
        if offset + SHM_ENTRY_SIZE > len(data):
            break
        raw = data[offset:offset + SHM_ENTRY_SIZE]
        pid, sent, recvd, last_active, comm_raw = struct.unpack_from("<i4xQQQ32s", raw)
        comm = comm_raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()
        if pid > 0:
            entries.append({
                "pid": pid,
                "comm": comm,
                "bytes_sent": sent,
                "bytes_received": recvd,
                "last_active": last_active,
            })
    return entries


def get_tool_from_comm(comm: str) -> str:
    return identify_tool(comm)


def read_shm_live(shm_path: str, storage: Storage, interval: float = 1.0):
    seen_pids = {}
    while True:
        entries = read_shm(shm_path)
        for entry in entries:
            pid = entry["pid"]
            prev = seen_pids.get(pid)
            if prev is not None:
                new_sent = entry["bytes_sent"] - prev["bytes_sent"]
                new_recv = entry["bytes_received"] - prev["bytes_received"]
                if new_sent > 0 or new_recv > 0:
                    tool = get_tool_from_comm(entry["comm"])
                    api = "anthropic" if "anthropic" in " ".join(KNOWN_HOSTNAMES) else "openai"
                    water_ml, in_tok, out_tok = estimate_water(
                        new_sent, new_recv, api
                    )
                    session_id = load_session() or f"session_{int(time.time())}"
                    save_session(session_id)
                    storage.record_call(
                        tool=tool,
                        api=api,
                        endpoint=f"pid:{pid}",
                        bytes_sent=new_sent,
                        bytes_received=new_recv,
                        estimated_input_tokens=in_tok,
                        estimated_output_tokens=out_tok,
                        water_ml=water_ml,
                        duration_sec=0,
                        session_id=session_id,
                    )
            seen_pids[pid] = entry
        time.sleep(interval)
