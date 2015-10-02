#!/usr/bin/env python

# Copyright (c) 2015. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import (
    print_function,
    division,
    absolute_import,
    unicode_literals
)
import math
from collections import namedtuple, OrderedDict
from os.path import join
import argparse
from time import time

import numpy as np
import pandas as pd
import sklearn
import sklearn.metrics
import sklearn.cross_validation
from sklearn.cross_validation import KFold

from mhcflurry.common import normalize_allele_name
from mhcflurry.feedforward import make_embedding_network, make_hotshot_network
from mhcflurry.data_helpers import load_data, indices_to_hotshot_encoding
from mhcflurry.paths import (
    CLASS1_DATA_DIRECTORY
)

PETERS2009_CSV_FILENAME = "bdata.2009.mhci.public.1.txt"
PETERS2009_CSV_PATH = join(CLASS1_DATA_DIRECTORY, PETERS2009_CSV_FILENAME)

PETERS2013_CSV_FILENAME = "bdata.20130222.mhci.public.1.txt"
PETERS2013_CSV_PATH = join(CLASS1_DATA_DIRECTORY, PETERS2013_CSV_FILENAME)

COMBINED_CSV_FILENAME = "combined_human_class1_dataset.csv"
COMBINED_CSV_PATH = join(CLASS1_DATA_DIRECTORY, COMBINED_CSV_FILENAME)

parser = argparse.ArgumentParser()

parser.add_argument(
    "--binding-data-csv-path",
    default=PETERS2009_CSV_PATH,
    help="CSV file with 'mhc', 'peptide', 'peptide_length', 'meas' columns")

parser.add_argument(
    "--min-samples-per-allele",
    default=5,
    help="Don't train predictors for alleles with fewer samples than this",
    type=int)

parser.add_argument(
    "--results-filename",
    required=True,
    help="Write all hyperparameter/allele results to this filename")

parser.add_argument(
    "--cv-folds",
    default=5,
    type=int,
    help="Number cross-validation folds")

parser.add_argument(
    "--training-epochs",
    default=100,
    type=int,
    help="Number of passes over the dataset to perform during model fitting")


parser.add_argument(
    "--max-dropout",
    default=0.25,
    type=float,
    help="Degree of dropout regularization to try in hyperparameter search")

ModelConfig = namedtuple(
    "ModelConfig",
    [
        "embedding_size",
        "hidden_layer_size",
        "activation",
        "loss",
        "init",
        "n_pretrain_epochs",
        "n_epochs",
        "dropout_probability",
        "max_ic50",
    ])

HIDDEN1_LAYER_SIZES = [
    64,
    512,
]

INITILIZATION_METHODS = [
    "glorot_uniform",
    "glorot_normal",
]

ACTIVATIONS = [
    "relu",
    "tanh",
]

MAX_IC50_VALUES = [
    5000,
    20000,
]


def generate_all_model_configs(
        embedding_sizes=[0, 64],
        n_training_epochs=125,
        max_dropout=0.25):
    configurations = []
    for activation in ACTIVATIONS:
        for loss in ["mse"]:
            for init in INITILIZATION_METHODS:
                for n_pretrain_epochs in [0, 10]:
                    for hidden_layer_size in HIDDEN1_LAYER_SIZES:
                        for embedding_size in embedding_sizes:
                            for dropout in [0, max_dropout]:
                                for max_ic50 in MAX_IC50_VALUES:
                                    config = ModelConfig(
                                        embedding_size=embedding_size,
                                        hidden_layer_size=hidden_layer_size,
                                        activation=activation,
                                        init=init,
                                        loss=loss,
                                        dropout_probability=dropout,
                                        n_pretrain_epochs=n_pretrain_epochs,
                                        n_epochs=n_training_epochs,
                                        max_ic50=max_ic50)
                                    print(config)
                                    configurations.append(config)
    return configurations


