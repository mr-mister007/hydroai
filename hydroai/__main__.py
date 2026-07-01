import argparse
import os
import signal
import sys
import time
import threading

from .storage import (
    Storage,
    load_pid,
    clear_pid,
    clear_session,
    save_session,
    load_session,
)
from .proxy import ProxyServer
from .watercalc import format_water, estimate_water


def cmd_start(args):
    proxy = ProxyServer(host=args.host, port=args.port)

    def shutdown(signum, frame):
        print("\n   Shutting down watermeter...")
        proxy.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    proxy.start()


def cmd_stop(args):
    pid = load_pid()
    if pid is None:
        print("   watermeter is not running")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        clear_pid()
        print("   watermeter stopped")
    except ProcessLookupError:
        clear_pid()
        print("   watermeter was not running (stale PID file cleaned)")


def cmd_status(args):
    pid = load_pid()
    running = pid is not None

    storage = Storage()
    session_id = load_session()

    if running:
        print(f"   watermeter is RUNNING (PID: {pid})")
    else:
        print("   watermeter is STOPPED")

    if session_id:
        stats = storage.get_session_stats(session_id)
        print(f"\n   Session: {session_id}")
        print(f"   API calls:     {stats.get('calls', 0)}")
        print(
            f"   Data sent:     {_fmt_bytes(stats.get('total_bytes_sent', 0))}"
        )
        print(
            f"   Data received: {_fmt_bytes(stats.get('total_bytes_received', 0))}"
        )
        print(
            f"   Est. tokens:   {stats.get('total_input_tokens', 0):,} in / {stats.get('total_output_tokens', 0):,} out"
        )
        print(
            f"   Water used:    {format_water(stats.get('total_water_ml', 0))}"
        )

    all_stats = storage.get_all_stats()
    if all_stats.get("calls", 0) > 0 and (
        not session_id
        or stats.get("calls", 0) != all_stats.get("calls", 0)
    ):
        print(f"\n   All-time:")
        print(f"   Total calls:   {all_stats['calls']}")
        print(
            f"   Total water:   {format_water(all_stats['total_water_ml'])}"
        )

    storage.close()


def cmd_stats(args):
    storage = Storage()
    all_stats = storage.get_all_stats()

    if all_stats.get("calls", 0) == 0:
        print("   No usage data recorded yet.")
        print("   Start the proxy and route your AI tools through it.")
        storage.close()
        return

    print(f"\n   📊 WATERMETER STATISTICS")
    print(f"   {'=' * 40}")
    print(f"   Total API calls:             {all_stats['calls']}")
    print(
        f"   Total data sent:             {_fmt_bytes(all_stats['total_bytes_sent'])}"
    )
    print(
        f"   Total data received:         {_fmt_bytes(all_stats['total_bytes_received'])}"
    )
    print(
        f"   Estimated input tokens:      {all_stats['total_input_tokens']:,}"
    )
    print(
        f"   Estimated output tokens:     {all_stats['total_output_tokens']:,}"
    )
    print(
        f"   Total estimated water used:  {format_water(all_stats['total_water_ml'])}"
    )
    print(
        f"   Total duration:              {_fmt_duration(all_stats['total_duration_sec'])}"
    )

    print(f"\n   Breakdown by tool:")
    for row in storage.get_breakdown("tool"):
        print(
            f"     {row['name']:<20} {row['calls']:>5} calls  {format_water(row['total_water_ml'])}"
        )

    print(f"\n   Breakdown by API:")
    for row in storage.get_breakdown("api"):
        print(
            f"     {row['name']:<20} {row['calls']:>5} calls  {format_water(row['total_water_ml'])}"
        )

    storage.close()


