"""Embedding helpers. The real-model test only runs when a model cache is provided."""

import os
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeEmbedder

from autoshelf.config import DEFAULT_EMBEDDING_MODEL
from autoshelf.extract import Extraction
from autoshelf.ml.embedder import split_chars
from autoshelf.ml.vectors import HEADER_WEIGHT, document_vector, from_blob, header_text, to_blob

MODEL_CACHE_ENV = "AUTOSHELF_TEST_MODEL_CACHE"


def test_split_chars_prefers_line_breaks() -> None:
    text = "first line\nsecond line\nthird line"
    assert split_chars(text, 25) == ["first line\nsecond line", "third line"]


def test_split_chars_cuts_long_words() -> None:
    assert split_chars("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_chars("", 10) == []


def test_header_text_falls_back_to_the_name() -> None:
    extraction = Extraction("text", ("Unsupervised", "Learning"), title="Lecture 3")
    assert header_text(extraction, "x.pdf") == "Unsupervised Learning\nLecture 3"
    assert header_text(Extraction("text", ()), "___.pdf") == "___.pdf"


def test_document_vector_mixes_header_and_body() -> None:
    embedder = FakeEmbedder()
    extraction = Extraction("text", ("invoice",), text="recipe")
    vector = document_vector(embedder, extraction, "invoice.txt")
    header, body = embedder.embed(["invoice", "recipe"])
    assert float(vector @ header) == pytest.approx(HEADER_WEIGHT / np.hypot(HEADER_WEIGHT, 0.6))
    assert float(vector @ body) > float(vector @ header)
    assert np.linalg.norm(vector) == pytest.approx(1.0)


def test_name_only_file_is_its_header() -> None:
    embedder = FakeEmbedder()
    vector = document_vector(embedder, Extraction("filename only", ("scan",)), "scan.pdf")
    assert np.allclose(vector, embedder.embed(["scan"])[0])


def test_vectors_round_trip_through_bytes() -> None:
    vector = np.array([0.25, -1.5, 3.0], dtype=np.float32)
    assert np.array_equal(from_blob(to_blob(vector)), vector)


@pytest.mark.real_model
@pytest.mark.skipif(
    not os.environ.get(MODEL_CACHE_ENV),
    reason=f"set {MODEL_CACHE_ENV} to a model cache folder to test the real model",
)
def test_real_model_understands_english_and_japanese() -> None:
    from autoshelf.ml.embedder import load_embedder

    embedder = load_embedder(DEFAULT_EMBEDDING_MODEL, Path(os.environ[MODEL_CACHE_ENV]))
    finance, recipe, curry = embedder.embed(
        ["invoice, receipts and taxes", "cooking recipes", "カレーの作り方: 玉ねぎを炒めて煮込む"]
    )
    assert float(curry @ recipe) > float(curry @ finance) + 0.2
    assert len(embedder.chunk("word " * 2000)) > 1
