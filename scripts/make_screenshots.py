r"""Regenerate the README screenshots (docs/screenshots/*.png) from a demo library.

Usage (from the repository root, with the language model already downloaded):
    .venv\Scripts\python.exe scripts\make_screenshots.py --model-cache MODEL_CACHE_DIR
(the model cache is usually %APPDATA%\AutoShelf\cache\models)

Everything happens in a temporary folder with made-up files; your own library and data
folder are never used. Paths shown in the app are replaced by neutral ones.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")  # real fonts; no window is shown

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from autoshelf.app_service import AppService
from autoshelf.config import get_app_paths
from autoshelf.db.connection import open_database
from autoshelf.ml.embedder import load_embedder
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Pipeline
from autoshelf.ui.main_window import MainWindow
from autoshelf.ui.tasks import TaskRunner

SHOWN_ROOT = r"C:\Users\you\iCloudDrive\AutoShelf"
SHOWN_HOME = r"%APPDATA%\AutoShelf"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

LIBRARY = {
    "Finance/invoice 2025-114.txt": (
        "INVOICE No. 2025-114\nWeb hosting, standard plan, 12 months: 10,000 JPY.\n"
        "Subtotal 11,500 JPY. Consumption tax 1,150 JPY. Total due 12,650 JPY. Bank transfer."
    ),
    "Finance/statement 2025-03.txt": (
        "Monthly account statement, March 2025. Opening balance 254,300 JPY. Card payment, "
        "electricity bill. Salary deposit. Closing balance 388,282 JPY."
    ),
    "Machine Learning/Unsupervised/clustering notes.md": (
        "# Clustering\nK-means alternates between assigning points to the nearest centroid and "
        "moving each centroid to the mean of its points. Choose k with the elbow method or the "
        "silhouette score. DBSCAN finds dense regions; hierarchical clustering builds a dendrogram."
    ),
    "Machine Learning/Unsupervised/PCA lecture.md": (
        "# Principal component analysis\nPCA projects data onto orthogonal directions of "
        "maximal variance, the eigenvectors of the covariance matrix."
    ),
    "Machine Learning/Deep Learning/backpropagation.md": (
        "# Backpropagation\nThe chain rule gives the gradient of the loss for every weight of a "
        "neural network. Train with stochastic gradient descent, dropout and batch norm."
    ),
    "Recipes/miso soup.md": "# 味噌汁\nだしを取り、豆腐とわかめを入れて味噌を溶く。",
    "Recipes/pancakes.md": (
        "# Pancakes\nMix flour, eggs, milk and sugar. Fry in butter until golden."
    ),
    "Travel/Kyoto itinerary.txt": (
        "Day 1: Shinkansen to Kyoto, Fushimi Inari. Day 2: Arashiyama bamboo forest, Kinkakuji."
    ),
    "就職活動/エントリーシート.txt": (
        "エントリーシート\n志望動機: 貴社の技術力に魅力を感じ志望しました。\n"
        "学生時代に力を入れたこと: 研究室での機械学習プロジェクト。"
    ),
}
DROPS = {
    "banana bread.md": (
        "# Banana bread\nPreheat the oven to 175C. Mash 3 ripe bananas, mix with butter, sugar, an "
        "egg and flour. Bake for 60 minutes."
    ),
    "給与明細 2025-04.txt": (
        "給与明細書 2025年4月分\n基本給 210,000円 健康保険 10,500円 所得税 4,370円"
    ),
    "UnsupervisedLearning_lecture-03.md": (
        "Lecture 3: Gaussian mixture models. The EM algorithm gives soft cluster assignments, "
        "unlike k-means. Neural networks can also learn representations."
    ),
    "scan_0034.txt": (
        "Hotel booking confirmation\nHotel Gracery Shinjuku. Check-in 12 Aug. 2 guests."
    ),
    "receipt amazon.txt": "Order receipt. USB-C cable x2 1,980 JPY. Order total 3,480 JPY.",
    "面接対策.txt": "よく聞かれる質問: 志望動機、学生時代に力を入れたこと、逆質問。",
    "todo.txt": "Shopping list: milk, eggs, bread. Call the plumber on Monday.",
}
DESCRIPTIONS = {
    "Finance": "invoices, receipts, bank statements, salary, taxes",
    "Recipes": "cooking recipes and baking",
    "Travel": "trips, hotels, bookings, itineraries",
}


def build_demo(base: Path) -> Path:
    root = base / "AutoShelf"
    for relative, text in LIBRARY.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def settle(app: QApplication, runner: TaskRunner, seconds: float = 0.6) -> None:
    for _ in range(2400):
        app.processEvents()
        if not runner.busy:
            break
        time.sleep(0.05)
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def neutral(window: MainWindow, root: Path, home: Path) -> None:
    """Replace real paths in the window with neutral ones."""
    for kind in (QLabel, QLineEdit):
        for widget in window.findChildren(kind):
            text = widget.text()
            if str(root) in text or str(home) in text:
                widget.setText(text.replace(str(root), SHOWN_ROOT).replace(str(home), SHOWN_HOME))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    base = Path(tempfile.mkdtemp(prefix="autoshelf-demo-"))
    try:
        root = build_demo(base)
        paths = get_app_paths(base / "data")
        embedder = load_embedder(MODEL, args.model_cache)
        service = AppService(paths, loader=lambda model, cache_dir: embedder)
        service.set_root(root)
        for folder, text in DESCRIPTIONS.items():
            service.describe(folder, text)
        service.add_rule("Receipts", "name", "receipt", "Finance")

        conn = open_database(paths.db_path)
        library = LibraryIndex(conn, root, embedder)
        library.refresh()
        pipeline = Pipeline(conn, root, library=library)
        moved = {}
        for name, text in DROPS.items():
            (root / name).write_text(text, encoding="utf-8")
            moved[name] = pipeline.process(root / name)
        # A correction: the user moves the payslip into Finance from the Inbox.
        payslip = moved["給与明細 2025-04.txt"]
        if payslip.dst and payslip.dst.parent.name == "_Inbox":
            service.assign(payslip.dst, "Finance", tags=["Taxes 2025"])
        bread = moved["banana bread.md"]
        if bread.dst and bread.dst.parent.name == "Recipes":
            service.tag(bread.dst, ["Baking"])
        conn.close()

        app = QApplication.instance() or QApplication(sys.argv[:1])
        runner = TaskRunner()
        window = MainWindow(service, runner, keep_running_on_close=False)
        window.resize(1180, 760)
        window.set_state("Ready: 8 folders, 13 files in them")
        window.refresh_all()
        settle(app, runner)

        def shoot(name: str, tab, prepare=None) -> None:
            window.show_tab(tab)
            settle(app, runner)
            if prepare is not None:
                prepare()
                settle(app, runner)
                settle(app, runner)
            neutral(window, root, paths.home)
            window.grab().save(str(args.out / f"{name}.png"))
            print("saved", args.out / f"{name}.png")

        shoot("inbox", window.inbox)

        def search() -> None:
            window.library.query.setText("how to choose the number of clusters")
            window.library.search()

        shoot("library", window.library, search)

        def select_auto() -> None:
            history = window.history
            for row in range(history.table.rowCount()):
                if history.table.item(row, 1).text() == "on its own":
                    history.table.selectRow(row)
                    break

        shoot("history", window.history, select_auto)
        shoot("rules", window.rules)

        def organize() -> None:
            (root / "old notes.md").write_text(
                "# Gradient descent\nSGD with momentum for neural networks.", encoding="utf-8"
            )
            (root / "electricity bill.txt").write_text(
                "Electricity bill, May 2025. Amount due 7,820 JPY, paid by bank transfer.",
                encoding="utf-8",
            )
            from autoshelf.ui.tidy_view import ROOT_CHOICE

            window.tools.organize.refresh()
            settle(app, runner)
            window.tools.organize.source.setCurrentText(ROOT_CHOICE)
            window.tools.organize.preview()

        shoot("tidy", window.tools.organize, organize)
        shoot("dashboard", window.dashboard, window.dashboard.refresh)
        shoot("settings", window.settings)
        service.release()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
