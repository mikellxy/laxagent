import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import time

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from sqlalchemy import update

from laxagent.appendices import DOCUMENT_TYPES, IMAGE_TYPES, materialize_input
from laxagent.appendix_text import MAX_TEXT_BYTES, extract_text
from laxagent.config import AppConfig
from laxagent.storage.database import DatabaseError
from laxagent.storage.models import Run
from laxagent.tools.context import ToolContext
from laxagent.tools.executor import execute_tool
from model_helpers import ModelStream, client_for, completed
from test_chat import chat_api, chat_path, payload, wait_run
from test_appendices import upload
from test_tool_loop import tool_response
from test_questions import answer_body, paused, question_response


def context(tmp_path, rows, **kwargs):
    repo = SimpleNamespace(get_tool_appendices=AsyncMock(return_value={row.id: row for row in rows}))
    return ToolContext(user_id='caller', repository=repo, workspace=str(tmp_path), **kwargs)


def row(tmp_path, name='note.txt', data=b'attachment text', identifier=1):
    path = tmp_path / name
    path.write_bytes(data)
    return SimpleNamespace(id=identifier, name=name, path=str(path), size_bytes=len(data),
                           media_type=(IMAGE_TYPES[Path(name).suffix][1] if Path(name).suffix in IMAGE_TYPES
                                       else DOCUMENT_TYPES[Path(name).suffix]))


@pytest.mark.anyio
async def test_wire_metadata_latest_user_and_no_history_io():
    refs = [{'id': 123, 'name': '旧文件.pdf'}]
    original = [{'role': 'user', 'content': 'previous', 'appendix': refs},
                {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'continue'}]
    before = copy.deepcopy(original)
    result = await materialize_input(original, 'owner', None)
    assert result[0]['content'] == 'previous\n\n---\n用户附件：\n' + json.dumps(refs, ensure_ascii=False) + '\n---'
    assert original == before
    assert await materialize_input(result, 'owner', None) == result
    # A non-multimodal main model receives metadata even for its latest message.
    assert (await materialize_input(original[:1], 'owner', None, supports_multimodal=False))[0] == result[0]


@pytest.mark.anyio
@pytest.mark.parametrize('extension', [suffix for suffix in DOCUMENT_TYPES if suffix != '.pdf'])
async def test_other_formats_are_metadata_only_and_ask_rejects_without_io(tmp_path, extension):
    file = row(tmp_path, 'document' + extension)
    Path(file.path).unlink()  # Metadata-only input must not even read the original.
    refs = [{'id': file.id, 'name': file.name}]
    original = [{'role': 'user', 'content': 'explain', 'appendix': refs}]
    before = copy.deepcopy(original)
    repo = SimpleNamespace(get_appendices=AsyncMock(return_value={file.id: file}))
    wire = await materialize_input(original, 'owner', repo)
    assert wire == [{'role': 'user', 'content': 'explain\n\n---\n用户附件：\n' + json.dumps(refs, ensure_ascii=False) + '\n---'}]
    assert original == before
    captured = []
    async with client_for(ModelStream([completed()]), captured) as client:
        image = row(tmp_path, 'image.png', identifier=2)
        ctx = context(tmp_path, [file, image], client=client, appendix_model='p:vision', appendix_supports_multimodal=True)
        for ids in ([1], [2, 1]):
            result = await execute_tool('ask_appendices', json.dumps({'ids': ids, 'question': 'q'}), context=ctx)
            assert result['error_code'] == 'APPENDIX_FORMAT_UNSUPPORTED'
    assert not captured


@pytest.mark.anyio
async def test_read_real_worker_and_unscoped_repository(repository, tmp_path):
    file = row(tmp_path)
    stored = await repository.create_appendix(user_id=str(uuid4()), name=file.name, path=file.path,
                                             media_type='text/plain', size_bytes=file.size_bytes)
    ctx = ToolContext(user_id='different-user', repository=repository, workspace=str(tmp_path))
    result = await execute_tool('read_appendix', json.dumps({'id': stored.id}), context=ctx)
    assert result == {'error_code': None, 'result': {'id': stored.id, 'name': file.name, 'content': 'attachment text'}}
    assert await repository.get_appendices('different-user', [stored.id]) == {}


