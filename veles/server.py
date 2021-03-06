# -*- coding: utf-8 -*-
"""
.. invisible:
     _   _ _____ _     _____ _____
    | | | |  ___| |   |  ___/  ___|
    | | | | |__ | |   | |__ \ `--.
    | | | |  __|| |   |  __| `--. \
    \ \_/ / |___| |___| |___/\__/ /
     \___/\____/\_____|____/\____/

Created on Jan 14, 2014.

Master part of the master-slave interoperability.

███████████████████████████████████████████████████████████████████████████████

Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.

███████████████████████████████████████████████████████████████████████████████
"""


import argparse
from collections import namedtuple
import json
import socket
import time
import uuid

import numpy
import six
from twisted.internet import reactor, threads, task
from twisted.internet.protocol import ServerFactory
from twisted.names import client as dns
from twisted.python.failure import Failure
import zmq

from veles.cmdline import CommandLineArgumentsRegistry
from veles.config import root
import veles.external.fysom as fysom
from veles.txzmq import ZmqConnection, ZmqEndpoint, SharedIO
from veles.logger import Logger
from veles.network_common import NetworkAgent, StringLineReceiver, IDLogger
from veles.thread_pool import errback


class ZmqRouter(ZmqConnection, Logger):
    socketType = zmq.ROUTER

    COMMANDS = {
        'job':
        lambda protocol, payload: protocol.jobRequestReceived(),
        'update':
        lambda protocol, payload: protocol.updateReceived(payload)
    }
    RESERVE_SHMEM_SIZE = 0.05

    def __init__(self, host, *endpoints, **kwargs):
        super(ZmqRouter, self).__init__(endpoints, logger=kwargs.get("logger"))
        ignore_unknown_commands = kwargs.get("ignore_unknown_commands", False)
        self.host = host
        self.routing = {b'job': {}, b'update': {}}
        self.shmem = {}
        self.use_shmem = kwargs.get('use_shared_memory', True)
        self._command = None
        self._command_str = None
        self.ignore_unknown_commands = ignore_unknown_commands
        self.pickles_compression = root.common.engine.network_compression

    def change_log_message(self, msg):
        return "zmq: " + msg

    def parseHeader(self, message):
        try:
            routing, node_id, command = message[0:3]
        except ValueError:
            self.error("ZeroMQ sent an invalid message %s", message[0:3])
            return
        node_id = node_id.decode('charmap')
        self.routing[command][node_id] = routing
        protocol = self.host.protocols.get(node_id)
        if protocol is None:
            self.error("ZeroMQ sent unknown node ID %s (unsync during drop?)",
                       node_id)
            self.reply(node_id, b'error', b'Unknown node ID')
            return
        cmdstr = command.decode('charmap')
        command = ZmqRouter.COMMANDS.get(cmdstr)
        if command is None and not self.ignore_unknown_commands:
            self.error("Received an unknown command %s with node ID %s",
                       cmdstr, node_id)
            self.reply(node_id, b'error', b'Unknown command')
            return
        return node_id, command, cmdstr, protocol

    def messageReceived(self, message):
        if self._command is None:
            self.messageHeaderReceived(message)
        self.debug("Received ZeroMQ message %s", str(message[0:3]))
        try:
            payload = message[3]
        except IndexError:
            self.error("ZeroMQ sent an invalid message %s with node ID %s",
                       message, self.node_id)
            self.reply(self.node_id, b'error', b'Invalid message')
            return
        self.event("ZeroMQ", "end", dir="receive", id=self.node_id,
                   command=self._command_str, height=0.5)
        if self._command is None:
            return
        self._command(self._protocol, payload)
        self._command = None

    def messageHeaderReceived(self, header):
        try:
            self.node_id, self._command, self._command_str, self._protocol = \
                self.parseHeader(header)
        except TypeError:
            self.warning("Failed to parse the message header")
            return
        except Exception as e:
            errback(Failure(e))
        self.event("ZeroMQ", "begin", dir="receive", id=self.node_id,
                   command=self._command_str, height=0.5)

    def reply(self, node_id, channel, message):
        self.event("ZeroMQ", "begin", dir="send", id=node_id,
                   command=channel.decode('charmap'), height=0.5)
        if self.use_shmem:
            is_ipc = self.host.nodes[node_id]['endpoint'].startswith("ipc://")
            io_overflow = False
            shmem = self.shmem.get(node_id)
            if shmem is not None and channel == b"job":
                self.shmem[node_id].seek(0)
        try:
            pickles_size = self.send(
                self.routing[channel].pop(node_id), channel, message,
                io=shmem, pickles_compression=self.pickles_compression
                if not is_ipc else None)
        except ZmqConnection.IOOverflow:
            pickles_size = shmem.size
            self.shmem[node_id] = None
            io_overflow = True
        except KeyError:
            self.warning("Could not find node %s on channel %s",
                         node_id, channel)
            return
        if self.use_shmem and is_ipc and channel == b"job":
            if io_overflow or self.shmem.get(node_id) is None:
                self.shmem[node_id] = SharedIO(
                    "veles-job-" + node_id,
                    int(pickles_size * (1.0 + ZmqRouter.RESERVE_SHMEM_SIZE)))
        self.event("ZeroMQ", "end", dir="send", id=node_id,
                   command=channel.decode('charmap'), height=0.5)


