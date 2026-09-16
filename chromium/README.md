# Chromium package development

Run `makepkg` once to fetch, prepare, build, and package Chromium. After editing files under `src/`, run:

```sh
makepkg --noextract --force
```

`--noextract` preserves the whole `src/` directory and skips `prepare()`, while `--force` replaces an existing package archive. The `build()` function regenerates the GN files and runs Ninja against the existing `out/Release` directory, so changed files are rebuilt incrementally. Leave out `--clean` (`-c`), which removes `src/` after a successful build.

With the default manual clone setting, an ordinary `makepkg --force` also reuses a prepared Chromium tree, but makepkg reextracts the launcher archive in that mode. Use `--noextract` when editing any file under `src/`, including the launcher. A partial tree that never completed preparation must be fixed manually or recreated with `makepkg --cleanbuild`, which removes `src/`.
