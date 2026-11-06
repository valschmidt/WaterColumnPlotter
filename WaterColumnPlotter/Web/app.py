"""
Browser-based Water Column Plotter: starts the UDP receiver and worker processes, serves the web page, and relays
display data to connected browsers over a WebSocket.

Usage: water_column_plotter_web [--udp-port 6020] [--multicast 225.255.255.255] [--http-port 8000] [--no-browser]
                               [--data-dir DIR] [--replay FILE]
"""

import argparse
import asyncio
import collections
import ipaddress
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

from WaterColumnPlotter.Web import gridding, receiver, replay, worker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'

DEFAULT_SETTINGS = {
    'binSize_m': 0.2,
    'acrossTrackAvg_m': 10.0,
    'sliceDepth_m': 10.0,
    'sliceThickness_m': 2.0,
    'alongTrackAvg_swath': 10,
    'historySwaths': 5000,
    'referenceToSurface': False,
    'transducerDepth_m': None,  # below the waterline; set from #IIP when one is received (None: not yet known)
    'heaveAllowance_m': 1.0,    # room shown above 0 m when depths are measured from the transducer
    'sidelobeSuppression': 'off',
    'backgroundSwaths': 10,      # 'ping_mean' sidelobe suppression: earlier swaths of the same fan averaged into each beam's background
    'layerMinLevel_dB': 1.0,    # layer tracking: minimum level above the profile's background
    # Thermocline tracking (after Cordero Ros, 2018)
    'thermoclineThreshold_dBpm': 0.2,  # minimum high-passed level gradient for a pick
    'thermoclineWindow_s': 60.0,       # along-track smoothing window
    'thermoclineSearch_m': 6.0,        # picks are searched for within this of the cast's thermocline bottom
    'castAlertError_m': 0.5,           # outer-beam depth error beyond which a new cast is advised
    'castAlertAngle_deg': 65.0,        # beam angle for the depth error
}

