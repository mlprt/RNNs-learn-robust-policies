#!/usr/bin/env python
# coding: utf-8

NB_ID = "2"

## Environment setup

from collections.abc import Callable, Sequence
import os
from pathlib import Path

os.environ["TF_CUDNN_DETERMINISTIC"] = "1"

import argparse
from functools import partial
from typing import Any, Literal, Optional, Type
import yaml

import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as jr
import jax.tree as jt
import jax.tree_util as jtu
from jaxtyping import PRNGKeyArray, PyTree
import optax 

import feedbax
from feedbax import (
    is_module,
    is_type,
    tree_concatenate,
    tree_map_tqdm,
    tree_unzip,
)
from feedbax.loss import AbstractLoss, ModelLoss
from feedbax.misc import where_func_to_labels, attr_str_tree_to_where_func
from feedbax.train import TaskTrainer, TaskTrainerHistory
from feedbax.xabdeef.losses import simple_reach_loss

import rnns_learn_robust_motor_policies
from rnns_learn_robust_motor_policies import PROJECT_SEED
from rnns_learn_robust_motor_policies.database import (
    get_db_session,
    save_model_and_add_record,
)
from rnns_learn_robust_motor_policies.misc import log_version_info, subdict
from rnns_learn_robust_motor_policies.setup_utils import (
    get_readout_norm_loss,
    train_histories_hps_select,
)
from rnns_learn_robust_motor_policies.train_setup_part1 import (
    setup_task_model_pair,
)
from rnns_learn_robust_motor_policies.tree_utils import deep_update, pp
from rnns_learn_robust_motor_policies.types import TaskModelPair, TrainingMethodDict
from rnns_learn_robust_motor_policies.train_setup import (
    concat_save_iterations,
    iterations_to_save_model_parameters,
    make_delayed_cosine_schedule,
    train_pair,
)
from rnns_learn_robust_motor_policies.types import TrainStdDict


## Create model-task pairings for different disturbance conditions


CONFIG_DIR = Path('../config')


def load_default_config(nb_id: str) -> dict:
    """Load config from file or use defaults"""
    return load_config(CONFIG_DIR / f"{nb_id}.yml")


def load_config(path: str | Path) -> dict:
    with open(path) as f:
        config = yaml.safe_load(f)
    return config


def construct_spread_dict(
    name: str,
    func: Callable[[], Any],
    keys: Sequence[Any],
):
    return dict(zip(
        keys, 
        map(
            lambda k: func(**{name: k}), 
            keys,
        )
    ))
    # return jt.map(
    #     lambda k: func(**{name: k}),
    #     dict_type(zip(keys, keys)),
    # )
    
    
def train_setup(
    train_hps: dict,
) -> tuple[TaskTrainer, AbstractLoss]:
    optimizer_class = partial(
        optax.adamw,
        weight_decay=train_hps['weight_decay'],
    ) 

    schedule = make_delayed_cosine_schedule(
        train_hps['learning_rate_0'], 
        train_hps['constant_lr_iterations'], 
        train_hps['n_batches_baseline'] + train_hps['n_batches_condition'], 
        train_hps['cosine_annealing_alpha'],
    ) 

    trainer = TaskTrainer(
        optimizer=optax.inject_hyperparams(optimizer_class)(
            learning_rate=schedule,
        ),
        checkpointing=True,
    )
    
    loss_func = simple_reach_loss()
    
    if all(k in train_hps for k in ('readout_norm_loss_weight', 'readout_norm_value')):
        readout_norm_loss = (
            train_hps['readout_norm_loss_weight'] 
            * get_readout_norm_loss(train_hps['readout_norm_value'])
        )
        loss_func = loss_func + readout_norm_loss
    
    return trainer, loss_func


def flatten_with_paths(tree, is_leaf=None):
    return jax.tree_util.tree_flatten_with_path(tree, is_leaf=is_leaf)


def index_multi(seq, *args):
    if args == ():
        return seq
    return index_multi(seq[args[0]], *args[1:])


def hps_given_path(path, model_hps, train_hps):
    # This is specific to notebook 2.
    # For training notebook 1, we only have `model_hps |= dict(disturbance_std=path[0])`.
    # In principle we might want to infer this from the structure of the PyTree itself,
    # except that provides insufficient information to properly name the hyperparameters.
    # One alternative is to make a constant (common) mapping from node types to 
    # hyperparameter names. For example, `TrainStdDict -> 'disturbance_std'` or 
    # `TrainingMethodDict -> 'train_method'`. But how can we infer whether it should be 
    # added to `train_hps`, versus `model_hps`? Or should we just add these kwargs to 
    # BOTH of them?
    model_hps |= dict(disturbance_std=path[1].key)
    train_hps |= dict(train_method=path[0].key)
    return model_hps, train_hps


