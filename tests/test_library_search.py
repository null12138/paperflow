from pathlib import Path
from urllib.parse import urlparse, parse_qs
from html import unescape
import re

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.web.app import create_app


def test_literal_unicode_multiterm_and_identifier_search(tmp_path):
    with PaperDatabase(tmp_path / 'db') as db:
        a = db.upsert_paper(Paper(title='Ecology of beetles', doi='10.1000/a',
            abstract='Survival is 50% in stage_a', authors=['MÜLLER'], pmid='12345',
            species={'Protaetia brevitarsis'}, sources={'Europe PMC'}))
        db.upsert_paper(Paper(title='Unrelated beetles', abstract='stageXa with 500 individuals'))
        for text in ['Protaetia', 'brevitarsis ecology', '  PROTAETIA\n brevitarsis ',
                     'müller', '12345', 'https://doi.org/10.1000/a', '50%', 'stage_a']:
            assert [r['id'] for r in db.list_papers(text=text)] == [a], text
        assert not db.list_papers(text='Protaetia absent')
        # Exact keyword filtering remains available to CLI/download scopes.
        assert not db.list_papers(keyword='Protaetia')


def test_library_legacy_keyword_is_partial_search_and_filters_reset(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPERFLOW_DATA_ROOT', str(tmp_path))
    with PaperDatabase(tmp_path / 'paperflow.db') as db:
        db.save_papers([Paper(title='Relevant paper', species={'Protaetia brevitarsis'}),
                        Paper(title='Other paper')])
    client = create_app({'TESTING': True, 'WTF_CSRF_DISABLED': True}).test_client()
    for url in ['/library?q=Protaetia', '/library?keyword=Protaetia', '/library?q=Relevant&keyword=stale']:
        page = client.get(url).get_data(as_text=True)
        assert 'Relevant paper' in page and 'Other paper' not in page
        assert 'name="keyword"' not in page
        assert '清除搜索与筛选' in page
    assert 'Other paper' in client.get('/library').get_data(as_text=True)


def test_library_pagination_keeps_both_if_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPERFLOW_DATA_ROOT', str(tmp_path))
    with PaperDatabase(tmp_path / 'paperflow.db') as db:
        db.save_papers([Paper(title=f'Beetle paper {i}', doi=f'10.1000/{i}', journal='Test Journal') for i in range(101)])
        db.connection.execute("INSERT INTO journal_metrics(normalized_journal,journal,year,impact_factor,source) VALUES ('testjournal','Test Journal',2024,2,'test')")
    client = create_app({'TESTING': True, 'WTF_CSRF_DISABLED': True}).test_client()
    page = client.get('/library?q=Beetle&min_if=0&max_if=3').get_data(as_text=True)
    link = unescape(re.search(r'href="([^"]+)">下一页', page).group(1))
    args = parse_qs(urlparse(link).query)
    assert args['min_if'] == ['0.0'] and args['max_if'] == ['3.0'] and args['q'] == ['Beetle']
    next_page = client.get(link).get_data(as_text=True)
    assert '下一页' not in next_page and '本页 1 条' in next_page
    with PaperDatabase(tmp_path / 'paperflow.db') as db:
        db.connection.execute('DELETE FROM papers WHERE id=101')
    assert '下一页' not in client.get('/library').get_data(as_text=True)
