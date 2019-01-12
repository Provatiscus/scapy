# This file is part of Scapy
# See http://www.secdev.org/projects/scapy for more information
# Copyright (C) Philippe Biondi <phil@secdev.org>
# Copyright (C) Gabriel Potter <gabriel@potter.fr>
# This program is published under a GPLv2 license

"""
Customizations needed to support Microsoft Windows.
"""

from __future__ import absolute_import
from __future__ import print_function
import os
import socket
import subprocess as sp
from glob import glob
import struct

import scapy
import scapy.consts
from scapy.arch.windows.structures import _windows_title, \
    GetAdaptersAddresses, GetIpForwardTable, GetIpForwardTable2
from scapy.consts import WINDOWS, WINDOWS_XP
from scapy.config import conf, ConfClass
from scapy.error import Scapy_Exception, log_loading, log_runtime, warning
from scapy.pton_ntop import inet_ntop, inet_pton
from scapy.utils import atol, itom, pretty_list, mac2str, str2mac
from scapy.utils6 import construct_source_candidate_set, in6_getscope
from scapy.data import ARPHDR_ETHER, load_manuf
import scapy.modules.six as six
from scapy.modules.six.moves import input, winreg, UserDict
from scapy.compat import plain_str
from scapy.supersocket import SuperSocket

conf.use_winpcapy = True
conf.use_pcap = False
conf.use_dnet = False

# These import must appear after setting conf.use_* variables
from scapy.arch import pcapdnet  # noqa: E402
from scapy.arch.pcapdnet import NPCAP_PATH, get_if_list  # noqa: E402

# hot-patching socket for missing variables on Windows
if not hasattr(socket, 'IPPROTO_IPIP'):
    socket.IPPROTO_IPIP = 4
if not hasattr(socket, 'IP_RECVTTL'):
    socket.IP_RECVTTL = 12
if not hasattr(socket, 'IPV6_HDRINCL'):
    socket.IPV6_HDRINCL = 36
# https://bugs.python.org/issue29515
if not hasattr(socket, 'IPPROTO_IPV6'):
    socket.SOL_IPV6 = 41
if not hasattr(socket, 'SOL_IPV6'):
    socket.SOL_IPV6 = socket.IPPROTO_IPV6
if not hasattr(socket, 'IPPROTO_GRE'):
    socket.IPPROTO_GRE = 47
if not hasattr(socket, 'IPPROTO_AH'):
    socket.IPPROTO_AH = 51
if not hasattr(socket, 'IPPROTO_ESP'):
    socket.IPPROTO_ESP = 50

_WlanHelper = NPCAP_PATH + "\\WlanHelper.exe"


def _encapsulate_admin(cmd):
    """Encapsulate a command with an Administrator flag"""
    # To get admin access, we start a new powershell instance with admin
    # rights, which will execute the command. This needs to be done from a
    # powershell as we run it from a cmd.
    # ! Behold !
    return ("powershell /command \"Start-Process cmd "
            "-windowstyle hidden -Wait -PassThru -Verb RunAs "
            "-ArgumentList '/c %s'\"" % cmd)


def _get_npcap_dot11_adapters():
    """
    Get the npcap 802.11 adapters from the registry or None if npcap
    is not 802.11 enabled.
    """
    hkey = winreg.HKEY_LOCAL_MACHINE
    node = r"SYSTEM\CurrentControlSet\Services\npcap\Parameters"
    try:
        key = winreg.OpenKey(hkey, node)
        dot11_adapters, _ = winreg.QueryValueEx(key, "Dot11Adapters")
        winreg.CloseKey(key)
    except WindowsError:
        return None
    return dot11_adapters


def _where(filename, dirs=None, env="PATH"):
    """Find file in current dir, in deep_lookup cache or in system path"""
    if dirs is None:
        dirs = []
    if not isinstance(dirs, list):
        dirs = [dirs]
    if glob(filename):
        return filename
    paths = [os.curdir] + os.environ[env].split(os.path.pathsep) + dirs
    try:
        return next(os.path.normpath(match)
                    for path in paths
                    for match in glob(os.path.join(path, filename))
                    if match)
    except (StopIteration, RuntimeError):
        raise IOError("File not found: %s" % filename)


