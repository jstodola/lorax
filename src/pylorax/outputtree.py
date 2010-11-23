#
# outputtree.py
#
# Copyright (C) 2010  Red Hat, Inc.
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Red Hat Author(s):  Martin Gracik <mgracik@redhat.com>
#

import logging
logger = logging.getLogger("pylorax.outputtree")

import sys
import os
import ConfigParser
import tempfile
import shutil
import gzip
import shlex
import fnmatch
import re
import itertools
import glob
import time
import datetime
import itertools
import subprocess
import operator
import math

from collections import namedtuple

from base import BaseLoraxClass
import output

import yum
import yumhelper
import ltmpl

import constants
from sysutils import *


class LoraxOutputTree(BaseLoraxClass):

    def __init__(self, root, installtree, product, version):
        BaseLoraxClass.__init__(self)
        self.root = root
        self.installtree = installtree

        self.product = product
        self.version = version

    def prepare(self):
        imgdir = joinpaths(self.root, "images")
        os.makedirs(imgdir)
        logger.debug("created directory {0}".format(imgdir))

        pxebootdir = joinpaths(self.root, "images/pxeboot")
        os.makedirs(pxebootdir)
        logger.debug("created directory {0}".format(pxebootdir))

        isolinuxdir = joinpaths(self.root, "isolinux")
        os.makedirs(isolinuxdir)
        logger.debug("created directory {0}".format(isolinuxdir))

        efibootdir = joinpaths(self.root, "EFI/BOOT")
        os.makedirs(efibootdir)
        logger.debug("created directory {0}".format(efibootdir))

        self.imgdir = imgdir
        self.pxebootdir = pxebootdir
        self.isolinuxdir = isolinuxdir
        self.efibootdir = efibootdir

    def get_kernels(self, kernels):
        # get the main kernel
        self.main_kernel = kernels.pop(0)

        # copy kernel to isolinuxdir
        shutil.copy2(self.main_kernel.fpath, self.isolinuxdir)

        # create kernel hard link in pxebootdir
        source = joinpaths(self.isolinuxdir, self.main_kernel.fname)
        link_name = joinpaths(self.pxebootdir, self.main_kernel.fname)
        os.link(source, link_name)

        # other kernels
        for kernel in kernels:
            shutil.copy2(kernel.fpath, self.pxebootdir)

    def get_isolinux(self):
        isolinuxbin = joinpaths(self.installtree.root,
                                "usr/share/syslinux/isolinux.bin")
        syslinuxcfg = joinpaths(self.installtree.root,
                                "usr/share/anaconda/boot/syslinux.cfg")

        # copy isolinux.bin
        shutil.copy2(isolinuxbin, self.isolinuxdir)

        # copy syslinux.cfg
        isolinuxcfg = joinpaths(self.isolinuxdir, "isolinux.cfg")
        shutil.copy2(syslinuxcfg, isolinuxcfg)

        # set product and version in isolinux.cfg
        replace(isolinuxcfg, r"@PRODUCT@", self.product)
        replace(isolinuxcfg, r"@VERSION@", self.version)

        # copy memtest
        memtest = joinpaths(self.installtree.root,
                            "boot/memtest*")

        for fname in glob.glob(memtest):
            shutil.copy2(fname, joinpaths(self.isolinuxdir, "memtest"))

            text = """label memtest86
  menu label ^Memory test
  kernel memtest
  append -

"""

            with open(isolinuxcfg, "a") as fobj:
                fobj.write(text)

            break

        # get splash
        vesasplash = joinpaths(self.installtree.root, "usr/share/anaconda",
                               "boot/syslinux-vesa-splash.jpg")

        vesamenu = joinpaths(self.installtree.root,
                             "usr/share/syslinux/vesamenu.c32")

        splashtolss = joinpaths(self.installtree.root,
                                "usr/share/anaconda/splashtolss.sh")

        syslinuxsplash = joinpaths(self.installtree.root, "usr/share/anaconda",
                                   "boot/syslinux-splash.jpg")

        splashlss = joinpaths(self.installtree.root, "usr/share/anaconda",
                              "boot/splash.lss")

        if os.path.isfile(vesasplash):
            shutil.copy2(vesasplash, joinpaths(self.isolinuxdir, "splash.jpg"))
            shutil.copy2(vesamenu, self.isolinuxdir)
            replace(isolinuxcfg, r"default linux", "default vesamenu.c32")
            replace(isolinuxcfg, r"prompt 1", "#prompt 1")
        elif os.path.isfile(splashtolss):
            cmd = [splashtolss, syslinuxsplash, splashlss]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            rc = p.wait()
            if not rc == 0:
                logger.error("failed to create splash.lss")
                sys.exit(1)

            if os.path.isfile(splashlss):
                shutil.copy2(splashlss, self.isolinuxdir)

    def get_msg_files(self):
        msgfiles = joinpaths(self.installtree.root,
                             "usr/share/anaconda/boot/*.msg")

        for fname in glob.glob(msgfiles):
            shutil.copy2(fname, self.isolinuxdir)
            path = joinpaths(self.isolinuxdir, os.path.basename(fname))
            replace(path, r"@VERSION@", self.version)

    def get_grub_conf(self):
        grubconf = joinpaths(self.installtree.root,
                             "usr/share/anaconda/boot/grub.conf")

        shutil.copy2(grubconf, self.isolinuxdir)

        grubconf = joinpaths(self.isolinuxdir, "grub.conf")
        replace(grubconf, r"@PRODUCT@", self.product)
        replace(grubconf, r"@VERSION@", self.version)
