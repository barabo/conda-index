"""
cache conda indexing metadata in sqlite.
"""

import fnmatch
import json
import logging
import os
import os.path
import sqlite3
from functools import cached_property
from os.path import join
from zipfile import BadZipFile

import yaml
from conda_package_handling.utils import checksums
from yaml.constructor import ConstructorError
from yaml.parser import ParserError
from yaml.reader import ReaderError
from yaml.scanner import ScannerError

from . import convert_cache, package_streaming
from .common import connect

log = logging.getLogger(__name__)


INDEX_JSON_PATH = "info/index.json"
ICON_PATH = "info/icon.png"
PATHS_PATH = "info/paths.json"

TABLE_TO_PATH = {
    "index_json": INDEX_JSON_PATH,
    "about": "info/about.json",
    "paths": PATHS_PATH,
    # will use the first one encountered
    "recipe": (
        "info/recipe/meta.yaml",
        "info/recipe/meta.yaml.rendered",
        "info/meta.yaml",
    ),
    # run_exports is rare but used. see e.g. gstreamer.
    # prevents 90% of early tar.bz2 exits.
    # also found in meta.yaml['build']['run_exports']
    "run_exports": "info/run_exports.json",
    "post_install": "info/post_install.json",  # computed
    "icon": ICON_PATH,  # very rare, 16 conda-forge packages
    # recipe_log: always {} in old version of cache
}

PATH_TO_TABLE = {}

for k, v in TABLE_TO_PATH.items():
    if isinstance(v, str):
        PATH_TO_TABLE[v] = k
    else:
        for path in v:
            PATH_TO_TABLE[path] = k

# read, but not saved for later
TABLE_NO_CACHE = {
    "paths",
}

# saved to cache, not found in package
COMPUTED = {"info/post_install.json"}


