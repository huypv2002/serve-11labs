#!/usr/bin/env python3
"""
ElevenLabs TTS Server Manager CLI

Manage TTS API servers with optional token pool and cloudflared tunnel.

Usage:
    python3 tts_server.py start [--mode basic|pool] [--port PORT] [--pool-size N] [--tunnel]
    python3 tts_server.py stop
    python3 tts_server.py status
    python3 tts_server.py logs [--follow] [--lines N]
    python3 tts_server.py tunnel [--start|--stop]

Examples:
    python3 tts_server.py start --mode pool --pool-size 5 --tunnel
    python3 tts_server.py start --mode basic --port 8899
    python3 tts_server.py status
    python3 tts_server.py stop
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
PID_FILE = BASE_DIR / ".tts_server.pid"
TUNNEL_PID_FILE = BASE_DIR / ".tts_tunnel.pid"
CONFIG_FILE = BASE_DIR / ".tts_server.json"
LOG_DIR = BASE_DIR / "logs"


def save_config(config: dict):
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def is_running(pid_file: Path) -> tuple[bool, int]:
    """Check if process is running. Returns (running, pid)."""
    if not pid_file.exists():
        return False, 0
    pid = int(pid_file.read_text().strip())
    try:
        if sys.platform == "win32":
            # Windows: use tasklist to check PID
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True
            )
            if str(pid) in result.stdout:
                return True, pid
            else:
                pid_file.unlink(missing_ok=True)
                return False, 0
        else:
            os.kill(pid, 0)
            return True, pid
    except OSError:
        pid_file.unlink(missing_ok=True)
        return False, 0


def start_server(args):
    """Start TTS API server."""
    running, pid = is_running(PID_FILE)
    if running:
        print(f"[!] Server already running (PID {pid}). Stop it first: python3 tts_server.py stop")
        return

    LOG_DIR.mkdir(exist_ok=True)

    mode = args.mode
    port = args.port
    pool_size = args.pool_size

    if mode == "basic":
        script = "elevenlabs_api.py"
        cmd = [sys.executable, "-u", str(BASE_DIR / script), "--port", str(port)]
        log_file = LOG_DIR / "server_basic.log"
    else:
        script = "elevenlabs_api_pool.py"
        cmd = [sys.executable, "-u", str(BASE_DIR / script), "--port", str(port), "--pool-size", str(pool_size)]
        log_file = LOG_DIR / "server_pool.log"

    print(f"[*] Starting TTS server...")
    print(f"    Mode: {mode}")
    print(f"    Port: {port}")
    if mode == "pool":
        print(f"    Pool size: {pool_size}")
    print(f"    Script: {script}")
    print(f"    Log: {log_file}")

    with open(log_file, "a") as lf:
        kwargs = {"stdout": lf, "stderr": subprocess.STDOUT, "cwd": str(BASE_DIR)}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)

    PID_FILE.write_text(str(proc.pid))
    save_config({
        "mode": mode,
        "port": port,
        "pool_size": pool_size,
        "pid": proc.pid,
        "log_file": str(log_file),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    # Wait for server to be ready
    print(f"    PID: {proc.pid}")
    print(f"[*] Waiting for server to start...", end="", flush=True)
    for i in range(30):
        time.sleep(1)
        print(".", end="", flush=True)
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2)
            if resp.status == 200:
                print(f"\n[✓] Server ready at http://localhost:{port}")
                break
        except Exception:
            pass
    else:
        print(f"\n[!] Server may still be starting. Check logs: python3 tts_server.py logs")

    # Start tunnel if requested
    if args.tunnel:
        start_tunnel(port)


def stop_server(args=None):
    """Stop TTS API server and tunnel."""
    # Stop server
    running, pid = is_running(PID_FILE)
    if running:
        print(f"[*] Stopping server (PID {pid})...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        PID_FILE.unlink(missing_ok=True)
        print(f"[✓] Server stopped")
    else:
        print(f"[*] Server not running")

    # Stop tunnel
    stop_tunnel()


def start_tunnel(port: int = None):
    """Start cloudflared named tunnel (tts-api → tts.liveyt.pro)."""
    running, pid = is_running(TUNNEL_PID_FILE)
    if running:
        print(f"[!] Tunnel already running (PID {pid})")
        return

    LOG_DIR.mkdir(exist_ok=True)
    tunnel_log = LOG_DIR / "tunnel.log"
    tunnel_config = Path.home() / ".cloudflared" / "tts-api.yml"

    if not tunnel_config.exists():
        print(f"[!] Tunnel config not found: {tunnel_config}")
        return

    print(f"[*] Starting cloudflared tunnel (tts.liveyt.pro)...")
    with open(tunnel_log, "w") as lf:
        kwargs = {"stdout": lf, "stderr": subprocess.STDOUT}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--config", str(tunnel_config), "run", "tts-api"],
            **kwargs
        )

    TUNNEL_PID_FILE.write_text(str(proc.pid))

    # Wait for tunnel connection
    print(f"    PID: {proc.pid}")
    print(f"[*] Waiting for tunnel...", end="", flush=True)
    tunnel_url = "https://tts.liveyt.pro"
    for i in range(15):
        time.sleep(1)
        print(".", end="", flush=True)
        if tunnel_log.exists():
            content = tunnel_log.read_text()
            if "Registered tunnel connection" in content:
                break

    print(f"\n[✓] Tunnel ready: {tunnel_url}")
    config = load_config()
    config["tunnel_url"] = tunnel_url
    config["tunnel_pid"] = proc.pid
    save_config(config)


def stop_tunnel(args=None):
    """Stop cloudflared tunnel."""
    running, pid = is_running(TUNNEL_PID_FILE)
    if running:
        print(f"[*] Stopping tunnel (PID {pid})...")
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        TUNNEL_PID_FILE.unlink(missing_ok=True)
        print(f"[✓] Tunnel stopped")
    else:
        print(f"[*] Tunnel not running")


def show_status(args=None):
    """Show server and tunnel status."""
    config = load_config()

    print("=" * 50)
    print("  ElevenLabs TTS Server Status")
    print("=" * 50)

    # Server status
    running, pid = is_running(PID_FILE)
    if running:
        mode = config.get("mode", "unknown")
        port = config.get("port", "?")
        pool_size = config.get("pool_size", "?")
        started = config.get("started_at", "?")
        print(f"\n  Server:  ✓ RUNNING (PID {pid})")
        print(f"  Mode:    {mode}")
        print(f"  Port:    {port}")
        if mode == "pool":
            print(f"  Pool:    {pool_size} tokens")
        print(f"  Started: {started}")
        print(f"  URL:     http://localhost:{port}")

        # Try health check
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3)
            health = json.loads(resp.read())
            if "pool_size" in health:
                print(f"\n  Pool Stats:")
                print(f"    Available tokens: {health['pool_size']}/{health['pool_target']}")
                print(f"    Solving now:      {health['solving_now']}")
                print(f"    Total solved:     {health['total_solved']}")
                print(f"    Total served:     {health['total_served']}")
                print(f"    Total expired:    {health['total_expired']}")
        except Exception:
            pass
    else:
        print(f"\n  Server:  ✗ STOPPED")

    # Tunnel status
    running_t, pid_t = is_running(TUNNEL_PID_FILE)
    if running_t:
        tunnel_url = config.get("tunnel_url", "unknown")
        print(f"\n  Tunnel:  ✓ RUNNING (PID {pid_t})")
        print(f"  Public:  {tunnel_url}")
    else:
        print(f"\n  Tunnel:  ✗ STOPPED")

    # Log files
    print(f"\n  Logs:")
    log_file = config.get("log_file")
    if log_file and Path(log_file).exists():
        print(f"    Server: {log_file}")
    print(f"    Tunnel: {LOG_DIR / 'tunnel.log'}")
    if (BASE_DIR / "api_requests.log").exists():
        print(f"    API:    {BASE_DIR / 'api_requests.log'}")
    if (BASE_DIR / "api_pool_requests.log").exists():
        print(f"    Pool:   {BASE_DIR / 'api_pool_requests.log'}")

    print("=" * 50)


def show_logs(args):
    """Show server logs."""
    config = load_config()
    log_file = config.get("log_file")

    if args.api:
        mode = config.get("mode", "basic")
        if mode == "pool":
            log_file = str(BASE_DIR / "api_pool_requests.log")
        else:
            log_file = str(BASE_DIR / "api_requests.log")

    if not log_file or not Path(log_file).exists():
        print("[!] No log file found. Is the server running?")
        return

    lines = args.lines or 30

    if args.follow:
        os.execvp("tail", ["tail", "-f", log_file])
    else:
        result = subprocess.run(["tail", f"-{lines}", log_file], capture_output=True, text=True)
        print(result.stdout)


def handle_tunnel(args):
    """Manage tunnel separately."""
    if args.stop:
        stop_tunnel()
    else:
        config = load_config()
        port = config.get("port", 8899)
        start_tunnel(port)


def main():
    parser = argparse.ArgumentParser(
        description="ElevenLabs TTS Server Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s start --mode pool --pool-size 5 --tunnel   Start pool server + public tunnel
  %(prog)s start --mode basic --port 8899             Start basic server (no pool)
  %(prog)s status                                     Show server status
  %(prog)s logs --follow --api                        Follow API request logs
  %(prog)s stop                                       Stop everything
  %(prog)s tunnel --stop                              Stop tunnel only
"""
    )
    sub = parser.add_subparsers(dest="command")

    # start
    p_start = sub.add_parser("start", help="Start TTS server")
    p_start.add_argument("--mode", choices=["basic", "pool"], default="pool", help="Server mode (default: pool)")
    p_start.add_argument("--port", type=int, default=8899, help="Port (default: 8899)")
    p_start.add_argument("--pool-size", type=int, default=5, help="Token pool size (default: 5)")
    p_start.add_argument("--tunnel", action="store_true", help="Start cloudflared tunnel")
    p_start.set_defaults(func=start_server)

    # stop
    p_stop = sub.add_parser("stop", help="Stop server and tunnel")
    p_stop.set_defaults(func=stop_server)

    # status
    p_status = sub.add_parser("status", help="Show status")
    p_status.set_defaults(func=show_status)

    # logs
    p_logs = sub.add_parser("logs", help="Show logs")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    p_logs.add_argument("--lines", "-n", type=int, help="Number of lines (default: 30)")
    p_logs.add_argument("--api", action="store_true", help="Show API request logs instead of server logs")
    p_logs.set_defaults(func=show_logs)

    # tunnel
    p_tunnel = sub.add_parser("tunnel", help="Manage tunnel")
    p_tunnel.add_argument("--stop", action="store_true", help="Stop tunnel")
    p_tunnel.set_defaults(func=handle_tunnel)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
