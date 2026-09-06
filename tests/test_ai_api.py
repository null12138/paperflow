import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from paperflow.web.app import create_app
from paperflow.database import PaperDatabase
from paperflow.models import Paper


def test_machine_submission_and_browser_csrf():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
        'PAPERFLOW_DATA_ROOT': directory, 'PAPERFLOW_WEB_USERNAME': 'test',
        'PAPERFLOW_WEB_PASSWORD': 'secret'
    }):
        client = create_app({'TESTING': True, 'SECRET_KEY': 'test'}).test_client()
        headers = {'Authorization': 'Basic ' + base64.b64encode(b'test:secret').decode()}
        payload = {'workflow': 'metadata', 'items': ['climate'], 'limit': 1}
        assert client.post('/api/v1/jobs', json=payload).status_code == 401
        assert client.post('/api/v1/jobs', json=payload, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 400
        response = client.post('/api/v1/jobs', json=payload, headers=headers)
        assert response.status_code == 202
        assert client.get('/api/v1/jobs/' + str(response.json['id']), headers=headers).json['status'] == 'queued'
        assert client.post('/api/v1/jobs', json=['invalid'], headers=headers).status_code == 400


def test_paper_pdf_and_private_fields():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PAPERFLOW_DATA_ROOT': directory}):
        root = Path(directory)
        (root / 'downloads').mkdir()
        pdf = root / 'downloads/test.pdf'
        pdf.write_bytes(b'%PDF-1.4\ntest')
        with PaperDatabase(root / 'paperflow.db') as database:
            paper = Paper(title='test', doi='10.1000/test', downloaded_path=str(pdf), download_detail='/secret')
            database.save_download(paper, True, '/secret')
        client = create_app({'TESTING': True}).test_client()
        row = client.get('/api/v1/papers').json['results'][0]
        detail = client.get('/api/v1/papers/' + str(row['id'])).json
        assert 'pdf_path' not in detail and 'attempts' not in detail and 'download_detail' not in detail
        response = client.get(detail['pdf_url'])
        assert response.status_code == 200 and response.data.startswith(b'%PDF-')
        response.close()


def test_search_exposes_txt_not_stale_zip():
    from paperflow.web.jobs import JobStore, download_archive_path
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PAPERFLOW_DATA_ROOT': directory}):
        client = create_app({'TESTING': True}).test_client()
        store = JobStore()
        jid = store.enqueue('search', {'keywords': ['climate']})
        store.finish(jid, 0, 'ZIP 已生成（0 篇 PDF）')
        Path(directory, 'exports', f'search-{jid}.txt').write_text('paper metadata')
        download_archive_path(jid).write_bytes(b'old zip')
        result = client.get(f'/api/jobs/{jid}').json
        assert result['txt_ready'] and not result['archive_ready']
        response = client.get(result['txt_url'])
        assert response.data == b'paper metadata'
        response.close()
        assert client.get(f'/downloads/{jid}/archive').status_code == 404
        assert b'TXT' in client.get('/jobs').data


def test_search_worker_never_builds_zip():
    import sys
    from paperflow.web.jobs import JobStore
    from paperflow.web.worker import Worker
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PAPERFLOW_DATA_ROOT': directory}):
        store = JobStore()
        jid = store.enqueue('search', {})
        job = store.claim_next()
        with patch('paperflow.web.worker.build_command', return_value=[sys.executable, '-c', 'print("search done")']), patch('paperflow.web.worker.create_download_archive') as archive:
            Worker(store).run_job(job)
            archive.assert_not_called()
        assert store.get(jid)['status'] == 'succeeded'