def win_find_exe(filename, installsubdir=None, env="ProgramFiles"):
    """Find executable in current dir, system path or given ProgramFiles subdir"""  # noqa: E501
    fns = [filename] if filename.endswith(".exe") else [filename + ".exe", filename]  # noqa: E501
    for fn in fns:
        try:
            if installsubdir is None:
                path = _where(fn)
            else:
                path = _where(fn, dirs=[os.path.join(os.environ[env], installsubdir)])  # noqa: E501
        except IOError:
            path = None
        else:
            break
    return path


class WinProgPath(ConfClass):
    _default = "<System default>"

    def __init__(self):
        self._reload()

    def _reload(self):
        self.pdfreader = None
        self.psreader = None
        self.svgreader = None
        # We try some magic to find the appropriate executables
        self.dot = win_find_exe("dot")
        self.tcpdump = win_find_exe("windump")
        self.tshark = win_find_exe("tshark")
        self.tcpreplay = win_find_exe("tcpreplay")
        self.display = self._default
        self.hexedit = win_find_exe("hexer")
        self.sox = win_find_exe("sox")
        self.wireshark = win_find_exe("wireshark", "wireshark")
        self.usbpcapcmd = win_find_exe(
            "USBPcapCMD",
            installsubdir="USBPcap",
            env="programfiles"
        )
        self.powershell = win_find_exe(
            "powershell",
            installsubdir="System32\\WindowsPowerShell\\v1.0",
            env="SystemRoot"
        )
        self.cscript = win_find_exe("cscript", installsubdir="System32",
                                    env="SystemRoot")
        self.cmd = win_find_exe("cmd", installsubdir="System32",
                                env="SystemRoot")
        if self.wireshark:
            try:
                manu_path = load_manuf(os.path.sep.join(self.wireshark.split(os.path.sep)[:-1]) + os.path.sep + "manuf")  # noqa: E501
            except (IOError, OSError):  # FileNotFoundError not available on Py2 - using OSError  # noqa: E501
                log_loading.warning("Wireshark is installed, but cannot read manuf !")  # noqa: E501
                manu_path = None
            scapy.data.MANUFDB = conf.manufdb = manu_path

        self.os_access = (self.powershell is not None) or (self.cscript is not None)  # noqa: E501


def _exec_cmd(command):
    """Call a CMD command and return the output"""
    proc = sp.Popen(command,
                    stdout=sp.PIPE,
                    shell=True)
    res = proc.communicate()[0]
    if proc.returncode != 0:
        return b"error"
    return res


conf.prog = WinProgPath()
if not conf.prog.os_access:
    warning("Scapy did not detect powershell and cscript ! Routes, interfaces and much more won't work !")  # noqa: E501

if conf.prog.tcpdump and conf.use_npcap and conf.prog.os_access:
    def test_windump_npcap():
        """Return whether windump version is correct or not"""
        try:
            p_test_windump = sp.Popen([conf.prog.tcpdump, "-help"], stdout=sp.PIPE, stderr=sp.STDOUT)  # noqa: E501
            stdout, err = p_test_windump.communicate()
            _windows_title()
            _output = stdout.lower()
            return b"npcap" in _output and b"winpcap" not in _output
        except Exception:
            return False
    windump_ok = test_windump_npcap()
    if not windump_ok:
        warning("The installed Windump version does not work with Npcap ! Refer to 'Winpcap/Npcap conflicts' in scapy's doc")  # noqa: E501
    del windump_ok


