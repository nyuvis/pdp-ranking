#!/usr/bin/env python
# coding: utf-8

"""
Compute partial dependence plots
"""

from operator import itemgetter
from itertools import chain
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed

from scipy.stats import iqr as inner_quartile_range

from tslearn.utils import to_time_series_dataset
from tslearn.clustering import TimeSeriesKMeans, silhouette_score as ts_silhouette_score

from sklearn.linear_model import Ridge
from sklearn.preprocessing import SplineTransformer, StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.metrics import mean_squared_error, silhouette_score
from sklearn.cluster import KMeans

from plotnine import (
    ggplot,
    geom_line,
    geom_point,
    geom_tile,
    aes,
    labs,
    scale_fill_distiller,
    scale_y_continuous,
    theme_light,
    ggtitle,
    coord_cartesian,
)

from pdpexplorer.metadata import Metadata
from pdpexplorer.ticks import nice, ticks


def partial_dependence(
    *,
    predict,
    df,
    one_way_features=None,
    two_way_feature_pairs=None,
    n_instances=100,
    resolution=20,
    one_hot_features=None,
    categorical_features=None,
    ordinal_features=None,
    n_jobs=1,
    output_path=None,
):
    """calculates the partial dependences for the given features and feature pairs"""

    # first check that the output path exists if provided so that the function
    # can fail quickly, rather than waiting until all the work is done
    if output_path:
        path = Path(output_path).resolve()

        if not path.parent.is_dir():
            raise OSError(f"Cannot write to {path.parent}")

    # handle default list arguments
    if one_way_features is None:
        one_way_features = []

    if two_way_feature_pairs is None:
        two_way_feature_pairs = []

    # we need to compute one-way partial dependencies for all features that are included in
    # two-way plots
    one_way_features = list(
        set(chain.from_iterable(two_way_feature_pairs)).union(one_way_features)
    )

    md = Metadata(df, one_hot_features, categorical_features, ordinal_features)

    subset = df.sample(n=n_instances)
    subset_copy = df.copy()

    iqr = inner_quartile_range(predict(df))

    # one-way

    one_way_work = [
        {
            "predict": predict,
            "data": subset,
            "data_copy": subset_copy,
            "feature": feature,
            "resolution": resolution,
            "md": md,
            "iqr": iqr,
            "feature_to_pd": None,
        }
        for feature in one_way_features
    ]

    if n_jobs == 1:
        one_way_pds = [_calc_pd(**args) for args in one_way_work]
    else:
        one_way_pds = Parallel(n_jobs=n_jobs)(
            delayed(_calc_pd)(**args) for args in one_way_work
        )

    feature_to_pd = _get_feature_to_pd(one_way_pds) if one_way_pds else None

    one_way_quantitative_clusters, one_way_categorical_clusters = one_way_clustering(
        one_way_pds=one_way_pds, feature_to_pd=feature_to_pd, md=md, n_jobs=n_jobs
    )

    # two-way

    two_way_work = [
        {
            "predict": predict,
            "data": subset,
            "data_copy": subset_copy,
            "feature": pair,
            "resolution": resolution,
            "md": md,
            "iqr": iqr,
            "feature_to_pd": feature_to_pd,
        }
        for pair in two_way_feature_pairs
    ]

    if n_jobs == 1:
        two_way_pds = [_calc_pd(**args) for args in two_way_work]
    else:
        two_way_pds = Parallel(n_jobs=n_jobs)(
            delayed(_calc_pd)(**args) for args in two_way_work
        )

    # min and max predictions

    min_pred = min(one_way_pds, key=itemgetter("min_prediction"))["min_prediction"]
    max_pred = max(one_way_pds, key=itemgetter("max_prediction"))["max_prediction"]

    if two_way_pds:
        min_pred = min(
            min_pred,
            min(two_way_pds, key=itemgetter("min_prediction"))["min_prediction"],
        )
        max_pred = max(
            max_pred,
            max(two_way_pds, key=itemgetter("max_prediction"))["max_prediction"],
        )

    # marginal distributions

    marginal_distributions = get_marginal_distributions(
        df=subset, features=one_way_features, md=md
    )

    # output

    results = {
        "one_way_pds": one_way_pds,
        "two_way_pds": two_way_pds,
        "one_way_quantitative_clusters": one_way_quantitative_clusters,
        "one_way_categorical_clusters": one_way_categorical_clusters,
        "prediction_extent": [min_pred, max_pred],
        "marginal_distributions": marginal_distributions,
        "n_instances": n_instances,
        "resolution": resolution,
    }

    if output_path:
        path.write_text(json.dumps(results), encoding="utf-8")
    else:
        return results