class CondaIndexCache:
    upstream_stage = "fs"

    def __init__(self, channel_root, channel, subdir):
        """
        channel_root: directory containing platform subdir's, e.g. /clones/conda-forge
        channel: name of channel, e.g. 'main', 'conda-forge'
        subdir: platform subdir, e.g. 'linux-64'
        """
        self.channel_root = channel_root
        self.channel = channel
        self.subdir = subdir

        log.debug(f"CondaIndexCache {channel=}, {subdir=}")

        self.subdir_path = os.path.join(channel_root, subdir)
        self.cache_dir = os.path.join(self.subdir_path, ".cache")
        self.db_filename = os.path.join(self.cache_dir, "cache.db")
        self.cache_is_brand_new = not os.path.exists(self.db_filename)

        # the subdir should already exist
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)

        log.debug(f"{self.db_filename=} {self.cache_is_brand_new=}")

    def __getstate__(self):
        """
        Remove db connection when pickled.
        """
        return {k: self.__dict__[k] for k in self.__dict__ if k != "db"}

    def __setstate__(self, d):
        self.__dict__ = d

    @cached_property
    def db(self):
        """
        Connection to our sqlite3 database.
        """
        conn = connect(self.db_filename)
        with conn:
            convert_cache.create(conn)
            convert_cache.migrate(conn)
        return conn

    def convert(self, force=False):
        """
        Load filesystem cache into sqlite.
        """
        # if this is interrupted, we may have to re-extract the missing files
        if self.cache_is_brand_new or force:
            convert_cache.convert_cache(
                self.db,
                convert_cache.extract_cache_filesystem(self.cache_dir),
                override_channel=self.channel,
            )

            with self.db:
                convert_cache.remove_prefix(self.db)

    def extract_to_cache_2(self, channel_root, subdir, fn_info):
        """
        fn_info: object with .fn, .st_size, and .st_msize properties
        """
        return self.extract_to_cache(
            channel_root, subdir, fn_info.fn, stat_result=fn_info
        )

    def extract_to_cache(self, channel_root, subdir, fn, stat_result=None):
        return self._extract_to_cache(channel_root, subdir, fn, stat_result=stat_result)

    @property
    def database_prefix(self):
        """
        All paths must be prefixed with this string.
        """
        return ""

    @property
    def database_path_like(self):
        """
        Pass to LIKE to filter paths belonging to this subdir only.
        """
        return self.database_prefix + "%"

    def database_path(self, fn):
        return f"{self.database_prefix}{fn}"

    def plain_path(self, path):
        """
        path with any database-specfic prefix stripped off.
        """
        return path.rsplit("/", 1)[-1]

    def _extract_to_cache(
        self, channel_root, subdir, fn, second_try=False, stat_result=None
    ):
        # This method WILL reread the tarball. Probably need another one to exit early if
        # there are cases where it's fine not to reread.  Like if we just rebuild repodata
        # from the cached files, but don't use the existing repodata.json as a starting point.
        subdir_path = join(channel_root, subdir)

        abs_fn = join(subdir_path, fn)

        if stat_result is None:
            stat_result = os.stat(abs_fn)

        size = stat_result.st_size
        mtime = stat_result.st_mtime
        retval = fn, mtime, size, None

        try:
            # we no longer re-use the .conda cache for .tar.bz2
            # faster conda extraction should preserve enough performance
            database_path = self.database_path(fn)

            # None, or a tuple containing the row
            # don't bother skipping on second_try
            cached_row = self.db.execute(
                "SELECT index_json FROM index_json WHERE path = :path",
                {"path": database_path},
            ).fetchone()

            if cached_row and not second_try:
                # log in caller?
                log.debug("Found %s in cache" % fn)
                index_json = json.loads(cached_row[0])

                # XXX now would be a great time to check hashes

                # calculate extra stuff to add to index.json cache, size, md5, sha256

                # conda_package_handling wastes a stat call to give us this information
                # let's see how slow this is
                # md5, sha256 = checksums(abs_fn, ("md5", "sha256"))

                # if the hashes don't match, would be a great time to invalidate the index_json cache
                # assert index_json["sha256"] == sha256
                # assert index_json["md5"] == md5

                with self.db:
                    # have to update stat or we will be asked to look up cached_row again
                    # XXX DRY this query
                    self.db.execute(
                        """INSERT OR REPLACE INTO stat
                        (stage, path, mtime, size, sha256, md5)
                        VALUES ('indexed', ?, ?, ?, ?, ?)""",
                        (
                            database_path,
                            mtime,
                            size,
                            index_json["sha256"],
                            index_json["md5"],
                        ),
                    )

            else:
                log.debug("Extract %s to cache" % fn)
                index_json = self.extract_to_cache_unconditional(
                    fn, abs_fn, size, mtime
                )

            retval = fn, mtime, size, index_json

        except (
            KeyError,
            EOFError,
            json.JSONDecodeError,
            BadZipFile,  # stdlib zipfile
            OSError,  # stdlib tarfile: OSError: Invalid data stream
        ):
            log.exception("Error extracting %s", fn)
            if not second_try:
                # recursion XXX measure whether this ever succeeds on the second
                # try. could wait until next index run.
                return self._extract_to_cache(
                    channel_root,
                    subdir,
                    fn,
                    stat_result=stat_result,
                    second_try=True,
                )

        return retval

    def extract_to_cache_unconditional(self, fn, abs_fn, size, mtime):
        """
        Add or replace fn into cache, disregarding whether it is already cached.

        Return index.json as dict, with added size, checksums.
        """
        database_path = self.database_path(fn)

        wanted = set(PATH_TO_TABLE) - COMPUTED

        # when we see one of these, remove the rest from wanted
        recipe_want_one = {
            "info/recipe/meta.yaml.rendered",
            "info/recipe/meta.yaml",  # by far the most common
            "info/meta.yaml",
        }

        have = {}
        package_stream = iter(package_streaming.stream_conda_info(abs_fn))
        for tar, member in package_stream:
            if member.name in wanted:
                wanted.remove(member.name)
                have[member.name] = tar.extractfile(member).read()

                # immediately parse index.json, decide whether we need icon
                if member.name == INDEX_JSON_PATH:  # early exit when no icon
                    index_json = json.loads(have[member.name])
                    if index_json.get("icon") is None:
                        wanted = wanted - {ICON_PATH}

                if member.name in recipe_want_one:
                    # convert yaml; don't look for any more recipe files
                    have[member.name] = _cache_recipe(have[member.name])
                    wanted = wanted - recipe_want_one

            if not wanted:  # we got what we wanted
                package_stream.close()
                log.debug(f"%s early close", fn)

        if wanted and wanted != {"info/run_exports.json"}:
            # very common for some metadata to be missing
            log.debug(f"{fn} missing {wanted} has {set(have.keys())}")

        index_json = json.loads(have["info/index.json"])

        # populate run_exports.json (all False's if there was no
        # paths.json). paths.json should not be needed after this; don't
        # cache large paths.json unless we want a "search for paths"
        # feature unrelated to repodata.json
        try:
            paths_str = have.pop(PATHS_PATH)
        except KeyError:
            paths_str = ""
        have["info/post_install.json"] = _cache_post_install_details(paths_str)

        # calculate extra stuff to add to index.json cache, size, md5, sha256

        # conda_package_handling wastes a stat call to give us this information
        md5, sha256 = checksums(abs_fn, ("md5", "sha256"))

        with self.db:
            for have_path in have:
                table = PATH_TO_TABLE[have_path]
                if table in TABLE_NO_CACHE or table == "index_json":
                    continue  # not cached, or for index_json cached at end

                parameters = {"path": database_path, "data": have.get(have_path)}
                if have_path == ICON_PATH:
                    query = """
                                INSERT OR REPLACE into icon (path, icon_png)
                                VALUES (:path, :data)
                                """
                elif parameters["data"] is not None:
                    query = f"""
                                INSERT OR REPLACE INTO {table} (path, {table})
                                VALUES (:path, json(:data))
                                """
                else:
                    query = f"""DELETE FROM {table} WHERE path = :path"""
                try:
                    self.db.execute(query, parameters)
                except sqlite3.OperationalError:  # e.g. malformed json.
                    log.exception("table=%s parameters=%s", table, parameters)
                    # XXX delete from cache
                    raise

            # decide what fields to filter out, like has_prefix
            filter_fields = {
                "arch",
                "has_prefix",
                "mtime",
                "platform",
                "ucs",
                "requires_features",
                "binstar",
                "target-triplet",
                "machine",
                "operatingsystem",
            }
            for field_name in filter_fields & set(index_json):
                del index_json[field_name]

            new_info = {"md5": md5, "sha256": sha256, "size": size}

            index_json.update(new_info)

            # sqlite json() function removes whitespace
            self.db.execute(
                "INSERT OR REPLACE INTO index_json (path, index_json) VALUES (:path, json(:index_json))",
                {"path": database_path, "index_json": json.dumps(index_json)},
            )

            self.db.execute(
                """INSERT OR REPLACE INTO stat (stage, path, mtime, size, sha256, md5)
                VALUES ('indexed', ?, ?, ?, ?, ?)""",
                (database_path, mtime, size, index_json["sha256"], index_json["md5"]),
            )

        return index_json  # we don't need this return value; it will be queried back out to generate repodata

    def load_all_from_cache(self, fn):
        subdir_path = self.subdir_path

        try:
            # recent stat information must exist here...
            stat = self.db.execute(
                "SELECT mtime FROM stat WHERE stage=:upstream_stage AND path=:path",
                {"upstream_stage": self.upstream_stage, "path": self.database_path(fn)},
            ).fetchone()
            mtime = stat["mtime"]
        except (KeyError, IndexError):
            log.debug("%s mtime not found in cache", fn)
            try:
                mtime = mtime or os.stat(join(subdir_path, fn)).st_mtime
            except FileNotFoundError:
                # XXX don't call if it won't be found
                log.warn("%s not found in load_all_from_cache", fn)
                return {}

        # This method reads up pretty much all of the cached metadata, except
        # for paths. It all gets dumped into a single map.

        UNHOLY_UNION = """
        SELECT
            index_json,
            about,
            post_install,
            recipe,
            run_exports
            -- icon_png
        FROM
            index_json
            LEFT JOIN about USING (path)
            LEFT JOIN post_install USING (path)
            LEFT JOIN recipe USING (path)
            LEFT JOIN run_exports USING (path)
            -- LEFT JOIN icon USING (path)
        WHERE
            index_json.path = :path
        LIMIT 2
        """  # each table must USING (path) or will cross join

        rows = self.db.execute(
            UNHOLY_UNION, {"path": self.database_path(fn)}
        ).fetchall()
        assert len(rows) < 2
        try:
            row = rows[0]
        except IndexError:
            row = None
        data = {}
        # this order matches the old implementation. clobber recipe, about fields with index_json.
        for column in ("recipe", "about", "post_install", "index_json"):
            if row[column]:  # is not null or empty
                data.update(json.loads(row[column]))

        data["mtime"] = mtime

        source = data.get("source", {})
        try:
            data.update({"source_" + k: v for k, v in source.items()})
        except AttributeError:
            # sometimes source is a  list instead of a dict
            pass
        _clear_newline_chars(data, "description")
        _clear_newline_chars(data, "summary")

        # if run_exports was NULL / empty string, 'loads' the empty object
        data["run_exports"] = json.loads(row["run_exports"] or "{}")

        return data

    def save_fs_state(self, subdir_path):
        """
        stat all files in subdir_path to compare against cached repodata.
        """
        path_like = self.database_path_like

        # gather conda package filenames in subdir
        log.debug("%s listdir", self.subdir)
        fns_in_subdir = {
            fn for fn in os.listdir(subdir_path) if fn.endswith((".conda", ".tar.bz2"))
        }

        # put filesystem 'ground truth' into stat table
        # will we eventually stat everything on fs, or can we shortcut for new?
        def listdir_stat():
            for fn in fns_in_subdir:
                abs_fn = os.path.join(subdir_path, fn)
                stat = os.stat(abs_fn)
                yield {
                    "path": self.database_path(fn),
                    "mtime": int(stat.st_mtime),
                    "size": stat.st_size,
                }

        log.debug("%s save fs state", self.subdir)
        with self.db:
            self.db.execute(
                "DELETE FROM stat WHERE stage='fs' AND path like :path_like",
                {"path_like": path_like},
            )
            self.db.executemany(
                """
            INSERT INTO STAT (stage, path, mtime, size)
            VALUES ('fs', :path, :mtime, :size)
            """,
                listdir_stat(),
            )

    def changed_packages(self):
        """
        Compare 'fs' to 'indexed' state.

        Return packages in 'fs' that are changed or missing compared to 'indexed'.
        """
        return self.db.execute(
            """
            WITH
            fs AS
                ( SELECT path, mtime, size FROM stat WHERE stage = 'fs' ),
            cached AS
                ( SELECT path, mtime FROM stat WHERE stage = 'indexed' )

            SELECT fs.path, fs.mtime, fs.size from fs LEFT JOIN cached USING (path)

            WHERE fs.path LIKE :path_like AND
                (fs.mtime != cached.mtime OR cached.path IS NULL)
        """,
            {"path_like": self.database_path_like},
        )  # XXX compare checksums if available, and prefer to mtimes


