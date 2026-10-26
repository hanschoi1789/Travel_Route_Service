import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def functions(path):
    return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n,ast.FunctionDef)}


def test_integrated_selection_identical():
    standalone=functions(ROOT/"selector.py")
    integrated=functions(ROOT/"app.py")
    assert all(integrated[name]==code for name,code in standalone.items())


def test_no_old_objective_or_source_names_in_product():
    source=(ROOT/"app.py").read_text(encoding="utf-8")
    assert "15 ×" not in source
    assert "한솔님" not in source and "승운님" not in source
    assert "pydeck_chart" not in source


def test_candidate_functions_preserved():
    original=Path.home()/"Desktop/project_part1/project_part1/project_part1/app.py"
    if not original.exists():
        return
    before=functions(original)
    after=functions(ROOT/"app.py")
    for name in ("category_to_features", "calculate_preference_score", "calculate_distance_score", "combine_preference_and_distance", "collect_candidate_places", "select_diverse_places", "remove_chain_and_similar_duplicates"):
        assert before[name]==after[name]
