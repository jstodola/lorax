# treebuilder.py - handle arch-specific tree building stuff using templates
#
# Copyright (C) 2011  Red Hat, Inc.
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
# Author(s):  Will Woods <wwoods@redhat.com>

import logging
logger = logging.getLogger("pylorax.treebuilder")

import os, re, glob
from os.path import join, basename, isdir, getsize
from subprocess import check_call, PIPE
from tempfile import NamedTemporaryFile

from sysutils import joinpaths, cpfile, replace, remove, linktree
from ltmpl import LoraxTemplate
from base import DataHolder
from imgutils import mkcpio

templatemap = {'i386':    'x86.tmpl',
               'x86_64':  'x86.tmpl',
               'ppc':     'ppc.tmpl',
               'ppc64':   'ppc.tmpl',
               'sparc':   'sparc.tmpl',
               'sparc64': 'sparc.tmpl',
               's390':    's390.tmpl',
               's390x':   's390.tmpl',
               }

def findkernels(root="/", kdir="boot"):
    # To find flavors, awk '/BuildKernel/ { print $4 }' kernel.spec
    flavors = ('debug', 'PAE', 'PAEdebug', 'smp', 'xen')
    kre = re.compile(r"vmlinuz-(?P<version>.+?\.(?P<arch>[a-z0-9_]+)"
                     r"(\.(?P<flavor>{0}))?)$".format("|".join(flavors)))
    kernels = []
    for f in os.listdir(joinpaths(root, kdir)):
        match = kre.match(f)
        if match:
            kernel = DataHolder(path=joinpaths(kdir, f))
            kernel.update(match.groupdict()) # sets version, arch, flavor
            kernels.append(kernel)

    # look for associated initrd/initramfs
    for kernel in kernels:
        # NOTE: if both exist, the last one found will win
        for imgname in ("initrd", "initramfs"):
            i = kernel.path.replace("vmlinuz", imgname, 1) + ".img"
            if os.path.exists(joinpaths(root, i)):
                kernel.initrd = DataHolder(path=i)

    return kernels

def _glob(globpat, root="", fatal=True):
    files_found = glob.glob(os.path.join(root, globpat))
    if fatal and not files_found:
        raise IOError, "nothing matching %s" % os.path.join(root, globpat)
    return files_found

def _exists(path, root=""):
    return (len(_glob(path, root, fatal=False)) > 0)

class BaseBuilder(object):
    def __init__(self, product, arch, inroot, outroot, templatedir=None):
        self.arch = arch
        self.product = product
        self.inroot = inroot
        self.outroot = outroot
        self.templatedir = templatedir

    def getdefaults(self):
        return dict(arch=self.arch, product=self.product,
                    inroot=self.inroot, outroot=self.outroot,
                    basearch=self.arch.basearch, libdir=self.arch.libdir)

    def runtemplate(self, tfile, **tvars):
        # get data for template - start with defaults and override from args
        for k,v in self.getdefaults().items():
            tvars.setdefault(k,v) # setdefault won't override existing keys
        logger.info("parsing %s with the following variables", tfile)
        for key, val in tvars.items():
            logger.info("  %s: %s", key, val)
        # set up functions for template
        tvars.setdefault('exists', lambda p: _exists(p, root=tvars['inroot']))
        tvars.setdefault('glob', lambda g: _glob(g, root=tvars['inroot']))
        # parse and run the template
        t = LoraxTemplate(directories=[self.templatedir])
        template = t.parse(tfile, tvars)
        runner = TemplateRunner(template, **tvars)
        logger.info("running template commands")
        runner.run()
        return runner

