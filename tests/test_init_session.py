import asyncio
import io
import os
from pathlib import Path
import stat
import warnings
from zipfile import ZipFile, ZipInfo

import pytest

from laxagent.eval_api.initialize import initialize_workspace, PackageError
from laxagent.eval_api.store import EvalStore
from laxagent.layout import get_project_workdir
from test_eval_api import eval_api


def package(entries):
    data = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with ZipFile(data, 'w') as archive:
            for name, content in entries:
                archive.writestr(name, content)
    data.seek(0)
    return data


def test_package_bytes_modes_directories_and_merge(tmp_path):
    workspace = tmp_path / 'project'
    script = ZipInfo('bin/test.sh')
    script.create_system = 3
    script.external_attr = (stat.S_IFREG | 0o755) << 16
    files = initialize_workspace(package([('src/main.py', b'bug'), (script, b'echo test'),
                                          ('empty/', b''), ('data.bin', b'\x00\xff')]), workspace)
    assert files == ['bin/test.sh', 'data.bin', 'src/main.py']
    assert (workspace / 'bin/test.sh').stat().st_mode & 0o777 == 0o755
    assert (workspace / 'empty').is_dir()
    assert (workspace / 'data.bin').read_bytes() == b'\x00\xff'
    initialize_workspace(package([('src/new.py', b'new')]), workspace)
    assert (workspace / 'src/main.py').read_bytes() == b'bug'
    assert (workspace / 'src/new.py').read_bytes() == b'new'


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../b', 'a//b', 'a/./b', 'C:/file', 'a\\b'])
def test_package_rejects_unsafe_paths_without_writes(tmp_path, name):
    workspace = tmp_path / 'project'
    with pytest.raises(PackageError):
        initialize_workspace(package([(name, b'data')]), workspace)
    assert not workspace.exists()


def test_package_rejects_bad_zip_duplicates_symlinks_and_crc(tmp_path):
    workspace = tmp_path / 'project'
    link = ZipInfo('link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    bad = [io.BytesIO(b'bad zip'), package([('a', b'a'), ('a', b'b')]),
           package([('a', b'a'), ('a/b', b'b')]), package([(link, b'../../escape')]),
           io.BytesIO(package([('a', b'original')]).getvalue().replace(b'original', b'corrupt!'))]
    for stream in bad:
        with pytest.raises(PackageError):
            initialize_workspace(stream, workspace)
        assert not workspace.exists()
        assert not list(tmp_path.glob('.eval-init-*'))


def test_package_existing_conflict_and_symlink_ancestor_do_not_write(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'existing').write_bytes(b'keep')
    with pytest.raises(PackageError, match='overwrite'):
        initialize_workspace(package([('new/file', b'new'), ('existing', b'replace')]), workspace)
    assert (workspace / 'existing').read_bytes() == b'keep'
    assert not (workspace / 'new').exists()
    outside = tmp_path / 'outside'
    (outside / 'sub').mkdir(parents=True)
    (workspace / 'link').symlink_to(outside)
    with pytest.raises(PackageError):
        initialize_workspace(package([('link/sub/file', b'bad')]), workspace)
    assert not (outside / 'sub/file').exists()


@pytest.mark.parametrize('limit', ['MAX_PACKAGE_BYTES', 'MAX_EXPANDED_BYTES', 'MAX_ENTRIES'])
def test_package_limits(tmp_path, monkeypatch, limit):
    monkeypatch.setattr('laxagent.eval_api.initialize.' + limit, 1)
    with pytest.raises(PackageError) as error:
        initialize_workspace(package([('one', b'abc'), ('two', b'def')]), tmp_path / 'project')
    assert error.value.status_code == 413
    assert not (tmp_path / 'project').exists()


def test_package_publish_failure_rolls_back_only_new_files(tmp_path, monkeypatch):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'old').write_bytes(b'keep')
    original = os.link
    def fail_second(source, target):
        if target.name == 'second':
            raise OSError('disk failure')
        original(source, target)
    monkeypatch.setattr(os, 'link', fail_second)
    with pytest.raises(OSError):
        initialize_workspace(package([('dir/first', b'a'), ('dir/second', b'b')]), workspace)
    assert list(workspace.iterdir()) == [workspace / 'old']


async def upload(h, session_id, raw=None):
    return await h.api.post('/eval/init_session', data={'session_id': str(session_id)},
                            files={'package': ('bug.zip', raw or package([('src/app.py', b'bug')]).getvalue(),
                                               'application/zip')})


@pytest.mark.anyio
async def test_init_multipart_and_no_overwrite(eval_api, conversation):
    h = eval_api
    response = await upload(h, conversation['session_id'])
    assert response.status_code == 200, response.text
    assert response.json() == {'session_id': conversation['session_id'], 'files': ['src/app.py']}
    workspace = get_project_workdir(conversation['user_id'], conversation['project_id'])
    assert (workspace / 'src/app.py').read_bytes() == b'bug'
    assert (await upload(h, conversation['session_id'])).status_code == 409
    assert (await upload(h, conversation['session_id'], b'not zip')).status_code == 400


@pytest.mark.anyio
async def test_init_rejects_missing_deleted_session_and_project_with_run(eval_api, conversation):
    h = eval_api
    assert (await upload(h, 999999999)).status_code == 404
    other = await h.repository.create_session(conversation['user_id'], conversation['project_id'], 'other')
    run = await h.repository.create_run(user_id=conversation['user_id'], session_id=other.id,
        user_query='question', model='example:model-name', context_limit=128000, output_limit=16000)
    assert (await upload(h, conversation['session_id'])).status_code == 409
    await h.repository.finish_run(run.id)
    assert (await upload(h, conversation['session_id'])).status_code == 409
    await h.repository.delete_session(conversation['user_id'], conversation['session_id'])
    assert (await upload(h, conversation['session_id'])).status_code == 404


@pytest.mark.anyio
async def test_init_serializes_chat_in_other_project_session(eval_api, conversation):
    h = eval_api
    other = await h.repository.create_session(conversation['user_id'], conversation['project_id'], 'other')
    async with EvalStore(h.repository.db).initialization(conversation['session_id']):
        task = asyncio.create_task(h.repository.create_run(user_id=conversation['user_id'], session_id=other.id,
            user_query='question', model='example:model-name', context_limit=128000, output_limit=16000))
        done, _ = await asyncio.wait({task}, timeout=0.1)
        assert not done
    assert (await asyncio.wait_for(task, 2)).session_id == other.id


@pytest.mark.anyio
async def test_init_form_validation(eval_api):
    assert (await eval_api.api.post('/eval/init_session', json={'session_id': 1})).status_code == 422
    assert (await upload(eval_api, 0)).status_code == 422


@pytest.mark.anyio
async def test_init_io_error_is_sanitized(eval_api, conversation, monkeypatch):
    def fail(*args):
        raise OSError('private filesystem data')
    monkeypatch.setattr('laxagent.eval_api.app.initialize_workspace', fail)
    response = await upload(eval_api, conversation['session_id'])
    assert response.status_code == 503
    assert 'private filesystem' not in response.text