def get_windows_if_list(extended=False):
    """Returns windows interfaces through GetAdaptersAddresses.

    params:
     - extended: include anycast and multicast IPv6 (default False)"""
    # Should work on Windows XP+
    def _get_mac(x):
        size = x["physical_address_length"]
        if size != 6:
            return ""
        data = bytearray(x["physical_address"])
        return str2mac(bytes(data)[:size])

    def _get_ips(x):
        unicast = x['first_unicast_address']
        anycast = x['first_anycast_address']
        multicast = x['first_multicast_address']

        def _resolve_ips(y):
            if not isinstance(y, list):
                return []
            ips = []
            for ip in y:
                addr = ip['address']['address'].contents
                if addr.si_family == socket.AF_INET6:
                    ip_key = "Ipv6"
                    si_key = "sin6_addr"
                else:
                    ip_key = "Ipv4"
                    si_key = "sin_addr"
                data = getattr(addr, ip_key)
                data = getattr(data, si_key)
                data = bytes(bytearray(data.byte))
                # Build IP
                if data:
                    ips.append(inet_ntop(addr.si_family, data))
            return ips

        ips = []
        ips.extend(_resolve_ips(unicast))
        if extended:
            ips.extend(_resolve_ips(anycast))
            ips.extend(_resolve_ips(multicast))
        return ips

    if six.PY2:
        _str_decode = lambda x: x.encode('utf8', errors='ignore')
    else:
        _str_decode = plain_str
    return [
        {
            "name": _str_decode(x["friendly_name"]),
            "win_index": x["interface_index"],
            "description": _str_decode(x["description"]),
            "guid": _str_decode(x["adapter_name"]),
            "mac": _get_mac(x),
            "ipv4_metric": 0 if WINDOWS_XP else x["ipv4_metric"],
            "ipv6_metric": 0 if WINDOWS_XP else x["ipv6_metric"],
            "ips": _get_ips(x)
        } for x in GetAdaptersAddresses()
    ]


def get_ips(v6=False):
    """Returns all available IPs matching to interfaces, using the windows system.
    Should only be used as a WinPcapy fallback."""
    res = {}
    for iface in six.itervalues(IFACES):
        ips = []
        for ip in iface.ips:
            if v6 and ":" in ip:
                ips.append(ip)
            elif not v6 and ":" not in ip:
                ips.append(ip)
        res[iface] = ips
    return res


def get_ip_from_name(ifname, v6=False):
    """Backward compatibility: indirectly calls get_ips
    Deprecated."""
    iface = IFACES.dev_from_name(ifname)
    return get_ips(v6=v6).get(iface, [""])[0]


