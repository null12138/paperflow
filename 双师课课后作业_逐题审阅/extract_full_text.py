from pathlib import Path
from zipfile import ZipFile
from lxml import etree
import olefile, io, re, json, contextlib

try:
    from mtef_py import MTEF
except ImportError:
    MTEF = None

SRC=Path('/Users/opener/Downloads/双师课课后作业.docx')
ROOT=Path(__file__).parent
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships','v':'urn:schemas-microsoft-com:vml','m':'http://schemas.openxmlformats.org/officeDocument/2006/math'}

def tex_from_ole(data):
    try:
        ole=olefile.OleFileIO(io.BytesIO(data))
        if not ole.exists('Equation Native'): return None
        d=ole.openstream('Equation Native').read()
        marker=b'TeX Input Language\x00'; i=d.find(marker)
        if i>=0:
            raw=d[i+len(marker):].split(b'\x00',1)[0]
            tex=raw.decode('utf-8','replace').strip()
            if tex:
                return tex
    except Exception:
        pass
    if MTEF is not None:
        try:
            # The parser is verbose; keep the reconstructed-text output clean.
            with contextlib.redirect_stdout(io.StringIO()):
                mtef, err = MTEF.OpenBytes(data)
                if not err:
                    tex = mtef.Translate().strip()
                    tex = re.sub(r'^\$\s*|\s*\$$', '', tex).strip()
                    if tex:
                        return tex
        except Exception:
            pass
    return None

with ZipFile(SRC) as z:
    names=set(z.namelist())
    tree=etree.fromstring(z.read('word/document.xml'))
    reltree=etree.fromstring(z.read('word/_rels/document.xml.rels'))
    rels={x.get('Id'):x.get('Target') for x in reltree}
    formulas={}
    for rid,target in rels.items():
        if target and target.startswith('embeddings/'):
            path='word/'+target
            if path in names: formulas[rid]=tex_from_ole(z.read(path))
    paras=[]
    for pi,p in enumerate(tree.xpath('//w:body/w:p | //w:body/w:tbl//w:p',namespaces=NS),1):
        parts=[]
        for node in p.iter():
            # Keep both ordinary Word text (w:t) and native Office Math text
            # (m:t).  The latter is intentionally linearized here; the MTEF
            # fallback below supplies the older embedded MathType objects.
            if etree.QName(node).localname == 't' and node.text:
                parts.append(node.text)
            elif node.tag=='{%s}object'%NS['w']:
                ole_nodes=node.xpath('.//*[local-name()="OLEObject"]')
                if ole_nodes:
                    rid=ole_nodes[0].get('{%s}id'%NS['r']); tex=formulas.get(rid)
                    parts.append('⟦'+(tex if tex else '公式无法提取')+'⟧')
        text=''.join(parts).strip()
        if text: paras.append({'p':pi,'text':text})

(ROOT/'reconstructed_paragraphs.json').write_text_text if False else None
(ROOT/'reconstructed_paragraphs.json').write_text(json.dumps(paras,ensure_ascii=False,indent=2),encoding='utf-8')
with (ROOT/'reconstructed_text.txt').open('w',encoding='utf-8') as f:
    for x in paras: f.write(f"[{x['p']}] {x['text']}\n")
stats={'relationships':len(rels),'ole_formulas':len(formulas),'tex_recovered':sum(bool(x) for x in formulas.values()),'paragraphs':len(paras)}
print(json.dumps(stats,ensure_ascii=False))
