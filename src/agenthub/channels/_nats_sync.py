"""Synchronous wrapper over nats-py.

nats-py is asyncio-only (no official sync client exists). This module runs
the entire client on a dedicated background event-loop thread and exposes
blocking, thread-safe calls: the same pub/sub semantics as ``nats.connect``
without requiring the caller to have an event loop.

Not affiliated with nats-py; requires ``nats-py>=2.6.0`` to be installed.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

__all__ = ["SyncNATS", "SyncMsg"]

ConnectCallback = Callable[[], None]
MessageCallback = Callable[["SyncMsg"], None]


class SyncMsg:
    """A received message. ``data`` is raw bytes, ``subject`` the topic."""

    __slots__ = ("subject", "data", "reply", "_ack")

    def __init__(self, subject: str, data: bytes, reply: str = "") -> None:
        self.subject = subject
        self.data = data
        self.reply = reply

    def decode(self) -> str:
        return self.data.decode("utf-8", errors="replace")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SyncMsg subject={self.subject!r} {len(self.data)} bytes>"


class SyncNATS:
    """Blocking NATS client. Create one per process (or per connection).

    Example:
        nc = SyncNATS("nats://localhost:4222")
        nc.subscribe("bots.*.in", callback=lambda m: print(m.decode()))
        nc.publish("bots.x.out", b"hello")
        nc.close()
    """

    def __init__(
        self,
        servers: str = "nats://localhost:4222",
        *,
        username: str = "",
        password: str = "",
        connect_timeout: float = 10.0,
        tls: Any | None = None,
        on_connect: ConnectCallback | None = None,
        on_disconnect: ConnectCallback | None = None,
    ) -> None:
        self._servers = servers
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._tls = tls
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._ready = threading.Event()
        self._connect_error: BaseException | None = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._thread_main, name="sync-nats", daemon=True
        )
        self._thread.start()
        # Wait for connect to finish (success or failure) before returning.
        self._ready.wait(timeout=self._connect_timeout + 5)
        if self._connect_error is not None:
            self._shutdown_loop()
            raise self._connect_error

    # ------------------------------------------------------------------
    # Background loop thread
    # ------------------------------------------------------------------

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())
        self._loop.run_forever()
        # Loop stopped: drain remaining callbacks then close.
        pending = asyncio.all_tasks(self._loop)
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    async def _connect(self) -> None:
        import nats

        servers = [s.strip() for s in self._servers.split(",") if s.strip()]

        async def _disc() -> None:
            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception:
                    pass

        async def _reconn() -> None:
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    pass

        try:
            self._nc = await nats.connect(
                servers=servers,
                user=self._username or None,
                password=self._password or None,
                connect_timeout=int(self._connect_timeout),
                tls=self._tls,
                reconnect_time_wait=1,
                max_reconnect_attempts=-1,
                disconnected_cb=_disc,
                reconnected_cb=_reconn,
            )
        except BaseException as exc:  # propagate to the constructing thread
            self._connect_error = exc
        finally:
            self._ready.set()

    def _shutdown_loop(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Public blocking API
    # ------------------------------------------------------------------

    def publish(self, subject: str, data: bytes | str) -> None:
        """Publish and flush before returning (send-and-forget semantics)."""
        if isinstance(data, str):
            data = data.encode("utf-8")

        done = threading.Event()
        err: list[BaseException] = []

        async def _pub() -> None:
            try:
                await self._nc.publish(subject, data)
                await self._nc.flush()
            except BaseException as exc:
                err.append(exc)
            finally:
                done.set()

        self._loop.call_soon_threadsafe(self._loop.create_task, _pub())
        if not done.wait(timeout=30):
            raise TimeoutError(f"publish timed out to {subject}")
        if err:
            raise err[0]

    def subscribe(self, subject: str, callback: MessageCallback) -> str:
        """Subscribe with a sync callback invoked on the NATS thread."""
        done = threading.Event()
        err: list[BaseException] = []
        sid_box: list[str] = []

        async def _sub() -> None:
            try:
                sub = await self._nc.subscribe(subject, cb=self._make_cb(callback))
                sid_box.append(str(sub._id))
            except BaseException as exc:
                err.append(exc)
            finally:
                done.set()

        self._loop.call_soon_threadsafe(self._loop.create_task, _sub())
        if not done.wait(timeout=10):
            raise TimeoutError(f"subscribe timed out for {subject}")
        if err:
            raise err[0]
        return sid_box[0]

    def unsubscribe(self, sid: str) -> None:
        done = threading.Event()

        async def _unsub() -> None:
            try:
                sub = self._nc._subs.get(int(sid))
                if sub is not None:
                    await sub.unsubscribe()
            finally:
                done.set()

        self._loop.call_soon_threadsafe(self._loop.create_task, _unsub())
        done.wait(timeout=10)

    def request(self, subject: str, data: bytes | str, timeout: float = 5.0) -> SyncMsg:
        """Blocking request/reply using nats-py's inbox machinery."""
        if isinstance(data, str):
            data = data.encode("utf-8")

        fut: asyncio.Future = self._loop.create_future()

        async def _req() -> None:
            try:
                msg = await self._nc.request(subject, data, timeout=timeout)
                if not fut.done():
                    fut.set_result(msg)
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(exc)

        self._loop.call_soon_threadsafe(self._loop.create_task, _req())
        msg = fut.result(timeout=timeout + 5)  # may raise TimeoutError / Error
        return SyncMsg(msg.subject, msg.data, msg.reply or "")

    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    def close(self) -> None:
        """Flush pending publishes and stop the background thread."""
        if getattr(self, "_nc", None) is not None:
            nc, self._nc = self._nc, None
            fut = asyncio.run_coroutine_threadsafe(nc.drain(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        self._shutdown_loop()

    # ------------------------------------------------------------------

    def _make_cb(self, user_cb: MessageCallback) -> Callable:
        async def _cb(msg: Any) -> None:
            sync_msg = SyncMsg(msg.subject, msg.data, msg.reply or "")
            # Run the user callback off the loop thread (it may block).
            await asyncio.get_running_loop().run_in_executor(None, user_cb, sync_msg)

        return _cb

    def __enter__(self) -> "SyncNATS":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
