import asyncio
import atexit
import dataclasses
import json
import logging
import os
import sys
import time
import traceback

from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional, ClassVar
from asyncio.subprocess import Process, STDOUT

from aiohttp import web
from aiopath import AsyncPath as Path
from zipstream import AioZipStream

SERVER_START_SCRIPT = "start_server_bepinex.sh"
DEFAULT_CONFIG_FILE = "config.json"
STARTUP_TIMEOUT_SECS = 120
VALHEIM_TCP_PORTS = (
    list(range(2456, 2457 + 1))
    + list(range(27015, 27030 + 1))
    + list(range(27036, 27037 + 1))
)
logging.basicConfig(level=logging.DEBUG)


class ServerStatus(Enum):
    STOPPED = auto()
    RUNNING = auto()


@dataclass
class ServerConfig:
    FIELD_PREFIX: ClassVar[str] = "server_"

    name: str = "Valheim Server"
    password: str = "secret"
    port: int = 27000
    world: str = "world"
    public: int = 1

    @classmethod
    async def load(cls, filename: str = DEFAULT_CONFIG_FILE) -> "ServerConfig":
        path = Path(filename)
        if await path.exists():
            with path.open("r") as config_file:
                return cls(**json.load(config_file))
        else:
            config = cls()
            logging.warning(f"Creating new config: {config}")
            return config

    async def dump(self, filename: str = DEFAULT_CONFIG_FILE) -> None:
        path = Path(filename)
        async with path.open("w") as config_file:
            await config_file.write(json.dumps(dataclasses.asdict(self)))


@dataclass
class ValheimServer:
    server_dir: str = "/home/valheim/server"
    worlds_dir: str = "/home/valheim/.config/unity3d/IronGate/Valheim/worlds"
    log_file: str = "valheim.log"

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()
        self.status = ServerStatus.STOPPED
        self.config: Optional[ServerConfig] = None
        self.process: Optional[Process] = None
        self.log_file = open(self.log_file, "w")

    async def start(self, request: web.Request) -> web.Response:
        async with self.lock:
            if self.status == ServerStatus.RUNNING:
                raise web.HTTPConflict(text="Server already running")

            try:
                request_body = await request.json()
            except json.JSONDecodeError:
                request_body = {}
            server_config = await ServerConfig.load()
            for field in dataclasses.fields(server_config):
                if field.name in request_body:
                    setattr(server_config, field.name, request_body[field.name])

            await self.configure_server(server_config)
            await self.start_server()

            logging.debug(f"Started server with configuration: {self.config}")

            return web.HTTPOk(text="Started server")

    async def configure_server(self, config: ServerConfig) -> None:
        await config.dump()
        for field in dataclasses.fields(ServerConfig):
            os.environ[f"{ServerConfig.FIELD_PREFIX}{field.name}"] = str(
                getattr(config, field.name)
            )
        self.config = config

    async def start_server(self) -> None:
        logging.info("Starting server")

        self.process = await asyncio.create_subprocess_shell(
            str(Path(self.server_dir) / Path(SERVER_START_SCRIPT)),
            stdout=self.log_file,
            stderr=self.log_file,
        )

        server_listening = False

        timeout_time = time.time() + STARTUP_TIMEOUT_SECS
        while time.time() < timeout_time:
            if await is_udp_port_open(self.config.port):
                server_listening = True
                break
            await asyncio.sleep(1)

        if server_listening:
            logging.info(f"Valheim server ready on port {self.config.port}")
        else:
            await self.stop_server()
            raise RuntimeError(
                f"Server did not start up within {STARTUP_TIMEOUT_SECS} seconds"
            )
        self.status = ServerStatus.RUNNING

    async def stop(self, request: web.Request) -> web.Response:
        async with self.lock:
            if self.status == ServerStatus.STOPPED:
                raise web.HTTPConflict(text="Server already stopped")

            await self.stop_server()

            return web.HTTPOk(text="Server stopped")

    async def stop_server(self) -> None:
        logging.info("Stopping server")
        self.process.terminate()
        await self.process.wait()
        self.status = ServerStatus.STOPPED
        self.config = None

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
        app = web.Application(middlewares=[json_responses])
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


@web.middleware
async def json_responses(request: web.Request, handler) -> web.Response:
    # Convert response body to a JSON payload
    try:
        response = await handler(request)
        if response.content_type not in ("application/json", "text/plain"):
            return response
        response_body = response.body
        if isinstance(response_body, bytes):
            response_body = response_body.decode(response.charset)
        if response.content_type == "application/json":
            response_body = json.loads(response_body)
        response.body = json.dumps(
            {"status": response.status, "result": response_body,}
        ).encode("utf-8")
        return response
    except web.HTTPException as ex:
        ex.body = json.dumps(
            {"status": ex.status_code, "message": ex.body.decode("utf-8"),}
        ).encode("utf-8")
        raise ex
    except Exception as ex:
        server_error = web.HTTPInternalServerError()
        server_error.body = json.dumps(
            {"status": 500, "error": traceback.format_exception(*sys.exc_info())}
        ).encode("utf-8")
        raise server_error


async def is_udp_port_open(port: int) -> bool:
    ss_proc = await asyncio.create_subprocess_shell(
        "ss -lnu", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await ss_proc.communicate()
    return f":{port} " in stdout.decode()


if __name__ == "__main__":
    server = ValheimServer()
    server.run_web()
