"""
Browser-based Water Column Plotter: starts the UDP receiver and worker processes, serves the web page, and relays
display data to connected browsers over a WebSocket.

Usage: wcp [--udp-port 6020] [--multicast 225.255.255.255] [--http-port 8000] [--no-browser]
       [--data-dir DIR] [--replay FILE]
"""

import argparse
import asyncio
import collections
import ipaddress
import json
import logging
import multiprocessing
import os
import queue
import signal
import struct
import time
import webbrowser
from pathlib import Path

import numpy as np
from aiohttp import WSCloseCode, WSMsgType, web

from WaterColumnPlotter.Web import filedialog, gridding, receiver, replay, worker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'
# Memory protection
FRAME_MAX_BYTES = 16 * 2 ** 20  # largest frame of swaths sent to a page catching up with the history
DG_QUEUE_MAX = 100              # complete datagrams waiting for the worker; beyond this the receiver drops them
# A page is disconnected if it stays behind: more than CLIENT_MAX_PENDING bytes waiting to be sent to it, with data
# waiting for more than CLIENT_MAX_LAG_S; or, whatever the time, more than CLIENT_HARD_PENDING bytes.
CLIENT_MAX_PENDING = 64 * 2 ** 20
CLIENT_MAX_LAG_S = 10.0
CLIENT_HARD_PENDING = 512 * 2 ** 20