class NetworkInterface(object):
    """A network interface of your local host"""

    def __init__(self, data=None):
        self.name = None
        self.ip = None
        self.mac = None
        self.pcap_name = None
        self.description = None
        self.invalid = False
        self.raw80211 = None
        self.cache_mode = None
        self.ipv4_metric = None
        self.ipv6_metric = None
        self.ips = None
        if data is not None:
            self.update(data)

    def update(self, data):
        """Update info about network interface according to given dnet dictionary"""  # noqa: E501
        self.data = data
        if 'netid' in data and data['netid'] == scapy.consts.LOOPBACK_NAME:
            # Force LOOPBACK_NAME: Some Windows systems overwrite 'name'
            self.name = scapy.consts.LOOPBACK_NAME
        else:
            self.name = data['name']
        self.description = data['description']
        self.win_index = data['win_index']
        self.guid = data['guid']
        self.mac = data['mac']
        self.ipv4_metric = data['ipv4_metric']
        self.ipv6_metric = data['ipv6_metric']
        self.ips = data['ips']
        if 'invalid' in data:
            self.invalid = data['invalid']
        # Other attributes are optional
        self._update_pcapdata()

        try:
            # Npcap loopback interface
            if self.name == scapy.consts.LOOPBACK_NAME and conf.use_npcap:
                # https://nmap.org/npcap/guide/npcap-devguide.html
                self.mac = "00:00:00:00:00:00"
                self.ip = "127.0.0.1"
                return
        except KeyError:
            pass

        try:
            self.ip = next(x for x in self.ips if ":" not in x)
        except StopIteration:
            pass

        try:
            # Windows native loopback interface
            if not self.ip and self.name == scapy.consts.LOOPBACK_NAME:
                self.ip = "127.0.0.1"
        except (KeyError, AttributeError, NameError) as e:
            print(e)

    def _update_pcapdata(self):
        if self.is_invalid():
            return
        for i in get_if_list():
            if i.endswith(self.guid):
                self.pcap_name = i
                return
        self.invalid = True

    def is_invalid(self):
        return self.invalid

    def _check_npcap_requirement(self):
        if not conf.use_npcap:
            raise OSError("This operation requires Npcap.")
        if self.raw80211 is None:
            # This checks if npcap has Dot11 enabled and if the interface is compatible,  # noqa: E501
            # by looking for the npcap/Parameters/Dot11Adapters key in the registry.  # noqa: E501
            dot11adapters = _get_npcap_dot11_adapters()
            self.raw80211 = (dot11adapters is not None and
                             (self.guid.lower() in dot11adapters.lower()))
        if not self.raw80211:
            raise Scapy_Exception("This interface does not support raw 802.11")

    def _npcap_set(self, key, val):
        """Internal function. Set a [key] parameter to [value]"""
        res = not _exec_cmd(_encapsulate_admin(
            " ".join([_WlanHelper, self.guid[1:-1], key, val])
        ))
        _windows_title()  # Reset title of the window
        return res

    def _npcap_get(self, key):
        data = _exec_cmd(" ".join([_WlanHelper, self.guid[1:-1], key]))
        _windows_title()  # Reset title of the window
        return data.strip()

    def mode(self):
        """Get the interface operation mode.
        Only available with Npcap."""
        self._check_npcap_requirement()
        return self._npcap_get("mode")

    def ismonitor(self):
        """Returns True if the interface is in monitor mode.
        Only available with Npcap."""
        if self.cache_mode is not None:
            return self.cache_mode
        try:
            res = (self.mode() == "monitor")
            self.cache_mode = res
            return res
        except Scapy_Exception:
            return False

    def setmonitor(self, enable=True):
        """Alias for setmode('monitor') or setmode('managed')
        Only available with Npcap"""
        # We must reset the monitor cache
        if enable:
            res = self.setmode('monitor')
            self.cache_mode = res
        else:
            res = self.setmode('managed')
            self.cache_mode = not res
        if not res:
            log_runtime.error("Npcap WlanHelper returned with an error code !")
        return res

    def availablemodes(self):
        """Get all available interface modes.
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return self._npcap_get("modes").split(",")

    def setmode(self, mode):
        """Set the interface mode. It can be:
        - 0 or managed: Managed Mode (aka "Extensible Station Mode")
        - 1 or monitor: Monitor Mode (aka "Network Monitor Mode")
        - 2 or master: Master Mode (aka "Extensible Access Point")
              (supported from Windows 7 and later)
        - 3 or wfd_device: The Wi-Fi Direct Device operation mode
              (supported from Windows 8 and later)
        - 4 or wfd_owner: The Wi-Fi Direct Group Owner operation mode
              (supported from Windows 8 and later)
        - 5 or wfd_client: The Wi-Fi Direct Client operation mode
              (supported from Windows 8 and later)
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        _modes = {
            0: "managed",
            1: "monitor",
            2: "master",
            3: "wfd_device",
            4: "wfd_owner",
            5: "wfd_client"
        }
        m = _modes.get(mode, "unknown") if isinstance(mode, int) else mode
        return self._npcap_set("mode", m)

    def channel(self):
        """Get the channel of the interface.
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return int(self._npcap_get("channel"))

    def setchannel(self, channel):
        """Set the channel of the interface (1-14):
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return self._npcap_set("channel", str(channel))

    def frequence(self):
        """Get the frequence of the interface.
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return int(self._npcap_get("freq"))

    def setfrequence(self, freq):
        """Set the channel of the interface (1-14):
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return self._npcap_set("freq", str(freq))

    def availablemodulations(self):
        """Get all available 802.11 interface modulations.
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return self._npcap_get("modus").split(",")

    def modulation(self):
        """Get the 802.11 modulation of the interface.
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        return self._npcap_get("modu")

    def setmodulation(self, modu):
        """Set the interface modulation. It can be:
           - 0: dsss
           - 1: fhss
           - 2: irbaseband
           - 3: ofdm
           - 4: hrdss
           - 5: erp
           - 6: ht
           - 7: vht
           - 8: ihv
           - 9: mimo-ofdm
           - 10: mimo-ofdm
           - the value directly
        Only available with Npcap."""
        # According to https://nmap.org/npcap/guide/npcap-devguide.html#npcap-feature-dot11  # noqa: E501
        self._check_npcap_requirement()
        _modus = {
            0: "dsss",
            1: "fhss",
            2: "irbaseband",
            3: "ofdm",
            4: "hrdss",
            5: "erp",
            6: "ht",
            7: "vht",
            8: "ihv",
            9: "mimo-ofdm",
            10: "mimo-ofdm",
        }
        m = _modus.get(modu, "unknown") if isinstance(modu, int) else modu
        return self._npcap_set("modu", str(m))

    def __repr__(self):
        return "<%s [%s] %s>" % (self.__class__.__name__,
                                 self.description,
                                 self.guid)


