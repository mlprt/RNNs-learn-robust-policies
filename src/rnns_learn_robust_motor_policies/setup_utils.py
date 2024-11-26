from pathlib import Path
import time
from typing import Literal, Optional
import fnmatch
import html
import json 
import os

import equinox as eqx
from ipyfilechooser import FileChooser
from ipywidgets import HTML
from IPython.display import display
import jax.numpy as jnp
import jax.tree as jt

from feedbax import is_type, is_module
from feedbax.loss import AbstractLoss, ModelLoss
from feedbax.misc import attr_str_tree_to_where_func
from feedbax.noise import Multiplicative, Normal
from feedbax.task import SimpleReaches
from feedbax.train import TaskTrainerHistory, init_task_trainer_history
from feedbax._tree import tree_zip_named, tree_unzip
from feedbax.xabdeef.losses import simple_reach_loss

from rnns_learn_robust_motor_policies import MODELS_DIR
from rnns_learn_robust_motor_policies.constants import (
    TASK_EVAL_PARAMS,
    N_STEPS,
    WORKSPACE,
)


def get_base_task(
    n_steps: int = N_STEPS,
    loss_func: AbstractLoss = simple_reach_loss(),
) -> SimpleReaches:
    return SimpleReaches(
        loss_func=loss_func,
        workspace=WORKSPACE, 
        n_steps=n_steps,
        **TASK_EVAL_PARAMS['full'], 
    )


readout_norm_func = lambda weights: jnp.linalg.norm(weights, axis=(-2, -1), ord='fro')
get_readout_norm_loss = lambda value: ModelLoss(
    "readout_norm",
    lambda model: (readout_norm_func(model.step.net.readout.weight) - value) ** 2
)


def setup_train_histories(
    models_tree,
    disturbance_stds,
    n_batches,
    batch_size,
    n_replicates,
    *,
    where_train_strs,
    save_model_parameters,
    readout_norm_value=None,
    readout_norm_loss_weight=None,
    key,
) -> dict[float, TaskTrainerHistory]:
    """Returns a skeleton PyTree for the training histories (losses, parameter history, etc.)
    
    Note that `init_task_trainer_history` depends on `task` to infer:
    
    1) The number and name of loss function terms;
    2) The structure of trial specs, in case `save_trial_specs is not None`.
    
    Here, neither of these are a concern since 1) we are always using the same 
    loss function for each set of saved/loaded models in this project, 2) `save_trial_specs is None`.
    """   
    # Assume that where funcs may be lists (normally defined as tuples, but retrieved through sqlite JSON)
    where_train = jt.map(
        attr_str_tree_to_where_func, 
        where_train_strs,
        is_leaf=is_type(list),
    )
    
    loss_func = simple_reach_loss()
    if readout_norm_loss_weight is not None:
        assert readout_norm_value is not None, (
            "readout_norm_value must be provided if readout_norm_loss_weight is not None"
        )
        loss_func_validation = loss_func + readout_norm_loss_weight * get_readout_norm_loss(readout_norm_value)
    else:
        loss_func_validation = loss_func
    
    return jt.map(
        lambda models: init_task_trainer_history(
            loss_func,
            n_batches,
            n_replicates,
            ensembled=True,
            ensemble_random_trials=False,
            save_model_parameters=jnp.array(save_model_parameters),
            save_trial_specs=None,
            batch_size=batch_size,
            loss_func_validation=loss_func_validation,
            model=models,
            where_train=where_train,  
        ),
        models_tree,
        is_leaf=is_module,
    )


def get_latest_matching_file(directory: str, pattern: str) -> Optional[str]:
    """
    Returns the filename of the latest file in the given directory that matches the given pattern.

    The 'latest' file is determined by sorting the filenames in descending order.

    Arguments:
        directory: The directory path to search in.
        pattern: The pattern to match filenames against (e.g., 'A-*.json').

    Returns:
        The filename of the latest matching file, or None if no match is found.

    Raises:
        OSError: If there's an error reading the directory.
    """
    try:
        all_files = os.listdir(directory)
    except OSError as e:
        print(f"Error reading directory {directory}: {e}")
        return None

    matching_files = fnmatch.filter(all_files, pattern)

    if not matching_files:
        return None

    sorted_files = sorted(matching_files, reverse=True)

    return sorted_files[0]


