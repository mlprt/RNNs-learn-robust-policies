from typing import Literal
import equinox as eqx
import jax.numpy as jnp
import jax.random as jr
import jax.tree as jt

from feedbax import get_ensemble, is_module
from feedbax.intervene import schedule_intervenor
from feedbax.misc import attr_str_tree_to_where_func
from feedbax.train import filter_spec_leaves
from feedbax.xabdeef.models import point_mass_nn

from rnns_learn_robust_motor_policies.constants import (
    DISTURBANCE_CLASSES, 
    INTERVENOR_LABEL, 
    MASS,
)
from rnns_learn_robust_motor_policies.misc import vector_with_gaussian_length
from rnns_learn_robust_motor_policies.setup_utils import get_base_task
from rnns_learn_robust_motor_policies.types import TaskModelPair, TrainStdDict


disturbance_params = {
    'curl': dict(amplitude=lambda trial_spec, key: jr.normal(key, (1,))),
    'constant': dict(field=vector_with_gaussian_length),
}


def setup_task_model_pairs(
    *,
    n_replicates,
    dt,
    hidden_size,
    n_steps,
    feedback_delay_steps,
    feedback_noise_std,
    motor_noise_std,
    disturbance_type: Literal['constant', 'curl'],
    disturbance_stds,
    key,
    **kwargs,
):
    task_base = get_base_task(
        n_steps=n_steps,
    )
    
    models = get_ensemble(
        point_mass_nn,
        task_base,
        n_ensemble=n_replicates,
        dt=dt,
        mass=MASS,
        hidden_size=hidden_size, 
        n_steps=n_steps,
        feedback_delay_steps=feedback_delay_steps,
        feedback_noise_std=feedback_noise_std,
        motor_noise_std=motor_noise_std,
        key=key,
    )
    
    def disturbance(field_std, active=True):
        return DISTURBANCE_CLASSES[disturbance_type].with_params(
            scale=field_std,
            active=active,
            **disturbance_params[disturbance_type],
        )
    
    task_model_pairs = jt.map(
        lambda field_std: TaskModelPair(*schedule_intervenor(
            task_base, models,
            lambda model: model.step.mechanics,
            disturbance(field_std),
            label=INTERVENOR_LABEL,
            default_active=False,
        )),
        TrainStdDict(zip(disturbance_stds, disturbance_stds)),  
    )
    
    return task_model_pairs


def setup_model_parameter_histories(
    models_tree,
    *,
    where_train_strs,
    save_model_parameters,
    key,
):
    n_save_steps = len(save_model_parameters)
    where_train = attr_str_tree_to_where_func(where_train_strs)
    
    models_parameters = jt.map(
        lambda models: eqx.filter(eqx.filter(
            models, 
            filter_spec_leaves(models, where_train),
        ), eqx.is_array),
        models_tree,
        is_leaf=is_module,
    )
    
    model_parameter_histories = jt.map(
        lambda x: (
            jnp.empty((n_save_steps,) + x.shape)
            if eqx.is_array(x) else x
        ),
        models_parameters,
    )
    
    return model_parameter_histories