def test_text_utf8_limits_and_source(tmp_path):
    file = row(tmp_path, 'source.html', b'\xef\xbb\xbf<script>example()</script>')
    assert extract_text(file.path)['content'] == '<script>example()</script>'
    Path(file.path).write_bytes(b'x' * MAX_TEXT_BYTES)
    assert extract_text(file.path)['content'] == 'x' * MAX_TEXT_BYTES
    Path(file.path).write_text('汉' * MAX_TEXT_BYTES)
    result = extract_text(file.path)['content']
    assert len(result.encode()) <= MAX_TEXT_BYTES and '文本已截断' in result and '\ufffd' not in result


def test_office_reading_order_and_formula_fallback(tmp_path):
    path = tmp_path / 'sample.docx'
    doc = Document()
    doc.add_paragraph('first')
    doc.add_table(rows=1, cols=1).cell(0, 0).text = 'middle'
    doc.add_paragraph('last')
    doc.save(path)
    text = extract_text(str(path))['content']
    assert text.index('first') < text.index('middle') < text.index('last')
    path = tmp_path / 'sample.xlsx'
    book = Workbook()
    book.active.title = 'sheet B'
    book.active['B2'] = 42
    book.active['C3'] = '=B2*2'
    book.create_sheet('sheet A')['A1'] = 'next'
    book.save(path)
    text = extract_text(str(path))['content']
    assert 'B2: 42' in text and 'C3: =B2*2' in text
    assert text.index('sheet B') < text.index('sheet A')
    path = tmp_path / 'sample.pptx'
    deck = Presentation()
    for label in ('first', 'second'):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = label
    ids = deck.slides._sldIdLst
    ids.insert(0, ids[1])
    deck.save(path)
    text = extract_text(str(path))['content']
    assert text.index('second') < text.index('first')


def test_blank_or_invalid_pdf_and_missing_file(tmp_path):
    path = tmp_path / 'blank.pdf'
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.write(path)
    assert extract_text(str(path))['error_code'] == 'APPENDIX_EXTRACTION_FAILED'
    path.write_bytes(b'%PDF-broken')
    assert extract_text(str(path))['error_code'] == 'APPENDIX_EXTRACTION_FAILED'
    path.unlink()
    assert extract_text(str(path))['error_code'] == 'APPENDIX_READ_FAILED'


def slow_extraction(path):
    Path(path + '.started').write_text('started')
    time.sleep(30)
    Path(path + '.finished').write_text('finished')
    return {'content': 'late result'}


@pytest.mark.anyio
async def test_read_worker_cancellation_stops_parsing(tmp_path, monkeypatch):
    file = row(tmp_path)
    monkeypatch.setattr('laxagent.tools.builtin.appendices.extract_text', slow_extraction)
    task = asyncio.create_task(execute_tool('read_appendix', '{"id":1}', context=context(tmp_path, [file])))
    try:
        async with asyncio.timeout(5):
            while not Path(file.path + '.started').exists():
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not Path(file.path + '.finished').exists()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_ask_single_isolated_call_with_originals_and_no_ownership(repository, tmp_path):
    file = row(tmp_path, 'note.pdf', b'%PDF-original')
    stored = await repository.create_appendix(user_id=str(uuid4()), name=file.name, path=file.path,
                                             media_type=file.media_type, size_bytes=file.size_bytes)
    captured = []
    stream = ModelStream([completed('found answer')])
    async with client_for(stream, captured) as client:
        ctx = ToolContext(user_id='different-user', repository=repository, workspace=str(tmp_path),
                          client=client, appendix_model='p:vision', appendix_supports_multimodal=True)
        result = await execute_tool('ask_appendices', json.dumps({'ids': [stored.id, stored.id], 'question': 'find fact'}), context=ctx)
    assert result == {'error_code': None, 'result': {'answer': 'found answer'}}
    assert stream.closed and len(captured) == 1
    request = captured[0]
    assert request['model'] == 'p:vision' and 'tools' not in request
    assert len(request['input']) == 2
    content = request['input'][1]['content']
    assert len(content) == 2 and content[1]['type'] == 'input_file'
    assert 'find fact' in content[0]['text'] and str(stored.id) in content[0]['text']
    assert 'base64' not in json.dumps(result)


