from __future__ import annotations

import warnings

from typing import TYPE_CHECKING, Optional

from sanic.compat import Header
from sanic.exceptions import BadRequest, ServerError
from sanic.helpers import Default
from sanic.http import Stage
from sanic.log import error_logger, logger
from sanic.models.asgi import ASGIReceive, ASGIScope, ASGISend, MockTransport
from sanic.request import Request
from sanic.response import BaseHTTPResponse
from sanic.server import ConnInfo
from sanic.server.websockets.connection import WebSocketConnection


if TYPE_CHECKING:
    from sanic import Sanic


class Lifespan:
    def __init__(
        self, sanic_app, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        self.sanic_app = sanic_app
        self.scope = scope
        self.receive = receive
        self.send = send

        if "server.init.before" in self.sanic_app.signal_router.name_index:
            logger.debug(
                'You have set a listener for "before_server_start" '
                "in ASGI mode. "
                "It will be executed as early as possible, but not before "
                "the ASGI server is started.",
                extra={"verbosity": 1},
            )
        if "server.shutdown.after" in self.sanic_app.signal_router.name_index:
            logger.debug(
                'You have set a listener for "after_server_stop" '
                "in ASGI mode. "
                "It will be executed as late as possible, but not after "
                "the ASGI server is stopped.",
                extra={"verbosity": 1},
            )

    async def startup(self) -> None:
        """
        Gather the listeners to fire on server start.
        Because we are using a third-party server and not Sanic server, we do
        not have access to fire anything BEFORE the server starts.
        Therefore, we fire before_server_start and after_server_start
        in sequence since the ASGI lifespan protocol only supports a single
        startup event.
        """
        await self.sanic_app._startup()
        await self.sanic_app._server_event("init", "before")
        await self.sanic_app._server_event("init", "after")

        if not isinstance(self.sanic_app.config.USE_UVLOOP, Default):
            warnings.warn(
                "You have set the USE_UVLOOP configuration option, but Sanic "
                "cannot control the event loop when running in ASGI mode."
                "This option will be ignored."
            )

    async def shutdown(self) -> None:
        """
        Gather the listeners to fire on server stop.
        Because we are using a third-party server and not Sanic server, we do
        not have access to fire anything AFTER the server stops.
        Therefore, we fire before_server_stop and after_server_stop
        in sequence since the ASGI lifespan protocol only supports a single
        shutdown event.
        """
        await self.sanic_app._server_event("shutdown", "before")
        await self.sanic_app._server_event("shutdown", "after")

    async def __call__(self) -> None:
        while True:
            message = await self.receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.startup()
                except Exception as e:
                    error_logger.exception(e)
                    await self.send(
                        {"type": "lifespan.startup.failed", "message": str(e)}
                    )
                else:
                    await self.send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await self.shutdown()
                except Exception as e:
                    error_logger.exception(e)
                    await self.send(
                        {"type": "lifespan.shutdown.failed", "message": str(e)}
                    )
                else:
                    await self.send({"type": "lifespan.shutdown.complete"})
                return


class ASGIApp:
    sanic_app: Sanic
    request: Request
    transport: MockTransport
    lifespan: Lifespan
    ws: Optional[WebSocketConnection]
    stage: Stage
    response: Optional[BaseHTTPResponse]

    @classmethod
    async def create(
        cls,
        sanic_app: Sanic,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> ASGIApp:
        instance = cls()
        instance.ws = None
        instance.sanic_app = sanic_app
        instance.transport = MockTransport(scope, receive, send)
        instance.transport.loop = sanic_app.loop
        instance.stage = Stage.IDLE
        instance.response = None
        instance.sanic_app.state.is_started = True
        setattr(instance.transport, "add_task", sanic_app.loop.create_task)

        try:
            headers = Header(
                [
                    (
                        key.decode("ASCII"),
                        value.decode(errors="surrogateescape"),
                    )
                    for key, value in scope.get("headers", [])
                ]
            )
        except UnicodeDecodeError:
            raise BadRequest(
                "Header names can only contain US-ASCII characters"
            )

        if scope["type"] == "http":
            version = scope["http_version"]
            method = scope["method"]
        elif scope["type"] == "websocket":
            version = "1.1"
            method = "GET"

            instance.ws = instance.transport.create_websocket_connection(
                send, receive
            )
        else:
            raise ServerError("Received unknown ASGI scope")

        url_bytes, query = scope["raw_path"], scope["query_string"]
        if query:
            # httpx ASGI client sends query string as part of raw_path
            url_bytes = url_bytes.split(b"?", 1)[0]
            # All servers send them separately
            url_bytes = b"%b?%b" % (url_bytes, query)

        request_class = sanic_app.request_class or Request  # type: ignore
        instance.request = request_class(
            url_bytes,
            headers,
            version,
            method,
            instance.transport,
            sanic_app,
        )
        request_class._current.set(instance.request)
        instance.request.stream = instance  # type: ignore
        instance.request_body = True
        instance.request.conn_info = ConnInfo(instance.transport)

        await instance.sanic_app.dispatch(
            "http.lifecycle.request",
            inline=True,
            context={"request": instance.request},
            fail_not_found=False,
        )

        return instance

    async def read(self) -> Optional[bytes]:
        """
        Read and stream the body in chunks from an incoming ASGI message.
        """
        if self.stage is Stage.IDLE:
            self.stage = Stage.REQUEST
        message = await self.transport.receive()
        body = message.get("body", b"")
        if not message.get("more_body", False):
            self.request_body = False
            if not body:
                return None
        return body

    async def __aiter__(self):
        while self.request_body:
            data = await self.read()
            if data:
                yield data

    def respond(self, response: BaseHTTPResponse):
        if self.stage is not Stage.HANDLER:
            self.stage = Stage.FAILED
            raise RuntimeError("Response already started")
        if self.response is not None:
            self.response.stream = None
        response.stream, self.response = self, response
        return response

    async def send(self, data, end_stream):
        self.stage = Stage.IDLE if end_stream else Stage.RESPONSE
        if self.response:
            response, self.response = self.response, None
            await self.transport.send(
                {
                    "type": "http.response.start",
                    "status": response.status,
                    "headers": response.processed_headers,
                }
            )
            response_body = getattr(response, "body", None)
            if response_body:
                data = response_body + data if data else response_body
        await self.transport.send(
            {
                "type": "http.response.body",
                "body": data.encode() if hasattr(data, "encode") else data,
                "more_body": not end_stream,
            }
        )

    _asgi_single_callable = True  # We conform to ASGI 3.0 single-callable

    async def __call__(self) -> None:
        """
        Handle the incoming request.
        """
        try:
            self.stage = Stage.HANDLER
            await self.sanic_app.handle_request(self.request)
        except Exception as e:
            try:
                await self.sanic_app.handle_exception(self.request, e)
            except Exception as exc:
                await self.sanic_app.handle_exception(self.request, exc, False)
