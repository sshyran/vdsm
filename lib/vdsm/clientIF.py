#
# Copyright 2011-2017 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import errno
import os
import os.path
import socket
import time
import threading
from functools import partial
from weakref import proxy
from collections import defaultdict

from yajsonrpc.betterAsyncore import Reactor
from yajsonrpc.exception import JsonRpcBindingsError
from yajsonrpc.stompreactor import StompClient, StompRpcServer
from yajsonrpc import Notification
from vdsm import sslutils
from vdsm.config import config
from vdsm.common import exception
from vdsm.common.define import doneCode, errCode
import vdsm.common.time
from vdsm.protocoldetector import MultiProtocolAcceptor
from vdsm.momIF import MomClient
from vdsm.virt import events
from vdsm.virt import migration
from vdsm.virt import recovery
from vdsm.virt import secret
from vdsm.virt import vmstatus
from vdsm.virt.vmchannels import Listener
from vdsm.virt.vmdevices.storage import DISK_TYPE
from vdsm.virt.utils import isVdsmImage
import libvirt
from vdsm import alignmentScan
from vdsm import numa
from vdsm.common import concurrent
from vdsm.common import function
from vdsm.common import libvirtconnection
from vdsm.common import response
from vdsm.common import supervdsm
from vdsm.virt import vm
from vdsm.virt.qemuguestagent import QemuGuestAgentPoller
from vdsm.virt.vm import DestroyedOnResumeError, Vm

try:
    import vdsm.gluster.api as gapi
    _glusterEnabled = True
except ImportError:
    _glusterEnabled = False


