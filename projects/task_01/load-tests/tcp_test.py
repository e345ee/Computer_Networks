#!/usr/bin/env python3
import argparse
import socket

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Create one job and test ping/load/stats over allocated TCP port")
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--keep", action="store_true", help="do not delete job after test")
    args = parser.parse_args()

    response = requests.post(f"{args.gateway.rstrip('/')}/api/jobs", timeout=30)
    response.raise_for_status()
    job = response.json()
    port = int(job["port"])
    print("created", job)

    with socket.create_connection((args.host, port), timeout=15) as sock:
        print(sock.recv(4096).decode(errors="replace").strip())
        for command in ["ping", "stats", "load 1", "stats"]:
            print(">", command)
            sock.sendall((command + "\n").encode())
            print(sock.recv(8192).decode(errors="replace").strip())

    if not args.keep:
        delete_response = requests.delete(f"{args.gateway.rstrip('/')}/api/jobs/{job['job_id']}", timeout=30)
        delete_response.raise_for_status()
        print("deleted", job["job_id"])


if __name__ == "__main__":
    main()
