"""paperflow 的 Textual 全屏终端界面。"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TextArea,
    TabbedContent,
    TabPane,
)

from .database import PaperDatabase
from .workflows import download_database_queue, fetch_wos_to_database, preflight_download_candidates, search_to_database


class ClearDatabaseScreen(ModalScreen[bool]):
    """清空数据库的二次确认弹窗。"""

    DEFAULT_CSS = """
    ClearDatabaseScreen { align: center middle; }
    #clear-dialog { width: 64; height: auto; padding: 2; border: round #ef4444; background: #111827; }
    #clear-dialog Button { margin: 1 1 0 0; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="clear-dialog"):
            yield Label("确认清空整个 SQLite 数据库？", classes="section-title")
            yield Static("论文、关键词、来源、PDF 候选、下载记录和影响因子关联都会被清除。执行前会自动备份。")
            with Horizontal():
                yield Button("取消", id="clear-cancel")
                yield Button("备份后清空", id="clear-confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "clear-confirm")


class PaperflowTui(App):
    """数据库优先的 paperflow TUI。"""

    TITLE = "paperflow"
    SUB_TITLE = "文献检索 · SQLite 队列 · PDF 下载 · 影响因子"
    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("r", "refresh", "刷新"),
    ]

    CSS = """
    Screen { background: #0b1220; color: #dbeafe; }
    Header { background: #172554; color: #eff6ff; }
    Footer { background: #111827; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 1 2; }
    .section-title { text-style: bold; color: #7dd3fc; margin: 0 0 1 0; }
    .form-row { height: auto; margin: 0 0 1 0; }
    .form-row Input, .form-row Select { width: 1fr; margin-right: 1; }
    #search-keywords { width: 1fr; height: 5; min-height: 5; max-height: 5; margin-right: 1; }
    .form-row Button { width: auto; min-width: 14; }
    .stat-row { height: 7; margin-bottom: 1; }
    .stat-card {
        width: 1fr; height: 6; margin-right: 1; padding: 1;
        border: round #1d4ed8; background: #111827;
        content-align: center middle; text-align: center;
    }
    #dashboard-note { border: round #334155; padding: 1 2; color: #94a3b8; }
    DataTable { height: 1fr; border: round #1e3a8a; }
    RichLog { height: 1fr; min-height: 10; border: round #334155; background: #020617; }
    .hint { color: #94a3b8; margin-bottom: 1; }
    #library-controls { height: auto; }
    """

    def __init__(self, db_path: Path | str = Path("paperflow.db")) -> None:
        super().__init__()
        self.db_path = Path(db_path)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("概览", id="dashboard"):
                yield Label("数据库概览", classes="section-title")
                with Horizontal(classes="stat-row"):
                    yield Static("文章\n-", id="stat-papers", classes="stat-card")
                    yield Static("PDF\n-", id="stat-downloaded", classes="stat-card")
                    yield Static("待下载\n-", id="stat-pending", classes="stat-card")
                    yield Static("候选\n-", id="stat-candidates", classes="stat-card")
                with Horizontal(classes="stat-row"):
                    yield Static("关键词\n-", id="stat-keywords", classes="stat-card")
                    yield Static("来源\n-", id="stat-sources", classes="stat-card")
                    yield Static("JIF记录\n-", id="stat-metrics", classes="stat-card")
                    yield Static("JIF匹配\n-", id="stat-matched", classes="stat-card")
                yield Static("", id="dashboard-note")
                yield Button("刷新概览", id="dashboard-refresh", variant="primary")
                yield Button("清空数据库", id="dashboard-clear", variant="error")

            with TabPane("文献库", id="library"):
                yield Label("文献库与影响因子", classes="section-title")
                with Vertical(id="library-controls"):
                    with Horizontal(classes="form-row"):
                        yield Input(placeholder="关键词（可空）", id="library-keyword")
                        yield Input(placeholder="来源，如 CNKI（可空）", id="library-source")
                        yield Select(
                            [("全部状态", ""), ("未尝试", "pending"), ("失败", "failed"),
                             ("未下载全部", "all"), ("已下载", "downloaded")],
                            value="", id="library-status",
                        )
                    with Horizontal(classes="form-row"):
                        yield Input(placeholder="最低影响因子", id="library-min-if", type="number")
                        yield Input(placeholder="最高影响因子", id="library-max-if", type="number")
                        yield Input(value="100", placeholder="条数", id="library-limit", type="integer")
                        yield Button("查询", id="library-refresh", variant="primary")
                yield DataTable(id="papers-table", cursor_type="row", zebra_stripes=True)

            with TabPane("检索", id="search"):
                yield Label("只检索并写入 SQLite（不会下载）", classes="section-title")
                yield Static("第一步：导入 TXT 后并行检索 WOS / PubMed / Crossref / S2 / CNKI（WOS 已默认加入），只写库不下载。SCI-Hub 在第二步作为 PDF 下载源。条数设为 0 表示不设本地上限，但各官方接口仍遵守自身上限。", classes="hint")
                with Horizontal(classes="form-row"):
                    yield Input(value="input.txt", placeholder="input.txt 路径（每行一个关键词）", id="search-input-file")
                    yield Button("导入 TXT", id="search-import", variant="primary")
                with Horizontal(classes="form-row"):
                    yield TextArea("", id="search-keywords")
                    yield Input(value="100", placeholder="每个源最多条数", id="search-limit", type="integer")
                with Horizontal(classes="form-row"):
                    yield Checkbox("WOS", value=True, id="source-wos")
                    yield Checkbox("PubMed", value=True, id="source-pubmed")
                    yield Checkbox("Crossref", value=True, id="source-crossref")
                    yield Checkbox("S2", value=True, id="source-s2")
                    yield Checkbox("CNKI", value=True, id="source-cnki")
                    yield Checkbox("Europe PMC", value=False, id="source-europe-pmc")
                    yield Input(placeholder="联系邮箱（可空）", id="search-email")
                    yield Button("开始检索", id="search-start", variant="success")
                yield RichLog(id="search-log", wrap=True, highlight=True, markup=True)

            with TabPane("WOS批量", id="wos-batch"):
                yield Label("WOS Starter API 分页批量获取", classes="section-title")
                yield Static(
                    "逐页写入 SQLite，默认 1 秒一次请求；0 表示获取该关键词全部记录。",
                    classes="hint",
                )
                with Horizontal(classes="form-row"):
                    yield Input(placeholder="关键词，多个用逗号或换行", id="wos-keywords")
                    yield Input(value="1000", placeholder="每词目标总数", id="wos-max-records", type="integer")
                    yield Input(value="1.0", placeholder="请求间隔秒", id="wos-interval", type="number")
                with Horizontal(classes="form-row"):
                    yield Input(value="wos_api_runs/manifest.json", placeholder="断点清单", id="wos-manifest")
                    yield Checkbox("从断点续传", value=True, id="wos-resume")
                    yield Button("开始批量获取", id="wos-start", variant="success")
                yield RichLog(id="wos-log", wrap=True, highlight=True, markup=True)

            with TabPane("下载队列", id="downloads"):
                yield Label("第二步：按来源从 SQLite 独立恢复并后台下载", classes="section-title")
                yield Static("来源为空表示下载全部已入库文献；通道可组合 CNKI / OA / 出版社 / 授权下载（WOS 浏览器模拟点击）。", classes="hint")
                with Horizontal(classes="form-row"):
                    yield Input(placeholder="关键词（可空）", id="download-keyword")
                    yield Input(placeholder="元数据来源（可空，如 WOS/CNKI）", id="download-source")
                    yield Select(
                        [("未尝试", "pending"), ("PMC候选优先", "candidate-pmc-pending"), ("候选且未尝试", "candidate-pending"), ("已有候选", "candidate"), ("重试失败", "failed"), ("全部未下载", "all")],
                        value="pending", id="download-status",
                    )
                with Horizontal(classes="form-row"):
                    yield Input(value="authorized", placeholder="PDF 下载通道（authorized=授权下载）", id="download-mode")
                    yield Input(value="downloads", placeholder="输出目录", id="download-out")
                    yield Input(value="100", placeholder="条数", id="download-limit", type="integer")
                    yield Input(value="60", placeholder="每分钟（推荐 60）", id="download-rpm", type="integer")
                    yield Button("预解析下载源", id="download-preflight", variant="primary")
                with Horizontal(classes="form-row"):
                    yield Input(placeholder="最低影响因子", id="download-min-if", type="number")
                    yield Input(placeholder="最高影响因子", id="download-max-if", type="number")
                    yield Input(placeholder="联系邮箱（OA可用）", id="download-email")
                    yield Button("开始下载", id="download-start", variant="warning")
                yield RichLog(id="download-log", wrap=True, highlight=True, markup=True)

            with TabPane("影响因子", id="impact"):
                yield Label("导入正式 JIF CSV / TSV", classes="section-title")
                yield Static(
                    "至少需要 journal、impact_factor、year 列；按期刊全名精确匹配，不会猜测。",
                    classes="hint",
                )
                with Horizontal(classes="form-row"):
                    yield Input(placeholder="JIF 文件路径", id="impact-file")
                    yield Input(value="JCR", placeholder="来源标签", id="impact-source")
                    yield Input(placeholder="默认年份（文件无year列时）", id="impact-year", type="integer")
                    yield Button("导入并匹配", id="impact-import", variant="primary")
                yield RichLog(id="impact-log", wrap=True, highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#papers-table", DataTable)
        table.add_columns("年份", "题名", "期刊", "影响因子", "来源", "PDF")
        self.refresh_dashboard()
        self.refresh_library()

    @staticmethod
    def _integer(value: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _number(value: str) -> float | None:
        try:
            return float(value) if value.strip() else None
        except (TypeError, ValueError):
            return None

    def _value(self, selector: str) -> str:
        widget = self.query_one(selector)
        return (widget.text if isinstance(widget, TextArea) else widget.value).strip()

    def refresh_dashboard(self) -> None:
        with PaperDatabase(self.db_path) as database:
            stats = database.stats()
            pending = database.connection.execute(
                "SELECT COUNT(*) FROM papers WHERE pdf_path = ''"
            ).fetchone()[0]
        values = {
            "#stat-papers": ("文章", stats["papers"]),
            "#stat-downloaded": ("PDF", stats["downloaded"]),
            "#stat-pending": ("待下载", pending),
            "#stat-candidates": ("候选", stats["pdf_candidates"]),
            "#stat-keywords": ("关键词", stats["keywords"]),
            "#stat-sources": ("来源", stats["sources"]),
            "#stat-metrics": ("JIF记录", stats["journal_metrics"]),
            "#stat-matched": ("JIF匹配", stats["papers_with_impact_factor"]),
        }
        for selector, (label, value) in values.items():
            self.query_one(selector, Static).update(f"{label}\n[bold cyan]{value:,}[/]")
        self.query_one("#dashboard-note", Static).update(
            f"数据库：[bold]{self.db_path}[/]  ·  r 刷新  ·  q 退出"
        )

    def refresh_library(self) -> None:
        status = self.query_one("#library-status", Select).value
        status = "" if status is Select.BLANK else str(status)
        with PaperDatabase(self.db_path) as database:
            rows = database.list_papers(
                keyword=self._value("#library-keyword"),
                source=self._value("#library-source"),
                status=status,
                min_if=self._number(self._value("#library-min-if")),
                max_if=self._number(self._value("#library-max-if")),
                limit=self._integer(self._value("#library-limit"), 100),
            )
        table = self.query_one("#papers-table", DataTable)
        table.clear()
        for row in rows:
            factor = (
                f"{row['impact_factor']:.3f} / {row['impact_factor_year']}"
                if row["impact_factor"] is not None else "-"
            )
            table.add_row(
                row["year"] or "-", row["title"], row["journal"] or "-", factor,
                row["sources"] or "-", "✓" if row["pdf_path"] else "-", key=str(row["id"]),
            )

    def _append_log(self, selector: str, message: str) -> None:
        self.query_one(selector, RichLog).write(message)

    def _finish_task(self, selector: str, message: str, severity: str = "information") -> None:
        self._append_log(selector, message)
        self.notify(message, severity=severity)
        button_selector = {
            "#search-log": "#search-start",
            "#wos-log": "#wos-start",
            "#download-log": "#download-start",
            "#impact-log": "#impact-import",
        }.get(selector)
        if button_selector:
            self.query_one(button_selector, Button).disabled = False
        if selector == "#download-log":
            self.query_one("#download-preflight", Button).disabled = False
        self.refresh_dashboard()
        self.refresh_library()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "dashboard-refresh":
            self.refresh_dashboard()
        elif button_id == "library-refresh":
            self.refresh_library()
        elif button_id == "dashboard-clear":
            self.push_screen(ClearDatabaseScreen(), self._handle_clear_database)
        elif button_id == "search-import":
            path = Path(self._value("#search-input-file")).expanduser()
            if not path.is_file():
                self.notify(f"TXT 文件不存在：{path}", severity="error")
                return
            values = []
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                value = raw.strip()
                if value and not value.startswith("#") and value not in values:
                    values.append(value)
            if not values:
                self.notify("TXT 中没有有效关键词", severity="warning")
                return
            self.query_one("#search-keywords", TextArea).text = "\n".join(values)
            self.notify(f"已导入 {len(values)} 个关键词", severity="information")
        elif button_id == "search-start":
            keywords = [item.strip() for item in re.split(r"[,，\n]+", self._value("#search-keywords")) if item.strip()]
            if not keywords:
                self.notify("请先输入检索关键词", severity="warning")
                return
            source_controls = (
                ("source-wos", "WOS"), ("source-pubmed", "PubMed"),
                ("source-crossref", "Crossref"), ("source-s2", "S2"),
                ("source-cnki", "CNKI"), ("source-europe-pmc", "Europe PMC"),
            )
            sources = [name for control_id, name in source_controls if self.query_one(f"#{control_id}", Checkbox).value]
            if not sources:
                self.notify("请至少勾选一个检索源", severity="warning")
                return
            self.query_one("#search-log", RichLog).clear()
            event.button.disabled = True
            self.run_search_worker(
                keywords, sources, self._integer(self._value("#search-limit"), 100, minimum=0),
                self._value("#search-email"),
            )
        elif button_id == "wos-start":
            keywords = [item.strip() for item in re.split(r"[,，\n]+", self._value("#wos-keywords")) if item.strip()]
            if not keywords:
                self.notify("请先输入 WOS 检索关键词", severity="warning")
                return
            max_records = self._integer(self._value("#wos-max-records"), 1000, minimum=0)
            interval = self._number(self._value("#wos-interval"))
            if interval is None or interval < 0:
                self.notify("请求间隔必须大于等于 0", severity="warning")
                return
            self.query_one("#wos-log", RichLog).clear()
            event.button.disabled = True
            self.run_wos_worker(
                keywords, max_records, interval,
                Path(self._value("#wos-manifest") or "wos_api_runs/manifest.json"),
                self.query_one("#wos-resume", Checkbox).value,
            )
        elif button_id == "download-start":
            self.query_one("#download-log", RichLog).clear()
            event.button.disabled = True
            status = self.query_one("#download-status", Select).value
            self.run_download_worker(
                self._value("#download-keyword"), self._value("#download-source"), str(status),
                self._value("#download-mode") or "authorized", Path(self._value("#download-out") or "downloads"),
                self._integer(self._value("#download-limit"), 100),
                self._integer(self._value("#download-rpm"), 30), self._value("#download-email"),
                self._number(self._value("#download-min-if")), self._number(self._value("#download-max-if")),
            )
        elif button_id == "download-preflight":
            event.button.disabled = True
            self.query_one("#download-log", RichLog).clear()
            self.run_preflight_worker(
                self._value("#download-keyword"), self._value("#download-source"),
                self._integer(self._value("#download-limit"), 100), self._value("#download-email"),
            )
        elif button_id == "impact-import":
            path = Path(self._value("#impact-file")).expanduser()
            if not path.is_file():
                self.notify("JIF 文件不存在", severity="error")
                return
            year_text = self._value("#impact-year")
            year = self._integer(year_text, 0, minimum=0) or None
            self.query_one("#impact-log", RichLog).clear()
            event.button.disabled = True
            self.run_impact_worker(path, self._value("#impact-source") or "JCR", year)

    @work(thread=True, exclusive=True, group="search")
    def run_search_worker(self, keywords: list[str], sources: list[str], limit: int, email: str) -> None:
        try:
            papers = search_to_database(
                keywords, sources, limit, email, self.db_path,
                progress=lambda message: self.call_from_thread(self._append_log, "#search-log", message),
            )
            self.call_from_thread(self._finish_task, "#search-log", f"检索完成：{len(papers)} 篇")
        except Exception as exc:
            self.call_from_thread(self._finish_task, "#search-log", f"检索失败：{exc}", "error")

    @work(thread=True, exclusive=True, group="download")
    def run_download_worker(
        self, keyword: str, source: str, status: str, mode: str, out_dir: Path,
        limit: int, rpm: int, email: str, min_if: float | None, max_if: float | None,
    ) -> None:
        try:
            result = download_database_queue(
                self.db_path, out_dir, mode, rpm, email, keyword, source, status,
                limit, min_if, max_if,
                progress=lambda message: self.call_from_thread(self._append_log, "#download-log", message),
            )
            severity = "information" if result["failed"] == 0 else "warning"
            self.call_from_thread(
                self._finish_task, "#download-log",
                f"下载完成：成功 {result['success']}，失败 {result['failed']}", severity,
            )
        except Exception as exc:
            self.call_from_thread(self._finish_task, "#download-log", f"下载失败：{exc}", "error")

    @work(thread=True, exclusive=True, group="preflight")
    def run_preflight_worker(self, keyword: str, source: str, limit: int, email: str) -> None:
        try:
            result = preflight_download_candidates(
                self.db_path, email, limit, keyword, source,
                progress=lambda message: self.call_from_thread(self._append_log, "#download-log", message),
            )
            self.call_from_thread(
                self._finish_task, "#download-log",
                f"预解析完成：发现候选 {result['with_candidates']} 篇",
            )
        except Exception as exc:
            self.call_from_thread(self._finish_task, "#download-log", f"预解析失败：{exc}", "error")

    @work(thread=True, exclusive=True, group="wos")
    def run_wos_worker(
        self, keywords: list[str], max_records: int, interval: float,
        manifest_path: Path, resume: bool,
    ) -> None:
        try:
            result = fetch_wos_to_database(
                keywords, max_records, self.db_path, manifest_path, interval, resume,
                progress=lambda message: self.call_from_thread(self._append_log, "#wos-log", message),
            )
            self.call_from_thread(
                self._finish_task, "#wos-log", f"WOS 完成：本次处理 {result['saved']} 条"
            )
        except Exception as exc:
            self.call_from_thread(self._finish_task, "#wos-log", f"WOS 失败：{exc}", "error")

    @work(thread=True, exclusive=True, group="impact")
    def run_impact_worker(self, path: Path, source: str, year: int | None) -> None:
        try:
            with PaperDatabase(self.db_path) as database:
                result = database.import_impact_factors(path, source, year)
            message = (
                f"导入 {result['imported']}，跳过 {result['skipped']}，"
                f"匹配论文 {result['matched_papers']}"
            )
            self.call_from_thread(self._finish_task, "#impact-log", message)
        except Exception as exc:
            self.call_from_thread(self._finish_task, "#impact-log", f"导入失败：{exc}", "error")

    def action_refresh(self) -> None:
        self.refresh_dashboard()
        self.refresh_library()

    def _handle_clear_database(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            # 在数据库连接内先做 WAL checkpoint，确保复制出的备份完整可恢复。
            with PaperDatabase(self.db_path) as database:
                database.connection.execute("PRAGMA wal_checkpoint(FULL)")
                backup = self.db_path.with_name(
                    f"{self.db_path.stem}.before-clear-{datetime.now().strftime('%Y%m%d-%H%M%S')}{self.db_path.suffix}.bak"
                )
                shutil.copy2(self.db_path, backup)
                counts = database.clear_all()
            removed = counts.get("papers", 0)
            self.refresh_dashboard()
            self.refresh_library()
            self.notify(f"已清空 {removed} 篇，备份：{backup.name}", severity="information", timeout=8)
        except Exception as exc:
            self.notify(f"清空失败：{exc}", severity="error")


def run_tui(db_path: Path | str = Path("paperflow.db")) -> int:
    PaperflowTui(db_path).run()
    return 0
