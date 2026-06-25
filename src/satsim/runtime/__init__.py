"""Runtime services: the Tier-1 control loop, the experiment/run API, and the CLI entry point.

These compose the domain, ports, and adapters into something runnable. They depend on adapters;
nothing in the lower layers depends back on them.
"""
