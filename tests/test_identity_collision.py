from paperflow.database import PaperDatabase
from paperflow.models import Paper


def test_bridge_preserves_download_and_relations(tmp_path):
    with PaperDatabase(tmp_path / 'db') as db:
        a = db.upsert_paper(Paper(title='DOI record', doi='10.1000/a', sources={'WOS'}))
        db.save_download(Paper(title='PMID record', pmid='123', sources={'PubMed'}, downloaded_path='downloads/a.pdf'), True, 'ok')
        assert db.upsert_paper(Paper(title='Combined record', doi='10.1000/a', pmid='123')) == a
        row = db.get_paper_detail(a)
        assert row['pmid'] == '123' and row['pdf_path'] == 'downloads/a.pdf'
        assert set(row['sources'].split(',')) == {'WOS', 'PubMed'}
        assert len(row['attempts']) == 1
        assert db.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 1


def test_conflicting_dois_remain_separate(tmp_path):
    with PaperDatabase(tmp_path / 'db') as db:
        a = db.upsert_paper(Paper(title='A', doi='10.1000/a'))
        b = db.upsert_paper(Paper(title='B', doi='10.1000/b', pmid='123'))
        db.upsert_paper(Paper(title='A', doi='10.1000/a', pmid='123'))
        assert db.get_paper_detail(a)['pmid'] == ''
        assert db.get_paper_detail(b)['pmid'] == '123'
