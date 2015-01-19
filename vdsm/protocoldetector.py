#
# Copyright 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import logging
import socket

from vdsm.utils import traceback
import vdsm.infra.filecontrol as filecontrol
from yajsonrpc.betterAsyncore import (
    Dispatcher,
    Reactor,
)

from vdsm.utils import monotonic_time
from vdsm.sslutils import SSLHandshakeDispatcher


def _create_socket(host, port):
    addr = socket.getaddrinfo(host, port, socket.AF_INET,
                              socket.SOCK_STREAM)
    if not addr:
        raise socket.error("Could not translate address '%s:%s'"
                           % (host, str(port)))

    server_socket = socket.socket(addr[0][0], addr[0][1], addr[0][2])
    filecontrol.set_close_on_exec(server_socket.fileno())
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(addr[0][4])

    return server_socket


def _is_handshaking(sock):
    if not hasattr(sock, "is_handshaking"):
        return False

    return sock.is_handshaking


class _AcceptorImpl(object):
    log = logging.getLogger("ProtocolDetector.AcceptorImpl")

    def __init__(self, dispatcher_factory):
        self._dispatcher_factory = dispatcher_factory

    def readable(self, dispatcher):
        return True

    def handle_accept(self, dispatcher):
        try:
            client, _ = dispatcher.socket.accept()
        except socket.error:
            pass
        else:
            client.setblocking(0)
            self.log.info("Accepting connection from %s:%d",
                          *client.getpeername())
            self._dispatcher_factory(client)


class _ProtocolDetector(object):
    log = logging.getLogger("ProtocolDetector.Detector")

    def __init__(self, detectors, timeout=None):
        self._detectors = detectors
        self._required_size = max(h.REQUIRED_SIZE for h in self._detectors)
        self.log.debug("Using required_size=%d", self._required_size)
        self._give_up_at = monotonic_time() + timeout

    def readable(self, dispatcher):
        return True

    def next_check_interval(self):
        return min(self._give_up_at - monotonic_time(), 0)

    def handle_read(self, dispatcher):
        sock = dispatcher.socket
        try:
            data = sock.recv(self._required_size, socket.MSG_PEEK)
        except socket.error, why:
            if why.args[0] == socket.EWOULDBLOCK:
                return
            dispatcher.handle_error()
            return

        if len(data) < self._required_size:
            return

        if monotonic_time() > self._give_up_at:
            self.log.debug("Timed out while waiting for data")
            dispatcher.close()

        for detector in self._detectors:
            if detector.detect(data):
                host, port = sock.getpeername()
                self.log.info(
                    "Detected protocol %s from %s:%d",
                    detector.NAME,
                    host,
                    port
                )
                detector.handle_dispatcher(dispatcher, (host, port))
                break
        else:
            self.log.warning("Unrecognized protocol: %r", data)
            dispatcher.close()


class MultiProtocolAcceptor:
    """
    Provides multiple protocol support on a single port.

    MultiProtocolAcceptor binds and listen on a single port. It accepts
    incoming connections and handles handshake if required. Next it peeks
    into the first bytes sent to detect the protocol, and pass the connection
    to the server handling this protocol.

    To support a new protocol, register a detector object using
    add_detector. Protocol detectors must implement this interface:

    class ProtocolDetector(object):
        NAME = "protocol name"

        # How many bytes are needed to detect this protocol
        REQUIRED_SIZE = 6

        def detect(self, data):
            Given first bytes read from the connection, try to detect the
            protocol. Returns True if protocol is detected.

        def handle_dispatcher(self, client_dispatcher, socket_address):
            Called after detect() succeeded. The detector owns the socket and
            is responsible for closing it or changing the implementation.
    """
    log = logging.getLogger("vds.MultiProtocolAcceptor")

    def __init__(
        self,
        host,
        port,
        sslctx=None,
        ssl_hanshake_timeout=SSLHandshakeDispatcher.SSL_HANDSHAKE_TIMEOUT,
    ):
        self._sslctx = sslctx
        self._reactor = Reactor()
        sock = _create_socket(host, port)
        self._host, self._port = sock.getsockname()
        self.log.info("Listening at %s:%d", self._host, self._port)
        self._acceptor = Dispatcher(
            _AcceptorImpl(
                self.handle_accept
            ),
            sock,
        )
        self._acceptor.listen(5)
        self._reactor.add_dispatcher(self._acceptor)
        self._handlers = []
        self.TIMEOUT = ssl_hanshake_timeout

    def handle_accept(self, client):
        if self._sslctx is None:
            self._reactor.add_dispatcher(
                self._register_protocol_detector(
                    Dispatcher(
                        sock=client,
                    ),
                ),
            )
        else:
            self._reactor.add_dispatcher(
                Dispatcher(
                    SSLHandshakeDispatcher(
                        self._sslctx,
                        self._register_protocol_detector,
                        self.TIMEOUT,
                    ),
                    client,
                ),
            )

    def _register_protocol_detector(self, dispatcher):
        dispatcher.switch_implementation(
            _ProtocolDetector(
                self._handlers,
                self.TIMEOUT,
            ),
        )

        return dispatcher

    @traceback(on=log.name)
    def serve_forever(self):
        self.log.debug("Running")
        self._reactor.process_requests()

    def add_detector(self, detector):
        self.log.debug("Adding detector %s", detector)
        self._handlers.append(detector)

    def stop(self):
        self.log.debug("Stopping Acceptor")
        self._reactor.stop()


class _CannotDetectProtocol(Exception):
    pass


class _Stopped(Exception):
    pass
