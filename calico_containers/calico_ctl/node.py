# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage:
  calicoctl node [--ip=<IP>] [--ip6=<IP6>] [--node-image=<DOCKER_IMAGE_NAME>] [--as=<AS_NUM>] [--log-dir=<LOG_DIR>] [--detach=<DETACH>] [--kubernetes] [--libnetwork]
  calicoctl node stop [--force]
  calicoctl node bgp peer add <PEER_IP> as <AS_NUM>
  calicoctl node bgp peer remove <PEER_IP>
  calicoctl node bgp peer show [--ipv4 | --ipv6]

Description:
  Configure the main calico/node container as well as default BGP information
  for this node.

Options:
  --force                   Stop the node process even if it has active endpoints.
  --node-image=<DOCKER_IMAGE_NAME>    Docker image to use for Calico's per-node
                                      container. Default is calico/node:latest.
                                      Default for Calico with libnetwork is
                                      calico/node-libnetwork:latest.
  --detach=<DETACH>         Set "true" to run Calico service as detached,
                            "false" to run in the foreground. [default: true]
  --log-dir=<LOG_DIR>       The directory for logs [default: /var/log/calico]
  --ip=<IP>                 The local management address to use.
  --ip6=<IP6>               The local IPv6 management address to use.
  --as=<AS_NUM>             The default AS number for this node.
  --ipv4                    Show IPv4 information only.
  --ipv6                    Show IPv6 information only.
  --kubernetes              Download and install the kubernetes plugin.
  --libnetwork              Use the libnetwork plugin.
