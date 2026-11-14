#!/usr/bin/env python3
"""Stage a Nuitka onefile build and create the release zip.

The zip contains:
  config.json                 (copied from config.example.json)
  <binary name>               (the Nuitka onefile executable)
  playlists/                  (the example playlist directory)

This script is intentionally Python 3.6 compatible because the Vista build
runs it under Python 3.6.
"""

import argparse
import os
import shutil
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, help="Path to the built onefile executable")
    parser.add_argument("--label", required=True, help="Platform label, e.g. Windows-x64")
    parser.add_argument("--binary-name", required=True, help="Name of the executable inside the zip")
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--output-dir", default="release-assets")
    parser.add_argument("--stage-dir", default=None)
    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root)
    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary):
        print("Binary not found: %s" % binary, file=sys.stderr)
        return 1

    config_example = os.path.join(project_root, "config.example.json")
    if not os.path.isfile(config_example):
        print("config.example.json not found: %s" % config_example, file=sys.stderr)
        return 1

    playlists = os.path.join(project_root, "playlists")
    if not os.path.isdir(playlists):
        print("playlists directory not found: %s" % playlists, file=sys.stderr)
        return 1

    stage_dir = args.stage_dir or os.path.join(args.output_dir, "stage-%s" % args.label)
    stage_dir = os.path.abspath(stage_dir)
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir)

    shutil.copy2(binary, os.path.join(stage_dir, args.binary_name))
    shutil.copy2(config_example, os.path.join(stage_dir, "config.json"))
    shutil.copytree(playlists, os.path.join(stage_dir, "playlists"))

    output_dir = os.path.abspath(args.output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    zip_path = os.path.join(output_dir, "HDHRProxyWMC-IPTV-%s.zip" % args.label)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(stage_dir):
            for filename in sorted(filenames):
                full_path = os.path.join(dirpath, filename)
                relative = os.path.relpath(full_path, stage_dir)
                info = zipfile.ZipInfo(relative.replace(os.sep, "/"))
                source_mode = os.stat(full_path).st_mode
                if filename == args.binary_name and os.name != "nt":
                    source_mode = 0o755
                else:
                    source_mode = 0o644
                info.external_attr = (source_mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full_path, "rb") as source:
                    archive.writestr(info, source.read())

    print(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
