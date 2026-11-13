import base64
import io
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from PIL import Image
from sqlalchemy import select

from laxagent.appendices import InvalidAppendix, MAX_FILE_BYTES, materialize_input, validate_file
from laxagent.layout import get_appendices_dir, get_project_workdir
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Appendix, Message, Run, SessionMessage
from test_chat import chat_api, chat_path, payload, wait_run


def picture(format='PNG'):
    stream = io.BytesIO()
    Image.new('RGB', (600, 300), 'red').save(stream, format)
    return stream.getvalue()


def office(extension):
    stream = io.BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr({'docx': 'word/document.xml', 'xlsx': 'xl/workbook.xml', 'pptx': 'ppt/presentation.xml'}[extension], '<root/>')
    return stream.getvalue()


@pytest.mark.parametrize('extension,data', [
    ('png', picture()), ('jpg', picture('JPEG')), ('jpeg', picture('JPEG')),
    ('webp', picture('WEBP')), ('gif', picture('GIF')), ('pdf', b'%PDF-1.4\n%%EOF'),
    ('txt', b'hello'), ('md', b'# hello'), ('csv', b'a,b\n1,2'),
    *[(extension, office(extension)) for extension in ('docx', 'xlsx', 'pptx')],
])
def test_allowed_formats(extension, data):
    media_type, thumbnail = validate_file(f'example.{extension.upper()}', data)
    if media_type.startswith('image/'):
        with Image.open(io.BytesIO(thumbnail)) as image:
            assert image.size == (256, 128) and image.format == 'WEBP'
    else:
        assert thumbnail is None


@pytest.mark.parametrize('name,data', [('bad.png', b'not image'), ('bad.jpg', picture()), ('bad.pdf', b'text'),
    ('bad.docx', office('xlsx')), ('bad.xlsx', b'not zip'), ('bad.txt', b'\xff'), ('bad.md', b'a\x00b'),
    ('bad.svg', b'<svg/>'), ('empty.txt', b''), ('file.zip', office('docx'))])
def test_reject_mismatched_content(name, data):
    with pytest.raises(InvalidAppendix):
        validate_file(name, data)


def test_animated_gif_rejected():
    stream = io.BytesIO()
    Image.new('RGB', (5, 5), 'red').save(stream, 'GIF', save_all=True, append_images=[Image.new('RGB', (5, 5), 'blue')])
    with pytest.raises(InvalidAppendix):
        validate_file('animation.gif', stream.getvalue())


async def upload(h, name='image.png', data=None):
    response = await h.api.post('/api/v1/appendices', files={'file': (name, picture() if data is None else data, 'application/octet-stream')})
    assert response.status_code == 201, response.text
    assert set(response.json()) == {'id', 'name'}
    return response.json()


@pytest.mark.anyio
async def test_upload_ownership_limits_and_failed_commit_cleanup(chat_api, monkeypatch):
    h = chat_api
    first, second = await upload(h), await upload(h)
    assert first['id'] != second['id']
    stored = await h.context.repository.get_appendices(h.conversation['user_id'], [first['id'], second['id']])
    assert len({item.path for item in stored.values()}) == 2
    assert not Path(stored[first['id']].path).is_relative_to(get_project_workdir(h.conversation['user_id'], h.conversation['project_id']))
    result = await h.api.get(f"/api/v1/appendices/{first['id']}")
    assert result.content == picture() and result.headers['content-type'] == 'image/png'
    assert (await h.api.post('/api/v1/appendices', files={'file': ('bad.pdf', b'fake')})).status_code == 415
    assert (await h.api.post('/api/v1/appendices', files={'file': ('big.txt', b'x' * (MAX_FILE_BYTES + 1))})).status_code == 413
    boundary = await upload(h, 'limit.txt', b'x' * MAX_FILE_BYTES)
    assert boundary['id']
    directory = get_appendices_dir(h.conversation['user_id'])
    before = set(directory.iterdir())
    async def fail(**kwargs):
        raise DatabaseError()
    monkeypatch.setattr(h.context.repository, 'create_appendix', fail)
    assert (await h.api.post('/api/v1/appendices', files={'file': ('no.txt', b'hello')})).status_code == 503
    assert set(directory.iterdir()) == before
    other = (await h.api.post('/api/v1/login', json={'user_id': str(uuid4())})).json()['access_token']
    assert (await h.api.get(f"/api/v1/appendices/{first['id']}", headers={'Authorization': f'Bearer {other}'})).status_code == 404
    assert (await h.api.get(f"/api/v1/appendices/{first['id']}", headers={'Authorization': ''})).status_code == 401


