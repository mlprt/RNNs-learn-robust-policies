from collections import namedtuple
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any, Dict, Generic, NamedTuple, TypeVar, overload
import jax
import jax.tree_util as jtu
import yaml


TaskModelPair = namedtuple("TaskModelPair", ["task", "model"])


K = TypeVar('K')
V = TypeVar('V')


@jax.tree_util.register_pytree_with_keys_class
class LDict(Mapping[K, V], Generic[K, V]):
    """Immutable dictionary with a string label for distinguishing dictionary types.
    
    Our PyTrees will contain levels corresponding to training conditions (standard deviation
    of disturbance amplitude during training), evaluation conditions (disturbance
    amplitudes during analysis), and so on. Associating a label with a mapping will allow us 
    to identify and map over specific levels of these PyTrees, as well as to keep track of the 
    names of hyperparameters stored in the PyTree, e.g. so we can automatically determine 
    which columns to store those hyperparameters in, in the DB.
    """
    
    def __init__(self, label: str, data: Mapping[K, V]):
        self._label = label
        self._data = dict(data)  
    
    @property
    def label(self) -> str:
        return self._label
    
    def __getitem__(self, key: K) -> V:
        return self._data[key]
    
    def __iter__(self):
        return iter(self._data)
    
    def __len__(self) -> int:
        return len(self._data)
    
    def __repr__(self) -> str:
        return f"LDict({repr(self._label)}, {self._data})"
    
    def tree_flatten_with_keys(self):
        children_with_keys = [(jtu.DictKey(k), v) for k, v in self.items()]
        return children_with_keys, (self._label, self.keys())
    
    @classmethod
    def tree_unflatten(cls, aux_data, children):
        label, keys = aux_data
        return cls(label, dict(zip(keys, children)))
    
    def items(self):
        return self._data.items()
    
    def keys(self):
        return self._data.keys()
    
    def values(self):
        return self._data.values()
    
    def get(self, key, default=None):
        return self._data.get(key, default)

    @staticmethod
    def of(label: str) -> Callable[[Mapping[K, V]], 'LDict[K, V]']:
        """Returns a constructor function for the given label."""
        class LDictConstructor:
            def __call__(self, data: Mapping[K, V]) -> 'LDict[K, V]':
                return LDict(label, data)
                
            def fromkeys(self, keys: Iterable[Any], value: Any = None) -> 'LDict':
                return LDict.fromkeys(label, keys, value)
                
        return LDictConstructor()
    
    @staticmethod
    def is_of(label: str) -> Callable[[Any], bool]:
        """Return a predicate checking if a node is a LDict with a specific label."""
        return lambda node: isinstance(node, LDict) and node.label == label
    
    @staticmethod
    @overload
    def fromkeys(label: str, keys: Iterable[K]) -> 'LDict[K, None]': ...
    
    @staticmethod
    @overload
    def fromkeys(label: str, keys: Iterable[K], value: V) -> 'LDict[K, V]': ...
    
    @staticmethod
    def fromkeys(label: str, keys: Iterable[Any], value: Any = None) -> 'LDict[Any, Any]':
        """Create a new LDict with the given label and keys, each with value set to value."""
        return LDict(label, dict.fromkeys(keys, value))


# YAML serialisation/deserialisation for LDict objects
def _ldict_representer(dumper, data):
    # Store both the label and the dictionary data
    # Format: !LDict:label {key1: value1, key2: value2, ...}
    return dumper.represent_mapping(f"!LDict:{data.label}", data._data)

yaml.add_representer(LDict, _ldict_representer)

def _ldict_multi_constructor(loader, tag_suffix, node):
    # Extract the label from the tag suffix (after the colon)
    label = tag_suffix
    mapping = loader.construct_mapping(node)
    return LDict(label, mapping)

yaml.SafeLoader.add_multi_constructor('!LDict:', _ldict_multi_constructor)
    

class ImpulseAmpTuple(tuple, Generic[K, V]):
    def __repr__(self):
        return f"ImpulseAmpTuple({tuple.__repr__(self)})"
    
    
for cls in (ImpulseAmpTuple,): 
    jtu.register_pytree_node(
        cls, 
        lambda x: (x, None), 
        lambda _, children: cls(children)  # type: ignore
    ) 


# TODO: Rename to Effector, or something
class ResponseVar(str, Enum):
    """Variables available in response state."""
    POSITION = 'pos'
    VELOCITY = 'vel'
    FORCE = 'force'


class Direction(str, Enum):
    """Available directions for vector components."""
    PARALLEL = 'parallel'
    ORTHOGONAL = 'orthogonal'


DIRECTION_IDXS = {
    Direction.PARALLEL: 0,
    Direction.ORTHOGONAL: 1,
}


class Responses(NamedTuple):
    pos: Any
    vel: Any
    force: Any


RESPONSE_VAR_LABELS = Responses('Position', 'Velocity', 'Control force')
            

RESPONSE_VAR_LABELS_SHORT = Responses(
    pos='p',
    vel='v',
    force='F',
)