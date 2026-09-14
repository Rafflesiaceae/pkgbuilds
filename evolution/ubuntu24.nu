#!/usr/bin/env nu
#
# Build and install Evolution 3.60.2 + Evolution Data Server 3.60.2
# + Evolution-EWS 3.60.2 as native .deb packages on Ubuntu 24.04 (Noble).
#
# The packages use the normal Debian/Ubuntu package names and install prefix
# (/usr, /etc), so they replace the distro Evolution packages rather than
# creating a parallel /usr/local installation.
#
# Build packaging base (Debian 3.56, intentionally chosen because its file/package
# layout is much closer to GNOME 3.60 than Noble's older 3.52 packaging):
#   evolution-data-server 3.56.2-8 (Debian)
#   evolution             3.56.2-10 (Debian)
#   evolution-ews         3.56.2-3 (Debian)
#
# Upstream source for all three components is GNOME 3.60.2.
#
# IMPORTANT:
#   Evolution 3.60 bumps two EDS SONAMEs:
#     libcamel-1.2.so.64          -> libcamel-1.2.so.67
#     libebook-contacts-1.2.so.4  -> libebook-contacts-1.2.so.5
#
#   Therefore this script creates NEW runtime packages
#     libcamel-1.2-67t64
#     libebook-contacts-1.2-5t64
#   and intentionally lets Noble's old ABI packages coexist. Removing the old
#   ABI packages could break Noble applications such as GNOME Contacts.
#
# Output:
#   ./evolution-3.60.2-deb-build/debs/
#
# NOTE: --install is accepted for interface consistency but is a no-op here;
# each build stage must install its output before the next stage can compile.
#

use ../.nupkg.ubuntu.nu *

const UPSTREAM_VERSION = "3.60.2"
const LOCAL_VERSION = "3.60.2-0ubuntu24.04.1+local1"

const EDS_PACKAGING_VERSION = "3.56.2-8"
const EVO_PACKAGING_VERSION = "3.56.2-10"
const EWS_PACKAGING_VERSION = "3.56.2-3"

const EDS_SHA256 = "2084dbdac396371b365d504c1ff45866ba8dca2f1252e5da1d3d9c33abdc1286"
const EVO_SHA256 = "218a49fa5068155dbdbd35a30d10a2f237c72465fb1d0e0aa78889f039c2ed8f"
const EWS_SHA256 = "940205818f6988411457ddf5804d210f3515c26e33415debd8fbb2731b1daf2b"

# Checksums published on packages.debian.org for the Debian packaging tarballs.
const EDS_DEBIAN_MD5 = "ac50a68cb8f9a0f479a82598f59df606"
const EVO_DEBIAN_MD5 = "323ebf28e4a611395b89c938d468b476"
const EWS_DEBIAN_MD5 = "3b9d49ca08467b1ec2ae259932805554"

def replace-in-file [file: string, old: string, new: string] {
    if ($file | path exists) {
        let before = (open --raw $file)
        if ($before | str contains $old) {
            ($before | str replace --all $old $new) | save --force $file
        }
    }
}

def clear-patch-series [src: string] {
    # Clear the patch series so debhelper does not try to apply Debian's
    # 3.56-targeted patches against the 3.60 source tree.
    let series = ($src | path join "debian" "patches" "series")
    if ($series | path exists) {
        "" | save --force $series
    }
}

def fix-dh-gnome-clean-for-noble [rules: string] {
    # Ubuntu 24.04's gnome-pkg-tools still makes dh_gnome_clean try to
    # regenerate debian/control from debian/control.in. Modern Evolution
    # packaging no longer ships control.in, so use the helper's supported
    # --no-control mode while keeping the rest of the GNOME debhelper sequence.
    let before = (open --raw $rules)

    if ($before | str contains "override_dh_gnome_clean:") {
        let fixed = (
            $before
            | str replace --all "\tdh_gnome_clean\n" "\tdh_gnome_clean --no-control\n"
            | str replace --all "dh_gnome_clean --no-control --no-control" "dh_gnome_clean --no-control"
        )
        $fixed | save --force $rules
    } else {
        let suffix = ("\n# Ubuntu 24.04 gnome-pkg-tools compatibility\n" +
            "override_dh_gnome_clean:\n" +
            "\tdh_gnome_clean --no-control\n")
        ($before + $suffix) | save --force $rules
    }
}

