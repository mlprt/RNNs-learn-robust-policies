from collections.abc import Sequence
from copy import deepcopy
from functools import partial
import logging
from pathlib import Path
from typing import Any, Optional, TypeAlias

import equinox as eqx
import jax.numpy as jnp
import jax.random as jr
import jax.tree as jt
from jaxtyping import Array, PRNGKeyArray, PyTree
import numpy as np
import optax

from feedbax._io import arrays_to_lists
from feedbax.loss import AbstractLoss
from feedbax.misc import attr_str_tree_to_where_func
from feedbax.task import AbstractTask
from feedbax.train import TaskTrainer
from feedbax.xabdeef.losses import simple_reach_loss
import jax_cookbook.tree as jtree
from jax_cookbook import is_type

from rnns_learn_robust_motor_policies.database import ModelRecord, get_record, save_model_and_add_record
from rnns_learn_robust_motor_policies.hyperparams import (
    TreeNamespace,
    flatten_hps, 
    load_hps,
    namespace_to_dict, 
    promote_model_hps, 
    fill_out_hps,
)
from rnns_learn_robust_motor_policies.tree_utils import (
    pp, 
)
from rnns_learn_robust_motor_policies.types import TaskModelPair

from rnns_learn_robust_motor_policies.training.part1_fixed import (
    get_train_pairs as get_train_pairs_1,
)
from rnns_learn_robust_motor_policies.training.part2_context import (
    get_train_pairs as get_train_pairs_2,
)

from .loss import get_readout_norm_loss
from .post_training import process_model_post_training


# These are the different types of training run, i.e. respective to parts/phases of the study.
EXPERIMENTS = {
    1: get_train_pairs_1, 
    2: get_train_pairs_2,   
}

# TODO: Move to config
LOG_STEP = 500


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def train_setup(
    train_hps: TreeNamespace,
) -> tuple[TaskTrainer, AbstractLoss]:
    """Given the training hyperparameters, return a trainer object and loss function."""
    optimizer_class = partial(
        optax.adamw,
        weight_decay=train_hps.weight_decay,
    ) 

    schedule = make_delayed_cosine_schedule(
        train_hps.learning_rate_0, 
        train_hps.constant_lr_iterations, 
        train_hps.n_batches_baseline + train_hps.n_batches_condition, 
        train_hps.cosine_annealing_alpha,
    ) 

    trainer = TaskTrainer(
        optimizer=optax.inject_hyperparams(optimizer_class)(
            learning_rate=schedule,
        ),
        checkpointing=True,
    )
    
    loss_func = simple_reach_loss()
    
    if all(
        getattr(train_hps , k, None) is not None
        for k in ('readout_norm_loss_weight', 'readout_norm_value')
    ):
        readout_norm_loss = (
            train_hps.readout_norm_loss_weight 
            * get_readout_norm_loss(train_hps.readout_norm_value)
        )
        loss_func = loss_func + readout_norm_loss
    
    return trainer, loss_func


def train_pair(
    trainer: TaskTrainer, 
    pair: TaskModelPair, 
    n_batches: int,
    task_baseline: Optional[AbstractTask] = None,
    n_batches_baseline: int = 0,
    *,
    key: PRNGKeyArray,
    **kwargs,
):   
    """Given a trainer instance and a task-model pair, train the model for a given number of batches."""
    key0, key1 = jr.split(key, 2)
    
    if n_batches_baseline > 0 and task_baseline is not None:
        pretrained, pretrain_history, opt_state = trainer(
            task_baseline,
            pair.model,
            n_batches=n_batches_baseline, 
            run_label="Baseline training",
            key=key0,
            **kwargs,
        )
    else: 
        pretrained = pair.model
        pretrain_history = None
        opt_state = None
    
    trained, train_history, _ = trainer(
        pair.task, 
        pretrained,
        opt_state=opt_state,
        n_batches=n_batches, 
        idx_start=n_batches_baseline,
        run_label="Condition training",
        key=key1,
        **kwargs,
    )
    
    if pretrain_history is None:
        train_history_all = train_history
    else:
        train_history_all = jtree.concatenate([pretrain_history, train_history])
    
    return trained, train_history_all


def where_strs_to_funcs(where_strs: Sequence[str] | dict[int, Sequence[str]]):
    if isinstance(where_strs, dict):
        return {
            i: attr_str_tree_to_where_func(strs) 
            # TODO: Let the user pass a single sequence, instead of a dict of them
            for i, strs in where_strs.items()
        }
    elif isinstance(where_strs, Sequence):
        return attr_str_tree_to_where_func(where_strs)
    else:
        raise ValueError("`where_strs` must be a sequence or dict of sequences")


