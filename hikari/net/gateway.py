#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Nekokatt 2019-2020
#
# This file is part of Hikari.
#
# Hikari is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Hikari is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Hikari. If not, see <https://www.gnu.org/licenses/>.
"""
Single-threaded asyncio V7 Gateway implementation. Handles regular heartbeating in a background task
on the same event loop. Implements zlib transport compression only.

Can be used as the main gateway connection for a single-sharded bot, or the gateway connection for a specific shard
in a swarm of shards making up a larger bot.

References:
    - IANA WS closure code standards: https://www.iana.org/assignments/websocket/websocket.xhtml
    - Gateway documentation: https://discordapp.com/developers/docs/topics/gateway
    - Opcode documentation: https://discordapp.com/developers/docs/topics/opcodes-and-status-codes
"""
import asyncio
import contextlib
import datetime
import json
import logging
import math
import platform
import time
import zlib

import aiohttp

from . import errors
from . import ratelimits


class GatewayClient:
    def __init__(
        self,
        *,
        compression=True,
        dispatch=lambda gw, e, p: None,
        guild_subscriptions=True,
        initial_presence=None,
        json_deserialize=json.loads,
        json_serialize=json.dumps,
        large_threshold=1_000,
        receive_timeout=10.0,
        session_id=None,
        seq=None,
        shard_id=0,
        shard_count=1,
        token,
        url,
    ):
        self.closed_event = asyncio.Event()
        self.compression = compression
        self.connected_at = float("nan")
        self.dispatch = dispatch
        self.guild_subscriptions = guild_subscriptions
        self.large_threshold = large_threshold
        self.json_deserialize = json_deserialize
        self.json_serialize = json_serialize
        self.last_heartbeat_sent = float("nan")
        self.last_heartbeat_ack_received = float("nan")
        self.last_ping_sent = float("nan")
        self.last_pong_received = float("nan")
        self.presence = initial_presence
        self.ratelimiter = ratelimits.GatewayRateLimiter(60.0, 120)
        self.receive_timeout = receive_timeout
        self.session = None
        self.session_id = session_id
        self.seq = seq
        self.shard_id = shard_id if shard_id is not None and shard_count is not None else 0
        self.shard_count = shard_count if shard_id is not None and shard_count is not None else 1
        self.token = token
        self.ws = None
        self.zlib = zlib.decompressobj()

        name = f"hikari.{type(self).__name__}"
        if shard_count > 1:
            name += f"{shard_id}"
        self.logger = logging.getLogger(name)

        url = f"{url}?v=7&encoding=json"
        if compression:
            url += "&compress=zlib-stream"
        self.url = url

    @property
    def latency(self):
        return self.last_pong_received - self.last_ping_sent

    @property
    def heartbeat_latency(self):
        return self.last_heartbeat_ack_received - self.last_heartbeat_sent

    @property
    def uptime(self):
        delta = time.perf_counter() - self.connected_at
        return datetime.timedelta(seconds=0 if math.isnan(delta) else delta)

    @property
    def is_connected(self):
        return not math.isnan(self.connected_at)

    async def connect(self):
        try:
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(
                self.url, receive_timeout=self.receive_timeout, compress=0, autoping=False, max_msg_size=0,
            )

            self.connected_at = time.perf_counter()

            ping_task = asyncio.create_task(self.ping_keep_alive())

            # Parse HELLO
            self.logger.debug("expecting HELLO")
            pl = await self.recv()
            op = pl["op"]
            if op != 10:
                raise errors.GatewayError(f"Expected HELLO opcode 10 but received {op}")
            hb_interval = pl["d"]["heartbeat_interval"] / 1_000.0
            self.logger.info("received HELLO, interval is %ss", hb_interval)

            heartbeat_task = asyncio.create_task(self.heartbeat_keep_alive(hb_interval))

            if self.session_id is None:
                await self.identify()
                self.logger.info("sent IDENTIFY, ready to listen to incoming events")
            else:
                await self.resume()
                self.logger.info("sent RESUME, ready to listen to incoming events")

            await asyncio.gather(
                ping_task, heartbeat_task, self.poll_events(),
            )
        finally:
            self.connected_at = float("nan")
            self.last_ping_sent = float("nan")
            self.last_pong_received = float("nan")
            self.last_heartbeat_sent = float("nan")
            self.last_heartbeat_ack_received = float("nan")

    def identify(self):
        self.logger.debug("sending IDENTIFY")
        pl = {
            "op": 2,
            "d": {
                "token": self.token,
                "compress": False,
                "large_threshold": self.large_threshold,
                "properties": {
                    "$os": " ".join((platform.system(), platform.release(),)),
                    "$browser": "hikari/1.0.0a1",
                    "$device": " ".join(
                        (platform.python_implementation(), platform.python_revision(), platform.python_version(),)
                    ),
                },
                "guild_subscriptions": self.guild_subscriptions,
                "shard": [self.shard_id, self.shard_count],
                # "intents": ...
            },
        }

        if self.presence:
            pl["d"]["presence"] = self.presence.to_dict()
        return self.send(pl)

    def resume(self):
        self.logger.debug("sending RESUME")
        pl = {
            "op": 6,
            "d": {"token": self.token, "seq": self.seq, "session_id": self.session_id,},
        }
        return self.send(pl)

    async def ping_keep_alive(self):
        while not self.closed_event.is_set():
            await self.ws.ping()
            self.last_ping_sent = time.perf_counter()
            self.logger.debug("sent ping")
            try:
                await asyncio.wait_for(self.closed_event.wait(), timeout=0.75 * self.receive_timeout)
            except asyncio.TimeoutError:
                pass

    async def heartbeat_keep_alive(self, heartbeat_interval):
        while not self.closed_event.is_set():
            if self.last_heartbeat_ack_received < self.last_heartbeat_sent:
                raise errors.GatewayZombiedError()
            await self.send({"op": 1, "d": self.seq})
            self.last_heartbeat_sent = time.perf_counter()
            try:
                await asyncio.wait_for(self.closed_event.wait(), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    async def poll_events(self):
        while True:
            next = await self.recv()

            op = next["op"]
            d = next["d"]

            if op == 0:
                self.seq = next["s"]
                event_name = next["t"]
                self.dispatch(self, event_name, d)
            elif op == 1:
                await self.send({"op": 11})
            elif op == 7:
                self.logger.debug("instructed by gateway server to restart connection")
                raise errors.GatewayMustReconnectError()
            elif op == 9:
                resumable = bool(d)
                self.logger.debug(
                    "instructed by gateway server to %s session", "resume" if resumable else "restart",
                )
                raise errors.GatewayInvalidSessionError(resumable)
            elif op == 11:
                self.last_heartbeat_ack_received = time.perf_counter()
                ack_wait = self.last_heartbeat_ack_received - self.last_heartbeat_sent
                self.logger.debug("received HEARTBEAT ACK in %ss", ack_wait)
            else:
                self.logger.debug("ignoring opcode %s with data %r", op, d)

    async def close(self):
        with contextlib.suppress(AttributeError):
            await asyncio.shield(self.ws.close())
        with contextlib.suppress(AttributeError):
            await asyncio.shield(self.session.close())
        if not self.closed_event.is_set():
            self.closed_event.set()

    async def recv(self):
        while True:
            message = await self.ws.receive()

            if message.type == aiohttp.WSMsgType.TEXT:
                self.logger.debug("recv payload %r", message.data)
                return self.json_deserialize(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                buffer = bytearray(message.data)
                packets = 1
                while not buffer.endswith(b"\x00\x00\xff\xff"):
                    packets += 1
                    message = await self.ws.receive()
                    if message.type != aiohttp.WSMsgType.BINARY:
                        raise errors.GatewayError(f"Expected a binary message but got {message.type}")
                    buffer.extend(message.data)

                pl = self.zlib.decompress(buffer)
                self.logger.debug("recv %s zlib-encoded packets containing payload %r", packets, pl)
                return self.json_deserialize(pl)
            elif message.type == aiohttp.WSMsgType.PING:
                self.logger.debug("recv ping")
                await self.ws.pong()
                self.logger.debug("sent pong")
            elif message.type == aiohttp.WSMsgType.PONG:
                self.last_pong_received = time.perf_counter()
                self.logger.debug("recv pong after %ss", self.last_pong_received - self.last_ping_sent)
            elif message.type == aiohttp.WSMsgType.CLOSE:
                close_code = self.ws.close_code
                self.logger.debug("connection closed with code %s", close_code)
                if close_code == errors.GatewayCloseCode.AUTHENTICATION_FAILED:
                    raise errors.GatewayInvalidTokenError()
                elif close_code in (errors.GatewayCloseCode.SESSION_TIMEOUT, errors.GatewayCloseCode.INVALID_SEQ):
                    raise errors.GatewayInvalidSessionError(False)
                elif close_code == errors.GatewayCloseCode.SHARDING_REQUIRED:
                    raise errors.GatewayNeedsShardingError()
                else:
                    raise errors.GatewayConnectionClosedError(close_code)
            elif message.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                self.logger.debug("connection has already closed, so giving up")
                raise errors.GatewayClientClosedError()
            elif message.type == aiohttp.WSMsgType.ERROR:
                ex = self.ws.exception()
                self.logger.debug("connection encountered some error", exc_info=ex)
                raise errors.GatewayError("Unexpected exception occurred") from ex

    async def send(self, payload):
        payload_str = self.json_serialize(payload)

        if len(payload_str) > 4096:
            raise errors.GatewayError(
                f"Tried to send a payload greater than 4096 bytes in size (was actually {len(payload_str)}"
            )

        await self.ratelimiter.acquire()
        await self.ws.send_str(payload_str)

        self.logger.debug("sent payload %r", payload)

    async def request_guild_members(
        self, guild_id, *guild_ids, **kwargs,
    ):
        guilds = [guild_id, *guild_ids]
        constraints = {}

        if "user_ids" in kwargs:
            constraints["user_ids"] = kwargs["user_ids"]
        else:
            constraints["query"] = kwargs.get("query", "")
            constraints["limit"] = kwargs.get("limit", 0)

        self.logger.debug(
            "requesting guild members for guilds %s with constraints %s", guilds, constraints,
        )

        await self.send({"guild_id": guilds, **constraints})

    #: TODO, reimplement this.
    async def update_status(self, presence) -> None:
        self.logger.debug("updating presence to %r", presence)
        await self.send(
            {
                "idle": presence.idle_since,
                "status": presence.status,
                "game": (presence.activity.to_dict() if presence.activity is not None else None),
                "afk": presence.is_afk,
            }
        )
        self.presence = presence

    def __str__(self):
        state = "Connected" if self.is_connected else "Disconnected"
        return f"{state} gateway connection to {self.url} at shard {self.shard_id}/{self.shard_count}"

    def __repr__(self):
        this_type = type(self).__name__
        major_attributes = ", ".join(
            (
                f"is_connected={self.is_connected!r}",
                f"latency={self.latency!r}",
                f"heartbeat_latency={self.heartbeat_latency!r}",
                f"presence={self.presence!r}",
                f"shard_id={self.shard_id!r}",
                f"shard_count={self.shard_count!r}",
                f"seq={self.seq!r}",
                f"session_id={self.session_id!r}",
                f"uptime={self.uptime!r}",
                f"url={self.url!r}",
            )
        )

        return f"{this_type}({major_attributes})"

    def __bool__(self):
        return self.is_connected
