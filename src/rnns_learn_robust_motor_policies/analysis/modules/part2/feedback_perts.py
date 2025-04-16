from collections.abc import Callable
from types import MappingProxyType, SimpleNamespace
from typing import ClassVar, Literal, Optional
import jax.numpy as jnp
import jax.tree as jt 

import equinox as eqx
from jax_cookbook import is_type, is_module, is_none
import jax_cookbook.tree as jtree


from feedbax.intervene import schedule_intervenor
import feedbax.plotly as fbp
from feedbax.task import TrialSpecDependency

# from rnns_learn_robust_motor_policies.analysis import measures
from rnns_learn_robust_motor_policies.analysis import AbstractAnalysis, AnalysisInputData
from rnns_learn_robust_motor_policies.analysis.aligned import AlignedEffectorTrajectories, AlignedVars
from rnns_learn_robust_motor_policies.analysis.analysis import _DummyAnalysis, DefaultFigParamNamespace, FigParamNamespace
from rnns_learn_robust_motor_policies.analysis.disturbance import FB_INTERVENOR_LABEL, get_pert_amp_vmap_eval_func, task_with_pert_amp
from rnns_learn_robust_motor_policies.analysis.effector import EffectorTrajectories
from rnns_learn_robust_motor_policies.analysis.measures import Measures
from rnns_learn_robust_motor_policies.analysis.profiles import VelocityProfiles
from rnns_learn_robust_motor_policies.plot import PLANT_VAR_LABELS, WHERE_PLOT_PLANT_VARS
from rnns_learn_robust_motor_policies.analysis.state_utils import get_best_replicate_states, vmap_eval_ensemble
from rnns_learn_robust_motor_policies.types import LDict, unflatten_dict_keys
from rnns_learn_robust_motor_policies.perturbations import feedback_impulse


ID = "2-2"


COLOR_FUNCS = dict()


#! TODO: Move; these are redundant with 1-2
PERT_VAR_NAMES = ('fb_pos', 'fb_vel')
COORD_NAMES = ('x', 'y')
I_IMPULSE_AMP_PLOT = -1  # The largest amplitude perturbation
COMPONENTS_LABELS = (r'\parallel', r'\bot')
COMPONENTS_NAMES = ('parallel', 'orthogonal')


def get_context_input_func(x, n_steps: int, n_trials: int):
    return lambda trial_spec, key: (
        jnp.full((n_trials, n_steps - 1), x, dtype=float)
    )


def setup_eval_tasks_and_models(task_base, models_base, hps):
    impulse_end_step = hps.pert.start_step + hps.pert.duration
    impulse_time_idxs = slice(hps.pert.start_step, impulse_end_step)

    all_impulse_amplitudes = jt.map(
        lambda max_amp: jnp.linspace(0, max_amp, hps.pert.n_amps + 1)[1:],
        LDict.of("pert__var").from_ns(hps.pert.amp_max),
    )

    all_tasks, all_models, all_hps = jtree.unzip(jt.map(
        lambda feedback_var_idx, impulse_amplitudes: (
            *schedule_intervenor(
                task_base, models_base,
                lambda model: model.step.feedback_channels[0],  # type: ignore
                feedback_impulse(  
                    hps.model.n_steps,
                    1.0,  # Will be varied later
                    hps.pert.duration,
                    feedback_var_idx,   
                    hps.pert.start_step,
                ),
                default_active=False,
                stage_name="update_queue",
                label=FB_INTERVENOR_LABEL,
            ),
            hps | unflatten_dict_keys(dict(pert__amp=impulse_amplitudes)),
        ),
        LDict.of("pert__var")(dict(fb_pos=0, fb_vel=1)),
        all_impulse_amplitudes,
        is_leaf=is_type(tuple),
    ))

    # # Get the perturbation directions, for later:
    # #? I think these values are equivalent to `line_vec` in the functions in `state_utils`
    # impulse_directions = jt.map(
    #     lambda task: task.validation_trials
    #         .intervene[FB_INTERVENOR_LABEL]
    #         .arrays[:, hps.pert.start_step],
    #     all_tasks,
    #     is_leaf=is_module,
    # )

    # Generate tasks with different context inputs
    # TODO: Ideally we'd just `tree_at` or `vmap` a single instance, instead of constructing a whole PyTree of them
    all_tasks, all_models, all_hps = jtree.unzip(jt.map(
        lambda task, model, hps: LDict.of("context_input")({
            context_input: (
                eqx.tree_at(
                    lambda task: task.input_dependencies,
                    task,
                    {
                        'context': TrialSpecDependency(get_context_input_func(
                            context_input, hps.model.n_steps, task.n_validation_trials,
                        ))
                    },
                ),
                model,  
                hps | dict(context_input=context_input),
            )
            for context_input in hps.context_input
        }),
        all_tasks,
        all_models,
        all_hps,
        is_leaf=is_module,
    ))
    
    extras = SimpleNamespace(
        # impulse_directions=impulse_directions,
        impulse_time_idxs=impulse_time_idxs,
    )
    
    return all_tasks, all_models, all_hps, extras


