#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PKGBUILD_ROOT="$HOME/workspace/pkgbuilds"

INSTALL_LIST="$PKGBUILD_ROOT/install-list"
INSTALL_LIST_AUR="$PKGBUILD_ROOT/install-list-aur"

REPO_DIR="$HOME/.local/share/pacman/custom"
REPO_NAME="custom"
REPO_DB="$REPO_DIR/$REPO_NAME.db.tar.zst"

AUR_BUILD_ROOT="$PKGBUILD_ROOT/.aur-build"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

check_only=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [--check]

Options:
  --check   Only check whether packages need updates.
            Do not build packages or modify the local repository.

  -h, --help
            Show this help.
EOF
}

while (($# > 0)); do
    case "$1" in
        --check)
            check_only=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac

    shift
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

trim() {
    local value="$1"

    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    printf '%s' "$value"
}

get_repo_version() {
    local wanted="$1"
    local file
    local info
    local name
    local version
    local newest=""

    shopt -s nullglob

    for file in "$REPO_DIR"/*.pkg.tar.*; do
        [[ "$file" == *.sig ]] && continue
        [[ -f "$file" ]] || continue

        info="$(LC_ALL=C pacman -Qp "$file" 2>/dev/null)" || continue

        name="${info%% *}"
        version="${info#* }"

        [[ "$name" == "$wanted" ]] || continue

        if [[ -z "$newest" ]]; then
            newest="$version"
        elif (( $(vercmp "$version" "$newest") > 0 )); then
            newest="$version"
        fi
    done

    shopt -u nullglob

    printf '%s\n' "$newest"
}

get_aur_info() {
    local package="$1"

    LC_ALL=C yay \
        -Si \
        --aur \
        --color never \
        "$package"
}

get_aur_version() {
    sed -n \
        's/^Version[[:space:]]*:[[:space:]]*//p' \
        | head -n 1
}

get_aur_pkgbase() {
    sed -n \
        's/^Package Base[[:space:]]*:[[:space:]]*//p' \
        | head -n 1
}

copy_packages_to_repo() {
    local entry="$1"
    shift

    local package
    local dest
    local copied=0

    for package in "$@"; do
        if [[ ! -f "$package" ]]; then
            echo "ERROR: Expected package does not exist: $package" >&2
            ((failed++))
            continue
        fi

        dest="$REPO_DIR/$(basename "$package")"

        echo "==> Copying $(basename "$package") to local repository"

        if ! cp -f -- "$package" "$dest"; then
            echo "ERROR: Failed to copy $package" >&2
            ((failed++))
            continue
        fi

        new_packages+=("$dest")
        ((copied++))
    done

    if ((copied > 0)); then
        ((updated++))
    fi
}

# Delete superseded package files from the repository.
#
# This deliberately runs only AFTER repo-add has successfully updated the
# repository database.
prune_old_package_files() {
    local new_file
    local new_info
    local package_name

    local candidate
    local candidate_info
    local candidate_name

    local keep
    local -a package_names=()

    # Determine all package names updated during this run.
    for new_file in "${new_packages[@]}"; do
        new_info="$(LC_ALL=C pacman -Qp "$new_file" 2>/dev/null)" || {
            echo "WARNING: Could not inspect $new_file while pruning." >&2
            continue
        }

        package_name="${new_info%% *}"

        if [[ ! " ${package_names[*]} " =~ " ${package_name} " ]]; then
            package_names+=("$package_name")
        fi
    done

    shopt -s nullglob

    for package_name in "${package_names[@]}"; do
        for candidate in "$REPO_DIR"/*.pkg.tar.*; do
            [[ "$candidate" == *.sig ]] && continue
            [[ -f "$candidate" ]] || continue

            candidate_info="$(
                LC_ALL=C pacman -Qp "$candidate" 2>/dev/null
            )" || continue

            candidate_name="${candidate_info%% *}"

            [[ "$candidate_name" == "$package_name" ]] || continue

            # Keep package files that were copied during this run.
            keep=false

            for new_file in "${new_packages[@]}"; do
                if [[ "$candidate" == "$new_file" ]]; then
                    keep=true
                    break
                fi
            done

            if $keep; then
                continue
            fi

            echo "==> Removing old package: $(basename "$candidate")"
            rm -f -- "$candidate"

            # Remove a detached package signature as well, if present.
            if [[ -f "$candidate.sig" ]]; then
                rm -f -- "$candidate.sig"
            fi
        done
    done

    shopt -u nullglob
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

if [[ ! -f "$INSTALL_LIST" && ! -f "$INSTALL_LIST_AUR" ]]; then
    echo "ERROR: Neither install-list nor install-list-aur exists." >&2
    exit 1
fi

if ! $check_only; then
    mkdir -p "$REPO_DIR"
    mkdir -p "$AUR_BUILD_ROOT"
fi

updated=0
outdated=0
unchecked=0
failed=0

new_packages=()

# ---------------------------------------------------------------------------
# Local/custom PKGBUILDs
# ---------------------------------------------------------------------------

if [[ -f "$INSTALL_LIST" ]]; then
    while IFS= read -r entry || [[ -n "$entry" ]]; do
        entry="$(trim "$entry")"

        [[ -z "$entry" ]] && continue
        [[ "$entry" == \#* ]] && continue

        if [[ "$entry" == */* || "$entry" == "." || "$entry" == ".." ]]; then
            echo "ERROR: Invalid install-list entry: $entry" >&2
            ((failed++))
            continue
        fi

        dir="$PKGBUILD_ROOT/$entry"

        echo
        echo "================================================================"
        echo "==> LOCAL: $entry"
        echo "================================================================"

        if [[ ! -d "$dir" ]]; then
            echo "ERROR: Directory does not exist: $dir" >&2
            ((failed++))
            continue
        fi

        if [[ ! -f "$dir/PKGBUILD" ]]; then
            echo "ERROR: No PKGBUILD found in: $dir" >&2
            ((failed++))
            continue
        fi

        cd "$dir" || {
            echo "ERROR: Cannot enter $dir" >&2
            ((failed++))
            continue
        }

        # -------------------------------------------------------------------
        # Version handling
        # -------------------------------------------------------------------

        if [[ -f .nvchecker.toml ]]; then
            echo "==> Checking upstream version..."

            pkgctl version check
            rc=$?

            case "$rc" in
                0)
                    echo "==> $entry is up to date."
                    continue
                    ;;

                2)
                    echo "==> $entry has an update available."
                    ((outdated++))

                    if $check_only; then
                        continue
                    fi
                    ;;

                *)
                    echo \
                        "ERROR: Version check failed for $entry (exit code $rc)" \
                        >&2
                    ((failed++))
                    continue
                    ;;
            esac

            echo "==> Updating PKGBUILD..."

            if ! pkgctl version upgrade; then
                echo \
                    "ERROR: pkgctl version upgrade failed for $entry" \
                    >&2
                ((failed++))
                continue
            fi
        else
            if $check_only; then
                echo "==> UNCHECKED: no .nvchecker.toml"
                ((unchecked++))
                continue
            fi

            echo "==> No .nvchecker.toml; skipping version check."
            echo "==> Ensuring current PKGBUILD is built."
        fi

        # -------------------------------------------------------------------
        # Build
        # -------------------------------------------------------------------

        echo "==> Building/checking $entry..."

        # No -f: reuse an existing build if possible.
        if ! PKGDEST="$PWD" makepkg -sc --noconfirm; then
            echo "ERROR: Build failed for $entry" >&2
            ((failed++))
            continue
        fi

        mapfile -t packages < <(
            PKGDEST="$PWD" makepkg --packagelist
        )

        if ((${#packages[@]} == 0)); then
            echo "ERROR: makepkg produced no package list for $entry" >&2
            ((failed++))
            continue
        fi

        copy_packages_to_repo "$entry" "${packages[@]}"

    done < "$INSTALL_LIST"
fi

# ---------------------------------------------------------------------------
# AUR packages
# ---------------------------------------------------------------------------

if [[ -f "$INSTALL_LIST_AUR" ]]; then
    while IFS= read -r entry || [[ -n "$entry" ]]; do
        entry="$(trim "$entry")"

        [[ -z "$entry" ]] && continue
        [[ "$entry" == \#* ]] && continue

        if [[ "$entry" == */* || "$entry" == "." || "$entry" == ".." ]]; then
            echo "ERROR: Invalid install-list-aur entry: $entry" >&2
            ((failed++))
            continue
        fi

        echo
        echo "================================================================"
        echo "==> AUR: $entry"
        echo "================================================================"

        echo "==> Checking AUR version..."

        if ! aur_info="$(get_aur_info "$entry")"; then
            echo "ERROR: Could not query AUR package: $entry" >&2
            ((failed++))
            continue
        fi

        aur_version="$(
            printf '%s\n' "$aur_info" |
                get_aur_version
        )"

        aur_pkgbase="$(
            printf '%s\n' "$aur_info" |
                get_aur_pkgbase
        )"

        [[ -n "$aur_pkgbase" ]] || aur_pkgbase="$entry"

        if [[ -z "$aur_version" ]]; then
            echo "ERROR: Could not determine AUR version for $entry" >&2
            ((failed++))
            continue
        fi

        repo_version="$(get_repo_version "$entry")"

        needs_build=false

        if [[ -z "$repo_version" ]]; then
            echo "==> $entry is not present in the local repository."
            echo "    AUR version: $aur_version"
            needs_build=true
        else
            cmp="$(vercmp "$aur_version" "$repo_version")"

            if ((cmp > 0)); then
                echo "==> Update available:"
                echo "    repo: $repo_version"
                echo "    AUR:  $aur_version"

                needs_build=true

            elif ((cmp == 0)); then
                echo "==> $entry is up to date ($aur_version)."

            else
                echo "==> Local repository is newer than AUR:"
                echo "    repo: $repo_version"
                echo "    AUR:  $aur_version"
            fi
        fi

        if $needs_build; then
            ((outdated++))
        else
            continue
        fi

        if $check_only; then
            continue
        fi

        # -------------------------------------------------------------------
        # Download
        # -------------------------------------------------------------------

        aur_dir="$AUR_BUILD_ROOT/$aur_pkgbase"

        echo "==> Downloading $entry from AUR..."

        rm -rf -- "$aur_dir"

        if ! (
            cd "$AUR_BUILD_ROOT" &&
            yay -G --aur "$entry"
        ); then
            echo "ERROR: yay failed to download $entry" >&2
            ((failed++))
            continue
        fi

        if [[ ! -f "$aur_dir/PKGBUILD" ]]; then
            echo "ERROR: Expected downloaded PKGBUILD at $aur_dir/PKGBUILD" >&2
            ((failed++))
            continue
        fi

        # -------------------------------------------------------------------
        # Build
        # -------------------------------------------------------------------

        echo "==> Building $entry with yay..."

        if ! PKGDEST="$aur_dir" \
            yay -Bi "$aur_dir" --noconfirm
        then
            echo "ERROR: yay build failed for $entry" >&2
            ((failed++))
            continue
        fi

        mapfile -t packages < <(
            cd "$aur_dir" &&
            PKGDEST="$aur_dir" makepkg --packagelist
        )

        if ((${#packages[@]} == 0)); then
            echo "ERROR: makepkg produced no package list for AUR package $entry" >&2
            ((failed++))
            continue
        fi

        copy_packages_to_repo "$entry" "${packages[@]}"

    done < "$INSTALL_LIST_AUR"
fi

# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

if $check_only; then
    echo
    echo "================================================================"
    echo "Check summary"
    echo "================================================================"
    echo "Needs update/build: $outdated"
    echo "Unchecked:          $unchecked"
    echo "Failed:             $failed"
    echo "================================================================"

    if ((failed > 0)); then
        exit 1
    fi

    if ((outdated > 0)); then
        exit 2
    fi

    exit 0
fi

# ---------------------------------------------------------------------------
# Update repository
# ---------------------------------------------------------------------------

if ((${#new_packages[@]} > 0)); then
    echo
    echo "================================================================"
    echo "==> Updating local repository database"
    echo "================================================================"

    if ! repo-add \
        --wait-for-lock \
        "$REPO_DB" \
        "${new_packages[@]}"
    then
        echo "ERROR: repo-add failed" >&2
        exit 1
    fi

    # The database now points to the new packages, so old physical package
    # files can safely be removed.
    echo
    echo "==> Removing superseded package files..."
    prune_old_package_files

    echo
    echo "==> Repository updated:"
    echo "    $REPO_DB"

    # -----------------------------------------------------------------------
    # Install newly published packages / finish system upgrade
    # -----------------------------------------------------------------------

    echo
    echo "================================================================"
    echo "==> Updating system from refreshed repositories"
    echo "================================================================"

    if ! sudo pacman -Syu; then
        echo "ERROR: pacman -Syu failed" >&2
        exit 1
    fi
else
    echo
    echo "==> Nothing needed rebuilding."
fi

echo
echo "================================================================"
echo "Processed: $updated"
echo "Failed:    $failed"
echo "================================================================"

((failed == 0))