class SlaveDescription(namedtuple(
        "SlaveDescriptionTuple",
        ['id', 'mid', 'pid', 'power', 'host', 'state'])):

    @staticmethod
    def make(info):
        args = dict(info)
        for f in SlaveDescription._fields:
            if f not in args:
                args[f] = None
        for f in info:
            if f not in SlaveDescription._fields:
                del args[f]
        return SlaveDescription(**args)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id


class VelesProtocol(StringLineReceiver, IDLogger):
    """A communication controller from server to client.

    Attributes:
        FSM_DESCRIPTION     The definition of the Finite State Machine of the
                            protocol.
    """

    def onFSMStateChanged(self, e):
        """
        Logs the current state transition.
        """
        self.debug("state: %s, %s -> %s", e.event, e.src, e.dst)

    def onConnected(self, e):
        self.info("Accepted %s", self.address)

    def onIdentified(self, e):
        desc = dict(self.nodes[self.id])
        for val in "executable", "argv", "PYTHONPATH", "cwd":
            if val in desc:
                del desc[val]
        self.info("New node joined from %s (%s)", self.address, desc)
        self.setWaiting(e)

    def onJobObtained(self, e):
        self.nodes[self.id]["state"] = "Working"

    def setWaiting(self, e):
        self.nodes[self.id]["state"] = "Waiting"

    def onDropped(self, e):
        self.warning("Lost connection with %s", self.address)
        if self.id in self.nodes:
            self.nodes[self.id]["state"] = "Offline"

    FSM_DESCRIPTION = {
        'initial': 'INIT',
        'events': [
            {'name': 'connect', 'src': 'INIT', 'dst': 'WAIT'},
            {'name': 'identify', 'src': 'WAIT', 'dst': 'WORK'},
            {'name': 'request_job', 'src': ('WORK', 'IDLE'),
             'dst': 'GETTING_JOB'},
            {'name': 'obtain_job', 'src': 'GETTING_JOB', 'dst': 'WORK'},
            {'name': 'refuse_job', 'src': 'GETTING_JOB', 'dst': 'WORK'},
            {'name': 'postpone_job', 'src': 'GETTING_JOB', 'dst': 'WORK'},
            {'name': 'idle', 'src': 'WORK', 'dst': 'IDLE'},
            {'name': 'drop', 'src': '*', 'dst': 'INIT'},
        ],
        'callbacks': {
            'onchangestate': onFSMStateChanged,
            'onconnect': onConnected,
            'onidentify': onIdentified,
            'onobtain_job': onJobObtained,
            'onpostpone_job': setWaiting,
            'onrequest_job': setWaiting,
            'onrefuse_job': setWaiting,
            'onidle': setWaiting,
            'ondrop': onDropped
        }
    }

    def __init__(self, addr, host):
        """
        Initializes the protocol.

        Parameters:
            addr    The address of the client (reported by Twisted).
            nodes   The clients which are known (dictionary, the key is ID).
            host An instance of producing VelesProtocolFactory.
        """
        super(VelesProtocol, self).__init__(logger=host.logger)
        self.addr = addr
        self.host = host
        self.nodes = self.host.nodes
        self._id = None
        self._not_a_slave = False
        self._balance = 0
        self._endpoint = None
        self.state = fysom.Fysom(VelesProtocol.FSM_DESCRIPTION, self)
        self._responders = {"handshake": self._handshake,
                            "change_power": self._changePower}
        self._jobs_processed = []
        self._last_job_submit_time = 0
        self._dropper_on_timeout = None
        self._job_timeout = host.job_timeout
        self._drop_on_timeout = (self._job_timeout > 0)

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, value):
        if value is None:
            return
        if self._id is not None:
            del self.host.protocols[self._id]
        self._id = value
        self.host.protocols[self._id] = self

    @property
    def address(self):
        return "%s:%d" % (self.addr.host, self.addr.port)

    @property
    def zmq_endpoint(self):
        return self._endpoint

    @property
    def not_a_slave(self):
        return self._not_a_slave

    @property
    def jobs_processed(self):
        return self._jobs_processed

    def connectionMade(self):
        self.hip = self.transport.getHost().host
        self.state.connect()

    def connectionLost(self, reason):
        if self.not_a_slave:
            return
        self.state.drop()
        if self._dropper_on_timeout is not None:
            self._dropper_on_timeout.cancel()
        try:
            self.host.job_requests.remove(self)
        except KeyError:
            pass
        if not self.host.workflow.is_running:
            self._erase_self(True)
            if len(self.host.protocols) == 0:
                self.host.launcher.stop()
        elif self.id in self.nodes:
            d = threads.deferToThreadPool(
                reactor, self.host.workflow.thread_pool,
                self.host.workflow.drop_slave,
                SlaveDescription.make(self.nodes[self.id])) \
                .addErrback(errback)
            d.addCallback(self._retryJobRequests)
            if self.host.must_respawn:
                reactor.callLater(1, self._respawn, self.nodes[self.id])
            self._erase_self()

    def lineReceived(self, line):
        self.debug("lineReceived:  %s", line)
        msg = json.loads(line.decode("utf-8"))
        if not isinstance(msg, dict):
            self.error("Could not parse the received line, dropping it")
            return
        if self._checkQuery(msg):
            return
        if self.not_a_slave:
            self._sendError("You must reconnect as a slave to send commands")
        cmd = msg.get('cmd')
        responder = self._responders.get(cmd)
        if responder is not None:
            responder(msg, line)
        else:
            self._sendError("No responder exists for command %s" % cmd)

    def jobRequestReceived(self):
        if self.id in self.host.paused_nodes:
            self.info("paused")
            self.host.paused_nodes[self.id] = True
            return
        self.state.request_job()
        if self.id in self.host.blacklist:
            self.warning("found in the blacklist, refusing the job")
            self._refuseJob()
        else:
            self._requestJob()

    def jobRequestFinished(self, data):
        if self.state.current != "GETTING_JOB":
            return
        if data is not None:
            if not data:
                # Try again later
                self.debug("job is not ready")
                self._balance -= 1
                if self._balance > 0:
                    # async mode - avoid deadlock
                    self.state.postpone_job()
                    self.host.zmq_connection.reply(self.id, b'job',
                                                   b'NEED_UPDATE')
                else:
                    self.debug("appending to the sync point job requests list")
                    self.host.job_requests.add(self)
                    hanged_slaves = []
                    for proto in self.host.protocols.values():
                        if len(proto.jobs_processed) == 0:
                            hanged_slaves.append(proto)
                    if len(hanged_slaves) > 0:
                        self.warning("Detected hanged nodes: %s",
                                     [s.id for s in hanged_slaves])
                    for slave in hanged_slaves:
                        self.host.blacklist.add(slave.id)
                        slave.transport.loseConnection()
                return
            self.state.obtain_job()
            self.host.zmq_connection.reply(self.id, b'job', data)
        else:
            self._refuseJob()

    def updateReceived(self, data):
        self.debug("update was received")
        if self._balance == 1:
            self.state.idle()
        upd = threads.deferToThreadPool(
            reactor, self.host.workflow.thread_pool,
            self.host.workflow.apply_data_from_slave, data,
            SlaveDescription.make(self.nodes[self.id]))
        upd.addCallback(self.updateFinished)
        upd.addErrback(errback)
        now = time.time()
        self.jobs_processed.append(now - self._last_job_submit_time)
        self.nodes[self.id]['jobs'] = len(self.jobs_processed)
        self._last_job_submit_time = now

    def updateFinished(self, result):
        if self.state.current not in ('WORK', 'GETTING_JOB', 'IDLE'):
            self.warning("Update was finished in an invalid state %s",
                         self.state.current)
            return
        if result:
            self.host.zmq_connection.reply(self.id, b'update', b'1')
        else:
            self.host.zmq_connection.reply(self.id, b'update', b'0')
        self._balance -= 1
        self.debug("update was finished, balance is %d now", self._balance)
        if self.state.current == 'GETTING_JOB':
            self._requestJob()
            return
        self._retryJobRequests()

    def sendLine(self, line):
        if six.PY3:
            super(VelesProtocol, self).sendLine(json.dumps(line))
        else:
            StringLineReceiver.sendLine(self, json.dumps(line))

    def _erase_self(self, del_node=False):
        if self.id in self.host.protocols:
            del self.host.protocols[self.id]
        if del_node:
            if self.id in self.nodes:
                del self.nodes[self.id]

    def _retryJobRequests(self, _=None):
        while len(self.host.job_requests) > 0:
            requester = self.host.job_requests.pop()
            requester._requestJob()

    def _checkQuery(self, msg):
        """Respond to possible informational requests.
        """
        query = msg.get('query')
        if query is None:
            return False
        self._not_a_slave = True
        checksum = msg.get('workflow')
        if checksum is None:
            self._sendError("Workflow checksum was not specified")
            return True
        valid_checksum = self.host.workflow.checksum
        if checksum != valid_checksum:
            self._sendError("Workflow checksum mismatch: mine is %s" %
                            valid_checksum)
            return True
        responders = {"nodes": lambda _: self.sendLine(self.host.active_nodes),
                      "endpoints":
                      lambda _: self.sendLine(self.host.zmq_endpoints)}
        responder = responders.get(query)
        self.info(self.nodes)
        if responder is None:
            self._sendError("%s query is not supported" % query)
        else:
            self.info("Fulfilled query \"%s\"", query)
            responder(msg)
        return True

    def _handshake(self, msg, line):
        if self.state.current != 'WAIT':
            self.error("Invalid state for a handshake command: %s",
                       self.state.current)
            self._sendError("Invalid state")
            return
        mysha = self.host.workflow.checksum
        your_sha = msg.get("checksum")
        if not your_sha:
            self.error("Did not receive the workflow checksum")
            self._sendError("Workflow checksum is missing")
            return
        if mysha != your_sha:
            self._sendError("Workflow checksum mismatch: "
                            "expected %s, got %s" % (mysha, your_sha))
            return
        must_reply = False
        msgid = msg.get("id")
        if msgid is None:
            self.id = str(uuid.uuid4())
            must_reply = True
        else:
            self.id = msgid
            if not self.nodes.get(self.id):
                self.warning("Did not recognize the received ID %s")
                must_reply = True
            else:
                self.sendLine({'reconnect': "ok"})
        if must_reply:
            try:
                _, mid, pid = self._extractClientInformation(msg)
            except Exception as e:
                self.error(str(e))
                return
            data = self.host.workflow.generate_initial_data_for_slave(
                SlaveDescription.make(self.nodes[self.id]))
            endpoint = self.host.choose_endpoint(self.id, mid, pid, self.hip)
            self.nodes[self.id]['endpoint'] = self._endpoint = endpoint
            retmsg = {'endpoint': endpoint, 'data': data}
            if not msgid:
                retmsg['id'] = self.id
            retmsg['log_id'] = self.host.launcher.log_id
            self.sendLine(retmsg)
        data = msg.get('data')
        if data is not None:
            threads.deferToThreadPool(
                reactor, self.host.workflow.thread_pool,
                self.host.workflow.apply_initial_data_from_slave,
                data, SlaveDescription.make(self.nodes[self.id])) \
                .addErrback(errback)
            self.nodes[self.id]['data'] = [d for d in data if d is not None]
        self.state.identify()

    def _changePower(self, msg, line):
        try:
            power = msg['power']
            self.nodes[self.id]['power'] = power
            self.info("power changed to %.2f", power)
        except KeyError:
            self.error("no 'power' key in the message")
        return

    def _extractClientInformation(self, msg):
        power = msg.get("power")
        mid = msg.get("mid")
        pid = msg.get("pid")
        if power is None:
            self._sendError("I need your computing power")
            raise Exception("Newly connected client did not send "
                            "it's computing power value, sending back "
                            "the error message")
        if mid is None:
            self._sendError("I need your machine id")
            raise Exception("Newly connected client did not send "
                            "it's machine id, sending back the error "
                            "message")
        if pid is None:
            self._sendError("I need your process id")
            raise Exception("Newly connected client did not send "
                            "it's process id, sending back the error "
                            "message")
        self.nodes[self.id] = {
            "power": power, "mid": mid, "pid": pid, "id": self.id, "jobs": 0,
            "backend": msg.get("backend"), "device": msg.get("device"),
            "argv": msg.get("argv"), "executable": msg.get("executable"),
            "PYTHONPATH": msg.get("PYTHONPATH"), "cwd": msg.get("cwd")}
        dns.lookupPointer(
            ".".join(reversed(self.addr.host.split("."))) + ".in-addr.arpa") \
            .addCallback(self._resolveAddr).addErrback(self._failToResolveAddr)
        return power, mid, pid

    def _failToResolveAddr(self, failure):
        self.warning("Failed to resolve %s: %s", self.addr.host,
                     failure.getErrorMessage())
        self.nodes[self.id]["host"] = self.addr.host

    def _resolveAddr(self, result):
        answers = result[0]
        if answers is None or len(answers) == 0:
            self._failToResolveAddr(Failure(ValueError(
                "no DNS records were found")))
            return
        host = str(answers[0].payload.name)
        if self.host.domain_name and host.endswith(self.host.domain_name):
            host = host[:-(len(self.host.domain_name) + 1)]
        self.debug("Address %s was resolved to %s", self.addr, host)
        self.nodes[self.id]["host"] = host

    def _sendError(self, err):
        """
        Sends the line with the specified error message.

        Parameters:
            err:    The error message.
        """
        self.error(err)
        self.sendLine({"error": err})

    def _requestJob(self):
        if self._balance > 1:
            self.debug("job balance %d, will give the job after applying "
                       "the update", self._balance)
            return
        self._balance += 1
        self.debug("generating the job, balance is %d", self._balance)
        if self._last_job_submit_time == 0:
            self._last_job_submit_time = time.time()
        job = threads.deferToThreadPool(
            reactor, self.host.workflow.thread_pool,
            self.host.workflow.generate_data_for_slave,
            SlaveDescription.make(self.nodes[self.id]))
        job.addCallback(self.jobRequestFinished)
        job.addErrback(errback)
        self._scheduleDropOnTimeout()

    def _refuseJob(self):
        self._balance -= 1
        self.state.refuse_job()
        self.host.zmq_connection.reply(self.id, b"job", False)
        self.debug("refused the job, balance is %d", self._balance)

    def _scheduleDropOnTimeout(self):
        if not self._drop_on_timeout or len(self.jobs_processed) < 3:
            return
        mean = numpy.mean(self.jobs_processed)
        stddev = numpy.std(self.jobs_processed)
        timeout = max(mean + stddev * 3, self._job_timeout)
        if self._dropper_on_timeout is not None:
            self._dropper_on_timeout.cancel()
        self._dropper_on_timeout = task.deferLater(
            reactor, timeout, self._dropOnTimeout, timeout)
        self._dropper_on_timeout.addErrback(lambda _: None)

    def _dropOnTimeout(self, timeout):
        self.error("timeout (%.3f seconds) was exceeded. Dropping this slave.",
                   timeout)
        self.transport.loseConnection()
        self.host.blacklist.add(self.id)

    def _respawn(self, desc, effort=1):
        executable = desc["executable"]
        cwd = desc["cwd"]
        python_path = desc["PYTHONPATH"]
        argv = desc["argv"]
        if any(x is None for x in (executable, argv, cwd)):
            self.warning("Failed to respawn: not enough information")
            return
        if "-b" not in argv and "--background" not in argv:
            argv.insert(1, "-b")
        effort += 1
        self.info("Respawning...")
        try:
            self.host.workflow.launcher.launch_remote_progs(
                self.addr.host, "%s %s" % (executable, " ".join(argv)),
                cwd=cwd, python_path=python_path)
        except:
            reactor.callLater(1 << effort, self._respawn, self.nodes[self.id],
                              effort)


