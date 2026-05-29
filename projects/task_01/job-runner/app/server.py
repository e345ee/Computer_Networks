import asyncio
import hashlib
import os
import platform
import time
from datetime import datetime, timezone

JOB_ID = os.getenv("JOB_ID", "local-job")
PORT = int(os.getenv("JOB_PORT", "7001"))
STARTED_AT = datetime.now(timezone.utc)
REQUESTS = 0


def cpu_load(seconds: float) -> str:
    deadline = time.monotonic() + max(seconds, 0.1)
    iterations = 0
    digest = b"seed"
    while time.monotonic() < deadline:
        digest = hashlib.sha256(digest + str(iterations).encode()).digest()
        iterations += 1
    return f"loaded cpu for {seconds:.1f}s, iterations={iterations}, digest={digest.hex()[:12]}"


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global REQUESTS
    peer = writer.get_extra_info("peername")
    writer.write(
        f"job-runner {JOB_ID} ready. commands: ping | load [seconds] | stats | stop | help\n".encode()
    )
    await writer.drain()
    while True:
        data = await reader.readline()
        if not data:
            break
        REQUESTS += 1
        command = data.decode(errors="replace").strip()
        parts = command.split()
        cmd = parts[0].lower() if parts else ""
        if cmd == "ping":
            response = "pong"
        elif cmd == "load":
            seconds = 2.0
            if len(parts) > 1:
                try:
                    seconds = min(max(float(parts[1]), 0.1), 30.0)
                except ValueError:
                    seconds = 2.0
            response = await asyncio.to_thread(cpu_load, seconds)
        elif cmd == "stats":
            uptime = (datetime.now(timezone.utc) - STARTED_AT).total_seconds()
            response = (
                f"job_id={JOB_ID} uptime_seconds={uptime:.1f} requests={REQUESTS} "
                f"python={platform.python_version()} peer={peer}"
            )
        elif cmd == "help":
            response = "commands: ping | load [seconds] | stats | stop | help"
        elif cmd == "stop":
            writer.write(b"stopping job container\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            os._exit(0)
        elif cmd == "":
            response = "empty command"
        else:
            response = f"unknown command: {command}"
        writer.write((response + "\n").encode())
        await writer.drain()
    writer.close()
    await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handle_client, "0.0.0.0", PORT)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"job-runner {JOB_ID} listening on {sockets}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
