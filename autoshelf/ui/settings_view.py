"""The Settings screen."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    DoubleSpinBox,
    LineEdit,
    ListWidget,
    PasswordLineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    Slider,
    StrongBodyLabel,
    SwitchButton,
    TitleLabel,
)

from autoshelf import __version__, llm, updates, wording
from autoshelf.actions.rules import extensions as parse_extensions
from autoshelf.app_service import AppService
from autoshelf.config import CONFIDENCE_LEVELS, Settings, confidence_level
from autoshelf.llm import claude as claude_provider
from autoshelf.ml import calibrate as calibration
from autoshelf.ui.rules_view import RulesView
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import open_folder, plain, plain_tooltip

DEFAULT_LEVEL = 2  # "Balanced"
LEVEL_WORDS = (
    "almost every file waits in the Inbox for you",
    "moves a file on its own only when it is very sure",
    "moves a file on its own when one folder is a clear match",
    "asks you less often",
    "asks only when two folders are about equally good",
)
SHOW_ADVANCED = "Show advanced settings"
HIDE_ADVANCED = "Hide advanced settings"


def describe_level(index: int) -> str:
    """Text under the slider for a preset."""
    level = CONFIDENCE_LEVELS[index]
    return (
        f"{level.name}: {LEVEL_WORDS[index]}. (It moves a file when its best folder scores "
        f"at least {level.threshold:.2f} and beats the others by {level.margin:.2f}.)"
    )


def nearest_level(threshold: float) -> int:
    """The preset closest to a custom threshold, for placing the slider."""
    return min(
        range(len(CONFIDENCE_LEVELS)),
        key=lambda i: abs(CONFIDENCE_LEVELS[i].threshold - threshold),
    )


def setting_row(title: str, note: str = "", *controls: QWidget) -> QWidget:
    """One line of a settings card: what it is (and a note) on the left, controls right."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(16, 10, 16, 10)
    text = QVBoxLayout()
    text.setSpacing(2)
    if title:
        label = BodyLabel(title)
        label.setWordWrap(True)
        text.addWidget(label)
    if note:
        caption = CaptionLabel(note)
        caption.setWordWrap(True)
        text.addWidget(caption)
    layout.addLayout(text, 1)
    for control in controls:
        layout.addWidget(control)
    return row


def setting_group(title: str, *rows: QWidget) -> QWidget:
    """A titled card of setting rows."""
    group = QWidget()
    layout = QVBoxLayout(group)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(StrongBodyLabel(title))
    card = SimpleCardWidget()
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(0, 4, 0, 4)
    card_layout.setSpacing(0)
    for row in rows:
        card_layout.addWidget(row)
    layout.addWidget(card)
    return group


def switch() -> SwitchButton:
    """An on/off switch for a settings row."""
    button = SwitchButton()
    button.setOnText("On")
    button.setOffText("Off")
    return button