class RuntimeBuilder(BaseBuilder):
    '''Builds the anaconda runtime image.
    inroot will be the same as outroot, so 'install' == 'copy'.'''
    def __init__(self, product, arch, yum, outroot, templatedir=None):
        BaseBuilder.__init__(self, product, arch, outroot, outroot, templatedir)
        self.yum = yum
        # FIXME pass yum to runner
        self.root = outroot

    def install(self):
        '''Install packages and do initial setup with runtime-install.tmpl'''
        self.runtemplate("runtime-install.tmpl", root=self.outroot)

    def postinstall(self, configdir="/usr/share/lorax/config_files"):
        '''Do some post-install setup work with runtime-postinstall.tmpl'''
        # link configdir into outroot
        configdir_outroot = "tmp/config_files"
        linktree(configdir, join(self.outroot, configdir_outroot))
        self.runtemplate("runtime-postinstall.tmpl", root=self.outroot,
                         configdir=configdir_outroot)

    def cleanup(self):
        '''Remove unneeded packages and files with runtime-cleanup.tmpl'''
        self.runtemplate("runtime-cleanup.tmpl", root=self.outroot,
                         removelocales=self.removelocales)

    @property
    def removelocales(self):
        localedir = join(self.root, "usr/share/locale")
        locales = set([basename(d) for d in _glob("*", localedir) if isdir(d)])
        langtable = join(self.root, "usr/share/anaconda/lang-table")
        keeplocales = set([line.split()[1] for line in open(langtable)])
        return locales.difference(keeplocales)

class TreeBuilder(BaseBuilder):
    '''Builds the arch-specific boot images.
    inroot should be the installtree root (the newly-built runtime dir)'''
    def build(self):
        template = templatemap[self.arch.basearch]
        runner = self.runtemplate(template, kernels=self.kernels)
        self.treeinfo_data = runner.results.treeinfo
        self.implantisomd5()

    @property
    def kernels(self):
        return findkernels(root=self.inroot)

    def rebuild_initrds(self, add_args=[], backup=""):
        '''Rebuild all the initrds in the tree. If backup is specified, each
        initrd will be renamed with backup as a suffix before rebuilding.
        If backup is empty, the existing initrd files will be overwritten.'''
        dracut = ["/sbin/dracut", "--nomdadmconf", "--nolvmconf"] + add_args
        if not backup:
            dracut.append("--force")
        for kernel in self.kernels:
            logger.info("rebuilding %s", kernel.initrd.path)
            if backup:
                initrd = joinpaths(self.inroot, kernel.initrd.path)
                os.rename(initrd, initrd + backup)
            check_call(["chroot", self.inroot] + \
                       dracut + [kernel.initrd.path, kernel.version])

    def initrd_append(self, rootdir):
        '''Place the given files into a cpio archive and append that archive
        to the initrds.'''
        cpio = NamedTemporaryFile(prefix="lorax.") # XXX workdir?
        mkcpio(rootdir, cpio.name, compression=None)
        for kernel in self.kernels:
            cpio.seek(0)
            initrd_path = joinpaths(self.inroot, kernel.initrd.path)
            with open(initrd_path, "ab") as initrd:
                logger.info("%s size before appending: %i",
                    kernel.initrd.path, getsize(initrd.name))
                initrd.write(cpio.read())

    def implantisomd5(self):
        for section, data in self.treeinfo_data.items():
            if 'boot.iso' in data:
                iso = joinpaths(self.outroot, data['boot.iso'])
                check_call(["implantisomd5", iso])


# note: "install", "replace", "exists" allow globs
# "install" and "exist" assume their first argument is in inroot
# everything else operates on outroot
# "mkdir", "treeinfo", "runcmd", "remove", "replace" will take multiple args

# TODO: to replace installtree:
#       module modname [modname...]
#       get yum object somehow

