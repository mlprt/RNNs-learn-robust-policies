from collections.abc import Callable
from types import MappingProxyType
from typing import ClassVar, Optional
import equinox as eqx
import jax.numpy as jnp 
import jax.random as jr
import jax.tree as jt
from jaxtyping import Array, Float, PRNGKeyArray

from feedbax.intervene import AbstractIntervenor
from feedbax.task import AbstractTask
import jax_cookbook.tree as jtree
from jax_cookbook import is_type, is_module

from rnns_learn_robust_motor_policies.analysis.analysis import AbstractAnalysis, AnalysisInputData, DefaultFigParamNamespace, FigParamNamespace
from rnns_learn_robust_motor_policies.constants import REPLICATE_CRITERION
from rnns_learn_robust_motor_policies.types import LDict, TreeNamespace


def angle_between_vectors(v2, v1):
    """Return the signed angle between two 2-vectors."""
    return jnp.arctan2(
        v1[..., 0] * v2[..., 1] - v1[..., 1] * v2[..., 0], 
        v1[..., 0] * v2[..., 0] + v1[..., 1] * v2[..., 1],
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


def get_lateral_distance(
    pos: Float[Array, "*batch conditions time xy=2"], 
    pos_endpoints: Float[Array, "point=2 conditions xy=2"],
) -> Float[Array, "*batch conditions time"]:
    """Compute the lateral distance of points from the straight line connecting init and goal positions.
    
    Arguments:
        pos: Trajectories of positions.
        pos_endpoints: Initial and goal reference positions for each condition.
    
    Returns:
        Trajectories of lateral distances to the straight line between endpoints.
    """
    init_pos, goal_pos = pos_endpoints
    
    # Calculate the vectors from 1) inits to goals, and 2) inits to trajectory positions
    direction_vec = goal_pos - init_pos
    point_vec = pos - init_pos[..., None, :]

    # Calculate the cross product between the line vector and the point vector
    # This is the area of the parallelogram they form.
    cross_product = jnp.cross(direction_vec[..., None, :], point_vec)
    
    # Obtain the parallelogram heights (i.e. the lateral distances) by dividing 
    # by the length of the line vectors.
    line_length = jnp.linalg.norm(direction_vec, axis=-1)
    # lateral_dist = jnp.abs(cross_product) / line_length
    lateral_dist = cross_product / line_length[..., None]

    return lateral_dist


def get_pos_endpoints(trial_specs):
    """Given a set of `SimpleReaches` trial specifications, return the stacked start and end positions."""
    return jnp.stack([
        trial_specs.inits['mechanics.effector'].pos, 
        jnp.take(trial_specs.targets['mechanics.effector.pos'].value, -1, axis=-2),
    ], 
    axis=0,
)


def _get_eval_ensemble(models, task):
    def eval_ensemble(key):
        return task.eval_ensemble(
            models,
            n_replicates=jtree.infer_batch_size(models, exclude=is_type(AbstractIntervenor)),
            # Each member of the model ensemble will be evaluated on the same trials
            ensemble_random_trials=False,
            key=key,
        )
    return eval_ensemble

    
@eqx.filter_jit
def vmap_eval_ensemble(
    key: PRNGKeyArray, 
    hps: TreeNamespace, 
    models: eqx.Module, 
    task: AbstractTask,
):
    """Evaluate an ensemble of models on `n` random repeats of a task's validation set."""
    return eqx.filter_vmap(_get_eval_ensemble(models, task))(
        jr.split(key, hps.eval_n)
    )
    
    
def get_aligned_vars(all_states, where_vars, endpoints): 
    """Get variables from state PyTree, and project them onto respective reach directions for their trials."""
    directions = endpoints[1] -  endpoints[0]
    
    return jt.map(
        lambda states: jt.map(
            lambda var: project_onto_direction(var, directions),
            where_vars(states, endpoints),
        ),
        all_states,
        is_leaf=is_module,
    )


def get_constant_task_input(x, n_steps, n_trials):
    return lambda trial_spec, key: (
        jnp.full((n_trials, n_steps), x, dtype=float)
    )


def get_step_task_input(x1, x2, step_step, n_steps, n_trials):
    def input_func(trial_spec, key):
        # Create array of x1 values
        inputs = jnp.full((n_trials, n_steps), x1, dtype=float)
        inputs = inputs.at[:, step_step:].set(x2)

        return inputs

    return input_func


class BestReplicateStates(AbstractAnalysis):
    conditions: tuple[str, ...] = ()
    variant: Optional[str] = None
    dependencies: ClassVar[MappingProxyType[str, type[AbstractAnalysis]]] = MappingProxyType(dict())
    fig_params: FigParamNamespace = DefaultFigParamNamespace()
    i_replicate: Optional[int] = None

    def compute(
        self,
        data: AnalysisInputData,
        *,
        replicate_info,
        **kwargs,
    ):
        result = jt.map(
            lambda states_by_std: LDict.of("train__pert__std")({
                std: jtree.take(
                    states,
                    replicate_info[std]["best_replicates"][REPLICATE_CRITERION],
                    axis=1,
                )
                for std, states in states_by_std.items()
            }),
            data.states,
            is_leaf=LDict.is_of("train__pert__std"),
        )
        return result
    
    