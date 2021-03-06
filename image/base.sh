#!/usr/bin/env bash
set -e
source /build/buildconfig
set -x

# Upgrade all packages.
apt-get update
apt-get dist-upgrade -y --no-install-recommends

# Determine the list of packages required for the base image.
dpkg -l | grep ^ii | sed 's_  _\t_g' | cut -f 2 >/tmp/base.txt

# Install curl, needed below for manual BIRD install.
$minimal_apt_get_install curl

# Find the list of packages just installed - these can be deleted later.
grep -Fxvf  /tmp/base.txt <(dpkg -l | grep ^ii | sed 's_  _\t_g' | cut \
-f 2) >/tmp/add-apt.txt

# Install packages that should not be removed in the cleanup processing.
# - packages required by felix
# - pip (which includes various setuptools package discovery).
#apt-get install -qy \
$minimal_apt_get_install \
        iptables \
        ipset \
        conntrack \
        net-tools \
        python-pip=1.5.4-1

# Copy patched BIRD daemon with tunnel support.
curl -L https://github.com/projectcalico/calico-bird/releases/download/v0.1.0/bird -o /usr/sbin/bird && \
    chmod +x /usr/sbin/bird
curl -L https://github.com/projectcalico/calico-bird/releases/download/v0.1.0/bird6 -o /usr/sbin/bird6 && \
    chmod +x /usr/sbin/bird6
curl -L https://github.com/projectcalico/calico-bird/releases/download/v0.1.0/birdcl -o /usr/sbin/birdcl && \
    chmod +x /usr/sbin/birdcl
