"""Worker that "vectorizes" environments."""
import collections
import copy

import numpy as np

from garage import EpisodeBatch, StepType
from garage.sampler import _apply_env_update
from garage.sampler.default_worker import DefaultWorker


class VecWorker(DefaultWorker):
    """Worker with a single policy and multiple environments.

    Alternates between taking a single step in all environments and asking the
    policy for an action for every environment. This allows computing a batch
    of actions, which is generally much more efficient than computing a single
    action when using neural networks.

    Args:
        seed (int): The seed to use to intialize random number generators.
        max_episode_length (int or float): The maximum length of episodes which
            will be sampled. Can be (floating point) infinity.
        worker_number (int): The number of the worker this update is
            occurring in. This argument is used  set a different seed for
            each worker.
        n_envs (int): Number of environment copies to use.
    """

    DEFAULT_N_ENVS = 8

    def __init__(self,
                 *,
                 seed,
                 max_episode_length,
                 worker_number,
                 n_envs=DEFAULT_N_ENVS):
        super().__init__(seed=seed,
                         max_episode_length=max_episode_length,
                         worker_number=worker_number)
        self._n_envs = n_envs
        self._completed_episodes = []
        self._needs_agent_reset = True
        self._needs_env_reset = True
        self._envs = [None] * n_envs
        self._agents = [None] * n_envs
        self._episode_lengths = [0] * self._n_envs

        n = len(self._envs)
        # yapf: disable
        self._episode_lengths = [0] * n
        self._observations = [[], ] * n
        self._actions = [[], ] * n
        self._rewards = [[], ] * n
        self._step_types = [[], ] * n
        self._env_infos = [collections.defaultdict(list), ] * n
        self._agent_infos = [collections.defaultdict(list), ] * n
        self._episode_infos = [collections.defaultdict(list), ] * n
        # yapf: enable

    def update_agent(self, agent_update):
        """Update an agent, assuming it implements :class:`~Policy`.

        Args:
            agent_update (np.ndarray or dict or Policy): If a
                tuple, dict, or np.ndarray, these should be parameters to
                agent, which should have been generated by calling
                `Policy.get_param_values`. Alternatively, a policy itself. Note
                that other implementations of `Worker` may take different types
                for this parameter.
        """
        super().update_agent(agent_update)
        self._needs_agent_reset = True

    def update_env(self, env_update):
        """Update the environments.

        If passed a list (*inside* this list passed to the Sampler itself),
        distributes the environments across the "vectorization" dimension.

        Args:
            env_update(Environment or EnvUpdate or None): The environment to
                replace the existing env with. Note that other implementations
                of `Worker` may take different types for this parameter.

        Raises:
            TypeError: If env_update is not one of the documented types.
            ValueError: If the wrong number of updates is passed.
        """
        if isinstance(env_update, list):
            if len(env_update) != self._n_envs:
                raise ValueError('If separate environments are passed for '
                                 'each worker, there must be exactly n_envs '
                                 '({}) environments, but received {} '
                                 'environments.'.format(
                                     self._n_envs, len(env_update)))
        elif env_update is not None:
            env_update = [
                copy.deepcopy(env_update) for _ in range(self._n_envs)
            ]
        if env_update:
            for env_index, env_up in enumerate(env_update):
                self._envs[env_index], up = _apply_env_update(
                    self._envs[env_index], env_up)
                self._needs_env_reset |= up

    def start_episode(self):
        """Begin a new episode."""
        if self._needs_agent_reset or self._needs_env_reset:
            n = len(self._envs)
            self.agent.reset([True] * n)
            if self._needs_env_reset:
                obs_list, episode_infos_list = [], []
                for env in self._envs:
                    obs, episode_info = env.reset()
                    obs_list.append(obs)
                    episode_infos_list.append(episode_info)
                
                self._prev_obs = np.asarray(obs_list)
                self._episode_infos = np.array(episode_infos_list)

                    # self._prev_obs = np.asarray(
                    #     [env.reset()[0] for env in self._envs])
            else:
                # Avoid calling reset on environments that are already at the
                # start of an episode.
                for i, env in enumerate(self._envs):
                    if self._episode_lengths[i] > 0:
                        self._prev_obs[i], self._episode_infos[i] = env.reset()
            self._episode_lengths = [0 for _ in range(n)]
            self._observations = [[] for _ in range(n)]
            self._actions = [[] for _ in range(n)]
            self._rewards = [[] for _ in range(n)]
            self._step_types = [[] for _ in range(n)]
            self._env_infos = [collections.defaultdict(list) for _ in range(n)]
            self._agent_infos = [
                collections.defaultdict(list) for _ in range(n)
            ]
            self._needs_agent_reset = False
            self._needs_env_reset = False

    def _gather_episode(self, episode_number, last_observation):
        assert 0 < self._episode_lengths[
            episode_number] <= self._max_episode_length
        env_infos = self._env_infos[episode_number]
        agent_infos = self._agent_infos[episode_number]
        episode_infos = self._episode_infos[episode_number]
        for k, v in env_infos.items():
            env_infos[k] = np.asarray(v)
        for k, v in agent_infos.items():
            agent_infos[k] = np.asarray(v)
        for k, v in episode_infos.items():
            episode_infos[k] = np.asarray(v)
        eps = EpisodeBatch(
            env_spec=self._envs[episode_number].spec,
            observations=np.asarray(self._observations[episode_number]),
            last_observations=np.asarray([last_observation]),
            actions=np.asarray(self._actions[episode_number]),
            rewards=np.asarray(self._rewards[episode_number]),
            step_types=np.asarray(self._step_types[episode_number],
                                  dtype=StepType),
            env_infos=dict(env_infos),
            agent_infos=dict(agent_infos),
            episode_infos=dict(episode_infos),
            lengths=np.asarray([self._episode_lengths[episode_number]],
                               dtype='l'))

        self._completed_episodes.append(eps)
        self._observations[episode_number] = []
        self._actions[episode_number] = []
        self._rewards[episode_number] = []
        self._step_types[episode_number] = []
        self._episode_lengths[episode_number] = 0
        self._prev_obs[episode_number] = self._envs[episode_number].reset()[0]
        self._env_infos[episode_number] = collections.defaultdict(list)
        self._agent_infos[episode_number] = collections.defaultdict(list)
        self._episode_infos[episode_number] = collections.defaultdict(list)

    def step_episode(self):
        """Take a single time-step in the current episode.

        Returns:
            bool: True iff at least one of the episodes was completed.
        """
        finished = False
        actions, agent_info = self.agent.get_actions(self._prev_obs)
        completes = [False] * len(self._envs)
        for i, action in enumerate(actions):
            if self._episode_lengths[i] < self._max_episode_length:
                es = self._envs[i].step(action)
                self._observations[i].append(self._prev_obs[i])
                self._rewards[i].append(es.reward)
                self._actions[i].append(es.action)
                for k, v in agent_info.items():
                    self._agent_infos[i][k].append(v[i])
                for k, v in es.env_info.items():
                    self._env_infos[i][k].append(v)
                self._episode_lengths[i] += 1
                self._step_types[i].append(es.step_type)
                self._prev_obs[i] = es.observation
            if self._episode_lengths[i] >= self._max_episode_length or es.last:
                self._gather_episode(i, es.observation)
                completes[i] = True
                finished = True
        if finished:
            self.agent.reset(completes)
        return finished

    def collect_episode(self):
        """Collect all completed episodes.

        Returns:
            EpisodeBatch: A batch of the episodes completed since the last call
                to collect_episode().

        """
        if len(self._completed_episodes) == 1:
            result = self._completed_episodes[0]
        else:
            result = EpisodeBatch.concatenate(*self._completed_episodes)
        self._completed_episodes = []
        return result

    def shutdown(self):
        """Close the worker's environments."""
        for env in self._envs:
            env.close()
