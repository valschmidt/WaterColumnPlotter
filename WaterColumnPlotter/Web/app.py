"""
Browser-based Water Column Plotter: starts the UDP receiver and worker processes, serves the web page, and relays
display data to connected browsers over a WebSocket.

Usage: water_column_plotter_web [--udp-port 6020] [--multicast 225.255.255.255] [--http-port 8000] [--no-browser]
"""

import argparse
import asyncio
import collections
import json
import logging
import math
import multiprocessing
import os
import queue
import signal
import struct
import webbrowser
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType, web

from WaterColumnPlotter.Web import gridding, receiver, worker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'

DEFAULT_SETTINGS = {
    'binSize_m': 0.2,
    'acrossTrackAvg_m': 10.0,
    'sliceDepth_m': 10.0,
    'sliceThickness_m': 2.0,
    'alongTrackAvg_ping': 5,
    'historyPings': 2500,
    'referenceToSurface': False,
    'transducerDepth_m': None,  # below the waterline; set from #IIP when one is received (None: not yet known)
    'heaveAllowance_m': 1.0,    # room shown above 0 m when depths are measured from the transducer
    'sidelobeSuppression': 'off',
    'layerMinLevel_dB': 1.0,    # layer tracking: minimum level above the profile's background
}

# name: (type, minimum, maximum)
SETTING_LIMITS = {
    'binSize_m': (float, 0.01, 10.0),
    'acrossTrackAvg_m': (float, 0.01, 10000.0),
    'sliceDepth_m': (float, 0.0, 12000.0),
    'sliceThickness_m': (float, 0.01, 1000.0),
    'alongTrackAvg_ping': (int, 1, 100),
    'historyPings': (int, 10, 100000),
    'referenceToSurface': (bool, False, True),
    'transducerDepth_m': (float, -100.0, 100.0),
    'heaveAllowance_m': (float, 0.0, 50.0),
    'sidelobeSuppression': ('choice', gridding.SIDELOBE_SUPPRESSION),
    'layerMinLevel_dB': (float, 0.1, 30.0),
}


def validate_settings(current, requested):
    """Returns a copy of current updated with requested values, each converted and clipped to its limits."""
    new = dict(current)
    for name, value in requested.items():
        if name not in SETTING_LIMITS:
            continue
        if SETTING_LIMITS[name][0] == 'choice':
            if value in SETTING_LIMITS[name][1]:
                new[name] = value
        else:
            kind, lo, hi = SETTING_LIMITS[name]
            new[name] = min(hi, max(lo, kind(value)))
    return new


def history_columns(settings):
    """Number of slice columns kept: each column averages alongTrackAvg_ping pings."""
    return math.ceil(settings['historyPings'] / settings['alongTrackAvg_ping'])


def frame(header, *arrays):
    """Binary WebSocket frame: uint32 header length, JSON header, padding to 4 bytes, then float32 arrays."""
    head = json.dumps(header).encode()
    head += b' ' * (-(4 + len(head)) % 4)
    return struct.pack('<I', len(head)) + head + b''.join(np.ascontiguousarray(a, '<f4').tobytes() for a in arrays)


