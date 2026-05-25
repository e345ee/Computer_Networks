#!/usr/bin/env python3
import argparse
import concurrent.futures
import socket
import time
from collections import Counter

import requests


def create_job(gateway: str) -> dict:
    response = requests.post(f"{gateway.rstrip('/')}/api/jobs", timeout=30)
    response.raise_for_status()
    return response.json()


def get_system_status(gateway: str) -> dict | None:
    try:
        response = requests.get(f"{gateway.rstrip('/')}/api/system/status", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"Could not read system status before test: {exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create many jobs and show distribution by workers")
    parser.add_argument("--gateway", default="http://localhost:8080")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--delete", action="store_true", help="delete created jobs at the end")
    args = parser.parse_args()

    status = get_system_status(args.gateway)
    if status:
        print(
            "System capacity before test: "
            f"free={status.get('free_capacity')} total={status.get('total_capacity')} "
            f"active={status.get('active_jobs')} workers={status.get('workers_online')}/{status.get('workers_total')}"
        )
        free_capacity = int(status.get("free_capacity") or 0)
        if args.count > free_capacity:
            print(
                f"WARNING: requested {args.count} jobs, but gateway reports only "
                f"{free_capacity} free capacity. Some POST /api/jobs calls may fail."
            )

    started = time.time()
    results: list[dict] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(create_job, args.gateway) for _ in range(args.count)]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                print(f"created {result['job_id']} worker={result['worker_id']} port={result['port']}")
            except Exception as exc:
                errors.append(str(exc))
                print(f"ERROR: {exc}")

    distribution = Counter(item["worker_id"] for item in results)
    print("\nCreated jobs:", len(results))
    print("Errors:", len(errors))
    print("Elapsed seconds:", round(time.time() - started, 2))
    print("\nDistribution:")
    for worker, count in sorted(distribution.items()):
        print(f"  {worker}: {count}")

    if results:
        first = results[0]
        print("\nTCP smoke test for first job:")
        try:
            with socket.create_connection(("localhost", int(first["port"])), timeout=10) as sock:
                sock.recv(4096)
                sock.sendall(b"ping\n")
                print(sock.recv(4096).decode(errors="replace").strip())
        except Exception as exc:
            print("TCP test failed:", exc)

    if args.delete:
        print("\nDeleting created jobs...")
        for item in results:
            try:
                requests.delete(f"{args.gateway.rstrip('/')}/api/jobs/{item['job_id']}", timeout=20).raise_for_status()
                print("deleted", item["job_id"])
            except Exception as exc:
                print("delete failed", item["job_id"], exc)


if __name__ == "__main__":
    main()
