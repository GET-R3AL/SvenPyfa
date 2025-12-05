# ===============================================================================
# getter_helpers.py - Bridge module for backward compatibility
# ===============================================================================
#
# This module provides the mixin classes and getter classes needed by graph.py
# while the core calculation functions are being refactored in getter.py.
#
# The mixin classes build caches and use the calculation functions to compute
# optimal ammo DPS/volley at each distance point.
#
# ===============================================================================

from bisect import bisect_right

# Import everything we need from getter_old for now
# These are the complex classes that depend on many helper functions
from graphs.data.fitAmmoOptimalDps.getter_old import (
    # Mixin classes
    YOptimalAmmoDpsMixin,
    YOptimalAmmoVolleyMixin,
    XDistanceMixin,
    # Getter classes (the ones graph.py needs)
    Distance2OptimalAmmoDpsGetter,
    Distance2OptimalAmmoVolleyGetter,
    # Helper functions used by graph.py
    get_missile_flight_multipliers_from_module,
)


def get_ammo_name_at_distance_fast(transitions, distance):
    """
    Fast lookup of ammo name at a distance using pre-computed transitions.
    
    Uses bisect for O(log n) lookup.
    
    Args:
        transitions: List of (distance, charge_index, charge_name, value) tuples
        distance: Distance in meters
    
    Returns:
        Ammo name (str) or None if no transitions
    """
    if not transitions:
        return None
    
    # Find the transition that applies at this distance
    distances = [t[0] for t in transitions]
    idx = bisect_right(distances, distance) - 1
    if idx < 0:
        idx = 0
    
    # Return the ammo name from the transition
    return transitions[idx][2]
