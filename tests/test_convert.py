"""Compress and convert (Phase 13): new files next to the originals, never in place."""

import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import samples
from fakes import FakeEmbedder
from helpers import make
from PIL import Image

from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.features import convert
from autoshelf.features.convert import ConvertError, Job
from autoshelf.pipeline import OutcomeKind, Pipeline


def png(path: Path, size=(40, 30), *, alpha: bool = False, noise: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "RGBA" if alpha else "RGB"
    if noise:
        image = Image.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))
    else:
        image = Image.new(mode, size, (200, 30, 30, 0) if alpha else (200, 30, 30))
    image.save(path)
    return path


# --- planning ----------------------------------------------------------------------------------


def test_what_compressing_makes(tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    photo = tmp_path / "photo.jpg"
    notes = tmp_path / "notes.txt"
    assert convert.plan_compress([pdf]) == Job("shrink_pdf", (pdf,), "report (smaller).pdf")
    assert convert.plan_compress([photo]).operation == "shrink_image"
    assert convert.plan_compress([notes]) == Job("zip", (notes,), "notes.zip")
    assert convert.plan_compress([pdf], zip_always=True).name == "report.zip"
    assert convert.plan_compress([notes, pdf, photo]).name == "notes and 2 more.zip"
    assert convert.plan_compress([tmp_path / "scan.JPEG"]).name == "scan (smaller).JPEG"
    with pytest.raises(ConvertError):
        convert.plan_compress([])


def test_what_can_be_converted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert convert.targets(tmp_path / "a.png") == ["jpg", "webp", "bmp", "tiff", "pdf"]
    assert "png" in convert.targets(tmp_path / "a.JPEG")
    assert convert.targets(tmp_path / "a.pdf") == ["docx"]
    assert convert.targets(tmp_path / "a.txt") == []
    monkeypatch.setattr(convert, "office_converter", lambda kind: None)
    assert convert.targets(tmp_path / "a.docx") == []  # hidden without Office or LibreOffice
    monkeypatch.setattr(convert, "office_converter", lambda kind: "libreoffice")
    assert convert.targets(tmp_path / "a.xlsx") == ["pdf"]
    monkeypatch.setattr(convert, "heic_supported", lambda: False)
    assert convert.targets(tmp_path / "a.heic") == []  # needs the optional pillow-heif
    monkeypatch.setattr(convert, "heic_supported", lambda: True)
    assert "jpg" in convert.targets(tmp_path / "a.heic")
    assert convert.plan_compress([tmp_path / "a.heic"]).name == "a (smaller).jpg"

    jobs = convert.plan_convert([tmp_path / "a.png", tmp_path / "b.jpg"], "pdf")
    assert [(job.operation, job.name) for job in jobs] == [("images_to_pdf", "a and 1 more.pdf")]
    jobs = convert.plan_convert([tmp_path / "a.png", tmp_path / "b.jpg"], "WEBP")
    assert [job.name for job in jobs] == ["a.webp", "b.webp"]
    with pytest.raises(ConvertError, match="can't be converted to DOCX"):
        convert.plan_convert([tmp_path / "a.png"], "docx")


# --- making the files ----------------------------------------------------------------------------


@pytest.fixture
def work(tmp_path: Path) -> Path:
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def test_zip_keeps_files_with_the_same_name(tmp_path: Path, work: Path) -> None:
    first = make(tmp_path / "a" / "notes.txt", "first")
    second = make(tmp_path / "b" / "notes.txt", "second")
    out = convert.run(convert.plan_compress([first, second]), work)
    with zipfile.ZipFile(out) as archive:
        assert sorted(archive.namelist()) == ["notes (2).txt", "notes.txt"]
        assert archive.read("notes (2).txt") == b"second"
    assert first.read_text(encoding="utf-8") == "first"  # the originals are untouched


def test_shrinking_an_image(tmp_path: Path, work: Path) -> None:
    big = png(tmp_path / "scan.png", (3200, 1600), noise=True)
    out = convert.run(convert.plan_compress([big]), work)
    with Image.open(out) as image:
        assert max(image.size) == convert.MAX_SIDE
    assert out.stat().st_size < big.stat().st_size
    tiny = png(tmp_path / "dot.png", (2, 2))
    with pytest.raises(ConvertError, match="already as small"):
        convert.run(convert.plan_compress([tiny]), work)


def test_shrinking_a_pdf_with_a_big_image(tmp_path: Path, work: Path) -> None:
    import pymupdf

    picture = png(tmp_path / "picture.png", (2400, 2400), noise=True)
    source = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, filename=str(picture))
        doc.save(source)
    out = convert.run(convert.plan_compress([source]), work)
    assert out.stat().st_size < source.stat().st_size
    with pymupdf.open(out) as doc:
        assert doc.page_count == 1


def test_converting_images(tmp_path: Path, work: Path) -> None:
    clear = png(tmp_path / "logo.png", alpha=True)
    (job,) = convert.plan_convert([clear], "jpg")
    with Image.open(convert.run(job, work)) as image:
        assert (image.format, image.mode) == ("JPEG", "RGB")
        assert image.getpixel((5, 5))[0] > 240  # transparent became white
    photos = [png(tmp_path / f"page {number}.png") for number in range(3)]
    (job,) = convert.plan_convert(photos, "pdf")
    import pymupdf

    with pymupdf.open(convert.run(job, work)) as doc:
        assert doc.page_count == 3


