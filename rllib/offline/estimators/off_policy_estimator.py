import numpy as np
import tree
from typing import Dict, Any, List

import logging
from ray.rllib.policy.sample_batch import (
    MultiAgentBatch,
    DEFAULT_POLICY_ID,
    SampleBatch,
)
from ray.rllib.policy import Policy
from ray.rllib.utils.policy import compute_log_likelihoods_from_input_dict
from ray.rllib.utils.annotations import (
    DeveloperAPI,
    OverrideToImplementCustomLogic,
)
from ray.rllib.utils.deprecation import Deprecated
from ray.rllib.utils.numpy import convert_to_numpy
from ray.rllib.utils.typing import TensorType, SampleBatchType
from ray.rllib.offline.offline_evaluator import OfflineEvaluator

logger = logging.getLogger(__name__)


@DeveloperAPI
class OffPolicyEstimator(OfflineEvaluator):
    """Interface for an off policy estimator for counterfactual evaluation."""

    @DeveloperAPI
    def __init__(self, policy: Policy, gamma: float = 0.0):
        """Initializes an OffPolicyEstimator instance.

        Args:
            policy: Policy to evaluate.
            gamma: Discount factor of the environment.
        """
        self.policy = policy
        self.gamma = gamma

    @DeveloperAPI
    def estimate_on_single_episode(self, episode: SampleBatch) -> Dict[str, Any]:
        """Returns off-policy estimates for the given one episode.

        Args:
            batch: The episode to calculate the off-policy estimates (OPE) on. The
            episode must be a sample batch type that contains the fields "obs",
            "actions", and "action_prob" and it needs to represent a
            complete trajectory.

        Returns:
            The off-policy estimates (OPE) calculated on the given episode. The returned
            dict can be any arbitrary mapping of strings to metrics.
        """
        raise NotImplementedError

    @DeveloperAPI
    def estimate_on_single_step_samples(
        self,
        batch: SampleBatch,
    ) -> Dict[str, List[float]]:
        """Returns off-policy estimates for the batch of single timesteps. This is
        highly optimized for bandits assuming each episode is a single timestep.

        Args:
            batch: The batch to calculate the off-policy estimates (OPE) on. The
            batch must be a sample batch type that contains the fields "obs",
            "actions", and "action_prob".

        Returns:
            The off-policy estimates (OPE) calculated on the given batch of single time
            step samples. The returned dict can be any arbitrary mapping of strings to
            a list of floats capturing the values per each record.
        """
        raise NotImplementedError

    def on_before_split_batch_by_episode(
        self, sample_batch: SampleBatch
    ) -> SampleBatch:
        """Called before the batch is split by episode. You can perform any
        preprocessing on the batch that you want here.
        e.g. adding done flags to the batch, or reseting some stats that you want to
        track per episode later during estimation, .etc.

        Args:
            sample_batch: The batch to split by episode. This contains multiple
            episodes.

        Returns:
            The modified batch before calling split_by_episode().
        """
        return sample_batch

    @OverrideToImplementCustomLogic
    def on_after_split_batch_by_episode(
        self, all_episodes: List[SampleBatch]
    ) -> List[SampleBatch]:
        """Called after the batch is split by episode. You can perform any
        postprocessing on each episode that you want here.
        e.g. computing advantage per episode, .etc.

        Args:
            all_episodes: The list of episodes in the original batch. Each element is a
            sample batch type that is a single episode.
        """

        return all_episodes

    @OverrideToImplementCustomLogic
    def peek_on_single_episode(self, episode: SampleBatch) -> None:
        """This is called on each episode before it is passed to
        estimate_on_single_episode(). Using this method, you can get a peek at the
        entire validation dataset before runnining the estimation. For examlpe if you
        need to perform any normalizations of any sorts on the dataset, you can compute
        the normalization parameters here.

        Args:
            episode: The episode that is split from the original batch. This is a
            sample batch type that is a single episode.
        """
        pass

    @DeveloperAPI
    def estimate(
        self, batch: SampleBatchType, split_batch_by_episode: bool = True
    ) -> Dict[str, Any]:
        """Compute off-policy estimates.

        Args:
            batch: The batch to calculate the off-policy estimates (OPE) on. The
            batch must contain the fields "obs", "actions", and "action_prob".
            split_batch_by_episode: Whether to split the batch by episode.

        Returns:
            The off-policy estimates (OPE) calculated on the given batch. The returned
            dict can be any arbitrary mapping of strings to metrics.
            The dict consists of the following metrics:
            - v_behavior: The discounted return averaged over episodes in the batch
            - v_behavior_std: The standard deviation corresponding to v_behavior
            - v_target: The estimated discounted return for `self.policy`,
            averaged over episodes in the batch
            - v_target_std: The standard deviation corresponding to v_target
            - v_gain: v_target / max(v_behavior, 1e-8)
            - v_delta: The difference between v_target and v_behavior.
        """
        batch = self.convert_ma_batch_to_sample_batch(batch)
        self.check_action_prob_in_batch(batch)
        estimates_per_epsiode = []
        if split_batch_by_episode:
            batch = self.on_before_split_batch_by_episode(batch)
            all_episodes = batch.split_by_episode()
            all_episodes = self.on_after_split_batch_by_episode(all_episodes)
            for episode in all_episodes:
                assert len(set(episode[SampleBatch.EPS_ID])) == 1, (
                    "The episode must contain only one episode id. For some reason "
                    "the split_by_episode() method could not successfully split "
                    "the batch by episodes. Each row in the dataset should be "
                    "one episode. Check your evaluation dataset for errors."
                )
                self.peek_on_single_episode(episode)

            for episode in all_episodes:
                estimate_step_results = self.estimate_on_single_episode(episode)
                estimates_per_epsiode.append(estimate_step_results)

            # turn a list of identical dicts into a dict of lists
            estimates_per_epsiode = tree.map_structure(
                lambda *x: list(x), *estimates_per_epsiode
            )
        else:
            # the returned dict is a mapping of strings to a list of floats
            estimates_per_epsiode = self.estimate_on_single_step_samples(batch)

        estimates = {
            "v_behavior": np.mean(estimates_per_epsiode["v_behavior"]),
            "v_behavior_std": np.std(estimates_per_epsiode["v_behavior"]),
            "v_target": np.mean(estimates_per_epsiode["v_target"]),
            "v_target_std": np.std(estimates_per_epsiode["v_target"]),
        }
        estimates["v_gain"] = estimates["v_target"] / max(estimates["v_behavior"], 1e-8)
        estimates["v_delta"] = estimates["v_target"] - estimates["v_behavior"]

        return estimates

    @DeveloperAPI
    def convert_ma_batch_to_sample_batch(self, batch: SampleBatchType) -> SampleBatch:
        """Converts a MultiAgentBatch to a SampleBatch if neccessary.

        Args:
            batch: The SampleBatchType to convert.

        Returns:
            batch: the converted SampleBatch

        Raises:
            ValueError if the MultiAgentBatch has more than one policy_id
            or if the policy_id is not `DEFAULT_POLICY_ID`
        """
        # TODO: Make this a util to sample_batch.py
        if isinstance(batch, MultiAgentBatch):
            policy_keys = batch.policy_batches.keys()
            if len(policy_keys) == 1 and DEFAULT_POLICY_ID in policy_keys:
                batch = batch.policy_batches[DEFAULT_POLICY_ID]
            else:
                raise ValueError(
                    "Off-Policy Estimation is not implemented for "
                    "multi-agent batches. You can set "
                    "`off_policy_estimation_methods: {}` to resolve this."
                )
        return batch

    @DeveloperAPI
    def check_action_prob_in_batch(self, batch: SampleBatchType) -> None:
        """Checks if we support off policy estimation (OPE) on given batch.

        Args:
            batch: The batch to check.

        Raises:
            ValueError: In case `action_prob` key is not in batch
        """

        if "action_prob" not in batch:
            raise ValueError(
                "Off-policy estimation is not possible unless the inputs "
                "include action probabilities (i.e., the policy is stochastic "
                "and emits the 'action_prob' key). For DQN this means using "
                "`exploration_config: {type: 'SoftQ'}`. You can also set "
                "`off_policy_estimation_methods: {}` to disable estimation."
            )

    @DeveloperAPI
    def train(self, batch: SampleBatchType) -> Dict[str, Any]:
        """Train a model for Off-Policy Estimation.

        Args:
            batch: SampleBatch to train on

        Returns:
            Any optional metrics to return from the estimator
        """
        return {}

    @Deprecated(
        old="OffPolicyEstimator.action_log_likelihood",
        new="ray.rllib.utils.policy.compute_log_likelihoods_from_input_dict",
        error=True,
    )
    def action_log_likelihood(self, batch: SampleBatchType) -> TensorType:
        log_likelihoods = compute_log_likelihoods_from_input_dict(self.policy, batch)
        return convert_to_numpy(log_likelihoods)
