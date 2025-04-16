from collections.abc import Callable
from copy import deepcopy
from functools import partial
from types import MappingProxyType
from typing import ClassVar, Optional, Literal as L

from equinox import Module
import jax.numpy as jnp
import jax.tree as jt
from jaxtyping import PyTree, Array, Float
import plotly.graph_objects as go

import feedbax.plotly as fbp
from feedbax.task import AbstractTask
from jax_cookbook import is_module, is_type
import jax_cookbook.tree as jtree

from rnns_learn_robust_motor_policies.analysis.analysis import (
    AbstractAnalysis,
    AnalysisInputData,
    DefaultFigParamNamespace,
    FigParamNamespace,
    get_validation_trial_specs,
)
from rnns_learn_robust_motor_policies.analysis.state_utils import get_pos_endpoints
from rnns_learn_robust_motor_policies.colors import COLORSCALES
from rnns_learn_robust_motor_policies.config import PLOTLY_CONFIG
from rnns_learn_robust_motor_policies.hyperparams import flat_key_to_where_func
from rnns_learn_robust_motor_policies.plot import add_endpoint_traces
from rnns_learn_robust_motor_policies.plot_utils import get_label_str
from rnns_learn_robust_motor_policies.types import (
    RESPONSE_VAR_LABELS, 
    Responses, 
    LDict,
    TreeNamespace,
)

#! See also `plot.WHERE_PLOT_PLANT_VARS`
WHERE_VARS_TO_ALIGN = lambda states, origins: Responses(
    # Positions with respect to the origin
    states.mechanics.effector.pos - origins[..., None, :],
    states.mechanics.effector.vel,
    states.efferent.output,
)


def get_forward_lateral_vel(
    velocity: Float[Array, "*batch conditions time xy=2"], 
    pos_endpoints: Float[Array, "point=2 conditions xy=2"],
) -> Float[Array, "*batch conditions time 2"]:
    """Given x-y velocity components, rebase onto components forward and lateral to the line between endpoints.
    
    Arguments:
        velocity: Trajectories of velocity vectors.
        pos_endpoints: Initial and goal reference positions for each condition, defining reference lines.
    
    Returns:
        forward: Forward velocity components (parallel to the reference lines).
        lateral: Lateral velocity components (perpendicular to the reference lines).
    """
    init_pos, goal_pos = pos_endpoints
    direction_vec = goal_pos - init_pos
    
    return project_onto_direction(velocity, direction_vec)
    

def project_onto_direction(
    var: Float[Array, "*batch conditions time xy=2"],
    direction_vec: Float[Array, "conditions xy=2"],
):
    """Projects components of arbitrary variables into components parallel and orthogonal to a given direction.
    
    Arguments:
        var: Data with x-y components to be projected. 
        direction_vector: Direction vectors. 
    
    Returns:
        projected: Projected components (parallel and orthogonal).
    """
    # Normalize the line vector
    direction_vec_norm = direction_vec / jnp.linalg.norm(direction_vec, axis=-1, keepdims=True)
    
    # Broadcast line_vec_norm to match velocity's shape
    direction_vec_norm = direction_vec_norm[:, None]  # Shape: (conditions, 1, xy)
    
    # Calculate forward velocity (dot product)
    parallel = jnp.sum(var * direction_vec_norm, axis=-1)
    
    # Calculate lateral velocity (cross product)
    orthogonal = jnp.cross(direction_vec_norm, var)
    
    return jnp.stack([parallel, orthogonal], axis=-1)


def get_aligned_vars(vars, directions): 
    """Get variables from state PyTree, and project them onto respective reach directions for their trials."""
    return jt.map(
        lambda var: project_onto_direction(var, directions),
        vars,
    )


def get_reach_origins_directions(task: AbstractTask, hps: TreeNamespace):
    pos_endpoints = get_pos_endpoints(get_validation_trial_specs(task))
    directions = pos_endpoints[1] - pos_endpoints[0]
    origins = pos_endpoints[0]
    return origins, directions


