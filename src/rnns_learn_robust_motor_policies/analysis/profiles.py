
from types import MappingProxyType
from typing import ClassVar, Optional

import feedbax.plotly as fbp
import jax.tree as jt
import jax_cookbook.tree as jtree
import numpy as np
from equinox import Module
from jax_cookbook import is_type
from jaxtyping import PyTree

from rnns_learn_robust_motor_policies.analysis.aligned import AlignedVars
from rnns_learn_robust_motor_policies.analysis.analysis import AbstractAnalysis, AnalysisInputData
from rnns_learn_robust_motor_policies.plot_utils import get_label_str
from rnns_learn_robust_motor_policies.types import Responses
from rnns_learn_robust_motor_policies.tree_utils import TreeNamespace
from rnns_learn_robust_motor_policies.types import LDict


class VelocityProfiles(AbstractAnalysis):
    """Generates forward and lateral velocity profile figures.
    """
    dependencies: ClassVar[MappingProxyType[str, type[AbstractAnalysis]]] = MappingProxyType(dict(
        aligned_vars=AlignedVars,
    ))
    variant: Optional[str] = "full"
    conditions: tuple[str, ...] = ()

    def compute(
        self,
        data: AnalysisInputData,
        *,
        aligned_vars,
        **kwargs,
    ):
        return jt.map(
            lambda responses: responses.vel,
            aligned_vars[self.variant],
            is_leaf=is_type(Responses),
        )

    def make_figs(
        self,
        data: AnalysisInputData,
        *,
        result,
        colors,
        **kwargs,
    ):
        def _get_fig(fig_data, i, label, colors):                      
            return fbp.profiles(
                jtree.take(fig_data, i, -1),
                varname=f"{label} velocity",
                legend_title=get_label_str(fig_data.label),
                mode='std', # or 'curves'
                n_std_plot=1,
                hline=dict(y=0, line_color="grey"),
                colors=list(colors[fig_data.label]['dark'].values()),
                # stride_curves=500,
                # curves_kws=dict(opacity=0.7),
                layout_kws=dict(
                    width=600,
                    height=400,
                    legend_tracegroupgap=1,
                ),
            )
        
        figs = LDict.of(result.label)({
            value: LDict.of("direction")({
                label.lower(): _get_fig(result[value], i, label, colors[self.variant][value])
                for i, label in enumerate(("Forward", "Lateral"))
            })
            for value in result.keys()
        })
        return figs

    def _params_to_save(self, hps: PyTree[TreeNamespace], *, result, **kwargs):
        return dict(
            n=int(np.prod(jt.leaves(result)[0].shape[:-2]))
        )