def get_if_raw_addr(iff):
    """Return the raw IPv4 address of interface"""
    if not iff.ip:
        return None
    return inet_pton(socket.AF_INET, iff.ip)


def pcap_service_name():
    """Return the pcap adapter service's name"""
    return "npcap" if conf.use_npcap else "npf"


def pcap_service_status():
    """Returns whether the windows pcap adapter is running or not"""
    outp = _exec_cmd('sc query %s | findstr STATE' % pcap_service_name())
    return b"running" in outp.lower()


def _pcap_service_control(action, askadmin=True):
    """Internal util to run pcap control command"""
    if not conf.prog.powershell:
        return False
    command = action + ' ' + pcap_service_name()
    stdout = _exec_cmd(_encapsulate_admin(command) if askadmin else command)
    return b"error" not in stdout.lower()


def pcap_service_start(askadmin=True):
    """Starts the pcap adapter. Will ask for admin. Returns True if success"""
    return _pcap_service_control('sc start', askadmin=askadmin)


def pcap_service_stop(askadmin=True):
    """Stops the pcap adapter. Will ask for admin. Returns True if success"""
    return _pcap_service_control('sc stop', askadmin=askadmin)


class NetworkInterfaceDict(UserDict):
    """Store information about network interfaces and convert between names"""

    def load(self):
        if not conf.prog.os_access:
            return
        for i in get_windows_if_list():
            try:
                interface = NetworkInterface(i)
                self.data[interface.guid] = interface
            except KeyError:
                pass

        if not self.data and conf.use_winpcapy:
            _detect = pcap_service_status()

            def _ask_user():
                if not conf.interactive:
                    return False
                while True:
                    _confir = input("Do you want to start it ? (yes/no) [y]: ").lower().strip()  # noqa: E501
                    if _confir in ["yes", "y", ""]:
                        return True
                    elif _confir in ["no", "n"]:
                        return False
                return False
            _error_msg = "No match between your pcap and windows network interfaces found. "  # noqa: E501
            if not _detect and not (hasattr(self, "restarted_adapter") and self.restarted_adapter):  # noqa: E501
                warning("Scapy has detected that your pcap service is not running !")  # noqa: E501
                if not conf.interactive or _ask_user():
                    succeed = pcap_service_start(askadmin=conf.interactive)
                    self.restarted_adapter = True
                    if succeed:
                        log_loading.info("Pcap service started !")
                        self.load()
                        return
                _error_msg = "Could not start the pcap service ! "
            warning(_error_msg +
                    "You probably won't be able to send packets. "
                    "Deactivating unneeded interfaces and restarting Scapy might help. "  # noqa: E501
                    "Check your winpcap and powershell installation, and access rights.")  # noqa: E501
        else:
            # Replace LOOPBACK_INTERFACE
            try:
                scapy.consts.LOOPBACK_INTERFACE = self.dev_from_name(
                    scapy.consts.LOOPBACK_NAME,
                )
            except ValueError:
                pass
        # Support non-windows cards (e.g. Napatech)
        for name, if_data in six.iteritems(conf.cache_iflist):
            guid, ips = if_data
            guid = guid.upper().replace("\\DEVICE\\NPF_", "")
            if guid not in self.data:
                dummy_data = {
                    'name': name,
                    'description': "[Unknown] %s" % name,
                    'win_index': -1,
                    'guid': guid,
                    'invalid': False,
                    'mac': '00:00:00:00:00:00',
                    'ipv4_metric': 0,
                    'ipv6_metric': 0,
                    'ips': ips
                }
                # No KeyError will happen here, as we get it from cache
                self.data[guid] = NetworkInterface(dummy_data)

    def dev_from_name(self, name):
        """Return the first pcap device name for a given Windows
        device name.
        """
        try:
            return next(iface for iface in six.itervalues(self)
                        if (iface.name == name or iface.description == name))
        except (StopIteration, RuntimeError):
            raise ValueError("Unknown network interface %r" % name)

    def dev_from_pcapname(self, pcap_name):
        """Return Windows device name for given pcap device name."""
        try:
            return next(iface for iface in six.itervalues(self)
                        if iface.pcap_name == pcap_name)
        except (StopIteration, RuntimeError):
            raise ValueError("Unknown pypcap network interface %r" % pcap_name)

    def dev_from_index(self, if_index):
        """Return interface name from interface index"""
        try:
            if_index = int(if_index)  # Backward compatibility
            return next(iface for iface in six.itervalues(self)
                        if iface.win_index == if_index)
        except (StopIteration, RuntimeError):
            if str(if_index) == "1":
                # Test if the loopback interface is set up
                if isinstance(scapy.consts.LOOPBACK_INTERFACE, NetworkInterface):  # noqa: E501
                    return scapy.consts.LOOPBACK_INTERFACE
            raise ValueError("Unknown network interface index %r" % if_index)

    def reload(self):
        """Reload interface list"""
        self.restarted_adapter = False
        self.data.clear()
        if conf.use_winpcapy:
            # Reload from Winpcapy
            from scapy.arch.pcapdnet import load_winpcapy
            load_winpcapy()
        self.load()

    def show(self, resolve_mac=True, print_result=True):
        """Print list of available network interfaces in human readable form"""
        res = []
        for iface_name in sorted(self.data):
            dev = self.data[iface_name]
            mac = dev.mac
            if resolve_mac and conf.manufdb:
                mac = conf.manufdb._resolve_MAC(mac)
            res.append((str(dev.win_index),
                        str(dev.description), str(dev.ip), mac))

        res = pretty_list(res, [("INDEX", "IFACE", "IP", "MAC")])
        if print_result:
            print(res)
        else:
            return res

    def __repr__(self):
        return self.show(print_result=False)


