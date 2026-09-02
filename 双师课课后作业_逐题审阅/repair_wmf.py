from pathlib import Path
import shutil, subprocess, zipfile, tempfile

ROOT=Path(__file__).parent
SRC=Path('/Users/opener/Downloads/双师课课后作业.docx')
OUT=ROOT/'双师课课后作业_公式可见修复版.docx'
work=ROOT/'wmf_repair_work'; unpack=work/'unpacked'; pdfs=work/'pdfs'; pngs=work/'pngs'
for d in (unpack,pdfs,pngs): d.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(SRC) as z: z.extractall(unpack)
wmfs=sorted((unpack/'word/media').glob('*.wmf'))
cmd=['soffice','-env:UserInstallation=file:///private/tmp/lo_wmf_all','--headless','--convert-to','pdf','--outdir',str(pdfs)]+[str(x) for x in wmfs]
subprocess.run(cmd,check=True,env={'PATH':'/opt/homebrew/bin:/usr/bin:/bin','TMPDIR':'/private/tmp'})
for i,w in enumerate(wmfs,1):
    p=pdfs/(w.stem+'.pdf'); raw=pngs/(w.stem+'_raw.png'); final=w.with_suffix('.png')
    subprocess.run(['pdftoppm','-png','-singlefile','-r','180',str(p),str(raw.with_suffix(''))],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['magick',str(raw),'-trim','+repage',str(final)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    w.unlink()
    if i%50==0: print(f'{i}/{len(wmfs)}')
for rel in unpack.rglob('*.rels'):
    s=rel.read_text(encoding='utf-8'); s2=s.replace('.wmf"','.png"')
    if s2!=s: rel.write_text(s2,encoding='utf-8')
with zipfile.ZipFile(OUT,'w',zipfile.ZIP_DEFLATED) as z:
    for p in unpack.rglob('*'):
        if p.is_file(): z.write(p,p.relative_to(unpack))
print(OUT)