def _cache_post_install_details(paths_json_str):
    post_install_details_json = {
        "binary_prefix": False,
        "text_prefix": False,
        "activate.d": False,
        "deactivate.d": False,
        "pre_link": False,
        "post_link": False,
        "pre_unlink": False,
    }
    if paths_json_str:  # if paths exists at all
        paths = json.loads(paths_json_str).get("paths", [])

        # get embedded prefix data from paths.json
        for f in paths:
            if f.get("prefix_placeholder"):
                if f.get("file_mode") == "binary":
                    post_install_details_json["binary_prefix"] = True
                elif f.get("file_mode") == "text":
                    post_install_details_json["text_prefix"] = True
            # check for any activate.d/deactivate.d scripts
            for k in ("activate.d", "deactivate.d"):
                if not post_install_details_json.get(k) and f["_path"].startswith(
                    "etc/conda/%s" % k
                ):
                    post_install_details_json[k] = True
            # check for any link scripts
            for pat in ("pre-link", "post-link", "pre-unlink"):
                if not post_install_details_json.get(pat) and fnmatch.fnmatch(
                    f["_path"], "*/.*-%s.*" % pat
                ):
                    post_install_details_json[pat.replace("-", "_")] = True

    return json.dumps(post_install_details_json)


def _cache_recipe(recipe_reader):
    recipe_json = {}

    try:
        recipe_json = yaml.safe_load(recipe_reader)
    except (ConstructorError, ParserError, ScannerError, ReaderError):
        pass

    try:
        recipe_json_str = json.dumps(recipe_json)
    except TypeError:
        recipe_json.get("requirements", {}).pop("build")  # XXX weird
        recipe_json_str = json.dumps(recipe_json)

    return recipe_json_str


def _clear_newline_chars(record, field_name):
    if field_name in record:
        try:
            record[field_name] = record[field_name].strip().replace("\n", " ")
        except AttributeError:
            # sometimes description gets added as a list instead of just a string
            record[field_name] = record[field_name][0].strip().replace("\n", " ")
