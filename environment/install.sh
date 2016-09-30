#!/bin/bash -e

cd /app
npm install --production

# remove unused packages
devpackages=$(dpkg -l| grep '\-dev' | cut -d ' ' -f 3)

apt-get remove --purge -y \
    git bzr mercurial openssh-client subversion \
    autoconf \
    automake \
    bzip2 \
    file \
    g++ \
    gcc \
    make \
    patch \
    xz-utils \
    gir1.2-glib-2.0 \
    gir1.2-freedesktop \
    gir1.2-gdkpixbuf-2.0 $devpackages

apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*
rm -rf /var/lib/dpkg/*

# remove useless documentation
rm -rf /usr/share/doc/*

# remove build trash
rm -rf /root/.npm
rm -rf /root/.node-gyp
rm -rf /root/.ssh
rm -rf /tmp/*
rm -rf /var/tmp/*

