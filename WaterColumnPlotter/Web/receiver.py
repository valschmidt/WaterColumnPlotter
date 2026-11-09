"""
UDP receiver: reads Kongsberg datagrams from a socket, reassembles partitioned #MWC datagrams, and puts each
complete #MWC datagram, and any #IIP (installation parameters) or #SVP (sound speed profile), on a queue. Runs in its
own process and does nothing else, so it keeps up with the sonar.
"""

import logging
import queue
import socket
import struct
import time

from KMALL.kmall import kmall

logger = logging.getLogger(__name__)

HEADER_SIZE = 20     # EMdgmHeader
PARTITION_SIZE = 4   # EMdgmMpartition
INCOMPLETE_TIMEOUT_S = 2.0
STATS_INTERVAL_S = 1.0


def open_socket(bind_ip, port, multicast_group, rcvbuf):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Ask for a large receive buffer; the OS may cap it (e.g. kern.ipc.maxsockbuf on macOS).
    while rcvbuf >= 2 ** 16:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            break
        except OSError:
            rcvbuf //= 2
    if multicast_group:
        sock.bind(('', port))
        mreq = struct.pack('4s4s', socket.inet_aton(multicast_group), socket.inet_aton(bind_ip or '0.0.0.0'))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    else:
        sock.bind((bind_ip, port))
    sock.settimeout(STATS_INTERVAL_S)
    logger.info("Listening for UDP on %s:%d%s (receive buffer %d bytes)", bind_ip, port,
                f" multicast {multicast_group}" if multicast_group else "",
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
    return sock


def reassemble(parts, lead_size):
    """
    Join the partitions of one 'M' datagram. Partition 1 is kept whole (without its trailing size); later partitions
    contribute only their data, after the leading fields repeated in every partition.
    """
    dgm = bytearray(parts[0][:-4])
    for p in parts[1:]:
        dgm += p[lead_size:-4]
    dgm += bytes(4)
    size = len(dgm)
    struct.pack_into('I', dgm, 0, size)
    struct.pack_into('2H', dgm, HEADER_SIZE, 1, 1)
    struct.pack_into('I', dgm, size - 4, size)
    return bytes(dgm)


def run(bind_ip, port, multicast_group, out_queue, rcvbuf=8 * 2 ** 20):
    """Process entry point. Puts complete #MWC datagrams (bytes) and ('stats', dict) tuples on out_queue."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    try:
        sock = open_socket(bind_ip, port, multicast_group, rcvbuf)
    except OSError as e:
        # Reported on the page; replay still works, and the address can be changed there.
        where = f"{bind_ip}:{port}" + (f" multicast {multicast_group}" if multicast_group else "")
        logger.error("Can't receive UDP on %s: %s", where, e)
        out_queue.put(('stats', {'error': f"Can't receive UDP on {where}: {e.strerror or e}"}))
        return
    km = kmall()
    pending = {}  # key -> [arrival time, parts list]
    # behind: complete datagrams dropped because the worker's queue was full (processing can't keep up).
    stats = {'packets': 0, 'datagrams': 0, 'discarded': 0, 'behind': 0}

    def forward(item):
        # The queue is bounded, so a worker that falls behind doesn't fill memory with datagrams waiting to be
        # processed: new ones are dropped (and counted) until it catches up.
        try:
            out_queue.put_nowait(item)
        except queue.Full:
            if not isinstance(item, tuple):
                stats['behind'] += 1
    next_stats = time.monotonic() + STATS_INTERVAL_S

    while True:
        try:
            data = sock.recv(2 ** 16)
        except socket.timeout:
            data = None
        now = time.monotonic()

        if data and data[4:8] in (b'#IIP', b'#SVP'):  # Installation parameters (transducer depth); sound speed profile.
            forward(data)
        elif data and data[4:8] == b'#MWC':
            stats['packets'] += 1
            km.OpenBytestoRead(data)
            header = km.read_EMdgmHeader()
            partition = km.read_EMdgmMpartition()
            if partition['numOfDgms'] == 1:
                forward(data)
                stats['datagrams'] += 1
            else:
                # Partitions of one datagram share the header's system, sounder and time fields.
                key = (header['systemID'], header['echoSounderID'], data[12:20])
                entry = pending.setdefault(key, [now, [None] * partition['numOfDgms']])
                entry[1][partition['dgmNum'] - 1] = data
                if all(entry[1]):
                    del pending[key]
                    lead_size = HEADER_SIZE + PARTITION_SIZE
                    if header['dgmVersion'] >= 2:  # REV I+: cmnPart is repeated in every partition.
                        lead_size += km.read_EMdgmMbody()['numBytesCmnPart']
                    forward(reassemble(entry[1], lead_size))
                    stats['datagrams'] += 1

        # Drop datagrams whose partitions did not all arrive.
        for key in [k for k, (t, _) in pending.items() if now - t > INCOMPLETE_TIMEOUT_S]:
            del pending[key]
            stats['discarded'] += 1

        if now >= next_stats:
            forward(('stats', dict(stats)))
            next_stats = now + STATS_INTERVAL_S