class Server:
    def __init__(self, args):
        self.args = args
        self.settings = dict(DEFAULT_SETTINGS)
        self.history = collections.deque(maxlen=history_columns(self.settings))  # (time, vertical, horizontal)
        self.pie = None
        self.svp = None  # latest sound speed profile message
        self.stats = {}
        self.clients = set()

        ctx = multiprocessing.get_context('spawn')
        self.dg_queue = ctx.Queue()
        self.settings_queue = ctx.Queue()
        self.result_queue = ctx.Queue()
        self.processes = [
            ctx.Process(target=receiver.run, name='receiver', daemon=True,
                        args=(args.udp_ip, args.udp_port, args.multicast, self.dg_queue)),
            ctx.Process(target=worker.run, name='worker', daemon=True,
                        args=(self.settings, self.dg_queue, self.settings_queue, self.result_queue,
                              args.update_interval)),
        ]

    # ----- Messages to browsers -----

    def settings_message(self):
        # Grid columns / rows averaged for the slices, so pages can draw them exactly as the worker uses them.
        bands = {'vertical': gridding.vertical_band(self.settings),
                 'horizontal': gridding.horizontal_band(self.settings)}
        return json.dumps({'type': 'settings', 'settings': self.settings, 'grid': bands})

    def columns_frame(self, columns):
        # Each column covers only its data: vertical values start at grid row v[0], horizontal at grid column h[0].
        header = {'type': 'columns', 'count': len(columns), 'times': [c[0] for c in columns],
                  'v': [[c[1][0], len(c[1][1])] for c in columns],
                  'h': [[c[2][0], len(c[2][1])] for c in columns],
                  'layers': [c[3] for c in columns]}
        return frame(header, *[c[1][1] for c in columns], *[c[2][1] for c in columns])

    def pie_frame(self):
        data, row0, col0 = self.pie
        return frame({'type': 'pie', 'rows': data.shape[0], 'cols': data.shape[1], 'row0': row0, 'col0': col0},
                     data)

    async def broadcast(self, message):
        for ws in list(self.clients):
            try:
                if isinstance(message, str):
                    await ws.send_str(message)
                else:
                    await ws.send_bytes(message)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    def clear(self):
        self.history.clear()
        self.pie = None

    # ----- Worker results -----

    async def relay_results(self):
        while True:
            dead = [p.name for p in self.processes if not p.is_alive()]
            if dead:
                # Without the receiver or worker no data can arrive; stop rather than serve a page that never updates.
                logger.error("The %s process exited (see its error above); shutting down.", ' and '.join(dead))
                os.kill(os.getpid(), signal.SIGINT)
                return
            try:
                msg = await asyncio.to_thread(self.result_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if msg['type'] == 'reset':
                self.clear()
                await self.broadcast(json.dumps({'type': 'reset'}))
            elif msg['type'] == 'svp':
                self.svp = json.dumps(msg)
                await self.broadcast(self.svp)
            elif msg['type'] == 'installation':
                # Transducer depth from an #IIP datagram; only a change is applied.
                if msg['transducerDepth_m'] != self.settings['transducerDepth_m']:
                    logger.info("Transducer depth from installation datagram: %.3f m", msg['transducerDepth_m'])
                    await self.change_settings({'transducerDepth_m': msg['transducerDepth_m']})
            elif msg['type'] == 'update':
                self.stats = msg['stats']
                if msg['columns']:
                    self.history.extend(msg['columns'])
                    await self.broadcast(self.columns_frame(msg['columns']))
                if msg['pie'] is not None:
                    self.pie = msg['pie']
                    await self.broadcast(self.pie_frame())
                await self.broadcast(json.dumps({'type': 'stats', 'stats': self.stats}))

    # ----- HTTP / WebSocket handlers -----

    async def index(self, request):
        return web.FileResponse(STATIC_DIR / 'index.html')

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(self.settings_message())
        if self.history:
            await ws.send_bytes(self.columns_frame(list(self.history)))
        if self.pie is not None:
            await ws.send_bytes(self.pie_frame())
        if self.svp is not None:
            await ws.send_str(self.svp)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.handle_client_message(json.loads(msg.data))
        finally:
            self.clients.discard(ws)
        return ws

    async def handle_client_message(self, msg):
        if msg.get('type') == 'settings':
            await self.change_settings(msg.get('settings', {}))

    async def change_settings(self, requested):
        new = validate_settings(self.settings, requested)
        if new == self.settings:
            await self.broadcast(self.settings_message())
            return
        reset = any(new[k] != self.settings[k] for k in worker.RESET_SETTINGS)
        self.settings = new
        self.history = collections.deque(self.history, maxlen=history_columns(new))
        self.settings_queue.put(new)
        await self.broadcast(self.settings_message())
        if reset:
            self.clear()
            await self.broadcast(json.dumps({'type': 'reset'}))

    # ----- Lifecycle -----

    async def on_startup(self, app):
        for p in self.processes:
            p.start()
        app['relay'] = asyncio.create_task(self.relay_results())
        url = f'http://{"localhost" if self.args.http_host in ("127.0.0.1", "0.0.0.0") else self.args.http_host}' \
              f':{self.args.http_port}/'
        logger.info("Water Column Plotter at %s", url)
        if not self.args.no_browser:
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, url)

    async def on_shutdown(self, app):
        # Close browser connections first; otherwise the server waits for them before shutting down.
        app['relay'].cancel()
        for ws in list(self.clients):
            await ws.close(code=1001, message=b'Server shutting down')

    async def on_cleanup(self, app):
        for p in self.processes:
            p.terminate()
            p.join(timeout=2)

    def make_app(self):
        app = web.Application()
        app.router.add_get('/', self.index)
        app.router.add_get('/ws', self.websocket)
        app.router.add_static('/static/', STATIC_DIR)
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)
        app.on_cleanup.append(self.on_cleanup)
        return app


def main():
    parser = argparse.ArgumentParser(description="Real-time water column display in a web browser.")
    parser.add_argument('--udp-ip', default='0.0.0.0',
                        help="Address to receive datagrams on (multicast: local interface address).")
    parser.add_argument('--udp-port', type=int, default=6020, help="UDP port to receive datagrams on.")
    parser.add_argument('--multicast', default=None, metavar='GROUP', help="Multicast group to join, if any.")
    parser.add_argument('--http-host', default='127.0.0.1',
                        help="Address to serve the page on; 0.0.0.0 allows viewing from other machines.")
    parser.add_argument('--http-port', type=int, default=8000, help="Port to serve the page on.")
    parser.add_argument('--update-interval', type=float, default=1.0, help="Seconds between display updates.")
    parser.add_argument('--no-browser', action='store_true', help="Don't open a browser window.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    server = Server(args)
    web.run_app(server.make_app(), host=args.http_host, port=args.http_port, print=None)


if __name__ == '__main__':
    main()
