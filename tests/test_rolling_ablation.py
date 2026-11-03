from backtest.ablation import generate_sprint2_cases
from backtest.rolling_ablation import TREND_AFFECTING_KEYS
from futures_trend_quant.config import default_config_path, load_config


def test_every_frozen_ablation_is_explicitly_replayed_or_trend_invariant() -> None:
    cases = generate_sprint2_cases(load_config(default_config_path()).raw)
    replayed = [case.name for case in cases if case.key_path in TREND_AFFECTING_KEYS]
    invariant = [case.name for case in cases if case.key_path not in TREND_AFFECTING_KEYS]
    assert set(replayed) == {
        "rules.div_mode=A47",
        "rules.swing_mode=S_B",
        "rules.h1_veto_enabled=False",
        "rules.h1_strong_atr=0.4",
        "rules.h1_strong_atr=0.8",
    }
    assert len(replayed) + len(invariant) == len(cases)
    assert len(invariant) == 6
