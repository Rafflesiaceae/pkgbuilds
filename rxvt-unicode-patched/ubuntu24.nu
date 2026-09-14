#!/usr/bin/env nu
# Build rxvt-unicode 9.30 with custom patches as a .deb on Ubuntu 24.04.
# Run from the rxvt-unicode-patched/ directory (where the patch files live).
# Usage: nu ubuntu24.nu [--install]

use ../.nupkg.ubuntu.nu *

const VERSION = "9.30"
const PKG_NAME = "rxvt-unicode-patched"
const SOURCE_URL = $"http://dist.schmorp.de/rxvt-unicode/Attic/rxvt-unicode-($VERSION).tar.bz2"

# Ordered list of patches to apply; missing files are skipped with a warning.
const PATCHES = [
    "font-width-fix.patch"
    "line-spacing-fix.patch"
    "unicode-fix.patch"
    "disable-cursor-blink.patch"
    "fix-smart-resize-with-x11-frame-borders.patch"
    "rxvt-unicode-9.21-Fix-hard-coded-wrong-path-to-xsubpp.patch"
    "rxvt-unicode-0001-Prefer-XDG_RUNTIME_DIR-over-the-HOME.patch"
]

def main [
    --install (-i)  # Install the produced .deb after building
] {
    # Resolve absolute paths before any cd so they stay valid throughout.
    let start_dir = (pwd)
    let build_dir = ($start_dir | path join "build-urxvt")
    let src_dir   = ($build_dir | path join $"rxvt-unicode-($VERSION)")
    let tarball   = ($build_dir | path join $"rxvt-unicode-($VERSION).tar.bz2")

    ensure-dir $build_dir
    download $SOURCE_URL $tarball

    # Always re-extract for a reproducible, patch-clean source tree.
    if ($src_dir | path exists) {
        rm --recursive --force $src_dir
    }
    run-cmd ["tar", "-xjf", $tarball, "-C", $build_dir]

    print $"\nApplying patches from ($start_dir)..."
    for p in $PATCHES {
        let patch_file = ($start_dir | path join $p)
        if ($patch_file | path exists) {
            print $"  Applying ($p)..."
            # -d changes into src_dir before applying, so strip count 0 is correct.
            run-cmd ["patch", "-p0", "-d", $src_dir, "--input", $patch_file]
        } else {
            print $"  Warning: ($p) not found, skipping."
        }
    }

    # Prepend PIE flags to any caller-supplied CFLAGS/CXXFLAGS.
    let base_cflags   = (try { $env.CFLAGS   } catch { "" })
    let base_cxxflags = (try { $env.CXXFLAGS } catch { "" })
    $env.CFLAGS   = $"-fPIE -pie ($base_cflags)"
    $env.CXXFLAGS = $"-fPIE -pie -std=c++11 ($base_cxxflags)"

    # configure, patch, make, and checkinstall must all run from the source tree.
    let old_pwd = (pwd)
    cd $src_dir

    run-cmd [
        "./configure",
        "--prefix=/usr",
        "--libdir=/usr/lib",
        "--enable-256-color",
        "--enable-combining",
        "--enable-fading",
        "--enable-font-styles",
        "--enable-iso14755",
        "--enable-keepscrolling",
        "--enable-mousewheel",
        "--enable-next-scroll",
        "--enable-perl",
        "--enable-pointer-blank",
        "--enable-rxvt-scroll",
        "--enable-selectionscrolling",
        "--enable-slipwheeling",
        "--disable-smart-resize",
        "--enable-startup-notification",
        "--enable-transparency",
        "--enable-unicode3",
        "--enable-xft",
        "--enable-xim",
        "--enable-xterm-scroll",
    ]

    # doc/Makefile unconditionally invokes tic to install terminfo entries into
    # the system, which fails in user builds.  Comment out those lines.
    let doc_makefile = ($src_dir | path join "doc" "Makefile")
    (
        open --raw $doc_makefile
        | lines
        | each {|l| if ($l =~ '^[[:blank:]]*/usr/bin/tic') { $"#($l)" } else { $l }}
        | str join "\n"
    ) | save --force $doc_makefile

    let jobs = (capture ["nproc"])
    run-cmd ["make", $"-j($jobs)"]

    # The Makefile's install target tries to create /usr/lib/urxvt/perl but
    # does not create the parent urxvt/ directory first; pre-create it.
    run-cmd ["sudo", "mkdir", "-p", "/usr/lib/urxvt/perl"]

    let maintainer = (capture ["whoami"])
    run-cmd [
        "sudo", "checkinstall", "-y",
        "--pkgname",    $PKG_NAME,
        "--pkgversion", $VERSION,
        "--pkgrelease", "1",
        "--pkggroup",   "x11",
        "--maintainer", $maintainer,
        "--provides",   "rxvt-unicode",
        "--requires",   "libxft2,libperl5.38,libstartup-notification0,libnsl2,libptytty0",
        "--nodoc",
        "make", "install"
    ]

    cd $old_pwd

    let debs = (glob ($src_dir | path join "*.deb"))
    if $install {
        apt-install-debs $debs
    } else {
        print "--------------------------------------------------"
        print $"Package built!  Install with: sudo dpkg -i ($debs | str join ' ')"
    }
}