class AlignedVars(AbstractAnalysis):
    """Align spatial variable (e.g. position and velocity) coordinates with the reach direction."""
    dependencies: ClassVar[MappingProxyType[str, type[AbstractAnalysis]]] = MappingProxyType(dict())
    conditions: tuple[str, ...] = ()
    variant: Optional[str] = None
    fig_params: FigParamNamespace = DefaultFigParamNamespace()
    origins_directions_func: Callable = get_reach_origins_directions

    def compute(
        self,
        data: AnalysisInputData,
        **kwargs,
    ):
        def _get_aligned_vars_by_std(task, states_by_std, hps):
            origins, directions = self.origins_directions_func(task, hps)
            
            return jt.map(
                lambda states: jt.map(
                    lambda var: project_onto_direction(var, directions),
                    WHERE_VARS_TO_ALIGN(states, origins)
                ),
                states_by_std,
                is_leaf=is_module,
            )

        result = jt.map(
            _get_aligned_vars_by_std,
            data.tasks,
            data.states,
            data.hps,
            is_leaf=is_module,
        )

        return result
        
        
class AlignedEffectorTrajectories(AbstractAnalysis):
    dependencies: ClassVar[MappingProxyType[str, type[AbstractAnalysis]]] = MappingProxyType(dict(
        aligned_vars=AlignedVars,
    ))
    conditions: tuple[str, ...] = ()
    variant: Optional[str] = "small"
    fig_params: FigParamNamespace = DefaultFigParamNamespace(
        var_labels=RESPONSE_VAR_LABELS,
        axes_labels=('Parallel', 'Orthogonal'),
        # mode='std',
        # n_curves_max=n_curves_max,
        darken_mean=PLOTLY_CONFIG.mean_lighten_factor,
        n_curves_max=20,
        layout_kws=dict(
            width=900,
            height=300,
            legend_tracegroupgap=1,
            margin_t=50,
            margin_b=20,
        ),
        scatter_kws=dict(
            line_width=0.5,
            opacity=0.5,
        ),
        mean_scatter_kws=dict(
            line_width=2.5,
            opacity=1,
        ),
    )
    colorscale_key: Optional[str] = None 
    colorscale_axis: Optional[int] = None
    pos_endpoints: bool = True

    def make_figs(
        self,
        data: AnalysisInputData,
        *,
        aligned_vars,
        hps_common,
        colorscales,
        **kwargs,
    ):
        fig_params = deepcopy(self.fig_params)

        if fig_params.legend_title is None and self.colorscale_key is not None:
            fig_params.legend_title = get_label_str(self.colorscale_key)
            
        try:
            fig_params.legend_labels = flat_key_to_where_func(self.colorscale_key)(hps_common)
        except:
            pass

        figs = jt.map(
            partial(
                fbp.trajectories_2D,
                colorscale=colorscales[self.colorscale_key],
                colorscale_axis=self.colorscale_axis,
                curves_mode='lines',
                **fig_params,
            ),
            aligned_vars[self.variant],
            is_leaf=is_type(Responses),
        )

        if self.pos_endpoints:
            #! Assume all tasks are straight reaches with the same length.
            #! TODO: Remove this assumption. Depending on `_pre_ops`/`_fig_ops`, the 
            #! PyTree structure of `data.tasks[self.variant]` may differ from that of `figs`
            #! and thus we have to be careful about how to perform the mapping. 
            #! (In the simplest case, without ops, the task PyTree is a prefix of `figs`)
            task_0 = jt.leaves(data.tasks[self.variant], is_leaf=is_type(AbstractTask))[0]
            pos_endpoints = self._get_aligned_pos_endpoints(task_0.eval_reach_length)

            figs = jt.map(
                lambda fig: add_endpoint_traces(
                    fig, 
                    pos_endpoints, 
                    xaxis='x1', 
                    yaxis='y1', 
                ),
                figs,
                is_leaf=is_type(go.Figure),
            )

        return figs

    def _get_aligned_pos_endpoints(self, eval_reach_length: TreeNamespace) -> Array:
        return jnp.array([[0., 0.], [eval_reach_length, 0.]])

    def _params_to_save(self, hps: PyTree[TreeNamespace], *, hps_common, **kwargs):
        return dict(
            # n=min(self.n_curves_max, hps_common.eval_n * n_replicates_included[train_pert_std] * self.n_conditions)
        )