IFACES = ifaces = NetworkInterfaceDict()
IFACES.load()


def pcapname(dev):
    """Return pypcap device name for given interface or libdnet/Scapy
    device name.

    """
    if isinstance(dev, NetworkInterface):
        if dev.is_invalid():
            return None
        return dev.pcap_name
    try:
        return IFACES.dev_from_name(dev).pcap_name
    except ValueError:
        if conf.use_pcap:
            # pcap.pcap() will choose a sensible default for sniffing if
            # iface=None
            return None
        raise


def dev_from_pcapname(pcap_name):
    """Return libdnet/Scapy device name for given pypcap device name"""
    return IFACES.dev_from_pcapname(pcap_name)


def dev_from_index(if_index):
    """Return Windows adapter name for given Windows interface index"""
    return IFACES.dev_from_index(if_index)


def show_interfaces(resolve_mac=True):
    """Print list of available network interfaces"""
    return IFACES.show(resolve_mac)


if conf.use_winpcapy:
    _orig_open_pcap = pcapdnet.open_pcap


def open_pcap(iface, *args, **kargs):
    """open_pcap: Windows routine for creating a pcap from an interface.
    This function is also responsible for detecting monitor mode.
    """
    iface_pcap_name = pcapname(iface)
    if not isinstance(iface, NetworkInterface) and iface_pcap_name is not None:
        iface = IFACES.dev_from_name(iface)
    if conf.use_npcap and isinstance(iface, NetworkInterface):
        monitored = iface.ismonitor()
        kw_monitor = kargs.get("monitor", None)
        if kw_monitor is None:
            # The monitor param is not specified. Matching it to current state
            kargs["monitor"] = monitored
        elif kw_monitor is not monitored:
            # The monitor param is specified, and not matching the current
            # interface state
            iface.setmonitor(kw_monitor)
    return _orig_open_pcap(iface_pcap_name, *args, **kargs)


pcapdnet.open_pcap = open_pcap

get_if_raw_hwaddr = pcapdnet.get_if_raw_hwaddr = lambda iface, *args, **kargs: (  # noqa: E501
    ARPHDR_ETHER, mac2str(IFACES.dev_from_pcapname(pcapname(iface)).mac)
)