class clientIF(object):
    """
    The client interface of vdsm.

    Exposes vdsm verbs as json-rpc or xml-rpc functions.
    """
    _instance = None
    _instanceLock = threading.Lock()

    def __init__(self, irs, log, scheduler):
        """
        Initialize the (single) clientIF instance

        :param irs: a Dispatcher object to be used as this object's irs.
        :type irs: :class:`vdsm.storage.dispatcher.Dispatcher`
        :param log: a log object to be used for this object's logging.
        :type log: :class:`logging.Logger`
        """
        self.vmContainerLock = threading.Lock()
        self._networkSemaphore = threading.Semaphore()
        self._shutdownSemaphore = threading.Semaphore()
        self.irs = irs
        if self.irs:
            self._contEIOVmsCB = partial(clientIF.contEIOVms, proxy(self))
            self.irs.registerDomainStateChangeCallback(self._contEIOVmsCB)
        self.log = log
        self._recovery = True
        # TODO: The guest agent related code spreads around too much. There is
        # QemuGuestAgentPoller and ChannelListner here and then many instances
        # of GuestAgent per VM in vm.py. This should be refactored and
        # operated by single object. Idealy the distinction between what is
        # served by QEMU-GA and what is server by oVirt GA should not be
        # visible to the rest of the code.
        self.channelListener = Listener(self.log)
        self.qga_poller = QemuGuestAgentPoller(self, log, scheduler)
        self.mom = None
        self.servers = {}
        self._broker_client = None
        self._subscriptions = defaultdict(list)
        self._scheduler = scheduler
        self._unknown_vm_ids = set()
        if _glusterEnabled:
            self.gluster = gapi.GlusterApi()
        else:
            self.gluster = None
        try:
            self.vmContainer = {}
            self.lastRemoteAccess = 0
            self._enabled = True
            self._netConfigDirty = False
            self._prepareMOM()
            secret.clear()
            concurrent.thread(self._recoverThread, name='vmrecovery').start()
            self.channelListener.settimeout(
                config.getint('vars', 'guest_agent_timeout'))
            self.channelListener.start()
            self.qga_poller.start()
            self.threadLocal = threading.local()
            self.threadLocal.client = ''

            host = config.get('addresses', 'management_ip')
            port = config.getint('addresses', 'management_port')

            # When IPv6 is not enabled, fallback to listen on IPv4 address
            try:
                self._createAcceptor(host, port)
            except socket.error as e:
                if e.errno == errno.EAFNOSUPPORT and host in ('::', '::1'):
                    fallback_host = '0.0.0.0'
                    self._createAcceptor(fallback_host, port)
                else:
                    raise

            self._prepareHttpServer()
            self._prepareJSONRPCServer()
            self._connectToBroker()
        except:
            self.log.error('failed to init clientIF, '
                           'shutting down storage dispatcher')
            if self.irs:
                self.irs.prepareForShutdown()
            raise

    def getVMs(self):
        """
        Get a snapshot of the currently registered VMs.
        Return value will be a dict of {vmUUID: VM_object}
        """
        with self.vmContainerLock:
            return self.vmContainer.copy()

    def get_unknown_vm_ids(self):
        """
        Return iterable of unknown VM ids that were spotted.

        This is intended to serve for detection of external VMs.
        """
        unknown_vm_ids = []
        with self.vmContainerLock:
            for vm_id in self._unknown_vm_ids.copy():
                if vm_id in self.vmContainer:
                    self._unknown_vm_ids.remove(vm_id)
                else:
                    unknown_vm_ids.append(vm_id)
        return unknown_vm_ids

    @property
    def ready(self):
        return (self.irs is None or self.irs.ready) and not self._recovery

    def notify(self, event_id, params=None):
        """
        Send notification using provided subscription id as
        event_id and a dictionary as event body. Before sending
        there is notify_time added on top level to the dictionary.

        Please consult event-schema.yml in order to build an appropriate event.
        https://github.com/oVirt/vdsm/blob/master/lib/api/vdsm-events.yml

        Args:
            event_id (string): unique event name
            params (dict): event content
        """
        if not params:
            params = {}

        if not self.ready:
            self.log.warning('Not ready yet, ignoring event %r args=%r',
                             event_id, params)
            return

        json_binding = self.servers['jsonrpc']

        def _send_notification(message):
            json_binding.reactor.server.send(
                message, config.get('addresses', 'event_queue'))

        try:
            notification = Notification(event_id, _send_notification,
                                        json_binding.bridge.event_schema)
            notification.emit(params)
            self.log.debug("Sending notification %s with params %s ",
                           event_id, params)
        except KeyError:
            self.log.warning("Attempt to send an event when jsonrpc binding"
                             " not available")

    def contEIOVms(self, sdUUID, isDomainStateValid):
        # This method is called everytime the onDomainStateChange
        # event is emitted, this event is emitted even when a domain goes
        # INVALID if this happens there is nothing to do
        if not isDomainStateValid:
            return

        libvirtCon = libvirtconnection.get()
        libvirtVms = libvirtCon.listAllDomains(
            libvirt.VIR_CONNECT_LIST_DOMAINS_PAUSED)

        with self.vmContainerLock:
            self.log.info("vmContainerLock acquired")
            for libvirtVm in libvirtVms:
                state = libvirtVm.state(0)
                if state[1] == libvirt.VIR_DOMAIN_PAUSED_IOERROR:
                    vmId = libvirtVm.UUIDString()
                    vmObj = self.vmContainer[vmId]
                    if sdUUID in vmObj.sdIds:
                        self.log.info("Trying to resume VM %s after EIO", vmId)
                        try:
                            vmObj.maybe_resume()
                        except DestroyedOnResumeError:
                            pass

    @classmethod
    def getInstance(cls, irs=None, log=None, scheduler=None):
        with cls._instanceLock:
            if cls._instance is None:
                if log is None:
                    raise Exception("Logging facility is required to create "
                                    "the single clientIF instance")
                else:
                    cls._instance = clientIF(irs, log, scheduler)
        return cls._instance

    def _createAcceptor(self, host, port):
        sslctx = sslutils.create_ssl_context()
        self._reactor = Reactor()

        self._acceptor = MultiProtocolAcceptor(self._reactor, host,
                                               port, sslctx)

    def _connectToBroker(self):
        if config.getboolean('vars', 'broker_enable'):
            broker_address = config.get('addresses', 'broker_address')
            broker_port = config.getint('addresses', 'broker_port')
            request_queues = config.get('addresses', 'request_queues')

            sslctx = sslutils.create_ssl_context()
            sock = socket.socket()
            sock.connect((broker_address, broker_port))
            if sslctx:
                sock = sslctx.wrapSocket(sock)

            self._broker_client = StompClient(sock, self._reactor)
            for destination in request_queues.split(","):
                self._subscriptions[destination] = StompRpcServer(
                    self.servers['jsonrpc'].server,
                    self._broker_client,
                    destination,
                    broker_address,
                    config.getint('vars', 'connection_stats_timeout'),
                    self
                )

    def _prepareHttpServer(self):
        if config.getboolean('vars', 'http_enable'):
            try:
                from vdsm.rpc.http import Server
                from vdsm.rpc.http import HttpDetector
            except ImportError:
                self.log.error('Unable to load the http server module. '
                               'Please make sure it is installed.')
            else:
                http_server = Server(self, self.log)
                self.servers['http'] = http_server
                http_detector = HttpDetector(http_server)
                self._acceptor.add_detector(http_detector)

    def _prepareJSONRPCServer(self):
        if config.getboolean('vars', 'jsonrpc_enable'):
            try:
                from vdsm.rpc import Bridge
                from vdsm.rpc.bindingjsonrpc import BindingJsonRpc
                from yajsonrpc.stompreactor import StompDetector
            except ImportError:
                self.log.warn('Unable to load the json rpc server module. '
                              'Please make sure it is installed.')
            else:
                bridge = Bridge.DynamicBridge()
                json_binding = BindingJsonRpc(
                    bridge, self._subscriptions,
                    config.getint('vars', 'connection_stats_timeout'),
                    self._scheduler, self)
                self.servers['jsonrpc'] = json_binding
                stomp_detector = StompDetector(json_binding)
                self._acceptor.add_detector(stomp_detector)

    def _prepareMOM(self):
        momconf = config.get("mom", "conf")

        self.mom = MomClient(momconf)

    def _wait_for_shutting_down_vms(self):
        """
        Wait loop checking remaining VMs in vm container

        This method is helper method that highers the
        probability of engine to properly acknowledge
        that all VMs are terminated by host shutdown.

        The VMs are shutdown by external service: libvirt-guests
        The service pauses system shutdown on systemd shutdown
        and gracefully shutdowns the running VMs.
        """
        # how long to wait before release shutdown
        # we are waiting in whole seconds
        # if config is not present, do not wait
        timeout = config.getint('vars', 'timeout_engine_clear_vms')

        for _ in range(timeout * 10):
            if not self.vmContainer:
                # once all VMs are cleared exit
                break
            time.sleep(0.1)

    def prepareForShutdown(self):
        """
        Prepare server for shutdown.

        Should be called before taking server down.
        """
        if not self._shutdownSemaphore.acquire(blocking=False):
            self.log.debug('cannot run prepareForShutdown concurrently')
            return errCode['unavail']
        try:
            if not self._enabled:
                self.log.debug('cannot run prepareForShutdown twice')
                return errCode['unavail']

            self._wait_for_shutting_down_vms()

            self._acceptor.stop()
            for binding in self.servers.values():
                binding.stop()

            self._enabled = False
            secret.clear()
            self.channelListener.stop()
            self.qga_poller.stop()
            if self.irs:
                return self.irs.prepareForShutdown()
            else:
                return {'status': doneCode}
        finally:
            self._shutdownSemaphore.release()

    def start(self):
        for binding in self.servers.values():
            binding.start()
        self.thread = concurrent.thread(self._reactor.process_requests,
                                        name='Reactor thread')
        self.thread.start()

    def prepareVolumePath(self, drive, vmId=None):
        if type(drive) is dict:
            device = drive['device']
            # PDIV drive format
            # Since version 4.2 cdrom may use a PDIV format
            if device in ("cdrom", "disk") and isVdsmImage(drive):
                res = self.irs.prepareImage(
                    drive['domainID'], drive['poolID'],
                    drive['imageID'], drive['volumeID'])

                if res['status']['code']:
                    raise vm.VolumeError(drive)

                # The order of imgVolumesInfo is not guaranteed
                drive['volumeChain'] = res['imgVolumesInfo']
                drive['volumeInfo'] = res['info']

                # Not applicable for Ceph network disk as
                # Ceph disks are not vdsm images
                if drive.get('diskType') == DISK_TYPE.NETWORK:
                    if device == "cdrom":
                        raise exception.UnsupportedOperation(
                            "A cdrom device is not supported as network disk",
                            drive=drive)
                    volPath = self._prepare_network_drive(drive, res)
                else:
                    volPath = res['path']
            # GUID drive format
            elif "GUID" in drive:
                res = self.irs.getDevicesVisibility([drive["GUID"]])
                if not res["visible"][drive["GUID"]]:
                    raise vm.VolumeError(drive)

                res = self.irs.appropriateDevice(drive["GUID"], vmId)
                if res['status']['code']:
                    raise vm.VolumeError(drive)

                # Update size for LUN volume
                drive["truesize"] = res['truesize']
                drive["apparentsize"] = res['apparentsize']

                volPath = res['path']

            # cdrom and floppy drives
            elif (device in ('cdrom', 'floppy') and 'specParams' in drive):
                params = drive['specParams']
                if 'vmPayload' in params:
                    volPath = self._prepareVolumePathFromPayload(
                        vmId, device, params['vmPayload'])
                # next line can be removed in future, when < 3.3 engine
                # is not supported
                elif (params.get('path', '') == '' and
                      drive.get('path', '') == ''):
                    volPath = ''
                else:
                    volPath = drive.get('path', '')

            elif "path" in drive:
                volPath = drive['path']

            else:
                raise vm.VolumeError(drive)

        # For BC sake: None as argument
        elif not drive:
            volPath = drive

        #  For BC sake: path as a string.
        elif os.path.exists(drive):
            volPath = drive

        else:
            raise vm.VolumeError(drive)

        self.log.info("prepared volume path: %s", volPath)
        return volPath

    def _prepareVolumePathFromPayload(self, vmId, device, payload):
        """
        param vmId:
            VM UUID or None
        param device:
            either 'floppy' or 'cdrom'
        param payload:
            a dict formed like this:
            {'volId': 'volume id',   # volId is optional
             'file': {'filename': 'content', ...}}
        """
        funcs = {'cdrom': 'mkIsoFs', 'floppy': 'mkFloppyFs'}
        if device not in funcs:
            raise vm.VolumeError("Unsupported 'device': %s" % device)
        func = getattr(supervdsm.getProxy(), funcs[device])
        return func(vmId, payload['file'], payload.get('volId'))

    def teardownVolumePath(self, drive):
        res = {'status': doneCode}
        try:
            if isVdsmImage(drive):
                res = self.irs.teardownImage(drive['domainID'],
                                             drive['poolID'], drive['imageID'])
        except TypeError:
            # paths (strings) are not deactivated
            if not isinstance(drive, basestring):
                self.log.warning("Drive is not a vdsm image: %s",
                                 drive, exc_info=True)

        return res['status']['code']

    def getDiskAlignment(self, drive):
        """
        Returns the alignment of the disk partitions

        param drive:
        is either {"poolID": , "domainID": , "imageID": , "volumeID": }
        or {"GUID": }

        Return type: a dictionary with partition names as keys and
        True for aligned partitions and False for unaligned as values
        """
        aligning = {}
        volPath = self.prepareVolumePath(drive)
        try:
            out = alignmentScan.scanImage(volPath)
            for line in out:
                aligning[line.partitionName] = line.alignmentScanResult
        finally:
            self.teardownVolumePath(drive)

        return {'status': doneCode, 'alignment': aligning}

    def createVm(self, vmParams, vmRecover=False):
        with self.vmContainerLock:
            if not vmRecover:
                if vmParams['vmId'] in self.vmContainer:
                    return errCode['exist']
            vm = Vm(self, vmParams, vmRecover)
            ret = vm.run()
            if not response.is_error(ret):
                self.vmContainer[vm.id] = vm
            return ret

    def getAllVmStats(self):
        return [v.getStats() for v in self.vmContainer.values()]

    def getAllVmIoTunePolicies(self):
        vm_io_tune_policies = {}
        for v in self.vmContainer.values():
                vm_io_tune_policies[v.id] = {'policy': v.getIoTunePolicy(),
                                             'current_values': v.getIoTune()}
        return vm_io_tune_policies

    def createStompClient(self, client_socket):
        if 'jsonrpc' in self.servers:
            json_binding = self.servers['jsonrpc']
            reactor = json_binding.reactor
            return reactor.createClient(client_socket)
        else:
            raise JsonRpcBindingsError()

    def _recoverThread(self):
        # Trying to run recover process until it works. During that time vdsm
        # stays in recovery mode (_recover=True), means all api requests
        # returns with "vdsm is in initializing process" message.
        function.retry(self._recoverExistingVms, sleep=5)

    def _recoverExistingVms(self):
        start_time = vdsm.common.time.monotonic_time()
        try:
            self.log.debug('recovery: started')

            # Starting up libvirt might take long when host under high load,
            # we prefer running this code in external thread to avoid blocking
            # API response.
            mog = min(config.getint('vars', 'max_outgoing_migrations'),
                      numa.cpu_topology().cores)
            migration.SourceThread.ongoingMigrations.bound = mog

            recovery.all_domains(self)

            # recover stage 3: waiting for domains to go up
            self._waitForDomainsUp()

            self._recovery = False

            # Now if we have VMs to restore we should wait pool connection
            # and then prepare all volumes.
            # Actually, we need it just to get the resources for future
            # volumes manipulations
            self._waitForStoragePool()

            self._preparePathsForRecoveredVMs()

            self.log.info('recovery: completed in %is',
                          vdsm.common.time.monotonic_time() - start_time)

        except:
            self.log.exception("recovery: failed")
            raise

    def dispatchLibvirtEvents(self, conn, dom, *args):
        try:
            eventid = args[-1]
            vmid = dom.UUIDString()
            v = self.vmContainer.get(vmid)

            if not v:
                self.log.debug('unknown vm %s event %s args %s',
                               vmid, events.event_name(eventid), args)
                self._unknown_vm_ids.add(vmid)
                return

            # pylint cannot tell that unpacking the args tuple is safe, so we
            # must disbale this check here.
            # TODO: The real solution is to create a method per callback with
            # fixed number of arguments, and register the callbacks separately
            # in libvirt.
            # pylint: disable=unbalanced-tuple-unpacking

            if eventid == libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE:
                event, detail = args[:-1]
                v.onLibvirtLifecycleEvent(event, detail, None)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_REBOOT:
                v.onReboot()
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_RTC_CHANGE:
                utcoffset, = args[:-1]
                v.onRTCUpdate(utcoffset)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_IO_ERROR_REASON:
                srcPath, devAlias, action, reason = args[:-1]
                v.onIOError(devAlias, reason, action)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_GRAPHICS:
                phase, localAddr, remoteAddr, authScheme, subject = args[:-1]
                v.log.debug('graphics event phase '
                            '%s localAddr %s remoteAddr %s'
                            'authScheme %s subject %s',
                            phase, localAddr, remoteAddr, authScheme, subject)
                if phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_INITIALIZE:
                    v.onConnect(remoteAddr['node'], remoteAddr['service'])
                elif phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_DISCONNECT:
                    v.onDisconnect(clientIp=remoteAddr['node'],
                                   clientPort=remoteAddr['service'])
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_WATCHDOG:
                action, = args[:-1]
                v.onWatchdogEvent(action)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_JOB_COMPLETED:
                v.onJobCompleted(args)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_DEVICE_REMOVED:
                device_alias, = args[:-1]
                v.onDeviceRemoved(device_alias)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD:
                dev, path, threshold, excess = args[:-1]
                v.drive_monitor.on_block_threshold(
                    dev, path, threshold, excess)
            else:
                v.log.debug('unhandled libvirt event (event_name=%s, args=%s)',
                            events.event_name(eventid), args)

        except:
            self.log.error("Error running VM callback", exc_info=True)

    def _waitForDomainsUp(self):
        while self._enabled:
            launching = sum(int(v.lastStatus == vmstatus.WAIT_FOR_LAUNCH)
                            for v in self.vmContainer.values())
            if not launching:
                break
            else:
                self.log.info(
                    'recovery: waiting for %d domains to go up',
                    launching)
            time.sleep(1)

    def _waitForStoragePool(self):
        while (self._enabled and self.vmContainer and
               not self.irs.getConnectedStoragePoolsList()['poollist']):
            self.log.info('recovery: waiting for storage pool to go up')
            time.sleep(5)

    def _preparePathsForRecoveredVMs(self):
        vm_objects = self.vmContainer.values()
        num_vm_objects = len(vm_objects)
        for idx, vm_obj in enumerate(vm_objects):
            # Let's recover as much VMs as possible
            try:
                # Do not prepare volumes when system goes down
                if self._enabled:
                    self.log.info(
                        'recovery [%d/%d]: preparing paths for'
                        ' domain %s', idx + 1, num_vm_objects, vm_obj.id)
                    vm_obj.preparePaths()
            except:
                self.log.exception(
                    "recovery [%d/%d]: failed for vm %s",
                    idx + 1, num_vm_objects, vm_obj.id)

    def _prepare_network_drive(self, drive, res):
        """
        Fills drive object for network drives with network-specific data.

        Network (gluster) drives have a very special ephemeral runtime
        path specification, and it can't be resolved to a typical storage
        path in runtime. Therefore, we have to replace storage path
        with a VM path.

        So
            /rhev/data-center/mnt/glusterSD/host:vol/sd_id/images/img_id/vol_id
        is replaced with
            vol/sd_id/images/img_id/vol_id

        Arguments:
            drive (dict like): Drive description. Function modifies it
                as a side-effect.
            res (dict): drive description.

        Returns:
            Network friendly drive's path value.
        """
        volinfo = res['info']
        img_dir, _ = os.path.split(volinfo["path"])
        for entry in drive['volumeChain']:
            entry["path"] = os.path.join(img_dir,
                                         entry["volumeID"])
        drive['protocol'] = volinfo['protocol']
        # currently, single host is provided due to this bug:
        # https://bugzilla.redhat.com/1465810
        drive['hosts'] = [volinfo['hosts'][0]]
        return volinfo['path']
