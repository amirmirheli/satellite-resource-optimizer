"""satsim — satellite resource-optimization control-plane simulator.

Hexagonal (ports & adapters) architecture: the control logic depends only on the
interfaces in :mod:`satsim.ports`; every external system has a license-free fake.
"""

__version__ = "0.1.0"
