from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

import analysis.all_monthly_clob_systematic_research_v2 as base


def rolling_logit(df: pd.DataFrame, min_history: int = 100, retrain_every: int = 25, lookback: int = 1200) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    df = df.sort_values("first_quote_ts").reset_index(drop=True)
    probs = np.full(len(df), np.nan)
    model = None
    med = None
    next_retrain = min_history

    for i in range(len(df)):
        if i < min_history:
            continue
        if model is None or i >= next_retrain:
            hist = df.iloc[max(0, i - lookback):i].copy()
            hist = hist[pd.notna(hist["outcome_up"])].copy()
            if len(hist) < max(30, min_history // 2):
                next_retrain = i + retrain_every
                continue

            y = hist["outcome_up"].astype(int)
            if y.nunique() < 2:
                next_retrain = i + retrain_every
                continue

            X = hist[base.LOGIT_FEATURES].copy()
            med = X.median(numeric_only=True)
            X = X.fillna(med)
            weights = 0.5 ** ((len(hist) - 1 - np.arange(len(hist))) / 200.0)
            try:
                model = LogisticRegression(max_iter=1000)
                model.fit(X, y, sample_weight=weights)
            except Exception:
                model = None
            next_retrain = i + retrain_every

        if model is not None and med is not None:
            x = df.iloc[[i]][base.LOGIT_FEATURES].copy().fillna(med)
            try:
                probs[i] = float(model.predict_proba(x)[:, 1][0])
            except Exception:
                pass

    return pd.Series(probs, index=df.index)


def main() -> None:
    base.rolling_logit = rolling_logit
    base.main()


if __name__ == "__main__":
    main()