class TemplateRunner(object):
    commands = ('install', 'mkdir', 'replace', 'append', 'treeinfo',
                'installkernel', 'installinitrd', 'hardlink', 'symlink',
                'copy', 'copyif', 'move', 'moveif', 'remove', 'chmod',
                'runcmd', 'log')

    def __init__(self, parsed_template, inroot=None, outroot=None,
                 fatalerrors=False, **kwargs):
        self.template = parsed_template
        self.inroot = inroot
        self.outroot = outroot
        self.fatalerrors = fatalerrors
        self.kwargs = kwargs

        self.results = DataHolder(treeinfo=dict()) # just treeinfo right now
        self.exists = lambda p: _exists(p, root=inroot)

    def _out(self, path):
        return joinpaths(self.outroot, path)
    def _in(self, path):
        return joinpaths(self.inroot, path)

    def run(self):
        for (num, line) in enumerate(self.template,1):
            logger.debug("template line %i: %s", num, line)
            (cmd, args) = (line[0], line[1:])
            try:
                if cmd not in self.commands:
                    raise ValueError, "unknown command %s" % cmd
                # grab the method named in cmd and pass it the given arguments
                f = getattr(self, cmd)
                f(*args)
            except Exception as e:
                logger.error("template command error: %s", str(line))
                if self.fatalerrors:
                    raise
                logger.error(str(e))

    def install(self, srcglob, dest):
        for src in _glob(srcglob, root=self.inroot):
            cpfile(src, self._out(dest))

    def mkdir(self, *dirs):
        for d in dirs:
            d = self._out(d)
            if not isdir(d):
                os.makedirs(d)

    def replace(self, pat, repl, *fileglobs):
        for g in fileglobs:
            for f in _glob(g, root=self.outroot):
                replace(f, pat, repl)

    def append(self, filename, data):
        with open(self._out(filename), "a") as fobj:
            fobj.write(data+"\n")

    def treeinfo(self, section, key, *valuetoks):
        if section not in self.results.treeinfo:
            self.results.treeinfo[section] = dict()
        self.results.treeinfo[section][key] = " ".join(valuetoks)

    def installkernel(self, section, src, dest):
        self.install(src, dest)
        self.treeinfo(section, "kernel", dest)

    def installinitrd(self, section, src, dest):
        self.install(src, dest)
        self.treeinfo(section, "initrd", dest)

    def hardlink(self, src, dest):
        if isdir(self._out(dest)):
            dest = join(dest, basename(src))
        os.link(self._out(src), self._out(dest))

    def symlink(self, target, dest):
        os.symlink(target, self._out(dest))

    def copy(self, src, dest):
        cpfile(self._out(src), self._out(dest))

    def copyif(self, src, dest):
        if self.exists(src):
            self.copy(src, dest)
            return True

    def move(self, src, dest):
        self.copy(src, dest)
        self.remove(src)

    def moveif(self, src, dest):
        if self.copyif(src, dest):
            self.remove(src)
            return True

    def remove(self, *targets):
        for t in targets:
            remove(self._out(t))

    def chmod(self, target, mode):
        os.chmod(self._out(target), int(mode,8))

    def gconfset(self, path, keytype, value, outfile=None):
        if outfile is None:
            outfile = self._out("etc/gconf/gconf.xml.defaults")
        check_call(["gconftool-2", "--direct",
                    "--config-source=xml:readwrite:%s" % outfile,
                    "--set", "--type", keytype, path, value])

    def log(self, msg):
        logger.info(msg)

    def runcmd(self, *cmdlist):
        '''Note that we need full paths for everything here'''
        chdir = lambda: None
        cmd = cmdlist
        if cmd[0].startswith("chdir="):
            dirname = cmd[0].split('=',1)[1]
            chdir = lambda: os.chdir(dirname)
            cmd = cmd[1:]
        check_call(cmd, preexec_fn=chdir)

    def module(self, *modnames):
        for mod in modnames:
            # XXX TODO surely this code is elsewhere?
            # expand groups
            # resolve deps
            # get firmware
            pass

    def installpkg(self, *pkgs):
        for p in pkgs:
            self.yum.install(pattern=p)

    def removepkg(self, *pkgs):
        for p in pkgs:
            self.yum.remove(pattern=p)

    def run_pkg_transaction(self):
        self.yum.buildTransaction()
        self.yum.repos.setProgressBar(LoraxDownloadCallback())
        self.yum.processTransaction(callback=LoraxTransactionCallback(),
                                    rpmDisplay=LoraxRpmCallback())
        self.yum.closeRpmDB()

    def removefrom(self, pkg, *globs):
        globs_re = re.compile("|".join([fnmatch.translate(g) for g in globs]))
        pkg_files = []
        for pkgobj in self.yum.doPackageLists(pkgnarrow="installed", patterns=[pkg]):
            pkg_files += pkgobj.filelist
        remove = filter(globs_re.match(pkg_files))
        logger.debug("removing %i files from %s", len(remove), pkg)
        self.remove(*remove)