"""
import sys
import os
import stat
import docker
import socket
import urllib
import signal

from pycalico.datastore_datatypes import IPPool
from pycalico.datastore_datatypes import BGPPeer
from pycalico.datastore import (ETCD_AUTHORITY_ENV,
                                ETCD_AUTHORITY_DEFAULT)
from pycalico.util import get_host_ips
from netaddr import IPAddress
from prettytable import PrettyTable

from connectors import client
from connectors import docker_client
from utils import DOCKER_ORCHESTRATOR_ID
from utils import hostname
from utils import print_paragraph
from utils import get_container_ipv_from_arguments
from utils import validate_ip
from checksystem import check_system

DEFAULT_IPV4_POOL = IPPool("192.168.0.0/16")
DEFAULT_IPV6_POOL = IPPool("fd80:24e2:f998:72d6::/64")

POLICY_ONLY_ENV = "POLICY_ONLY_CALICO"

KUBERNETES_PLUGIN_VERSION = 'v0.1.0'
KUBERNETES_BINARY_URL = 'https://github.com/projectcalico/calico-kubernetes/releases/download/%s/calico_kubernetes' % KUBERNETES_PLUGIN_VERSION
KUBERNETES_PLUGIN_DIR = '/usr/libexec/kubernetes/kubelet-plugins/net/exec/calico/'
KUBERNETES_PLUGIN_DIR_BACKUP = '/etc/kubelet-plugins/calico/'

CALICO_DEFAULT_IMAGE = "calico/node:latest"
LIBNETWORK_IMAGE = 'calico/node-libnetwork:latest'


def validate_arguments(arguments):
    """
    Validate argument values:
        <IP>
        <IP6>
        <PEER_IP>
        <AS_NUM>
        <DETACH>

    Arguments not validated:
        <DOCKER_IMAGE_NAME>
        <LOG_DIR>

    :param arguments: Docopt processed arguments
    """
    # Validate IPs
    ip_ok = arguments.get("--ip") is None or \
            validate_ip(arguments.get("--ip"), 4)
    ip6_ok = arguments.get("--ip6") is None or \
             validate_ip(arguments.get("--ip6"), 6)
    container_ip_ok = arguments.get("<IP>") is None or \
                      validate_ip(arguments["<IP>"], 4) or \
                      validate_ip(arguments["<IP>"], 6)
    peer_ip_ok = arguments.get("<PEER_IP>") is None or \
                 validate_ip(arguments["<PEER_IP>"], 4) or \
                 validate_ip(arguments["<PEER_IP>"], 6)

    asnum_ok = True
    if arguments.get("<AS_NUM>") or arguments.get("--as"):
        try:
            asnum = int(arguments["<AS_NUM>"] or arguments["--as"])
            asnum_ok = 0 <= asnum <= 4294967295
        except ValueError:
            asnum_ok = False

    detach_ok = True
    if arguments.get("<DETACH>") or arguments.get("--detach"):
        detach_ok = arguments.get("--detach") in ["true", "false"]

    # Print error message
    if not ip_ok:
        print "Invalid IPv4 address specified with --ip argument."
    if not ip6_ok:
        print "Invalid IPv6 address specified with --ip6 argument."
    if not container_ip_ok or not peer_ip_ok:
        print "Invalid IP address specified."
    if not asnum_ok:
        print "Invalid AS Number specified."
    if not detach_ok:
        print "Valid values for --detach are 'true' and 'false'"

    # Exit if not valid argument
    if not (ip_ok and ip6_ok and container_ip_ok and peer_ip_ok and asnum_ok
            and detach_ok):
        sys.exit(1)


def node(arguments):
    """
    Main dispatcher for node commands. Calls the corresponding helper function.

    :param arguments: A dictionary of arguments already processed through
    this file's docstring with docopt
    :return: None
    """
    validate_arguments(arguments)

    if arguments.get("bgp"):
        if arguments.get("peer"):
            ip_version = get_container_ipv_from_arguments(arguments)
            if arguments.get("add"):
                node_bgppeer_add(arguments.get("<PEER_IP>"), ip_version,
                                 arguments.get("<AS_NUM>"))
            elif arguments.get("remove"):
                node_bgppeer_remove(arguments.get("<PEER_IP>"), ip_version)
            elif arguments.get("show"):
                if not ip_version:
                    node_bgppeer_show(4)
                    node_bgppeer_show(6)
                else:
                    node_bgppeer_show(ip_version)
    elif arguments.get("stop"):
        node_stop(arguments.get("--force"))
    else:
        assert arguments.get("--detach") in ["true", "false"]
        detach = arguments.get("--detach") == "true"
        node_start(ip=arguments.get("--ip"),
                   node_image=arguments.get('--node-image'),
                   log_dir=arguments.get("--log-dir"),
                   ip6=arguments.get("--ip6"),
                   as_num=arguments.get("--as"),
                   detach=detach,
                   kubernetes=arguments.get("--kubernetes"),
                   libnetwork=arguments.get("--libnetwork"))


def node_start(node_image, log_dir, ip, ip6, as_num, detach, kubernetes,
               libnetwork):
    """
    Create the calico-node container and establish Calico networking on this
    host.

    :param ip:  The IPv4 address of the host.
    :param node_image:  The calico-node image to use.
    :param ip6:  The IPv6 address of the host (or None if not configured)
    :param as_num:  The BGP AS Number to use for this node.  If not specified
    the global default value will be used.
    :param detach: True to run in Docker's "detached" mode, False to run
    attached.
    :param kubernetes: True to install the kubernetes plugin, False otherwise.
    :param libnetwork: True to use the calico/node-libnetwork image as the node
    image, False otherwise.
    :return:  None.
    """
    # Print warnings for any known system issues before continuing
    check_system(fix=False, quit_if_error=False)

    # Ensure log directory exists
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Get IP address of host, if none was specified
    if not ip:
        ips = get_host_ips(exclude=["^docker.*", "^cbr.*"])
        try:
            ip = ips.pop()
        except IndexError:
            print "Couldn't autodetect a management IP address. Please provide" \
                  " an IP by rerunning the command with the --ip=<IP_ADDRESS> flag."
            sys.exit(1)
        else:
            print "No IP provided. Using detected IP: %s" % ip

    # Verify that the chosen IP exists on the current host
    warn_if_unknown_ip(ip, ip6)

    # Warn if this hostname conflicts with an existing host
    warn_if_hostname_conflict(ip)

    # Install kubernetes plugin
    if kubernetes:
        try:
            # Attempt to install to the default kubernetes directory
            install_kubernetes(KUBERNETES_PLUGIN_DIR)
        except OSError:
            # Use the backup directory
            install_kubernetes(KUBERNETES_PLUGIN_DIR_BACKUP)

    # Set up etcd
    ipv4_pools = client.get_ip_pools(4)
    ipv6_pools = client.get_ip_pools(6)

    # Create default pools if required
    if not ipv4_pools:
        client.add_ip_pool(4, DEFAULT_IPV4_POOL)
    if not ipv6_pools:
        client.add_ip_pool(6, DEFAULT_IPV6_POOL)

    client.ensure_global_config()
    client.create_host(hostname, ip, ip6, as_num)

    try:
        docker_client.remove_container("calico-node", force=True)
    except docker.errors.APIError as err:
        if err.response.status_code != 404:
            raise

    # Always try to convert the address(hostname) to an IP. This is a noop if
    # the address is already an IP address.  Note that the format of the authority
    # string has already been validated.
    etcd_authority = os.getenv(ETCD_AUTHORITY_ENV, ETCD_AUTHORITY_DEFAULT)
    etcd_authority_address, etcd_authority_port = etcd_authority.split(':')
    etcd_authority = '%s:%s' % (socket.gethostbyname(etcd_authority_address),
                                etcd_authority_port)

    environment = [
        "HOSTNAME=%s" % hostname,
        "IP=%s" % ip,
        "IP6=%s" % (ip6 or ""),
        "ETCD_AUTHORITY=%s" % etcd_authority,  # etcd host:port
        "FELIX_ETCDADDR=%s" % etcd_authority,  # etcd host:port
        "POLICY_ONLY_CALICO=%s" % os.getenv(POLICY_ONLY_ENV, ""),
    ]

    binds = {
        "/proc":
            {
                "bind": "/proc_host",
                "ro": False
            },
        log_dir:
            {
                "bind": "/var/log/calico",
                "ro": False
            },
        "/run/docker/plugins":
            {
                "bind": "/usr/share/docker/plugins",
                "ro": False
            }
    }

    host_config = docker.utils.create_host_config(
        privileged=True,
        restart_policy={"Name": "Always"},
        network_mode="host",
        binds=binds)

    if not node_image:
        # Use the calico/node-libnetwork image if the libnetwork flag was
        # passed in.  Otherwise, use the default calico/node image.
        node_image = LIBNETWORK_IMAGE if libnetwork else CALICO_DEFAULT_IMAGE

    _find_or_pull_node_image(node_image)
    container = docker_client.create_container(
        node_image,
        name="calico-node",
        detach=True,
        environment=environment,
        host_config=host_config,
        volumes=["/proc_host",
                 "/var/log/calico",
                 "/usr/share/docker/plugins"])
    cid = container["Id"]

    docker_client.start(container)

    print "Calico node is running with id: %s" % cid

    if not detach:
        _attach_and_stream(container)


def node_stop(force):
    if force or len(client.get_endpoints(hostname=hostname,
                                         orchestrator_id=DOCKER_ORCHESTRATOR_ID)) == 0:
        client.remove_host(hostname)
        try:
            docker_client.stop("calico-node")
        except docker.errors.APIError as err:
            if err.response.status_code != 404:
                raise

        print "Node stopped and all configuration removed"
    else:
        print "Current host has active endpoints so can't be stopped." + \
              " Force with --force"


def node_bgppeer_add(ip, version, as_num):
    """
    Add a new BGP peer with the supplied IP address and AS Number to this node.

    :param ip: The address to add
    :param version: 4 or 6
    :param as_num: The peer AS Number.
    :return: None
    """
    address = IPAddress(ip)
    peer = BGPPeer(address, as_num)
    client.add_bgp_peer(version, peer, hostname=hostname)


def node_bgppeer_remove(ip, version):
    """
    Remove a global BGP peer from this node.

    :param ip: The address to use.
    :param version: 4 or 6
    :return: None
    """
    address = IPAddress(ip)
    try:
        client.remove_bgp_peer(version, address, hostname=hostname)
    except KeyError:
        print "%s is not a configured peer for this node." % address
        sys.exit(1)
    else:
        print "BGP peer removed from node configuration"


def node_bgppeer_show(version):
    """
    Print a list of the BGP Peers for this node.
    """
    assert version in (4, 6)
    peers = client.get_bgp_peers(version, hostname=hostname)
    if peers:
        heading = "Node specific IPv%s BGP Peer" % version
        x = PrettyTable([heading, "AS Num"], sortby=heading)
        for peer in peers:
            x.add_row([peer.ip, peer.as_num])
        x.align = "l"
        print x.get_string(sortby=heading)
    else:
        print "No IPv%s BGP Peers defined for this node.\n" % version


def warn_if_unknown_ip(ip, ip6):
    """
    Prints a warning message if the IP addresses are not assigned to interfaces
    on the current host.

    :param ip: IPv4 address which should be present on the host.
    :param ip6: IPv6 address which should be present on the host.
    :return: None
    """
    if ip and ip not in get_host_ips(version=4, exclude=["docker0"]):
        print "WARNING: Could not confirm that the provided IPv4 address is assigned" \
              " to this host."

    if ip6 and ip6 not in get_host_ips(version=6, exclude=["docker0"]):
        print "WARNING: Could not confirm that the provided IPv6 address is assigned" \
              " to this host."


def warn_if_hostname_conflict(ip):
    """
    Prints a warning message if it seems like an existing host is already running
    calico using this hostname.

    :param ip: User-provided IP address to start this node with.
    :return: Nothing
    """
    # If there's already a calico-node container on this host, they're probably
    # just re-running node to update one of the ip addresses, so skip..
    if len(docker_client.containers(filters={'name': 'calico-node'})) == 0:
        # Otherwise, check if another host with the same hostname
        # is already configured
        try:
            current_ipv4, _ = client.get_host_bgp_ips(hostname)
        except KeyError:
            # No other machine has registered configuration under this hostname.
            # This must be a new host with a unique hostname, which is the
            # expected behavior.
            pass
        else:
            if current_ipv4 != "" and current_ipv4 != ip:
                print_paragraph("WARNING: Hostname '%s' is already in use "
                    "with IP address %s. Calico requires each compute host to "
                    "have a unique hostname. If this is your first time "
                    "running 'calicoctl node' on this host, ensure that " \
                    "another host is not already using the " \
                    "same hostname."  % (hostname, ip))


def _find_or_pull_node_image(image_name):
    """
    Check if Docker has a cached copy of an image, and if not, attempt to pull
    it.

    :param image_name: The full name of the image.
    :return: None.
    """
    try:
        _ = docker_client.inspect_image(image_name)
    except docker.errors.APIError as err:
        if err.response.status_code == 404:
            # TODO: Display proper status bar
            print_paragraph("Pulling Docker image %s" % image_name)

            try:
                # Pull the image and then verify that it was succesfully
                # pulled (the pull doesn't raise an exception on failure).
                docker_client.pull(image_name)
                docker_client.inspect_image(image_name)
            except docker.errors.APIError:
                # Unable to download the Docker image.
                print_paragraph("ERROR: Unable to download Docker image.")
                print_paragraph("Please verify that you have network "
                                "connectivity to DockerHub and that, if you "
                                "explicitly specified which calico/node image "
                                "to use, the image name is correct.")
                sys.exit(1)


def _attach_and_stream(container):
    """
    Attach to a container and stream its stdout and stderr output to this
    process's stdout, until the container stops.  If the user presses Ctrl-C or
    the process is killed, also stop the Docker container.

    Used to run the calico-node as a foreground attached service.

    :param container: Docker container to attach to.
    :return: None.
    """

    # Register a SIGTERM handler, so we shut down the container if this
    # process is kill'd.
    def handle_sigterm(sig, frame):
        print "Got SIGTERM"
        docker_client.stop(container)
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_sigterm)

    output = docker_client.attach(container, stream=True)
    try:
        for raw_data in output:
            sys.stdout.write(raw_data)
    except KeyboardInterrupt:
        # mainline.  someone press Ctrl-C.
        print "Stopping Calico node..."
    finally:
        # Could either be this process is being killed, or output generator
        # raises an exception.
        docker_client.stop(container)


def install_kubernetes(kubernetes_plugin_dir):
    """
    Downloads the kubernetes plugin to the specified directory.
    :param kubernetes_plugin_dir: Desired download location for the plugin.
    :return: Nothing
    """
    if not os.path.exists(kubernetes_plugin_dir):
        os.makedirs(kubernetes_plugin_dir)
    kubernetes_binary_path = kubernetes_plugin_dir + 'calico'
    try:
        urllib.urlretrieve(KUBERNETES_BINARY_URL, kubernetes_binary_path)
    except IOError:
        print "ERROR: Couldn't download the Kubernetes plugin"
        sys.exit(1)
    else:
        # Download successful - change permissions to allow execution.
        try:
            st = os.stat(kubernetes_binary_path)
            executable_permissions = st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            os.chmod(kubernetes_binary_path, executable_permissions)
        except OSError:
            print "ERROR: Unable to set permissions on Kubernetes plugin"
            sys.exit(1)
