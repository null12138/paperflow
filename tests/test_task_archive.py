import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.web.archive import save_corpus
from paperflow.web.jobs import create_download_archive


def test_full_run_keyword_structure_and_snapshot_scope(tmp_path):
    with patch.dict(os.environ, {'PAPERFLOW_DATA_ROOT':str(tmp_path)}):
        for folder in ('a','b'):
            p=tmp_path/'downloads'/folder/'same.pdf'
            p.parent.mkdir(parents=True)
            p.write_bytes(b'%PDF-1.4\ntest')
        papers=[Paper(title='First',doi='10.1000/a',authors=['Author A'],year='2024',journal='Journal',abstract='Full abstract',species={'A/B','A:B'},downloaded_path=str(tmp_path/'downloads/a/same.pdf')),
                Paper(title='Second',doi='10.1000/b',species={'A/B'},downloaded_path=str(tmp_path/'downloads/b/same.pdf')),
                Paper(title='Failed',doi='10.1000/c',species={'A:B'},abstract='Failed abstract')]
        with PaperDatabase(tmp_path/'paperflow.db') as db:
            db.save_papers(papers)
            db.save_papers([Paper(title='Other task',doi='10.1000/unrelated',species={'A/B'})])
        save_corpus(7,papers)
        archive,included=create_download_archive({'id':7,'kind':'full_run','params':{'keywords':['A/B','A:B','Empty']}})
        assert included==2
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            names=z.namelist()
            assert len(names)==len(set(names))
            assert all(n.startswith('task-7/') for n in names)
            assert 'task-7/03_Empty/pdf_downloaded/' in names
            assert len([n for n in names if n.endswith('.pdf')])==3
            text=z.read('task-7/01_A_B/summary.txt').decode()
            assert 'Author A. First. Journal, 2024. DOI: 10.1000/a.' in text
            assert 'Other task' not in text
            assert 'Failed' in z.read('task-7/02_A_B/summary.txt').decode().split('failedDownload:')[1]
            assert 'Full abstract' in z.read('task-7/01_A_B/abstracts.txt').decode()


def test_legacy_input_and_report_and_path_protection(tmp_path):
    root=tmp_path/'data';root.mkdir()
    outside=tmp_path/'outside.pdf';outside.write_bytes(b'%PDF-1.4\nprivate')
    with patch.dict(os.environ,{'PAPERFLOW_DATA_ROOT':str(root)}):
        (root/'imports').mkdir();(root/'imports/words.txt').write_text('Species\n')
        (root/'exports').mkdir();(root/'exports/run-summary-8.txt').write_text('[downloaded] Target | doi: | old path\n')
        with PaperDatabase(root/'paperflow.db') as db:
            db.save_papers([Paper(title='Target',doi='10.1000/t',species={'Species'},downloaded_path=str(outside)),Paper(title='Other',species={'Species'})])
        archive,included=create_download_archive({'id':8,'kind':'full_run','params':{'path':'imports/words.txt'}})
        assert included==0
        with zipfile.ZipFile(archive) as z:
            assert not any(n.endswith('.pdf') for n in z.namelist())
            summary=z.read('task-8/01_Species/summary.txt').decode()
            assert 'Target' in summary.split('failedDownload:')[1] and 'Other' not in summary


def test_failed_rebuild_keeps_previous_archive(tmp_path):
    with patch.dict(os.environ,{'PAPERFLOW_DATA_ROOT':str(tmp_path)}):
        (tmp_path/'exports').mkdir()
        target=tmp_path/'exports/pdf-task-9.zip';target.write_bytes(b'previous archive')
        with PaperDatabase(tmp_path/'paperflow.db') as db: pass
        try:
            create_download_archive({'id':9,'kind':'full_run','params':{}})
        except ValueError: pass
        else: raise AssertionError('invalid task should fail')
        assert target.read_bytes()==b'previous archive'
