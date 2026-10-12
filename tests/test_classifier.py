"""Scoring and the auto-move / ask decision (SPEC section 4.2)."""

from pathlib import Path

import numpy as np
import pytest

from autoshelf.ml.classifier import (
    FULL_POOL,
    NEIGHBOR_WEIGHT,
    FolderProfile,
    FolderScore,
    choose,
    depth,
    is_related,
    score_folders,
)

THRESHOLD = 0.4
MARGIN = 0.1


def scores(**by_folder: float) -> list[FolderScore]:
    """FolderScores from keyword arguments; "__" in a name stands for "/"."""
    return [FolderScore(name.replace("__", "/"), score, score) for name, score in by_folder.items()]


def decide(**by_folder: float) -> str | None:
    choice = choose(scores(**by_folder), THRESHOLD, MARGIN).choice
    return None if choice is None else choice.folder


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


# --- the decision rule ------------------------------------------------------


def test_clear_winner_is_chosen() -> None:
    assert decide(Finance=0.6, Recipes=0.2) == "Finance"


def test_below_threshold_asks() -> None:
    assert decide(Finance=0.39, Recipes=0.1) is None


def test_threshold_is_inclusive() -> None:
    assert decide(Finance=0.4, Recipes=0.3) == "Finance"


def test_two_close_folders_ask() -> None:
    classification = choose(scores(Finance=0.60, Taxes=0.55), THRESHOLD, MARGIN)
    assert classification.choice is None
    assert "too close" in classification.explanation


def test_low_score_explanation_names_the_threshold() -> None:
    classification = choose(scores(Finance=0.2), THRESHOLD, MARGIN)
    assert "below the 0.40" in classification.explanation


def test_single_folder_needs_only_the_threshold() -> None:
    assert decide(Finance=0.45) == "Finance"


def test_no_folders_asks() -> None:
    classification = choose([], THRESHOLD, MARGIN)
    assert classification.choice is None
    assert "no folders" in classification.explanation


def test_confident_subfolder_wins_even_though_its_parent_scores_close() -> None:
    # The parent's pool contains the subfolder's files, so it always scores alike.
    # That must not count as "two folders fit equally".
    choice = decide(
        ML=0.58,
        ML__Unsupervised=0.60,
        ML__Deep_Learning=0.35,
        Finance=0.20,
    )
    assert choice == "ML/Unsupervised"


def test_deepest_qualifying_folder_wins_over_a_higher_scoring_parent() -> None:
    assert decide(ML=0.70, ML__Unsupervised=0.65, ML__Deep=0.30, Finance=0.1) == "ML/Unsupervised"


def test_falls_back_to_the_parent_when_the_subfolder_is_below_threshold() -> None:
    assert decide(ML=0.55, ML__Unsupervised=0.38, ML__Deep=0.20, Finance=0.1) == "ML"


def test_falls_back_to_the_parent_when_subfolders_are_too_close() -> None:
    assert decide(ML=0.60, ML__Unsupervised=0.58, ML__Deep=0.56, Finance=0.2) == "ML"


def test_parent_choice_explains_why_not_the_higher_scoring_subfolder() -> None:
    close = choose(scores(ML=0.52, ML__Unsup=0.59, ML__Deep=0.52, Fin=0.2), THRESHOLD, MARGIN)
    assert close.choice.folder == "ML"
    assert "'ML/Unsup' (0.59) and 'ML/Deep' (0.52) are too close" in close.explanation

    low = choose(scores(ML=0.55, ML__Unsup=0.38, Fin=0.1), THRESHOLD, MARGIN)
    assert low.choice.folder == "ML"
    assert low.explanation == "best match 'ML' 0.55, next 'Fin' 0.10"

    weak = choose(scores(ML__Unsup=0.39, ML=0.45, Fin=0.1), THRESHOLD, MARGIN)
    assert "its subfolder" not in weak.explanation


