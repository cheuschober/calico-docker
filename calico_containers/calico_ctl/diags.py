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
  calicoctl diags [--log-dir=<LOG_DIR>]

Description:
  Save diagnostic information

Options:
  --log-dir=<LOG_DIR>  The directory for logs [default: /var/log/calico]
"""
import sys
import sh
import os
from datetime import datetime
import tarfile
import socket
import tempfile
import subprocess

from etcd import EtcdException
from pycalico.datastore import DatastoreClient
from shutil import copytree, ignore_patterns

from utils import print_paragraph


def diags(arguments):
    """
    Main dispatcher for diags commands. Calls the corresponding helper function.

    :param arguments: A dictionary of arguments already processed through
    this file's docstring with docopt
    :return: None
    """
    print("Collecting diags")
    save_diags(arguments["--log-dir"])
    sys.exit(0)


def save_diags(log_dir):
    # Create temp directory
    temp_dir = tempfile.mkdtemp()
    temp_diags_dir = os.path.join(temp_dir, 'diagnostics')
    os.mkdir(temp_diags_dir)
    print("Using temp dir: %s" % temp_dir)

    # Write date to file
    with open(os.path.join(temp_diags_dir, 'date'), 'w') as f:
        f.write("DATE=%s" % datetime.strftime(datetime.today(),
                                              "%Y-%m-%d_%H-%M-%S"))

    # Write hostname to file
    with open(os.path.join(temp_diags_dir, 'hostname'), 'w') as f:
        f.write("%s" % socket.gethostname())

    # Write netstat output to file
    with open(os.path.join(temp_diags_dir, 'netstat'), 'w') as f:
        try:
            print("Dumping netstat output")
            netstat = sh.Command._create("netstat")

            f.writelines(netstat(
                # Display all sockets (default: connected)
                all=True,
                # Don't resolve names
                numeric=True))

        except sh.CommandNotFound as e:
            print "Missing command: %s" % e.message

    # Write routes
    print("Dumping routes")
    with open(os.path.join(temp_diags_dir, 'route'), 'w') as f:
        try:
            route = sh.Command._create("route")
            f.write("route --numeric\n")
            f.writelines(route(numeric=True))
            f.write('\n')
        except sh.CommandNotFound as e:
            print "Missing command: %s" % e.message

        try:
            ip = sh.Command._create("ip")
            f.write("ip route\n")
            f.writelines(ip("route"))
            f.write('\n')

            f.write("ip -6 route\n")
            f.writelines(ip("-6", "route"))
            f.write('\n')
        except sh.CommandNotFound as e:
            print "Missing command: %s" % e.message

    # Dump iptables
    with open(os.path.join(temp_diags_dir, 'iptables'), 'w') as f:
        try:
            iptables_save = sh.Command._create("iptables-save")
            print("Dumping iptables")
            f.writelines(iptables_save())
        except sh.CommandNotFound as e:
            print "Missing command: %s" % e.message

    # Dump ipset list
    # TODO: ipset might not be installed on the host. But we don't want to
    # gather the diags in the container because it might not be running...
    with open(os.path.join(temp_diags_dir, 'ipset'), 'w') as f:
        try:
            ipset = sh.Command._create("ipset")
            print("Dumping ipset")
            f.writelines(ipset("list"))
        except sh.CommandNotFound as e:
            print "Missing command: %s" % e.message
        except sh.ErrorReturnCode_1 as e:
            print "Error running ipset. Maybe you need to run as root."

    # Ask Felix to dump stats to its log file - ignore errors as the
    # calico-node might not be running
    subprocess.call(["docker", "exec", "calico-node",
                     "pkill", "-SIGUSR1", "felix"])

    if os.path.isdir(log_dir):
        print("Copying Calico logs")
        # Skip the lock files as they can only be copied by root.
        copytree(log_dir, os.path.join(temp_diags_dir, "logs"),
                 ignore=ignore_patterns('lock'))
    else:
        print('No logs found in %s; skipping log copying' % log_dir)

    print("Dumping datastore")
    # TODO: May want to move this into datastore.py as a dump-calico function
    try:
        datastore_client = DatastoreClient()
        datastore_data = datastore_client.etcd_client.read("/calico",
                                                           recursive=True)
        with open(os.path.join(temp_diags_dir, 'etcd_calico'), 'w') as f:
            f.write("dir?, key, value\n")
            # TODO: python-etcd bug: Leaves show up twice in get_subtree().
            for child in datastore_data.get_subtree():
                if child.dir:
                    f.write("DIR,  %s,\n" % child.key)
                else:
                    f.write("FILE, %s, %s\n" % (child.key, child.value))
    except EtcdException:
        print "Unable to dump etcd datastore"

    # Create tar.
    tar_filename = datetime.strftime(datetime.today(),
                                     "diags-%d%m%y_%H%M%S.tar.gz")
    full_tar_path = os.path.join(temp_dir, tar_filename)
    with tarfile.open(full_tar_path, "w:gz") as tar:
        # pass in arcname, otherwise zip contains layers of subfolders
        tar.add(temp_dir, arcname="")

    print("\nDiags saved to %s\n" % (full_tar_path))
    print_paragraph("If required, you can upload the diagnostics bundle to a "
                    "file sharing service such as transfer.sh using curl or "
                    "similar.  For example:")
    print("  curl --upload-file %s https://transfer.sh/%s" %
             (full_tar_path, os.path.basename(full_tar_path)))
