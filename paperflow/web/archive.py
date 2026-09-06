"""Task archives: one task root, keyword folders, metadata and verified PDFs."""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ..models import normalize_doi
from ..paths import data_path, data_root


def _component(value: str) -> str:
    # Prefixes supplied by the caller also avoid Windows reserved basenames.
    return re.sub(r'[^\w.\-]+', '_', value, flags=re.UNICODE).strip('._')[:100] or 'unnamed'


def _input_path(value: str) -> Path:
    path = Path(value)
    path = path if path.is_absolute() else data_root() / path
    path = path.resolve()
    path.relative_to(data_root().resolve())
    return path


def corpus_path(job_id: int) -> Path:
    return data_path('exports', f'corpus-{job_id}.json')


def save_corpus(job_id: int, papers: list) -> None:
    """Snapshot the exact retrieved corpus before downloads start."""
    from dataclasses import asdict
    target = corpus_path(job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for paper in papers:
        row = asdict(paper)
        row['sources'] = sorted(paper.sources)
        row['species'] = sorted(paper.species)
        records.append(row)
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)


def _groups(job: dict, db: sqlite3.Connection) -> tuple[dict, str]:
    params = job.get('params') or {}
    kind = job['kind']
    groups: dict[str, list[dict]] = {}
    if kind == 'full_run':
        keywords = params.get('keywords') or []
        if not keywords and params.get('path'):
            keywords = _input_path(params['path']).read_text(encoding='utf-8-sig').splitlines()
        keywords = list(dict.fromkeys(str(k).strip() for k in keywords if str(k).strip()))
        groups = {k: [] for k in keywords}
        snapshot = corpus_path(int(job['id']))
        if snapshot.is_file():
            for item in json.loads(snapshot.read_text(encoding='utf-8')):
                current = None
                for key in ('doi', 'pmid', 'pmcid'):
                    if item.get(key):
                        current = db.execute(f'SELECT * FROM papers WHERE {key}=?', (item[key],)).fetchone()
                        if current:
                            break
                if current is None:
                    current = db.execute('SELECT * FROM papers WHERE title=?', (item['title'],)).fetchone()
                row = dict(current) if current else {}
                row.update({k: item.get(k, '') for k in ('title','abstract','year','journal','doi','pmid','pmcid')})
                row['authors_json'] = json.dumps(item.get('authors') or [])
                for keyword in item.get('species') or keywords:
                    if keyword in groups:
                        groups[keyword].append(row)
            return groups, 'task corpus snapshot'
        # Compatibility for existing tasks. Their completed report is the best
        # available membership evidence; do not use global download timestamps.
        report = data_path('exports', f"run-summary-{job['id']}.txt")
        members = None
        if report.is_file():
            members = set()
            for line in report.read_text(encoding='utf-8-sig').splitlines():
                match = re.match(r'^\[(?:downloaded|failed)\] (.*?) \| doi:(.*?) \|', line)
                if match:
                    members.add((match[1], normalize_doi(match[2])))
        for keyword in keywords:
            rows = db.execute('''SELECT DISTINCT p.* FROM papers p
                JOIN paper_keywords pk ON pk.paper_id=p.id JOIN keywords k ON k.id=pk.keyword_id
                WHERE k.keyword=? COLLATE NOCASE ORDER BY p.year DESC,p.id''', (keyword,))
            groups[keyword] = [dict(r) for r in rows if members is None or
                              (r['doi'] and any(doi == r['doi'] for _,doi in members)) or
                              (r['title'], '') in members]
        if not groups:
            raise ValueError('任务没有关键词或输入文件，无法分类打包')
        return groups, ('legacy task report + keywords' if members is not None else
                        'legacy keyword associations; exact historical corpus unavailable')
    if kind == 'download_list':
        lines = _input_path(params['path']).read_text(encoding='utf-8-sig').splitlines()
        items = {}
        for line in lines:
            doi, _, title = line.partition('\t')
            doi = normalize_doi(doi)
            if doi:
                items.setdefault(doi, title)
        limit = int(params.get('limit') or 0)
        for doi, title in list(items.items())[:limit or None]:
            row = db.execute('SELECT * FROM papers WHERE doi=?', (doi,)).fetchone()
            groups.setdefault('DOI清单', []).append(dict(row) if row else {'doi':doi,'title':title or doi})
        groups.setdefault('DOI清单', [])
        return groups, 'task DOI input'
    if kind != 'download_db':
        raise ValueError('该任务类型不生成 PDF ZIP')
    if not job.get('started_at'):
        raise ValueError('数据库下载任务缺少开始时间')
    end = job.get('finished_at') or '9999-12-31'
    rows = db.execute('''SELECT DISTINCT p.* FROM papers p JOIN download_attempts a ON a.paper_id=p.id
        WHERE a.attempted_at>=? AND a.attempted_at<=? ORDER BY p.id''', (job['started_at'],end))
    for r in rows:
        keywords = [x[0] for x in db.execute('''SELECT k.keyword FROM keywords k JOIN paper_keywords pk
            ON pk.keyword_id=k.id WHERE pk.paper_id=? ORDER BY k.keyword''',(r['id'],))]
        for keyword in keywords or ['未分类']:
            groups.setdefault(keyword, []).append(dict(r))
    return groups or {'未分类':[]}, 'task attempt time interval'


