# Shared helpers for Ubuntu package build scripts (.nupkg.ubuntu.nu).
#
# Import with:  use ../.nupkg.ubuntu.nu *
#
# Convention: all helpers surface failures through `fail`, which raises a
# nushell error and halts execution.  External commands are logged before they
# run (run-cmd) so progress is visible.  Download helpers are idempotent —
# they skip files that are already present on disk.

# Raise a fatal error with message and halt execution.
export def fail [message: string] {
    error make {msg: $message}
}

# Run an external command, printing the full argv first; halt on non-zero exit.
export def run-cmd [argv: list<string>] {
    if ($argv | is-empty) {
        fail "run-cmd: argv must not be empty"
    }

    let exe = ($argv | first)
    let args = ($argv | skip 1)

    print $"\n==> ($argv | str join ' ')"
    ^$exe ...$args

    if $env.LAST_EXIT_CODE != 0 {
        fail $"Command failed (exit ($env.LAST_EXIT_CODE)): ($argv | str join ' ')"
    }
}

# Run an external command and return its trimmed stdout; halt on non-zero exit.
export def capture [argv: list<string>] {
    if ($argv | is-empty) {
        fail "capture: argv must not be empty"
    }

    let exe = ($argv | first)
    let args = ($argv | skip 1)
    let result = (do { ^$exe ...$args } | complete)

    if $result.exit_code != 0 {
        fail $"Command failed: ($argv | str join ' ')\n($result.stderr)"
    }

    $result.stdout | str trim
}

# Create a directory (and any missing parents) when it does not already exist.
export def ensure-dir [dir: string] {
    if not ($dir | path exists) {
        mkdir $dir
    }
}

# Download url to dest; skip the download if dest already exists (cache-friendly).
export def download [url: string, dest: string] {
    if ($dest | path exists) {
        print $"==> Using cached ($dest)"
        return
    }
    run-cmd [
        "curl", "--fail", "--location",
        "--retry", "4", "--retry-delay", "2",
        "--output", $dest, $url
    ]
}

# Verify a file's SHA-256 digest against expected; halt on mismatch.
export def verify-sha256 [file: string, expected: string] {
    let actual = (capture ["sha256sum", $file] | split row " " | first)

    if $actual != $expected {
        fail $"SHA-256 mismatch for ($file)\n  expected: ($expected)\n  actual:   ($actual)"
    }

    print $"==> SHA-256 OK: ($file | path basename)"
}

# Verify a file's MD5 digest against expected; halt on mismatch.
export def verify-md5 [file: string, expected: string] {
    let actual = (capture ["md5sum", $file] | split row " " | first)

    if $actual != $expected {
        fail $"MD5 mismatch for ($file)\n  expected: ($expected)\n  actual:   ($actual)"
    }

    print $"==> MD5 OK: ($file | path basename)"
}

# Abort execution when the running system is not Ubuntu 24.04 (Noble Numbat).
export def assert-ubuntu-24 [] {
    let os_release = (open --raw /etc/os-release)
    if not ($os_release | str contains 'VERSION_ID="24.04"') {
        fail "This script targets Ubuntu 24.04 (Noble Numbat) only."
    }
}

# Return the installed Debian package version string, or null when not installed.
export def dpkg-version [pkg: string] {
    let result = (do { ^dpkg-query -W -f='${Version}' $pkg } | complete)
    if $result.exit_code == 0 { $result.stdout | str trim } else { null }
}

# Install a list of .deb files via `sudo apt-get install`; skips -dbgsym packages.
export def apt-install-debs [debs: list<string>] {
    # Debug-symbol packages are large and rarely needed on the build host.
    let installable = ($debs | where {|p|
        not (($p | path basename) | str contains "-dbgsym_")
    })

    if ($installable | is-empty) {
        fail "apt-install-debs: no installable packages in the provided list"
    }

    run-cmd (["sudo", "apt-get", "install", "-y"] | append $installable)
}
