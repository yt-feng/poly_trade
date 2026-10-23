"""Synthetic native-tree serialization and physical-output contract checks.

Local tests bypass only the cloud version assertion. They exercise the installed
implementation; the exact pinned full runtime remains a separate cloud gate.
"""
from copy import deepcopy
import json
import math
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import StandardScaler

from analysis import offline_histogram_residual_audit as audit


PARAMETERS = {
    "categorical_features": None, "early_stopping": False, "interaction_cst": None,
    "l2_regularization": 0.0, "learning_rate": 0.1, "loss": "squared_error", "max_bins": 255,
    "max_depth": 3, "max_features": 1.0, "max_iter": 100, "max_leaf_nodes": 8,
    "min_samples_leaf": 100, "monotonic_cst": None, "n_iter_no_change": 10, "quantile": None,
    "random_state": 0, "scoring": "loss", "tol": 1e-7, "validation_fraction": None,
    "verbose": 0, "warm_start": False,
}
CONTRACT = {
    "format": "native_numeric_histogram_tree_json_v1", "expected_n_iter_per_fit": 100,
    "expected_trees_per_iteration": 1, "expected_total_trees_per_fit": 100,
    "max_trees_per_fit": 100, "max_depth_per_tree": 3, "max_leaves_per_tree": 8,
    "max_nodes_per_tree": 15,
}
CONFIG = {
    "base_protocol": {"features": audit.FEATURES.copy()},
    "treatment": {"model": {"estimator": "sklearn.ensemble.HistGradientBoostingRegressor",
                              "parameters": PARAMETERS}},
    "model_artifact_contract": CONTRACT,
}


def frames():
    rng = np.random.default_rng(684)
    x = rng.normal(size=(700, len(audit.FEATURES))) * np.linspace(.3, 3., len(audit.FEATURES))
    x += np.linspace(-3., 7., len(audit.FEATURES))
    x[:, -1] = 11.
    train = pd.DataFrame(x, columns=audit.FEATURES)
    train["side"] = np.where(np.arange(len(train)) % 3, "up", "down")
    signs = train.side.map({"up": 1., "down": -1.}).to_numpy()
    train["bid_residual_target"] = signs * (.12 + np.where(x[:, 0] < -3., -.18, .23))
    current = train.iloc[[1, 3, 15, 104, 333, 618]].drop(columns="bid_residual_target").copy()
    current["side"] = ["up", "down", "up", "down", "down", "up"]
    current["decision_bid_price"] = [.3, .6, .2, .7, .4, .5]
    current.index = [901, -7, 420, 19, 801, 3]
    return train, current


def independent_raw(saved, x):
    z = (np.asarray(x) - np.asarray(saved["scaler_mean"])) / np.asarray(saved["scaler_scale"])
    result = []
    for row in z:
        value = float(saved["histogram_model"]["baseline_prediction"])
        for tree in saved["histogram_model"]["trees"]:
            nodes = tree["nodes"]
            node = nodes[0]
            while not node["is_leaf"]:
                threshold = node["num_threshold"]
                if isinstance(threshold, dict):
                    threshold = math.inf if threshold["special_float"] == "positive_infinity" else -math.inf
                node = nodes[node["left"] if row[node["feature_idx"]] <= threshold else node["right"]]
            value += node["value"]
        result.append(value)
    return np.asarray(result)


class HistogramTreeArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train, cls.current = frames()
        cls.scaler = StandardScaler().fit(cls.train[audit.FEATURES].to_numpy())
        target = cls.train.side.map({"up": 1., "down": -1.}).to_numpy() * cls.train.bid_residual_target.to_numpy()
        cls.model = HistGradientBoostingRegressor(**PARAMETERS).fit(
            cls.scaler.transform(cls.train[audit.FEATURES].to_numpy()), target)
        cls.saved = audit.serialize_histogram_model(cls.model, cls.scaler, CONFIG)

    def test_native_json_roundtrip_has_complete_bounded_tree_state(self):
        saved = json.loads(json.dumps(self.saved, allow_nan=False))
        h = saved["histogram_model"]
        self.assertEqual(h["effective_parameters"], PARAMETERS)
        self.assertEqual((h["n_iter"], h["n_trees_per_iteration"], len(h["trees"])), (100, 1, 100))
        self.assertEqual(saved["scaler_n_samples_seen"], len(self.train))
        self.assertEqual([t["iteration"] for t in h["trees"]], list(range(100)))
        self.assertTrue(any(len(tree["nodes"]) > 1 for tree in h["trees"]))
        for tree in h["trees"]:
            with self.subTest(iteration=tree["iteration"]):
                self.assertEqual(tree["tree_index"], 0)
                self.assertEqual([x["name"] for x in tree["node_dtype"]], audit.NODE_FIELDS)
                self.assertLessEqual(len(tree["nodes"]), 15)
                self.assertLessEqual(sum(n["is_leaf"] for n in tree["nodes"]), 8)
                self.assertLessEqual(max(n["depth"] for n in tree["nodes"]), 3)
                audit.validate_serialized_tree(tree, len(audit.FEATURES), CONTRACT)

    def test_raw_tree_reconstruction_matches_native_predict_without_double_learning_rate(self):
        x = self.current[audit.FEATURES].to_numpy()
        expected = self.model.predict(self.scaler.transform(x))
        np.testing.assert_allclose(independent_raw(self.saved, x), expected, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(audit.reconstruct_serialized_histogram(self.saved, x), expected,
                                   rtol=1e-12, atol=1e-12)
        baseline = self.saved["histogram_model"]["baseline_prediction"]
        wrong_double_shrinkage = baseline + PARAMETERS["learning_rate"] * (expected-baseline)
        self.assertGreater(np.max(np.abs(expected-wrong_double_shrinkage)), .1)

    def test_threshold_equality_goes_left_and_preserves_original_float64_sum_order(self):
        saved = deepcopy(self.saved)
        saved["scaler_mean"] = [0.] * len(audit.FEATURES)
        saved["scaler_scale"] = [1.] * len(audit.FEATURES)
        prototype = deepcopy(saved["histogram_model"]["trees"][0]["nodes"][0])
        def leaf(value, depth=0):
            return {**prototype, "value": value, "count": 100, "feature_idx": 0,
                    "num_threshold": 0., "missing_go_to_left": 0, "left": 0, "right": 0,
                    "gain": 0., "depth": depth, "is_leaf": 1, "bin_threshold": 0,
                    "is_categorical": 0, "bitset_idx": 0}
        root = {**leaf(0.), "count": 200, "is_leaf": 0, "left": 1, "right": 2}
        trees = saved["histogram_model"]["trees"]
        trees[0]["nodes"] = [root, leaf(2., 1), leaf(3., 1)]
        for tree in trees[1:]:
            tree["nodes"] = [leaf(0.)]
        saved["histogram_model"]["baseline_prediction"] = 0.
        x = np.zeros((3, len(audit.FEATURES)))
        x[:, 0] = [np.nextafter(0., -np.inf), 0., np.nextafter(0., np.inf)]
        np.testing.assert_array_equal(audit.reconstruct_serialized_histogram(saved, x), [2., 2., 3.])
        saved["histogram_model"]["baseline_prediction"] = 1e16
        trees[0]["nodes"] = [leaf(-1e16)]
        trees[1]["nodes"] = [leaf(1.)]
        np.testing.assert_array_equal(audit.reconstruct_serialized_histogram(saved, x), [1., 1., 1.])
        # Summing all tree values before the baseline would incorrectly lose the unit.
        self.assertEqual(1e16 + sum([-1e16, 1.]), 0.)

    def test_only_threshold_and_gain_allow_tagged_infinity_and_nan_is_rejected(self):
        for field in ("num_threshold", "gain"):
            for value, tag in ((math.inf, "positive_infinity"), (-math.inf, "negative_infinity")):
                with self.subTest(field=field, tag=tag):
                    encoded = audit.encode_node_value(value, field)
                    self.assertEqual(encoded, {"special_float": tag})
                    self.assertEqual(audit.decode_node_value(encoded, field), value)
        for field in audit.NODE_FIELDS:
            with self.subTest(field=field, invalid="nan"):
                with self.assertRaises(ValueError): audit.encode_node_value(math.nan, field)
        for field in ("value", "count", "feature_idx"):
            for value in (math.inf, -math.inf):
                with self.subTest(field=field, invalid=value):
                    with self.assertRaises(ValueError): audit.encode_node_value(value, field)
                    with self.assertRaises(ValueError): audit.decode_node_value({"special_float": "positive_infinity"}, field)

    def test_missing_trees_nonfinite_leaf_scaler_or_bins_and_categorical_state_rejected(self):
        for mutation in ("missing_tree", "leaf_nan", "leaf_inf", "bin_nan", "categorical"):
            with self.subTest(mutation=mutation):
                model = deepcopy(self.model)
                if mutation == "missing_tree": model._predictors.pop()
                elif mutation.startswith("leaf_"):
                    nodes = model._predictors[0][0].nodes
                    index = np.flatnonzero(nodes["is_leaf"])[0]
                    nodes["value"][index] = math.nan if mutation == "leaf_nan" else math.inf
                elif mutation == "bin_nan": model._bin_mapper.bin_thresholds_[0][0] = math.nan
                else: model._predictors[0][0].nodes["is_categorical"][0] = 1
                with self.assertRaises(ValueError): audit.serialize_histogram_model(model, self.scaler, CONFIG)
        scaler = deepcopy(self.scaler)
        scaler.scale_[0] = 0.
        with self.assertRaises(ValueError): audit.serialize_histogram_model(self.model, scaler, CONFIG)

    def test_cyclic_out_of_range_and_unreachable_native_nodes_rejected(self):
        original = self.saved["histogram_model"]["trees"][0]
        for mutation in ("cycle", "out_of_range", "unreachable", "depth"):
            with self.subTest(mutation=mutation):
                tree = deepcopy(original)
                if mutation == "cycle": tree["nodes"][0]["left"] = 0
                elif mutation == "out_of_range": tree["nodes"][0]["right"] = 999
                elif mutation == "unreachable": tree["nodes"].append(deepcopy(tree["nodes"][-1]))
                else: tree["nodes"][0]["depth"] = 1
                with self.assertRaises(ValueError): audit.validate_serialized_tree(tree, len(audit.FEATURES), CONTRACT)

    def test_signed_target_train_only_scaler_unknown_targets_and_single_predict(self):
        train, current = frames()
        current.loc[:, audit.FEATURES] += 10000.
        current["future_bid_price"] = np.nan
        current["bid_residual_target"] = np.nan
        current["label_available_ts"] = pd.NaT
        with patch.object(audit, "assert_runtime_environment", return_value={"synthetic_version_gate_bypass": True}), \
             patch.object(HistGradientBoostingRegressor, "predict", autospec=True,
                          side_effect=HistGradientBoostingRegressor.predict) as predicted:
            scored, saved = audit.fit_predict_once(train, current, CONFIG)
        self.assertEqual(predicted.call_count, 1)
        self.assertEqual(scored.index.tolist(), current.index.tolist())
        self.assertTrue(scored.future_bid_price.isna().all())
        np.testing.assert_allclose(saved["scaler_mean"], self.scaler.mean_, rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(saved["scaler_scale"], self.scaler.scale_, rtol=1e-12, atol=1e-12)
        signed_target = train.side.map({"up": 1., "down": -1.}) * train.bid_residual_target
        self.assertAlmostEqual(saved["histogram_model"]["baseline_prediction"], signed_target.mean(), places=12)
        oriented = independent_raw(saved, current[audit.FEATURES].to_numpy())
        physical = current.side.map({"up": 1., "down": -1.}).to_numpy() * oriented
        for column, expected in (("predicted_oriented_residual_raw", oriented),
                                 ("predicted_bid_residual_raw", physical),
                                 ("predicted_exit_bid_unclipped", current.decision_bid_price+physical),
                                 ("predicted_exit_bid_price", np.clip(current.decision_bid_price+physical, 0, 1))):
            np.testing.assert_allclose(scored[column], expected, rtol=1e-12, atol=1e-12)

    def test_physical_side_inverse_and_bid_addition_precede_clip(self):
        train = pd.DataFrame(0., index=range(8), columns=audit.FEATURES)
        train["side"] = ["up", "down"] * 4
        train["bid_residual_target"] = train.side.map({"up": 1., "down": -1.}) * -.4
        current = train.iloc[:3].copy()
        current["side"] = ["down", "up", "down"]
        current["decision_bid_price"] = [.35, .35, .75]
        with patch.object(audit, "assert_runtime_environment", return_value={"synthetic_version_gate_bypass": True}):
            actual, saved = audit.fit_predict_once(train, current, CONFIG)
        np.testing.assert_allclose(actual.predicted_oriented_residual_raw, [-.4, -.4, -.4], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_bid_residual_raw, [.4, -.4, .4], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_exit_bid_unclipped, [.75, -.05, 1.15], atol=1e-12)
        np.testing.assert_allclose(actual.predicted_exit_bid_price, [.75, 0., 1.], atol=1e-12)
        self.assertTrue(saved["orientation"]["whole_model_inverted_with_side"])

    def test_invalid_side_fails_before_any_fit(self):
        train, current = frames()
        for location in ("train", "current"):
            for value in ("UP", "", None, 1, True):
                with self.subTest(location=location, value=value):
                    left, right = train.copy(), current.copy()
                    frame = left if location == "train" else right
                    frame["side"] = frame["side"].astype(object)
                    frame.iloc[0, frame.columns.get_loc("side")] = value
                    with patch.object(audit, "assert_runtime_environment", return_value={}), \
                         patch.object(HistGradientBoostingRegressor, "fit") as fitted:
                        with self.assertRaises(ValueError): audit.fit_predict_once(left, right, CONFIG)
                        fitted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
