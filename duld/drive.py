import asyncio
import concurrent.futures as cf
import contextlib as cl
import functools as ft
import json
import multiprocessing as mp
import os.path as op
import pathlib
import re
import threading

import aiohttp
from wcpan.drive.cli.util import get_media_info
from wcpan.drive.core.drive import DriveFactory
from wcpan.drive.core.util import upload_from_local
from wcpan.logger import DEBUG, INFO, ERROR, EXCEPTION, WARNING
import wcpan.worker as ww

from . import settings


RETRY_TIMES = 3


class DriveUploader(object):

    def __init__(self):
        self._jobs = set()
        self._sync_lock = asyncio.Lock()
        self._drive = None
        self._curl = None
        self._queue = None
        self._pool = None
        self._raii = None

    async def __aenter__(self):
        async with cl.AsyncExitStack() as stack:
            factory = DriveFactory()
            factory.load_config()
            self._drive = await stack.enter_async_context(factory())
            self._queue = await stack.enter_async_context(ww.AsyncQueue(8))
            self._curl = await stack.enter_async_context(aiohttp.ClientSession())
            self._pool = stack.enter_context(cf.ProcessPoolExecutor())
            self._raii = stack.pop_all()
        return self

    async def __aexit__(self, type_, exc, tb):
        await self._raii.aclose()
        self._drive = None
        self._curl = None
        self._queue = None
        self._pool = None
        self._raii = None

    async def upload_path(self, remote_path, local_path):
        if local_path in self._jobs:
            WARNING('duld') << local_path << 'is still uploading'
            return False

        with job_guard(self._jobs, local_path):
            await self._sync()

            node = await self._drive.get_node_by_path(remote_path)
            if not node:
                ERROR('duld') << remote_path << 'not found'
                return False

            local_path = pathlib.Path(local_path)
            ok = await self._upload(node, local_path)
            if not ok:
                ERROR('duld') << local_path << 'upload failed'
            return ok

    async def upload_torrent(self, remote_path, torrent_id, torrent_root, root_items):
        if torrent_id in self._jobs:
            WARNING('duld') << torrent_id << 'is still uploading'
            return False

        with job_guard(self._jobs, torrent_id):
            await self._sync()

            node = await self._drive.get_node_by_path(remote_path)
            if not node:
                ERROR('duld') << remote_path << 'not found'
                return False

            # files/directories to be upload
            items = map(lambda _: pathlib.Path(torrent_root, _), root_items)
            all_ok = True
            for item in items:
                ok = await self._upload(node, item)
                if not ok:
                    ERROR('duld') << item << 'upload failed'
                    all_ok = False
                    continue

            return all_ok

    async def _sync(self):
        async with self._sync_lock:
            await asyncio.sleep(1)
            count = 0
            async for changes in self._drive.sync():
                count += 1
            INFO('duld') << 'sync' << count

    async def _upload(self, node, local_path):
        if await self._should_exclude(local_path.name):
            INFO('duld') << 'excluded' << local_path
            return True

        if not local_path.exists():
            WARNING('duld') << 'cannot upload non-exist path' << local_path
            return False

        if local_path.is_dir():
            ok = await self._upload_directory(node, local_path)
        else:
            ok = await self._upload_file_retry(node, local_path)
        return ok

    async def _upload_directory(self, node, local_path):
        dir_name = local_path.name

        # find or create remote directory
        child_node = await self._drive.get_node_by_name_from_parent(dir_name, node)
        if child_node and child_node.is_file:
            # is file
            path = await self._drive.get_path(child_node)
            ERROR('duld') << '(remote)' << path << 'is a file'
            return False
        elif not child_node or child_node.trashed or node.trashed:
            # not exists
            child_node = await self._drive.create_folder(node, dir_name)
            if not child_node:
                path = await self._drive.get_path(node)
                path = op.join(path, dir_name)
                ERROR('duld') << '(remote) cannot create' << path
                return False

            # Need to update local cache for the added folder.
            # In theory we should pass remote path instead of doing this.
            while True:
                try:
                    await self._drive.get_path(child_node)
                    break
                except Exception:
                    pass
                await self._sync()

        all_ok = True
        for child_path in local_path.iterdir():
            ok = await self._upload(child_node, child_path)
            if not ok:
                ERROR('duld') << '(remote) cannot upload' << child_path
                all_ok = False

        return all_ok

    async def _upload_file_retry(self, node, local_path):
        for _ in range(RETRY_TIMES):
            try:
                ok = await self._upload_file(node, local_path)
            except Exception as e:
                EXCEPTION('duld', e) << 'retry upload file'
            else:
                return ok

            await self._sync()
        else:
            ERROR('duld') << f'tried upload {RETRY_TIMES} times'
            return False

    async def _upload_file(self, node, local_path):
        file_name = local_path.name
        remote_path = await self._drive.get_path(node)
        remote_path = pathlib.Path(remote_path, file_name)

        child_node = await self._drive.get_node_by_name_from_parent(file_name, node)

        if child_node and not child_node.trashed:
            if child_node.is_folder:
                ERROR('duld') << '(remote)' << remote_path << 'is a directory'
                return False

            # check integrity
            ok = await self._verify_remote_file(local_path, remote_path, child_node.hash_)
            if not ok:
                return False
            INFO('duld') << remote_path << 'already exists'

        if not child_node or child_node.trashed:
            INFO('duld') << 'uploading' << remote_path

            media_info = await get_media_info(local_path)
            child_node = await upload_from_local(
                self._drive,
                node,
                local_path,
                media_info,
            )

            # check integrity
            ok = await self._verify_remote_file(local_path, remote_path, child_node.hash_)
            if not ok:
                return False

        return True

    async def _verify_remote_file(self, local_path, remote_path, remote_hash):
        loop = asyncio.get_running_loop()
        hasher = await self._drive.get_hasher()
        local_hash = await loop.run_in_executor(
            self._pool,
            md5sum,
            hasher,
            local_path,
        )
        if local_hash != remote_hash:
            ERROR('duld') << f'(remote) {remote_path} has a different hash ({local_hash}, {remote_hash})'
            return False
        return True

    # used in exception handler, DO NOT throw another exception again
    async def _try_resolve_name_confliction(self, node, local_path):
        name = op.basename(local_path)
        node = await self._drive.get_node_by_name_from_parent(name, node)
        if not node:
            return True
        try:
            await self._drive.trash_node_by_id(node.id_)
            return True
        except Exception as e:
            EXCEPTION('duld', e)
        return False

    async def _should_exclude(self, name):
        for pattern in settings['exclude_pattern']:
            if re.match(pattern, name, re.IGNORECASE):
                return True

        if settings['exclude_url']:
            async with self._curl.get(settings['exclude_url']) as rv:
                rv = await rv.json()
                for _, pattern in rv.items():
                    if re.match(pattern, name, re.IGNORECASE):
                        return True

        return False


def md5sum(hasher, path):
    with path.open('rb') as fin:
        while True:
            chunk = fin.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


@cl.contextmanager
def job_guard(set_, token):
    set_.add(token)
    try:
        yield
    finally:
        set_.discard(token)
