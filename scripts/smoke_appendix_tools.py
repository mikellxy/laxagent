"""Real-provider attachment acceptance in Lima, using temporary API/router ports.

uv run python scripts/smoke_appendix_tools.py --config config/config.json
Retains the smoke user's records. The text-main alias changes only the in-memory
capability declaration and reuses a configured upstream; it is not a new model.
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
from laxagent.main import run_servers
from laxagent.storage.checkpoints import open_checkpointer
from laxagent.storage.database import Database
from laxagent.storage.models import Message
from laxagent.storage.repositories import Repository
from smoke_appendices import pdf_document, terminal


async def smoke(config, text_model):
    user_id = str(uuid4())
    image_marker, pdf_marker = 'IMG' + uuid4().hex[:6].upper(), 'PDF' + uuid4().hex[:6].upper()
    picture = Image.new('RGB', (900, 300), 'white')
    ImageDraw.Draw(picture).text((40, 70), image_marker, fill='black', font_size=70)
    buffer = io.BytesIO()
    picture.save(buffer, 'PNG')
    async with Database.open(config.database) as db, httpx2.AsyncClient(
        base_url=f'http://127.0.0.1:{config.api_server.port}', timeout=300, trust_env=False,
    ) as api:
        repo = Repository(db)
        login = await api.post('/api/v1/login', json={'user_id': user_id, 'from_page': 'guest_login'})
        login.raise_for_status()
        api.headers['Authorization'] = 'Bearer ' + login.json()['access_token']
        session_id = login.json()['initial_session_id']
        catalog = (await api.get('/api/v1/models')).json()
        assert catalog['appendix_model'] == config.llm.appendix_model

        async def upload(name, data):
            result = await api.post('/api/v1/appendices', files={'file': (name, data)})
            result.raise_for_status()
            return result.json()

        async def chat(query, refs=(), model=None, tool=None, markers=()):
            before = (await api.get(f'/api/v1/sessions/{session_id}')).json()['next_msg_seq']
            result = await api.post(f'/api/v1/sessions/{session_id}/chat', json={
                'model': model or config.llm.default_model, 'user_query': query, 'appendix': list(refs),
            })
            result.raise_for_status()
            run_id = result.json()['run_id']
            ended = await terminal(api, run_id)
            assert ended['status'] == 'success', ended['error_desc']
            run = await repo.get_run(run_id, user_id)
            assert run.appendix_model == (config.llm.appendix_model or run.model)
            async with db.transaction() as tx:
                messages = list(await tx.scalars(select(Message).where(
                    Message.session_id == session_id, Message.seq >= before,
                ).order_by(Message.seq)))
            if tool:
                results = [json.loads(m.content) for m in messages if m.tool_name == tool and m.content.startswith('{')]
                assert any(r['error_code'] is None and all(marker in json.dumps(r['result']) for marker in markers)
                           for r in results), f'{tool} did not return the expected markers'
            async with open_checkpointer(config.database) as saver:
                snapshot = await saver.aget_tuple({'configurable': {'thread_id': run.thread_id}})
                assert snapshot and 'base64,' not in json.dumps(snapshot.checkpoint['channel_values'])
            return run

        image_ref = await upload('marker.png', buffer.getvalue())
        pdf_ref = await upload('marker.pdf', pdf_document(pdf_marker))
        await chat('仅回复“附件已收到”，不要使用工具，不要解读附件。', [image_ref, pdf_ref])
        print('PASS latest multimodal attachments and metadata', flush=True)
        await chat(f'请调用 read_appendix 读取 ID={pdf_ref["id"]} 的 PDF，返回里面的秘密代码。',
                   tool='read_appendix', markers=[pdf_marker])
        print('PASS read historical PDF through local extraction', flush=True)
        await chat(f'必须调用 ask_appendices，ids=[{image_ref["id"]},{pdf_ref["id"]}]，'
                   '问题为“图片里的大号文字和 PDF 里的秘密代码分别是什么？”。返回两者，不要仅根据历史回答。',
                   tool='ask_appendices', markers=[image_marker, pdf_marker])
        print('PASS isolated image/PDF question using configured appendix_model', flush=True)

        image_row = (await repo.get_appendices(user_id, [image_ref['id']]))[image_ref['id']]
        original = Path(image_row.path)
        held = original.with_suffix('.smoke-held')
        original.rename(held)
        try:
            await chat('仅回复 OK，不要使用工具或读取附件。')
        finally:
            held.rename(original)
        print('PASS missing historical original does not block new text messages', flush=True)

        await chat(f'请调用 ask_appendices 查看本次图片 ID={image_ref["id"]}，问题为“图片里的大号文字是什么？”。'
                   '不要直接根据历史回答。', [image_ref], model=text_model,
                   tool='ask_appendices', markers=[image_marker])
        print('PASS text-main capability path with configured attachment model', flush=True)
        print(json.dumps({'session_id': session_id, 'appendix_model': config.llm.appendix_model,
                          'text_main_alias': text_model}), flush=True)


async def main(args):
    config = load_config(args.config)
    if not config.llm.appendix_model:
        raise ValueError('This smoke requires a configured appendix_model')
    provider_name, model_name = config.llm.default_model.split(':', 1)
    provider = next(p for p in config.llm.provider_list if p.name == provider_name)
    model = next(m for m in provider.model_list if m.name == model_name)
    alias = 'smoke-text-' + uuid4().hex[:8]
    config.llm.provider_list.append(provider.model_copy(update={
        'name': alias, 'model_list': [model.model_copy(update={'supports_multimodal': False})],
    }))
    with socket.socket() as api_socket, socket.socket() as router_socket:
        api_socket.bind(('127.0.0.1', 0))
        router_socket.bind(('127.0.0.1', 0))
        config.api_server.port = api_socket.getsockname()[1]
        config.llm_router.port = router_socket.getsockname()[1]
    servers = asyncio.create_task(run_servers(config))
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=1) as api, asyncio.timeout(20):
            while True:
                if servers.done():
                    await servers
                    raise RuntimeError('Temporary server stopped')
                try:
                    if (await api.get(f'http://127.0.0.1:{config.api_server.port}/openapi.json')).status_code == 200:
                        break
                except httpx2.HTTPError:
                    pass
                await asyncio.sleep(.1)
        await smoke(config, f'{alias}:{model_name}')
    finally:
        servers.cancel()
        await asyncio.gather(servers, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('config/config.json'))
    asyncio.run(main(parser.parse_args()))