def _read_routes_c_v1():
    """Retrieve Windows routes through a GetIpForwardTable call.

    This is compatible with XP but won't get IPv6 routes."""
    def _extract_ip(obj):
        return inet_ntop(socket.AF_INET, struct.pack("<I", obj))
    routes = []
    for route in GetIpForwardTable():
        ifIndex = route['ForwardIfIndex']
        dest = route['ForwardDest']
        netmask = route['ForwardMask']
        nexthop = _extract_ip(route['ForwardNextHop'])
        metric = route['ForwardMetric1']
        # Build route
        try:
            iface = dev_from_index(ifIndex)
            if iface.ip == "0.0.0.0":
                continue
        except ValueError:
            continue
        ip = iface.ip
        # RouteMetric + InterfaceMetric
        metric = metric + iface.ipv4_metric
        routes.append((dest, netmask, nexthop, iface, ip, metric))
    return routes


def _read_routes_c(ipv6=False):
    """Retrieve Windows routes through a GetIpForwardTable2 call.

    This is not available on Windows XP !"""
    af = socket.AF_INET6 if ipv6 else socket.AF_INET
    sock_addr_name = 'Ipv6' if ipv6 else 'Ipv4'
    sin_addr_name = 'sin6_addr' if ipv6 else 'sin_addr'
    metric_name = 'ipv6_metric' if ipv6 else 'ipv4_metric'
    ip_len = 16 if ipv6 else 4
    if ipv6:
        lifaddr = in6_getifaddr()
    routes = []

    def _extract_ip_netmask(obj):
        ip = obj[sock_addr_name][sin_addr_name]
        ip = bytes(bytearray(ip['byte']))
        # Extract netmask
        netmask = (ip_len - (len(ip) - len(ip.rstrip(b"\x00")))) * 8
        # Build IP
        ip = inet_ntop(af, ip)
        return ip, netmask

    for route in GetIpForwardTable2(af):
        # Extract data
        ifIndex = route['InterfaceIndex']
        _dest = route['DestinationPrefix']
        dest, netmask = _extract_ip_netmask(_dest['Prefix'])
        nexthop, _ = _extract_ip_netmask(route['NextHop'])
        metric = route['Metric']
        # Build route
        try:
            iface = dev_from_index(ifIndex)
            if iface.ip == "0.0.0.0":
                continue
        except ValueError:
            continue
        ip = iface.ip
        # RouteMetric + InterfaceMetric
        metric = metric + getattr(iface, metric_name)
        if ipv6:
            _append_route6(routes, dest, netmask, nexthop,
                           iface, lifaddr, metric)
        else:
            routes.append((atol(dest), itom(int(netmask)),
                           nexthop, iface, ip, metric))
    return routes


def read_routes():
    routes = []
    try:
        if WINDOWS_XP:
            routes = _read_routes_c_v1()
        else:
            routes = _read_routes_c(False)
    except Exception as e:
        warning("Error building scapy IPv4 routing table : %s", e)
    else:
        if not routes:
            warning("No default IPv4 routes found. Your Windows release may no be supported and you have to enter your routes manually")  # noqa: E501
    return routes


############
#   IPv6   #
############


def in6_getifaddr():
    """
    Returns all IPv6 addresses found on the computer
    """
    ifaddrs = []
    ip6s = get_ips(v6=True)
    for iface in ip6s:
        ips = ip6s[iface]
        for ip in ips:
            scope = in6_getscope(ip)
            ifaddrs.append((ip, scope, iface))
    # Appends Npcap loopback if available
    if conf.use_npcap and scapy.consts.LOOPBACK_INTERFACE:
        ifaddrs.append(("::1", 0, scapy.consts.LOOPBACK_INTERFACE))
    return ifaddrs


def _append_route6(routes, dpref, dp, nh, iface, lifaddr, metric):
    cset = []  # candidate set (possible source addresses)
    if iface.name == scapy.consts.LOOPBACK_NAME:
        if dpref == '::':
            return
        cset = ['::1']
    else:
        devaddrs = (x for x in lifaddr if x[2] == iface)
        cset = construct_source_candidate_set(dpref, dp, devaddrs)
    if not cset:
        return
    # APPEND (DESTINATION, NETMASK, NEXT HOP, IFACE, CANDIDATS, METRIC)
    routes.append((dpref, dp, nh, iface, cset, metric))


def read_routes6():
    routes6 = []
    if WINDOWS_XP:
        return routes6
    try:
        # Interface metrics have been added to powershell in win10+
        routes6 = _read_routes_c(ipv6=True)
    except Exception as e:
        warning("Error building scapy IPv6 routing table : %s", e)
    return routes6