@six.add_metaclass(CommandLineArgumentsRegistry)
class Server(NetworkAgent, ServerFactory):
    """
    UDT/TCP server operating on a single socket
    """

    def __init__(self, configuration, workflow, **kwargs):
        super(Server, self).__init__(configuration, workflow)
        self._listener_ = None
        parser = Server.init_parser(**kwargs)
        self.args, _ = parser.parse_known_args(self.argv)
        self.job_timeout = self.args.job_timeout * 60
        self.must_respawn = self.args.respawn
        self.nodes = {}
        self.protocols = {}
        self.job_requests = set()
        self.blacklist = set()
        self.paused_nodes = {}
        fqdn = socket.getfqdn()
        host = socket.gethostname()
        self.domain_name = fqdn[len(host) + 1:] if fqdn != host else ""
        if self.domain_name:
            self.debug("Domain name was resolved to %s", self.domain_name)
        self._listener_ = \
            reactor.listenTCP(self.port, self, interface=self.address)
        self.info("Accepting new connections on %s:%d",
                  self.address, self.port)
        try:
            self.zmq_connection = ZmqRouter(
                self, ZmqEndpoint("bind", "inproc://veles"),
                ZmqEndpoint("bind", "rndipc://veles-ipc-:"),
                ZmqEndpoint("bind", "rndtcp://*:1024:65535:1"),
                logger=self.logger)
        except zmq.error.ZMQBindError:
            self.exception("Could not setup ZeroMQ socket")
            raise
        self.zmq_ipc_fn, self.zmq_tcp_port = self.zmq_connection.rnd_vals
        self.zmq_endpoints = {"inproc": "inproc://veles",
                              "ipc": "ipc://%s" % self.zmq_ipc_fn,
                              "tcp": "tcp://*:%d" % self.zmq_tcp_port}
        self.info("ZeroMQ endpoints: %s",
                  " ".join(sorted(self.zmq_endpoints.values())))

    def __repr__(self):
        return "veles.Server with %d nodes and %d protocols on %s:%d" % (
            len(self.nodes), len(self.protocols), self.address, self.port)

    @staticmethod
    def init_parser(**kwargs):
        """
        Initializes an instance of argparse.ArgumentParser.
        """
        parser = kwargs.get("parser", argparse.ArgumentParser())
        parser.add_argument("--job-timeout", type=int,
                            default=kwargs.get("job_timeout", 2),
                            help="Slaves which remain in WORK state longer "
                            "than this time (in mins) will be dropped.") \
            .mode = ("master",)
        parser.add_argument("--respawn", default=False,
                            help="Relaunch dropped slaves via SSH.",
                            action='store_true').mode = ("master",)
        return parser

    def choose_endpoint(self, sid, mid, pid, hip):
        if self.mid == mid:
            if self.pid == pid:
                self.debug("Considering %s to be in the same process, "
                           "assigned inproc endpoint", sid)
                return self.zmq_endpoints["inproc"]
            else:
                self.debug("Considering %s to be on the same machine, "
                           "assigned ipc endpoint", sid)
                return self.zmq_endpoints["ipc"]
        else:
            return self.zmq_endpoints["tcp"].replace("*", hip)

    def pause(self, slave_id):
        self.paused_nodes[slave_id] = False

    def resume(self, slave_id):
        try:
            paused = self.paused_nodes[slave_id]
            del self.paused_nodes[slave_id]
            self.info("resumed")
            if paused:
                self.protocols[slave_id].jobRequestReceived()
        except KeyError:
            self.warning("Slave %s was not paused, so not resumed", slave_id)

    def print_stats(self):
        pass

    def buildProtocol(self, addr):
        return VelesProtocol(addr, self)

    @property
    def active_nodes(self):
        nodes = {}
        for pid in self.protocols.keys():
            nodes[pid] = self.nodes[pid]
        return nodes

    def close(self):
        if self._listener_ is not None:
            self._listener_.stopListening()
