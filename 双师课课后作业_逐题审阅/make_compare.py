from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib.units import mm

root=Path(__file__).parent
out=root/'双师课课后作业_修改前后高亮对比.pdf'
pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
c=canvas.Canvas(str(out),pagesize=A4); W,H=A4; f='STSong-Light'
items=[('第1节第2题｜题干条件','原文：给定 M = 3m 和 α = 45°，未给出重力加速度 g 的数值。','修改后：补充“取 g = 10 m/s²”，使 h = 1.40 m 与选项 E 有确定依据。'),('第1节第2题｜术语','原文：假设木块为无体积的重点。','修改后：假设木块为无体积的质点。'),('第1节第3题｜B 的加速度','原文：a_B = √(a² + a₁²) 的根号、平方和分式排版层次混乱。','修改后：a_B = √(a₁² + a²)，其中 a = (m_B g − m_A a₁)/(m_A + m_B)。'),('第1节第3题｜A 的加速度','原文：a_A = a + a₁ 后接分式，等号和分式错位。','修改后：a_A = m_B(g + a₁)/(m_A + m_B)。'),('第2节第3题｜功率公式','原文：P = Nm/T + [N²L²/T² gh]，括号与分式层次不清。','修改后：P = Nm(v₀² + gh)/T，且 v₀ = NL/T。'),('文字规范｜单位','原文：m / s、m/s2、N/J 等写法不统一。','修改后：统一为 m/s²、N、J，并规范上下标。'),('文字规范｜错别字','原文：“重点”。','修改后：“质点”。'),('公式缺失标注','原文：部分 MathType/OLE 公式在页面中显示为空白。','修改后：标注“源公式缺失，不能安全猜改”，避免伪造答案；待获得可见公式源后补录。')]
def wrap(t,x,y,size=10,maxw=174*mm,leading=15):
    c.setFont(f,size); line=''
    for ch in t:
        if c.stringWidth(line+ch,f,size)>maxw: c.drawString(x,y,line); y-=leading; line=ch
        else: line+=ch
    if line: c.drawString(x,y,line); y-=leading
    return y
def lines_for(t,size=9,maxw=166*mm):
    lines=[]; line=''
    for ch in t:
        if c.stringWidth(line+ch,f,size)>maxw: lines.append(line); line=ch
        else: line+=ch
    if line: lines.append(line)
    return lines
def block(t,y,color):
    ls=lines_for(t); h=len(ls)*12+8
    c.setFillColor(color); c.roundRect(18*mm,y-h,174*mm,h,2,fill=1,stroke=0); c.setFillColor(black)
    yy=y-14
    for ln in ls: c.setFont(f,9); c.drawString(22*mm,yy,ln); yy-=12
    return y-h-8
c.setFont(f,18); c.drawString(18*mm,H-22*mm,'双师课课后作业｜修改前后高亮对比'); c.setStrokeColor(HexColor('#2F5597')); c.line(18*mm,H-26*mm,W-18*mm,H-26*mm)
y=H-40*mm; y=wrap('说明：本文件只列出已确认的修改项。红色区域为修改前，绿色区域为修改后；未列出的题目尚未确认存在可安全修改的差异。',18*mm,y,10); y-=5
for title,before,after in items:
    if y<45*mm: c.showPage(); c.setFont(f,16); c.drawString(18*mm,H-22*mm,'双师课课后作业｜修改前后高亮对比（续）'); c.line(18*mm,H-26*mm,W-18*mm,H-26*mm); y=H-40*mm
    c.setFillColor(HexColor('#1F4E79')); c.setFont(f,11); c.drawString(18*mm,y,title); y-=16
    y=block(before,y,HexColor('#FCE4D6'))
    y=block(after,y,HexColor('#E2F0D9'))
c.save(); print(out)