def plot(
    *,
    data,
    one_way_sort_by=None,
    two_way_sort_by=None,
    same_prediction_scale=True,
    output_path=None,
):
    # if data is a path or string, then read the file at that path
    if isinstance(data, Path) or isinstance(data, str):
        path = Path(data).resolve()

        if not path.exists():
            raise OSError(f"Cannot read {path}")

        json_data = path.read_text()
        data = json.loads(json_data)

    # check that the output path existsm fail early if it doesn't
    if output_path:
        output_path = Path(output_path).resolve()

        if not output_path.exists():
            raise OSError(f"Cannot write to {output_path}")

        one_way_output_path = output_path.joinpath("one_way")
        one_way_output_path.mkdir()

        if data["two_way_pds"]:
            two_way_output_path = output_path.joinpath("two_way")
            two_way_output_path.mkdir()

    min_pred, max_pred = data["prediction_extent"]

    nice_prediction_limits = nice(min_pred, max_pred, 5)
    nice_prediction_breaks = ticks(
        nice_prediction_limits[0], nice_prediction_limits[1], 5
    )

    # one-way

    one_way_pdps = []

    if one_way_sort_by == "good_fit":
        sorted_one_way_pds = sorted(
            data["one_way_pds"],
            key=lambda x: (-x["knots_good_fit"], -x["nrmse_good_fit"]),
        )
    elif one_way_sort_by == "deviation":
        sorted_one_way_pds = sorted(data["one_way_pds"], key=lambda x: -x["deviation"])
    else:
        sorted_one_way_pds = data["one_way_pds"]

    num_digits = math.floor(math.log10(len(sorted_one_way_pds))) + 1

    for i, par_dep in enumerate(sorted_one_way_pds):
        if par_dep["kind"] == "quantitative":
            df = pd.DataFrame(
                {
                    "x_values": par_dep["x_values"],
                    "mean_predictions": par_dep["mean_predictions"],
                    "trend": par_dep["trend_good_fit"],
                }
            )

            pdp = (
                ggplot(data=df, mapping=aes(x="x_values"))
                + geom_line(aes(y="trend"), color="#7C7C7C")
                + geom_line(aes(y="mean_predictions"), color="#792367")
                + labs(x=par_dep["x_feature"], y="prediction")
                + theme_light()
            )

            if same_prediction_scale:
                pdp += coord_cartesian(ylim=nice_prediction_limits, expand=False)
        else:
            df = pd.DataFrame(
                {
                    "x_values": par_dep["x_values"],
                    "mean_predictions": par_dep["mean_predictions"],
                }
            )

            pdp = (
                ggplot(data=df, mapping=aes(x="factor(x_values)", y="mean_predictions"))
                + geom_point(color="#792367")
                + labs(x=par_dep["x_feature"], y="prediction")
                + theme_light()
            )

            if same_prediction_scale:
                pdp += scale_y_continuous(limits=nice_prediction_limits, expand=(0, 0))

        if output_path:
            pdp.save(
                filename=one_way_output_path.joinpath(
                    f'{i:0>{num_digits}}_{par_dep["id"]}.png'
                ),
                format="png",
                dpi=300,
                verbose=False,
            )
        else:
            one_way_pdps.append(pdp)

    # two-way

    two_way_pdps = []

    if data["two_way_pds"]:
        if two_way_sort_by == "h":
            sorted_two_way_pds = sorted(data["two_way_pds"], key=lambda x: -x["H"])
        elif two_way_sort_by == "deviation":
            sorted_two_way_pds = sorted(
                data["two_way_pds"], key=lambda x: -x["deviation"]
            )
        else:
            sorted_two_way_pds = data["two_way_pds"]

        num_digits = math.floor(math.log10(len(sorted_two_way_pds))) + 1

        for i, par_dep in enumerate(sorted_two_way_pds):
            limits_and_breaks = (
                {"limits": nice_prediction_limits, "breaks": nice_prediction_breaks}
                if same_prediction_scale
                else {}
            )

            if par_dep["kind"] == "quantitative":
                df = pd.DataFrame(
                    {
                        "x_values": par_dep["x_values"],
                        "y_values": par_dep["y_values"],
                        "mean_predictions": par_dep["mean_predictions"],
                    }
                )

                pdp = (
                    ggplot(data=df, mapping=aes(x="x_values", y="y_values"))
                    + geom_tile(mapping=aes(fill="mean_predictions"))
                    + scale_fill_distiller(
                        palette="YlGnBu", direction=1, **limits_and_breaks
                    )
                    + labs(
                        fill="prediction",
                        x=par_dep["x_feature"],
                        y=par_dep["y_feature"],
                    )
                    + ggtitle(f'H* = {par_dep["H"]:.3f}')
                    + theme_light()
                )
            elif par_dep["kind"] == "categorical":
                df = pd.DataFrame(
                    {
                        "x_values": par_dep["x_values"],
                        "y_values": par_dep["y_values"],
                        "mean_predictions": par_dep["mean_predictions"],
                    }
                )

                pdp = (
                    ggplot(
                        data=df, mapping=aes(x="factor(x_values)", y="factor(y_values)")
                    )
                    + geom_tile(mapping=aes(fill="mean_predictions"))
                    + scale_fill_distiller(
                        palette="YlGnBu", direction=1, **limits_and_breaks
                    )
                    + labs(
                        fill="prediction",
                        x=par_dep["x_feature"],
                        y=par_dep["y_feature"],
                    )
                    + ggtitle(f'H* = {par_dep["H"]:.3f}')
                    + theme_light()
                )
            else:
                df = pd.DataFrame(
                    {
                        "x_values": par_dep["x_values"],
                        "y_values": par_dep["y_values"],
                        "mean_predictions": par_dep["mean_predictions"],
                    }
                )

                pdp = (
                    ggplot(data=df, mapping=aes(x="x_values", y="factor(y_values)"))
                    + geom_tile(mapping=aes(fill="mean_predictions"))
                    + scale_fill_distiller(
                        palette="YlGnBu", direction=1, **limits_and_breaks
                    )
                    + labs(
                        fill="prediction",
                        x=par_dep["x_feature"],
                        y=par_dep["y_feature"],
                    )
                    + ggtitle(f'H* = {par_dep["H"]:.3f}')
                    + theme_light()
                )

            if output_path:
                pdp.save(
                    filename=two_way_output_path.joinpath(
                        f'{i:0>{num_digits}}_{par_dep["id"]}.png'
                    ),
                    format="png",
                    dpi=300,
                    verbose=False,
                )
            else:
                two_way_pdps.append(pdp)

    if not output_path:
        return one_way_pdps, two_way_pdps


