#!/usr/bin/env python3
import json
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)


HERE = Path(__file__).resolve().parent
OUT = Path('/Users/opener/code/abz/output/pdf/双师课课后作业_逐题纠错高亮对比.pdf')


ISSUES = [
    dict(section=1, question=2, page=3,
         before='题干未给出重力加速度取值；答案直接选择 E（1.40 m）。',
         problem='条件不足。由推导只能得到 h=14/g，不能唯一得到数值 1.40 m。',
         after='在题干补充：“取重力加速度 g=10 m/s²。”',
         basis='此时 h=14/g=1.40 m，答案 E 才唯一成立。'),
    dict(section=1, question=2, page=3,
         before='物块的大小可以忽略，即可以视为无体积的重点。',
         problem='“重点”为错别字。',
         after='物块的大小可以忽略，即可以视为无体积的质点。',
         basis='物理模型的规范术语是“质点”。'),
    dict(section=1, question=2, page=4,
         before='代数数据解得……', problem='动词误用。',
         after='代入数据解得……', basis='应把已知数据代入公式。'),
    dict(section=1, question=3, page=5,
         before='B 对地的加速度公式中根号、平方和分式层级错乱。',
         problem='公式排版不能清楚表达两个互相垂直的加速度分量。',
         after='a_B=√(a₁²+a²)，其中 a=(m_Bg−m_Aa₁)/(m_A+m_B)。',
         basis='B 相对车的竖直加速度与车的水平加速度正交，应作矢量合成。'),
    dict(section=3, question=2, page=15,
         before='3n(n 是大于1的正整数）……√(βμgL)(β 为足够大的常数）',
         problem='两处均为半角左括号与全角右括号混用，第一处还破坏了句法。',
         after='3n（n为大于1的正整数）……√(βμgL)（β为足够大的常数）',
         basis='统一中文括号并恢复完整限定条件。'),
    dict(section=4, question=2, page=25,
         before='（1）v_A=v_B=2gr　（3）v_m=(1+√2)gr',
         problem='原答案漏失根号，量纲不成立。',
         after='（1）v_A=v_B=√(2gr)　（3）v_m=√[(1+√2)gr]',
         basis='速度的量纲应为 √(gr)，与解析中的能量关系一致。'),
    dict(section=4, question=3, page=29,
         before='同一题的“答案 C＋完整解析”连续排入两遍。',
         problem='内容完全重复，且导致后续题号错觉。',
         after='只保留第一份“答案 C＋解析”，删除第二份重复内容。',
         basis='两份文字、公式和结论完全相同。'),
    dict(section=4, question=4, page=30,
         before='仅有“4.【答案】A”和解析，题干、配图及 A—D 选项全部缺失。',
         problem='题目不完整，读者无法作答，也无法从解析唯一反推原选项。',
         after='从原命题稿补回第4题完整题干、配图和 A—D 选项；在补回前不得作为完整练习题发布。',
         basis='现存信息不足以无歧义重建原题，不能擅自编写选项。'),
    dict(section=5, question=1, page=32,
         before='“子弹射入木块后末穿出”“子弹最终末穿出木块”',
         problem='两处“末”均为错别字。',
         after='“子弹射入木块后未穿出”“子弹最终未穿出木块”',
         basis='此处表示否定，应使用“未”。'),
    dict(section=5, question=2, page=34,
         before='ΔE=½mv²−½(M−m)v′²',
         problem='新核的反冲动能被错误相减，且与后面的质量亏损结果矛盾。',
         after='ΔE=½mv²+½(M−m)v′²',
         basis='释放的核能等于 α 粒子和新核动能之和。'),
    dict(section=5, question=3, page=35,
         before='第（4）问答案：0.90 m；解析：木板长度大于0.90 m。',
         problem='答案只给临界值，且答案与解析的范围表述不一致。',
         after='第（4）问答案：L≥0.90 m。',
         basis='“木块不会从小车左端掉下”包含恰好到达左端的临界情形。'),
    dict(section=5, question=4, page=37,
         before='第（3）问答案只列出“4 kg＜M≤6 kg：Q=1 J”，低质量区间缺项。',
         problem='分段答案不完整。',
         after='第（3）问：4 kg＜M≤6 kg 时，Q=1 J；1 kg≤M≤4 kg 时，Q=2M/(M+4) J。',
         basis='按小车是否在木块离开前达到共同速度分段，M=4 kg 处两式连续。'),
    dict(section=6, question=1, page=40,
         before='题干和 A—D 选项完整重复两遍，随后没有本题答案与解析。',
         problem='重复排版且答案缺失。',
         after='删除第二份重复题干和选项；补充答案：ABD。关键结果：m_A=3 kg，m_C=10 kg，Δt 内 B 的位移为 7/12 m，最大弹性势能为30 J。',
         basis='由碰撞动量守恒、系统质心运动及最短压缩时共同速度复算；选项 C 所述78 J不成立。'),
    dict(section=6, question=2, page=43,
         before='第2题题干→第3题题干→第2题题干（重复）→第2题答案→第3题题干（重复）→第3题答案。',
         problem='第2、3题交叉错序，且两道题干均重复。',
         after='重排为：第2题题干→第2题答案与解析→第3题题干→第3题答案与解析；删除重复题干。',
         basis='按题号和各自解析中的物理对象可明确配对。'),
    dict(section=6, question=2, page=43,
         before='第（1）问答案：10 m/s；31.2。',
         problem='第二个数值缺少单位。',
         after='第（1）问答案：10 m/s；31.2 N。',
         basis='该量为滑块通过圆弧管道最高点时所受支持力。'),
    dict(section=6, question=3, page=45,
         before='第3题题干被夹在第2题内容中，并在第2题答案后再次出现。',
         problem='题序错乱且题干重复。',
         after='只保留一份第3题题干，并将其连同答案、解析整体置于第2题之后。',
         basis='保证一题一组“题干—答案—解析”。'),
    dict(section=7, question=2, page=49,
         before='（3）求小球与10号球碰后的速度。',
         problem='“小球”指代不明，此时已有多个粘连小球。',
         after='（3）求A球与1—9号球形成的粘连整体同10号球碰撞后的共同速度。',
         basis='明确碰撞双方和所求速度的对象。'),
    dict(section=8, question=2, page=58,
         before='½mv_A²+mv_B²=½mv_A1²+½mv_B1²',
         problem='碰撞前 B 的动能项漏写系数 1/2。',
         after='½mv_A²+½mv_B²=½mv_A1²+½mv_B1²',
         basis='弹性碰撞前后总动能守恒，每个平动动能项均为 ½mv²。'),
    dict(section=9, question=2, page=68,
         before='第（2）问的第 ii 小问，解析标题写成“（3）”。',
         problem='小问编号与题干不对应。',
         after='将该解析标题改为“（2）ii”。',
         basis='该段内容回答的是第（2）问的第二种情形。'),
    dict(section=9, question=4, page=70,
         before='第（3）问答案：（10 cm，8.4 cm）（10 cm，1.2 cm）',
         problem='两个可能坐标之间缺少连接词，容易被误读成一个连续表达式。',
         after='第（3）问答案：（10 cm，8.4 cm）或（10 cm，1.2 cm）',
         basis='粒子可能落在关于中线对称的两个位置。'),
    dict(section=11, question=3, page=95,
         before='“如图（甲)所示”“图（乙)”', problem='中英文括号混用。',
         after='“如图（甲）所示”“图（乙）”', basis='统一中文全角括号。'),
    dict(section=11, question=5, page=102,
         before='cosα=25/√41', problem='三角函数数值错误；该值大于1，不可能成立。',
         after='cosα=5/√41', basis='已知 sinα=4/√41，由 sin²α+cos²α=1 得 cosα=5/√41。'),
    dict(section=14, question=4, page=130,
         before='“导轨MNPQ与MNPQ′”“以PP为边界”“连接在MM间”',
         problem='第二根导轨及横向边界的撇号缺失，几何对象标记不唯一。',
         after='“导轨MNPQ与M′N′P′Q′”“以PP′为边界”“连接在MM′间”',
         basis='与图中的两条平行导轨端点命名保持一致。'),
    dict(section=14, question=4, page=130,
         before='刚好从cc′为无碰撞地进入', problem='多余的“为”导致语病。',
         after='刚好从cc′无碰撞地进入', basis='删除赘词后句法完整。'),
    dict(section=15, question=2, page=134,
         before='ab边通过第一个磁场区域用时0.1 s。',
         problem='题干称磁场区域宽度 L=2 m，但解析用 v̄t₁=d₁=8 m；“通过区域”与实际采用的位移不一致。',
         after='线框向前移动 d₁=8 m 用时0.1 s。',
         basis='修正后与解析的 v̄t₁=d₁ 及后续数值计算一致；若原图另有定义，应以命题原稿为准重写。'),
    dict(section=16, question=1, page=144,
         before='第1题的答案与解析在第2题答案位置又完整复制一次。',
         problem='第1题内容重复，并挤占了第2题答案位置。',
         after='第1题答案与解析只保留紧接第1题题干的第一份；删除第2题后的误复制内容。',
         basis='复制段落与第1题原答案、解析完全相同。'),
    dict(section=16, question=2, page=143,
         before='第2题没有自己的答案与解析，原位置误放第1题答案与解析。',
         problem='本题解答缺失。',
         after='补充答案：（1）①C；②增大；③423 Ω。（2）①A；②R_x=√(R₁R₂)。',
         basis='平衡时 R_x/R_M=R_a/(R_b+R_c)=1/2，故 R_x=846/2=423 Ω；交换下桥臂后两次平衡关系相乘得 R_x²=R₁R₂。'),
    dict(section=17, question=2, page=152,
         before='其轻质包带长度约为4d。',
         problem='解析把长度直接按精确的4d使用，“约为”不足以支持精确定量图像。',
         after='其轻质包带长度为4d。',
         basis='题目要求选择唯一的定量函数图像，几何长度必须明确。'),
    dict(section=17, question=4, page=156,
         before='在剩余1/3球面AB上均匀分布负电荷，总电荷量为q。',
         problem='“负电荷”与正号 q 并列，符号不明确；解析实际按 −q 计算。',
         after='在剩余1/3球面AB上均匀分布负电荷，总电荷量为−q。',
         basis='明确代数量，避免将 q 同时理解为电荷量大小和带符号电荷量。'),
    dict(section=17, question=7, page=163,
         before='证明角度极小，故可看作单摆模型。',
         problem='“证明角度”搭配错误，且缺少近似成立的量化依据。',
         after='由 cosθ=0.9968≈1 可知 θ 很小，故可近似看作单摆模型。',
         basis='小角度条件来自计算结果，模型只能表述为近似。'),
]


