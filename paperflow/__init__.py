"""paperflow — 多源文献检索 + PDF 下载编排工具。

设计：数据源 = 元数据检索适配器（WOS/PubMed/Crossref/S2/CNKI/EuropePMC…）
      PDF 引擎 = 按 DOI 依次尝试 [Sci-Hub → Unpaywall/PMC → 出版社订阅适配器 → 本地已有]
      全部经过 PDF 头校验、去重、断点续跑；默认限速慢节奏，稳定优先。
"""

__version__ = "0.6.0"
