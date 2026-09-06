"""RecallOps VM sidecar watcher.

Runs as PID 1 of a "project VM" container (or a systemd unit on a real VM):
supervises the app process, tails its output, sends heartbeats, and reports
crashes / failed health checks to the agent backend, which diagnoses the
incident and opens a draft fix PR. Restarts the app after a crash.

Configuration (env):
  AGENT_URL           e.g. http://agent-backend:8000
  VM_ID               numeric id returned by POST /api/vms
  VM_API_KEY          key returned once by POST /api/vms
  APP_CMD             e.g. "node server.js"
  HEALTH_URL          e.g. http://localhost:3000/health (optional)
  HEALTH_INTERVAL     seconds between health checks (default 5)
  HEALTH_FAILURES     consecutive failures before reporting (default 3)
  HEARTBEAT_INTERVAL  seconds between heartbeats (default 15)
  RESTART_DELAY       seconds to wait before restarting the app (default 5)
  MAX_LOG_LINES       ring buffer size shipped with events (default 200)
"""

import asyncio
import hashlib
import os
import re
import shlex
import signal
import sys
import time
from collections import deque

import httpx

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8000").rstrip("/")
VM_ID = os.environ.get("VM_ID", "")
VM_API_KEY = os.environ.get("VM_API_KEY", "")
APP_CMD = os.environ.get("APP_CMD", "")
HEALTH_URL = os.environ.get("HEALTH_URL", "")
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "5"))
HEALTH_FAILURES = int(os.environ.get("HEALTH_FAILURES", "3"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
RESTART_DELAY = float(os.environ.get("RESTART_DELAY", "5"))
MAX_LOG_LINES = int(os.environ.get("MAX_LOG_LINES", "200"))
COOLDOWN_SECONDS = float(os.environ.get("EVENT_COOLDOWN_SECONDS", "600"))

log_buffer: deque[str] = deque(maxlen=MAX_LOG_LINES)
recent_signatures: dict[str, float] = {}
app_process: asyncio.subprocess.Process | None = None
app_started_at = time.monotonic()
shutting_down = False

ERROR_LINE = re.compile(r"(error|exception|traceback|fatal)", re.IGNORECASE)
NOISE = re.compile(r"(0x[0-9a-f]+|\d+)")


def log(message: str) -> None:
    print(f"[watcher] {message}", flush=True)


def signature(event_type: str) -> str:
    last_error = next(
        (line for line in reversed(log_buffer) if ERROR_LINE.search(line)), ""
    )
    normalized = NOISE.sub("#", last_error.strip())[:300]
    return hashlib.sha256(f"{event_type}:{normalized}".encode()).hexdigest()[:32]


def cooled_down(sig: str) -> bool:
    now = time.monotonic()
    for key, seen in list(recent_signatures.items()):
        if now - seen > COOLDOWN_SECONDS:
            del recent_signatures[key]
    if sig in recent_signatures:
        return False
    recent_signatures[sig] = now
    return True


async def post(path: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{AGENT_URL}{path}",
                json=payload,
                headers={"X-VM-Api-Key": VM_API_KEY},
            )
        if response.status_code >= 400:
            log(f"agent rejected {path}: {response.status_code} {response.text[:300]}")
        elif path.endswith("/events"):
            log(f"event reported: {response.text[:300]}")
    except Exception as error:
        log(f"failed to reach agent at {path}: {error}")


async def report_event(event_type: str, message: str, exit_code: int | None = None) -> None:
    sig = signature(event_type)
    if not cooled_down(sig):
        log(f"suppressed duplicate {event_type} (cooldown)")
        return
    await post(
        f"/api/vms/{VM_ID}/events",
        {
            "type": event_type,
            "signature": sig,
            "message": message,
            "exitCode": exit_code,
            "logExcerpt": "\n".join(log_buffer)[-100_000:],
            "occurredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def pump_output(stream: asyncio.StreamReader) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n")
        log_buffer.append(text)
        print(text, flush=True)


async def run_app() -> None:
    global app_process, app_started_at
    while not shutting_down:
        log(f"starting app: {APP_CMD}")
        app_started_at = time.monotonic()
        app_process = await asyncio.create_subprocess_exec(
            *shlex.split(APP_CMD),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        pump = asyncio.create_task(pump_output(app_process.stdout))
        code = await app_process.wait()
        await pump
        if shutting_down:
            return
        log(f"app exited with code {code}")
        await report_event(
            "crash", f"process exited with code {code}", exit_code=code
        )
        log(f"restarting in {RESTART_DELAY}s")
        await asyncio.sleep(RESTART_DELAY)


async def heartbeat_loop() -> None:
    while not shutting_down:
        alive = app_process is not None and app_process.returncode is None
        await post(
            f"/api/vms/{VM_ID}/heartbeat",
            {
                "status": "online" if alive else "degraded",
                "appPid": app_process.pid if alive else None,
                "uptimeSeconds": time.monotonic() - app_started_at,
            },
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def health_loop() -> None:
    if not HEALTH_URL:
        return
    failures = 0
    reported = False
    while not shutting_down:
        await asyncio.sleep(HEALTH_INTERVAL)
        if app_process is None or app_process.returncode is not None:
            failures = 0
            reported = False
            continue
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(HEALTH_URL)
            healthy = response.status_code < 400
            detail = f"HTTP {response.status_code}"
        except Exception as error:
            healthy = False
            detail = str(error)
        if healthy:
            failures = 0
            reported = False
            continue
        failures += 1
        log(f"health check failed ({failures}/{HEALTH_FAILURES}): {detail}")
        if failures >= HEALTH_FAILURES and not reported:
            reported = True
            await report_event(
                "health_check_failed",
                f"{HEALTH_FAILURES} consecutive health check failures: {detail}",
            )


def handle_signal() -> None:
    global shutting_down
    shutting_down = True
    if app_process and app_process.returncode is None:
        app_process.terminate()


async def main() -> int:
    if not (VM_ID and VM_API_KEY and APP_CMD):
        log("VM_ID, VM_API_KEY and APP_CMD are required")
        return 1
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:  # Windows host runs
            pass
    await asyncio.gather(run_app(), heartbeat_loop(), health_loop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