DEFAULT_SETTINGS = {
    'binSize_m': 0.2,
    'acrossTrackAvg_m': 10.0,    # tracking column: width of the band under the vessel
    'alongTrackAvg_swath': 10,
    'historySwaths': 5000,
    'historyMemory_MB': 500,     # the most memory the swath history may use, in the plotter and in each page
    'referenceToSurface': False,
    'transducerDepth_m': None,  # below the waterline; set from #IIP when one is received (None: not yet known)
    'heaveAllowance_m': 1.0,    # room shown above 0 m when depths are measured from the transducer
    'sidelobeSuppression': 'off',
    'dualSwath': 'both',         # dual-swath systems: 'both' swaths of each ping cycle, or only the 'first' or 'second'
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
    'alongTrackAvg_swath': (int, 1, 100),
    'historySwaths': (int, 10, 100000),
    'historyMemory_MB': (int, 50, 16000),
    'referenceToSurface': (bool, False, True),
    'transducerDepth_m': (float, -100.0, 100.0),
    'heaveAllowance_m': (float, 0.0, 50.0),
    'sidelobeSuppression': ('choice', gridding.SIDELOBE_SUPPRESSION),
    'dualSwath': ('choice', tuple(worker.DUAL_SWATH)),
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


def swaths_frame(swaths):
    """
    Binary WebSocket frame of encoded swaths (worker.encode_swath): uint32 header length, JSON header
    {'type': 'swaths', 'swaths': [meta, ...]}, then each swath's rows x cols bytes in turn.
    """
    head = json.dumps({'type': 'swaths', 'swaths': [meta for meta, _ in swaths]}).encode()
    return struct.pack('<I', len(head)) + head + b''.join(data for _, data in swaths)


class Client:
    """
    One connected page, with a queue of messages for it sent by its own task (run). If the page stays behind (see
    CLIENT_MAX_PENDING), it is disconnected (it reconnects and catches up from the history), rather than letting
    its queue fill memory or holding up the other pages. One large update alone doesn't count as falling behind.
    """

    def __init__(self, ws, remote):
        self.ws = ws
        self.remote = remote
        self.queue = collections.deque()  # str or bytes messages, or ('history', swaths) to send in frames
        self.pending = 0  # bytes of the messages queued (the history isn't counted: it is held anyway)
        self.waiting_since = None  # when data began waiting to be sent (None: everything sent)
        self.wake = asyncio.Event()
        self.dropped = False

    def send(self, message):
        if self.dropped:
            return
        now = time.monotonic()
        if self.waiting_since is None:
            self.waiting_since = now
        self.queue.append(message)
        self.pending += len(message)
        self.wake.set()
        lagging = now - self.waiting_since > CLIENT_MAX_LAG_S
        if self.pending > CLIENT_HARD_PENDING or (self.pending > CLIENT_MAX_PENDING and lagging):
            self.dropped = True
            logger.warning("Disconnecting %s: it isn't keeping up (%.0f MB waiting to be sent)", self.remote,
                           self.pending / 1e6)
            asyncio.get_running_loop().create_task(self.ws.close(
                code=WSCloseCode.TRY_AGAIN_LATER, message=b"This connection can't keep up with the data"))

    def send_history(self, swaths):
        self.queue.append(('history', swaths))
        self.wake.set()

    async def run(self):
        try:
            while not self.dropped:
                await self.wake.wait()
                self.wake.clear()
                while self.queue and not self.dropped:
                    message = self.queue.popleft()
                    if isinstance(message, tuple):
                        await self.send_frames(message[1])
                        continue
                    self.pending -= len(message)
                    if isinstance(message, str):
                        await self.ws.send_str(message)
                    else:
                        await self.ws.send_bytes(message)
                if not self.queue:
                    self.waiting_since = None
        except (ConnectionError, RuntimeError):
            pass  # the page went away

    async def send_frames(self, swaths):
        frame, size = [], 0
        for swath in swaths:
            if frame and size + len(swath[1]) > FRAME_MAX_BYTES:
                await self.ws.send_bytes(swaths_frame(frame))
                frame, size = [], 0
            frame.append(swath)
            size += len(swath[1])
        if frame:
            await self.ws.send_bytes(swaths_frame(frame))


class Server:
    def __init__(self, args):
        self.args = args
        self.settings = dict(DEFAULT_SETTINGS)
        # The most recent swaths, (meta, bytes) as the worker encoded them, and tracking results, for pages that
        # connect later: each page keeps the same history and builds its plots from it.
        # Kept to the swath history and its memory budget (trim_history).
        self.swaths = collections.deque()
        self.swath_bytes = 0
        self.tracks = collections.deque()
        self.svp = None  # latest sound speed profile message
        # Data source: real-time UDP (the receiver's address) or replay of a recorded file (dataDir: the directory of
        # the last file opened, where browsing starts). The mode and the file being replayed are the worker's ('source'
        # messages from it); the rest is kept here.
        self.source = {'mode': 'realtime'}
        self.opening = None  # name of the file the worker is opening (indexing), until it reports the result
        self.source_config = {'udpIp': args.udp_ip, 'udpPort': args.udp_port, 'multicast': args.multicast or '',
                              'dataDir': str(Path(args.data_dir).resolve())}
        self.stats = {}
        self.clients = set()
        self.dialog_open = False  # the native Open dialog is showing

        self.ctx = ctx = multiprocessing.get_context('spawn')
        self.dg_queue = ctx.Queue(maxsize=DG_QUEUE_MAX)
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
        return json.dumps({**self.source, 'type': 'source', 'config': self.source_config, 'opening': self.opening})

    def settings_message(self):
        return json.dumps({'type': 'settings', 'settings': self.settings})

    async def broadcast(self, message):
        # Queued for each page and sent by its own task, so a slow page doesn't hold up the others.
        for client in list(self.clients):
            client.send(message)

    def clear(self):
        self.swaths.clear()
        self.swath_bytes = 0
        self.tracks.clear()

    def trim_history(self):
        """Keep the newest swaths within both the swath history and its memory budget; tracking goes with them."""
        s = self.settings
        budget = s['historyMemory_MB'] * 1e6
        while self.swaths and (len(self.swaths) > s['historySwaths'] or self.swath_bytes > budget):
            self.swath_bytes -= len(self.swaths.popleft()[1])
        oldest = self.swaths[0][0]['seq'] if self.swaths else None
        while self.tracks and (oldest is None or self.tracks[0]['seq1'] < oldest):
            self.tracks.popleft()

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
            elif msg['type'] == 'opening':
                self.opening = msg['name']
                await self.broadcast(self.source_message())
            elif msg['type'] == 'source':
                if 'error' in msg:
                    await self.broadcast(json.dumps(msg))
                    if self.opening is not None:  # the file couldn't be opened
                        self.opening = None
                        await self.broadcast(self.source_message())
                else:
                    self.source, self.opening = msg, None
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
                if msg['swaths']:
                    self.swaths.extend(msg['swaths'])
                    self.swath_bytes += sum(len(data) for _, data in msg['swaths'])
                    self.trim_history()
                    await self.broadcast(swaths_frame(msg['swaths']))
                if msg['tracks']:
                    self.tracks.extend(msg['tracks'])
                    await self.broadcast(json.dumps({'type': 'tracks', 'tracks': msg['tracks']}))
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

    async def choose_file(self, request):
        """
        Show the operating system's Open dialog on the plotter's computer, for a browser on that computer, and replay
        the file chosen. Returns {'path'}, {'cancelled': true}, or {'unavailable': reason} when a browser elsewhere
        asked (the dialog would appear on a screen its viewer can't see) or no dialog can be shown; the page then
        offers its own file browser instead.
        """
        try:
            local = ipaddress.ip_address(request.remote or '').is_loopback
        except ValueError:
            local = False
        if not local:
            return web.json_response({'unavailable': "the plotter is on another computer"})
        if self.dialog_open:
            return web.json_response({'busy': True})
        self.dialog_open = True
        try:
            path = await filedialog.choose_recording(self.source_config['dataDir'])
        except filedialog.Unavailable as e:
            logger.warning("No native file dialog: %s", e)
            return web.json_response({'unavailable': str(e)})
        finally:
            self.dialog_open = False
        if path is None:
            return web.json_response({'cancelled': True})
        await self.source_command({'cmd': 'open', 'path': path})
        return web.json_response({'path': path})

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        # Everything for this page goes through its queue, in order: the settings, the history held now (sent in
        # frames built as they go, so it isn't copied all at once), then what arrives meanwhile.
        client = Client(ws, request.remote)
        client.send(self.settings_message())
        client.send_history(list(self.swaths))
        if self.tracks:
            client.send(json.dumps({'type': 'tracks', 'tracks': list(self.tracks)}))
        if self.svp is not None:
            client.send(self.svp)
        client.send(self.source_message())
        self.clients.add(client)
        sender = asyncio.create_task(client.run())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.handle_client_message(json.loads(msg.data))
        finally:
            self.clients.discard(client)
            sender.cancel()
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
            try:
                self.dg_queue.put_nowait(('stats', {}))  # forget the old receiver's counts (and any error)
            except queue.Full:
                pass
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
        self.trim_history()
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
        for client in list(self.clients):
            await client.ws.close(code=WSCloseCode.GOING_AWAY, message=b'Server shutting down')

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
        app.router.add_post('/choose-file', self.choose_file)
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
