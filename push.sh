#!/bin/bash
set -eo pipefail

# cd to parent dir of current script
cd "$(dirname "${BASH_SOURCE[0]}")"

files=(
    "$PWD/./cwb/PKGBUILD"
    "$PWD/./ssh-encrypt/PKGBUILD"
    "$PWD/./xkeyboard-config-desc/PKGBUILD"
    "$PWD/./xkeyboard-config-desc/desc"
    "$PWD/./srt-live-server/PKGBUILD"
)

printf "%s\n" "${files[@]}" | rsync --files-from "-" "/" "$(rsync-hosts pkgbuilds)"