def adapt-eds-build-deps-for-noble [control: string] {
    # Debian 3.56 uses the newer cross-build-friendly gir1.2-*-dev dependency
    # names. Noble has the same development data, but some of those virtual
    # package names are not satisfiable from Noble's archive metadata.
    #
    # Translate them to the concrete Noble development packages/providers.
    # This changes only Build-Depends metadata; the compiled source and binary
    # package contents remain upstream Evolution Data Server 3.60.2.
    replace-in-file $control "gir1.2-gio-2.0-dev" "gir1.2-glib-2.0-dev"
    replace-in-file $control "gir1.2-gobject-2.0-dev" "gir1.2-glib-2.0-dev"
    replace-in-file $control "gir1.2-gtk-3.0-dev" "libgtk-3-dev"
    replace-in-file $control "gir1.2-gtk-4.0-dev" "libgtk-4-dev"
    replace-in-file $control "gir1.2-icalglib-3.0-dev" "libical-dev"
    replace-in-file $control "gir1.2-json-1.0-dev" "libjson-glib-dev"
    replace-in-file $control "gir1.2-libxml2-2.0-dev" "libgirepository1.0-dev"
    replace-in-file $control "gir1.2-soup-3.0-dev" "libsoup-3.0-dev"
}

def rename-package-helper-files [debian_dir: string, old_name: string, new_name: string] {
    let pattern = ($debian_dir | path join $"($old_name).*")
    for old_path in (glob $pattern) {
        let old_base = ($old_path | path basename)
        let new_base = ($old_base | str replace $old_name $new_name)
        let new_path = ($debian_dir | path join $new_base)
        mv $old_path $new_path
    }
}

def remove-if-present [pattern: string] {
    for file in (glob $pattern) {
        rm --force $file
    }
}

def reset-component-dir [component_root: string, source_dir: string] {
    if ($component_root | path exists) {
        rm --recursive --force $component_root
    }
    mkdir $component_root
    mkdir $source_dir
}

def unpack-source [upstream_tar: string, debian_tar: string, source_dir: string] {
    run-cmd [
        "tar", "-xf", $upstream_tar,
        "-C", $source_dir,
        "--strip-components=1"
    ]
    # Debian packaging tarballs contain debian/ at their root.
    run-cmd ["tar", "-xf", $debian_tar, "-C", $source_dir]
}

def add-local-changelog [source_dir: string, component: string] {
    let old_pwd = (pwd)
    cd $source_dir

    run-cmd [
        "dch",
        "--newversion", $LOCAL_VERSION,
        "--distribution", "noble",
        "--force-distribution",
        $"Local Ubuntu 24.04 build of upstream ($component) ($UPSTREAM_VERSION)."
    ]

    cd $old_pwd
}

def install-build-deps [source_dir: string] {
    let old_pwd = (pwd)
    cd $source_dir

    print $"
==> mk-build-deps --install --remove --root-cmd sudo --tool 'apt-get -y --no-install-recommends' debian/control"
    ^mk-build-deps --install --remove --root-cmd sudo --tool "apt-get -y --no-install-recommends" debian/control
    let mk_code = $env.LAST_EXIT_CODE

    if $mk_code != 0 {
        print --stderr "
==> mk-build-deps failed. Remaining unsatisfied Build-Depends:"
        let check = (do { ^dpkg-checkbuilddeps debian/control } | complete)

        if not ($check.stdout | str trim | is-empty) {
            print --stderr $check.stdout
        }
        if not ($check.stderr | str trim | is-empty) {
            print --stderr $check.stderr
        }

        cd $old_pwd
        fail $"mk-build-deps failed with exit code ($mk_code)"
    }

    cd $old_pwd
}

