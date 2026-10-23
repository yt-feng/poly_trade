"""Synthetic algebra checks for the frozen side-oriented residual model.

These tests establish the signed linear-map contract, not an empirical advantage,
token-bid complementarity, or a nested extension of the direct residual model.
"""
import json
import unittest

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from analysis import offline_bid_residual_audit as parent
from analysis import offline_side_interaction_audit as audit


CONFIG = {"features": parent.FEATURES.copy(), "ridge_alpha": 1.0, "ridge_solver": "svd"}


def signs(frame):
    return frame["side"].map({"up": 1.0, "down": -1.0}).to_numpy()


def synthetic_frames():
    rng = np.random.default_rng(683)
    offsets = np.linspace(-2.0, 4.0, len(parent.FEATURES))
    scales = np.linspace(.3, 3.0, len(parent.FEATURES))
    matrix = rng.normal(size=(96, len(parent.FEATURES))) * scales + offsets
    matrix[:, -1] = 11.0
    train = pd.DataFrame(matrix, columns=parent.FEATURES)
    train["side"] = np.where(np.arange(len(train)) % 3 == 0, "down", "up")
    oriented = .23 + .035 * (matrix[:, 0] - offsets[0]) - .02 * (matrix[:, 3] - offsets[3])
    train["bid_residual_target"] = signs(train) * oriented
    current = train.iloc[[2, 7, 13, 31, 42, 79]].drop(columns="bid_residual_target").copy()
    current["side"] = ["up", "down", "up", "down", "down", "up"]
    current["decision_bid_price"] = [.31, .73, .42, .64, .38, .55]
    current.index = [101, -7, 42, 9, 810, 3]
    return train, current


def independent_reference(train, current):
    scaler = StandardScaler().fit(train[parent.FEATURES])
    target = signs(train) * train["bid_residual_target"].to_numpy()
    ridge = Ridge(alpha=1.0, solver="svd").fit(scaler.transform(train[parent.FEATURES]), target)
    oriented = ridge.predict(scaler.transform(current[parent.FEATURES]))
    return scaler, ridge, oriented


