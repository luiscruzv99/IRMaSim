"""
The environment is the representation of the agent's observable context.
"""

from collections import OrderedDict
from functools import partial
import gym
import gym.spaces
import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from irmasim.workload_manager.Policy import Policy
from irmasim.Simulator import Simulator
from irmasim.Options import Options


class Environment(gym.Env):
    """Environment for workload management in HDeepRM.

It is composed of an action space and an observation space. For every decision step, the agent
selects an action, which is applied to the environment. This involves mapping pending jobs to
available cores. Changes in environment's state are manifested as observations. For each action
taken, the environment provides a reward as feedback to the agent based on its objective. The
environment implementation is compliant with OpenAI gym format.

Any observation is formed by the following data fields:
  | - Fraction of available memory in each node
  | - Fraction of available memory bandwidth in each processor
  | - Fraction of current GFLOPs and Watts with respect to the maximum values for each core
  | - Fraction left for completing the served job by the core
  | - Fraction of requested resources with respect to the maximum values of requested
    time/cores/mem/mem_bw for pending jobs; five percentiles are shown (min, Q1, med, Q3, max) such
    that the agent can devise a job distribution
  | - Variability ratio of job queue size with respect to last observation

The action space is constituted by 37 possible actions, including a void action:
  +---------------+-----------------------------------------------+
  | Job selection | Core selection                                |
  +===============+=======+=======+=======+=======+=======+=======+
  |               | RANDM | HICOM | HICOR | HIMEM | HIMBW | LPOWR |
  +---------------+-------+-------+-------+-------+-------+-------+
  | RANDM         | 0     | 1     | 2     | 3     | 4     | 5     |
  +---------------+-------+-------+-------+-------+-------+-------+
  | FIARR         | 6     | 7     | 8     | 9     | 10    | 11    |
  +---------------+-------+-------+-------+-------+-------+-------+
  | SHORT         | 12    | 13    | 14    | 15    | 16    | 17    |
  +---------------+-------+-------+-------+-------+-------+-------+
  | SMALL         | 18    | 19    | 20    | 21    | 22    | 23    |
  +---------------+-------+-------+-------+-------+-------+-------+
  | LRMEM         | 24    | 25    | 26    | 27    | 28    | 29    |
  +---------------+-------+-------+-------+-------+-------+-------+
  | LRMBW         | 30    | 31    | 32    | 33    | 34    | 35    |
  +---------------+-------+-------+-------+-------+-------+-------+
  | Void action   | 36    |       |       |       |       |       |
  +---------------+-------+-------+-------+-------+-------+-------+

Job selection policies:
  | - RANDM (`random`): random job in the job queue.
  | - FIARR (`first`): oldest job in the job queue.
  | - SHORT (`shortest`): job with the least requested running time.
  | - SMALL (`smallest`): job with the least requested cores.
  | - LRMEM (`low_mem`): job with the least requested memory capacity.
  | - LRMBW (`low_mem_bw`): job with the least requested memory bandwidth.

Core selection policies:
  | - RANDM (`random`): random core in the core pool.
  | - HICOM (`high_gflops`): core with the highest peak compute capability.
  | - HICOR (`high_cores`): core in the processor with the most amount of available cores.
  | - HIMEM (`high_mem`): core in the node with the most amount of current memory capacity.
  | - HIMBW (`high_mem_bw`): core in the processor with the most amount of current memory bandwidth.
  | - LPOWR (`low_power`): core with the lowest power consumption.

Possible objectives for the agent:
  | - Average job slowdown: on average, how much of the service time is due to stalling of jobs in
    the job queue.
  | - Average job completion time: on average, how much service time for jobs in the platform.
  | - Utilization: number of active cores over the simulation time.
  | - Makespan: time span from the arrival of the absolute first job until the completion of the
    absolute last job.
  | - Energy consumption: total amount of energy consumed during the simulation.
  | - Energy Delay Product (EDP): product of the energy consumption by the makespan.

Attributes:
    workload_manager (:class:`~hdeeprm.entrypoints.HDeepRMWorkloadManager.HDeepRMWorkloadManager`):
        Reference to HDeepRM workload manager required to schedule the jobs on the decision step.
    action_space (gym.spaces.Discrete):
        The action space described above. See `Spaces <https://gym.openai.com/docs/#spaces>`_.
    action_keys (list):
        List of sorting key pairs indexed by action IDs. Keys are applied to the job scheduler and
        the resource manager selections.
    observation_space (gym.spaces.Box):
        The observation space described above. See `Spaces <https://gym.openai.com/docs/#spaces>`_.
    reward (function):
        Mapped to a reward function depending on the agent's objective.
    queue_sensitivity (float):
        Sensitivity of the observation to variations in job queue size. If sensitivity is high,
        larger variations will be noticed, however smaller ones will not have significant impact.
        If sensitivity is low, smaller variations will be noticed and large ones will be clipped,
        thus impactless.
    last_job_queue_length (int):
        Last value of the job queue length. Used for calculating the variation ratio.
    """

    def __init__(self, workload_manager: 'Policy', simulator: Simulator) -> None:
        self.workload_manager = workload_manager
        self.simulator = simulator
        self.env_options = Options().get()["workload_manager"]["env"]
        self.last_job_queue_length = None

        self.job_selections = OrderedDict({
            'random': None,
            'first': lambda job: job.submit_time,
            'shortest': lambda job: job.req_time,
            'smallest': lambda job: job.resources,
            'low_mem': lambda job: job.memory,
            'low_mem_ops': lambda job: job.memory_vol
        })

        self.core_selections = OrderedDict({
            'random': None,
            'high_gflops': lambda core: - core.parent['gflops_per_core'],
            'high_cores': lambda core: - len([c for c in core.parent.children \
                                              if c.task is not None]),
            'high_mem': lambda core: - core.parent.parent['current_mem'],
            'high_mem_bw': lambda core: core.parent['current_mem_bw'],
            'low_power': lambda core: core.static_power + core.dynamic_power
        })

        self.actions = []
        if 'actions' in self.env_options:
            for sel in self.env_options['actions']['selection']:
                for job_sel, core_sels in sel.items():
                    for core_sel in core_sels:
                        self.actions.append(
                            (self.job_selections[job_sel], self.core_selections[core_sel])
                        )
            self.with_void = self.env_options['actions']['void']
        else:
            for job_sel in self.job_selections.values():
                for core_sel in self.core_selections.values():
                    self.actions.append((job_sel, core_sel))
            self.with_void = True

        if self.with_void:
            nb_actions = len(self.actions) + 1
        else:
            nb_actions = len(self.actions)
        self.action_space = gym.spaces.Discrete(nb_actions)

        if 'observation' in self.env_options:
            observation_type = self.env_options['observation']
        else:
            observation_type = 'normal'
        self.observation = partial(self._base_observation, otype=observation_type)
        observation_size = self.observation().size
        self.observation_space = gym.spaces.Box(
            low=np.zeros(observation_size, dtype=np.float32),
            high=np.ones(observation_size, dtype=np.float32),
            dtype=np.float32
        )

        objective_to_reward = {
            'makespan': self.makespan_reward,
            'energy_consumption': self.energy_consumption_reward,
            'edp': self.edp_reward
        }
        self.reward = objective_to_reward[self.env_options['objective']]
        self.queue_sensitivity = self.env_options['queue_sensitivity']
        self.last_job_queue_length = 0

    @property
    def action_size(self):
        return self.action_space.n

    @property
    def observation_size(self):
        return self.observation_space.shape[0]

    def _base_observation(self, otype: str):
        def to_range(variation_ratio: float):
            variation_ratio = (variation_ratio + self.queue_sensitivity) \
                              / (2 * self.queue_sensitivity)
            return max(0.0, min(1.0, variation_ratio))

        observation = []
        if otype != 'minimal':
            # TODO Implement Memory metric in node
            """
            for cluster in self.simulator.platform.children:
                for node in cluster.children:
                    observation.append(node.current_mem / node.max_mem)
                    max_mem_bw_proccesor = 0
                    for processor in node.children:
                        if max_mem_bw_proccesor < processor.current_mem_bw:
                            max_mem_bw_proccesor = processor.current_mem_bw

                    for processor in node.children:
                        if max_mem_bw_proccesor == 0:
                            observation.append(0.0)
                        else:
                            observation.append(max(0.0, processor.current_mem_bw) / max_mem_bw_proccesor)

                        if otype != 'small':
                            for core in processor.children:
                                task_remaining_fraction = core.remaining_fraction()
                                observation.extend(
                                    [core.state.current_gflops / processor.gflops_per_core,
                                     core.state.current_power / (core.static_power+core.dynamic_power),
                                     task_remaining_fraction]
                                )
            """
        req_time = np.array(
            [job.req_time for job in self.workload_manager.pending_jobs])
        req_core = np.array(
            [job.resources for job in self.workload_manager.pending_jobs])
        req_mem = np.array(
            [job.memory for job in self.workload_manager.pending_jobs])
        req_mem_vol = np.array(
            [job.memory_vol for job in self.workload_manager.pending_jobs])

        job_limits = self.simulator.job_limits
        reqes = (req_time, req_core, req_mem, req_mem_vol)
        maxes = (job_limits['max_time'], job_limits['max_core'],
                 job_limits['max_mem'], job_limits['max_mem_vol'])

        for reqe, maxe in zip(reqes, maxes):
            if reqe.size != 0:
                pmin = np.min(reqe) / maxe
                p25 = np.percentile(reqe, 25) / maxe
                pmed = np.median(reqe) / maxe
                p75 = np.percentile(reqe, 75) / maxe
                pmax = np.max(reqe) / maxe
            else:
                pmin = p25 = pmed = p75 = pmax = 0

            observation.extend([pmin, p25, pmed, p75, pmax])

        if self.last_job_queue_length is None or \
                min(len(self.workload_manager.pending_jobs), self.last_job_queue_length) == 0:
            variation_ratio = 1.0

        else:
            variation = len(self.workload_manager.pending_jobs) - self.last_job_queue_length
            variation_ratio = to_range(
                variation / min(len(self.workload_manager.pending_jobs), self.last_job_queue_length)
            )
        observation.append(variation_ratio)
        self.last_job_queue_length = len(self.workload_manager.pending_jobs)
        return np.array(observation, dtype=np.float32)

    def makespan_reward(self) -> float:
        return self.simulator.simulation_time - self.workload_manager.last_time

    def energy_consumption_reward(self) -> float:
        delta_time = self.simulator.simulation_time - self.workload_manager.last_time
        return -1 * self.simulator.platform.get_joules(delta_time)

    def edp_reward(self) -> float:
        return self.energy_consumption_reward() * self.makespan_reward()