def get_impulse_origins_directions(task, hps):
    # Steady-state positions
    origins = task.validation_trials.inits["mechanics.effector"].pos

    # Impulse directions
    directions = (
        task
        .validation_trials
        .intervene[FB_INTERVENOR_LABEL]
        .arrays[:, hps.pert.start_step]
    )

    return origins, directions


eval_func = get_pert_amp_vmap_eval_func(lambda hps: hps.pert.amp, FB_INTERVENOR_LABEL)


MEASURE_KEYS = [
    "max_net_force",
    "max_parallel_force_reverse",
    "sum_net_force",
    "max_parallel_vel_forward",
    "max_parallel_vel_reverse",
    "max_orthogonal_vel_left",
    "max_orthogonal_vel_right",
    "max_deviation",
    "sum_deviation",
]


# TODO: We wouldn't need to hardcode this if we could pass a callable to `after_indexing`
ORIGIN_GRID_IDX = 12

#! Add a couple more measures over specific intervals of the trials
# shortly_after_impulse = slice(impulse_end_step, impulse_end_step + impulse_duration)
# after_impulse = slice(impulse_end_step, None)
# custom_measures, custom_measure_labels = jtree.unzip({
#     "max_parallel_force_forward_shortly_after_impulse": (
#         measures.set_timesteps(
#             measures.max_parallel_force, shortly_after_impulse,
#         ),
#         f"Max forward force within {impulse_duration} steps of pert. end",
#     ),
#     "max_net_force_during_impulse": (
#         measures.set_timesteps(
#             measures.max_net_force, impulse_time_idxs,
#         ),
#         "Max net force during pert.",
#     ),
#     "max_net_force_after_impulse": (
#         measures.set_timesteps(
#             measures.max_net_force, after_impulse,
#         ),
#         "Max net force after pert.",
#     ),
# })
# all_measures = subdict(MEASURES, measure_keys) | custom_measures
# measure_labels = MEASURE_LABELS | custom_measure_labels

# State PyTree structure: ['pert__var', 'context_input', 'train__pert__std']
# Array batch shape: (evals, replicates, impulse amplitudes, reach conditions)
ALL_ANALYSES = [
    # 1. Example trial sets (single trial, single replicate)
    # 2. Aligned profiles: compare training conditions 
    # 3. Aligned profiles: compare feedback variables for lo-hi train conditions
    # 4. Measures: Comparison across train conditions
    # 5. Measures: Comparison across lo-hi train conditions
    # 6. Measures: Comparison across impulse amplitudes
    
    # (
    #     EffectorTrajectories(
    #         variant="full",
    #         colorscale_axis=1,  # impulse amplitude  # TODO: change to 0 if indexing eval
    #         colorscale_key='pert__amp',
    #     )
    #     .transform(get_best_replicate_states) 
    #     # .after_indexing(2, ORIGIN_GRID_IDX, axis_label='grid')
    #     # .after_indexing(0, i_eval, axis_label='eval')
    #     .with_fig_params(
    #         mean_exclude_axes=(-3,),  # TODO: uncomment if not indexing grid
    #         # curves_mode='markers+lines',
    #         # ms=3,
    #         # scatter_kws=dict(line_width=0.75),
    #         # mean_scatter_kws=dict(line_width=0),
    #     )
    # ),

    (
        AlignedEffectorTrajectories(
            variant="full",
            colorscale_axis=1,
            colorscale_key='pert__amp',
            dependency_params={
                AlignedVars: dict(
                    origins_directions_func=get_impulse_origins_directions,
                )
            }
        )
        .transform(get_best_replicate_states)
    ),

    # (
    #     AlignedEffectorTrajectories(variant="full")
    #     .transform(get_best_replicate_states)
    #     .after_stacking(level='train__pert__std')
    # ),

    (
        VelocityProfiles(
            variant="full",
        )
        # .transform(get_best_replicate_states) 
    ),

    # Measures(measure_keys=MEASURE_KEYS),

    # Effector_SingleEval(
    #     variant="full",
    #     #! TODO: This doesn't result in the impulse amplitude *values* showing up in the legend!
    #     #! (could try to access `colorscale_key` from `hps`, in `Effector_SingleEval`)
    #     colorscale_key='pert__amp',
    # ).with_fig_params(legend_title="Impulse amplitude"),
]