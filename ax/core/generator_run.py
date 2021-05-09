#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import copy
from collections import OrderedDict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, MutableMapping, NamedTuple, Optional, Set, Tuple

import pandas as pd
from ax.core.arm import Arm
from ax.core.optimization_config import OptimizationConfig
from ax.core.search_space import SearchSpace
from ax.core.types import (
    TCandidateMetadata,
    TGenMetadata,
    TModelPredict,
    TModelPredictArm,
)
from ax.utils.common.base import SortableBase
from ax.utils.common.typeutils import not_none


class GeneratorRunType(Enum):
    """Class for enumerating generator run types."""

    STATUS_QUO = 0
    MANUAL = 1


class ArmWeight(NamedTuple):
    """NamedTuple for tying together arms and weights."""

    arm: Arm
    weight: float


def extract_arm_predictions(
    model_predictions: TModelPredict, arm_idx: int
) -> TModelPredictArm:
    """Extract a particular arm from model_predictions.

    Args:
        model_predictions: Mean and Cov for all arms.
        arm_idx: Index of arm in prediction list.

    Returns:
        (mean, cov) for specified arm.
    """

    means = model_predictions[0]
    covariances = model_predictions[1]
    means_per_arm = {metric: means[metric][arm_idx] for metric in means.keys()}
    covar_per_arm = {
        metric: {
            other_metric: covariances[metric][other_metric][arm_idx]
            for other_metric in covariances[metric].keys()
        }
        for metric in covariances.keys()
    }
    return (means_per_arm, covar_per_arm)