def get_working_if():
    try:
        # return the interface associated with the route with smallest
        # mask (route by default if it exists)
        return min(conf.route.routes, key=lambda x: x[1])[3]
    except ValueError:
        # no route
        return scapy.consts.LOOPBACK_INTERFACE


def _get_valid_guid():
    if scapy.consts.LOOPBACK_INTERFACE:
        return scapy.consts.LOOPBACK_INTERFACE.guid
    else:
        return next((i.guid for i in six.itervalues(IFACES)
                     if not i.is_invalid()), None)


def route_add_loopback(routes=None, ipv6=False, iflist=None):
    """Add a route to 127.0.0.1 and ::1 to simplify unit tests on Windows"""
    if not WINDOWS:
        warning("Not available")
        return
    warning("This will completely mess up the routes. Testing purpose only !")
    # Add only if some adpaters already exist
    if ipv6:
        if not conf.route6.routes:
            return
    else:
        if not conf.route.routes:
            return
    data = {
        'name': scapy.consts.LOOPBACK_NAME,
        'description': "Loopback",
        'win_index': -1,
        'guid': "{0XX00000-X000-0X0X-X00X-00XXXX000XXX}",
        'invalid': True,
        'mac': '00:00:00:00:00:00',
        'ipv4_metric': 0,
        'ipv6_metric': 0,
        'ips': ["127.0.0.1", "::"]
    }
    adapter = NetworkInterface()
    adapter.pcap_name = "\\Device\\NPF_{0XX00000-X000-0X0X-X00X-00XXXX000XXX}"
    adapter.update(data)
    adapter.invalid = False
    adapter.ip = "127.0.0.1"
    if iflist:
        iflist.append(adapter.pcap_name)
        return
    # Remove all LOOPBACK_NAME routes
    for route in list(conf.route.routes):
        iface = route[3]
        if iface.name == scapy.consts.LOOPBACK_NAME:
            conf.route.routes.remove(route)
    # Remove LOOPBACK_NAME interface
    for devname, iface in list(IFACES.items()):
        if iface.name == scapy.consts.LOOPBACK_NAME:
            IFACES.pop(devname)
    # Inject interface
    IFACES["{0XX00000-X000-0X0X-X00X-00XXXX000XXX}"] = adapter
    scapy.consts.LOOPBACK_INTERFACE = adapter
    if isinstance(conf.iface, NetworkInterface):
        if conf.iface.name == scapy.consts.LOOPBACK_NAME:
            conf.iface = adapter
    if isinstance(conf.iface6, NetworkInterface):
        if conf.iface6.name == scapy.consts.LOOPBACK_NAME:
            conf.iface6 = adapter
    conf.netcache.arp_cache["127.0.0.1"] = "ff:ff:ff:ff:ff:ff"
    conf.netcache.in6_neighbor["::1"] = "ff:ff:ff:ff:ff:ff"
    # Build the packed network addresses
    loop_net = struct.unpack("!I", socket.inet_aton("127.0.0.0"))[0]
    loop_mask = struct.unpack("!I", socket.inet_aton("255.0.0.0"))[0]
    # Build the fake routes
    loopback_route = (loop_net, loop_mask, "0.0.0.0", adapter, "127.0.0.1", 1)
    loopback_route6 = ('::1', 128, '::', adapter, ["::1"], 1)
    loopback_route6_custom = ("fe80::", 128, "::", adapter, ["::1"], 1)
    if routes is None:
        # Injection
        conf.route6.routes.append(loopback_route6)
        conf.route6.routes.append(loopback_route6_custom)
        conf.route.routes.append(loopback_route)
        # Flush the caches
        conf.route6.invalidate_cache()
        conf.route.invalidate_cache()
    else:
        if ipv6:
            routes.append(loopback_route6)
            routes.append(loopback_route6_custom)
        else:
            routes.append(loopback_route)


class _NotAvailableSocket(SuperSocket):
    desc = "wpcap.dll missing"

    def __init__(self, *args, **kargs):
        raise RuntimeError(
            "Sniffing and sending packets is not available at layer 2: "
            "winpcap is not installed. You may use conf.L3socket or"
            "conf.L3socket6 to access layer 3"
        )