@pytest.mark.anyio
async def test_tool_failures_are_safe_results(tmp_path):
    file = row(tmp_path)
    ctx = context(tmp_path, [file])
    assert (await execute_tool('ask_appendices', '{"ids":[1],"question":"q"}', context=ctx))['error_code'] == 'APPENDIX_MODEL_UNSUPPORTED'
    assert (await execute_tool('read_appendix', '{"id":2}', context=ctx))['error_code'] == 'APPENDIX_NOT_FOUND'
    ctx.repository.get_tool_appendices.side_effect = DatabaseError('secret database detail')
    assert (await execute_tool('read_appendix', '{"id":1}', context=ctx))['error_code'] == 'APPENDIX_STORAGE_FAILED'
    ctx = context(tmp_path, [row(tmp_path, 'image.png')])
    assert (await execute_tool('read_appendix', '{"id":1}', context=ctx))['error_code'] == 'APPENDIX_NO_TEXT'


@pytest.mark.anyio
@pytest.mark.parametrize('body', [{'ids': [], 'question': 'q'}, {'ids': [1]*6, 'question': 'q'},
    {'ids': [1], 'question': '  '}, {'ids': ['1'], 'question': 'q'}])
async def test_ask_invalid_arguments(tmp_path, body):
    assert (await execute_tool('ask_appendices', json.dumps(body), context=context(tmp_path, [])))['error_code'] == 'INVALID_ARGUMENTS'


@pytest.mark.anyio
async def test_ask_size_limit_and_upstream_failure(tmp_path):
    file = row(tmp_path, 'note.pdf')
    captured = []
    async with client_for(ModelStream([]), captured, status=400) as client:
        ctx = context(tmp_path, [file], client=client, appendix_model='p:vision', appendix_supports_multimodal=True)
        file.size_bytes = 20 * 1024 * 1024 + 1
        result = await execute_tool('ask_appendices', '{"ids":[1],"question":"q"}', context=ctx)
        assert result['error_code'] == 'APPENDIX_TOO_LARGE' and not captured
        file.size_bytes = 10
        result = await execute_tool('ask_appendices', '{"ids":[1],"question":"q"}', context=ctx)
        assert result['error_code'] == 'APPENDIX_MODEL_FAILED' and 'secret' not in json.dumps(result)


@pytest.mark.anyio
async def test_ask_cancel_closes_stream_without_result(tmp_path):
    entered = asyncio.Event()
    stream = ModelStream([], wait=entered)
    async with client_for(stream, []) as client:
        ctx = context(tmp_path, [row(tmp_path, 'note.pdf')], client=client, appendix_model='p:vision', appendix_supports_multimodal=True)
        task = asyncio.create_task(execute_tool('ask_appendices', '{"ids":[1],"question":"q"}', context=ctx))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


def vision_config(h):
    base = h.config.llm.provider_list[0].model_list[0]
    h.config.llm.provider_list[0].model_list.append(base.model_copy(update={'name': 'vision', 'supports_multimodal': True}))
    h.config.llm.appendix_model = 'example:vision'


@pytest.mark.anyio
async def test_text_main_vision_tool_and_snapshot_resume(chat_api):
    h = chat_api
    vision_config(h)
    ref = await upload(h)
    first = tool_response(('unused',))
    first['response']['output'][1].update(name='ask_appendices', arguments=json.dumps({'ids': [ref['id']], 'question': 'image fact'}))
    captured = []
    streams = [ModelStream([first]), ModelStream([completed('image fact answer')]), ModelStream([completed('final')])]
    async with client_for(streams, captured) as client:
        h.context.client = client
        response = await h.api.post(chat_path(h), json=payload(appendix=[ref]))
        assert response.status_code == 202
        run_id = response.json()['run_id']
        assert (await wait_run(h, run_id))['status'] == 'success'
    assert [r['model'] for r in captured] == ['example:model-name', 'example:vision', 'example:model-name']
    assert 'base64,' not in json.dumps(captured[0]) and 'base64,' not in json.dumps(captured[2])
    assert 'input_image' in json.dumps(captured[1]['input'])
    run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
    assert run.appendix_model == 'example:vision'
    saved = await h.graph.aget_state({'configurable': {'thread_id': run.thread_id}})
    assert saved.values['appendix_model'] == 'example:vision' and 'base64,' not in json.dumps(saved.values)
    assert (await h.api.get('/api/v1/models')).json()['appendix_model'] == 'example:vision'

    # Fail before calling the model; a changed global default must not change the run's choice.
    async with client_for(ModelStream([]), [], status=500) as client:
        h.context.client = client
        response = await h.api.post(chat_path(h), json=payload())
        run_id = response.json()['run_id']
        assert (await wait_run(h, run_id))['status'] == 'failed'
    h.config.llm.appendix_model = None
    resumed = []
    async with client_for([ModelStream([first]), ModelStream([completed('answer')]), ModelStream([completed('done')])], resumed) as client:
        h.context.client = client
        assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
    assert resumed[1]['model'] == 'example:vision'


