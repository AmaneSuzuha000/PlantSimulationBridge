"""Experiment layer: instance I/O, engines, runner, metrics, validity, storage.

The experiment layer is the only place that writes experiment records. It is
backend-agnostic: it drives a DispatchEngine (psbridge.experiment.engine).
"""