class SettingsView(QWidget):
    """Shows and edits settings; asks the app to restart the watcher when needed."""

    change_root_requested = Signal()
    paused_toggled = Signal(bool)
    notifications_toggled = Signal(bool)
    restart_needed = Signal(str)  # a message to show once restarted
    message = Signal(str)  # a message for the status bar
    calibration_progress = Signal(int, int)  # (files tested, of), from a worker thread
    ai_state = Signal(bool)  # whether an AI provider is chosen
    check_updates_requested = Signal()  # "Check now"
    update_requested = Signal()  # "Update…" (or "Download…" for an unzipped copy)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._settings: Settings | None = None
        self._load_number = 0  # results of older loads are ignored
        self._recommended: int | None = None

        content = QWidget()
        content.setObjectName("settingsContent")
        content.setStyleSheet("QWidget#settingsContent{background: transparent}")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 20, 28, 28)
        layout.setSpacing(20)
        layout.addWidget(TitleLabel("Settings"))
        layout.addWidget(self._folder_group())
        layout.addWidget(self._sorting_group())
        layout.addWidget(self._skipped_group())
        layout.addWidget(self._notifications_group())
        layout.addWidget(self._windows_group())
        layout.addWidget(self._updates_group())
        self.rules = RulesView(service, runner)
        self.rules.setMinimumHeight(420)
        rules_group = QWidget()
        rules_layout = QVBoxLayout(rules_group)
        rules_layout.setContentsMargins(0, 0, 0, 0)
        rules_layout.addWidget(StrongBodyLabel("Rules"))
        rules_layout.addWidget(self.rules)
        layout.addWidget(rules_group)
        self.advanced_button = PushButton(SHOW_ADVANCED)
        self.advanced_button.clicked.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced = self._advanced_settings()
        self.advanced.setVisible(False)
        layout.addWidget(self.advanced)
        layout.addStretch(1)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background: transparent; border: none}")
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # --- the groups -------------------------------------------------------------------

    def _folder_group(self) -> QWidget:
        self.root = LineEdit()
        self.root.setReadOnly(True)
        self.root.setPlaceholderText("Not chosen yet")
        self.root.setMinimumWidth(320)
        change = PushButton("Change…")
        change.clicked.connect(self.change_root_requested)
        open_root = PushButton("Open")
        open_root.clicked.connect(self._open_root)
        return setting_group(
            "Your AutoShelf folder",
            setting_row(
                "The folder AutoShelf sorts",
                "New files dropped into it, or into its _Inbox, are sorted into your own "
                f"folders. AutoShelf's own data stays in {self.service.paths.home}.",
                self.root,
                change,
                open_root,
            ),
        )

    def _sorting_group(self) -> QWidget:
        self.slider = Slider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(CONFIDENCE_LEVELS) - 1)
        self.slider.setPageStep(1)
        self.slider.setMinimumWidth(260)
        self.slider.valueChanged.connect(self._level_changed)
        self.level_text = CaptionLabel()
        self.level_text.setWordWrap(True)
        slider_row = QWidget()
        slider_layout = QVBoxLayout(slider_row)
        slider_layout.setContentsMargins(16, 10, 16, 10)
        line = QHBoxLayout()
        line.addWidget(BodyLabel("How automatic"), 1)
        line.addWidget(CaptionLabel("Cautious"))
        line.addWidget(self.slider, 2)
        line.addWidget(CaptionLabel("Automatic"))
        slider_layout.addLayout(line)
        slider_layout.addWidget(self.level_text)

        self.calibrate_button = PushButton("Test it")
        self.calibrate_button.setToolTip(
            "Sorts every file in your folders as if it had just arrived and compares the "
            "result with where it is. Nothing is moved."
        )
        self.calibrate_button.clicked.connect(self.calibrate)
        self.use_recommended = PrimaryPushButton("Use the recommended setting")
        self.use_recommended.clicked.connect(self._use_recommended)
        self.use_recommended.setVisible(False)
        self.calibration_text = plain(CaptionLabel())
        self.calibration_text.setWordWrap(True)
        self.calibration_text.setVisible(False)
        self.calibration_progress.connect(self._show_calibration_progress)
        calibrate_row = setting_row(
            "How well would AutoShelf sort my library?",
            "Sorts your files as if they were new and compares; nothing moves",
            self.use_recommended,
            self.calibrate_button,
        )
        calibration_box = QWidget()
        calibration_layout = QVBoxLayout(calibration_box)
        calibration_layout.setContentsMargins(16, 0, 16, 10)
        calibration_layout.addWidget(self.calibration_text)

        self.paused = switch()
        self.paused.checkedChanged.connect(self._paused_changed)
        return setting_group(
            "Sorting",
            slider_row,
            calibrate_row,
            calibration_box,
            setting_row("Pause sorting", "New files wait until you turn this off", self.paused),
        )

    def _skipped_group(self) -> QWidget:
        self.skipped = ListWidget()
        self.skipped.setMaximumHeight(90)
        self.skipped.setSelectionMode(ListWidget.SelectionMode.ExtendedSelection)
        read_selected = PushButton("Read selected again")
        read_selected.clicked.connect(self.clear_selected_skipped)
        read_all = PushButton("Read all again")
        read_all.clicked.connect(self.clear_all_skipped)
        buttons = QVBoxLayout()
        buttons.addWidget(read_selected)
        buttons.addWidget(read_all)
        buttons.addStretch(1)
        list_row = QWidget()
        list_layout = QHBoxLayout(list_row)
        list_layout.setContentsMargins(16, 0, 16, 10)
        list_layout.addWidget(self.skipped, 1)
        list_layout.addLayout(buttons)
        self.skipped_box = setting_group(
            "Files sorted by name only",
            setting_row(
                "AutoShelf stopped while reading these files",
                "So it sorts them by their names only, to avoid stopping again. A power cut "
                "or a forced restart looks the same as a crash: if that is what happened, "
                "read them again.",
            ),
            list_row,
        )
        self.skipped_box.setVisible(False)  # shown when there are any
        return self.skipped_box

    def _notifications_group(self) -> QWidget:
        self.notifications = switch()
        self.notifications.checkedChanged.connect(self._notifications_changed)
        self.buttons = switch()
        self.buttons.setToolTip(
            "Registers AutoShelf with Windows for its notifications (the installer does this "
            "too). Without it, a notification opens the Inbox when you click it."
        )
        self.buttons.checkedChanged.connect(self._buttons_changed)
        self.weekly = switch()
        self.weekly.checkedChanged.connect(lambda on: self._update(None, False, weekly_digest=on))
        return setting_group(
            "Notifications",
            setting_row("Ask me when a file needs a decision", "", self.notifications),
            setting_row(
                "Folder buttons in the notification",
                "Put a file away without opening AutoShelf",
                self.buttons,
            ),
            setting_row(
                "A short summary once a week",
                "How many files were sorted, are waiting, and what AutoShelf learned",
                self.weekly,
            ),
        )

    def _windows_group(self) -> QWidget:
        self.startup = switch()
        self.startup.checkedChanged.connect(self._startup_changed)
        self.explorer_menu = switch()
        self.explorer_menu.setToolTip(
            "Sort with AutoShelf, Why is this here?, Compress, and Convert for the file "
            "types AutoShelf can convert"
        )
        self.explorer_menu.checkedChanged.connect(self._explorer_menu_changed)
        return setting_group(
            "Windows",
            setting_row("Start AutoShelf when I sign in", "It starts in the tray", self.startup),
            setting_row(
                "AutoShelf in Explorer's right-click menu",
                "Sort, Why is this here?, Compress and Convert (on Windows 11: under "
                "\u201cShow more options\u201d)",
                self.explorer_menu,
            ),
        )

    def _updates_group(self) -> QWidget:
        self.update_button = PrimaryPushButton("Update…")
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self.update_requested)
        self.check_button = PushButton("Check now")
        self.check_button.clicked.connect(self.check_updates_requested)
        version = setting_row(f"AutoShelf {__version__}", "", self.update_button, self.check_button)
        self.update_text = CaptionLabel("Click Check now to look for a newer version.")
        self.update_text.setWordWrap(True)
        version.layout().itemAt(0).layout().addWidget(self.update_text)
        self.auto_updates = switch()
        self.auto_updates.checkedChanged.connect(
            lambda on: self._update(None, False, check_updates=on)
        )
        return setting_group(
            "Updates",
            version,
            setting_row(
                "Look for a newer version once a day",
                "On GitHub. Nothing is downloaded or installed until you click Update.",
                self.auto_updates,
            ),
        )

    def checking_updates(self) -> None:
        """Say that AutoShelf is looking for a newer version."""
        self.check_button.setEnabled(False)
        self.update_text.setText("Looking for a newer version...")

    def show_update(
        self, release: updates.Release | None, mode: str, error: str | None = None
    ) -> None:
        """Show what the last look for a newer version found."""
        self.check_button.setEnabled(True)
        offer = release is not None and mode != updates.SOURCE
        self.update_button.setVisible(offer)
        self.update_button.setText("Download…" if mode == updates.PORTABLE else "Update…")
        if mode == updates.SOURCE:
            found = f"AutoShelf {release.version} is available. " if release else ""
            text = found + "Running from source: update with git pull."
        elif error:
            text = f"Couldn't check for a newer version: {error}"
        elif release is not None:
            text = f"AutoShelf {release.version} is available."
            if mode == updates.PORTABLE:
                text += " This copy wasn't installed with setup.exe: download the new one."
        else:
            text = "You have the newest version."
        self.update_text.setText(text)

    def _advanced_settings(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        layout.addWidget(self._files_group())
        layout.addWidget(self._watched_group())
        layout.addWidget(self._ai_group())
        self.model = CaptionLabel()
        self.model.setWordWrap(True)
        self.model.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.model)
        return box

    def _files_group(self) -> QWidget:
        self.stable = DoubleSpinBox()
        self.stable.setRange(1.0, 120.0)
        self.stable.setDecimals(0)
        self.stable.setSuffix(" s")
        self.sweep = DoubleSpinBox()
        self.sweep.setRange(0.0, 120.0)
        self.sweep.setDecimals(0)
        self.sweep.setSuffix(" min")
        self.sweep.setSpecialValueText("never")
        self.sweep.setToolTip(
            "Windows can miss new files in busy iCloud folders. AutoShelf looks at the "
            "folder again this often, so no file stays unsorted until the next start."
        )
        self.patterns = PlainTextEdit()
        self.patterns.setPlaceholderText("One pattern per line, e.g. *.tmp")
        self.patterns.setFixedHeight(110)
        self.patterns.setMinimumWidth(260)
        apply = PushButton("Apply")
        apply.clicked.connect(self._apply_files)
        self.sort_created = switch()
        self.sort_created.checkedChanged.connect(
            lambda checked: self._update(None, False, sort_created_files=checked)
        )
        return setting_group(
            "New files",
            setting_row("Wait until a new file stops changing", "", self.stable),
            setting_row("Look for files Windows missed, every", "0: never", self.sweep),
            setting_row(
                "Ignore files named like",
                "One pattern per line. Temporary and partial files are always ignored.",
                self.patterns,
            ),
            setting_row("", "Saves the three settings above", apply),
            setting_row(
                "Sort compressed and converted copies like new files",
                "When they land in your AutoShelf folder or its Inbox",
                self.sort_created,
            ),
        )

    def _watched_group(self) -> QWidget:
        self.watched = ListWidget()
        self.watched.setFixedHeight(80)
        self.watched.setMinimumWidth(260)
        add_watched = PushButton("Add folder…")
        add_watched.clicked.connect(self.add_watched)
        remove_watched = PushButton("Remove")
        remove_watched.clicked.connect(self.remove_watched)
        self.extensions = LineEdit()
        self.extensions.setPlaceholderText(".pdf, .docx")
        self.extensions.setMinimumWidth(260)
        save_extensions = PushButton("Apply")
        save_extensions.clicked.connect(self._apply_extensions)
        buttons = QWidget()
        buttons_layout = QVBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.addWidget(add_watched)
        buttons_layout.addWidget(remove_watched)
        return setting_group(
            "Take new files from other folders",
            setting_row(
                "Folders to watch",
                "For example Downloads. New files with these extensions are moved into your "
                "library and sorted; undo puts them back. Files already there, and "
                "subfolders, are left alone.",
                self.watched,
                buttons,
            ),
            setting_row("Take files ending in", "", self.extensions, save_extensions),
        )

    def _ai_group(self) -> QWidget:
        self.ai_provider = QComboBox()
        for key, name in llm.PROVIDER_NAMES.items():
            self.ai_provider.addItem(name, key)
        self.ai_provider.currentIndexChanged.connect(self._ai_provider_changed)
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setMinimumContentsLength(22)
        self.ai_bulk_model = QComboBox()
        self.ai_bulk_model.setEditable(True)
        self.ai_bulk_model.setMinimumContentsLength(22)
        self.ai_bulk_model.setToolTip(
            "Many small requests: the cheapest model is the default. Choose a bigger one "
            "for better summaries, names and flashcards."
        )
        self.api_key = PasswordLineEdit()
        self.api_key.setPlaceholderText("Paste your Claude API key")
        self.api_key.setMinimumWidth(260)
        self.key_status = CaptionLabel()
        save_key = PushButton("Save key")
        save_key.clicked.connect(self._save_key)
        forget_key = PushButton("Forget key")
        forget_key.clicked.connect(self._forget_key)
        self.ollama_url = LineEdit()
        self.ollama_url.setMinimumWidth(260)
        apply_ai = PrimaryPushButton("Apply")
        apply_ai.clicked.connect(self._apply_ai)
        test_ai = PushButton("Test")
        test_ai.clicked.connect(self.test_ai)
        self.ai_result = plain(CaptionLabel())
        self.ai_result.setWordWrap(True)
        result_row = QWidget()
        result_layout = QHBoxLayout(result_row)
        result_layout.setContentsMargins(16, 10, 16, 10)
        result_layout.addWidget(self.ai_result, 1)
        result_layout.addWidget(test_ai)
        result_layout.addWidget(apply_ai)
        return setting_group(
            "AI (optional)",
            setting_row(
                "Provider",
                "Summaries, answers from your files, study tools. Sorting never uses it. "
                + llm.PRIVACY_NOTE,
                self.ai_provider,
            ),
            setting_row("Model for questions", "", self.ai_model),
            setting_row(f"Model for {llm.BULK_TASKS}", "", self.ai_bulk_model),
            setting_row("Claude API key", "", self.api_key, save_key, forget_key),
            setting_row("", "", self.key_status),
            setting_row("Ollama address", "", self.ollama_url),
            result_row,
        )

    def _toggle_advanced(self) -> None:
        shown = not self.advanced.isVisible()
        self.advanced.setVisible(shown)
        self.advanced_button.setText(HIDE_ADVANCED if shown else SHOW_ADVANCED)

    # --- loading ------------------------------------------------------------------

    def load(self) -> None:
        """Reload the settings in the background."""
        self._load_number += 1
        number = self._load_number

        def loaded(settings: Settings) -> None:
            if number == self._load_number:  # a newer load may already be under way
                self.show_settings(settings)

        self.runner.run(self.service.settings, loaded)

        def startup_loaded(enabled: bool) -> None:
            self.startup.blockSignals(True)
            self.startup.setChecked(enabled)
            self.startup.blockSignals(False)

        self.runner.run(self.service.starts_with_windows, startup_loaded, lambda exc: None)

        def menu_loaded(added: bool) -> None:
            self.explorer_menu.blockSignals(True)
            self.explorer_menu.setChecked(added)
            self.explorer_menu.blockSignals(False)

        self.runner.run(self.service.explorer_menu, menu_loaded, lambda exc: None)

        def buttons_loaded(ready: bool) -> None:
            self.buttons.blockSignals(True)
            self.buttons.setChecked(ready)
            self.buttons.blockSignals(False)

        self.runner.run(self.service.notification_buttons, buttons_loaded, lambda exc: None)
        self.runner.run(self.service.skipped_files, self.show_skipped, lambda exc: None)

    def show_skipped(self, skipped) -> None:
        """List the files that are only sorted by name (hidden when there are none)."""
        self.skipped.clear()
        for item in skipped:
            entry = QListWidgetItem(item.path.name)
            entry.setData(Qt.ItemDataRole.UserRole, str(item.path))
            entry.setToolTip(plain_tooltip(str(item.path)))
            self.skipped.addItem(entry)
        self.skipped_box.setVisible(bool(skipped))

    def clear_selected_skipped(self) -> None:
        """Read the selected skipped files again."""
        paths = [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.skipped.selectedItems()]
        if paths:
            self._clear_skipped(paths)

    def clear_all_skipped(self) -> None:
        """Read every skipped file again."""
        self._clear_skipped(None)

    def _clear_skipped(self, paths: list[Path] | None) -> None:
        def cleared(count: int) -> None:
            self.message.emit(f"AutoShelf will read {count} file(s) again")
            self.runner.run(self.service.skipped_files, self.show_skipped)

        self.runner.run(lambda: self.service.clear_skipped(paths), cleared)

    def show_settings(self, settings: Settings) -> None:
        """Fill in the widgets without triggering any changes."""
        self._settings = settings
        widgets = (
            self.slider,
            self.paused,
            self.notifications,
            self.weekly,
            self.ai_provider,
            self.sort_created,
            self.auto_updates,
        )
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
        self.weekly.setChecked(settings.weekly_digest)
        self.auto_updates.setChecked(settings.check_updates)
        self.stable.setValue(settings.stable_seconds)
        self.sweep.setValue(settings.sweep_minutes)
        self.sort_created.setChecked(settings.sort_created_files)
        self.patterns.setPlainText("\n".join(settings.ignore_patterns))
        self.watched.clear()
        self.watched.addItems(settings.watched_folders)
        self.extensions.setText(", ".join(settings.watched_extensions))
        self.ai_provider.setCurrentIndex(max(0, self.ai_provider.findData(settings.llm_provider)))
        self._fill_models(settings.llm_provider, settings.llm_model, settings.llm_bulk_model)
        self.ollama_url.setText(settings.ollama_url)
        self.ai_state.emit(settings.llm_provider != llm.OFF)
        self.runner.run(
            self.service.api_key_status, lambda text: self.key_status.setText(f"Key: {text}")
        )
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

    def calibrate(self) -> None:
        """Test how well sorting would go on the files already in the folders."""
        self.calibrate_button.setEnabled(False)
        self.use_recommended.setVisible(False)
        self.calibration_text.setVisible(True)
        self.calibration_text.setText(
            "Sorting each file in your folders as if it had just arrived... (the first time "
            "loads the language model)"
        )

        def done(result: calibration.Calibration) -> None:
            self.calibrate_button.setEnabled(True)
            settings = self._settings
            current = (
                confidence_level(settings.auto_threshold, settings.auto_margin)
                if settings
                else None
            )
            self.calibration_text.setText("\n".join(wording.calibration(result, current)))
            self._recommended = result.recommended
            self.use_recommended.setVisible(
                result.recommended is not None and result.recommended != current
            )

        def failed(exc: Exception) -> None:
            self.calibrate_button.setEnabled(True)
            self.calibration_text.setText(f"Couldn't test sorting: {exc}")

        self.runner.run(
            lambda: self.service.calibrate(progress=self.calibration_progress.emit), done, failed
        )

    def _show_calibration_progress(self, done: int, total: int) -> None:
        self.calibration_text.setText(f"Sorting each file as if it were new: {done}/{total}")

    def _use_recommended(self) -> None:
        index = self._recommended
        if index is None:
            return
        self.use_recommended.setVisible(False)
        if self.slider.value() == index:
            self._level_changed(index)  # a custom setting shown at its nearest preset
        else:
            self.slider.setValue(index)  # saves it and restarts the watcher

    def _paused_changed(self, paused: bool) -> None:
        self.paused_toggled.emit(paused)

    def _notifications_changed(self, enabled: bool) -> None:
        self._update(None, False, notifications=enabled)
        self.notifications_toggled.emit(enabled)

    def _startup_changed(self, enabled: bool) -> None:
        def failed(exc: Exception) -> None:
            QMessageBox.warning(self, "Couldn't change the startup setting", str(exc))
            self.load()

        self.runner.run(lambda: self.service.set_start_with_windows(enabled), None, failed)

    def _explorer_menu_changed(self, enabled: bool) -> None:
        def failed(exc: Exception) -> None:
            QMessageBox.warning(self, "Couldn't change Explorer's menu", str(exc))
            self.load()

        self.runner.run(lambda: self.service.set_explorer_menu(enabled), None, failed)

    def _buttons_changed(self, enabled: bool) -> None:
        def failed(exc: Exception) -> None:
            QMessageBox.warning(self, "Couldn't change the notification buttons", str(exc))
            self.load()

        self.runner.run(lambda: self.service.set_notification_buttons(enabled), None, failed)

    def _apply_files(self) -> None:
        patterns = [line.strip() for line in self.patterns.toPlainText().splitlines()]
        self._update(
            "Saved",
            True,
            stable_seconds=float(self.stable.value()),
            sweep_minutes=float(self.sweep.value()),
            ignore_patterns=[pattern for pattern in patterns if pattern],
        )

    def add_watched(self) -> None:
        """Opt a folder in for watching."""
        folder = QFileDialog.getExistingDirectory(self, "Watch this folder for new files")
        if not folder:
            return
        self.runner.run(
            lambda: self.service.add_watched(Path(folder)),
            lambda path: (self.load(), self.restart_needed.emit(f"Watching {path}")),
            lambda exc: QMessageBox.warning(self, "This folder can't be watched", str(exc)),
        )

    def remove_watched(self) -> None:
        """Stop watching the selected folder."""
        item = self.watched.currentItem()
        if item is None:
            return
        folder = Path(item.text())
        self.runner.run(
            lambda: self.service.remove_watched(folder),
            lambda _: (self.load(), self.restart_needed.emit(f"No longer watching {folder}")),
        )

    def _apply_extensions(self) -> None:
        wanted = sorted(parse_extensions(self.extensions.text())) if self.extensions.text() else []
        self._update("Saved", True, watched_extensions=wanted)

    def _fill_models(self, provider: str, current: str, current_bulk: str = "") -> None:
        on = provider != llm.OFF
        for box in (self.ai_model, self.ai_bulk_model):
            box.clear()
            if provider == llm.CLAUDE:
                box.addItems(claude_provider.MODELS)
            elif provider == llm.OLLAMA:
                box.addItems(["qwen2.5", "llama3.2", "gemma3"])
            box.setEnabled(on)
        self.ai_model.setCurrentText(current or (llm.default_model(provider) if on else ""))
        self.ai_bulk_model.setCurrentText(
            llm.bulk_model(provider, current, current_bulk) if on else ""
        )

    def _ai_provider_changed(self) -> None:
        self._fill_models(self.ai_provider.currentData(), "")

    def _apply_ai(self) -> None:
        provider = self.ai_provider.currentData()
        on = provider != llm.OFF
        model = self.ai_model.currentText().strip() if on else ""
        bulk = self.ai_bulk_model.currentText().strip() if on else ""
        if model == llm.default_model(provider):
            model = ""
        if bulk == llm.default_bulk_model(provider, model):
            bulk = ""  # follow the default (and, with Ollama, the main model)
        self._update(
            None,
            False,
            llm_provider=provider,
            llm_model=model,
            llm_bulk_model=bulk,
            ollama_url=self.ollama_url.text().strip() or "http://localhost:11434",
        )
        self.ai_state.emit(provider != llm.OFF)
        self.ai_result.setText(
            "AI is off." if provider == llm.OFF else f"Using {llm.PROVIDER_NAMES[provider]}."
        )

    def _save_key(self) -> None:
        key = self.api_key.text()
        self.api_key.clear()
        self.runner.run(
            lambda: (self.service.save_api_key(key), self.service.api_key_status())[1],
            lambda text: self.key_status.setText(f"Key: {text} in the Windows Credential Manager"),
            lambda exc: QMessageBox.warning(self, "Couldn't save the key", str(exc)),
        )

    def _forget_key(self) -> None:
        self.runner.run(
            lambda: (self.service.forget_api_key(), self.service.api_key_status())[1],
            lambda text: self.key_status.setText(f"Key: {text}"),
        )

    def test_ai(self) -> None:
        """Send a tiny request to the chosen provider."""
        self.ai_result.setText("Asking...")
        self.runner.run(
            self.service.test_ai,
            self.ai_result.setText,
            lambda exc: self.ai_result.setText(f"It didn't work: {exc}"),
        )

    def _open_root(self) -> None:
        if self.root.text():
            self.runner.run(self.service.root, lambda root: root and open_folder(root))
