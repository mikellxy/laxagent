import io
from dataclasses import replace
import os
from uuid import uuid4

import pytest
from PIL import Image

from laxagent.layout import get_project_workdir
from laxagent.tools.builtin.edit_file import EditFileArgs, EditFileError, edit_file
from laxagent.tools.builtin.write_file import WriteFileArgs, write_file
from laxagent.tools.builtin.list_written_files import ListWrittenFilesArgs, list_written_files
from laxagent.workspace_files import SHORT_BYTES, TEXT_BYTES, IMAGE_BYTES, open_file, preview, WorkspaceFileError
from test_chat import chat_api
from tool_helpers import tool_context


def test_candidate_paths_are_ordered_deduplicated_and_include_noops(tmp_path):
    context = replace(tool_context(tmp_path), user_id=str(uuid4()))
    for path in ['./nested//a.txt', 'b.txt', 'nested/a.txt']:
        write_file(context, WriteFileArgs(path=path, content='same'))
    (tmp_path / 'existing.txt').write_text('same')
    edit_file(context, EditFileArgs(path='existing.txt', edits=[{'old_text': 'same', 'new_text': 'same'}]))
    edit_file(context, EditFileArgs(path='b.txt', edits=[{'old_text': 'same', 'new_text': ''}]))
    with pytest.raises(EditFileError):
        edit_file(context, EditFileArgs(path='missing', edits=[{'old_text': 'a', 'new_text': 'b'}]))
    write_file(context, WriteFileArgs(path='empty', content=''))
    assert list_written_files(context, ListWrittenFilesArgs()) == {
        'paths': ['nested/a.txt', 'b.txt', 'existing.txt', 'empty'],
    }
    write_file(context, WriteFileArgs(skill_name='demo', path='extra.txt', content='skill'))
    assert 'extra.txt' not in context.written_workspace_paths
    result = list_written_files(context, ListWrittenFilesArgs())
    result['paths'].clear()
    assert context.written_workspace_paths
    assert not tool_context(tmp_path).written_workspace_paths


def picture():
    data = io.BytesIO()
    Image.new('RGB', (20, 10), 'red').save(data, 'PNG')
    return data.getvalue()


@pytest.mark.anyio
async def test_current_file_preview_download_limits_and_ownership(chat_api):
    h = chat_api
    root = get_project_workdir(h.conversation['user_id'], h.conversation['project_id'])
    root.mkdir(parents=True)
    base = f"/api/v1/sessions/{h.conversation['session_id']}/workspace-files"
    data = {'中文 #?%.py': b'print(42)', 'empty.txt': b'', 'image.png': picture(),
            'page.html': b'<script>alert(1)</script>', 'notes.md': b'# Title',
            'binary': b'\x00\xff', 'report.pdf': b'%PDF-1.7\nhello',
            'boundary.txt': b'a' * TEXT_BYTES, 'large.txt': b'a' * (TEXT_BYTES + 1),
            'short.txt': b'a' * (SHORT_BYTES - 1) + '中'.encode(),
            'large.png': picture() + b'0' * IMAGE_BYTES}
    for name, content in data.items():
        (root / name).write_bytes(content)
    for name in data:
        response = await h.api.get(base + '/preview', params={'path': name})
        assert response.status_code == 200, response.text
        info = response.json()
        assert info['path'] == name and info['size'] == len(data[name])
        assert info['kind'] == ('image' if name == 'image.png' else 'download' if name in ('binary', 'report.pdf', 'large.png') else 'text')
        original = await h.api.get(base + '/content', params={'path': name, 'download': 'true'})
        assert original.content == data[name]
        assert 'attachment;' in original.headers['content-disposition']
        assert original.headers['cache-control'] == 'private, no-store'
    text = (await h.api.get(base + '/preview', params={'path': 'short.txt'})).json()
    assert text['truncated'] and text['text'] == 'a' * (SHORT_BYTES - 1)
    assert (await h.api.get(base + '/content', params={'path': 'large.txt'})).status_code == 413
    assert (await h.api.get(base + '/content', params={'path': 'boundary.txt'})).status_code == 200
    assert (await h.api.get(base + '/content', params={'path': 'report.pdf'})).status_code == 415
    html = await h.api.get(base + '/content', params={'path': 'page.html'})
    assert html.headers['content-type'].startswith('text/plain') and html.headers['x-content-type-options'] == 'nosniff'
    (root / 'notes.md').write_text('current')
    assert (await h.api.get(base + '/preview', params={'path': 'notes.md'})).json()['text'] == 'current'
    (root / 'notes.md').unlink()
    assert (await h.api.get(base + '/preview', params={'path': 'notes.md'})).status_code == 404
    other = (await h.api.post('/api/v1/login', json={'user_id': str(uuid4())})).json()['access_token']
    assert (await h.api.get(base + '/preview', params={'path': 'empty.txt'}, headers={'Authorization': f'Bearer {other}'})).status_code == 404
    assert (await h.api.get(base + '/content', params={'path': 'empty.txt'}, headers={'Authorization': ''})).status_code == 401
    assert (await h.api.delete(f"/api/v1/sessions/{h.conversation['session_id']}")).status_code == 204
    assert (await h.api.get(base + '/content', params={'path': 'empty.txt'})).status_code == 404


@pytest.mark.anyio
async def test_file_routes_reject_escapes_and_special_files(chat_api, tmp_path):
    h = chat_api
    root = get_project_workdir(h.conversation['user_id'], h.conversation['project_id'])
    root.mkdir(parents=True)
    (tmp_path / 'secret').write_text('secret')
    (root / 'link').symlink_to(tmp_path / 'secret')
    (root / 'dirlink').symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / 'fifo')
    (root / 'dir').mkdir()
    base = f"/api/v1/sessions/{h.conversation['session_id']}/workspace-files"
    for path in ['../secret', str(tmp_path / 'secret'), 'x\x00y', 'link', 'dirlink/secret', 'fifo', 'dir']:
        for suffix in ['/preview', '/content']:
            assert (await h.api.get(base + suffix, params={'path': path})).status_code == 400
    # No second percent-decoding: this is a literal name, not a traversal.
    assert (await h.api.get(base + '/preview', params={'path': '%2e%2e/secret'})).status_code == 404


def test_open_descriptor_survives_path_swap_and_rejects_ancestor_symlink(tmp_path):
    user = str(uuid4())
    root = get_project_workdir(user, 1)
    root.mkdir(parents=True)
    (root / 'file').write_text('original')
    path, file = open_file(user, 1, 'file')
    with file:
        (root / 'file').unlink()
        (tmp_path / 'outside').write_text('secret')
        (root / 'file').symlink_to(tmp_path / 'outside')
        assert preview(file, path)['text'] == 'original'
    root.rename(root.with_name('moved'))
    root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceFileError):
        open_file(user, 1, 'outside')