def save_all_models(
    db_session,
    all_models: PyTree[eqx.Module, 'T'], 
    train_hps: dict, 
    model_hps: dict, 
    all_train_histories: Optional[PyTree[TaskTrainerHistory, 'T']] = None,
    **kwargs,
):
    model_records_flat = []
    
    path_vals, treedef = jtu.tree_flatten_with_path(all_models, is_leaf=is_module)
    
    for path, models in path_vals:
        
        # # TODO: These two lines depend on the structure of the `all_models` tree
        # # i.e. we should turn this into a function that can be swapped out
        # # Also, we should not hardcode the names of the variables
        # model_hps |= dict(disturbance_std=path[1])
        # train_hps |= dict(train_method=path[0])
        
        # DONE:
        model_hps_i, train_hps_i = hps_given_path(path, model_hps, train_hps)
        
        model_record = save_model_and_add_record(
            db_session,
            origin=NB_ID,
            model=models,
            model_hyperparameters=model_hps_i,
            other_hyperparameters=train_hps_i,
            # Assume all the pytree levels are dicts
            train_history=index_multi(all_train_histories, *[p.key for p in path[:-1]]),
            train_history_hyperparameters=train_histories_hps_select(
                train_hps_i, 
                model_hps_i,
            ),
            **kwargs,
        )
        
        model_records_flat.append(model_record)
    
    model_records = jt.unflatten(treedef, model_records_flat)
    return model_records


def process_hps(config: dict):
    model_hps = config['model']
    train_hps = config['training']
    disturbance = config['disturbance']
    
    ## Compute any extra dependencies 
    ## and add to the hyperparameters for model construction
    intervention_scaleup_batches = (
        train_hps['n_batches_baseline'],
        train_hps['n_batches_baseline'] + train_hps['n_scaleup_batches'],
    )
    
    # Update with missing arguments to `setup_task_model_pair` and `train_setup`, respectively
    model_hps |= dict(
        disturbance_type=disturbance['type'],
        intervention_scaleup_batches=intervention_scaleup_batches,
    )
    train_hps['n_batches'] = train_hps['n_batches_baseline'] + train_hps['n_batches_condition']
    train_hps['save_model_parameters'] = iterations_to_save_model_parameters(
        train_hps['n_batches']
    )
    
    return model_hps, train_hps, disturbance    


def main(config: dict, key: PRNGKeyArray):
    key_init, key_train, key_eval = jr.split(key, 3)
    
    model_hps, train_hps, disturbance = process_hps(config)
    
    # TODO: Refactor this into a function, and then I think the only difference between part 1 and part 2
    # is 1) that function, and 2) `hps_given_path`, if we are using it. All the other differences are taken 
    # care of by `setup_task_model_pair`
    ## Construct a model (ensemble) for each value of the disturbance std
    task_model_pairs = TrainingMethodDict({
        method_label: TrainStdDict(construct_spread_dict(
            'disturbance_std',
            partial(
                setup_task_model_pair, 
                **model_hps | dict(training_method=method_label), 
                key=key_init,
            ),
            disturbance['stds'][disturbance['type']],  # The sequence of stds for the given disturbance type
        ))
        for method_label in train_hps['methods']
    })
    
    trainer, loss_func = train_setup(train_hps)
    
    # Convert string representations of where-functions to actual functions.
    # 
    #   - Strings are easy to serialize, or to specify in config files; functions are not.
    #   - These where-functions are for selecting the trainable nodes in the pytree of model 
    #     parameters.
    #
    where_train = {
        i: attr_str_tree_to_where_func(strs) 
        for i, strs in train_hps['where_train_strs'].items()
    }
    
    ## Train all the models.
    # Organize the constant arguments for the calls to `train_pair`
    train_args = dict(
        ensembled=True,
        loss_func=loss_func,
        # TODO: Is this correct? Or should we pass the task for the respective training method?
        task_baseline=jt.leaves(task_model_pairs, is_leaf=is_type(TrainStdDict))[0][0].task, 
        where_train=where_train,
        batch_size=train_hps['batch_size'], 
        log_step=500,
        save_model_parameters=train_hps['save_model_parameters'],
        state_reset_iterations=train_hps['state_reset_iterations'],
        # disable_tqdm=True,
    )

    # The imported `train_pair` function actually runs the trainer
    trained_models, train_histories = tree_unzip(tree_map_tqdm(
        partial(train_pair, trainer, train_hps['n_batches'], key=key_train, **train_args),
        task_model_pairs,
        label="Training all pairs",
        is_leaf=is_type(TaskModelPair),
    ))
    
    ## Create a database record for each ensemble of models trained (i.e. one per disturbance std).   
    # Save the models and training histories to disk.
    model_records = save_all_models(
        db_session,
        trained_models,
        train_hps,
        model_hps,
        train_histories,
    )
    
    return model_records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process some files.")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()
    
    version_info = log_version_info(
        jax, eqx, optax, git_modules=(feedbax, rnns_learn_robust_motor_policies),
    )
    
    db_session = get_db_session()
    
    default_config = load_default_config(NB_ID)
    
    if args.config is not None:
        config = deep_update(default_config, load_config(args.config))
    else:
        config = default_config
    
    key = jr.PRNGKey(PROJECT_SEED)
    
    model_records = main(config, key)