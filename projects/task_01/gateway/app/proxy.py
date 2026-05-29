import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProxyInstance:
    job_id: str
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int
    server: asyncio.base_events.Server


class TcpProxyManager:
    """Динамический TCP proxy: gateway:port -> internal_host:internal_port."""

    def __init__(self) -> None:
        self._proxies: dict[str, ProxyInstance] = {}
        self._lock = asyncio.Lock()

    async def start_proxy(
        self,
        job_id: str,
        listen_host: str,
        listen_port: int,
        target_host: str,
        target_port: int,
    ) -> None:
        async with self._lock:
            if job_id in self._proxies:
                raise RuntimeError(f"proxy for job {job_id} already exists")

            async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                peer = writer.get_extra_info("peername")
                logger.info(
                    "proxy connection accepted job=%s peer=%s target=%s:%s",
                    job_id,
                    peer,
                    target_host,
                    target_port,
                )
                try:
                    target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
                except Exception as exc:
                    logger.exception("failed to connect to target for job=%s: %s", job_id, exc)
                    writer.write(f"Target is unavailable: {exc}\n".encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

                async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                    try:
                        while True:
                            data = await src.read(65536)
                            if not data:
                                break
                            dst.write(data)
                            await dst.drain()
                    except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
                        pass
                    finally:
                        try:
                            dst.close()
                        except Exception:
                            pass

                t1 = asyncio.create_task(pipe(reader, target_writer))
                t2 = asyncio.create_task(pipe(target_reader, writer))
                done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    try:
                        task.result()
                    except Exception:
                        pass

            server = await asyncio.start_server(handler, listen_host, listen_port)
            self._proxies[job_id] = ProxyInstance(
                job_id=job_id,
                listen_host=listen_host,
                listen_port=listen_port,
                target_host=target_host,
                target_port=target_port,
                server=server,
            )
            logger.info(
                "started proxy job=%s %s:%s -> %s:%s",
                job_id,
                listen_host,
                listen_port,
                target_host,
                target_port,
            )

    async def stop_proxy(self, job_id: str) -> None:
        async with self._lock:
            proxy = self._proxies.pop(job_id, None)
        if not proxy:
            return
        proxy.server.close()
        await proxy.server.wait_closed()
        logger.info("stopped proxy job=%s port=%s", job_id, proxy.listen_port)

    async def stop_all(self) -> int:
        """Stop every TCP proxy, including proxies that no longer have a registry job.

        This is used by DELETE /api/jobs and gateway shutdown. It protects the
        system from stale asyncio servers left in memory when the registry has
        already been cleaned or restored from persistent state.
        """
        async with self._lock:
            proxies = list(self._proxies.values())
            self._proxies.clear()
        for proxy in proxies:
            proxy.server.close()
        for proxy in proxies:
            await proxy.server.wait_closed()
            logger.info("stopped proxy job=%s port=%s", proxy.job_id, proxy.listen_port)
        return len(proxies)

    async def stop_except(self, keep_job_ids: set[str]) -> int:
        """Stop proxies whose job_id is not present in keep_job_ids."""
        async with self._lock:
            stale = [proxy for job_id, proxy in self._proxies.items() if job_id not in keep_job_ids]
            for proxy in stale:
                self._proxies.pop(proxy.job_id, None)
        for proxy in stale:
            proxy.server.close()
        for proxy in stale:
            await proxy.server.wait_closed()
            logger.warning("stopped stale proxy job=%s port=%s", proxy.job_id, proxy.listen_port)
        return len(stale)

    def list_ports(self) -> list[int]:
        return [p.listen_port for p in self._proxies.values()]

    def list_job_ids(self) -> set[str]:
        return set(self._proxies.keys())

    def has_proxy(self, job_id: str) -> bool:
        return job_id in self._proxies
