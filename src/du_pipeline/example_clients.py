"""Secret-free executable plugin examples used to validate factory wiring.

These deliberately do no I/O. Replace ``placeholder_factory`` with a factory
returning a concrete SDK wrapper for activation.
"""
def placeholder_factory(kind):
    return object()