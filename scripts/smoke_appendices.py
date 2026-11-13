"""Run against the real service inside Lima; retain the fresh smoke user and records.

uv run python scripts/smoke_appendices.py --config config/config.json --document-format pdf

Known provider compatibility: ark:glm-5.3-flash accepts images and PDF; its
input_file handling rejects TXT as invalid PDF. Other non-PDF formats have not
passed live acceptance. The application deliberately forwards originals without
conversion; use --document-format txt to test a provider with native text-file support.
"""
import argparse
import asyncio
import io
import json
import socket
from pathlib import Path
from uuid import uuid4

import httpx2
from PIL import Image, ImageDraw
from sqlalchemy import select

from laxagent.config import load_config
from laxagent.layout import get_project_workdir
from laxagent.storage.database import Database
from laxagent.storage.models import Message, SessionMessage
from laxagent.storage.repositories import Repository
from laxagent.storage.checkpoints import open_checkpointer


def pdf_document(marker: str) -> bytes:
    # A single-page synthetic fixture; no document authoring dependency is needed.
    content = f"BT /F1 24 Tf 50 700 Td ({marker}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        result += f"{offset:010} 00000 n \n".encode()
    return result + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


async def terminal(api, run_id):
    async with asyncio.timeout(240):
        while True:
            response = await api.get(f'/api/v1/runs/{run_id}')
            response.raise_for_status()
            run = response.json()
            if run['status'] in ('success', 'failed', 'cancelled'):
                print(json.dumps({'run_id': run_id, 'status': run['status'], 'error_code': run['error_code']}), flush=True)
                return run
            await asyncio.sleep(0.5)