def display_model_filechooser(path, filter_pattern='*.eqx',):
    """Display a file chooser interface for the files at `path` whose names satisfy `filter_pattern`.
    
    The default filename is the one that sorts last.
    """
    fc = FileChooser(path)
    fc.filter_pattern = filter_pattern
    fc.title = "Select model file:"
    params_widget = HTML("")
    
    default_filename = get_latest_matching_file(path, fc.filter_pattern)
    if default_filename is not None:
        fc.default_filename = default_filename

    def display_params(path, html_widget):
        with open(path, 'r') as f:
            params = json.load(f)
        params_str = eqx.tree_pformat(params, truncate_leaf=lambda x: isinstance(x, list) and len(x) > 10)
        html_widget.value = '<pre>' + params_str.replace(':\n', ':') + '</pre>'       
    
    def display_params_callback(fc: Optional[FileChooser]):
        if fc is None:
            return
        if fc.selected is None:
            raise RuntimeError("")
        return display_params(
            fc.selected.replace('trained_models.eqx', 'hyperparameters.json'),
            params_widget,
        )
        
    fc.register_callback(display_params_callback)

    display(fc, params_widget)
    
    return fc


def wait_for_value(variable, timeout: float = 3600):
    end_time = time.monotonic() + timeout
    while variable is None:
        if time.monotonic() > end_time:
            return False  # Timeout occurred
        time.sleep(0.1)
    return True


def choose_model_file(filter_pattern="*.eqx", timeout: float = 3600) -> str:
    """Displays a file chooser in the model directory until """
    fc = display_model_filechooser(MODELS_DIR, filter_pattern=filter_pattern)
    
    if wait_for_value(fc, timeout=timeout):
        assert fc.selected is not None
        return fc.selected
    else:
        return f"{fc.default_path}/{fc.default_filename}"


def find_unique_filepath(path: str | Path, search_string: str) -> Optional[Path]:
    """
    Returns the path of the unique file in a directory whose filename contains a given string.

    Arguments:
        directory: The path to the directory to search in.
        search_string: The string to search for in filenames.

    Returns:
        The path of the unique file if found, None otherwise.
    """
    # Convert directory to Path object if it's a string
    dir_path = Path(path) if isinstance(path, str) else path
    
    matching_files = [
        filename for filename in dir_path.iterdir()
        if filename.is_file() and search_string.lower() in filename.name.lower()
    ]

    if len(matching_files) == 1:
        return matching_files[0]
    elif len(matching_files) == 0:
        print(f"No files found containing '{search_string}'.")
        return None
    else:
        print(f"Multiple files found containing '{search_string}':")
        for file in matching_files:
            print(file.name)
        return None


def filename_join(strs, joinwith="__"):
    """Helper for formatting filenames from lists of strings."""
    return joinwith.join(s for s in strs if s)


def set_model_noise(
    model, 
    noise_stds: dict[Literal['feedback', 'motor'], Optional[float]], 
    enable_noise: bool = True,
):
    """Change the system noise strength of a model."""
    get_noise_funcs = dict(
        feedback=lambda std: Normal(std=std),
        motor=lambda std: Multiplicative(Normal(std=std)) + Normal(std=1.8 * std),
    )
    
    noise_funcs = jt.map(
        lambda std, get_noise_func: get_noise_func(std),
        noise_stds, get_noise_funcs,
    )
    
    wheres = dict(
        feedback=lambda model: model.step.feedback_channels[0].noise_func,
        motor=lambda model: model.step.efferent_channel.noise_func,
    )
    
    pairs, LeafTuple = tree_zip_named(
        noise_func=noise_funcs,
        where=wheres, 
        is_leaf=is_module,
    )
    
    for noise_func, where in jt.leaves(pairs, is_leaf=is_type(LeafTuple)):
        model = eqx.tree_at(where, model, noise_func)
    
    if enable_noise:
        model = eqx.tree_at(
            lambda model: (
                model.step.feedback_channels[0].add_noise,
                model.step.efferent_channel.add_noise,
            ),
            model,
            (True, True),
        )
    
    return model
    

def setup_models_only(task_model_pair_setup_func, **kwargs):
    """Given a function that returns task-model pairs, just get the models."""
    task_model_pairs = task_model_pair_setup_func(**kwargs)
    _, models = tree_unzip(task_model_pairs)
    return models    


def setup_tasks_only(task_model_pair_setup_func, **kwargs):
    """Given a function that returns task-model pairs, just get the tasks."""
    task_model_pairs = task_model_pair_setup_func(**kwargs)
    tasks, _ = tree_unzip(task_model_pairs)
    return tasks


def convert_tasks_to_small(tasks):
    """Given a PyTree of tasks, return a matching PyTree where each task uses the small set of validation trials."""
    return jt.map(
        lambda task: eqx.tree_at(
            lambda task: tuple(getattr(task, k) for k in TASK_EVAL_PARAMS['small']),
            task, 
            tuple(TASK_EVAL_PARAMS['small'].values()),
        ),
        tasks,
        is_leaf=is_module,
    )
    
    
