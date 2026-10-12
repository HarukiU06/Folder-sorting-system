"""The Settings screen."""

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService
from autoshelf.config import CONFIDENCE_LEVELS, Settings, confidence_level
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import open_folder

DEFAULT_LEVEL = 2  # "Balanced"


def describe_level(index: int) -> str:
    """Text under the slider for a preset."""
    level = CONFIDENCE_LEVELS[index]
    return (
        f"{level.name}: a file is moved on its own only if its best folder scores at least "
        f"{level.threshold:.2f} and beats every other folder by {level.margin:.2f}. "
        "Everything else waits in the Inbox."
    )


def nearest_level(threshold: float) -> int:
    """The preset closest to a custom threshold, for placing the slider."""
    return min(
        range(len(CONFIDENCE_LEVELS)),
        key=lambda i: abs(CONFIDENCE_LEVELS[i].threshold - threshold),
    )


class SettingsView(QWidget):
    """Shows and edits settings; asks the app to restart the watcher when needed."""

    change_root_requested = Signal()
    paused_toggled = Signal(bool)
    notifications_toggled = Signal(bool)
    restart_needed = Signal(str)  # a message to show once restarted

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._settings: Settings | None = None
        self._load_number = 0  # results of older loads are ignored

        # Library folder
        self.root = QLineEdit()
        self.root.setReadOnly(True)
        self.root.setPlaceholderText("Not chosen yet")
        change = QPushButton("Change...")
        change.clicked.connect(self.change_root_requested)
        open_root = QPushButton("Open")
        open_root.clicked.connect(self._open_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root, 1)
        root_row.addWidget(change)
        root_row.addWidget(open_root)
        root_box = QGroupBox("Folder to sort")
        root_layout = QVBoxLayout(root_box)
        root_layout.addLayout(root_row)
        note = QLabel(
            "Pick the folder you keep your sorted files in, for example one inside iCloud "
            "Drive. New files dropped into it (or into its _Inbox) are sorted into your own "
            f"subfolders. AutoShelf's own data stays in {service.paths.home}."
        )
        note.setWordWrap(True)
        root_layout.addWidget(note)

        # Sorting
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(CONFIDENCE_LEVELS) - 1)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._level_changed)
        self.level_text = QLabel()
        self.level_text.setWordWrap(True)
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Cautious"))
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(QLabel("Automatic"))
        self.paused = QCheckBox("Pause automatic sorting (new files wait until you resume)")
        self.paused.toggled.connect(self._paused_changed)
        self.notifications = QCheckBox("Notify me when a file needs my decision")
        self.notifications.toggled.connect(self._notifications_changed)
        sorting_box = QGroupBox("Sorting")
        sorting = QVBoxLayout(sorting_box)
        sorting.addLayout(slider_row)
        sorting.addWidget(self.level_text)
        sorting.addWidget(self.paused)
        sorting.addWidget(self.notifications)

        # Files
        self.stable = QDoubleSpinBox()
        self.stable.setRange(1.0, 120.0)
        self.stable.setDecimals(0)
        self.stable.setSuffix(" s")
        self.stable.setMaximumWidth(110)
        self.patterns = QPlainTextEdit()
        self.patterns.setPlaceholderText("One pattern per line, e.g. *.tmp")
        self.patterns.setMaximumHeight(110)
        apply = QPushButton("Apply")
        apply.clicked.connect(self._apply_files)
        files_box = QGroupBox("New files")
        files = QFormLayout(files_box)
        files.addRow("Wait until a file is unchanged for", self.stable)
        files.addRow("Ignore files named like", self.patterns)
        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        apply_row.addWidget(apply)
        files.addRow("", apply_row)

        self.model = QLabel()
        self.model.setWordWrap(True)
        self.model.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.addWidget(root_box)
        layout.addWidget(sorting_box)
        layout.addWidget(files_box)
        layout.addWidget(self.model)
        layout.addStretch(1)

    # --- loading ------------------------------------------------------------------

    def load(self) -> None:
        """Reload the settings in the background."""
        self._load_number += 1
        number = self._load_number

        def loaded(settings: Settings) -> None:
            if number == self._load_number:  # a newer load may already be under way
                self.show_settings(settings)

        self.runner.run(self.service.settings, loaded)

    def show_settings(self, settings: Settings) -> None:
        """Fill in the widgets without triggering any changes."""
        self._settings = settings
        widgets = (self.slider, self.paused, self.notifications)
        for widget in widgets:
            widget.blockSignals(True)
        self.root.setText(settings.root or "")
        level = confidence_level(settings.auto_threshold, settings.auto_margin)
        if level is None:
            self.slider.setValue(nearest_level(settings.auto_threshold))
            self.level_text.setText(
                f"Custom: threshold {settings.auto_threshold:.2f}, margin "
                f"{settings.auto_margin:.2f} (set with 'autoshelf config'). Move the slider "
                "to use a preset instead."
            )
        else:
            self.slider.setValue(level)
            self.level_text.setText(describe_level(level))
        self.paused.setChecked(settings.paused)
        self.notifications.setChecked(settings.notifications)
        self.stable.setValue(settings.stable_seconds)
        self.patterns.setPlainText("\n".join(settings.ignore_patterns))
        self.model.setText(
            f"Language model: {settings.embedding_model} (runs on this PC; stored in "
            f"{self.service.paths.model_dir})"
        )
        for widget in widgets:
            widget.blockSignals(False)

    # --- changes ------------------------------------------------------------------

    def _update(self, done_message: str | None, restart: bool, **changes) -> None:
        def save() -> Settings:
            settings = replace(self.service.settings(), **changes)
            self.service.save_settings(settings)
            return settings

        def saved(settings: Settings) -> None:
            self._settings = settings
            if restart:
                self.restart_needed.emit(done_message or "Settings saved")

        self.runner.run(save, saved)

    def _level_changed(self, index: int) -> None:
        level = CONFIDENCE_LEVELS[index]
        self.level_text.setText(describe_level(index))
        self._update(
            f"Sorting is now {level.name.lower()}",
            True,
            auto_threshold=level.threshold,
            auto_margin=level.margin,
        )

    def _paused_changed(self, paused: bool) -> None:
        self.paused_toggled.emit(paused)

    def _notifications_changed(self, enabled: bool) -> None:
        self._update(None, False, notifications=enabled)
        self.notifications_toggled.emit(enabled)

    def _apply_files(self) -> None:
        patterns = [line.strip() for line in self.patterns.toPlainText().splitlines()]
        self._update(
            "Saved",
            True,
            stable_seconds=float(self.stable.value()),
            ignore_patterns=[pattern for pattern in patterns if pattern],
        )

    def _open_root(self) -> None:
        if self.root.text():
            self.runner.run(self.service.root, lambda root: root and open_folder(root))