def build-component [source_dir: string, jobs: string] {
    let old_pwd = (pwd)
    cd $source_dir

    run-cmd [
        "dpkg-buildpackage",
        "-b",
        "-us",
        "-uc",
        $"-j($jobs)"
    ]

    cd $old_pwd
}

def component-debs [component_root: string] {
    glob ($component_root | path join "*.deb")
}

def install-component-debs [component_root: string] {
    # Skip docs/tests during the staged host install; they are still copied to
    # the final output directory. Dev/GIR packages are installed because the
    # next component needs them at build time.
    let debs = (
        component-debs $component_root
        | where {|p| let base = ($p | path basename); (not ($base | str contains "-dbgsym_")) and (not ($base | str contains "-doc_")) and (not ($base | str contains "-tests_")) }
    )

    if ($debs | is-empty) {
        fail $"No .deb packages were produced in ($component_root)"
    }

    let argv = ["sudo", "apt-get", "install", "-y", ...$debs]
    run-cmd $argv
}

def copy-component-debs [component_root: string, out_dir: string] {
    for deb in (component-debs $component_root) {
        cp $deb $out_dir
    }
}

def verify-no-usr-local [out_dir: string] {
    for deb in (glob ($out_dir | path join "*.deb")) {
        let listing = (capture ["dpkg-deb", "-c", $deb])
        if ($listing | str contains "./usr/local/") {
            fail $"Package unexpectedly contains /usr/local files: ($deb)"
        }
    }
}

# Fetch the latest patch release in the current GNOME Evolution series from the
# GNOME download server.  Returns null on network error or parse failure.
def gnome-evolution-latest [] {
    # Derive the major.minor series directory from the hardcoded upstream version.
    let series = ($UPSTREAM_VERSION | split row "." | first 2 | str join ".")
    let result = (do {
        ^curl -fsSL $"https://download.gnome.org/sources/evolution/($series)/"
    } | complete)
    if $result.exit_code != 0 { return null }

    # Directory listings contain hrefs like:  evolution-3.60.X.tar.xz
    # Escape the dots in the series string so they match literally in the regex.
    let series_re = ($series | str replace --all "." "\\.")
    let pattern = $"evolution-(?P<ver>($series_re)\\.[0-9]+)\\.tar\\.xz"
    let versions = (
        $result.stdout
        | lines
        | each {|l|
            let m = ($l | parse --regex $pattern)
            if ($m | is-empty) { null } else { $m.ver.0 }
        }
        | where {|v| $v != null}
        | sort
        | reverse
    )
    if ($versions | is-empty) { null } else { $versions | first }
}

# Print a version status report for all three Evolution components and exit.
# Checks the installed `evolution` package against LOCAL_VERSION and queries
# the GNOME download server for newer patch releases in the wrapped series.
def do-check [] {
    let installed = (dpkg-version "evolution")
    let installed_str = if $installed == null { "(not installed)" } else { $installed }

    print $"Package:      evolution (+ evolution-data-server, evolution-ews)"
    print $"Installed:    ($installed_str)"
    print $"Builds:       ($LOCAL_VERSION)"

    print "              (fetching GNOME latest...)"
    let upstream = (gnome-evolution-latest)
    let upstream_str = if $upstream != null {
        $"($upstream) (download.gnome.org/sources/evolution)"
    } else {
        "(could not fetch)"
    }
    print $"Upstream:     ($upstream_str)"

    let needs_build = $installed == null or $installed != $LOCAL_VERSION
    # A newer upstream exists when the server reports a version beyond what the
    # script currently wraps, signalling that the script itself needs updating.
    let newer_upstream = $upstream != null and $upstream != $UPSTREAM_VERSION

    if $needs_build {
        print "Status:       NEEDS BUILD"
        exit 1
    }
    if $newer_upstream {
        print $"Status:       NEWER UPSTREAM AVAILABLE (($upstream) vs script wraps ($UPSTREAM_VERSION))"
        exit 1
    }
    print "Status:       UP TO DATE"
}