def kfold_cross_validation_for_single_allele(
        allele_name,
        model,
        X,
        Y,
        ic50,
        n_training_epochs=100,
        cv_folds=5,
        max_ic50=5000):
    """
    Estimate the per-allele AUC score of a model via k-fold cross-validation.
    Returns the per-fold AUC scores and accuracies.
    """
    n_samples = len(Y)
    initial_weights = [w.copy() for w in model.get_weights()]
    fold_aucs = []
    fold_accuracies = []

    if not n_training_epochs:
        target_number_updates = 0.25 * 10 ** 6
        n_samples_per_fold = (cv_folds - 1) * n_samples / cv_folds
        ratio = target_number_updates / n_samples_per_fold
        n_training_epochs = int(math.ceil(ratio))
        print("-- Using nb_epoch=%s for %s with %s samples" % (
            n_training_epochs, allele_name, n_samples))

    for cv_iter, (train_idx, test_idx) in enumerate(KFold(
            n=n_samples,
            n_folds=cv_folds,
            shuffle=True,
            random_state=0)):
        X_train, Y_train = X[train_idx, :], Y[train_idx]
        X_test = X[test_idx, :]
        ic50_test = ic50[test_idx]
        label_test = ic50_test <= 500
        if label_test.all() or not label_test.any():
            print(
                "Skipping CV iter %d of %s since all outputs are the same" % (
                    cv_iter, allele_name))
            continue
        model.set_weights(initial_weights)

        model.fit(
            X_train,
            Y_train,
            nb_epoch=n_training_epochs,
            verbose=0)

        pred = model.predict(X_test)
        auc = sklearn.metrics.roc_auc_score(label_test, pred)
        ic50_pred = max_ic50 ** (1.0 - pred)
        accuracy = np.mean(label_test == (ic50_pred <= 500))
        print(
            "-- AUC for fold #%d of %s: %0.5f, Accuracy: %0.5f" % (
                cv_iter + 1,
                allele_name,
                auc,
                accuracy))
        fold_aucs.append(auc)
        fold_accuracies.append(accuracy)
    return fold_aucs, fold_accuracies


def leave_out_allele_cross_validation(
        model,
        binary_encoding=False,
        n_pretrain_epochs=0,
        min_samples_per_allele=5,
        cv_folds=5,
        max_ic50=5000):
    """
    Fit the model for every allele in the dataset and return a DataFrame
    with the following columns:
            allele_name
            dataset_size
            auc_mean
            auc_median
            auc_std
            auc_min
            auc_max
            accuracy_mean
            accuracy_median
            accuracy_std
            accuracy_min
            accuracy_max
    """
    result_dict = OrderedDict([
        ("allele_name", []),
        ("dataset_size", []),
        ("auc_mean", []),
        ("auc_median", []),
        ("auc_std", []),
        ("auc_min", []),
        ("auc_max", []),
        ("accuracy_mean", []),
        ("accuracy_median", []),
        ("accuracy_std", []),
        ("accuracy_min", []),
        ("accuracy_max", [])
    ])
    initial_weights = [w.copy() for w in model.get_weights()]
    for allele_name, dataset in sorted(
            allele_datasets.items(), key=lambda pair: pair[0]):
        # Want alleles to be 4-digit + gene name e.g. C0401
        if allele_name.isdigit() or len(allele_name) < 5:
            print("Skipping allele %s" % (allele_name,))
            continue
        allele_name = normalize_allele_name(allele_name)
        X_allele = dataset.X
        n_samples_allele = X_allele.shape[0]
        if n_samples_allele < min_samples_per_allele:
            print("Skipping allele %s due to too few samples: %d" % (
                allele_name, n_samples_allele))
            continue
        if binary_encoding:
            X_allele = indices_to_hotshot_encoding(X_allele, n_indices=20)

        ic50_allele = dataset.ic50
        Y_allele = 1.0 - np.minimum(1.0, np.log(ic50_allele) / np.log(max_ic50))
        model.set_weights(initial_weights)
        if n_pretrain_epochs > 0:
            X_other_alleles = np.vstack([
                other_dataset.X
                for (other_allele, other_dataset) in allele_datasets.items()
                if normalize_allele_name(other_allele) != allele_name])
            if binary_encoding:
                X_other_alleles = indices_to_hotshot_encoding(
                    X_other_alleles, n_indices=20)
            ic50_other_alleles = np.concatenate([
                other_allele.ic50 for (other_allele, other_dataset)
                in allele_datasets.items()
                if normalize_allele_name(other_allele) != allele_name])
            Y_other_alleles = 1.0 - np.minimum(
                1.0,
                np.log(ic50_other_alleles) / np.log(max_ic50))
            print("Pre-training X shape: %s" % (X_other_alleles.shape,))
            print("Pre-training Y shape: %s" % (Y_other_alleles.shape,))

            model.fit(
                X_other_alleles,
                Y_other_alleles,
                nb_epoch=n_pretrain_epochs)
        print("Cross-validation for %s (%d):" % (allele_name, len(Y_allele)))
        aucs, accuracies = kfold_cross_validation_for_single_allele(
            allele_name=allele_name,
            model=model,
            X=X_allele,
            Y=Y_allele,
            ic50=ic50_allele,
            n_training_epochs=config.n_epochs,
            cv_folds=cv_folds,
            max_ic50=max_ic50)
        if len(aucs) == 0:
            print("Skipping allele %s" % allele_name)
            continue
        result_dict["allele_name"].append(allele_name)
        result_dict["dataset_size"].append(len(ic50_allele))
        for (name, values) in [("auc", aucs), ("accuracy", accuracies)]:
            result_dict["%s_mean" % name].append(np.mean(values))
            result_dict["%s_median" % name].append(np.median(values))
            result_dict["%s_std" % name].append(np.std(values))
            result_dict["%s_min" % name].append(np.min(values))
            result_dict["%s_max" % name].append(np.max(values))
    return pd.DataFrame(result_dict)