def widget_partial_dependence(
    *, predict, subset, features, resolution, md, n_jobs, one_way_pds=None, iqr=None
):
    subset_copy = subset.copy()

    feature_to_pd = _get_feature_to_pd(one_way_pds) if one_way_pds else None

    work = [
        {
            "predict": predict,
            "data": subset,
            "data_copy": subset_copy,
            "feature": feature,
            "resolution": resolution,
            "md": md,
            "iqr": iqr,
            "feature_to_pd": feature_to_pd,
        }
        for feature in features
    ]

    if n_jobs == 1:
        results = [_calc_pd(**args) for args in work]
    else:
        results = Parallel(n_jobs=n_jobs)(delayed(_calc_pd)(**args) for args in work)

    return results


def _calc_pd(
    predict,
    data,
    data_copy,
    feature,
    resolution,
    md,
    iqr,
    feature_to_pd,
):
    if isinstance(feature, tuple) or isinstance(feature, list):
        return _calc_two_way_pd(
            predict,
            data,
            data_copy,
            feature,
            resolution,
            md,
            feature_to_pd,
        )
    else:
        return _calc_one_way_pd(
            predict,
            data,
            data_copy,
            feature,
            resolution,
            md,
            iqr,
        )


def _calc_one_way_pd(
    predict,
    data,
    data_copy,
    feature,
    resolution,
    md,
    iqr,
):
    feat_info = md.feature_info[feature]

    x_values = _get_feature_values(feature, feat_info, resolution)

    mean_predictions = []

    min_pred = math.inf
    max_pred = -math.inf

    for value in x_values:
        _set_feature(feature, value, data, feat_info)

        predictions = predict(data)
        mean_pred = np.mean(predictions).item()

        mean_predictions.append(mean_pred)

        if mean_pred < min_pred:
            min_pred = mean_pred

        if mean_pred > max_pred:
            max_pred = mean_pred

    _reset_feature(feature, data, data_copy, feat_info)

    x_is_quant = (
        feat_info["kind"] == "integer"
        or feat_info["kind"] == "continuous"
        or feat_info["kind"] == "ordinal"
    )

    mean_predictions_centered = (
        np.array(mean_predictions) - np.mean(mean_predictions)
    ).tolist()

    par_dep = {
        "num_features": 1,
        "kind": "quantitative" if x_is_quant else "categorical",
        "id": feature,
        "x_feature": feature,
        "x_values": x_values,
        "mean_predictions": mean_predictions,
        "mean_predictions_centered": mean_predictions_centered,
        "min_prediction": min_pred,
        "max_prediction": max_pred,
    }

    if x_is_quant:
        # This code is adapted from
        # https://scikit-learn.org/stable/auto_examples/linear_model/plot_polynomial_interpolation.html
        # Author: Mathieu Blondel
        #         Jake Vanderplas
        #         Christian Lorentzen
        #         Malte Londschien
        # License: BSD 3 clause

        # good-fit

        X = np.array(x_values).reshape((-1, 1))
        y = np.array(mean_predictions)

        for i in range(2, 10):
            trend_model = make_pipeline(
                StandardScaler(), SplineTransformer(i, 3), Ridge(alpha=0.1)
            )

            trend_model.fit(X, y)
            trend = trend_model.predict(X)

            rmse = mean_squared_error(y, trend, squared=False)
            # normalize it
            nrmse = rmse / iqr

            if nrmse < 0.02 or i == 9:
                par_dep["trend_good_fit"] = trend.tolist()
                par_dep["nrmse_good_fit"] = nrmse.item()
                par_dep["knots_good_fit"] = i
                break
    else:
        par_dep["nrmse_good_fit"] = -1
        par_dep["knots_good_fit"] = -1

    par_dep["deviation"] = np.std(mean_predictions)

    return par_dep