SECTION_PAGES = {
    1:[1,3,5,6], 2:[7,8,11], 3:[14,16,18,20], 4:[23,25,27,30],
    5:[32,34,35,37], 6:[40,43,45], 7:[47,49,51], 8:[55,57,61],
    9:[64,66,69,70], 10:[76,78,81,84], 11:[88,91,95,97,101],
    12:[104,107,110,114], 13:[116,118,121], 14:[124,126,127,130],
    15:[133,134,136,138], 16:[141,143,146,148], 17:[150,152,154,156,158,160,162],
}


def build_review_results():
    bad = {(x['section'], x['question']) for x in ISSUES}
    out=[]; seq=0
    for sec, pages in SECTION_PAGES.items():
        for q, page in enumerate(pages, 1):
            seq += 1
            ids=[i+1 for i,x in enumerate(ISSUES) if (x['section'],x['question'])==(sec,q)]
            out.append(dict(seq=seq, section=sec, question=q, page=page,
                            status='需修改' if (sec,q) in bad else '通过', issues=ids))
    assert seq == 67
    return out


def esc(s):
    return (str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            .replace('\n','<br/>'))


def register_fonts():
    candidates = [
        '/System/Library/Fonts/STHeiti Medium.ttc',
        '/System/Library/Fonts/Supplemental/Songti.ttc',
        '/System/Library/AssetsV2/com_apple_MobileAsset_Font8/259e8f5a322e8dae602d51ac00acf92e0e5c224.asset/AssetData/SimSong.ttc',
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('CN', path, subfontIndex=0))
                return 'CN'
            except Exception:
                pass
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    return 'STSong-Light'