def evaluate_model_config(
        config,
        allele_datasets,
        min_samples_per_allele=5,
        cv_folds=5,
        max_ic50=5000):
    print("===")
    print(config)
    if config.embedding_size:
        model = make_embedding_network(
            peptide_length=9,
            embedding_input_dim=20,
            embedding_output_dim=config.embedding_size,
            layer_sizes=[config.hidden_layer_size],
            activation=config.activation,
            init=config.init,
            loss=config.loss,
            dropout_probability=config.dropout_probability)
    else:
        model = make_hotshot_network(
            peptide_length=9,
            layer_sizes=[config.hidden_layer_size],
            activation=config.activation,
            init=config.init,
            loss=config.loss,
            dropout_probability=config.dropout_probability)
    return leave_out_allele_cross_validation(
        model,
        binary_encoding=config.embedding_size == 0,
        n_pretrain_epochs=config.n_pretrain_epochs,
        min_samples_per_allele=min_samples_per_allele,
        cv_folds=cv_folds,
        max_ic50=max_ic50)

if __name__ == "__main__":
    args = parser.parse_args()
    configs = generate_all_model_configs(
        max_dropout=args.max_dropout,
        n_training_epochs=args.training_epochs)
    print("Total # configurations = %d" % len(configs))

    datasets_by_max_ic50 = {}

    all_dataframes = []
    all_elapsed_times = []
    allele_datasets, _ = load_data(
        args.binding_data_csv_path,
        peptide_length=9,
        binary_encoding=False)
    for i, config in enumerate(configs):
        t_start = time()
        print("\n\n=== Config %d/%d: %s" % (i + 1, len(configs), config))
        result_df = evaluate_model_config(
            config,
            allele_datasets,
            min_samples_per_allele=args.min_samples_per_allele,
            cv_folds=args.cv_folds,
            max_ic50=config.max_ic50)
        n_rows = len(result_df)
        result_df["config_idx"] = [i] * n_rows
        for hyperparameter_name in config._fields:
            value = getattr(config, hyperparameter_name)
            result_df[hyperparameter_name] = [value] * n_rows
        # overwrite existing files for first config
        file_mode = "a" if i > 0 else "w"
        # append results to CSV
        with open(args.results_filename, file_mode) as f:
            result_df.to_csv(f, index=False)
        all_dataframes.append(result_df)
        t_end = time()
        t_elapsed = t_end - t_start
        all_elapsed_times.append(t_elapsed)
        mean_elapsed = sum(all_elapsed_times) / len(all_elapsed_times)
        estimate_remaining = (len(configs) - i - 1) * mean_elapsed
        print(
            "-- Time for config = %0.2fs, estimated remaining: %0.2f hours" % (
                t_elapsed,
                estimate_remaining / (60 * 60)))

    combined_df = pd.concat(all_dataframes)

    print("\n=== Hyperparameters ===")
    for hyperparameter_name in config._fields:
        print("\n%s" % hyperparameter_name)
        groups = combined_df.groupby(hyperparameter_name)
        for hyperparameter_value, group in groups:
            aucs = group["auc_mean"]
            accuracies = group["accuracy_mean"]
            unique_configs = group["config_idx"].unique()
            print(
                "-- %s (%d): AUC=%0.4f/%0.4f/%0.4f, Acc=%0.4f/%0.4f/%0.4f" % (
                    hyperparameter_value,
                    len(unique_configs),
                    np.percentile(aucs, 25.0),
                    np.percentile(aucs, 50.0),
                    np.percentile(aucs, 75.0),
                    np.percentile(accuracies, 25.0),
                    np.percentile(accuracies, 50.0),
                    np.percentile(accuracies, 75.0)))