def _calc_two_way_pd(
    predict,
    data,
    data_copy,
    pair,
    resolution,
    md,
    feature_to_pd,
):
    x_feature, y_feature = pair

    x_feat_info = md.feature_info[x_feature]
    y_feat_info = md.feature_info[y_feature]

    # when one feature is quantitative and the other is categorical,
    # make the y feature be categorical

    x_is_quant = (
        x_feat_info["kind"] == "integer"
        or x_feat_info["kind"] == "continuous"
        or x_feat_info["kind"] == "ordinal"
    )
    y_is_quant = (
        y_feat_info["kind"] == "integer"
        or y_feat_info["kind"] == "continuous"
        or y_feat_info["kind"] == "ordinal"
    )

    if y_is_quant and not x_is_quant:
        x_feature, y_feature = y_feature, x_feature
        x_is_quant, y_is_quant = y_is_quant, x_is_quant
        x_feat_info, y_feat_info = y_feat_info, x_feat_info

    x_axis = _get_feature_values(x_feature, x_feat_info, resolution)
    y_axis = _get_feature_values(y_feature, y_feat_info, resolution)

    x_values = []
    y_values = []
    rows = []
    cols = []
    mean_predictions = []

    x_pdp = feature_to_pd[x_feature]
    y_pdp = feature_to_pd[y_feature]
    no_interactions = []

    min_pred = math.inf
    max_pred = -math.inf

    for c, x_value in enumerate(x_axis):
        _set_feature(x_feature, x_value, data, x_feat_info)

        for r, y_value in enumerate(y_axis):
            _set_feature(y_feature, y_value, data, y_feat_info)

            predictions = predict(data)
            mean_pred = np.mean(predictions).item()

            mean_predictions.append(mean_pred)

            x_values.append(x_value)
            y_values.append(y_value)
            rows.append(r)
            cols.append(c)

            no_interactions.append(
                x_pdp["mean_predictions_centered"][c]
                + y_pdp["mean_predictions_centered"][r]
            )

            if mean_pred < min_pred:
                min_pred = mean_pred

            if mean_pred > max_pred:
                max_pred = mean_pred

            _reset_feature(y_feature, data, data_copy, y_feat_info)

        _reset_feature(x_feature, data, data_copy, x_feat_info)

    mean_predictions_centered = np.array(mean_predictions) - np.mean(mean_predictions)
    interactions = (mean_predictions_centered - np.array(no_interactions)).tolist()
    mean_predictions_centered = mean_predictions_centered.tolist()

    h_statistic = np.sqrt(np.square(interactions).sum()).item()

    if x_is_quant and y_is_quant:
        kind = "quantitative"
    elif x_is_quant or y_is_quant:
        kind = "mixed"
    else:
        kind = "categorical"

    par_dep = {
        "num_features": 2,
        "kind": kind,
        "id": x_feature + "_" + y_feature,
        "x_feature": x_feature,
        "x_values": x_values,
        "x_axis": x_axis,
        "y_feature": y_feature,
        "y_values": y_values,
        "y_axis": y_axis,
        "mean_predictions": mean_predictions,
        "interactions": interactions,
        "min_prediction": min_pred,
        "max_prediction": max_pred,
        "H": h_statistic,
        "deviation": np.std(mean_predictions).item(),
    }

    return par_dep


