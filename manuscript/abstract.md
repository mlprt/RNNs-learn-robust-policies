
*Intro paragraph should be a bit shorter.*

Human reaching movements exhibit a fundamental **trade-off** between **efficiently** exploiting known dynamics versus **robustly** defending against unpredictable disturbances [1]. In predictable environments, our reaches resemble those generated by an optimal feedback controller (OFC). However, when faced with disturbances we cannot exactly predict, we **resist** them by increasing our reach vigour and feedback reactivity, similarly to the formalism of ~~H-infinity~~ robust control. But while the behavioral signatures of *policy robustening* are well-characterized, its neural basis remains unclear. 

*Bridge better: investigate the neural basis, mechanisms… predictions*

(Methods/results) To generate testable neural predictions, we trained a single-layer recurrent neural network (RNN) to perform straight reaches with feedback, while manipulating the unpredictability of the environment (UE) through balanced perturbations of the mechanics. The RNN, when input with scalar information about the UE (SIUE) on each training trial, learned a continuum of policies of varying robustness. Behavioural signatures on test trials reflected those seen in theory and human experiments, with their robustness manipulable by the SIUE alone. We then probed the unit responses and population dynamics of the trained network. First, \[something about unit tuning via perturbation analysis.\]. Second, the RNN’s fixed point trajectories varied with the SIUE, **being arranged along a top principle component of the population activity** (maybe more general: dimensionality reduction → systematic organization in state space). The dynamics of individual fixed points became both richer and more stable, with increasing value of the SIUE. 

*Next paragraph is better as into paragraph for Discussion but should be shorter (same content) for abstract*

~~Our results show that a single-layer RNN can vary the robustness of its reaching policy through systematic variations in its neural properties, driven by information about UE.~~ ~~While we have not determined whether or how the brain computes an analog of the SIUE, we do demonstrate that a continuum of policies of varying robustness can be generated through a~~ (The interpretation of this is…) simple and general computational shift (**be specific**) which could be implemented locally in the brain, and we provide specific, testable predictions about the neural activity in brain areas which may do so. More broadly, our results suggest that local populations of neurons across the brain may adjust their robustness or **reactivity** to ~~**external influences**, including the influences of~~ other neural populations, according to information about their unpredictability.

[1] [https://doi.org/10.1523/JNEUROSCI.0770-19.2019](https://doi.org/10.1523/JNEUROSCI.0770-19.2019)

## Omitted material

### Part 1

Describing part 1 of the experiment in the abstract means we need to clarify some tricky methodological details, and means it takes us longer to get to the point, since part 1 is not relevant to the neural predictions.

> Networks trained separately, each at a fixed level of unpredictability, learned policies whose robustness scaled with unpredictability and showed the same behavioral signatures seen in theory and human experiments. 

### Different types of perturbations

It might be worth adding a sentence about how robustening depends on the type of perturbation, but the same general changes to behavioural signatures tend to be seen when testing on the same kinds of perturbations as seen during training.

### Feedbax

1. This is not the focus of the paper. 
2. It will certainly be addressed in the intro/methods.
3. Maybe it should be mentioned briefly in the abstract, anyway.