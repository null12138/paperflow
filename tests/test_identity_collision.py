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


def test_pmid_key_is_replaced_when_doi_and_corrected_pmid_arrive(tmp_path):
    with PaperDatabase(tmp_path / 'db') as db:
        first = db.upsert_paper(Paper(title='Shared title', pmid='old-pmid', species={'first'}))
        db.save_download(Paper(title='Shared title', pmid='old-pmid', downloaded_path='downloads/first.pdf'), True, 'ok')
        db.upsert_paper(Paper(title='Shared title', doi='10.1000/new', pmid='corrected-pmid'))
        second = db.upsert_paper(Paper(title='Another title', pmid='old-pmid', species={'second'}))
        assert first != second
        a = db.get_paper_detail(first)
        assert a['identity_key'] == 'doi:10.1000/new'
        assert a['pdf_path'] == 'downloads/first.pdf'
        assert len(a['attempts']) == 1
        assert db.get_paper_detail(second)['pmid'] == 'old-pmid'


def test_normalized_title_tracks_retained_longer_title(tmp_path):
    from paperflow.models import normalize_title
    with PaperDatabase(tmp_path / 'db') as db:
        first = db.upsert_paper(Paper(title='The complete paper title', doi='10.1000/a'))
        db.upsert_paper(Paper(title='Short title', doi='10.1000/a'))
        assert db.get_paper_detail(first)['normalized_title'] == normalize_title('The complete paper title')
        assert db.upsert_paper(Paper(title='The complete paper title')) == first


def test_startup_repairs_stale_identity_key_without_merging_conflicting_ids(tmp_path):
    import sqlite3
    path = tmp_path / 'db'
    with PaperDatabase(path) as db:
        db.upsert_paper(Paper(title='Corrigendum title', doi='10.1016/x.123188', pmid='39843091'))
        db.connection.execute("UPDATE papers SET identity_key='doi:10.1016/x.123127'")
    with PaperDatabase(path) as db:
        assert db.upsert_paper(Paper(title='Corrigendum title', doi='10.1016/x.123127', pmid='39778984')) != 0
        assert db.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 2