@pytest.mark.anyio
async def test_chat_history_model_input_and_checkpoint_references(chat_api):
    h = chat_api
    h.config.llm.provider_list[0].model_list[0].supports_multimodal = True
    image = await upload(h)
    document = await upload(h, 'notes.txt', b'the answer is 42')
    references = [image, document]
    response = await h.api.post(chat_path(h), json=payload(user_query='', appendix=[{**image, 'name': 'forged.png'}, document]))
    assert response.status_code == 202
    run_id = response.json()['run_id']
    assert (await wait_run(h, run_id))['status'] == 'success'
    run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
    assert run.appendix == references
    user_input = next(message for message in h.captured[0]['input'] if message.get('role') == 'user')
    assert [part['type'] for part in user_input['content']] == ['input_image', 'input_file']
    assert base64.b64decode(user_input['content'][0]['image_url'].split(',', 1)[1]) == picture()
    assert user_input['content'][1]['filename'] == 'notes.txt'
    assert 'appendix' not in user_input
    snapshot = await h.graph.aget_state({'configurable': {'thread_id': run.thread_id}})
    assert 'base64' not in json.dumps(snapshot.values)
    assert snapshot.values['model_input'][1]['appendix'] == references
    async with h.context.repository.db.transaction() as tx:
        for model in (Message, SessionMessage):
            user = await tx.scalar(select(model).where(model.session_id == run.session_id, model.role == 'user'))
            assert user.appendix == references
    path = f'/api/v1/sessions/{run.session_id}'
    assert (await h.api.get(path)).json()['has_appendix'] is True
    history = (await h.api.get(path + '/messages')).json()['items'][0]
    assert history['appendix'][0]['thumbnail_data_url'].startswith('data:image/webp;base64,')
    assert history['appendix'][1] == {**document, 'kind': 'file', 'thumbnail_data_url': None}
    assert 'path' not in json.dumps(history)
    second = await h.api.post(chat_path(h), json=payload(user_query='What about that image?'))
    assert (await wait_run(h, second.json()['run_id']))['status'] == 'success'
    assert next(m for m in h.captured[-1]['input'] if m.get('role') == 'user') == user_input
    h.config.llm.provider_list[0].model_list[0].supports_multimodal = False
    assert (await h.api.post(chat_path(h), json=payload())).status_code == 422
    # Even when the current page has only the last assistant message, the session flag persists.
    assert len((await h.api.get(path + '/messages?limit=1')).json()['items']) == 1
    assert (await h.api.get(path)).json()['has_appendix'] is True


@pytest.mark.anyio
async def test_chat_rejects_invalid_references_before_creating_run(chat_api):
    h = chat_api
    ref = await upload(h)
    assert (await h.api.post(chat_path(h), json=payload(appendix=[ref]))).status_code == 422
    h.config.llm.provider_list[0].model_list[0].supports_multimodal = True
    for refs, status in [([ref] * 2, 422), ([ref] * 6, 422), ([{'id': 987654, 'name': 'missing'}], 404)]:
        assert (await h.api.post(chat_path(h), json=payload(appendix=refs))).status_code == status
    other = await h.context.repository.create_appendix(user_id=str(uuid4()), name='other.txt', path='/not/read', media_type='text/plain', size_bytes=1)
    assert (await h.api.post(chat_path(h), json=payload(appendix=[{'id': other.id, 'name': other.name}]))).status_code == 404
    refs = [await upload(h, f'{i}.txt', b'x' * MAX_FILE_BYTES) for i in range(2)]
    assert (await h.api.post(chat_path(h), json=payload(appendix=[*refs, ref]))).status_code == 422
    async with h.context.repository.db.transaction() as tx:
        assert await tx.scalar(select(Run.id)) is None
    response = await h.api.post(chat_path(h), json=payload(appendix=refs))
    assert response.status_code == 202
    assert (await wait_run(h, response.json()['run_id']))['status'] == 'success'


@pytest.mark.anyio
async def test_missing_original_fails_then_resumes_and_missing_thumbnail_does_not_break_history(chat_api):
    h = chat_api
    h.config.llm.provider_list[0].model_list[0].supports_multimodal = True
    ref = await upload(h)
    row = (await h.context.repository.get_appendices(h.conversation['user_id'], [ref['id']]))[ref['id']]
    Path(row.path).unlink()
    Path(row.thumbnail_path).unlink()
    response = await h.api.post(chat_path(h), json=payload(appendix=[ref]))
    run_id = response.json()['run_id']
    assert (await wait_run(h, run_id))['error_code'] == 'attachment_read_failed'
    assert not h.captured
    page = await h.api.get(f"/api/v1/sessions/{h.conversation['session_id']}/messages")
    assert page.status_code == 200 and page.json()['items'][0]['appendix'][0]['thumbnail_data_url'] is None
    assert (await h.api.get(f"/api/v1/appendices/{ref['id']}")).status_code == 404
    Path(row.path).write_bytes(picture())
    assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
    assert (await wait_run(h, run_id))['status'] == 'success'
    async with h.context.repository.db.transaction() as tx:
        users = list(await tx.scalars(select(Message).where(Message.role == 'user')))
        assert len(users) == 1 and users[0].appendix == [ref]


@pytest.mark.anyio
async def test_old_input_needs_no_attachment_lookup():
    original = [{'role': 'user', 'content': 'legacy'}]
    assert await materialize_input(original, 'owner', None) == original