def test_pdf_to_word(tmp_path: Path, work: Path) -> None:
    import docx

    source = samples.pdf(tmp_path / "lecture.pdf", ["Gradient descent lecture notes"])
    (job,) = convert.plan_convert([source], "docx")
    document = docx.Document(str(convert.run(job, work)))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "Gradient descent" in text


def test_office_files_go_through_office(
    tmp_path: Path, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode, self.stdout, self.stderr = code, "", "Word refused"

    def fake_run(command, env, **kwargs):
        calls.append((command, env.get("AUTOSHELF_IN"), env.get("AUTOSHELF_OUT")))
        if len(calls) == 1:
            Path(env["AUTOSHELF_OUT"]).write_bytes(b"%PDF fake")
            return Done(0)
        return Done(1)

    monkeypatch.setattr(convert, "office_converter", lambda kind: "msoffice")
    monkeypatch.setattr(convert.subprocess, "run", fake_run)
    source = make(tmp_path / "my 'odd' $name.docx", "x")
    (job,) = convert.plan_convert([source], "pdf")

    out = convert.run(job, work)

    assert out.read_bytes() == b"%PDF fake"
    command, given_in, given_out = calls[0]
    assert given_in == str(source) and given_out == str(out)  # paths never in the script
    assert str(source) not in " ".join(command)
    with pytest.raises(ConvertError, match="Word refused"):
        convert.run(job, tmp_path)


@pytest.mark.skipif(
    not os.environ.get("AUTOSHELF_TEST_OFFICE"), reason="set AUTOSHELF_TEST_OFFICE=1 to use Office"
)
def test_real_office_conversion(tmp_path: Path, work: Path) -> None:
    source = samples.docx(
        tmp_path / "レポート.docx", title="T", heading="H", paragraph="Hello", cells=["a"]
    )
    (job,) = convert.plan_convert([source], "pdf")
    assert convert.run(job, work).read_bytes().startswith(b"%PDF")


# --- in the app: next to the original, recorded, undoable ---------------------------------------


@pytest.fixture
def service(paths: AppPaths, root: Path):
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(root)
    yield app
    app.release()


def test_a_converted_copy_lands_next_to_the_original(service: AppService, root: Path) -> None:
    photo = png(root / "Travel" / "kyoto.png")
    make(root / "Travel" / "kyoto.jpg", "someone's own jpg")

    (created,) = service.convert([photo], "jpg")

    assert created.dst == root / "Travel" / "kyoto (2).jpg"  # never overwrites
    assert (root / "Travel" / "kyoto.jpg").read_text(encoding="utf-8") == "someone's own jpg"
    assert photo.exists()
    history = service.history()
    assert (history[0].action, history[0].reason) == ("create", "convert")
    assert history[0].details["reason"]["to"] == "jpg"
    assert not list((service.paths.cache_dir / "work").iterdir())  # working files cleaned up

    service.undo(created.history_id)
    assert (root / "_Trash" / "kyoto (2).jpg").exists()  # set aside, never deleted


def test_several_conversions_are_one_batch(service: AppService, root: Path) -> None:
    photos = [png(root / f"page {number}.png") for number in range(2)]
    created = service.convert(photos, "webp")
    batches = {service.history()[index].batch_id for index in range(2)}
    assert len(created) == 2 and len(batches) == 1 and None not in batches
    service.undo_batch(batches.pop())
    assert sorted(path.name for path in (root / "_Trash").iterdir()) == [
        "page 0.webp",
        "page 1.webp",
    ]


def test_files_outside_the_library_are_refused(service: AppService, tmp_path: Path) -> None:
    outside = make(tmp_path / "elsewhere" / "a.txt", "x")
    with pytest.raises(ConvertError, match="outside your library"):
        service.compress([outside])


def test_new_files_in_the_root_are_left_alone_unless_asked(
    service: AppService, root: Path, conn
) -> None:
    notes = make(root / "notes.txt", "notes")
    created = service.compress([notes])
    assert created.dst == root / "notes.zip"
    outcome = Pipeline(conn, root).process(created.dst)
    assert outcome.kind is OutcomeKind.SKIPPED  # the watcher leaves it where it is

    service.save_settings(replace(service.settings(), sort_created_files=True))
    again = service.compress([notes], zip_always=True)
    assert repository.get_file(conn, again.file_id).status == "new"
    assert Pipeline(conn, root).process(again.dst).kind is OutcomeKind.MOVED  # sorted


def test_the_running_watcher_ignores_the_new_file(service: AppService, root: Path) -> None:
    assert service.acquire()
    assert service.start()
    try:
        expect = service._before_create(sort=False)
        assert expect is not None
        assert service._before_create(sort=True) is None
    finally:
        service.stop()


def test_cli_compress_and_convert(
    service: AppService, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = make(root / "Finance" / "notes.txt", "notes")
    assert main(["compress", str(notes)]) == 0
    assert "Made " in capsys.readouterr().out and (root / "Finance" / "notes.zip").exists()
    pdf = samples.pdf(root / "Finance" / "slides.pdf", ["Some text"])
    assert main(["convert", str(pdf), "--to", "docx"]) == 0
    out = capsys.readouterr().out
    assert "slides.docx" in out and "only approximated" in out
    assert main(["convert", str(notes), "--to", "pdf"]) == 1
    assert "can't be converted" in capsys.readouterr().err
