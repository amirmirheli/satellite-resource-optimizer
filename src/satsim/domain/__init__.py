"""Domain layer: pure data (enums + frozen dataclasses), no I/O and no side effects.

These types are the vocabulary exchanged across every port boundary. Keeping them
free of behavior and dependencies is what lets the ports stay thin and the fakes
stay trivial.
"""