def main [
    --install (-i)  # Accepted for interface consistency; Evolution always installs during build
    --check   (-c)  # Report version status without building; exits 0 (ok) or 1 (action needed)
    --force   (-f)  # Skip up-to-date check and always rebuild
] {
    if $check and $force {
        fail "--check and --force are mutually exclusive"
    }

    if $check {
        do-check
        return
    }

    # Without --force, skip the long multi-hour build when the package is current.
    if not $force {
        let installed = (dpkg-version "evolution")
        if $installed != null and $installed == $LOCAL_VERSION {
            print $"Evolution ($LOCAL_VERSION) is already installed. Use --force to rebuild."
            return
        }
    }

    assert-ubuntu-24

    $env.DEBFULLNAME = "Local Evolution Builder"
    $env.DEBEMAIL = "local-evolution-builder@localhost"
    $env.DEB_BUILD_OPTIONS = "nocheck"

    let arch = (capture ["dpkg", "--print-architecture"])
    let jobs = (capture ["nproc"])
    let start_dir = (pwd)

    let work = ($start_dir | path join "evolution-3.60.2-deb-build")
    let downloads = ($work | path join "downloads")
    let build_root = ($work | path join "build")
    let out_dir = ($work | path join "debs")

    let eds_root = ($build_root | path join "evolution-data-server")
    let evo_root = ($build_root | path join "evolution")
    let ews_root = ($build_root | path join "evolution-ews")

    let eds_src = ($eds_root | path join "source")
    let evo_src = ($evo_root | path join "source")
    let ews_src = ($ews_root | path join "source")

    ensure-dir $work
    ensure-dir $downloads

    if ($build_root | path exists) {
        rm --recursive --force $build_root
    }
    mkdir $build_root

    if ($out_dir | path exists) {
        rm --recursive --force $out_dir
    }
    mkdir $out_dir

    print $"Building Evolution ($UPSTREAM_VERSION) for Ubuntu 24.04 / ($arch)"
    print $"Local package version: ($LOCAL_VERSION)"
    print $"Parallel build jobs: ($jobs)"
    print $"\nOutput will be written to:\n  ($out_dir)"

    # Avoid changing files underneath a running Evolution process.
    let running = (do { ^pgrep -x evolution } | complete)
    if $running.exit_code == 0 {
        fail "Evolution is currently running. Close it completely, then rerun this script."
    }

    print "\n==> Installing packaging/bootstrap tools"
    run-cmd ["sudo", "apt-get", "update"]
    run-cmd [
        "sudo", "apt-get", "install", "-y", "--no-install-recommends",
        "build-essential",
        "ca-certificates",
        "curl",
        "debhelper",
        "devscripts",
        "dpkg-dev",
        "equivs",
        "libdistro-info-perl",
        "libgirepository1.0-dev",
        "fakeroot",
        "gnome-pkg-tools",
        "pkg-config",
        "quilt",
        "xz-utils"
    ]

    let eds_tar = ($downloads | path join $"evolution-data-server-($UPSTREAM_VERSION).tar.xz")
    let evo_tar = ($downloads | path join $"evolution-($UPSTREAM_VERSION).tar.xz")
    let ews_tar = ($downloads | path join $"evolution-ews-($UPSTREAM_VERSION).tar.xz")

    let eds_debian_tar = ($downloads | path join $"evolution-data-server_($EDS_PACKAGING_VERSION).debian.tar.xz")
    let evo_debian_tar = ($downloads | path join $"evolution_($EVO_PACKAGING_VERSION).debian.tar.xz")
    let ews_debian_tar = ($downloads | path join $"evolution-ews_($EWS_PACKAGING_VERSION).debian.tar.xz")

    print "\n==> Downloading official GNOME 3.60.2 sources"
    download $"https://download.gnome.org/sources/evolution-data-server/3.60/evolution-data-server-($UPSTREAM_VERSION).tar.xz" $eds_tar
    download $"https://download.gnome.org/sources/evolution/3.60/evolution-($UPSTREAM_VERSION).tar.xz" $evo_tar
    download $"https://download.gnome.org/sources/evolution-ews/3.60/evolution-ews-($UPSTREAM_VERSION).tar.xz" $ews_tar

    verify-sha256 $eds_tar $EDS_SHA256
    verify-sha256 $evo_tar $EVO_SHA256
    verify-sha256 $ews_tar $EWS_SHA256

    print "\n==> Downloading recent Debian packaging metadata"
    download $"https://deb.debian.org/debian/pool/main/e/evolution-data-server/evolution-data-server_($EDS_PACKAGING_VERSION).debian.tar.xz" $eds_debian_tar
    download $"https://deb.debian.org/debian/pool/main/e/evolution/evolution_($EVO_PACKAGING_VERSION).debian.tar.xz" $evo_debian_tar
    download $"https://deb.debian.org/debian/pool/main/e/evolution-ews/evolution-ews_($EWS_PACKAGING_VERSION).debian.tar.xz" $ews_debian_tar

    verify-md5 $eds_debian_tar $EDS_DEBIAN_MD5
    verify-md5 $evo_debian_tar $EVO_DEBIAN_MD5
    verify-md5 $ews_debian_tar $EWS_DEBIAN_MD5

    # ----------------------------------------------------------------------
    # 1. Evolution Data Server
    # ----------------------------------------------------------------------
    print "\n============================================================"
    print "  1/3  evolution-data-server 3.60.2"
    print "============================================================"

    reset-component-dir $eds_root $eds_src
    unpack-source $eds_tar $eds_debian_tar $eds_src
    clear-patch-series $eds_src

    let eds_control = ($eds_src | path join "debian" "control")
    let eds_rules = ($eds_src | path join "debian" "rules")
    let eds_debian = ($eds_src | path join "debian")

    # Debian's current metadata is for 3.56.2. Tight build dependencies in the
    # package metadata need to point at our matched 3.60.2 stack.
    replace-in-file $eds_control "3.56.2" $UPSTREAM_VERSION
    replace-in-file $eds_control "3.57" "3.61"
    replace-in-file $eds_rules "3.56.2" $UPSTREAM_VERSION
    replace-in-file $eds_rules "3.57" "3.61"
    fix-dh-gnome-clean-for-noble $eds_rules

    # Keep the newer 3.56 packaging layout (closer to upstream 3.60), while
    # translating only Build-Depends names that Noble cannot resolve.
    adapt-eds-build-deps-for-noble $eds_control

    # Upstream 3.60 changed these SONAMEs. Do not pretend the new .so files
    # implement Noble's old .so.64/.so.4 ABI package names.
    replace-in-file $eds_control "libcamel-1.2-64t64" "libcamel-1.2-67t64"
    replace-in-file $eds_control "libebook-contacts-1.2-4t64" "libebook-contacts-1.2-5t64"

    rename-package-helper-files $eds_debian "libcamel-1.2-64t64" "libcamel-1.2-67t64"
    rename-package-helper-files $eds_debian "libebook-contacts-1.2-4t64" "libebook-contacts-1.2-5t64"

    # Old symbols/shlibs manifests encode the old SONAME. Let debhelper derive
    # fresh shlibs metadata for the two new ABI packages instead.
    remove-if-present ($eds_debian | path join "libcamel-1.2-67t64.symbols*")
    remove-if-present ($eds_debian | path join "libcamel-1.2-67t64.shlibs*")
    remove-if-present ($eds_debian | path join "libebook-contacts-1.2-5t64.symbols*")
    remove-if-present ($eds_debian | path join "libebook-contacts-1.2-5t64.shlibs*")

    # Debian carries a tiny patch making this internal helper static. Reproduce
    # the packaging change directly, instead of applying the rest of Debian's
    # 3.56-specific patch series to the newer 3.60 source.
    let edbus_cmake = ($eds_src | path join "src" "private" "CMakeLists.txt")
    let edbus_text = (open --raw $edbus_cmake)
    if ($edbus_text | str contains "add_library(edbus-private SHARED") {
        ($edbus_text
            | str replace "add_library(edbus-private SHARED" "add_library(edbus-private STATIC"
        ) | save --force $edbus_cmake
    } else if not ($edbus_text | str contains "add_library(edbus-private STATIC") {
        fail "Could not locate the edbus-private library declaration in EDS 3.60.2."
    }

    # Debian 3.56.2 symbols files don't match 3.60.2 new symbols; drop them all
    # so debhelper generates fresh ones from the actual built libraries.
    remove-if-present ($eds_debian | path join "*.symbols")

    # evolution-scan-gconf-tree-xml was removed in 3.58+; drop the stale entry
    # from the Debian 3.56 packaging so dh_install doesn't abort on a missing file.
    let eds_install = ($eds_debian | path join "evolution-data-server.install")
    if ($eds_install | path exists) {
        let filtered = (open --raw $eds_install | lines | where {|l| not ($l | str contains "evolution-scan-gconf-tree-xml")} | str join "\n")
        ($filtered + "\n") | save --force $eds_install
    }

    add-local-changelog $eds_src "evolution-data-server"

    print "
==> EDS Build-Depends after Ubuntu 24.04 compatibility translation:"
    let control_lines = (open --raw $eds_control | lines)
    let bd_start = (
        $control_lines
        | enumerate
        | where {|row| $row.item | str starts-with "Build-Depends:"}
        | get 0.index
    )
    mut bd_end = ($bd_start + 1)
    while ($bd_end < ($control_lines | length)) and (($control_lines | get $bd_end) | str starts-with " ") {
        $bd_end = $bd_end + 1
    }
    $control_lines
        | slice $bd_start..<$bd_end
        | each {|line| print $line }

    print "\n==> dh_gnome_clean compatibility:"
    if ((open --raw $eds_rules) | str contains "dh_gnome_clean --no-control") {
        print "OK: dh_gnome_clean will run with --no-control"
    } else {
        fail "Failed to install dh_gnome_clean --no-control override"
    }

    install-build-deps $eds_src
    build-component $eds_src $jobs
    copy-component-debs $eds_root $out_dir

    print "\n==> Installing freshly built EDS packages needed for the next stage"
    install-component-debs $eds_root
    run-cmd ["sudo", "ldconfig"]

    # ----------------------------------------------------------------------
    # 2. Evolution
    # ----------------------------------------------------------------------
    print "\n============================================================"
    print "  2/3  evolution 3.60.2"
    print "============================================================"

    reset-component-dir $evo_root $evo_src
    unpack-source $evo_tar $evo_debian_tar $evo_src
    clear-patch-series $evo_src

    let evo_control = ($evo_src | path join "debian" "control")
    let evo_rules = ($evo_src | path join "debian" "rules")

    replace-in-file $evo_control "3.56.2" $UPSTREAM_VERSION
    replace-in-file $evo_control "3.57" "3.61"
    replace-in-file $evo_rules "3.56.2" $UPSTREAM_VERSION
    replace-in-file $evo_rules "3.57" "3.61"
    fix-dh-gnome-clean-for-noble $evo_rules

    # Noble ships debhelper 13; evolution 3.56.2 packaging uses compat 14.
    replace-in-file $evo_control "debhelper-compat (= 14)" "debhelper-compat (= 13)"

    # Upstream renamed the appdata file to .metainfo.xml in 3.58+.
    let evo_debian = ($evo_src | path join "debian")
    let evo_install = ($evo_debian | path join "evolution.install")
    replace-in-file $evo_install "org.gnome.Evolution.appdata.xml" "org.gnome.Evolution.metainfo.xml"

    # In 3.60, the evolution private libs are unversioned (.so only, no .so.*).
    # Move them from the .so.* pattern in libevolution.install to just .so,
    # and remove the duplicate glob from evolution-dev.install.
    let libevo_install = ($evo_debian | path join "libevolution.install")
    let evo_dev_install = ($evo_debian | path join "evolution-dev.install")
    replace-in-file $libevo_install "usr/lib/evolution/*.so.*" "usr/lib/evolution/*.so"
    if ($evo_dev_install | path exists) {
        let filtered = (open --raw $evo_dev_install | lines | where {|l| not ($l | str trim | str starts-with "usr/lib/evolution/*.so")} | str join "\n")
        ($filtered + "\n") | save --force $evo_dev_install
    }

    # The rules file has a stale rm for a file that is now in libevolution, not evolution-dev.
    replace-in-file $evo_rules "\trm debian/evolution-dev/usr/lib/evolution/libevolution-rss-common.so" "\trm -f debian/evolution-dev/usr/lib/evolution/libevolution-rss-common.so"

    add-local-changelog $evo_src "evolution"
    install-build-deps $evo_src
    build-component $evo_src $jobs
    copy-component-debs $evo_root $out_dir

    print "\n==> Installing freshly built Evolution packages needed for EWS"
    install-component-debs $evo_root
    run-cmd ["sudo", "ldconfig"]

    # ----------------------------------------------------------------------
    # 3. Evolution-EWS / Microsoft 365
    # ----------------------------------------------------------------------
    print "\n============================================================"
    print "  3/3  evolution-ews 3.60.2 (EWS + Microsoft 365)"
    print "============================================================"

    reset-component-dir $ews_root $ews_src
    unpack-source $ews_tar $ews_debian_tar $ews_src
    clear-patch-series $ews_src

    let ews_control = ($ews_src | path join "debian" "control")
    let ews_rules = ($ews_src | path join "debian" "rules")

    replace-in-file $ews_control "3.56.2" $UPSTREAM_VERSION
    replace-in-file $ews_control "3.57" "3.61"
    replace-in-file $ews_rules "3.56.2" $UPSTREAM_VERSION
    replace-in-file $ews_rules "3.57" "3.61"
    fix-dh-gnome-clean-for-noble $ews_rules

    # Noble ships debhelper 13; ews packaging may use compat 14.
    replace-in-file $ews_control "debhelper-compat (= 14)" "debhelper-compat (= 13)"

    add-local-changelog $ews_src "evolution-ews"
    install-build-deps $ews_src
    build-component $ews_src $jobs
    copy-component-debs $ews_root $out_dir

    print "\n==> Installing Evolution-EWS / Microsoft 365 packages"
    install-component-debs $ews_root
    run-cmd ["sudo", "ldconfig"]

    # ----------------------------------------------------------------------
    # Validation
    # ----------------------------------------------------------------------
    print "\n============================================================"
    print "  Validation"
    print "============================================================"

    verify-no-usr-local $out_dir

    let evo_version = (capture ["evolution", "--version"])
    if not ($evo_version | str contains $UPSTREAM_VERSION) {
        fail $"Installed Evolution does not report ($UPSTREAM_VERSION): ($evo_version)"
    }

    let m365 = (
        do {
            ^find /usr/lib -type f -name "module-microsoft365-configuration.so" -print -quit
        } | complete
    )

    if ($m365.exit_code != 0) or (($m365.stdout | str trim | is-empty)) {
        fail "Evolution-EWS installed, but the Microsoft 365 configuration module was not found."
    }

    print $"\nInstalled: ($evo_version)"
    print $"Microsoft 365 module: ($m365.stdout | str trim)"

    print "\nInstalled package versions:"
    run-cmd [
        "dpkg-query", "-W",
        "evolution",
        "evolution-common",
        "libevolution",
        "evolution-data-server",
        "evolution-data-server-common",
        "evolution-ews",
        "evolution-ews-core",
        "libcamel-1.2-67t64",
        "libebook-contacts-1.2-5t64"
    ]

    print $"\nSUCCESS"
    print $"All generated .deb files are in:\n  ($out_dir)"
    print "\nThe main packages are same-name, higher-version replacements for Noble's"
    print "Evolution packages and install under /usr. The old libcamel .so.64 and"
    print "libebook-contacts .so.4 runtime packages are intentionally allowed to"
    print "coexist because 3.60.2 uses the new .so.67/.so.5 ABIs."
    print "\nLog out/in (or reboot) before using Evolution so any old EDS background"
    print "processes are replaced by the newly installed binaries/libraries."
}
