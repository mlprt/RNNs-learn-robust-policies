import dataclasses
from functools import cached_property
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Optional, Dict
from pathlib import Path
import yaml

import equinox as eqx
from equinox import AbstractVar, Module
import jax.tree as jt
from jaxtyping import PyTree, Array
import plotly.graph_objects as go
from sqlalchemy.orm import Session

from jax_cookbook import is_type
import jax_cookbook.tree as jtree

from rnns_learn_robust_motor_policies.database import EvaluationRecord, add_evaluation_figure, savefig
from rnns_learn_robust_motor_policies.tree_utils import tree_level_labels
from rnns_learn_robust_motor_policies.misc import camel_to_snake, get_dataclass_fields, is_json_serializable
from rnns_learn_robust_motor_policies.plot_utils import figs_flatten_with_paths
from rnns_learn_robust_motor_policies.types import TreeNamespace

if TYPE_CHECKING:
    from typing import ClassVar as AbstractClassVar
else:
    from equinox import AbstractClassVar


logger = logging.getLogger(__name__)


# Define a string representer for objects PyYAML doesn't know how to handle
def represent_undefined(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', str(data))
yaml.add_representer(object, represent_undefined)


def create_analysis(analysis_class, *args, **kwargs):
    provided = kwargs.copy()
    for i, arg in enumerate(args):
        sig = inspect.signature(analysis_class.__init__)
        param_names = list(sig.parameters.keys())[1:]  # Skip 'self'
        if i < len(param_names):
            provided[param_names[i]] = arg
    
    # Create instance with captured args
    instance = analysis_class(*args, **kwargs)
    
    # Set _provided_fields using Equinox's update mechanism
    instance = eqx.tree_at(lambda x: x._provided_fields, instance, provided)
    return instance


class AnalysisInputData(Module):
    models: PyTree[Module]
    tasks: PyTree[Module]
    states: PyTree[Module]
    hps: PyTree[TreeNamespace]  
    extras: PyTree[TreeNamespace] 


class AbstractAnalysis(Module):
    """Component in an analysis pipeline.
    
    In `run_analysis`, multiple sets of evaluations may be performed
    prior to analysis. In particular, we may evaluate a full/large set
    of task conditions for statistical purposes, and evaluate a smaller 
    version for certain visualizations. Thus `AbstractAnalysis` 
    subclasses expect arguments `models`, `tasks`, `states`, and `hps` all 
    of which are PyTrees. The top-level structure of these PyTrees is always 
    a 
    
    Now, while it may be the case that an analysis would depend on both the 
    larger and smaller variants (in our example), we still must specify only a 
    single `variant`, since this determines the hyperparameters that are passed 
    to `analysis.save`. Thus it is assumed that all figures that result from a 
    call to some `AbstractAnalysis.make_figs` will be best associated with only
    one (and always the same one) of the eval variants.
    
    TODO: If we return the hps on a fig-by-fig basis from within `make_figs`, then 
    we could avoid this limitation.    
    
    Abstract class attributes:
        dependencies: Specifies the subclasses of `AbstractAnalysis`
            whose results are needed for this subclass of `AbstractAnalysis`.
        variant: Label of the evaluation variant this analysis uses (primarily).
    
    Abstract fields:
        conditions: In `run_analysis`, certain condition checks are performed. The 
            analysis is only run if all of the checks whose keys are in `conditions`
            are successful. For example, certain figures may only make sense to generate
            when there is system noise (i.e. multiple evals per condition), and in 
            that case we could give the condition `"any_system_noise"` to those analyses.
    """
    _exclude_fields = ('dependencies', 'conditions')
    dependencies: AbstractClassVar[MappingProxyType[str, "type[AbstractAnalysis]"]]
    variant: AbstractVar[Optional[str]] 
    conditions: AbstractVar[tuple[str, ...]]
    
    def __call__(
        self, 
        data: AnalysisInputData,
        **kwargs,
    ) -> tuple[PyTree[Any], PyTree[go.Figure]]:
        result = self.compute(data, **kwargs)
        figs = self.make_figs(data, result=result, **kwargs)
        return result, figs
        
    def compute(
        self, 
        data: AnalysisInputData,
        **kwargs,
    ) -> Optional[PyTree[Any]]:
        return 
    
    def make_figs(
        self, 
        data: AnalysisInputData,
        *,
        result: Optional[Any],
        **kwargs,
    ) -> Optional[PyTree[go.Figure]]:
        return 
    
    @property
    def name(self):
        return self.__class__.__name__
    
    def _params_to_save(self, hps: PyTree[TreeNamespace], **kwargs):
        """Additional parameters to save.
        
        Note that `**kwargs` here may not only contain the dependencies, but that `save` 
        passes the key-value pairs of parameters inferred from the `figs` PyTree. 
        Thus for example `train_pert_std` is explicitly referred to in the argument list of 
        `plant_perts.Effector_ByEval._params_to_save`.
        """
        return dict()

    def dependency_kwargs(self) -> Dict[str, Dict[str, Any]]:
        """Return kwargs to be used when instantiating dependencies.
        
        Subclasses can override this method to provide parameters for their dependencies.
        Returns a dictionary mapping dependency name to a dictionary of kwargs.
        """
        return {}

    def save_figs(
        self, 
        db_session: Session, 
        eval_info: EvaluationRecord, 
        result, 
        figs: PyTree[go.Figure],   
        hps: PyTree[TreeNamespace], 
        model_info=None,
        dump_path: Optional[Path] = None,
        **dependencies,
    ):
        """Save to disk and record in the database each figure in a PyTree of figures, for this analysis.
        """
        # `sep="_"`` switches the label dunders for single underscores, so 
        # in `_params_to_save` we can use an argument e.g. `train_pert_std` rather than `train__pert__std`
        param_keys = tree_level_labels(figs, is_leaf=is_type(go.Figure), sep="_")
        
        if dump_path is not None:
            dump_path = Path(dump_path)
            dump_path.mkdir(exist_ok=True, parents=True)
        
        figs_with_paths_flat = figs_flatten_with_paths(figs)
        
        # Construct this for reference to hps that should only vary with the task variant.
        hps_0 = jt.leaves(hps[self.variant], is_leaf=is_type(TreeNamespace))[0]
        
        for i, (path, fig) in enumerate(figs_with_paths_flat):
            path_params = dict(zip(param_keys, tuple(jtree.node_key_to_value(p) for p in path)))
            
            # Include fields from this instance, but only if they are JSON serializable
            field_params = {k: v for k, v in self._field_params.items() if is_json_serializable(v)}
            
            params = dict(
                **path_params,  # Inferred from the structure of the figs PyTree
                **field_params,  # From the fields of this subclass
                **self._params_to_save(
                    hps, 
                    result=result, 
                    **path_params, 
                    **dependencies,  # Specified by the subclass `dependency_kwargs`, via `run_analysis`
                ),  
                eval_n=hps_0.eval_n,  #? Some things should always be included
            )
            
            add_evaluation_figure(
                db_session, 
                eval_info, 
                fig, 
                camel_to_snake(self.__class__.__name__), 
                model_records=model_info, 
                **params,
            )
            
            # Include any fields that have non-default values in the filename; 
            # this serves to distinguish different instances of the same analysis,
            # according to the kwargs passed by the user upon instantiation.
            # TODO: Exclude non-determining fields like `legend_title` 
            # TODO: (could group all the figure layout kwargs under a single field and exclude it)
            non_default_field_params_str = '__'.join([
                f"{k}-{v}" for k, v in self._non_default_field_params.items()
            ])
            
            # Additionally dump to specified path if provided
            if dump_path is not None:                                
                # Create a unique filename
                analysis_name = camel_to_snake(self.__class__.__name__)
                filename = f"{analysis_name}__{self.variant}__{non_default_field_params_str}__{i}"
                
                savefig(fig, filename, dump_path, ["html"])
                
                # Save parameters as YAML
                params_path = dump_path / f"{filename}.yaml"
                with open(params_path, 'w') as f:
                    yaml.dump(params, f, default_flow_style=False, sort_keys=False)
                    
    @cached_property
    def _field_params(self):
        # TODO: Inherit from dependencies? e.g. if we depend on `BestReplicateStates`, maybe we should include `i_replicate` from there
        return get_dataclass_fields(self, exclude=AbstractAnalysis._exclude_fields)

    @property
    def _non_default_field_params(self) -> Dict[str, Any]:
        """
        Returns a dictionary of fields that have non-default values.
        Works without knowing field names in advance.
        """
        result = {}
        
        # Get all dataclass fields for this instance
        for field in dataclasses.fields(self):
            if field.name in AbstractAnalysis._exclude_fields:
                continue

            current_value = getattr(self, field.name)
            
            # Check if this field has a default value defined
            has_default = field.default is not dataclasses.MISSING
            has_default_factory = field.default_factory is not dataclasses.MISSING
            
            if has_default and current_value != field.default:
                # Field has a different value than its default
                result[field.name] = current_value
            elif has_default_factory:
                # For default_factory fields, we can't easily tell if the value
                # was explicitly provided, so we include the current value
                # This is an approximation - we'll include fields with default_factory
                result[field.name] = current_value
            elif not has_default and not has_default_factory:
                # Field has no default, so it must have been provided
                result[field.name] = current_value
                
        return result