# name: (type, minimum, maximum)
SETTING_LIMITS = {
    'binSize_m': (float, 0.01, 10.0),
    'acrossTrackAvg_m': (float, 0.01, 10000.0),
    'sliceDepth_m': (float, 0.0, 12000.0),
    'sliceThickness_m': (float, 0.01, 1000.0),
    'alongTrackAvg_swath': (int, 1, 100),
    'historySwaths': (int, 10, 100000),
    'referenceToSurface': (bool, False, True),
    'transducerDepth_m': (float, -100.0, 100.0),
    'heaveAllowance_m': (float, 0.0, 50.0),
    'sidelobeSuppression': ('choice', gridding.SIDELOBE_SUPPRESSION),
    'backgroundSwaths': (int, 1, 100),
    'layerMinLevel_dB': (float, 0.1, 30.0),
    'thermoclineThreshold_dBpm': (float, 0.01, 50.0),
    'thermoclineWindow_s': (float, 0.0, 3600.0),
    'thermoclineSearch_m': (float, 0.5, 1000.0),
    'castAlertError_m': (float, 0.01, 100.0),
    'castAlertAngle_deg': (float, 0.0, 80.0),
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
    """Number of slice columns kept: each column averages alongTrackAvg_swath swaths."""
    return math.ceil(settings['historySwaths'] / settings['alongTrackAvg_swath'])


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
        # Data source: real-time UDP (the receiver's address) or replay of a recorded file (dataDir: the directory of
        # the last file opened, where browsing starts). The mode and the file being replayed are the worker's ('source'
        # messages from it); the rest is kept here.
        self.source = {'mode': 'realtime'}
        self.source_config = {'udpIp': args.udp_ip, 'udpPort': args.udp_port, 'multicast': args.multicast or '',
                              'dataDir': str(Path(args.data_dir).resolve())}
        self.stats = {}
        self.clients = set()

        self.ctx = ctx = multiprocessing.get_context('spawn')
        self.dg_queue = ctx.Queue()
        self.settings_queue = ctx.Queue()
        self.result_queue = ctx.Queue()
        self.control_queue = ctx.Queue()  # source commands for the worker
        if args.replay:
            self.control_queue.put({'cmd': 'open', 'path': str(Path(args.replay).resolve())})
        self.receiver = self.new_receiver()
        self.worker = ctx.Process(target=worker.run, name='worker', daemon=True,
                                  args=(self.settings, self.dg_queue, self.settings_queue, self.control_queue,
                                        self.result_queue, args.update_interval))

    def new_receiver(self):
        c = self.source_config
        return self.ctx.Process(target=receiver.run, name='receiver', daemon=True,
                                args=(c['udpIp'], c['udpPort'], c['multicast'] or None, self.dg_queue))

    # ----- Messages to browsers -----

    def source_message(self):
        return json.dumps({**self.source, 'type': 'source', 'config': self.source_config})

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
                  'hz': [[c[5][0], len(c[5][1])] for c in columns],
                  'layers': [c[3] for c in columns], 'thermocline': [c[4] for c in columns]}
        return frame(header, *[c[1][1] for c in columns], *[c[2][1] for c in columns],
                     *[c[5][1] for c in columns])

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
            if not self.worker.is_alive():
                # Without the worker nothing is processed; stop rather than serve a page that never updates. (If the
                # receiver can't open its socket it reports why and exits; the page shows that.)
                logger.error("The worker process exited (see its error above); shutting down.")
                os.kill(os.getpid(), signal.SIGINT)
                return
            try:
                msg = await asyncio.to_thread(self.result_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if msg['type'] == 'reset':
                self.clear()
                if msg.get('svp'):  # source changed: the cast came from the other source
                    self.svp = None
                await self.broadcast(json.dumps(msg))
            elif msg['type'] == 'source':
                if 'error' in msg:
                    await self.broadcast(json.dumps(msg))
                else:
                    self.source = msg
                    await self.broadcast(self.source_message())
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

    async def browse(self, request):
        """
        Browse the plotter computer's directories for recorded files: ?dir=PATH (default: the replay directory).
        Returns the directory, its parent and its subdirectories and recorded files.
        """
        try:
            path = Path(request.query.get('dir') or self.source_config['dataDir']).expanduser().resolve()
            dirs, files = await asyncio.to_thread(replay.list_directory, path)
        except OSError as e:
            return web.json_response({'error': f"Can't open {request.query.get('dir')}: {e.strerror or e}"})
        return web.json_response({'dir': str(path), 'parent': str(path.parent) if path.parent != path else None,
                                  'dirs': dirs, 'files': files})

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
        await ws.send_str(self.source_message())
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
        elif msg.get('type') == 'source':
            await self.source_command(msg)

    async def source_error(self, error):
        await self.broadcast(json.dumps({'type': 'source', 'error': error}))

    async def source_command(self, msg):
        """
        {'cmd': 'realtime' | 'replay'}: switch source. {'cmd': 'config', 'config': {...}}: change the UDP address
        (restarting the receiver). Replay commands ({'cmd': 'open', 'path'}, 'play', 'pause', 'speed', 'seek') go to
        the worker.
        """
        cmd = msg.get('cmd')
        if cmd == 'config':
            await self.change_source_config(msg.get('config', {}))
        elif cmd == 'open':
            # A recorded file anywhere on the plotter's computer; its directory becomes the replay directory.
            path = Path(str(msg.get('path', ''))).expanduser().resolve()
            if not (path.suffix.lower() in replay.EXTENSIONS and path.is_file()):
                await self.source_error(f"Not a recorded .kmall / .kmwcd file: {msg.get('path')}")
                return
            self.source_config['dataDir'] = str(path.parent)
            self.control_queue.put({'cmd': 'open', 'path': str(path)})
        elif cmd in ('realtime', 'replay', 'play', 'pause', 'speed', 'seek'):
            self.control_queue.put({k: msg[k] for k in ('cmd', 'speed', 'time') if k in msg})

    async def change_source_config(self, requested):
        new = dict(self.source_config)
        try:
            if 'udpIp' in requested:
                new['udpIp'] = str(ipaddress.IPv4Address(str(requested['udpIp']).strip()))
            if 'udpPort' in requested:
                new['udpPort'] = int(requested['udpPort'])
                if not 1 <= new['udpPort'] <= 65535:
                    raise ValueError("The UDP port must be 1-65535")
            if 'multicast' in requested:
                group = str(requested['multicast'] or '').strip()
                if group and not ipaddress.IPv4Address(group).is_multicast:
                    raise ValueError(f"{group} is not a multicast address (224.0.0.0-239.255.255.255)")
                new['multicast'] = group
        except ValueError as e:
            await self.source_error(str(e))
            return
        udp_changed = any(new[k] != self.source_config[k] for k in ('udpIp', 'udpPort', 'multicast'))
        self.source_config = new
        if udp_changed:
            logger.info("Restarting the receiver on %s:%d%s", new['udpIp'], new['udpPort'],
                        f" multicast {new['multicast']}" if new['multicast'] else "")
            old = self.receiver
            old.terminate()
            await asyncio.to_thread(old.join, 2)
            self.dg_queue.put(('stats', {}))  # forget the old receiver's counts (and any error)
            self.receiver = self.new_receiver()
            self.receiver.start()
        await self.broadcast(self.source_message())

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
        self.receiver.start()
        self.worker.start()
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
        for p in (self.receiver, self.worker):
            p.terminate()
            p.join(timeout=2)

    @staticmethod
    @web.middleware
    async def revalidate(request, handler):
        """Have browsers check the page and its files for changes on every load, so an update is never stale."""
        response = await handler(request)
        if request.path == '/' or request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

    def make_app(self):
        app = web.Application(middlewares=[self.revalidate])
        app.router.add_get('/', self.index)
        app.router.add_get('/ws', self.websocket)
        app.router.add_get('/browse', self.browse)
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
    parser.add_argument('--data-dir', default=None,
                        help="Directory where browsing for recorded .kmall/.kmwcd files to replay starts (default: "
                             "the --replay file's directory, or the current directory).")
    parser.add_argument('--replay', default=None, metavar='FILE', help="Replay this recorded file at start-up.")
    args = parser.parse_args()
    if args.data_dir is None:
        args.data_dir = str(Path(args.replay).resolve().parent) if args.replay else os.getcwd()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    server = Server(args)
    web.run_app(server.make_app(), host=args.http_host, port=args.http_port, print=None)


if __name__ == '__main__':
    main()