def cmd_hook(args):
    from .tracker_hook import compile_so, resolve_ai_ips, create_shm, KNOWN_PROCS

    so_path = compile_so()
    if not so_path:
        print("   Failed to compile the LD_PRELOAD library")
        return

    shm_path = create_shm()
    ips = resolve_ai_ips()
    ips_str = ",".join(ips)
    proc_str = ",".join(KNOWN_PROCS)

    print(f"   Library: {so_path}")
    print(f"   SHM:     {shm_path}")
    print(f"   AI IPs:  {', '.join(ips) if ips else 'none resolved'}")
    print(f"   Procs:   {', '.join(KNOWN_PROCS)}")
    print()
    print(f"   To track a tool, run it with these env vars:")
    print()
    print(f"   LD_PRELOAD={so_path} \\")
    print(f"   WATERMETER_SHM={shm_path} \\")
    print(f"   WATERMETER_IPS={ips_str} \\")
    print(f"   WATERMETER_PROC_NAMES={proc_str} \\")
    print(f"   your-command-here")
    print()
    print(f"   Or use: watermeter run <command>")


def cmd_run(args):
    import subprocess as sp
    from .tracker_hook import compile_so, resolve_ai_ips, create_shm, read_shm, KNOWN_PROCS

    so_path = compile_so()
    if not so_path:
        print("   Failed to compile the LD_PRELOAD library")
        return

    shm_path = create_shm()
    ips = resolve_ai_ips()
    ips_str = ",".join(ips)

    if not ips:
        print("   Warning: no AI API IPs resolved")

    env = os.environ.copy()
    env["LD_PRELOAD"] = so_path
    env["WATERMETER_SHM"] = shm_path
    env["WATERMETER_IPS"] = ips_str
    env["WATERMETER_PROC_NAMES"] = ",".join(KNOWN_PROCS)

    tool_cmd = args.command_args
    if not tool_cmd or tool_cmd[0] == "--":
        tool_cmd = tool_cmd[1:] if tool_cmd and tool_cmd[0] == "--" else []
    if not tool_cmd:
        print("   Usage: watermeter run <command> [args...]")
        return

    storage = Storage()
    session_id = f"session_{int(time.time())}"
    save_session(session_id)

    seen_pids = {}
    running = [True]

    def record_if_new(entry):
        from .watercalc import estimate_water, identify_tool, identify_api
        pid = entry["pid"]
        prev = seen_pids.get(pid)
        if prev is not None:
            ns = entry["bytes_sent"] - prev["bytes_sent"]
            nr = entry["bytes_received"] - prev["bytes_received"]
        else:
            ns = entry["bytes_sent"]
            nr = entry["bytes_received"]
        if ns > 0 or nr > 0:
            tool = identify_tool(entry["comm"])
            api = identify_api("api.anthropic.com")
            water_ml, in_tok, out_tok = estimate_water(ns, nr, api)
            storage.record_call(
                tool=tool, api=api, endpoint=f"pid:{pid}",
                bytes_sent=ns, bytes_received=nr,
                estimated_input_tokens=in_tok, estimated_output_tokens=out_tok,
                water_ml=water_ml, duration_sec=0, session_id=session_id,
            )
            return True
        return False

    def monitor():
        while running[0]:
            try:
                for entry in read_shm(shm_path):
                    pid = entry["pid"]
                    if pid == 0:
                        continue
                    if pid not in seen_pids:
                        record_if_new(entry)
                        seen_pids[pid] = entry.copy()
                    else:
                        prev = seen_pids[pid]
                        if (entry["bytes_sent"] != prev["bytes_sent"] or
                            entry["bytes_received"] != prev["bytes_received"]):
                            record_if_new(entry)
                            seen_pids[pid] = entry.copy()
            except Exception:
                pass
            time.sleep(1)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()

    print(f"   Running: {' '.join(tool_cmd)}")
    print(f"   Session: {session_id}")

    proc = sp.Popen(tool_cmd, env=env)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()

    running[0] = False
    time.sleep(1)

    for entry in read_shm(shm_path):
        pid = entry["pid"]
        if pid == 0:
            continue
        if pid not in seen_pids:
            record_if_new(entry)
            seen_pids[pid] = entry.copy()
        else:
            prev = seen_pids[pid]
            if (entry["bytes_sent"] != prev["bytes_sent"] or
                entry["bytes_received"] != prev["bytes_received"]):
                ns = entry["bytes_sent"] - prev["bytes_sent"]
                nr = entry["bytes_received"] - prev["bytes_received"]
                if ns > 0 or nr > 0:
                    from .watercalc import estimate_water, identify_tool, identify_api
                    tool = identify_tool(entry["comm"])
                    api = identify_api("api.anthropic.com")
                    water_ml, in_tok, out_tok = estimate_water(ns, nr, api)
                    storage.record_call(tool=tool, api=api, endpoint=f"pid:{pid}",
                        bytes_sent=ns, bytes_received=nr,
                        estimated_input_tokens=in_tok, estimated_output_tokens=out_tok,
                        water_ml=water_ml, duration_sec=0, session_id=session_id)
                    seen_pids[pid] = entry.copy()

    total = storage.get_session_stats(session_id)
    print()
    print(f"   {'=' * 40}")
    print(f"   Total water used this session: {format_water(total.get('total_water_ml', 0))}")
    print(f"   API calls: {total.get('calls', 0)}")
    print(f"   Output tokens: {total.get('total_output_tokens', 0):,}")
    storage.close()