class SideConditionedRidgeTests(unittest.TestCase):
    def test_all_up_reduces_to_parent_ridge(self):
        train, current = synthetic_frames()
        train["side"] = current["side"] = "up"
        expected, parent_model = parent.fit_predict_once(train, current, CONFIG)
        actual, model = audit.fit_predict_once(train, current, CONFIG)
        for name in ("predicted_bid_residual_raw", "predicted_exit_bid_unclipped", "predicted_exit_bid_price"):
            np.testing.assert_allclose(actual[name], expected[name], rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(actual.predicted_oriented_residual_raw, expected.predicted_bid_residual_raw,
                                   rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(model["ridge_coef"], parent_model["ridge_coef"], rtol=1e-12, atol=1e-12)
        self.assertAlmostEqual(model["ridge_intercept"], parent_model["ridge_intercept"], places=12)

    def test_all_down_target_and_output_signs_cancel_to_parent_ridge(self):
        train, current = synthetic_frames()
        train["side"] = current["side"] = "down"
        expected, parent_model = parent.fit_predict_once(train, current, CONFIG)
        actual, model = audit.fit_predict_once(train, current, CONFIG)
        for name in ("predicted_bid_residual_raw", "predicted_exit_bid_unclipped", "predicted_exit_bid_price"):
            np.testing.assert_allclose(actual[name], expected[name], rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(actual.predicted_oriented_residual_raw, -expected.predicted_bid_residual_raw,
                                   rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(model["ridge_coef"], -np.asarray(parent_model["ridge_coef"]), rtol=1e-12, atol=1e-12)
        self.assertAlmostEqual(model["ridge_intercept"], -parent_model["ridge_intercept"], places=12)

    def test_mixed_sides_sign_the_intercept_and_slope_after_oriented_target_fit(self):
        train, current = synthetic_frames()
        scaler, ridge, expected_oriented = independent_reference(train, current)
        actual, model = audit.fit_predict_once(train, current, CONFIG)
        np.testing.assert_allclose(model["scaler_mean"], scaler.mean_, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(model["scaler_scale"], scaler.scale_, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(model["ridge_coef"], ridge.coef_, rtol=1e-12, atol=1e-12)
        self.assertAlmostEqual(model["ridge_intercept"], ridge.intercept_, places=12)
        np.testing.assert_allclose(actual.predicted_oriented_residual_raw, expected_oriented, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(actual.predicted_bid_residual_raw, signs(current) * expected_oriented,
                                   rtol=1e-12, atol=1e-12)
        self.assertGreater(abs(ridge.intercept_), .1)
        wrong_slope_only_sign = ridge.intercept_ + signs(current) * (expected_oriented - ridge.intercept_)
        down = current.side.eq("down").to_numpy()
        self.assertGreater(np.min(np.abs(actual.predicted_bid_residual_raw.to_numpy()[down]
                                        - wrong_slope_only_sign[down])), .2)

    def test_inverse_map_and_add_physical_bid_before_clipping(self):
        train = pd.DataFrame(0.0, index=range(8), columns=parent.FEATURES)
        train["side"] = ["up", "down"] * 4
        train["bid_residual_target"] = signs(train) * -.4
        current = pd.DataFrame(0.0, index=[18, 2, 71], columns=parent.FEATURES)
        current["side"] = ["down", "up", "down"]
        current["decision_bid_price"] = [.35, .35, .75]
        actual, _ = audit.fit_predict_once(train, current, CONFIG)
        np.testing.assert_allclose(actual.predicted_oriented_residual_raw, [-.4, -.4, -.4], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_bid_residual_raw, [.4, -.4, .4], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_exit_bid_unclipped, [.75, -.05, 1.15], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_exit_bid_price, [.75, 0., 1.], atol=1e-12)
        wrong_preclipped_oriented = np.clip(actual.predicted_oriented_residual_raw, 0, 1)
        wrong_bid = np.clip(current.decision_bid_price + signs(current) * wrong_preclipped_oriented, 0, 1)
        self.assertFalse(np.allclose(actual.predicted_exit_bid_price, wrong_bid))

    def test_scaler_uses_original_training_x_without_side_or_test_refit(self):
        train, current = synthetic_frames()
        extreme = current.copy()
        extreme.loc[:, parent.FEATURES] += 100000.0
        _, expected_model = audit.fit_predict_once(train, current, CONFIG)
        actual, model = audit.fit_predict_once(train, extreme, CONFIG)
        self.assertEqual(len(model["scaler_mean"]), len(parent.FEATURES))
        self.assertEqual(len(model["ridge_coef"]), len(parent.FEATURES))
        np.testing.assert_allclose(model["scaler_mean"], train[parent.FEATURES].mean().to_numpy(), atol=1e-12)
        expected_scale = train[parent.FEATURES].std(ddof=0).to_numpy()
        expected_scale = np.where(expected_scale == 0, 1.0, expected_scale)
        np.testing.assert_allclose(model["scaler_scale"], expected_scale, atol=1e-12)
        for name in ("scaler_mean", "scaler_scale", "ridge_coef", "ridge_intercept"):
            np.testing.assert_allclose(model[name], expected_model[name], rtol=1e-12, atol=1e-12)
        signed_x_mean = (train[parent.FEATURES].to_numpy() * signs(train)[:, None]).mean(axis=0)
        self.assertFalse(np.allclose(model["scaler_mean"], signed_x_mean))
        self.assertEqual(len(actual), len(extreme))

    def test_unknown_future_test_labels_are_scored_without_reordering_or_mutation(self):
        train, current = synthetic_frames()
        original_train = train.copy(deep=True)
        original_current = current.copy(deep=True)
        expected, expected_model = audit.fit_predict_once(train, current, CONFIG)
        with_unknowns = current.assign(future_bid_price=np.nan, bid_residual_target=np.nan,
                                      label_available_ts=pd.NaT)
        actual, model = audit.fit_predict_once(train, with_unknowns, CONFIG)
        self.assertEqual(len(actual), len(current))
        self.assertEqual(actual.index.tolist(), current.index.tolist())
        self.assertTrue(actual.future_bid_price.isna().all())
        self.assertTrue(np.isfinite(actual.predicted_exit_bid_price).all())
        np.testing.assert_allclose(actual.predicted_exit_bid_price, expected.predicted_exit_bid_price)
        for name in ("scaler_mean", "scaler_scale", "ridge_coef", "ridge_intercept"):
            np.testing.assert_allclose(model[name], expected_model[name], rtol=1e-12, atol=1e-12)
        pd.testing.assert_frame_equal(train, original_train)
        pd.testing.assert_frame_equal(current, original_current)

    def test_invalid_or_missing_side_is_rejected_in_train_and_current(self):
        train, current = synthetic_frames()
        for location in ("train", "current"):
            for bad in ("UP", "", "other", None, np.nan, 1, True):
                with self.subTest(location=location, invalid_side=bad):
                    left, right = train.copy(), current.copy()
                    target = left if location == "train" else right
                    target["side"] = target["side"].astype(object)
                    target.loc[target.index[0], "side"] = bad
                    with self.assertRaises(ValueError):
                        audit.fit_predict_once(left, right, CONFIG)
            with self.subTest(location=location, missing_column=True):
                left, right = train.copy(), current.copy()
                if location == "train":
                    left = left.drop(columns="side")
                else:
                    right = right.drop(columns="side")
                with self.assertRaises((ValueError, KeyError)):
                    audit.fit_predict_once(left, right, CONFIG)

    def test_serialized_vectors_reconstruct_all_outputs_in_physical_units(self):
        train, current = synthetic_frames()
        actual, model = audit.fit_predict_once(train, current, CONFIG)
        saved = json.loads(json.dumps(model, allow_nan=False))
        z = ((current[parent.FEATURES].to_numpy() - np.asarray(saved["scaler_mean"]))
             / np.asarray(saved["scaler_scale"]))
        oriented = saved["ridge_intercept"] + z @ np.asarray(saved["ridge_coef"])
        residual = signs(current) * oriented
        physical_bid = current.decision_bid_price.to_numpy() + residual
        for name, expected in (("predicted_oriented_residual_raw", oriented),
                               ("predicted_bid_residual_raw", residual),
                               ("predicted_exit_bid_unclipped", physical_bid),
                               ("predicted_exit_bid_price", np.clip(physical_bid, 0, 1))):
            np.testing.assert_allclose(actual[name], expected, rtol=1e-12, atol=1e-12)
        self.assertEqual(saved["orientation"]["side_sign"], {"up": 1, "down": -1})
        self.assertTrue(saved["orientation"]["intercept_inverted_with_side"])


if __name__ == "__main__":
    unittest.main()