class GeneratorRun(SortableBase):
    """An object that represents a single run of a generator.

    This object is created each time the ``gen`` method of a generator is
    called. It stores the arms and (optionally) weights that were
    generated by the run. When we add a generator run to a trial, its
    arms and weights will be merged with those from previous generator
    runs that were already attached to the trial.
    """

    def __init__(
        self,
        arms: List[Arm],
        weights: Optional[List[float]] = None,
        optimization_config: Optional[OptimizationConfig] = None,
        search_space: Optional[SearchSpace] = None,
        model_predictions: Optional[TModelPredict] = None,
        best_arm_predictions: Optional[Tuple[Arm, Optional[TModelPredictArm]]] = None,
        type: Optional[str] = None,
        fit_time: Optional[float] = None,
        gen_time: Optional[float] = None,
        model_key: Optional[str] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        bridge_kwargs: Optional[Dict[str, Any]] = None,
        gen_metadata: Optional[TGenMetadata] = None,
        model_state_after_gen: Optional[Dict[str, Any]] = None,
        generation_step_index: Optional[int] = None,
        candidate_metadata_by_arm_signature: Optional[
            Dict[str, TCandidateMetadata]
        ] = None,
    ) -> None:
        """
        Inits GeneratorRun.

        Args:
            arms: The list of arms generated by this run.
            weights: An optional list of weights to associate with the arms.
            optimization_config: The optimization config used during generation
                of this run.
            search_space: The search used during generation of this run.
            model_predictions: Means and covariances for the arms in this
                run recorded at the time the run was executed.
            best_arm_predictions: Optional tuple of best arm in this run
                (according to the optimization config) and its optional respective
                model predictions.
            type: Optional type of the run.
            fit_time: Optional number of seconds it took to fit the model that produced
                this generator run. For models with multiple invocations of gen, this is
                typically the fitting time since the last call to gen.
            gen_time: Optional number of seconds generation took.
            model_key: Optional name of the model that was used to produce this
                generator run.
            model_kwargs: Optional dictionary of keyword arguments to the model
                that was used to produce this generator run.
            bridge_kwargs: Optional dictionary of keyword arguments to the model
                bridge that was used to produce this generator run.
            gen_metadata: Optional dictionary of metadata generated by alongside
                the generator_run.
            model_state_after_gen: Optional dictionary of model state attributes
                to those attributes' values, to use when reinstantiating the model
                from the generator run. Note that this is the state of the model
                after generation, so these settings should only be applied to the
                model when reinstantiating it to continue generation from it,
                rather than to reproduce the conditions, in which this generator
                run was created.
            generation_step_index: Optional index of the generation step that produced
                this generator run. Applicable only if the genetator run was created
                via a generation strategy.
            candidate_metadata_by_arm_signature: Optional dictionary of arm signatures
                to model-produced candidate metadata that corresponds to that arm in
                this generator run.
        """
        self._arm_weight_table: OrderedDict[str, ArmWeight] = OrderedDict()
        if weights is None:
            weights = [1.0 for i in range(len(arms))]
        if len(arms) != len(weights):
            raise ValueError("Weights and arms must have the same length.")
        if bridge_kwargs is not None or model_kwargs is not None:
            if model_key is None:
                raise ValueError(
                    "Model key is required if model or bridge kwargs are provided."
                )
            if bridge_kwargs is None or model_kwargs is None:
                raise ValueError(
                    "Both model kwargs and bridge kwargs are required if either "
                    "one is provided."
                )
        for arm, weight in zip(arms, weights):
            existing_cw = self._arm_weight_table.get(arm.signature)
            if existing_cw:
                self._arm_weight_table[arm.signature] = ArmWeight(
                    arm=arm, weight=existing_cw.weight + weight
                )
            else:
                self._arm_weight_table[arm.signature] = ArmWeight(
                    arm=arm, weight=weight
                )

        self._generator_run_type: Optional[str] = type
        self._time_created: datetime = datetime.now()
        self._optimization_config = optimization_config
        self._search_space = search_space
        self._model_predictions = model_predictions
        self._best_arm_predictions = best_arm_predictions
        self._index: Optional[int] = None
        self._fit_time = fit_time
        self._gen_time = gen_time
        self._model_key = model_key
        self._model_kwargs = model_kwargs
        self._bridge_kwargs = bridge_kwargs
        self._gen_metadata = gen_metadata
        self._model_state_after_gen = model_state_after_gen
        # If candidate metadata is not None and not empty, check that all arm
        # signatures in it correspond to arms in this generator run.
        if candidate_metadata_by_arm_signature:
            unknown_arms_in_cand_metadata = (
                set(candidate_metadata_by_arm_signature.keys()) - self.arm_signatures
            )
            if unknown_arms_in_cand_metadata:
                raise ValueError(
                    f"Arms with signatures {unknown_arms_in_cand_metadata} appear in "
                    "candidate metadata, but not among the arms on this GeneratorRun."
                )
        self._candidate_metadata_by_arm_signature = candidate_metadata_by_arm_signature

        # Validate that generation step index is non-negative.
        assert generation_step_index is None or generation_step_index >= 0
        self._generation_step_index = generation_step_index

    @property
    def arms(self) -> List[Arm]:
        """Returns arms generated by this run."""
        return [cw.arm for cw in self._arm_weight_table.values()]

    @property
    def arm_signatures(self) -> Set[str]:
        """Returns signatures of arms generated by this run."""
        return {cw.arm.signature for cw in self._arm_weight_table.values()}

    @property
    def weights(self) -> List[float]:
        """Returns weights associated with arms generated by this run."""
        return [cw.weight for cw in self._arm_weight_table.values()]

    @property
    def arm_weights(self) -> MutableMapping[Arm, float]:
        """Mapping from arms to weights (order matches order in
        `arms` property).
        """
        return OrderedDict(zip(self.arms, self.weights))

    @property
    def generator_run_type(self) -> Optional[str]:
        """The type of the generator run."""
        return self._generator_run_type

    @property
    def time_created(self) -> datetime:
        """Creation time of the batch."""
        return self._time_created

    @property
    def index(self) -> Optional[int]:
        """The index of this generator run within a trial's list of generator run structs.
        This field is set when the generator run is added to a trial.
        """
        return self._index

    @index.setter
    def index(self, index: int) -> None:
        if self._index is not None and self._index != index:
            raise ValueError("Cannot change the index of a generator run once set.")
        self._index = index

    @property
    def optimization_config(self) -> Optional[OptimizationConfig]:
        """The optimization config used during generation of this run."""
        return self._optimization_config

    @property
    def search_space(self) -> Optional[SearchSpace]:
        """The search used during generation of this run."""
        return self._search_space

    @property
    def model_predictions(self) -> Optional[TModelPredict]:
        return self._model_predictions

    @property
    def fit_time(self) -> Optional[float]:
        return self._fit_time

    @property
    def gen_time(self) -> Optional[float]:
        return self._gen_time

    @property
    def model_predictions_by_arm(self) -> Optional[Dict[str, TModelPredictArm]]:
        if self._model_predictions is None:
            return None

        predictions: Dict[str, TModelPredictArm] = {}
        for idx, cond in enumerate(self.arms):
            predictions[cond.signature] = extract_arm_predictions(
                model_predictions=not_none(self._model_predictions), arm_idx=idx
            )
        return predictions

    @property
    def best_arm_predictions(self) -> Optional[Tuple[Arm, Optional[TModelPredictArm]]]:
        return self._best_arm_predictions

    @property
    def gen_metadata(self) -> Optional[TGenMetadata]:
        """Returns metadata generated by this run."""
        return self._gen_metadata

    @property
    def candidate_metadata_by_arm_signature(
        self,
    ) -> Optional[Dict[str, TCandidateMetadata]]:
        """Retrieves model-produced candidate metadata as a mapping from arm name (for
        the arm the candidate became when added to experiment) to the metadata dict.
        """
        return self._candidate_metadata_by_arm_signature

    @property
    def param_df(self) -> pd.DataFrame:
        """
        Constructs a Pandas dataframe with the parameter values for each arm.

        Useful for inspecting the contents of a generator run.

        Returns:
            pd.DataFrame: a dataframe with the generator run's arms.
        """
        return pd.DataFrame.from_dict(
            {a.name_or_short_signature: a.parameters for a in self.arms}, orient="index"
        )

    def clone(self) -> GeneratorRun:
        """Return a deep copy of a GeneratorRun."""
        cand_metadata = self.candidate_metadata_by_arm_signature
        generator_run = GeneratorRun(
            arms=[a.clone() for a in self.arms],
            weights=self.weights[:] if self.weights is not None else None,
            # pyre-fixme[16]: `Optional` has no attribute `clone`.
            optimization_config=self.optimization_config.clone()
            if self.optimization_config is not None
            else None,
            # pyre-fixme[16]: `Optional` has no attribute `clone`.
            search_space=self.search_space.clone()
            if self.search_space is not None
            else None,
            model_predictions=copy.deepcopy(self.model_predictions),
            best_arm_predictions=copy.deepcopy(self.best_arm_predictions),
            type=self.generator_run_type,
            fit_time=self.fit_time,
            gen_time=self.gen_time,
            model_key=self._model_key,
            model_kwargs=self._model_kwargs,
            bridge_kwargs=self._bridge_kwargs,
            gen_metadata=self._gen_metadata,
            model_state_after_gen=self._model_state_after_gen,
            generation_step_index=self._generation_step_index,
            candidate_metadata_by_arm_signature=cand_metadata,
        )
        generator_run._time_created = self._time_created
        generator_run._index = self._index
        generator_run._model_key = self._model_key
        generator_run._model_kwargs = (
            # pyre-fixme[16]: `Optional` has no attribute `copy`.
            self._model_kwargs.copy()
            if self._model_kwargs is not None
            else None
        )
        generator_run._bridge_kwargs = (
            self._bridge_kwargs.copy() if self._bridge_kwargs is not None else None
        )
        generator_run._model_state_after_gen = (
            self._model_state_after_gen.copy()
            if self._model_state_after_gen is not None
            else None
        )
        return generator_run

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        num_arms = len(self.arms)
        total_weight = sum(self.weights)
        return f"{class_name}({num_arms} arms, total weight {total_weight})"

    @property
    def _unique_id(self) -> str:
        if self.index is not None:
            return str(self.index)
        elif self._generation_step_index is not None:
            return str(self._generation_step_index)
        else:
            raise ValueError(
                "GeneratorRuns only have a unique id if attached "
                "to a Trial or GenerationStrategy."
            )