def create_archive(job: dict[str, Any]) -> tuple[Path,int]:
    from .jobs import download_archive_path
    with sqlite3.connect(str(data_path('paperflow.db'))) as db:
        db.row_factory = sqlite3.Row
        groups, scope = _groups(job, db)
    archive = download_archive_path(job['id'])
    archive.parent.mkdir(parents=True,exist_ok=True)
    # A unique temporary archive keeps an interrupted rebuild from replacing
    # the existing download. PDFs are streamed once per keyword, not read whole.
    included = set()
    with tempfile.NamedTemporaryFile(dir=archive.parent,suffix='.zip.tmp',delete=False) as f:
        temp = Path(f.name)
    root = f"task-{int(job['id'])}/"
    try:
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:
            manifest = {'task_id':job['id'],'scope':scope,'keywords':[]}
            for number,(keyword,rows) in enumerate(groups.items(),1):
                prefix = root + f'{number:02d}_{_component(keyword)}/'
                bundle.writestr(prefix+'pdf_downloaded/',b'')
                abstracts, downloaded, failed = [], [], []
                for index,row in enumerate(rows,1):
                    title = row.get('title') or row.get('doi') or '题名缺失'
                    authors = json.loads(row.get('authors_json') or '[]')
                    citation = f"{'; '.join(authors) or '作者缺失'}. {title}. {row.get('journal') or '期刊缺失'}, {row.get('year') or '年份缺失'}."
                    if row.get('doi'): citation += f" DOI: {row['doi']}."
                    abstracts.append(f"[{index}] {citation}\nKeyword: {keyword}\nPMID: {row.get('pmid') or ''}\nPMCID: {row.get('pmcid') or ''}\nAbstract: {row.get('abstract') or '未检索到摘要'}\n")
                    valid = False
                    if row.get('pdf_path'):
                        try:
                            pdf = _input_path(row['pdf_path'])
                            # Do not publish internal files, even with a PDF header.
                            if pdf.relative_to(data_root().resolve()).parts[0] not in {'downloads','pdf_downloaded'}:
                                raise ValueError('not an output PDF')
                            with pdf.open('rb') as handle:
                                valid = handle.read(5) == b'%PDF-'
                            if valid:
                                name = f'{index:04d}_{_component(title)}.pdf'
                                bundle.write(pdf,prefix+'pdf_downloaded/'+name)
                                downloaded.append(f'[{index}] {citation} [PDF: pdf_downloaded/{name}]')
                                included.add(row.get('doi') or row.get('id') or title)
                        except (ValueError,OSError):
                            valid = False
                    if not valid:
                        failed.append(f'[{index}] {citation} [未取得有效 PDF]')
                bundle.writestr(prefix+'abstracts.txt','\n'.join(abstracts))
                bundle.writestr(prefix+'summary.txt',f'关键词: {keyword}\n论文: {len(rows)}; 已下载: {len(downloaded)}; 未下载: {len(failed)}\n\n'
                                +'downloaded:\n'+'\n'.join(downloaded)+'\n\nfailedDownload:\n'+'\n'.join(failed)+'\n')
                manifest['keywords'].append({'keyword':keyword,'directory':prefix,'papers':len(rows),'downloaded':len(downloaded),'failed':len(failed)})
            bundle.writestr(root+'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        temp.replace(archive)
    finally:
        temp.unlink(missing_ok=True)
    return archive,len(included)
