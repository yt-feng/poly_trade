from analysis.h160_new_signal_family_exact_target_chain import CANDIDATES, FRACTIONS, add_fraction_variants, configs_pass, fraction_label, variant_name


def test_fraction_ladder_is_frozen_for_new_signal_family():
    assert CANDIDATES == ("classic_milddrop_down", "classic_extremeup_fade_down")
    assert FRACTIONS == (0.05, 0.10, 0.20, 0.40, 0.80)
    assert fraction_label(0.2) == "0p20"
    assert variant_name(CANDIDATES[0], 0.2).endswith("__fraction_0p20")


def test_fraction_variants_preserve_skip_rows():
    import pandas as pd

    frame = pd.DataFrame({
        "classic_milddrop_down_side": ["buy_down", "skip"],
        "classic_milddrop_down_minute": [2, -1],
        "classic_extremeup_fade_down_side": ["skip", "buy_down"],
        "classic_extremeup_fade_down_minute": [-1, 2],
    })
    out, variants = add_fraction_variants(frame)
    assert len(variants) == 10
    assert out["classic_milddrop_down__fraction_0p20_fraction"].tolist() == [0.2, 0.0]
    assert out["classic_extremeup_fade_down__fraction_0p20_fraction"].tolist() == [0.0, 0.2]


def test_target_selection_still_requires_all_cost_cells():
    rows = [{"first_ten_passed": True, "first_ten_fills": 10, "first_ten_target_growth_passed": True} for _ in range(4)]
    assert configs_pass(rows, target=True)
    rows[-1]["first_ten_target_growth_passed"] = False
    assert not configs_pass(rows, target=True)