def train_and_save_models(
    db_session,
    config_path: str | Path, 
    key: PRNGKeyArray,
    untrained_only: bool = True,
    postprocess: bool = True,
    n_std_exclude: int = 2,  # re: postprocessing
    save_figures: bool = True,  # re: postprocessing
    version_info: Optional[dict[str, str]] = None,
):
    """Given a path to a YAML config, execute the respective training run.
    
    The config must have a top-level key `id` whose positive integer value 
    indicates which training experiment to run. 
    """
    key_init, key_train, key_eval = jr.split(key, 3)
    
    hps_common: TreeNamespace = load_hps(config_path)

    # User specifies which variant to run using the `id` key
    get_train_pairs = EXPERIMENTS[hps_common.expt_id]
    
    task_model_pairs = get_train_pairs(hps_common, key_init)
    
    # Get one set of complete hyperparameters for each task-model pair
    # (Add in the hyperparameters corresponding to the pytree levels)
    #? We might also do this inside `get_train_pairs`, and avoid needing to re-parse the 
    #? PyTree structure of `task_model_pairs` here. (See `hyperparams.TYPE_HP_KEY_MAPPING`)
    all_hps = fill_out_hps(hps_common, task_model_pairs)

    if untrained_only:
        # TODO: Optionally do post-processing for models that were previously trained, but not post-processed
        task_model_pairs = skip_already_trained(db_session, task_model_pairs, all_hps)
        
    if not any(jt.leaves(task_model_pairs, is_leaf=is_type(TaskModelPair))):
        logger.info("No models to train. Exiting.")
        # return jt.map(lambda _: None, task_model_pairs, is_leaf=is_type(TaskModelPair))
        return None, None, None
    
    # TODO: Also get `trainer`, `loss_func`, ... as trees like `task_model_pairs`
    # Otherwise certain hyperparameters (e.g. learning rate) will be constant 
    # when the user might expect them to vary due to their config file. 
    trainer, loss_func = train_setup(hps_common.train)
    
    ## Train and save all the models.
    # TODO: Is this correct? Or should we pass the task for the respective training method?
    task_baseline: AbstractTask = jt.leaves(task_model_pairs, is_leaf=is_type(TaskModelPair))[0].task

    def train_and_save_pair(pair, hps):
        trained_model, train_history = train_pair(
            trainer, 
            pair,
            hps.train.n_batches, 
            key=key_train,  #! Use the same PRNG key for all training runs
            ensembled=True,
            loss_func=loss_func,
            task_baseline=task_baseline,  
            where_train=where_strs_to_funcs(hps.train.where),
            batch_size=hps.train.batch_size, 
            log_step=LOG_STEP,
            save_model_parameters=hps.train.save_model_parameters,
            state_reset_iterations=hps.train.state_reset_iterations,
            # disable_tqdm=True,
        )
        model_record = save_model_and_add_record(
            db_session,
            trained_model,
            hps,
            train_history=train_history,
            version_info=version_info,
        )
        if postprocess:
            process_model_post_training(
                db_session,
                model_record,
                n_std_exclude,
                process_all=True,
                save_figures=save_figures,
            )
            
        return trained_model, train_history, model_record
        

    trained_models, train_histories, model_records = jtree.unzip(jtree.map_tqdm(
        train_and_save_pair,
        task_model_pairs, 
        all_hps,
        label="Training all pairs",
        is_leaf=is_type(TaskModelPair),
    ))
    
    return trained_models, train_histories, model_records
    

def concat_save_iterations(iterations: Array, n_batches_seq: Sequence[int]):
    total_batches = np.cumsum([0] + list(n_batches_seq))
    return jnp.concatenate([
        iterations[iterations < n] + total for n, total in zip(n_batches_seq, total_batches)
    ])


def does_model_record_exist(db_session, hyperparameters):
    try: 
        existing_record = get_record(db_session, ModelRecord, **hyperparameters)
    except AttributeError as e:
        raise e
        
        existing_record = None
    return existing_record is not None


def skip_already_trained(
    db_session, 
    task_model_pairs: PyTree[TaskModelPair, 'T'], 
    all_hps: PyTree[dict, 'T'],
):
    """Replace leaves in the tree of training pairs with None, where those models were already trained.
    
    Optionally 
    """
    all_hps = arrays_to_lists(all_hps)
    
    def get_query_hps(hps: TreeNamespace) -> TreeNamespace:
        hps = deepcopy(hps)
        # flatten the `model` key into the top level
        
        hps.is_path_defunct = False
        hps.postprocessed = False
        hps = promote_model_hps(hps)
 
        return hps
        
    record_exists = jt.map(
        lambda _, hps: does_model_record_exist(
            db_session, 
            namespace_to_dict(flatten_hps(get_query_hps(hps))),
        ),   
        task_model_pairs, all_hps,
         is_leaf=is_type(TaskModelPair),
    )
    
    pairs_to_skip, task_model_pairs = eqx.partition(
        task_model_pairs,
        record_exists, 
        is_leaf=is_type(TaskModelPair),
    )
    
    pairs_to_skip_flat = jt.leaves(pairs_to_skip, is_leaf=is_type(TaskModelPair))
    logger.info(
        f"Skipping training of {len(pairs_to_skip_flat)} models whose hyperparameters "
        "match models already in the database"
    )

    return task_model_pairs


def make_delayed_cosine_schedule(init_lr, constant_steps, total_steps, alpha=0.001):
    """Returns an Optax schedule that starts with constant learning rate, then cosine anneals."""
    constant_schedule = optax.constant_schedule(init_lr)
    
    cosine_schedule = optax.cosine_decay_schedule(
        init_value=init_lr,
        decay_steps=max(0, total_steps - constant_steps),
        alpha=alpha,
    )
    return optax.join_schedules(
        schedules=[constant_schedule, cosine_schedule],
        boundaries=[constant_steps]
    )