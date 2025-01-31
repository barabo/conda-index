import os
import sys

from conda.exports import ArgumentParser

from .. import api
from ..index import MAX_THREADS_DEFAULT, logutil
from ..utils import DEFAULT_SUBDIRS


def parse_args(args):
    p = ArgumentParser(
        description="Update package index metadata files in given directories."
    )
    p.add_argument(
        "dir",
        help="Directory that contains an index to be updated.",
        nargs="*",
        default=[os.getcwd()],
    )
    p.add_argument(
        "--output",
        help="Directory to generate index. Default [dir]. Cannot be used with multiple [dir].",
    )
    p.add_argument(
        "-c",
        "--check-md5",
        action="store_true",
        help="""Use hash values instead of file modification times for determining if a
        package's metadata needs to be updated.""",
    )
    p.add_argument(
        "-n",
        "--channel-name",
        help="Customize the channel name listed in each channel's index.html.",
    )
    p.add_argument(
        "-s",
        "--subdir",
        action="append",
        help="Optional. The subdir to index. Can be given multiple times. If not provided, will "
        "default to all of %s. If provided, will not create channeldata.json for the channel."
        "" % ", ".join(DEFAULT_SUBDIRS),
    )
    p.add_argument(
        "-t",
        "--threads",
        default=MAX_THREADS_DEFAULT,
        type=int,
    )
    p.add_argument(
        "-p",
        "--patch-generator",
        help="Path to Python file that outputs metadata patch instructions from its "
        "_patch_repodata function or a .tar.bz2/.conda file which contains a "
        "patch_instructions.json file for each subdir",
    )
    p.add_argument("--verbose", help="show extra debugging info", action="store_true")
    p.add_argument(
        "--no-progress",
        help="Hide progress bars",
        action="store_false",
        dest="progress",
    )
    p.add_argument(
        "--current-index-versions-file",
        "-m",
        help="""
        YAML file containing name of package as key, and list of versions as values.  The current_index.json
        will contain the newest from this series of versions.  For example:

        python:
          - 3.8
          - 3.9

        will keep python 3.8.X and 3.9.Y in the current_index.json, instead of only the very latest python version.
        """,
    )

    args = p.parse_args(args)
    return p, args


def execute(args):
    _, args = parse_args(args)

    if len(args.dir) != 1 and args.output:
        print("Must specify exactly one input dir when using --output", file=sys.stderr)
        sys.exit(1)

    api.update_index(
        args.dir,
        output_dir=args.output,
        check_md5=args.check_md5,
        channel_name=args.channel_name,
        threads=args.threads,
        subdir=args.subdir,
        patch_generator=args.patch_generator,
        verbose=args.verbose,
        progress=args.progress,
        current_index_versions=args.current_index_versions_file,
    )


def main():
    logutil.configure()
    return execute(sys.argv[1:])
