from zipfile import ZipFile
from lxml import etree
from pathlib import Path

doc=Path('/Users/opener/Downloads/双师课课后作业.docx')
ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships','v':'urn:schemas-microsoft-com:vml'}
with ZipFile(doc) as z:
    tree=etree.fromstring(z.read('word/document.xml'))
    reltree=etree.fromstring(z.read('word/_rels/document.xml.rels'))
rels={x.get('Id'):x.get('Target') for x in reltree}
for p in tree.xpath('//w:p',namespaces=ns):
    text=''.join(p.xpath('.//w:t/text()',namespaces=ns))
    if '木板上有' in text or ('质量均为' in text and '相同小滑块' in text):
        print('TEXT',text)
        for im in p.xpath('.//v:imagedata',namespaces=ns):
            rid=im.get('{%s}id'%ns['r']); print('IMAGE',rid,rels.get(rid),im.get('title'))
        for ob in p.xpath('.//w:object',namespaces=ns):
            shape=ob.xpath('.//v:shape',namespaces=ns)
            ole=ob.xpath('.//*[local-name()="OLEObject"]')
            print('OBJECT', shape[0].get('style') if shape else None,
                  [(x.get('ProgID'),x.get('{%s}id'%ns['r']),rels.get(x.get('{%s}id'%ns['r']))) for x in ole])
