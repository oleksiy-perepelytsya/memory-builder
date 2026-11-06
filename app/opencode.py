"""Thin async client + process manager for a private OpenCode server.

The app owns its own `opencode serve` process so it does not interfere with the
user's shared background service. The server prints its Basic-auth password on
startup, which we capture and use for every request.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import socket
from typing import Any, AsyncIterator, Optional

import httpx

from . import config

log = logging.getLogger("memory_builder.opencode")

_PASSWORD_RE = re.compile(r"server password\s+(\S+)")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def format_model_ref(model: dict | None) -> Optional[dict]:
    """Turn a stored model selection into the OpenCode `Model.Ref` shape."""
    if not model:
        return None
    ref: dict[str, Any] = {"id": model["id"], "providerID": model["providerID"]}
    if model.get("variant"):
        ref["variant"] = model["variant"]
    return ref


class OpenCodeServer:
    """Manages the lifecycle of a private `opencode serve` child process."""

    def __init__(self, host: str = config.OPENCODE_HOST, port: int = config.OPENCODE_PORT):
        self.host = host
        self.port = port
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._password: Optional[str] = None
        self._password_ready = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None
        self._log_path = config.STATE_DIR / "opencode-serve.log"

    # -- properties ---------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def password(self) -> Optional[str]:
        return self._password

    def auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{config.OPENCODE_USER}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if shutil.which(config.OPENCODE_BIN) is None:
            raise RuntimeError(f"`{config.OPENCODE_BIN}` not found on PATH")

        # Pick a free port when the configured one is already taken so we never
        # collide with a running service.
        if self._port_in_use(self.port):
            log.info("port %s in use, selecting a free port", self.port)
            self.port = _free_port()

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(self._log_path, "wb")
        self._proc = await asyncio.create_subprocess_exec(
            config.OPENCODE_BIN, "serve",
            "--hostname", self.host,
            "--port", str(self.port),
            stdout=log_file,
            stderr=log_file,
            cwd=str(config.BASE_DIR),
        )
        self._reader_task = asyncio.create_task(self._watch_password(log_file))
        try:
            await asyncio.wait_for(self._password_ready.wait(), timeout=30)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("timed out waiting for opencode server password") from exc
        await self._wait_healthy()
        log.info("opencode server ready at %s", self.base_url)

    @staticmethod
    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    async def _watch_password(self, log_file) -> None:
        """Tail the server log for the password line."""
        while not self._password_ready.is_set():
            try:
                text = self._log_path.read_text(errors="ignore")
            except OSError:
                text = ""
            match = _PASSWORD_RE.search(text)
            if match:
                self._password = match.group(1)
                self._password_ready.set()
                return
            await asyncio.sleep(0.2)

    async def _wait_healthy(self, timeout: float = 30.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(f"{self.base_url}/api/info", headers=self.auth_header())
                    if resp.status_code == 200:
                        return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.4)
        raise RuntimeError("opencode server did not become healthy")

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._proc.kill()


class OpenCodeClient:
    """HTTP client for the OpenCode v2 API."""

    def __init__(self, server: OpenCodeServer):
        self.server = server
        self._client = httpx.AsyncClient(
            base_url=server.base_url,
            timeout=httpx.Timeout(connect=10, read=None, write=60, pool=10),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return self.server.auth_header()

    async def info(self) -> dict:
        r = await self._client.get("/api/info", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def list_models(self, directory: str) -> list[dict]:
        r = await self._client.get(
            "/api/model", params={"location[directory]": directory}, headers=self._headers()
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_agents(self, directory: str) -> list[dict]:
        r = await self._client.get(
            "/api/agent", params={"location[directory]": directory}, headers=self._headers()
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_mcp(self, directory: str) -> list[dict]:
        r = await self._client.get(
            "/api/mcp", params={"location[directory]": directory}, headers=self._headers()
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def reload_location(self, directory: str) -> None:
        try:
            r = await self._client.post(
                "/api/location/reload",
                json={"directory": directory},
                headers=self._headers(),
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("location reload failed: %s", exc)

    async def create_session(
        self, directory: str, agent: str | None, model: dict | None, title: str
    ) -> dict:
        body: dict[str, Any] = {
            "title": title,
            "location": {"directory": directory},
        }
        if agent:
            body["agent"] = agent
        ref = format_model_ref(model)
        if ref:
            body["model"] = ref
        r = await self._client.post("/api/session", json=body, headers=self._headers())
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", payload)

    async def prompt(self, session_id: str, text: str) -> dict:
        r = await self._client.post(
            f"/api/session/{session_id}/prompt",
            json={"text": text},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def messages(self, session_id: str, order: str = "asc") -> list[dict]:
        r = await self._client.get(
            f"/api/session/{session_id}/message",
            params={"order": order},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def abort(self, session_id: str) -> None:
        try:
            await self._client.post(
                f"/api/session/{session_id}/interrupt", headers=self._headers()
            )
        except httpx.HTTPError:
            pass

    async def events(self) -> AsyncIterator[dict]:
        """Yield decoded events from the server-sent event stream."""
        headers = {**self._headers(), "Accept": "text/event-stream"}
        async with self._client.stream("GET", "/api/event", headers=headers) as resp:
            resp.raise_for_status()
            data_lines: list[str] = []
            async for raw in resp.aiter_lines():
                if raw.startswith("data:"):
                    data_lines.append(raw[5:].strip())
                elif raw == "":
                    if data_lines:
                        chunk = "\n".join(data_lines)
                        data_lines = []
                        try:
                            yield json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