def _set_feature(feature, value, data, feature_info):
    if feature_info["kind"] == "one_hot":
        col = feature_info["value_to_column"][value]
        all_features = [feat for feat, _ in feature_info["columns_and_values"]]
        data[all_features] = 0
        data[col] = 1
    else:
        data[feature] = value


def _reset_feature(
    feature,
    data,
    data_copy,
    feature_info,
):
    if feature_info["kind"] == "one_hot":
        all_features = [col for col, _ in feature_info["columns_and_values"]]
        data[all_features] = data_copy[all_features]
    else:
        data[feature] = data_copy[feature]


def _get_feature_values(feature, feature_info, resolution):
    n_unique = len(feature_info["unique_values"])

    if feature_info["kind"] == "continuous" and resolution < n_unique:
        min_val = feature_info["unique_values"][0]
        max_val = feature_info["unique_values"][-1]
        return (np.linspace(min_val, max_val, resolution)).tolist()
    elif feature_info["kind"] == "integer" and resolution < n_unique:
        return (
            [feature_info["unique_values"][0]]
            + feature_info["unique_values"][1 : -1 : n_unique // resolution]
            + [feature_info["unique_values"][-1]]
        )
    else:
        return feature_info["unique_values"]


def _get_feature_to_pd(one_way_pds):
    return {par_dep["x_feature"]: par_dep for par_dep in one_way_pds}


def get_marginal_distributions(*, df, features, md):
    marginal_distributions = {}

    for feature in features:
        feature_info = md.feature_info[feature]
        if feature_info["kind"] == "one_hot":
            bins = []
            counts = []

            for col, value in feature_info["columns_and_values"]:
                bins.append(value)
                counts.append(df[col].sum().item())

            marginal_distributions[feature] = {
                "kind": "categorical",
                "bins": bins,
                "counts": counts,
            }
        elif feature_info["kind"] == "integer" or feature_info["kind"] == "continuous":
            counts, bins = np.histogram(
                df[feature],
                "auto",
                (
                    feature_info["unique_values"][0],
                    feature_info["unique_values"][-1],
                ),
            )
            marginal_distributions[feature] = {
                "kind": "quantitative",
                "bins": bins.tolist(),
                "counts": counts.tolist(),
            }
        elif feature_info["kind"] == "ordinal":
            counts, bins = np.histogram(
                df[feature],
                len(feature_info["unique_values"]),
                (
                    feature_info["unique_values"][0],
                    feature_info["unique_values"][-1],
                ),
            )
            marginal_distributions[feature] = {
                "kind": "quantitative",
                "bins": bins.tolist(),
                "counts": counts.tolist(),
            }
        else:
            bins, counts = np.unique(df[feature], return_counts=True)
            marginal_distributions[feature] = {
                "kind": "categorical",
                "bins": bins.tolist(),
                "counts": counts.tolist(),
            }

    return marginal_distributions


def one_way_clustering(*, one_way_pds, feature_to_pd, md, n_jobs):
    quant_one_way = []
    cat_one_way = []

    for p in one_way_pds:
        if (
            md.feature_info[p["x_feature"]]["kind"] == "integer"
            or md.feature_info[p["x_feature"]]["kind"] == "continuous"
        ):
            quant_one_way.append(p)
        else:
            cat_one_way.append(p)

    one_way_quantitative_clusters = quantitative_feature_clustering(
        one_way_pds=quant_one_way, feature_to_pd=feature_to_pd, n_jobs=n_jobs
    )

    n_quant_clusters = len(one_way_quantitative_clusters)

    one_way_categorical_clusters = categorical_feature_clustering(
        one_way_pds=cat_one_way,
        feature_to_pd=feature_to_pd,
        first_id=n_quant_clusters,
    )

    return one_way_quantitative_clusters, one_way_categorical_clusters


def quantitative_feature_clustering(*, one_way_pds, feature_to_pd, n_jobs):
    timeseries_dataset = to_time_series_dataset(
        [d["mean_predictions"] for d in one_way_pds]
    )
    features = [d["x_feature"] for d in one_way_pds]

    min_clusters = 2
    max_clusters = (
        int(len(features) / 2) if int(len(features) / 2) > 2 else len(features)
    )
    n_clusters_options = range(min_clusters, max_clusters)

    best_n_clusters = -1
    best_clusters = []
    best_score = -math.inf
    best_model = None

    for n in n_clusters_options:
        cluster_model = TimeSeriesKMeans(
            n_clusters=n, metric="dtw", max_iter=50, n_init=5, n_jobs=n_jobs
        )
        cluster_model.fit(timeseries_dataset)
        labels = cluster_model.predict(timeseries_dataset)

        score = ts_silhouette_score(timeseries_dataset, labels, metric="dtw")

        if score > best_score:
            best_score = score
            best_clusters = labels
            best_n_clusters = n
            best_model = cluster_model

    distance_to_centers = best_model.transform(timeseries_dataset)
    distances = np.min(distance_to_centers, axis=1).tolist()

    clusters_dict = {
        i: {
            "id": i,
            "type": "quantitative",
            "mean_distance": 0,
            "mean_complexity": 0,
            "features": [],
        }
        for i in range(best_n_clusters)
    }

    for feature, cluster, distance in zip(features, best_clusters, distances):
        p = feature_to_pd[feature]
        c = clusters_dict[cluster]

        p["distance_to_cluster_center"] = distance
        p["cluster"] = cluster.item()

        c["mean_distance"] += distance
        c["features"].append(feature)

    clusters = list(clusters_dict.values())

    for c in clusters:
        c["mean_distance"] = c["mean_distance"] / len(c["features"])

    return clusters


def categorical_feature_clustering(*, one_way_pds, feature_to_pd, first_id):
    features = [d["x_feature"] for d in one_way_pds]

    min_clusters = 2
    max_clusters = (
        int(len(features) / 2) if int(len(features) / 2) > 2 else len(features)
    )
    n_clusters_options = range(min_clusters, max_clusters)

    deviations = np.array([d["deviation"] for d in one_way_pds]).reshape(-1, 1)

    best_n_clusters = -1
    best_clusters = []
    best_score = -math.inf
    best_model = None

    for n in n_clusters_options:
        cluster_model = KMeans(n_clusters=n, n_init=5, max_iter=300)
        cluster_model.fit(deviations)
        labels = cluster_model.labels_

        score = silhouette_score(deviations, labels, metric="euclidean")

        if score > best_score:
            best_score = score
            best_clusters = labels
            best_n_clusters = n
            best_model = cluster_model

    distance_to_centers = best_model.transform(deviations)
    distances = np.min(distance_to_centers, axis=1).tolist()

    best_clusters += first_id

    clusters_dict = {
        (i + first_id): {
            "id": i + first_id,
            "type": "categorical",
            "mean_distance": 0,
            "mean_complexity": 0,
            "features": [],
        }
        for i in range(best_n_clusters)
    }

    for feature, cluster, distance in zip(features, best_clusters, distances):
        p = feature_to_pd[feature]
        c = clusters_dict[cluster]

        p["distance_to_cluster_center"] = distance
        p["cluster"] = cluster.item()

        c["mean_distance"] += distance
        c["features"].append(feature)

    clusters = list(clusters_dict.values())

    for c in clusters:
        c["mean_distance"] = c["mean_distance"] / len(c["features"])

    return clusters
