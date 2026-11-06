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
import multiprocessing
import queue
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
    'maxHeave_m': 2.5,
    'maxGridCells': 500,
    'acrossTrackAvg_m': 10.0,
    'sliceDepth_m': 10.0,
    'sliceThickness_m': 2.0,
    'alongTrackAvg_ping': 5,
    'historyColumns': 1000,
}

# name: (type, minimum, maximum)
SETTING_LIMITS = {
    'binSize_m': (float, 0.01, 10.0),
    'maxHeave_m': (float, 0.0, 50.0),
    'maxGridCells': (int, 50, 2000),
    'acrossTrackAvg_m': (float, 0.01, 10000.0),
    'sliceDepth_m': (float, 0.0, 12000.0),
    'sliceThickness_m': (float, 0.01, 1000.0),
    'alongTrackAvg_ping': (int, 1, 100),
    'historyColumns': (int, 10, 20000),
}


def validate_settings(current, requested):
    """Returns a copy of current updated with requested values, each converted and clipped to its limits."""
    new = dict(current)
    for name, value in requested.items():
        if name in SETTING_LIMITS:
            kind, lo, hi = SETTING_LIMITS[name]
            new[name] = min(hi, max(lo, kind(value)))
    return new


def frame(header, *arrays):
    """Binary WebSocket frame: uint32 header length, JSON header, padding to 4 bytes, then float32 arrays."""
    head = json.dumps(header).encode()
    head += b' ' * (-(4 + len(head)) % 4)
    return struct.pack('<I', len(head)) + head + b''.join(np.ascontiguousarray(a, '<f4').tobytes() for a in arrays)


class Server:
    def __init__(self, args):
        self.args = args
        self.settings = dict(DEFAULT_SETTINGS)
        self.history = collections.deque(maxlen=self.settings['historyColumns'])  # (time, vertical, horizontal)
        self.pie = None
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
        # Grid rows / columns averaged for the slices, so pages can draw them exactly as the worker uses them.
        bands = {'vertical': gridding.vertical_band(self.settings),
                 'horizontal': gridding.horizontal_band(self.settings),
                 'heaveRows': gridding.heave_rows(self.settings['binSize_m'], self.settings['maxHeave_m'])}
        return json.dumps({'type': 'settings', 'settings': self.settings, 'grid': bands})

    def columns_frame(self, columns):
        n = self.settings['maxGridCells']
        times = [c[0] for c in columns]
        vertical = np.array([c[1] for c in columns], dtype=np.float32).reshape(len(columns), n)
        horizontal = np.array([c[2] for c in columns], dtype=np.float32).reshape(len(columns), n)
        return frame({'type': 'columns', 'count': len(columns), 'n': n, 'times': times}, vertical, horizontal)

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
            try:
                msg = await asyncio.to_thread(self.result_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if msg['type'] == 'reset':
                self.clear()
                await self.broadcast(json.dumps({'type': 'reset'}))
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
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.handle_client_message(json.loads(msg.data))
        finally:
            self.clients.discard(ws)
        return ws

    async def handle_client_message(self, msg):
        if msg.get('type') != 'settings':
            return
        new = validate_settings(self.settings, msg.get('settings', {}))
        if new == self.settings:
            await self.broadcast(self.settings_message())
            return
        grid_changed = any(new[k] != self.settings[k] for k in worker.GRID_SETTINGS)
        self.settings = new
        self.history = collections.deque(self.history, maxlen=new['historyColumns'])
        self.settings_queue.put(new)
        await self.broadcast(self.settings_message())
        if grid_changed:
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

    async def on_cleanup(self, app):
        app['relay'].cancel()
        for ws in list(self.clients):
            await ws.close()
        for p in self.processes:
            p.terminate()
            p.join(timeout=2)

    def make_app(self):
        app = web.Application()
        app.router.add_get('/', self.index)
        app.router.add_get('/ws', self.websocket)
        app.router.add_static('/static/', STATIC_DIR)
        app.on_startup.append(self.on_startup)
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
