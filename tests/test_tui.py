import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Checkbox, DataTable, Input, Static, TabbedContent, TextArea

from paperflow.database import PaperDatabase
from paperflow.models import Paper
from paperflow.tui import PaperflowTui


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_and_library_load_from_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "tui.db"
            with PaperDatabase(db_path) as database:
                database.save_papers([
                    Paper(
                        title="TUI test paper", journal="Test Journal",
                        sources={"WOS"}, species={"tiger"},
                    )
                ])
            app = PaperflowTui(db_path)
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                self.assertEqual(app.query_one("#papers-table", DataTable).row_count, 1)
                self.assertIn("1", str(app.query_one("#stat-papers", Static).renderable))
                self.assertEqual(app.query_one("#wos-max-records", Input).value, "1000")

    async def test_import_txt_populates_search_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            txt = root / "input.txt"
            txt.write_text("# comment\nPanthera tigris\n\nPanthera tigris\nGinkgo biloba\n", encoding="utf-8")
            app = PaperflowTui(root / "tui.db")
            async with app.run_test(size=(140, 45)) as pilot:
                app.query_one(TabbedContent).active = "search"
                await pilot.pause()
                app.query_one("#search-input-file", Input).value = str(txt)
                await pilot.click("#search-import")
                self.assertEqual(app.query_one("#search-keywords", TextArea).text, "Panthera tigris\nGinkgo biloba")

    async def test_search_sources_follow_checkboxes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PaperflowTui(Path(directory) / "tui.db")
            async with app.run_test(size=(140, 45)) as pilot:
                app.query_one(TabbedContent).active = "search"
                await pilot.pause()
                app.query_one("#search-keywords", TextArea).text = "Ginkgo biloba"
                app.query_one("#source-wos", Checkbox).value = False
                app.query_one("#source-s2", Checkbox).value = False
                app.query_one("#search-limit", Input).value = "0"
                with patch.object(app, "run_search_worker") as worker:
                    await pilot.click("#search-start")
                self.assertEqual(worker.call_args.args[1], ["PubMed", "Crossref", "CNKI"])
                self.assertEqual(worker.call_args.args[2], 0)

    async def test_search_limit_defaults_to_batch_size(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PaperflowTui(Path(directory) / "tui.db")
            async with app.run_test(size=(140, 45)) as pilot:
                app.query_one(TabbedContent).active = "search"
                await pilot.pause()
                self.assertEqual(app.query_one("#search-limit", Input).value, "100")

    async def test_clear_database_requires_confirmation_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "tui.db"
            with PaperDatabase(db_path) as database:
                database.save_papers([Paper(title="To clear", sources={"WOS"})])
            app = PaperflowTui(db_path)
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.click("#dashboard-clear")
                await pilot.pause()
                await pilot.click("#clear-confirm")
                await pilot.pause()
            with PaperDatabase(db_path) as database:
                self.assertEqual(database.stats()["papers"], 0)
            self.assertEqual(len(list(Path(directory).glob("tui.before-clear-*.db.bak"))), 1)

    async def test_impact_factor_import_worker_updates_tui_and_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "tui.db"
            jif_path = root / "jif.csv"
            jif_path.write_text(
                "journal,impact_factor,year\nTest Journal,6.5,2024\n", encoding="utf-8"
            )
            with PaperDatabase(db_path) as database:
                database.save_papers([Paper(title="Matched", journal="TEST JOURNAL")])

            app = PaperflowTui(db_path)
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                app.query_one(TabbedContent).active = "impact"
                await pilot.pause()
                app.query_one("#impact-file", Input).value = str(jif_path)
                await pilot.click("#impact-import")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn("1", str(app.query_one("#stat-matched", Static).renderable))

            with PaperDatabase(db_path) as database:
                row = database.list_papers(limit=1)[0]
                self.assertEqual(row["impact_factor"], 6.5)


if __name__ == "__main__":
    unittest.main()