def build_pdf(review):
    font = register_fonts()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(OUT), pagesize=A4,
        leftMargin=17*mm, rightMargin=17*mm, topMargin=17*mm, bottomMargin=16*mm,
        title='双师课课后作业：逐题纠错高亮对比', author='逐题审阅')
    frame=Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='main')

    def footer(canvas, _doc):
        canvas.saveState(); canvas.setFont(font, 8); canvas.setFillColor(colors.HexColor('#667085'))
        canvas.drawString(17*mm, 9*mm, '双师课课后作业｜逐题纠错高亮对比')
        canvas.drawRightString(A4[0]-17*mm, 9*mm, f'第 {canvas.getPageNumber()} 页')
        canvas.restoreState()
    doc.addPageTemplates(PageTemplate(id='p', frames=[frame], onPage=footer))

    S=getSampleStyleSheet()
    title=ParagraphStyle('title', parent=S['Title'], fontName=font, fontSize=20, leading=29,
                         textColor=colors.HexColor('#152238'), alignment=TA_CENTER, spaceAfter=8*mm)
    intro=ParagraphStyle('intro', parent=S['BodyText'], fontName=font, fontSize=10, leading=17,
                         textColor=colors.HexColor('#475467'), alignment=TA_LEFT)
    h=ParagraphStyle('h', parent=S['Heading2'], fontName=font, fontSize=13, leading=19,
                     textColor=colors.HexColor('#173B57'), spaceBefore=3*mm, spaceAfter=2*mm)
    label=ParagraphStyle('label', parent=S['BodyText'], fontName=font, fontSize=9, leading=14,
                         textColor=colors.HexColor('#344054'))
    red=ParagraphStyle('red', parent=label, fontSize=10, leading=16, textColor=colors.HexColor('#B42318'))
    green=ParagraphStyle('green', parent=label, fontSize=10, leading=16, textColor=colors.HexColor('#067647'))
    note=ParagraphStyle('note', parent=label, fontSize=9, leading=14, textColor=colors.HexColor('#344054'))
    tiny=ParagraphStyle('tiny', parent=label, fontSize=7.4, leading=10, alignment=TA_CENTER)

    story=[Paragraph('双师课课后作业：逐题纠错高亮对比',title),
           Paragraph('审阅范围：原稿17节、67题、163页。以下仅列出发现问题的题；同一题如有多个独立问题，分别列示。红色为修改前，绿色为修改后。文末附67题覆盖表。',intro),
           Spacer(1,5*mm)]

    for idx,x in enumerate(ISSUES,1):
        head=f'{idx:02d}｜第{x["section"]}节 第{x["question"]}题（原稿约第{x["page"]}页）'
        box=Table([
            [Paragraph('修改前',label), Paragraph(esc(x['before']),red)],
            [Paragraph('问题',label), Paragraph(esc(x['problem']),note)],
            [Paragraph('修改后',label), Paragraph(esc(x['after']),green)],
            [Paragraph('校核依据',label), Paragraph(esc(x['basis']),note)],
        ], colWidths=[25*mm, doc.width-25*mm], hAlign='LEFT')
        box.setStyle(TableStyle([
            ('FONTNAME',(0,0),(-1,-1),font), ('VALIGN',(0,0),(-1,-1),'TOP'),
            ('BACKGROUND',(0,0),(0,-1),colors.HexColor('#F2F4F7')),
            ('BACKGROUND',(1,0),(1,0),colors.HexColor('#FFF1F0')),
            ('BACKGROUND',(1,2),(1,2),colors.HexColor('#ECFDF3')),
            ('BOX',(0,0),(-1,-1),0.6,colors.HexColor('#D0D5DD')),
            ('INNERGRID',(0,0),(-1,-1),0.35,colors.HexColor('#E4E7EC')),
            ('LEFTPADDING',(0,0),(-1,-1),6), ('RIGHTPADDING',(0,0),(-1,-1),6),
            ('TOPPADDING',(0,0),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),5),
        ]))
        story.append(KeepTogether([Paragraph(head,h),box,Spacer(1,2.5*mm)]))

    story += [PageBreak(), Paragraph('67题逐题审阅覆盖表',title),
              Paragraph('“通过”表示题干、选项、答案与解析经逐题核对后未发现需要修改之处；“需修改”对应前文问题编号。页码为该题在原稿中的起始或答案所在页附近，供快速定位。',intro), Spacer(1,4*mm)]
    rows=[[Paragraph('顺序',tiny),Paragraph('定位',tiny),Paragraph('原稿页',tiny),Paragraph('结论',tiny),Paragraph('对应问题',tiny)]]
    for x in review:
        refs='、'.join(f'{i:02d}' for i in x['issues']) if x['issues'] else '—'
        status=x['status']
        rows.append([Paragraph(str(x['seq']),tiny),Paragraph(f'第{x["section"]}节 第{x["question"]}题',tiny),
                     Paragraph(str(x['page']),tiny),Paragraph(status,tiny),Paragraph(refs,tiny)])
    table=Table(rows, colWidths=[14*mm,47*mm,21*mm,24*mm,doc.width-106*mm], repeatRows=1)
    ts=[('FONTNAME',(0,0),(-1,-1),font),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#173B57')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.35,colors.HexColor('#D0D5DD')),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]
    for r,x in enumerate(review,1):
        ts.append(('BACKGROUND',(0,r),(-1,r), colors.HexColor('#FFF1F0') if x['status']=='需修改' else (colors.white if r%2 else colors.HexColor('#F8FAFC'))))
        if x['status']=='需修改': ts.append(('TEXTCOLOR',(3,r),(4,r),colors.HexColor('#B42318')))
    table.setStyle(TableStyle(ts)); story.append(table)
    doc.build(story)


if __name__ == '__main__':
    review=build_review_results()
    (HERE/'issues.json').write_text(json.dumps(ISSUES,ensure_ascii=False,indent=2),encoding='utf-8')
    (HERE/'review_results.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
    build_pdf(review)
    print(OUT)
    print(f'issues={len(ISSUES)}, affected_questions={len({(x["section"],x["question"]) for x in ISSUES})}, reviewed={len(review)}')