def test_unrelated_rival_of_the_parent_blocks_the_whole_branch() -> None:
    assert decide(ML=0.60, ML__Unsupervised=0.62, Statistics=0.58) is None


def test_sibling_of_a_different_parent_is_a_rival() -> None:
    assert decide(A__X=0.60, B__X=0.57) is None


def test_scores_are_returned_best_first() -> None:
    classification = choose(scores(A=0.1, B=0.5, C=0.3), THRESHOLD, MARGIN)
    assert [item.folder for item in classification.scores] == ["B", "C", "A"]
    assert [item.folder for item in classification.top(2)] == ["B", "C"]


def test_details_for_history_list_the_top_three() -> None:
    details = choose(scores(A=0.9, B=0.5, C=0.3, D=0.1), THRESHOLD, MARGIN).to_details()
    assert [c["folder"] for c in details["candidates"]] == ["A", "B", "C"]
    assert details["candidates"][0]["score"] == 0.9
    assert details["explanation"]


@pytest.mark.parametrize(
    ("a", "b", "related"),
    [
        ("ML", "ML", True),
        ("ML", "ML/Deep", True),
        ("ml/deep", "ML", True),
        ("ML/Deep", "ML/Unsupervised", False),
        ("ML", "MLOps", False),
        ("Finance", "ML", False),
    ],
)
def test_is_related(a: str, b: str, related: bool) -> None:
    assert is_related(a, b) is related


def test_depth() -> None:
    assert depth("A") == 1
    assert depth("A/B/C") == 3


# --- scoring ------------------------------------------------------------------


def test_empty_folder_is_scored_by_its_profile_alone() -> None:
    profile = FolderProfile("Recipes", unit(1, 0), np.empty((0, 2), np.float32))
    (score,) = score_folders(unit(1, 1), [profile])
    assert score.score == pytest.approx(score.profile_similarity)
    assert score.neighbor_similarity is None
    assert score.neighbors == ()


def test_neighbours_weigh_more_as_a_folder_fills_up() -> None:
    vector = unit(1, 0)
    profile_vector = unit(0, 1)  # the name says nothing about this file
    pool = np.stack([unit(1, 0)] * FULL_POOL)  # but its files match exactly
    paths = tuple(Path(f"f{i}") for i in range(FULL_POOL))

    one = score_folders(vector, [FolderProfile("A", profile_vector, pool[:1], paths[:1])])[0]
    full = score_folders(vector, [FolderProfile("A", profile_vector, pool, paths)])[0]

    assert full.score == pytest.approx(NEIGHBOR_WEIGHT)
    assert 0 < one.score < full.score


def test_neighbours_are_the_most_similar_files() -> None:
    pool = np.stack([unit(1, 0), unit(0, 1), unit(1, 1)])
    paths = (Path("same"), Path("other"), Path("half"))
    (score,) = score_folders(unit(1, 0), [FolderProfile("A", unit(1, 0), pool, paths)])
    assert [n.path for n in score.neighbors] == [Path("same"), Path("half"), Path("other")]
    assert score.neighbors[0].similarity == pytest.approx(1.0)


def test_exclude_keeps_a_file_from_matching_itself() -> None:
    pool = np.stack([unit(1, 0)])
    profile = FolderProfile("A", unit(0, 1), pool, (Path("me.pdf"),))
    (score,) = score_folders(unit(1, 0), [profile], exclude=Path("me.pdf"))
    assert score.neighbor_similarity is None


def test_boost_is_added_per_folder() -> None:
    profiles = [
        FolderProfile("A", unit(1, 0), np.empty((0, 2), np.float32)),
        FolderProfile("B", unit(1, 0), np.empty((0, 2), np.float32)),
    ]
    ranked = score_folders(unit(1, 0), profiles, boosts={"B": 0.25})
    assert ranked[0].folder == "B"
    assert ranked[0].score == pytest.approx(1.25)
