from importlib import resources
import os
from pathlib import Path
from typing import Literal, Optional 
import yaml


CONFIG_DIR_ENV_VARIABLE_NAME = 'RLRMP_CONFIG_DIR'


def get_user_config_dir():
    """Get user config directory from environment variable or return None"""
    env_config_dir = os.environ.get(CONFIG_DIR_ENV_VARIABLE_NAME)
    if env_config_dir is None:
        return 
    else:
        return Path(env_config_dir).expanduser() 


def load_config(path: str):
    with open(path) as f:
        return yaml.safe_load(f)


def load_named_config(name: str, config_type: Optional[Literal['training', 'analysis']] = None):
    user_config_dir = get_user_config_dir()
    
    # If the user has specified a config directory, try to load the paths config from it
    if user_config_dir is not None:
        try:
            with open(user_config_dir / f'{name}.yml') as f:
                return yaml.safe_load(f)
        except:  # TODO
            pass
    
    if config_type is None:
        subpackage_name = 'rnns_learn_robust_motor_policies.config'
    else:
        subpackage_name = f'rnns_learn_robust_motor_policies.config.{config_type}'
    
    # Otherwise, load the default
    with resources.open_text(subpackage_name, f'{name}.yml') as f:
        return yaml.safe_load(f)