async def smoke(args, config=None):
    config = config or load_config(args.config)
    model = args.model or config.llm.default_model
    user_id = str(uuid4())
    image_marker, document_marker = 'IMAGE7319', f'DOC{uuid4().hex[:8].upper()}'
    image = Image.new('RGB', (640, 360), 'white')
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((35, 35, 180, 180), fill='red')
    drawing.text((35, 240), image_marker, fill='black', font_size=54)
    buffer = io.BytesIO(); image.save(buffer, 'PNG'); image_bytes = buffer.getvalue()
    async with Database.open(config.database) as db, httpx2.AsyncClient(
        base_url=f'http://127.0.0.1:{config.api_server.port}', timeout=240, trust_env=False,
    ) as api:
        repository = Repository(db)
        login = await api.post('/api/v1/login', json={'user_id': user_id, 'from_page': 'guest_login'})
        login.raise_for_status()
        api.headers['Authorization'] = f"Bearer {login.json()['access_token']}"
        session_id = login.json()['initial_session_id']
        session, _ = await repository.session_detail(user_id, session_id)
        print(json.dumps({'session_id': session_id, 'model': model}), flush=True)
        catalog = (await api.get('/api/v1/models')).json()
        assert next(item for item in catalog['models'] if item['id'] == model)['supports_multimodal']

        async def upload(name, data):
            response = await api.post('/api/v1/appendices', files={'file': (name, data)})
            response.raise_for_status()
            ref = response.json()
            assert set(ref) == {'id', 'name'}
            downloaded = await api.get(f"/api/v1/appendices/{ref['id']}")
            assert downloaded.content == data
            return ref

        async def chat(query, refs=(), expected='success'):
            response = await api.post(f'/api/v1/sessions/{session_id}/chat', json={
                'model': model, 'user_query': query, 'appendix': list(refs),
            })
            response.raise_for_status()
            run_id = response.json()['run_id']
            result = await terminal(api, run_id)
            assert result['status'] == expected, result['error_desc']
            return run_id

        async def answer():
            page = (await api.get(f'/api/v1/sessions/{session_id}/messages')).json()
            return next(item['content'] for item in reversed(page['items']) if item['role'] == 'assistant')

        image_ref = await upload('image.png', image_bytes)
        first = await chat('Read the large text printed in this image and identify the colored shape. Reply briefly. Do not use tools.', [image_ref])
        image_answer = await answer()
        assert image_marker in image_answer, 'Model did not read the image marker'
        print('PASS image understanding', flush=True)
        document_data = pdf_document(document_marker) if args.document_format == 'pdf' else f'The secret document code is {document_marker}.'.encode()
        document_ref = await upload(f'notes.{args.document_format}', document_data)
        await chat('Read the attached document and reply with its secret document code only. Do not use tools.', [document_ref])
        assert document_marker in await answer(), 'Model did not read the document marker'
        print('PASS document understanding', flush=True)
        await chat('What were the text in the earlier image and the secret code in the earlier document? Reply with both. Do not use tools.')
        history_answer = await answer()
        assert image_marker in history_answer and document_marker in history_answer
        print('PASS historical attachments', flush=True)

        image_row = (await repository.get_appendices(user_id, [image_ref['id']]))[image_ref['id']]
        original = Path(image_row.path)
        holding = original.with_suffix('.smoke-held')
        original.rename(holding)
        try:
            failed = await chat('Repeat the text in the image only. Do not use tools.', expected='failed')
            assert (await api.get(f'/api/v1/runs/{failed}')).json()['error_code'] == 'attachment_read_failed'
        finally:
            holding.rename(original)
        resumed = await api.post(f'/api/v1/runs/{failed}/resume', json={'resume_time': 0})
        resumed.raise_for_status()
        assert (await terminal(api, failed))['status'] == 'success'
        print('PASS original-file restoration and resume', flush=True)

        details = (await api.get(f'/api/v1/sessions/{session_id}')).json()
        assert details['has_appendix']
        history = (await api.get(f'/api/v1/sessions/{session_id}/messages')).json()
        assert history['items'][0]['appendix'][0]['thumbnail_data_url'].startswith('data:image/webp;base64,')
        assert not list(get_project_workdir(user_id, session.project_id).iterdir())
        async with db.transaction() as tx:
            rows = [list((await tx.execute(select(table.seq, table.role, table.content, table.appendix)
                    .where(table.session_id == session_id).order_by(table.seq))).all()) for table in (Message, SessionMessage)]
            assert rows[0] == rows[1]
        run = await repository.get_run(first, user_id)
        async with open_checkpointer(config.database) as saver:
            checkpoint = await saver.aget_tuple({'configurable': {'thread_id': run.thread_id}})
            assert checkpoint is not None
            assert 'base64,' not in json.dumps(checkpoint.checkpoint['channel_values'])
        print('PASS thumbnails, downloads, dual tables, workspace isolation and reference-only checkpoint', flush=True)


async def main(args):
    if args.output_limit is None:
        return await smoke(args)
    # Isolate diagnostic quota overrides in memory and bind separate ports.
    from laxagent.main import run_servers
    config = load_config(args.config)
    with socket.socket() as api_socket, socket.socket() as router_socket:
        api_socket.bind(("127.0.0.1", 0)); router_socket.bind(("127.0.0.1", 0))
        config.api_server.port = api_socket.getsockname()[1]
        config.llm_router.port = router_socket.getsockname()[1]
    for provider in config.llm.provider_list:
        for model in provider.model_list:
            model.limit.output = min(args.output_limit, model.limit.context)
    servers = asyncio.create_task(run_servers(config))
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=1) as api, asyncio.timeout(15):
            while True:
                if servers.done():
                    await servers
                    raise RuntimeError("Diagnostic service stopped")
                try:
                    response = await api.get(f"http://127.0.0.1:{config.api_server.port}/openapi.json")
                    if response.status_code == 200:
                        break
                except httpx2.HTTPError:
                    pass
                await asyncio.sleep(.1)
        await smoke(args, config)
    finally:
        servers.cancel()
        await asyncio.gather(servers, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('config/config.json'))
    parser.add_argument('--model')
    parser.add_argument('--document-format', choices=('txt', 'pdf'), default='txt')
    parser.add_argument('--output-limit', type=int, help='Use temporary servers and an in-memory output quota override')
    asyncio.run(main(parser.parse_args()))