def test_appendix_model_config_validation(config_data):
    assert AppConfig.model_validate(config_data).llm.appendix_model is None
    config_data['llm']['appendix_model'] = 'unknown:model'
    with pytest.raises(ValueError, match='appendix_model'):
        AppConfig.model_validate(config_data)
    config_data['llm']['appendix_model'] = 'example:model-name'
    with pytest.raises(ValueError, match='multimodal'):
        AppConfig.model_validate(config_data)
    config_data['llm']['provider_list'][0]['model_list'][0]['supports_multimodal'] = True
    assert AppConfig.model_validate(config_data).llm.appendix_model == 'example:model-name'


@pytest.mark.anyio
async def test_ask_cancel_resume_does_not_repeat_model_request(chat_api):
    h = chat_api
    vision_config(h)
    ref = await upload(h)
    response = tool_response(('unused',))
    response['response']['output'][1].update(name='ask_appendices', arguments=json.dumps({'ids': [ref['id']], 'question': 'q'}))
    entered = asyncio.Event()
    stalled = ModelStream([], wait=entered)
    captured = []
    async with client_for([ModelStream([response]), stalled, ModelStream([completed('recovered')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload(appendix=[ref]))).json()['run_id']
        await asyncio.wait_for(entered.wait(), 3)
        assert (await h.api.post(f'/api/v1/runs/{run_id}/cancel')).json()['status'] == 'cancelled'
        assert stalled.closed
        assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
    assert [r['model'] for r in captured].count('example:vision') == 1
    outputs = [i['output'] for i in captured[-1]['input'] if i.get('type') == 'function_call_output']
    assert len(outputs) == 1 and 'TOOL_STATE_UNCERTAIN' in outputs[0]


@pytest.mark.anyio
async def test_question_resume_preserves_attachment_model(chat_api):
    h = chat_api
    vision_config(h)
    ref = await upload(h)
    question = question_response(('ask_user_question', 'read_file'))
    question['response']['output'][2].update(name='ask_appendices', arguments=json.dumps({'ids': [ref['id']], 'question': 'q'}))
    captured = []
    async with client_for([ModelStream([question]), ModelStream([completed('image answer')]), ModelStream([completed('done')])], captured) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload(appendix=[ref]))).json()['run_id']
        _, interrupt_id = await paused(h, run_id)
        h.config.llm.appendix_model = None
        result = await h.api.post(f'/api/v1/runs/{run_id}/answers', json=answer_body(interrupt_id))
        assert result.status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
    assert captured[1]['model'] == 'example:vision'


@pytest.mark.anyio
async def test_legacy_run_without_attachment_snapshot_uses_original_main(chat_api):
    h = chat_api
    h.config.llm.provider_list[0].model_list[0].supports_multimodal = True
    ref = await upload(h)
    async with client_for(ModelStream([]), []) as client:
        h.context.client = client
        run_id = (await h.api.post(chat_path(h), json=payload(appendix=[ref]))).json()['run_id']
        assert (await wait_run(h, run_id))['status'] == 'failed'
    run = await h.context.repository.get_run(run_id, h.conversation['user_id'])
    async with h.context.repository.db.transaction() as tx:
        await tx.execute(update(Run).where(Run.id == run_id).values(appendix_model=None))
    # Simulate an old checkpoint without a usable attachment-model selection.
    await h.graph.aupdate_state({'configurable': {'thread_id': run.thread_id}}, {'appendix_model': None})
    vision_config(h)
    response = tool_response(('unused',))
    response['response']['output'][1].update(name='ask_appendices', arguments=json.dumps({'ids': [ref['id']], 'question': 'q'}))
    captured = []
    async with client_for([ModelStream([response]), ModelStream([completed('answer')]), ModelStream([completed('done')])], captured) as client:
        h.context.client = client
        assert (await h.api.post(f'/api/v1/runs/{run_id}/resume', json={'resume_time': 0})).status_code == 202
        assert (await wait_run(h, run_id))['status'] == 'success'
    assert len(captured) == 3 and all(r['model'] == run.model for r in captured)