def cmd_estimate(args):
    tokens = args.tokens
    water_ml, in_tok, out_tok = estimate_water(
        tokens * 4, tokens * 4, "anthropic"
    )
    print(f"   {tokens:,} tokens → ~{format_water(water_ml)} of water")


def cmd_watch(args):
    storage = Storage()
    session_id = load_session() or f"session_{int(time.time())}"
    save_session(session_id)

    print(f"   Watching water usage (Ctrl+C to stop)...\n")

    last_calls = storage.get_session_stats(session_id).get("calls", 0)

    try:
        while True:
            time.sleep(2)
            stats = storage.get_session_stats(session_id)
            calls = stats.get("calls", 0)
            if calls > last_calls:
                new_calls = calls - last_calls
                last_calls = calls
            water = stats.get("total_water_ml", 0)

            bar = _water_bar(water)
            sys.stdout.write(
                f"\r   {bar}  {format_water(water)}  |  {calls} calls  |  {stats.get('total_output_tokens', 0):,} tokens out  "
            )
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n")
        total = storage.get_session_stats(session_id)
        print(
            f"   Total water used this session: {format_water(total.get('total_water_ml', 0))}"
        )

    storage.close()


def _water_bar(water_ml: float) -> str:
    max_ml = 2000
    filled = min(int((water_ml / max_ml) * 20), 20)
    return "█" * filled + "░" * (20 - filled)


def _fmt_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.1f} MB"


def _fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.0f}m {sec % 60:.0f}s"
    return f"{sec / 3600:.1f}h"


def main():
    parser = argparse.ArgumentParser(
        description="hydroai - Track AI water consumption from network traffic"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start the proxy server")
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument("--port", type=int, default=8090)

    sub.add_parser("stop", help="Stop the proxy server")
    sub.add_parser("status", help="Show current status and session stats")
    sub.add_parser("stats", help="Show all-time statistics")

    p_est = sub.add_parser("estimate", help="Estimate water for N tokens")
    p_est.add_argument("tokens", type=int, help="Number of tokens")

    sub.add_parser("watch", help="Live water usage monitor")

    sub.add_parser("hook", help="Show how to use LD_PRELOAD hook")
    p_run = sub.add_parser("run", help="Run a command with LD_PRELOAD hook")
    p_run.add_argument("command_args", nargs=argparse.REMAINDER,
                       help="Command to run (use -- to separate flags)")

    sub.add_parser("reset", help="Reset all data")

    args = parser.parse_args()

    if args.command == "reset":

        db_path = os.path.expanduser("~/.watermeter/watermeter.db")
        try:
            os.remove(db_path)
            clear_session()
            print("   All data reset")
        except FileNotFoundError:
            print("   No data to reset")
        return

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "stats": cmd_stats,
        "estimate": cmd_estimate,
        "watch": cmd_watch,
        "hook": cmd_hook,
        "run": cmd_run,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
