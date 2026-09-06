from unittest.mock import Mock, patch
import pytest
from paperflow.sources.pubmed_crossref_s2 import CrossrefSource, SemanticScholarSource, PubMedSource


def response(payload):
    r = Mock(status_code=200)
    r.json.return_value = payload
    return r


def cross_item(i):
    return {'DOI': f'10.1000/{i}', 'title': ['Ginkgo paper'], 'type': 'journal-article'}


def test_crossref_past_1000_and_stable_cursor():
    client = Mock()
    client.get.side_effect = [
        response({'message': {'items': [cross_item(i) for i in range(1000)], 'next-cursor': 'stable'}}),
        response({'message': {'items': [cross_item(i) for i in range(1000, 2000)], 'next-cursor': 'stable'}}),
        response({'message': {'items': [cross_item(2000)], 'next-cursor': 'stable'}}),
    ]
    papers = CrossrefSource().search_species(client, 'Ginkgo', 0)
    assert len(papers) == 2001
    assert client.get.call_args_list[2].kwargs['params']['cursor'] == 'stable'


def test_crossref_limit_counts_matching_papers_across_pages():
    client = Mock()
    bad = {**cross_item(1), 'title': ['Unrelated']}
    client.get.side_effect = [response({'message': {'items': [bad, cross_item(2)], 'next-cursor': 'next'}}), response({'message': {'items': [cross_item(3), cross_item(4)]}})]
    assert len(CrossrefSource().search_species(client, 'Ginkgo', 2)) == 2
    assert client.get.call_count == 2


def test_s2_bulk_follows_token_and_deduplicates():
    source = SemanticScholarSource()
    source._limiter = Mock()
    client = Mock()
    make = lambda i: {'paperId': str(i), 'title': 'Ginkgo paper', 'externalIds': {'DOI': f'10.1000/{i}'}}
    client.get.side_effect = [response({'data': [make(i) for i in range(1000)], 'token': 'next'}), response({'data': [make(999), make(1000)]})]
    papers = source.search_species(client, 'Ginkgo', 0)
    assert len(papers) == 1001
    assert papers[0].species == {'Ginkgo'}
    assert client.get.call_args_list[1].kwargs['params']['token'] == 'next'
    assert client.get.call_args.args[0].endswith('/search/bulk')


def test_s2_limit_and_repeated_token_error():
    source = SemanticScholarSource()
    source._limiter = Mock()
    client = Mock()
    client.get.return_value = response({'data': [{'title': 'Ginkgo', 'paperId': '1'}], 'token': 'repeat'})
    assert len(source.search_species(client, 'Ginkgo', 1)) == 1
    with pytest.raises(RuntimeError, match='重复分页'):
        source.search_species(client, 'Ginkgo', 0)


def test_pubmed_pages_without_old_cap():
    source = PubMedSource()
    def fetch(client, url, params, operation):
        start = params['retstart']
        return {'esearchresult': {'count': '2300', 'idlist': [str(i) for i in range(start, min(start + params['retmax'], 2300))]}}
    with patch.object(source, '_json', side_effect=fetch) as calls:
        ids = source._search_ids(None, {'term': 'Ginkgo'}, 0)
    assert len(ids) == 2300
    assert calls.call_count == 3


def test_pubmed_splits_over_9999_with_no_loss():
    source = PubMedSource()
    def fetch(client, url, params, operation):
        term = params['term']
        if 'AND 1:1073741824' in term:
            count, first = 6000, 1
        elif 'AND 1073741825:' in term:
            count, first = 4001, 1073741825
        else:
            count, first = 10001, 1
        start = params['retstart']
        assert start + params['retmax'] <= 9999
        return {'esearchresult': {'count': str(count), 'idlist': [str(first + i) for i in range(start, min(start + params['retmax'], count))]}}
    with patch.object(source, '_json', side_effect=fetch):
        ids = source._search_ids(None, {'term': 'Ginkgo'}, 0)
    assert len(ids) == len(set(ids)) == 10001


def test_pubmed_partition_count_mismatch_is_reported():
    source = PubMedSource()
    with patch.object(source, '_json', side_effect=[{'esearchresult': {'count': '10001'}}, {'esearchresult': {'count': '5000'}}, {'esearchresult': {'count': '5000'}}]):
        with pytest.raises(RuntimeError, match='总数不一致'):
            source._search_ids(None, {'term': 'Ginkgo'}, 0)
