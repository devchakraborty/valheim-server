import asyncio
import atexit
import logging
import os

from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional
from asyncio.subprocess import Process, STDOUT

from aiohttp import web
from aiopath import AsyncPath as Path
from zipstream import AioZipStream

SERVER_START_SCRIPT = "start_server_bepinex.sh"
logging.basicConfig(level=logging.DEBUG)


class ServerStatus:
    STOPPED = auto()
    RUNNING = auto()


@dataclass
class ValheimServer:
    server_dir: str = "/home/valheim/server"
    worlds_dir: str = "/home/valheim/.config/unity3d/IronGate/Valheim/worlds"
    log_file: str = "valheim.log"

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()
        self.status = ServerStatus.STOPPED
        self.process: Optional[Process] = None
        self.log_file = open(self.log_file, "w")

    async def start(self, request: web.Request) -> web.Response:
        async with self.lock:
            if self.status == ServerStatus.RUNNING:
                raise web.HTTPConflict(reason="Server already running")

            await self.start_server()

            return web.HTTPOk(reason="Started server")

    async def start_server(self) -> None:
        logging.info("Starting server")
        self.process = await asyncio.create_subprocess_shell(
            str(Path(self.server_dir) / Path(SERVER_START_SCRIPT)),
            stdout=self.log_file,
            stderr=self.log_file,
        )
        self.status = ServerStatus.RUNNING

    async def stop(self, request: web.Request) -> web.Response:
        async with self.lock:
            if self.status == ServerStatus.STOPPED:
                raise web.HTTPConflict(reason="Server already stopped")

            await self.stop_server()

            return web.HTTPOk(reason="Server stopped")

    async def stop_server(self) -> None:
        logging.info("Stopping server")
        self.process.terminate()
        await self.process.wait()
        self.status = ServerStatus.STOPPED

    async def backup(self, request: web.Request) -> web.Response:
        async with self.lock:
            was_running = self.status == ServerStatus.RUNNING
            if was_running:
                await self.stop_server()

            # Perform backup - stream worlds folder to client as zip
            response = web.StreamResponse()
            response.content_type = "application/zip"
            await response.prepare(request)

            worlds_path = Path(self.worlds_dir)

            files = []
            async for file_path in worlds_path.glob("**/*"):
                files.append(
                    {
                        "name": str(file_path.relative_to(worlds_path)),
                        "file": str(file_path),
                    }
                )

            zip_stream = AioZipStream(files=files)
            async for chunk in zip_stream.stream():
                await response.write(chunk)

            if was_running:
                await self.start_server()

            return response

    async def update(self, request: web.Request) -> web.Response:
        async with self.lock:
            was_running = self.status == ServerStatus.RUNNING
            if was_running:
                await self.stop_server()
            # Update
            if was_running:
                await self.start_server()

    async def get_worlds(self) -> List[str]:
        worlds_path = Path(self.worlds_dir)
        world_names = set()
        world_file_extensions = {".fwl", ".db"}
        async for file_path in worlds_path.glob("**/*"):
            if file_path.suffix in world_file_extensions:
                world_names.add(file_path.stem)
        return sorted(world_names)

    async def list_worlds(self, request: web.Request) -> web.Response:
        return web.json_response(await self.get_worlds())

    def run_web(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.post("/start", self.start),
                web.post("/stop", self.stop),
                web.get("/backup", self.backup),
                web.post("/update", self.update),
                web.get("/worlds", self.list_worlds),
            ]
        )
        port = os.environ.get("PORT", 8080)
        logging.info(f"Starting web server on port: {port}")
        web.run_app(app, port=port)


if __name__ == "__main__":
    server = ValheimServer()
    server.run_web()
